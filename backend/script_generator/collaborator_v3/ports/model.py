"""Logic-model port.  Provider SDKs belong in adapters, not the runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..domain.models import ActionEnvelope


@dataclass(frozen=True)
class ModelRequest:
    turn_id: str
    user_text: str
    protocol: dict[str, Any]
    result_summary: dict[str, Any] | None = None
    workspace_summary: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelReply:
    action: ActionEnvelope
    model_id: str
    model_version: str


class LogicModel(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def invoke(self, request: ModelRequest) -> ModelReply: ...
