import asyncio
import random

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

img_path = IMG_PATH / "minashigo" / "孤儿挖矿-活动"

seted = True


async def execute_settings(browser: UserBrowser):
    global seted
    while True:
        await browser.update_frame()
        #browser.script_log(f"execute_settings(seted):{seted}")
        if await browser.match_image(img_path / "1_ok") or await browser.match_image(img_path / "3_skip"):
            return False
        if await browser.match_image(img_path / "1_jx") or await browser.match_image(img_path / "1_sx"):
            if not await browser.match_image(img_path / "1_jx"):
                await browser.click_image(img_path / "1_sx")
                await asyncio.sleep(1)
                continue

            if seted:
                return True
            else:
                await browser.click_image(img_path / "1_set")
                await asyncio.sleep(1)
                continue
        if await browser.match_image(img_path / "2_set"):
            if seted:
                await browser.click_image(img_path / "2_bg", )
                continue
            if await browser.match_image(img_path / "2_seted", use_color_check=True, threshold=0.9999):
                seted = True
                continue
            await browser.click_image(img_path / "2_wd", use_color_check=True)


async def do_work(browser: UserBrowser):
    slide_count = 0
    global seted
    wait_story = False
    while True:
        await browser.update_frame()

        if await browser.click_image(img_path / "1_ok"):
            await asyncio.sleep(1)
            wait_story = False
            if await browser.match_image(img_path / "1_next"):
                wait_story = True
            continue

        if await browser.click_image(img_path / "3_skip") or await browser.click_image(img_path / "3_skip_2") or wait_story:
            await asyncio.sleep(1)
            continue

        if await browser.match_image(img_path / "1_eventStory"):
            if await browser.match_image(img_path / "1_loading"):
                await asyncio.sleep(1)
                continue
            if not seted and len(await browser.match_image_multi(img_path=img_path / '1_story')) > 1:
                if await execute_settings(browser):
                    await asyncio.sleep(1)
                    continue

            if await browser.match_image(img_path / "1_st"):
                if not await browser.click_image(img_path / "1_st",
                                                 pianyi=(random.randint(-100, -50),
                                                         random.randint(-18, -10)),
                                                 match_select="top"):
                    await asyncio.sleep(2)
                    continue
            elif slide_count < 3:
                start = await browser.match_image(img_path / "1_story", match_select="bottom")
                start_co = (start.x, start.y)
                if all(start_co):
                    end_co = (start.x, start.y - 250)
                    await browser.slide(coordinate1=start_co,
                                        coordinate2=end_co,
                                        hold_before=round(random.uniform(0.2, 0.5), 3),
                                        hold_after=round(random.uniform(0.1, 0.5), 3),
                                        slide_time=round(random.uniform(0.5, 1.5), 3))
                    slide_count -= -1
                    await asyncio.sleep(1)
                    if slide_count == 3 and not await browser.match_image(img_path / "1_st"):
                        browser.script_log("已完成一个活动剧情")
                        seted = False
                        slide_count = 0
                    continue
