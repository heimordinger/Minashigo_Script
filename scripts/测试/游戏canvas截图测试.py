"""实验游戏窗口截图（无需重启 main.py；日常 update_frame 仍为全视口）。"""

import importlib

import cv2

import backend.browser.game_frame_capture as _gfc_mod
importlib.reload(_gfc_mod)
from backend.browser.game_frame_capture import capture_game_frame
from backend.browser.user_browser import UserBrowser
from core.path import PROJECT_ROOT


def _stats(img) -> str:
    if img is None:
        return "无图像"
    import numpy as np
    return f"{img.shape[1]}x{img.shape[0]} mean={float(np.mean(img)):.1f}"


async def do_work(browser: UserBrowser):
    page = browser.page
    out_dir = PROJECT_ROOT / "screenshots" / "canvas_test"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        await browser.release_fps("script")
        browser.script_log("已暂停后台截图")
    except Exception as e:
        browser.script_log(f"release_fps: {e}")

    scroll0 = await page.evaluate("() => ({x: scrollX, y: scrollY})")
    browser.script_log(f"scroll_before={scroll0}")

    game_img, clip, meta = await capture_game_frame(
        page,
        dpr=browser.device_pixel_ratio or 1.0,
        restore_scroll=False,
    )

    browser.script_log(f"selector={meta.get('selector')} method={meta.get('method')}")
    browser.script_log(f"box_before={meta.get('box_before')}")
    browser.script_log(f"box_after={meta.get('box_after')}")
    if meta.get("skipped_scroll"):
        browser.script_log("未滚动（游戏已在视口内）")
    elif meta.get("scrolled"):
        browser.script_log("已滚到游戏区，不还原 scroll")
    browser.script_log(f"scroll {meta.get('scroll_before')} → {meta.get('scroll_after')}")

    if game_img is not None:
        p = out_dir / "99_recommended.png"
        cv2.imwrite(str(p), game_img)
        dpr = browser.device_pixel_ratio or 1.0
        exp_w = int(round(clip["width"] * dpr)) if clip else 0
        exp_h = int(round(clip["height"] * dpr)) if clip else 0
        browser.script_log(f"★ {_stats(game_img)} 预期≈{exp_w}x{exp_h} → {p}")
    else:
        browser.script_log("★ 截图失败")

    browser.script_log("日常识图仍用全视口 update_frame()")
