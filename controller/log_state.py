# controller/log_state.py
from dataclasses import dataclass
from enum import Enum


class LogLevel(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    DEBUG = "DEBUG"


@dataclass
class LogEvent:
    browser: str
    level: LogLevel
    message: str
