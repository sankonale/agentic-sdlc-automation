"""Generation agents: CodeGenerator, TestGenerator, ChangeImplementer.

Each generator writes into the sandboxed staging workspace only. Content is
scanned by guardrails before the Validator ever executes it. When a task is
re-run because validation routed a defect back to it, `task.feedback` holds
the failing checks/tests so the generator can repair the specific output.
"""
from __future__ import annotations

import difflib
import json

from ..guardrails import scan_content
from ..models import Artifact, Task
from ..packs import legacy_expiry as lx
from ..packs import url_shortener as us
from .base import Agent, AgentContext

SERVICE = "service"


def _guard(path: str, content: str) -> None:
    errs = [f for f in scan_content(path, content) if f.severity == "error"]
    if errs:
        raise RuntimeError(f"guardrail blocked {path}: {errs[0].message}")


class CodeGenerator(Agent):
    name = "code_generator"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        spec = ctx.blackboard.require("spec")
        comp = task.inputs["component"]
        if task.feedback:
            ctx.tracer.emit("info", f"{task.id}: repairing using {len(task.feedback)} validator note(s)")
        if spec["domain"] != us.DOMAIN:
            return self._llm_generate(task, ctx, spec, comp)
        arts: list[Artifact] = []
        if comp == "ops":
            for rel, content in us.OPS_FILES.items():
                arts.append(self.write(ctx, task, f"{SERVICE}/{rel}", content, "config"))
            ctx.blackboard.put("code:ops", [a.path for a in arts], producer=task.id)
            return arts

        files = {rel: us.render(rel, spec["parameters"]) for rel in us.COMPONENTS[comp]}
        if comp == "api":   # consistent error envelope for request-validation errors too
            files["app/api.py"] = files["app/api.py"].replace(
                "    @app.exception_handler(ShortenerError)",
                "    @app.exception_handler(RequestValidationError)\n"
                "    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:\n"
                "        first = exc.errors()[0] if exc.errors() else {}\n"
                "        loc = \".\".join(str(x) for x in first.get(\"loc\", []) if x != \"body\")\n"
                "        return error(422, \"validation_error\", f\"{loc}: {first.get('msg', 'invalid request')}\")\n\n"
                "    @app.exception_handler(ShortenerError)").replace(
                "from fastapi.responses import", "from fastapi.exceptions import RequestValidationError\n"
                "from fastapi.responses import")

        if ctx.faults.take("defect", task.id) and comp in us.DEFECTS:
            rel, good, bad = us.DEFECTS[comp]
            assert good in files[rel], f"defect anchor missing in {rel}"
            files[rel] = files[rel].replace(good, bad)
            ctx.tracer.emit("info", f"{task.id}: [fault injection] emitted a subtle defect in {rel}")

        for rel, content in files.items():
            _guard(rel, content)
            arts.append(self.write(ctx, task, f"{SERVICE}/{rel}", content, "code"))
        ctx.blackboard.put(f"code:{comp}", [a.path for a in arts], producer=task.id)
        return arts

    def _llm_generate(self, task: Task, ctx: AgentContext, spec: dict, comp: str) -> list[Artifact]:
        if not ctx.llm.available:
            raise RuntimeError(f"no skill pack for domain '{spec['domain']}' and no LLM provider configured "
                               "(run with --llm anthropic)")
        out = ctx.llm.complete_json(
            "You are a senior Python engineer. Produce production-quality code.",
            json.dumps({"task": task.title, "component": comp, "spec": spec, "feedback": task.feedback,
                        "architecture": ctx.blackboard.get("architecture"),
                        "format": {"files": {"relative/path.py": "content"}}}))
        if not isinstance(out, dict) or not isinstance(out.get("files"), dict):
            raise RuntimeError("LLM returned no usable files")
        arts = []
        for rel, content in out["files"].items():
            _guard(rel, str(content))
            arts.append(self.write(ctx, task, f"{SERVICE}/{rel}", str(content), "code"))
        return arts


class TestGenerator(Agent):
    name = "test_generator"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        suite = task.inputs["suite"]
        spec = ctx.blackboard.require("spec")
        arts: list[Artifact] = []
        if suite == "brownfield":
            for rel, content in lx.NEW_TESTS.items():
                arts.append(self.write(ctx, task, f"patched_repo/{rel}", content, "test"))
            ctx.blackboard.put("tests:brownfield", [a.path for a in arts], producer=task.id)
            ctx.blackboard.put("test_targets_bf", {
                "tests/test_expiry_and_collisions.py": ["shortener/core.py", "shortener/store.py",
                                                         "shortener/web.py"],
                lx.REPRO_TEST_PATH: ["shortener/core.py"]}, producer=task.id)
            return arts
        if spec["domain"] != us.DOMAIN:
            raise RuntimeError("no test pack for this domain and no LLM provider configured")
        for rel in us.TEST_SUITES[suite]:
            arts.append(self.write(ctx, task, f"{SERVICE}/{rel}", us.render(rel, spec["parameters"]), "test"))
        targets = dict(ctx.blackboard.get("test_targets", {}) or {})
        targets.update({k: v for k, v in us.TEST_TARGETS.items() if k in us.TEST_SUITES[suite]})
        ctx.blackboard.put("test_targets", targets, producer=task.id)
        ctx.blackboard.put(f"tests:{suite}", [a.path for a in arts], producer=task.id)
        return arts


class ChangeImplementer(Agent):
    """Applies a change to a sandbox copy of the target repo and emits a unified diff."""

    name = "change_implementer"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        model = ctx.blackboard.require("codebase_model")
        design = ctx.blackboard.require("change_design")
        unmet = lx.matches(model)
        if unmet:
            if not ctx.llm.available:
                raise RuntimeError(f"recipe preconditions unmet ({', '.join(unmet[:3])}) and no LLM provider; "
                                   "refusing to guess edits")
            raise RuntimeError("LLM-driven brownfield edits are not enabled in this prototype")
        if task.feedback:
            ctx.tracer.emit("info", f"{task.id}: repairing using {len(task.feedback)} validator note(s)")

        ws = ctx.workspace
        base = ws.resolve("baseline_repo")
        patched = ws.copy_tree_in(base, "patched_repo")
        files = dict(lx.FILES)
        if ctx.faults.take("defect", task.id):
            rel, good, bad = lx.DEFECT
            files[rel] = files[rel].replace(good, bad)
            ctx.tracer.emit("info", f"{task.id}: [fault injection] emitted a subtle defect in {rel}")

        diffs: list[str] = []
        arts: list[Artifact] = []
        for rel, new in files.items():
            _guard(rel, new)
            old = (base / rel).read_text()
            (patched / rel).write_text(new)
            arts.append(Artifact(f"patched_repo/{rel}", "code", task.id, "modified"))
            diffs.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                              fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        patch = "".join(diffs)
        arts.append(self.write(ctx, task, "changes.patch", patch, "patch", "unified diff against the source repo"))
        stats = {"files_changed": len(files), "insertions": sum(1 for line in diffs if line.startswith("+")
                                                                and not line.startswith("+++")),
                 "deletions": sum(1 for line in diffs if line.startswith("-") and not line.startswith("---"))}
        ctx.blackboard.put("patch", {"path": "changes.patch", "files": list(files), "stats": stats,
                                     "design_items": len(design["plan"])}, producer=task.id)
        return arts
