from dataclasses import dataclass, field


@dataclass
class UIState:
    app_status: str = "Idle"
    message: str = "UI Ready"

    accounts: list[dict] = field(default_factory=list)
    current_account: dict | None = None

    task_rows: list = field(default_factory=list)
