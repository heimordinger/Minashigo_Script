"""
DO日常 —— 每日日常脚本
流程：①房间领体力 → ②竞技场 → ③爬塔 → ④每日关卡
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH


@dataclass
class Config:
    img_dir: Path = IMG_PATH / 'DeepOne' / 'DO日常'
    threshold: float = 0.9
    nav_threshold: float = 0.8
    high_threshold: float = 0.99
    use_polling_cache: bool = True

CFG = Config()


def _img(name: str) -> Path:
    return CFG.img_dir / (name if name.endswith('.png') else name + '.png')


# ── 场景识别 ──

async def detect_scene(browser: UserBrowser) -> str | None:
    scenes = {
        "home":   _img('rank'),
        "room":   _img('room_logo'),
        "sorite": _img('出击_logo'),
    }
    results = await asyncio.gather(*[
        browser.match_image(path, threshold=CFG.nav_threshold)
        for path in scenes.values()
    ])
    for name, r in zip(scenes.keys(), results):
        if r:
            return name
    return None


# ── 辅助: 返回主界面 ──

async def go_home(browser: UserBrowser) -> None:
    """左上角返回按钮 → 直到 rank 出现"""
    s = await detect_scene(browser)
    browser.script_log(f"[go_home] 当前={s}")
    if s == "home":
        return
    for i in range(15):
        browser.script_log(f"[go_home] 尝试{i+1}")
        await browser.click(30, 30)
        await browser.b_sleep(1.0, 1.5)
        await browser.update_frame()
        if await detect_scene(browser) == "home":
            browser.script_log("  ✓ 回到主界面")
            return
    raise RuntimeError("无法回到主界面")


# ── 辅助: 返回出击界面 ──

async def go_sorite(browser: UserBrowser) -> None:
    """回主界面 → 点出击按钮 → 直到出击_logo出现"""
    if await detect_scene(browser) == "sorite":
        return
    await go_home(browser)
    for i, (x, y) in enumerate([(400, 550), (500, 550), (600, 550)]):
        browser.script_log(f"[go_sorite] 试坐标({x},{y})")
        await browser.click(x, y)
        await browser.b_sleep(1.5, 2.5)
        await browser.update_frame()
        if await detect_scene(browser) == "sorite":
            browser.script_log("  ✓ 到达出击界面")
            return
    raise RuntimeError("无法到达出击界面")


# ── 1) 房间领体力 ──

async def room_task(browser: UserBrowser) -> None:
    await go_home(browser)
    browser.script_log("[房间] 点 room 按钮")
    await browser.click(150, 300)
    await browser.b_sleep(1.0, 1.5)
    await browser.update_frame()
    browser.script_log("[房间] 等待 room_logo")
    if not await browser.wait_image(_img('room_logo'), timeout=5):
        browser.script_log("[房间] 房间加载超时")
        return
    browser.script_log("[房间] room_logo 确认")

    browser.script_log("[房间] 检查 room_收取奖励")
    if not await browser.match_image(_img('room_收取奖励'), threshold=CFG.threshold):
        browser.script_log("[房间] 没有可领取奖励")
        return
    browser.script_log("[房间] 有奖励，点击领取")
    await browser.click_image(_img('room_收取奖励'), threshold=CFG.threshold)
    await browser.b_sleep(0.3, 0.5)
    for _ in range(3):
        await browser.b_sleep(0.5)
        if await browser.match_image(_img('room_收取奖励'), threshold=CFG.threshold):
            browser.script_log("[房间] AP上限")
            return

    browser.script_log("[房间] 等待 room_ok")
    if await browser.wait_image(_img('room_ok'), timeout=3):
        browser.script_log("[房间] 点 ok")
        await browser.click_image(_img('room_ok'), threshold=CFG.threshold)
        await browser.b_sleep(0.5, 1.0)
        browser.script_log("[房间] 检查二次 ok")
        if await browser.match_image(_img('room_ok'), threshold=CFG.threshold):
            browser.script_log("[房间] 二次 ok")
            await browser.click_image(_img('room_ok'), threshold=CFG.threshold)
            await browser.b_sleep(0.3, 0.5)
    else:
        browser.script_log("[房间] 未出现 ok")
    browser.script_log("[房间] ✓")


# ── 2) 竞技场 ──

async def jjc_task(browser: UserBrowser) -> None:
    await go_sorite(browser)
    # 点 jjc 按钮
    for x, y in [(200, 300), (250, 250)]:
        await browser.click(x, y)
        await browser.b_sleep(1.5, 2.5)
        await browser.update_frame()
        if await browser.match_image(_img('jjc_logo'), threshold=CFG.nav_threshold):
            break
    else:
        browser.script_log("[竞技场] 进入竞技场失败")
        return

    for _ in range(50):
        await browser.update_frame()
        if await browser.match_image(_img('jjc_end'), threshold=CFG.high_threshold):
            browser.script_log("[竞技场] 次数耗尽")
            return

        for __ in range(15):
            if await browser.match_image(_img('jjc_倍率'), threshold=CFG.threshold):
                break
            await browser.click_image(_img('jjc_刷新'), pianyi=(-20, 0), threshold=CFG.threshold)
            await browser.b_sleep(0.3, 0.5)

        if not await browser.click_image(_img('jjc_段位'), threshold=CFG.threshold):
            await browser.b_sleep(0.5)
            continue
        await browser.b_sleep(1.0, 1.5)
        if not await browser.wait_image(_img('jjc_出击'), timeout=5):
            continue
        await browser.click_image(_img('jjc_出击'), threshold=CFG.threshold)
        await browser.b_sleep(2.0, 3.0)

        if await browser.match_image(_img('jjc_ok'), threshold=CFG.threshold):
            await browser.click_image(_img('jjc_ok'), threshold=CFG.threshold)
            await browser.b_sleep(0.5, 1.0)
        if await browser.wait_image(_img('jjc_结算'), timeout=30):
            await browser.click_image(_img('jjc_结算'), pianyi=(150, 80), threshold=CFG.nav_threshold)
            await browser.b_sleep(1.0, 1.5)
    browser.script_log("[竞技场] ✓")


# ── 3) 爬塔 ──

async def ta_task(browser: UserBrowser) -> None:
    await go_sorite(browser)
    for x, y in [(350, 300), (400, 300)]:
        await browser.click(x, y)
        await browser.b_sleep(1.5, 2.5)
        await browser.update_frame()
        if await browser.match_image(_img('ta_logo'), threshold=CFG.nav_threshold):
            break
    else:
        browser.script_log("[爬塔] 进入塔失败")
        return

    for _ in range(30):
        await browser.update_frame()
        if await browser.match_image(_img('ta_cishu'), threshold=CFG.nav_threshold):
            browser.script_log("[爬塔] 次数耗尽")
            return
        for __ in range(20):
            if await browser.match_image(_img('ta_nandu'), threshold=CFG.threshold):
                break
            await browser.click_image(_img('ta_biancheng'), pianyi=(20, -130), threshold=CFG.threshold)
            await browser.b_sleep(0.2, 0.4)
        else:
            continue
        await browser.click_image(_img('ta_nandu'), threshold=CFG.threshold)
        await browser.b_sleep(1.0, 1.5)
        if not await browser.wait_image(_img('ta_tiaozhan'), timeout=5):
            continue
        await browser.click_image(_img('ta_tiaozhan'), threshold=CFG.threshold)
        await browser.b_sleep(1.0, 2.0)
        if not await browser.wait_image(_img('ta_chuji'), timeout=5):
            continue
        await browser.click_image(_img('ta_chuji'), threshold=CFG.threshold)
        await browser.b_sleep(0.5, 1.0)
        if await browser.match_image(_img('ta_auto1'), threshold=CFG.threshold):
            await browser.click_image(_img('ta_auto1'), threshold=CFG.threshold)
            await browser.b_sleep(0.3, 0.5)
        if not await browser.wait_image(_img('ta_chuji_chuji'), timeout=3):
            continue
        await browser.click_image(_img('ta_chuji_chuji'), threshold=CFG.threshold)
        start = asyncio.get_event_loop().time()
        while True:
            await browser.update_frame()
            if await browser.match_image(_img('ta_jiangli'), threshold=CFG.nav_threshold):
                await browser.b_sleep(0.5, 1.0)
                await browser.click_image(_img('ta_ok'), threshold=CFG.threshold)
                await browser.b_sleep(0.5, 1.0)
                break
            if asyncio.get_event_loop().time() - start > 300:
                break
            await browser.b_sleep(0.3, 0.5)
    browser.script_log("[爬塔] ✓")


# ── 4) 每日关卡 ──

async def meiri_task(browser: UserBrowser) -> None:
    await go_sorite(browser)
    for x, y in [(500, 300), (550, 300)]:
        await browser.click(x, y)
        await browser.b_sleep(1.0, 2.0)
        await browser.update_frame()
        if await browser.match_image(_img('meiri_logo'), threshold=CFG.nav_threshold):
            break
    else:
        browser.script_log("[每日] 进入每日关卡失败")
        return

    for name, img in [("扫荡", 'meiri_skip'), ("确认", 'meiri_skip_skip')]:
        if not await browser.click_image(_img(img), threshold=CFG.threshold):
            browser.script_log(f"[每日] 找不到 {name} 按钮")
            return
        await browser.b_sleep(0.5, 1.0)

    if await browser.wait_image(_img('meiri_ok'), timeout=60):
        await browser.click_image(_img('meiri_ok'), threshold=CFG.threshold)
        await browser.b_sleep(0.5, 1.0)
        browser.script_log("[每日] ✓")
    else:
        browser.script_log("[每日] 扫荡超时")


# ── 主流程 ──

async def do_work(browser: UserBrowser):
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True

    scene = await detect_scene(browser)
    browser.script_log(f"[DO日常] 当前场景: {scene or '未知'}")
    browser.script_log("===== 开始 =====")

    tasks = [("房间领体力", room_task), ("竞技场", jjc_task),
             ("爬塔", ta_task), ("每日关卡", meiri_task)]

    for name, fn in tasks:
        browser.script_log(f"--- {name} ---")
        await fn(browser)
        browser.script_log(f"✓ {name}")

    browser.script_log("===== 全部完成 =====")
