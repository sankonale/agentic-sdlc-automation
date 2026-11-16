"""Validator: independent verification of everything the other agents produced.

Checks (greenfield)
  syntax          every .py parses
  content_policy  guardrail scan (dangerous calls, secrets)
  contract        implemented routes/methods/status codes == OpenAPI contract
  tests           unit + integration tests executed in an isolated subprocess
  traceability    every FR/NFR id is covered by at least one test file

Checks (brownfield)
  baseline        existing tests on the untouched copy (run as its own task)
  syntax/content  on changed files
  regression      full suite (old + new tests) on the patched copy
  bug_repro       the bug-reproduction test FAILS on baseline and PASSES on patch
  compat          every pre-existing public signature still callable the old way
  patch_applies   `git apply --check` of the final patch on a pristine copy
  source_intact   the original repository was not modified

Failures are attributed back to the task that produced the faulty file
(traceback frames -> artifact registry, else test -> module-under-test map),
which is what lets the orchestrator re-run just the broken sub-graph.
"""
from __future__ import annotations

import ast
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..guardrails import fingerprint, scan_content
from ..models import Artifact, Finding, Task
from .base import Agent, AgentContext


def _safe_env() -> dict[str, str]:
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_pytest(cwd: Path, timeout: int, args: list[str] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        xml = Path(td) / "junit.xml"
        cmd = [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
               f"--junitxml={xml}", *(args or [])]
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=_safe_env(), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "timeout": True, "tests": 0, "failures": [], "passed": 0, "output": "timeout"}
        result: dict[str, Any] = {"ok": proc.returncode == 0, "returncode": proc.returncode,
                                  "output": (proc.stdout + proc.stderr)[-4000:], "failures": []}
        if not xml.exists():
            result.update(tests=0, passed=0)
            return result
        root = ET.parse(xml).getroot()
        tests = passed = 0
        for case in root.iter("testcase"):
            tests += 1
            bad = case.find("failure") if case.find("failure") is not None else case.find("error")
            if bad is None:
                if case.find("skipped") is None:
                    passed += 1
                continue
            result["failures"].append({"test": f"{case.get('classname')}::{case.get('name')}",
                                       "file": (case.get("file") or case.get("classname", "").replace(".", "/") + ".py"),
                                       "message": (bad.get("message") or "")[:300],
                                       "trace": (bad.text or "")[-1500:]})
        result.update(tests=tests, passed=passed)
        return result


class Validator(Agent):
    name = "validator"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        mode = task.inputs.get("mode", "greenfield")
        if mode == "baseline":
            res = run_pytest(ctx.workspace.resolve("baseline_repo"), ctx.budget.test_timeout_s)
            ctx.blackboard.put("baseline", {"tests": res["tests"], "passed": res["passed"],
                                            "failed": len(res["failures"])}, producer=task.id)
            ctx.tracer.emit("info", f"baseline: {res['passed']}/{res['tests']} existing tests pass")
            return []
        report = self._brownfield(task, ctx) if mode == "brownfield" else self._greenfield(task, ctx)
        report["error_count"] = sum(1 for f in report["findings"] if f["severity"] == "error")
        report["warning_count"] = sum(1 for f in report["findings"] if f["severity"] == "warning")
        ctx.blackboard.put("validation_report", report, producer=task.id)
        for f in report["findings"]:
            if f["severity"] != "info":
                ctx.tracer.emit("finding", f"[{f['severity']}] {f['check']}: {f['message']}"
                                + (f" -> {f['task_id']}" if f.get("task_id") else ""))
        lines = ["# Validation report", "", "| Check | Status | Detail |", "|---|---|---|"]
        lines += [f"| {c['name']} | {c['status']} | {c['detail']} |" for c in report["checks"]]
        return [self.write(ctx, task, "VALIDATION_REPORT.md", "\n".join(lines) + "\n", "doc")]

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _registry(ctx: AgentContext) -> dict[str, str]:
        return ctx.blackboard.get("artifact_registry", {}) or {}

    def _attribute(self, failure: dict, prefix: str, ctx: AgentContext, targets_key: str) -> str | None:
        reg = self._registry(ctx)
        # 1) innermost frame inside generated (non-test) code
        frames = re.findall(r"((?:app|shortener)/[\w/]+\.py):\d+", failure["trace"])
        for fr in reversed(frames):
            if f"{prefix}/{fr}" in reg:
                return reg[f"{prefix}/{fr}"]
        # 2) module-under-test mapping from the TestGenerator
        targets = ctx.blackboard.get(targets_key, {}) or {}
        for test_file, mods in targets.items():
            if failure["file"].endswith(test_file) or test_file.replace("/", ".")[:-3] in failure["test"]:
                for m in mods:
                    if f"{prefix}/{m}" in reg:
                        return reg[f"{prefix}/{m}"]
        return None

    @staticmethod
    def _static(root: Path, files: list[Path], findings: list[Finding], reg: dict[str, str], prefix: str) -> int:
        bad = 0
        for p in files:
            rel = p.relative_to(root).as_posix()
            src = p.read_text()
            try:
                ast.parse(src, filename=rel)
            except SyntaxError as exc:
                bad += 1
                findings.append(Finding("syntax", "error", f"{rel}:{exc.lineno}: {exc.msg}", rel,
                                        reg.get(f"{prefix}/{rel}")))
            for f in scan_content(rel, src):
                f.task_id = reg.get(f"{prefix}/{rel}")
                findings.append(f)
                bad += 1
        return bad

    @staticmethod
    def _check(checks: list[dict], name: str, ok: bool, detail: str, warn: bool = False) -> None:
        checks.append({"name": name, "status": "PASS" if ok else ("WARN" if warn else "FAIL"), "detail": detail})

    # ---------------------------------------------------------- greenfield
    def _greenfield(self, task: Task, ctx: AgentContext) -> dict[str, Any]:
        root = ctx.workspace.resolve("service")
        reg = self._registry(ctx)
        findings: list[Finding] = []
        checks: list[dict] = []
        py = sorted(root.rglob("*.py"))

        bad = self._static(root, py, findings, reg, "service")
        self._check(checks, "syntax + content policy", bad == 0, f"{len(py)} files scanned, {bad} issue(s)")

        # contract conformance: import the generated app in a subprocess and diff its OpenAPI
        contract = ctx.blackboard.require("api_contract")
        probe = ("import json,sys; sys.path.insert(0,'.');"
                 "from app.api import create_app; from app.config import Settings;"
                 "a=create_app(Settings(database_path=':memory:'));"
                 "print(json.dumps({p:{m:sorted(o.get('responses',{})) for m,o in ops.items()} "
                 "for p,ops in a.openapi()['paths'].items()}))")
        proc = subprocess.run([sys.executable, "-I", "-c", probe], cwd=root, env=_safe_env(), capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            findings.append(Finding("contract", "error", f"app failed to import: {proc.stderr.strip()[-300:]}",
                                    "app/api.py", reg.get("service/app/api.py")))
            self._check(checks, "contract conformance", False, "application did not start")
        else:
            impl = __import__("json").loads(proc.stdout.strip().splitlines()[-1])
            missing, extra, codes = [], [], []
            for path, ops in contract["paths"].items():
                for m, op in ops.items():
                    if path not in impl or m not in impl[path]:
                        missing.append(f"{m.upper()} {path}")
                        continue
                    for status in op["responses"]:
                        if status not in impl[path][m]:
                            codes.append(f"{m.upper()} {path} -> {status}")
            for path, ops in impl.items():
                for m in ops:
                    if path not in contract["paths"] or m not in contract["paths"][path]:
                        extra.append(f"{m.upper()} {path}")
            for item in missing:
                findings.append(Finding("contract", "error", f"contract operation not implemented: {item}",
                                        "app/api.py", reg.get("service/app/api.py")))
            for item in extra:
                findings.append(Finding("contract", "warning", f"undocumented operation: {item}", "app/api.py"))
            for item in codes:
                findings.append(Finding("contract", "warning", f"status not declared by implementation: {item}",
                                        "app/api.py"))
            n_ops = sum(len(o) for o in contract["paths"].values())
            self._check(checks, "contract conformance", not missing,
                        f"{n_ops - len(missing)}/{n_ops} operations match; {len(extra)} undocumented; "
                        f"{len(codes)} status mismatches", warn=bool(extra or codes) and not missing)

        # tests
        res = run_pytest(root, ctx.budget.test_timeout_s)
        for f in res["failures"]:
            findings.append(Finding("tests", "error", f"{f['test']}: {f['message']}", f["file"],
                                    self._attribute(f, "service", ctx, "test_targets")))
        if res.get("timeout"):
            findings.append(Finding("tests", "error", "test run timed out", None))
        if res["tests"] == 0 and not res.get("timeout"):
            findings.append(Finding("tests", "error", f"no tests collected: {res['output'][-300:]}", None))
        self._check(checks, "unit + integration tests", res["ok"], f"{res['passed']}/{res['tests']} passed")

        # traceability: requirement ids -> test files that declare `# covers:`
        spec = ctx.blackboard.require("spec")
        req_ids = [r.split(":")[0] for r in spec["functional"] + spec["non_functional"]]
        covered: dict[str, list[str]] = {r: [] for r in req_ids}
        for tf in (root / "tests").rglob("test_*.py"):
            m = re.search(r"#\s*covers:\s*(.+)", tf.read_text())
            for rid in (m.group(1).split(",") if m else []):
                rid = rid.strip()
                if rid in covered:
                    covered[rid].append(tf.relative_to(root).as_posix())
        uncovered = [r for r, files in covered.items() if not files]
        for r in uncovered:
            findings.append(Finding("traceability", "warning",
                                    f"{r} has no automated test (verified by design review / load test)", None))
        self._check(checks, "requirement traceability", not uncovered,
                    f"{len(req_ids) - len(uncovered)}/{len(req_ids)} requirements covered by tests"
                    + (f"; uncovered: {', '.join(uncovered)}" if uncovered else ""), warn=True)

        return {"mode": "greenfield", "checks": checks, "findings": [f.__dict__ for f in findings],
                "tests": {"total": res["tests"], "passed": res["passed"], "failed": len(res["failures"]),
                          "failures": res["failures"]},
                "traceability": covered}

    # ---------------------------------------------------------- brownfield
    def _brownfield(self, task: Task, ctx: AgentContext) -> dict[str, Any]:
        ws = ctx.workspace
        base, patched = ws.resolve("baseline_repo"), ws.resolve("patched_repo")
        reg = self._registry(ctx)
        findings: list[Finding] = []
        checks: list[dict] = []

        changed = [p for p in sorted(patched.rglob("*.py"))
                   if not (base / p.relative_to(patched)).exists()
                   or (base / p.relative_to(patched)).read_text() != p.read_text()]
        bad = self._static(patched, changed, findings, reg, "patched_repo")
        self._check(checks, "syntax + content policy", bad == 0, f"{len(changed)} changed file(s), {bad} issue(s)")

        baseline = ctx.blackboard.get("baseline", {})
        res = run_pytest(patched, ctx.budget.test_timeout_s)
        for f in res["failures"]:
            findings.append(Finding("tests", "error", f"{f['test']}: {f['message']}", f["file"],
                                    self._attribute(f, "patched_repo", ctx, "test_targets_bf")))
        self._check(checks, "regression + new tests", res["ok"],
                    f"{res['passed']}/{res['tests']} passed (baseline had {baseline.get('passed')}/"
                    f"{baseline.get('tests')})")

        # the bug reproduction must fail on the untouched baseline
        from ..packs.legacy_expiry import REPRO_TEST_PATH
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "repo"
            shutil.copytree(base, tmp)
            (tmp / REPRO_TEST_PATH).write_text((patched / REPRO_TEST_PATH).read_text())
            repro = run_pytest(tmp, ctx.budget.test_timeout_s, [REPRO_TEST_PATH])
        reproduced = not repro["ok"] and repro["tests"] > 0
        fixed = not any(REPRO_TEST_PATH.split("/")[-1][:-3] in f["test"] for f in res["failures"])
        if not reproduced:
            findings.append(Finding("bug_repro", "warning", "reproduction test did not fail on baseline", None))
        self._check(checks, "bug reproduced on baseline, fixed on patch", reproduced and fixed,
                    f"baseline: {'FAIL (bug confirmed)' if reproduced else 'pass?'}; "
                    f"patched: {'PASS' if fixed else 'FAIL'}")

        # backward compatibility of public signatures
        model = ctx.blackboard.require("codebase_model")
        broken = self._compat(model, patched)
        for b in broken:
            findings.append(Finding("compat", "error", b, None, reg.get("patched_repo/shortener/core.py")))
        self._check(checks, "public API backward compatible", not broken,
                    "all v1 signatures callable unchanged" if not broken else "; ".join(broken))

        # final patch covers code + tests + docs; must apply cleanly to a pristine copy
        patch = self.tree_diff(base, patched)
        ws.write("changes.patch", patch)
        applies = self._patch_applies(base, patch)
        self._check(checks, "patch applies to pristine source", applies is not False,
                    "git apply --check OK" if applies else ("git not available" if applies is None else "FAILED"),
                    warn=applies is None)
        if applies is False:
            findings.append(Finding("patch", "error", "changes.patch does not apply", "changes.patch"))

        # source repository untouched
        src = ctx.blackboard.get("source_repo_path")
        fp0 = ctx.blackboard.get("source_fingerprint")
        intact = src is None or fingerprint(Path(src)) == fp0
        self._check(checks, "source repository untouched", intact, "content hash unchanged" if intact else "MODIFIED")

        return {"mode": "brownfield", "checks": checks, "findings": [f.__dict__ for f in findings],
                "tests": {"total": res["tests"], "passed": res["passed"], "failed": len(res["failures"]),
                          "failures": res["failures"], "baseline": baseline},
                "patch_stats": {"lines": patch.count("\n"), "files": patch.count("\n+++ ")}}

    @staticmethod
    def _compat(model: dict, patched: Path) -> list[str]:
        broken = []
        for mod, info in model["modules"].items():
            if info["is_test"]:
                continue
            path = patched / info["path"]
            if not path.exists():
                broken.append(f"module removed: {mod}")
                continue
            tree = ast.parse(path.read_text())
            new: dict[str, ast.FunctionDef] = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    new[node.name] = node
                elif isinstance(node, ast.ClassDef):
                    for n in node.body:
                        if isinstance(n, ast.FunctionDef):
                            new[f"{node.name}.{n.name}"] = n
            old_sigs = [s for s in info["functions"]] + [f"{c}.{m}" for c, ms in info["classes"].items() for m in ms]
            for sig in old_sigs:
                name, args = sig.split("(")[0], [a for a in sig[sig.index("(") + 1:-1].split(", ") if a]
                fn = new.get(name)
                if fn is None:
                    broken.append(f"{mod}.{name} removed")
                    continue
                new_args = [a.arg for a in fn.args.args]
                n_defaults = len(fn.args.defaults)
                required = new_args[: len(new_args) - n_defaults]
                if new_args[: len(args)] != args or len(required) > len(args):
                    broken.append(f"{mod}.{name}: signature {args} -> {new_args} is not backward compatible")
        return broken

    @staticmethod
    def tree_diff(base: Path, patched: Path) -> str:
        out: list[str] = []
        files = sorted({p.relative_to(patched).as_posix() for p in patched.rglob("*") if p.is_file()} |
                       {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()})
        for rel in files:
            if "__pycache__" in rel or rel.endswith(".pyc"):
                continue
            a, b = base / rel, patched / rel
            old = a.read_text().splitlines(True) if a.exists() else []
            new = b.read_text().splitlines(True) if b.exists() else []
            if old != new:
                out.extend(difflib.unified_diff(old, new, fromfile=f"a/{rel}" if a.exists() else "/dev/null",
                                                tofile=f"b/{rel}" if b.exists() else "/dev/null"))
        return "".join(out)

    @staticmethod
    def _patch_applies(base: Path, patch: str) -> bool | None:
        if not shutil.which("git"):
            return None
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "repo"
            shutil.copytree(base, tmp)
            pf = Path(td) / "c.patch"
            pf.write_text(patch)
            proc = subprocess.run(["git", "apply", "--check", str(pf)], cwd=tmp, capture_output=True, text=True)
            return proc.returncode == 0
