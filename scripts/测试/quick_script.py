import asyncio
from pathlib import Path

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

IMG_DIR = IMG_PATH / "quick_script"


async def do_work(browser: UserBrowser):
    """快速脚本: quick_script (5 步)"""

    # ── 步骤1：在 (1341,752) 处点击 217×61 的按钮 ──
    await browser.click_image(
        img_path=IMG_DIR / "step_001.png",
        threshold=0.85,
    )
    await asyncio.sleep(0.5)

    # ── 步骤2：在 (408,874) 处点击 56×58 的按钮 ──
    await browser.click_image(
        img_path=IMG_DIR / "step_002.png",
        threshold=0.85,
    )
    await asyncio.sleep(0.5)

    # ── 步骤3：在 (493,877) 处点击 53×52 的按钮 ──
    await browser.click_image(
        img_path=IMG_DIR / "step_003.png",
        threshold=0.85,
    )
    await asyncio.sleep(0.5)

    # ── 步骤4：在 (570,876) 处点击 60×60 的按钮 ──
    await browser.click_image(
        img_path=IMG_DIR / "step_004.png",
        threshold=0.85,
    )
    await asyncio.sleep(0.5)

    # ── 步骤5：在 (656,873) 处点击 60×60 的按钮 ──
    await browser.click_image(
        img_path=IMG_DIR / "step_005.png",
        threshold=0.85,
    )
    await asyncio.sleep(0.5)
