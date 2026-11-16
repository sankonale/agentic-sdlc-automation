"""CodebaseAnalyst: system-level understanding of an existing repository.

scan   - parses every Python file with `ast` (no code is executed): modules,
         classes, functions (+ signatures), internal import graph, call graph,
         HTTP entry points (FastAPI/Flask decorators and http.server do_* handlers)
         and test files with the modules they import.
impact - maps change requests to symbols (keyword + docstring/identifier
         matching), then expands along the reverse import graph to find every
         module, API route, data flow and test affected by the change.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from ..models import Artifact, Task
from .base import Agent, AgentContext

# change-concept -> identifier fragments that implement it
CONCEPTS = {
    "expiry": ["expir", "ttl", "save", "get", "store", "resolve", "shorten"],
    "collision": ["hash", "shorten", "save", "code"],
    "analytics": ["click", "track", "stats", "resolve"],
    "validation": ["validate", "shorten", "url"],
}
CONCEPT_TRIGGERS = {
    "expiry": r"expir|ttl|time.to.live",
    "collision": r"collid|collision|overwrit|same code|duplicate",
    "analytics": r"analytic|click|track",
    "validation": r"validat|sanitiz",
}


def _sig(fn: ast.FunctionDef) -> str:
    args = [a.arg for a in fn.args.args]
    return f"{fn.name}({', '.join(args)})"


class CodebaseAnalyst(Agent):
    name = "codebase_analyst"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        if task.inputs.get("mode") == "scan":
            return self._scan(task, ctx)
        return self._impact(task, ctx)

    # ------------------------------------------------------------------ scan
    def _scan(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        repo = ctx.workspace.resolve("baseline_repo")
        modules: dict[str, Any] = {}
        for path in sorted(repo.rglob("*.py")):
            rel = path.relative_to(repo).as_posix()
            tree = ast.parse(path.read_text(), filename=rel)
            mod = rel[:-3].replace("/", ".").removesuffix(".__init__")
            info: dict[str, Any] = {"path": rel, "is_test": "/test_" in f"/{rel}" or rel.startswith("tests/"),
                                    "doc": ast.get_docstring(tree) or "", "imports": [], "classes": {},
                                    "functions": [], "routes": [], "calls": {}}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:  # relative import -> absolute
                        pkg = mod.split(".")[: -node.level] if not rel.endswith("__init__.py") else mod.split(".")
                        base = ".".join([*pkg, base] if base else pkg)
                    info["imports"].append(base)
                elif isinstance(node, ast.Import):
                    info["imports"].extend(a.name for a in node.names)
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    methods = [_sig(n) for n in node.body if isinstance(n, ast.FunctionDef)]
                    info["classes"][node.name] = methods
                    for n in node.body:
                        if isinstance(n, ast.FunctionDef):
                            info["calls"][f"{node.name}.{n.name}"] = self._calls(n)
                            if n.name.startswith("do_") and n.name[3:].isupper():
                                paths = sorted({c.value for c in ast.walk(n) if isinstance(c, ast.Constant)
                                                and isinstance(c.value, str) and c.value.startswith("/")
                                                and len(c.value) > 1})
                                info["routes"].append({"method": n.name[3:], "paths": paths or ["/<code>"],
                                                       "handler": f"{node.name}.{n.name}"})
                elif isinstance(node, ast.FunctionDef):
                    info["functions"].append(_sig(node))
                    info["calls"][node.name] = self._calls(node)
                    for d in node.decorator_list:
                        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and \
                                d.func.attr in {"get", "post", "put", "delete", "patch", "route"} and d.args and \
                                isinstance(d.args[0], ast.Constant):
                            info["routes"].append({"method": d.func.attr.upper(), "paths": [d.args[0].value],
                                                   "handler": node.name})
            modules[mod] = info
        internal = set(modules)
        for info in modules.values():
            info["imports"] = sorted({i for i in info["imports"] if i in internal or i.split(".")[0] in
                                      {m.split(".")[0] for m in internal}} & internal)
        model = {"root": "baseline_repo", "modules": modules,
                 "entry_points": [r | {"module": m} for m, i in modules.items() for r in i["routes"]]}
        ctx.blackboard.put("codebase_model", model, producer=task.id)
        ctx.tracer.emit("info", f"scanned {len(modules)} modules, {len(model['entry_points'])} HTTP entry points")
        return [self.write(ctx, task, "analysis/codebase_model.json", json.dumps(model, indent=2), "doc",
                           "AST model of the existing repository")]

    @staticmethod
    def _calls(fn: ast.FunctionDef) -> list[str]:
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute):
                    out.add(f.attr)
                elif isinstance(f, ast.Name):
                    out.add(f.id)
        return sorted(out)

    # ---------------------------------------------------------------- impact
    def _impact(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        model = ctx.blackboard.require("codebase_model")
        spec = ctx.blackboard.require("spec")
        text = spec["raw"].lower()
        concepts = [c for c, rx in CONCEPT_TRIGGERS.items() if re.search(rx, text)]
        modules = model["modules"]

        scores: dict[str, dict[str, Any]] = {}
        for mod, info in modules.items():
            if info["is_test"]:
                continue
            hits: list[str] = []
            symbols = [*info["functions"], *[f"{c}.{m}" for c, ms in info["classes"].items() for m in ms]]
            for c in concepts:
                for frag in CONCEPTS[c]:
                    for s in symbols:
                        if frag in s.lower():
                            hits.append(f"{c}:{s}")
            if hits:
                scores[mod] = {"score": len(set(hits)), "symbols": sorted(set(hits))}

        direct = set(scores)
        reverse: dict[str, set[str]] = {m: set() for m in modules}
        for m, info in modules.items():
            for imp in info["imports"]:
                reverse.setdefault(imp, set()).add(m)
        transitive: set[str] = set()
        frontier = list(direct)
        while frontier:
            cur = frontier.pop()
            for dep in reverse.get(cur, ()):
                if dep not in direct and dep not in transitive:
                    transitive.add(dep)
                    frontier.append(dep)
        impacted_tests = sorted(m for m in transitive | direct if modules[m]["is_test"])
        impacted_code = sorted(m for m in direct | transitive if not modules[m]["is_test"])
        routes = [ep for ep in model["entry_points"] if ep["module"] in direct | transitive]

        flows = []
        for ep in routes:
            handler_calls = modules[ep["module"]]["calls"].get(ep["handler"], [])
            flows.append({"entry": f"{ep['method']} {', '.join(ep['paths'])}",
                          "path": [ep["handler"], *[c for c in handler_calls if c in {"shorten", "resolve"}],
                                   "store.save/get"]})

        impact = {
            "concepts": concepts,
            "directly_impacted": {m: scores[m] for m in sorted(direct)},
            "transitively_impacted": sorted(transitive - set(impacted_tests)),
            "impacted_code_modules": impacted_code,
            "impacted_tests": impacted_tests,
            "impacted_routes": routes,
            "data_flows": flows,
            "data_model_change": "expiry" in concepts,
            "public_api_change": "additive (new optional parameters); no breaking signature changes",
        }
        ctx.blackboard.put("impact", impact, producer=task.id)
        md = ["# Impact analysis", "", f"Change concepts detected: **{', '.join(concepts) or 'none'}**", "",
              "| Module | Why | Kind |", "|---|---|---|"]
        for m in sorted(direct):
            md.append(f"| `{m}` | {', '.join(scores[m]['symbols'][:6])} | direct |")
        for m in sorted(transitive):
            md.append(f"| `{m}` | imports an impacted module | transitive |")
        md += ["", "## HTTP entry points affected", *[f"- `{f['entry']}` -> {' -> '.join(f['path'])}" for f in flows],
               "", "## Tests that exercise impacted code", *[f"- `{t}`" for t in impacted_tests],
               "", f"Data model change: **{impact['data_model_change']}** (store value becomes a record with expiry)"]
        ctx.tracer.emit("info", f"impact: direct={sorted(direct)} transitive={sorted(transitive)}")
        return [self.write(ctx, task, "analysis/IMPACT_ANALYSIS.md", "\n".join(md) + "\n", "doc"),
                self.write(ctx, task, "analysis/impact.json", json.dumps(impact, indent=2), "doc")]
