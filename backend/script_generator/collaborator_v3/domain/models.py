"""Immutable domain records for V3.

The records intentionally contain no persistence, GUI, model-provider, or V2
runtime imports.  Construction validates cheap structural invariants; policies
that require stores or evidence live above this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .enums import (
    ActionType,
    ClaimStatus,
    EvidenceKind,
    ResourceStatus,
    RuntimePhase,
    TaskStatus,
    TurnStatus,
)
from .errors import DomainError, ErrorCode


def _required(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise DomainError(ErrorCode.INVALID_ARGUMENTS, f"{name} must not be empty")
    return normalized


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DomainError(ErrorCode.INVALID_ARGUMENTS, f"{name} must be >= 1")
    return value


@dataclass(frozen=True, order=True)
class ResourceRef:
    id: str
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "resource id"))
        _positive(self.version, "resource version")


@dataclass(frozen=True)
class TaskRecord:
    id: str
    objective: str
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.ACTIVE
    current_step_id: str | None = None
    plan_revision: int = 1
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "task id"))
        object.__setattr__(self, "objective", _required(self.objective, "objective"))
        _positive(self.plan_revision, "plan revision")
        _positive(self.revision, "task revision")
        _positive(self.schema_version, "schema version")


@dataclass(frozen=True)
class ResourceRecord:
    id: str
    type: str
    version: int
    revision: int
    status: ResourceStatus
    display_name: str
    content_path: str | None = None
    content_hash: str | None = None
    source_kind: str = "unknown"
    source_ref: str | None = None
    derived_from: tuple[ResourceRef, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    reusable_scope: str = "none"
    task_id: str | None = None
    created_in_turn: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "resource id"))
        object.__setattr__(self, "type", _required(self.type, "resource type"))
        object.__setattr__(self, "display_name", _required(self.display_name, "display name"))
        _positive(self.version, "resource version")
        _positive(self.revision, "resource revision")
        if self.reusable_scope not in {"none", "task", "global"}:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "invalid reusable_scope")

    # ── 序列化（进主轮检查点；见 TurnWorkspace.runtime_snapshot）──
    #
    # 为什么要存：这些 record 是"本主轮还没提交的待写内容"（pending_resources /
    # pending_evidence）。主轮一暂停恢复就丢的话，finish 时 resources_to_create /
    # evidence_to_create 会是空的，Committer 直接判"交付物不存在/证据不存在"。

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "version": int(self.version),
            "revision": int(self.revision),
            "status": self.status.value if isinstance(self.status, Enum) else str(self.status),
            "display_name": self.display_name,
            "content_path": self.content_path,
            "content_hash": self.content_hash,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "derived_from": [{"id": r.id, "version": int(r.version)} for r in self.derived_from],
            "metadata": dict(self.metadata or {}),
            "reusable_scope": self.reusable_scope,
            "task_id": self.task_id,
            "created_in_turn": self.created_in_turn,
            "schema_version": int(self.schema_version),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResourceRecord":
        data = dict(payload or {})
        derived = data.get("derived_from")
        return cls(
            id=str(data.get("id") or ""),
            type=str(data.get("type") or ""),
            version=int(data.get("version") or 0),
            revision=int(data.get("revision") or 0),
            status=ResourceStatus(str(data.get("status") or ResourceStatus.CANDIDATE.value)),
            display_name=str(data.get("display_name") or ""),
            content_path=data.get("content_path"),
            content_hash=data.get("content_hash"),
            source_kind=str(data.get("source_kind") or "unknown"),
            source_ref=data.get("source_ref"),
            derived_from=tuple(
                ResourceRef(str(row.get("id") or ""), int(row.get("version") or 0))
                for row in (derived if isinstance(derived, list) else [])
                if isinstance(row, Mapping)
            ),
            metadata=dict(data.get("metadata") or {}),
            reusable_scope=str(data.get("reusable_scope") or "none"),
            task_id=data.get("task_id"),
            created_in_turn=str(data.get("created_in_turn") or ""),
            schema_version=int(data.get("schema_version") or 1),
        )


@dataclass(frozen=True)
class ViewportSnapshot:
    id: str
    turn_id: str
    revision: int
    base: ResourceRef | None = None
    overlays: tuple[ResourceRef, ...] = ()
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    selections: tuple[ResourceRef, ...] = ()
    captured_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "snapshot id"))
        object.__setattr__(self, "turn_id", _required(self.turn_id, "turn id"))
        _positive(self.revision, "viewport revision")
        if self.zoom <= 0:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "zoom must be positive")


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    kind: EvidenceKind
    payload: dict[str, Any]
    turn_id: str
    subject: ResourceRef | None = None
    producer_capability_id: str | None = None
    producer_version: str | None = None
    confidence: float | None = None
    status: str = "active"
    created_at: str = ""
    schema_version: int = 1
    #: 视觉证据指纹（设计 §5.5，migration 8）：其他证据留空 —— **不伪造**。
    fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "evidence id"))
        object.__setattr__(self, "turn_id", _required(self.turn_id, "turn id"))
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "confidence must be between 0 and 1")

    # ── 序列化（进主轮检查点）──
    #
    # 为什么必须存：`Runner._evidence_ref_exists` 与 `_finish` 的
    # `"latest_tool_evidence" → 最新证据 id` 替换都依赖 `workspace.pending_evidence`。
    # 丢了它，恢复后 finish 提交会因"acceptance proof does not exist"直接失败。

    def to_dict(self) -> dict[str, Any]:
        subject = self.subject
        return {
            "id": self.id,
            "kind": self.kind.value if isinstance(self.kind, Enum) else str(self.kind),
            "payload": dict(self.payload or {}),
            "turn_id": self.turn_id,
            "subject": (
                None
                if subject is None
                else {"id": subject.id, "version": int(subject.version)}
            ),
            "producer_capability_id": self.producer_capability_id,
            "producer_version": self.producer_version,
            "confidence": self.confidence,
            "status": self.status,
            "created_at": self.created_at,
            "schema_version": int(self.schema_version),
            "fingerprint": str(self.fingerprint or ""),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceRecord":
        data = dict(payload or {})
        subject = data.get("subject")
        return cls(
            id=str(data.get("id") or ""),
            kind=EvidenceKind(str(data.get("kind") or EvidenceKind.TOOL_MEASUREMENT.value)),
            payload=dict(data.get("payload") or {}),
            turn_id=str(data.get("turn_id") or ""),
            subject=(
                ResourceRef(str(subject.get("id") or ""), int(subject.get("version") or 0))
                if isinstance(subject, Mapping)
                else None
            ),
            producer_capability_id=data.get("producer_capability_id"),
            producer_version=data.get("producer_version"),
            confidence=(
                None if data.get("confidence") is None else float(data.get("confidence"))
            ),
            status=str(data.get("status") or "active"),
            created_at=str(data.get("created_at") or ""),
            schema_version=int(data.get("schema_version") or 1),
            fingerprint=str(data.get("fingerprint") or ""),
        )


@dataclass(frozen=True)
class ClaimRecord:
    id: str
    predicate: str
    value: Any
    evidence_ids: tuple[str, ...]
    status: ClaimStatus
    created_in_turn: str
    subject: ResourceRef | None = None
    confidence: float | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "claim id"))
        object.__setattr__(self, "predicate", _required(self.predicate, "predicate"))
        object.__setattr__(self, "created_in_turn", _required(self.created_in_turn, "turn id"))
        _positive(self.revision, "claim revision")
        if self.status is ClaimStatus.VERIFIED and not self.evidence_ids:
            raise DomainError(ErrorCode.EVIDENCE_INSUFFICIENT, "verified claim requires evidence")

    # ── 序列化（进主轮检查点；候选主张要在 pause→resume 后仍能提交）──

    def to_dict(self) -> dict[str, Any]:
        subject = self.subject
        return {
            "id": self.id,
            "predicate": self.predicate,
            "value": self.value,
            "evidence_ids": [str(x) for x in (self.evidence_ids or ())],
            "status": self.status.value if isinstance(self.status, Enum) else str(self.status),
            "created_in_turn": self.created_in_turn,
            "subject": (
                None
                if subject is None
                else {"id": subject.id, "version": int(subject.version)}
            ),
            "confidence": self.confidence,
            "scope": dict(self.scope or {}),
            "revision": int(self.revision),
            "schema_version": int(self.schema_version),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClaimRecord":
        data = dict(payload or {})
        subject = data.get("subject")
        refs = data.get("evidence_ids")
        return cls(
            id=str(data.get("id") or ""),
            predicate=str(data.get("predicate") or ""),
            value=data.get("value"),
            evidence_ids=tuple(str(x) for x in (refs if isinstance(refs, list) else [])),
            status=ClaimStatus(str(data.get("status") or ClaimStatus.CANDIDATE.value)),
            created_in_turn=str(data.get("created_in_turn") or ""),
            subject=(
                ResourceRef(str(subject.get("id") or ""), int(subject.get("version") or 0))
                if isinstance(subject, Mapping)
                else None
            ),
            confidence=(
                None if data.get("confidence") is None else float(data.get("confidence"))
            ),
            scope=dict(data.get("scope") or {}),
            revision=int(data.get("revision") or 1),
            schema_version=int(data.get("schema_version") or 1),
        )


@dataclass(frozen=True)
class MainTurnRecord:
    id: str
    user_event_id: str
    status: TurnStatus
    phase: RuntimePhase
    active_snapshot_id: str
    task_id: str | None = None
    checkpoint_id: str | None = None
    commit_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    #: 任务定向（设计 §3，migration 7）：任务类型属**主轮状态**，不属 workspace。
    #: ``intent_json`` 是 ``TurnIntent`` 的序列化，意图的唯一事实源；空串/``{}``
    #: 表示"这一轮还没通过定向闸"，**不得**被当成默认值（默认脚本任务是最坏的解释）。
    task_type: str = ""
    intent_json: str = "{}"
    intent_revision: int = 0
    started_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "turn id"))
        object.__setattr__(self, "user_event_id", _required(self.user_event_id, "user event id"))
        object.__setattr__(self, "active_snapshot_id", _required(self.active_snapshot_id, "snapshot id"))


@dataclass(frozen=True)
class SubturnRecord:
    """一次副轮的身份（设计 §14.1/§14.2）。

    事件表只记"发生了什么"；副轮自己的身份、所属 Run 和**整个主轮内单调递增**的
    序号记在这里，于是 ``(main_turn_id, seq)`` 的唯一性由数据库保证，而不是靠
    调用方自觉。``index_in_run`` 只是本 run 内的展示序号，可以随 resume 重置。
    """

    subturn_id: str
    main_turn_id: str
    seq: int
    run_id: str = ""
    index_in_run: int | None = None
    action_type: str = ""
    action_id: str = ""
    outcome: str = ""
    #: Runtime 自动生成的结构化决策留痕。它记录可审计事实与模型主动提交的
    #: ``next_action_reason``，不保存、也不要求模型暴露隐含思维链。
    trace: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "subturn_id", _required(self.subturn_id, "subturn id"))
        object.__setattr__(self, "main_turn_id", _required(self.main_turn_id, "turn id"))
        _positive(self.seq, "subturn seq")
        _positive(self.schema_version, "schema version")


@dataclass(frozen=True)
class UserEvent:
    id: str
    type: str
    text: str
    viewport_snapshot_id: str
    attachment_refs: tuple[ResourceRef, ...] = ()
    task_hint: str | None = None
    interaction_id: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required(self.id, "user event id"))
        object.__setattr__(self, "viewport_snapshot_id", _required(self.viewport_snapshot_id, "snapshot id"))
        if self.type not in {
            "message", "supplement", "selection", "permission_decision", "resume", "cancel", "undo"
        }:
            raise DomainError(ErrorCode.INVALID_ARGUMENTS, "invalid user event type")


@dataclass(frozen=True)
class ActionEnvelope:
    protocol_version: int
    action_id: str
    type: ActionType
    payload: dict[str, Any]
    expected_task_revision: int | None = None
    expected_viewport_revision: int | None = None
    #: 副轮定向思考（设计 §16）。**结构化字段**，不是自由思维链：
    #: 只允许固定键（在适配层校验），Runtime 只校验必要字段与一致性、不展示推理独白。
    orientation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.protocol_version != 1:
            raise DomainError(
                ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
                f"unsupported protocol version: {self.protocol_version}",
            )
        object.__setattr__(self, "action_id", _required(self.action_id, "action id"))
        if not isinstance(self.payload, dict):
            raise DomainError(ErrorCode.INVALID_ACTION, "action payload must be an object")
