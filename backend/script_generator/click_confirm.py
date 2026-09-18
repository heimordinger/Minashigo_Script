# -*- coding: utf-8 -*-
"""方案 F：click_image 确认策略（expect=none|appear|gone）。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional, Union


# 进程级默认；脚本可设 browser.click_confirm = ...
_CLICK_CONFIRM_DEFAULT = "critical"  # off | critical | all


def get_click_confirm_mode(browser=None) -> str:
    mode = getattr(browser, "click_confirm", None) if browser is not None else None
    if mode is None:
        try:
            from backend.script_generator.agent import _load_config
            mode = (
                (_load_config().get("defaults") or {}).get("click_confirm")
                or _CLICK_CONFIRM_DEFAULT
            )
        except Exception:
            mode = _CLICK_CONFIRM_DEFAULT
    mode = str(mode or "off").strip().lower()
    if mode in ("critical", "all", "off", "none", "warn"):
        return "off" if mode == "none" else mode
    return "off"


async def _wait_frame_advance(browser, prev_ts: float, timeout: float = 1.5) -> None:
    """等到至少一帧刷新（或超时）。"""
    deadline = time.time() + max(0.05, timeout)
    while time.time() < deadline:
        ts = float(getattr(browser, "_frame_ts", 0) or 0)
        if ts > prev_ts:
            return
        # UserBrowser 可能把 ts 放在 _browser
        inner = getattr(browser, "_browser", None)
        if inner is not None:
            ts2 = float(getattr(inner, "_frame_ts", 0) or 0)
            if ts2 > prev_ts:
                return
        await asyncio.sleep(0.05)
        ensure = getattr(browser, "update_frame", None) or getattr(browser, "invalidate_frame", None)
        if callable(getattr(browser, "invalidate_frame", None)):
            try:
                browser.invalidate_frame()
            except Exception:
                pass
        if callable(getattr(browser, "update_frame", None)):
            try:
                await browser.update_frame()
            except Exception:
                pass


async def confirm_after_click(
    browser,
    img_path: Union[str, Path],
    *,
    expect: str = "none",
    appear_path: Union[str, Path, None] = None,
    timeout: float = 8.0,
    threshold: float = 0.9,
    stable_ms: float = 350.0,
    poll: float = 0.2,
) -> bool:
    """点击后确认。expect=none 立即 True；appear/gone 轮询。"""
    exp = (expect or "none").strip().lower()
    if exp in ("", "none", "off"):
        return True

    prev_ts = float(
        getattr(browser, "_frame_ts", 0)
        or getattr(getattr(browser, "_browser", None), "_frame_ts", 0)
        or 0
    )
    await _wait_frame_advance(browser, prev_ts, timeout=min(1.5, timeout))

    deadline = time.time() + max(0.3, float(timeout))
    gone_since: float | None = None
    appear = appear_path

    while time.time() < deadline:
        if callable(getattr(browser, "invalidate_frame", None)):
            try:
                browser.invalidate_frame()
            except Exception:
                pass
        if exp == "appear" and appear:
            hit = await browser.match_image(appear, threshold=threshold)
            if hit:
                return True
        elif exp == "gone":
            hit = await browser.match_image(img_path, threshold=threshold)
            if not hit:
                now = time.time()
                if gone_since is None:
                    gone_since = now
                elif (now - gone_since) * 1000 >= stable_ms:
                    return True
            else:
                gone_since = None
        else:
            return True
        await asyncio.sleep(max(0.1, float(poll)))

    return False
