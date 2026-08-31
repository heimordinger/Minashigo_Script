"""UI 主题加载：light / dark 纸感工作室双主题。"""

from __future__ import annotations

from pathlib import Path

THEMES = ("light", "dark")
DEFAULT_THEME = "light"
_STYLES_DIR = Path(__file__).resolve().parent
_QSS_FILES = ("main.qss", "account_panel.qss", "script_gen.qss", "script_spec.qss")


def normalize_theme(name: str | None) -> str:
    key = (name or DEFAULT_THEME).strip().lower()
    return key if key in THEMES else DEFAULT_THEME


def theme_dir(theme: str | None = None) -> Path:
    return _STYLES_DIR / normalize_theme(theme)


def load_theme_qss(theme: str | None = None) -> str:
    """拼接指定主题下的 qss 文本。"""
    folder = theme_dir(theme)
    parts: list[str] = []
    for name in _QSS_FILES:
        path = folder / name
        if not path.is_file():
            print(f"[theme] 缺少样式文件: {path}")
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def current_theme_from_config() -> str:
    try:
        from core.config.config import config
        config.load()
        return normalize_theme(config.get("ui.theme"))
    except Exception:
        return DEFAULT_THEME
