# core/config/model.py
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AppConfig:
    chrome_path: Path | None = None
    edge_path: Path | None = None
    allow_commercial: bool = False
    author_note: str = "仅供学习交流使用"
