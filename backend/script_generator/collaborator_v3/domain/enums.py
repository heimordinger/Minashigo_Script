"""Closed vocabularies used by the V3 runtime and persistence layer."""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    ACTIVE = "active"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"


class RuntimePhase(str, Enum):
    INTAKE = "intake"
    HYDRATE = "hydrate"
    PLAN = "plan"
    EXECUTE = "execute"
    OBSERVE = "observe"
    REPLAN = "replan"
    VALIDATE = "validate"
    COMMIT = "commit"
    RESPOND = "respond"
    WAIT_USER = "wait_user"
    PAUSE = "pause"
    FAIL = "fail"


class ResourceStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    VERIFIED = "verified"
    SUPERSEDED = "superseded"
    INVALID = "invalid"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ClaimStatus(str, Enum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    CONFLICTED = "conflicted"
    STALE = "stale"
    REJECTED = "rejected"


class EvidenceKind(str, Enum):
    USER_STATEMENT = "user_statement"
    MODEL_OBSERVATION = "model_observation"
    TOOL_MEASUREMENT = "tool_measurement"
    RUN_OBSERVATION = "run_observation"
    #: 视觉观察证据（Phase B2 / 设计 §5）：跨主轮按 (kind, fingerprint, status) 复用。
    VISION_OBSERVATION = "vision_observation"


class ActionType(str, Enum):
    QUERY = "query"
    ACT = "act"
    UPDATE_PLAN = "update_plan"
    PROPOSE_CLAIM = "propose_claim"
    REQUEST_INTERACTION = "request_interaction"
    VALIDATE_SCRIPT = "validate_script"
    FINISH = "finish"
