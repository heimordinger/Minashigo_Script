"""Collaborative script Agent V3.

V3 is deliberately isolated from the V2 runtime.  The public surface stays
small until the domain and persistence contracts are stable.
"""

from .domain.enums import RuntimePhase, TaskStatus, TurnStatus
from .domain.errors import DomainError

__all__ = ["DomainError", "RuntimePhase", "TaskStatus", "TurnStatus"]
