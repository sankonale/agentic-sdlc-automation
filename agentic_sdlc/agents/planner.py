"""Planner: Spec -> task DAG.

Plans are built from scenario-specific decomposition strategies. Each task
names the agent that executes it, its dependencies (data it needs from the
blackboard), and what it produces. Independent branches (e.g. API contract vs.
data schema, or analytics vs. persistence) are deliberately left unordered so
the orchestrator runs them in parallel.
"""
from __future__ import annotations

from ..models import ScenarioType, Spec, Task
from .base import Agent


def T(id: str, title: str, agent: str, deps: list[str] | None = None, produces: list[str] | None = None,
      **inputs) -> Task:
    return Task(id=id, title=title, agent=agent, depends_on=deps or [], produces=produces or [], inputs=inputs)


class Planner(Agent):
    name = "planner"

    def plan(self, spec: Spec) -> list[Task]:
        if spec.scenario == ScenarioType.BROWNFIELD and spec.target_repo:
            return self._brownfield(spec)
        if spec.scenario == ScenarioType.TESTS_DOCS and spec.target_repo:
            return self._tests_docs(spec)
        return self._greenfield(spec)

    @staticmethod
    def _greenfield(spec: Spec) -> list[Task]:
        return [
            T("architecture", "Design system architecture and ADRs", "architect", [], ["architecture"]),
            T("api_contract", "Define OpenAPI contract", "api_designer", ["architecture"], ["api_contract"]),
            T("data_schema", "Define data model and SQL schema", "data_modeler", ["architecture"], ["data_schema"]),
            T("impl_core", "Implement config, models, code generation", "code_generator",
              ["api_contract", "data_schema"], ["code:core"], component="core"),
            T("impl_persistence", "Implement repository and cache", "code_generator",
              ["data_schema", "impl_core"], ["code:persistence"], component="persistence"),
            T("impl_analytics", "Implement click analytics pipeline", "code_generator",
              ["data_schema", "impl_core"], ["code:analytics"], component="analytics"),
            T("impl_service", "Implement application service (business rules)", "code_generator",
              ["impl_persistence", "impl_analytics"], ["code:service"], component="service"),
            T("impl_api", "Implement HTTP API conforming to contract", "code_generator",
              ["impl_service", "api_contract"], ["code:api"], component="api"),
            T("ops_config", "Container image, dependencies, test config", "code_generator",
              ["impl_api"], ["code:ops"], component="ops"),
            T("unit_tests", "Generate unit tests", "test_generator",
              ["impl_service"], ["tests:unit"], suite="unit"),
            T("integration_tests", "Generate API integration tests", "test_generator",
              ["impl_api"], ["tests:integration"], suite="integration"),
            T("risk_analysis", "Risks, trade-offs, failure scenarios", "risk_analyst",
              ["architecture"], ["risks"]),
            T("docs", "Service README, API reference, runbook", "doc_writer",
              ["api_contract", "impl_api", "risk_analysis"], ["docs"]),
            T("validate", "Static checks, guardrails, contract conformance, tests, traceability", "validator",
              ["unit_tests", "integration_tests", "ops_config", "docs"], ["validation_report"]),
        ]

    @staticmethod
    def _brownfield(spec: Spec) -> list[Task]:
        return [
            T("codebase_scan", "Scan repository structure (AST)", "codebase_analyst", [], ["codebase_model"],
              mode="scan"),
            T("baseline_tests", "Run existing tests to establish a baseline", "validator", ["codebase_scan"],
              ["baseline"], mode="baseline"),
            T("impact_analysis", "Identify impacted modules, APIs and data flows", "codebase_analyst",
              ["codebase_scan"], ["impact"], mode="impact"),
            T("change_design", "Design the change (backward-compatible)", "architect", ["impact_analysis"],
              ["change_design"], mode="change"),
            T("impl_change", "Implement change on a sandbox copy and produce patch", "change_implementer",
              ["change_design", "baseline_tests"], ["patch"]),
            T("regression_tests", "Add tests for new behaviour and the bug fix", "test_generator",
              ["impl_change"], ["tests:brownfield"], suite="brownfield"),
            T("risk_analysis", "Change risks and rollback plan", "risk_analyst", ["impact_analysis"], ["risks"]),
            T("docs", "Changelog and updated README", "doc_writer", ["impl_change", "risk_analysis"], ["docs"],
              mode="change"),
            T("validate", "Regression + new tests, guardrails, repo-untouched check", "validator",
              ["regression_tests", "docs"], ["validation_report"], mode="brownfield"),
        ]

    def _tests_docs(self, spec: Spec) -> list[Task]:
        # test/documentation improvements reuse the brownfield flow (scan -> baseline -> change -> validate)
        return self._brownfield(spec)

    def run(self, task, ctx):
        raise NotImplementedError
