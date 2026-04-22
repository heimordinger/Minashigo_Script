from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TaskStatus(Enum):
    FINISHED = "finished"
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass(frozen=True)
class TaskSnapshot:
    browser: str
    script: Optional[str]
    status: TaskStatus
    step: str
    message: str
