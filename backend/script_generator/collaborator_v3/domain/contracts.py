"""Runtime contracts that compose the smaller immutable domain records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import DomainError, ErrorCode
from .models import ClaimRecord, EvidenceRecord, ResourceRecord, ResourceRef


def _nonempty(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError(ErrorCode.INVALID_ARGUMENTS, f"{field_name} must not be empty")
    return text


@dataclass(frozen=True)
class CapabilityRecord:
    id: str
    kind: str
    name: str
    version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission_actions: tuple[str, ...] = ()
    cost_class: str = "local"
    availability: str = "available"
    scope: str = "global"
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty(self.id, "capability id")
        _nonempty(self.name, "capability name")
        _nonempty(self.version, "capability version")
        if self.kind not in {"tool", "skill", "model"}:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "invalid capability kind")
        if self.scope not in {"task", "global"}:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "invalid capability scope")
        if self.kind == "skill" and self.permission_actions:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "skill cannot hold execution permissions")
        if self.kind == "tool" and not isinstance(self.input_schema, dict):
            raise DomainError(ErrorCode.CAPABILITY_SCHEMA_MISMATCH, "tool input schema is required")


@dataclass(frozen=True)
class PermissionGrantRecord:
    id: str
    capability_id: str
    actions: tuple[str, ...]
    task_id: str
    resource_refs: tuple[ResourceRef, ...] = ()
    capability_version: str | None = None
    stage_id: str | None = None
    batch_id: str | None = None
    expires_kind: str = "task"
    expires_value: str | None = None
    environment_fingerprint: str = ""
    target_fingerprint: str = ""
    revision: int = 1
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.id, "grant id")
        _nonempty(self.capability_id, "capability id")
        _nonempty(self.task_id, "task id")
        if not self.actions:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "grant requires actions")


@dataclass(frozen=True)
class InteractionRequestRecord:
    id: str
    turn_id: str
    reason_code: str
    question: str
    reason: str
    options: tuple[dict[str, str], ...] = ()
    allow_free_text: bool = True
    blocking: bool = True
    resume_token_hash: str = ""
    status: str = "open"

    def __post_init__(self) -> None:
        _nonempty(self.id, "interaction id")
        _nonempty(self.turn_id, "turn id")
        _nonempty(self.question, "interaction question")
        if self.reason_code not in {
            "PERMISSION_REQUIRED",
            "REQUIREMENT_AMBIGUOUS",
            "REALITY_CONFLICT",
            "RESOURCE_REQUIRED",
            "LOGIC_DRAFT_CONFIRMATION",
        }:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "invalid interaction reason")
        if self.status not in {"open", "resolved", "cancelled"}:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "invalid interaction status")


@dataclass(frozen=True)
class CheckpointRecord:
    id: str
    turn_id: str
    task_id: str | None
    phase: str
    viewport_snapshot_id: str
    completed_step_ids: tuple[str, ...] = ()
    acquired_refs: tuple[dict[str, Any], ...] = ()
    fact_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_snapshot: dict[str, Any] = field(default_factory=dict)
    unresolved_interaction_id: str | None = None
    budget_summary: dict[str, Any] = field(default_factory=dict)
    resume_phase: str = "hydrate"
    created_at: str = ""


@dataclass(frozen=True)
class RuntimeResult:
    ok: bool
    action_id: str
    summary: dict[str, Any] | None = None
    refs: tuple[str, ...] = ()
    error_code: str | None = None
    recoverable: bool = False
    retry_policy: str = "none"
    details_ref: str | None = None
    user_visible: bool = False


@dataclass(frozen=True)
class CommitProposal:
    idempotency_key: str
    turn_id: str
    expected_task_revision: int | None
    task_patch: dict[str, Any] = field(default_factory=dict)
    resources_to_create: tuple[ResourceRecord, ...] = ()
    evidence_to_create: tuple[EvidenceRecord, ...] = ()
    claims_to_create: tuple[ClaimRecord, ...] = ()
    interactions_to_create: tuple[InteractionRequestRecord, ...] = ()
    memory_summary: dict[str, Any] = field(default_factory=dict)
    acceptance_proofs: tuple[str, ...] = ()
    final_response: dict[str, Any] = field(default_factory=dict)
