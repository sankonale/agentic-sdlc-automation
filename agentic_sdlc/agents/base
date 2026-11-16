from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..blackboard import Blackboard
from ..guardrails import Budget, Workspace
from ..llm import LLMProvider
from ..models import Artifact, Task
from ..trace import Tracer


class TransientError(Exception):
    """Recoverable failure (timeouts, rate limits, flaky tool). Retried with backoff."""


class FaultInjector:
    """Deterministic fault injection used to demonstrate recovery paths.

    spec strings:  transient:<task_id>   raise TransientError on first attempt
                   defect:<task_id>      make the agent emit a subtle bug once
    """

    def __init__(self, specs: list[str] | None = None) -> None:
        self._pending: set[tuple[str, str]] = set()
        for s in specs or []:
            kind, _, tid = s.partition(":")
            if kind not in {"transient", "defect"} or not tid:
                raise ValueError(f"bad fault spec: {s}")
            self._pending.add((kind, tid))

    def take(self, kind: str, task_id: str) -> bool:
        if (kind, task_id) in self._pending:
            self._pending.discard((kind, task_id))
            return True
        return False


@dataclass
class AgentContext:
    blackboard: Blackboard
    workspace: Workspace
    tracer: Tracer
    llm: LLMProvider
    budget: Budget
    faults: FaultInjector = field(default_factory=FaultInjector)
    project_root: Any = None   # repo root of this tool (for packs/examples)


class Agent:
    name = "agent"

    def run(self, task: Task, ctx: AgentContext) -> list[Artifact]:
        raise NotImplementedError

    # helpers -------------------------------------------------------------
    def write(self, ctx: AgentContext, task: Task, rel: str, content: str, kind: str, desc: str = "") -> Artifact:
        ctx.workspace.write(rel, content)
        return Artifact(rel, kind, task.id, desc)
