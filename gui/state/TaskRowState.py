from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TaskRowState:
    account_name: str
    available_tasks: List[str]
    selected_task: Optional[str] | None
    status: str
    running: bool