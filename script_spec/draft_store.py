"""脚本说明草稿落盘（与 SpecEditor UI 解耦）。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from script_spec.model import ScriptSpec

# 草稿落在本隔离目录下，不写进主工程 user_data
# autosave.json = 工作草稿（编辑自动覆盖）；named/*.json = 具名多版
DRAFT_DIR = Path(__file__).resolve().parent / "drafts"
AUTOSAVE_PATH = DRAFT_DIR / "autosave.json"
LEGACY_DRAFT_PATH = DRAFT_DIR / "draft.json"  # 旧单文件，启动时迁到 autosave
NAMED_DRAFT_DIR = DRAFT_DIR / "named"
DRAFT_AUTOSAVE_MS = 800


def migrate_legacy_draft() -> None:
    """旧 draft.json → autosave.json（仅当 autosave 尚不存在）。"""
    try:
        if LEGACY_DRAFT_PATH.is_file() and not AUTOSAVE_PATH.is_file():
            DRAFT_DIR.mkdir(parents=True, exist_ok=True)
            LEGACY_DRAFT_PATH.replace(AUTOSAVE_PATH)
    except Exception:
        pass


def safe_draft_filename(name: str) -> str:
    raw = (name or "").strip() or "未命名"
    safe = "".join(c if c.isalnum() or c in "-_." or ("\u4e00" <= c <= "\u9fff") else "_" for c in raw)
    safe = safe.strip("._") or "未命名"
    return safe[:48]


def suggest_draft_name(spec: ScriptSpec) -> str:
    goal = (spec.goal or "").strip().splitlines()[0] if (spec.goal or "").strip() else ""
    goal = "".join(goal.split())[:24]
    if goal:
        return goal
    folder = Path((spec.source_dir or "").strip()).name if (spec.source_dir or "").strip() else ""
    if folder:
        return folder
    return datetime.now().strftime("草稿_%m%d_%H%M")


def draft_preview_line(spec: ScriptSpec) -> str:
    goal = (spec.goal or "").strip().replace("\n", " ")
    if goal:
        return goal[:40] + ("…" if len(goal) > 40 else "")
    n_img = sum(1 for e in spec.images if (e.image or "").strip())
    n_task = sum(1 for t in spec.tasks if (t.name or "").strip() or (t.steps or "").strip())
    return f"{n_img} 图 · {n_task} 任务"
