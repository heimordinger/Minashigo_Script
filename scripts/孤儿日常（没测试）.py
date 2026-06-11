import random

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

img_path = IMG_PATH / 'minashigo' / '孤儿日常'


async def get_scene(browser: UserBrowser) -> str:
    await browser.update_frame()

    if await browser.match_image(img_path / "home_rank"):
        return "主页"

    if await browser.match_image(img_path / "quest_quest") or \
            await browser.match_image(img_path / "quest_skip"):
        return "关卡"

    if await browser.match_image(img_path / "shop_shop"):
        return "商店"

    if await browser.match_image(img_path / "guild_guild"):
        return "公会"

    if await browser.match_image(img_path / "daily_daily"):
        return "每日任务"

    return "未知"


async def go_home(browser: UserBrowser):
    while True:
        await browser.update_frame()
        if await browser.match_image(img_path / "home_rank"):
            return True
        if await browser.click_image(img_path / 'home_home'):
            await browser.b_sleep(0.5,1)
            continue
        elif await browser.click_image(img_path / 'home_back'):
            await browser.b_sleep(0.5,1)
            continue


async def do_work(browser: UserBrowser):
    quested = False
    shoped = False
    guilded = False
    dailyed = False
    sence = await get_scene(browser)
    while True:
        await browser.update_frame()

        # ===== 全部完成 =====
        if dailyed:
            break

        # ===== 决策层 =====
        if not quested:
            target = "关卡"
        elif not shoped:
            target = "商店"
        elif not guilded:
            target = "公会"
        else:
            target = "每日任务"

        browser.script_log(f"当前场景: {sence}，目标: {target}")

        if target == "关卡":
            if sence != target:
                await browser.click_image(img_path / "quest")
                await browser.b_sleep(0.5, 1)
                sence = "关卡"
                continue
            if await browser.match_image(img_path / "quest_quest"):
                imgs = [
                    ("quest_tf", {"use_color_check": True}),
                    ("quest_skip", {}),
                    ("quest_skip_1", {}),
                    ("quest_cihe", {}),
                ]
                for name, kwargs in imgs:
                    if await browser.click_image(img_path / name, **kwargs):
                        await browser.b_sleep(0.5, 1)
                        continue
                await browser.click_image(img_path / "quest_quest")
                quested = True
            continue

        if target == "商店":
            if sence != target:
                await browser.click_image(img_path / "shop")
                await browser.b_sleep(0.5, 1)
                sence = "商店"
                continue

        if target == "公会":
            if sence != target:
                await browser.click_image(img_path / "guild_guild")

        if target == "每日任务":
            if sence != target:
                await go_home(browser)
