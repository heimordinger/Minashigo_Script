"""Framework-independent V3 domain model."""

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
from .models import (
    ActionEnvelope,
    ClaimRecord,
    EvidenceRecord,
    MainTurnRecord,
    ResourceRecord,
    ResourceRef,
    TaskRecord,
    UserEvent,
    ViewportSnapshot,
)

__all__ = [
    "ActionEnvelope",
    "ActionType",
    "ClaimRecord",
    "ClaimStatus",
    "DomainError",
    "ErrorCode",
    "EvidenceKind",
    "EvidenceRecord",
    "MainTurnRecord",
    "ResourceRecord",
    "ResourceRef",
    "ResourceStatus",
    "RuntimePhase",
    "TaskRecord",
    "TaskStatus",
    "TurnStatus",
    "UserEvent",
    "ViewportSnapshot",
]
