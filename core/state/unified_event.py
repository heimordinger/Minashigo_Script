# core/state/unified_event.py
from typing import Literal, Any


class UnifiedEvent(dict):
    @property
    def type(self) -> Literal["task", "runtime", "legacy"]:
        return self["type"]

    @property
    def payload(self) -> Any:
        return self["payload"]
