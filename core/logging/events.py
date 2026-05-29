# core/logging/events.py
from dataclasses import dataclass
from enum import Enum
from datetime import datetime


class LogLevel(Enum):
    INFO = "info"
    ERROR = "error"
    DEBUG = "debug"
    WARNING = "warning"


class LogSource(Enum):
    SYSTEM = "system"     # Controller / lifecycle
    BROWSER = "browser"   # Browser 启动、CDP、关闭
    SCRIPT = "scripts"     # 具体脚本执行
    UI = "ui"             # UI 触发


@dataclass(slots=True)
class LogEvent:
    account: str                 # 账号名
    message: str                 # 原始文本
    level: LogLevel = LogLevel.INFO
    source: LogSource = LogSource.SYSTEM
    timestamp: datetime = None   # 自动填充当前时间

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
