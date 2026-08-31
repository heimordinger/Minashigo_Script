"""游戏区截图：尽量不滚页；必须滚时只滚一次且不还原（避免双向抖动）。"""

from __future__ import annotations

import asyncio
from typing import Optional, Tuple

import cv2
import numpy as np

GAME_AREA_SELECTORS = (
    "#GameCanvas",
    "canvas#GameCanvas",
    "canvas.gameCanvas",
    "#Cocos2dGameContainer",
    "#GameDiv",
)

_DEFAULT_HEADER_CSS = 60
# GameCanvas 在登录/过场时可能晚于页面出现，bounding_box 不宜过短
_GAME_BOX_TIMEOUT_MS = 15_000


async def find_game_locator(page, *, box_timeout_ms: int = _GAME_BOX_TIMEOUT_MS):
    for frame in page.frames:
        for selector in GAME_AREA_SELECTORS:
            loc = frame.locator(selector).first
            try:
                if await loc.count() == 0:
                    continue
                box = await loc.bounding_box(timeout=box_timeout_ms)
            except Exception:
                continue
            if not box or box.get("width", 0) < 100 or box.get("height", 0) < 100:
                continue
            return frame, selector, loc, box
    return None, None, None, None


def _box_clip(box: dict) -> dict:
    return {
        "x": max(0.0, box["x"]),
        "y": max(0.0, box["y"]),
        "width": box["width"],
        "height": box["height"],
    }


async def _get_viewport_css(page) -> dict:
    vp = page.viewport_size
    if vp and vp.get("width") and vp.get("height"):
        return {"width": int(vp["width"]), "height": int(vp["height"])}
    inner = await page.evaluate(
        "() => ({ width: window.innerWidth, height: window.innerHeight })"
    )
    return {"width": int(inner["width"]), "height": int(inner["height"])}


async def _is_box_fully_visible(page, box: dict, margin: float = 2) -> bool:
    return await page.evaluate(
        """
        ([vx, vy, vw, vh, m]) => {
            return vx >= m && vy >= m
                && (vx + vw) <= window.innerWidth - m
                && (vy + vh) <= window.innerHeight - m;
        }
        """,
        [box["x"], box["y"], box["width"], box["height"], margin],
    )


async def _scroll_game_box_to_view(page, box: dict, header_css: float) -> None:
    await page.evaluate(
        """
        ([vy, header]) => {
            const docTop = vy + window.scrollY;
            window.scrollTo({
                top: Math.max(0, docTop - header),
                left: window.scrollX,
                behavior: 'instant',
            });
        }
        """,
        [box["y"], header_css],
    )


def _crop_viewport_frame(full: np.ndarray, clip: dict, dpr: float) -> Optional[np.ndarray]:
    x1 = max(0, int(round(clip["x"] * dpr)))
    y1 = max(0, int(round(clip["y"] * dpr)))
    x2 = min(full.shape[1], int(round((clip["x"] + clip["width"]) * dpr)))
    y2 = min(full.shape[0], int(round((clip["y"] + clip["height"]) * dpr)))
    if x2 <= x1 or y2 <= y1:
        return None
    out = full[y1:y2, x1:x2].copy()
    # 不要求裁出完整 CSS box：canvas 常比视口大，旧 95% 校验会整段失败回退 full
    if out.shape[0] < 80 or out.shape[1] < 80:
        return None
    return out


async def _screenshot_crop(page, clip: dict, dpr: float) -> Tuple[Optional[np.ndarray], Optional[tuple]]:
    png = await page.screenshot(timeout=60_000)
    full = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if full is None:
        return None, None
    img = _crop_viewport_frame(full, clip, dpr)
    return img, (full.shape[1], full.shape[0])


async def _element_screenshot(loc) -> Optional[np.ndarray]:
    try:
        png = await loc.screenshot(timeout=30_000)
    except Exception:
        return None
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.shape[0] < 80 or img.shape[1] < 80:
        return None
    return img


async def capture_game_frame(
    page,
    *,
    dpr: float = 1.0,
    header_css: float = _DEFAULT_HEADER_CSS,
    restore_scroll: bool = False,
) -> Tuple[Optional[np.ndarray], Optional[dict], dict]:
    """
    截取 #GameCanvas：
    - 已在视口内 → 不滚动，1 次 screenshot + 裁剪
    - 不在视口内 → 滚一次，默认不滚回（避免双向抖动）
    - 视口裁剪失败 → 元素截图兜底
    """
    meta: dict = {}

    _frame, selector, loc, box = await find_game_locator(page)
    meta["selector"] = selector
    if not loc or not box:
        return None, None, meta

    orig_scroll = await page.evaluate("() => ({x: scrollX, y: scrollY})")
    meta["scroll_before"] = orig_scroll
    meta["box_before"] = dict(box)

    scrolled = False
    if not await _is_box_fully_visible(page, box):
        await _scroll_game_box_to_view(page, box, header_css)
        scrolled = True
        await asyncio.sleep(0.03)
        try:
            box = await loc.bounding_box(timeout=_GAME_BOX_TIMEOUT_MS)
        except Exception:
            box = None
        if not box:
            if restore_scroll and scrolled:
                await page.evaluate("(s) => window.scrollTo(s.x, s.y)", orig_scroll)
            return None, None, meta
    else:
        meta["skipped_scroll"] = True

    meta["box_after"] = dict(box)
    meta["scroll_after"] = await page.evaluate("() => ({x: scrollX, y: scrollY})")
    meta["scrolled"] = scrolled

    clip = _box_clip(box)
    meta["clip"] = clip

    img, full_shape = await _screenshot_crop(page, clip, dpr)
    meta["method"] = "viewport_crop"
    meta["full_shape"] = full_shape

    if img is None:
        meta["viewport_crop_failed"] = True
        elem = await _element_screenshot(loc)
        if elem is not None:
            img = elem
            meta["method"] = "element"
            # 元素截图已是游戏区本体，原点仍用 clip
            full_shape = (elem.shape[1], elem.shape[0])

    if img is None and scrolled and not await _is_box_fully_visible(page, box):
        meta["crop_incomplete"] = True

    if restore_scroll and scrolled:
        await page.evaluate("(s) => window.scrollTo(s.x, s.y)", orig_scroll)
        meta["scroll_restored"] = await page.evaluate("() => ({x: scrollX, y: scrollY})")
    else:
        meta["scroll_restored"] = None

    return img, clip, meta


async def capture_game_from_viewport(page, *, dpr: float = 1.0, **kwargs):
    return await capture_game_frame(page, dpr=dpr, **kwargs)


async def align_game_viewport(
    page,
    *,
    header_css: float = _DEFAULT_HEADER_CSS,
) -> dict:
    """将 GameCanvas 滚入视口（识图前调用，不截图）。"""
    meta: dict = {"found": False}
    _frame, selector, loc, box = await find_game_locator(page)
    meta["selector"] = selector
    if not loc or not box:
        return meta
    meta["found"] = True
    meta["box"] = dict(box)
    if await _is_box_fully_visible(page, box):
        meta["scrolled"] = False
        return meta
    await _scroll_game_box_to_view(page, box, header_css)
    meta["scrolled"] = True
    await asyncio.sleep(0.25)
    return meta
