# core/state/events.py
from dataclasses import dataclass
from enum import Enum
from datetime import datetime


class StateDomain(Enum):
    RUNTIME = "runtime"
    DEBUG = "debug"
    BROWSER = "browser"
    TASK = "task"
    SYSTEM = "system"


@dataclass
class StateEvent:
    account: str
    domain: StateDomain      # browser / task / system
    key: str                 # status / progress / step
    value: str | int | bool
    message: str | None = None
    timestamp: datetime = datetime.now()
