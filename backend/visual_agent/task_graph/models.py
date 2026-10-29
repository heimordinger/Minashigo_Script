"""Domain-neutral task graph records.

Tasks describe constraints over logical sets.  They deliberately do not contain
screen coordinates or model messages; those belong to runtime observations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


TASK_STATUSES = {
    "unexpanded", "pending", "ready", "running", "paused", "verify",
    "succeeded", "blocked", "needs_user", "failed", "obsolete",
    "superseded", "cancelled",
}
TERMINAL_STATUSES = {
    "succeeded", "failed", "obsolete", "superseded", "cancelled",
}
COMPOSITE_OPERATORS = {"all", "any", "at_least"}
INSERTION_POLICIES = {"append", "interrupt_at_safe_point", "merge", "replace"}


@dataclass(slots=True)
class TaskRun:
    id: str
    title: str
    period_key: str = ""
    status: str = "pending"
    root_node_id: str = ""
    tree_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskNode:
    id: str
    run_id: str
    title: str
    node_type: str = "goal"
    status: str = "unexpanded"
    parent_id: str = ""
    required: bool = True
    operator: str = ""
    required_count: int = 0
    goal_spec: dict[str, Any] = field(default_factory=dict)
    scheduling: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    revision_created: int = 0
    revision_updated: int = 0

    def __post_init__(self) -> None:
        if self.status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {self.status}")
        if self.node_type == "composite" and self.operator not in COMPOSITE_OPERATORS:
            raise ValueError(f"invalid composite operator: {self.operator}")


def initial_status(*, requires_discovery: bool = False,
                   dependencies_satisfied: bool = True,
                   executable: bool = True,
                   completion_candidate: bool = False,
                   verified_completion: bool = False,
                   requires_user: bool = False,
                   blocked: bool = False) -> str:
    if requires_user:
        return "needs_user"
    if verified_completion:
        return "succeeded"
    if completion_candidate:
        return "verify"
    if blocked:
        return "blocked"
    if requires_discovery:
        return "unexpanded"
    if dependencies_satisfied and executable:
        return "ready"
    return "pending"
