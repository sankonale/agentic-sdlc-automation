"""Reporter: assembles the structured engineering summary (markdown + JSON)."""
from __future__ import annotations

from typing import Any

from .base import Agent


class Reporter(Agent):
    name = "reporter"

    def render(self, run: dict[str, Any]) -> str:
        spec = run["spec"]
        L: list[str] = [f"# Engineering summary: {spec['title']}", "",
                        f"- **Run id:** `{run['run_id']}`  |  **Status:** `{run['status']}`  |  "
                        f"**Scenario:** {spec['scenario']}  |  **Domain:** {spec['domain']}",
                        f"- **LLM provider:** {run['llm']}  |  **Approver:** {run['approver']}  |  "
                        f"**Duration:** {run['duration_s']:.1f}s", "",
                        "## 1. Requirement understanding", "", f"> {spec['raw']}", "",
                        f"Ambiguity score: **{spec['ambiguity_score']}** ({len(spec['ambiguities'])} open points)", "",
                        "| Term | Category | Severity | Question | Resolution |", "|---|---|---|---|---|"]
        for i, a in enumerate(spec["ambiguities"]):
            resolution = spec["assumptions"][i] if i < len(spec.get("assumptions", [])) else "unresolved"
            L.append(f"| {a['term']} | {a['category']} | {a['severity']} | {a['question']} | {resolution} |")
        L += ["", "**Functional requirements**", *[f"- {r}" for r in spec["functional"]],
              "", "**Non-functional requirements**", *[f"- {r}" for r in spec["non_functional"]]]
        if spec.get("out_of_scope"):
            L += ["", "**Out of scope**", *[f"- {r}" for r in spec["out_of_scope"]]]

        if run.get("plan"):
            L += ["", "## 2. Implementation plan and rationale", "",
                  "| Task | Agent | Depends on | Status | Attempts |", "|---|---|---|---|---|"]
            for t in run["plan"]:
                L.append(f"| `{t['id']}` {t['title']} | {t['agent']} | {', '.join(t['depends_on']) or '-'} | "
                         f"{t['status']} | {t['attempts']} |")
            L += ["", "**Execution waves** (tasks in one wave ran in parallel):", ""]
            L += [f"{i}. {', '.join(w)}" for i, w in enumerate(run.get("waves", []), 1)]
            L += ["", "**Orchestration events:** "
                  f"{run['stats']['retries']} transient retr(y/ies), {run['stats']['reworks']} rework loop(s), "
                  f"{run['stats']['skipped']} skipped task(s), {run['stats']['failed']} failed task(s)."]
            if spec["scenario"] == "greenfield":
                L += ["", "Rationale: design artefacts (architecture -> contract + schema in parallel) are produced "
                      "first so every code task builds against agreed interfaces; persistence and analytics are "
                      "independent and run in parallel; the API is implemented last against the contract and is "
                      "verified by the contract-conformance check; tests, docs and risk analysis fan out and join "
                      "at validation."]
            else:
                L += ["", "Rationale: understand before changing - scan, baseline tests and impact analysis come "
                      "first; the change is made on a sandbox copy, never on the source; new tests include a "
                      "bug reproduction that must fail on the baseline; delivery is a reviewable patch."]

        if run.get("impact"):
            imp = run["impact"]
            L += ["", "## 3. Codebase reasoning (impact analysis)", "",
                  f"- Directly impacted: {', '.join(f'`{m}`' for m in imp['directly_impacted'])}",
                  f"- Transitively impacted: {', '.join(f'`{m}`' for m in imp['transitively_impacted']) or '-'}",
                  f"- Impacted tests: {', '.join(f'`{m}`' for m in imp['impacted_tests'])}",
                  *[f"- Data flow `{f['entry']}`: {' -> '.join(f['path'])}" for f in imp["data_flows"]],
                  f"- Data model change: {imp['data_model_change']}; public API: {imp['public_api_change']}"]

        L += ["", "## 4. Generated artifacts", "", "| Path | Kind | Produced by |", "|---|---|---|"]
        for a in sorted(run["artifacts"], key=lambda x: x["path"]):
            L.append(f"| `{a['path']}` | {a['kind']} | {a['produced_by']} |")

        v = run.get("validation")
        if v:
            L += ["", "## 5. Validation", "", "| Check | Status | Detail |", "|---|---|---|",
                  *[f"| {c['name']} | {c['status']} | {c['detail']} |" for c in v["checks"]]]
            if v.get("traceability"):
                L += ["", "<details><summary>Requirement -> test traceability</summary>", "",
                      "| Requirement | Test files |", "|---|---|",
                      *[f"| {r} | {', '.join(fs) or '**none** (design review / load test)'} |"
                        for r, fs in v["traceability"].items()], "", "</details>"]
            if run.get("repair_history"):
                L += ["", "**Repair history (validate -> rework -> revalidate):**", ""]
                for h in run["repair_history"]:
                    L.append(f"- iteration {h['iteration']}: {h['errors']} error(s) routed to "
                             f"{', '.join(h['routed_to']) or '(unattributed)'}; re-ran {len(h['reset'])} task(s)")
            L += ["", "**Validation approach:** static analysis and guardrail scan before anything executes; "
                  "tests run in an isolated subprocess (`python -I`, scrubbed environment, timeout); the "
                  "implementation is checked against the contract produced by a *different* agent; each failure is "
                  "attributed to the producing task and only that sub-graph is re-run; release is gated on zero "
                  "errors."]
        if run.get("risks"):
            L += ["", "## 6. Risks, trade-offs and failure scenarios", "",
                  "| Risk | L | I | Failure scenario | Mitigation |", "|---|---|---|---|---|",
                  *[f"| {r['risk']} | {r['likelihood']} | {r['impact']} | {r['failure_scenario']} | "
                    f"{r['mitigation']} |" for r in run["risks"]]]
        if run.get("architecture"):
            L += ["", "**Key trade-offs (ADRs)**", "",
                  *[f"- **{d['id']} {d['title']}** - {d['trade_off']}" for d in run["architecture"]["decisions"]]]

        L += ["", "## 7. Human oversight (gates)", "", "| Gate | Decision | By | Reason |", "|---|---|---|---|",
              *[f"| {g['gate']} | {'APPROVED' if g['approved'] else 'BLOCKED'} | {g['approver']} | {g['reason']} |"
                for g in run["gates"]]]
        L += ["", "## 8. Assumptions and limitations", "", "**Assumptions**",
              *([f"- {a}" for a in spec.get("assumptions", [])] or ["- none"]), "", "**Limitations**",
              *[f"- {x}" for x in run["limitations"]], ""]
        return "\n".join(L)

    def run(self, task, ctx):
        raise NotImplementedError
