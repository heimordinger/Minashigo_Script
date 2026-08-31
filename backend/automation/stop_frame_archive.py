"""任务异常 / 手动停止时保存最后一帧截图。"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.path import PROJECT_ROOT

STOP_FRAME_DIR = PROJECT_ROOT / "screenshots" / "daily_stop"
MAX_STOP_FRAMES_KEEP = 5


def _safe_account_name(account: dict) -> str:
    name = str(account.get("name", "unknown"))
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "unknown"


def _safe_task_slug(task_name: str) -> str:
    stem = Path(task_name.replace("\\", "/")).stem
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", stem).strip("_")
    return (s[:20] or "task")


def _resolve_frame_holder(target: Any) -> Any:
    if target is None:
        return None
    inner = getattr(target, "_obj", target)
    if hasattr(inner, "_browser"):
        return inner._browser
    return inner


def _get_frame(target: Any) -> np.ndarray | None:
    holder = _resolve_frame_holder(target)
    if holder is None:
        return None
    frame = getattr(holder, "_frame", None)
    if frame is not None:
        return frame
    return getattr(target, "_frame", None)


async def _try_refresh_frame(target: Any) -> None:
    """归档前尽量刷新一帧；绕过 TaskController.check，避免停止时二次抛 TaskStopped。"""
    holder = _resolve_frame_holder(target)
    if holder is None:
        return
    update = getattr(holder, "update_frame", None)
    if update is not None and asyncio.iscoroutinefunction(update):
        try:
            if getattr(target, "use_game_frame_capture", False):
                await update(save_screenshot=False, crop_game_canvas=True)
            else:
                await update(save_screenshot=False)
            return
        except Exception:
            pass
    capture = getattr(target, "_capture_raw", None)
    if capture is not None and asyncio.iscoroutinefunction(capture):
        try:
            await capture()
        except Exception:
            pass


def _rotate_stop_frames(account_safe: str, keep: int) -> None:
    if not STOP_FRAME_DIR.is_dir():
        return
    files = sorted(
        STOP_FRAME_DIR.glob(f"{account_safe}_*.png"),
        key=lambda p: p.stat().st_mtime,
    )
    for path in files[:-keep]:
        try:
            path.unlink()
        except OSError:
            pass


async def save_task_stop_frame(
    target: Any,
    *,
    account: dict,
    reason: str,
    task_name: str = "",
) -> str | None:
    """
    保存结束帧。reason: stopped | error
    返回相对路径字符串；无帧时返回 None。
    """
    try:
        await _try_refresh_frame(target)
        frame = _get_frame(target)
        if frame is None:
            return None

        STOP_FRAME_DIR.mkdir(parents=True, exist_ok=True)
        account_safe = _safe_account_name(account)
        ts = time.strftime("%Y%m%d_%H%M%S")
        slug = _safe_task_slug(task_name) if task_name else "task"
        filename = f"{account_safe}_{ts}_{reason}_{slug}.png"
        path = STOP_FRAME_DIR / filename
        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            return None
        buf.tofile(str(path))
        _rotate_stop_frames(account_safe, MAX_STOP_FRAMES_KEEP)
        return f"screenshots/daily_stop/{filename}"
    except Exception:
        return None
