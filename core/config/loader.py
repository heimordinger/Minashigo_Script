# core/config/loader.py
import json
from pathlib import Path
from .model import AppConfig

CONFIG_PATH = Path("user/config.json")


def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        return AppConfig()

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return AppConfig(
        chrome_path=Path(data["chrome_path"]) if data.get("chrome_path") else None,
        edge_path=Path(data["edge_path"]) if data.get("edge_path") else None,
        allow_commercial=data.get("allow_commercial", False),
        author_note=data.get("author_note", "")
    )


def save_config(cfg: AppConfig):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({
            "chrome_path": str(cfg.chrome_path) if cfg.chrome_path else None,
            "edge_path": str(cfg.edge_path) if cfg.edge_path else None,
            "allow_commercial": cfg.allow_commercial,
            "author_note": cfg.author_note
        }, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
