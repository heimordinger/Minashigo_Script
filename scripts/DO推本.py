import asyncio
import random

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

img_path = IMG_PATH / 'DeepOne' / 'DO推本'
STATE_TIMEOUT = {
    "选关": 10,
    "备战": 10,
    "跳过剧情": 15,
    "等待战后结算": 20,
    "未知": 5,
}


async def get_current_state(browser: UserBrowser) -> str:
    await browser.update_frame()
    try:
        # 检查是否在选关页面
        if await asyncio.wait_for(browser.match_image(img_path=img_path / '1_select'), timeout=2):
            return "选关"

        # 检查是否在备战页面
        if await asyncio.wait_for(browser.match_image(img_path=img_path / '2_chuji'), timeout=2):
            return "备战"

        # 检查是否在跳过剧情页面
        if await asyncio.wait_for(browser.match_image(img_path=img_path / '3_skip'), timeout=2):
            return "跳过剧情"

        # 检查是否在战后结算页面
        if await asyncio.wait_for(browser.match_image(img_path=img_path / '4_result'), timeout=2):
            return "等待战后结算"

    except asyncio.TimeoutError:
        return "未知"

    return "未知"


async def do_work(browser: UserBrowser):
    state = await get_current_state(browser)
    state_enter_time = asyncio.get_event_loop().time()
    while True:
        await browser.update_frame()
        browser.script_log(state)

        now = asyncio.get_event_loop().time()

        # ===== 状态超时检测 =====
        timeout = STATE_TIMEOUT.get(state, 100)
        if now - state_enter_time > timeout:
            browser.script_log(f"[STATE TIMEOUT] {state} 超时，重新获取状态")
            state = await get_current_state(browser)
            state_enter_time = now
            continue

        if state == "未知":
            state = await get_current_state(browser)

        if state == "选关":
            # 找到new作战
            if await browser.click_image(img_path=img_path / '1_new'):
                await browser.b_sleep(0.5, 1)
                state = "备战"
                state_enter_time = now
                continue
            if await browser.click_image(img_path=img_path / '1_c', pianyi=(20, 0)):
                continue

        if state == "备战":
            # 点击出击按钮
            if await browser.click_image(img_path=img_path / '2_chuji'):
                await browser.b_sleep(0.5, 1)
                continue
            # 如果看到skip说明已进入剧情
            if await browser.match_image(img_path=img_path / '3_skip'):
                state = "跳过剧情"
                state_enter_time = now
                continue

        if state == "跳过剧情":
            await browser.click_image(img_path=img_path / '3_skip')
            if not await browser.wait_image(img_path=img_path / '3_hai', timeout=2):
                continue
            if await browser.click_image(img_path=img_path / '3_hai'):
                await browser.b_sleep(0.5, 1)
            if (not browser.wait_image(img_path=img_path / '3_hai', timeout=2) or
                    await browser.match_image(img_path=img_path / '4_result')):
                state = "等待战后结算"
                state_enter_time = now
                continue

        if state == "等待战后结算":
            if (await browser.click_image(img_path=img_path / '4_next') or
                    await browser.click_image(img_path=img_path / '4_next_1')):
                await browser.b_sleep(0.5, 1)
                continue
            elif await browser.click_image(img_path=img_path / '4_cihe'):
                await browser.b_sleep(0.5, 1)
                continue
            if await browser.click_image(img_path=img_path / '4_result',
                                         pianyi=(
                                                 random.randint(100, 200),
                                                 random.randint(100, 200))
                                         ):
                await browser.b_sleep(0.1, 0.5)
            if await browser.match_image(img_path=img_path / '3_skip'):
                state = "跳过剧情"
                state_enter_time = now
                continue
            if await browser.match_image(img_path=img_path / '1_new'):
                state = "选关"
                state_enter_time = now
                continue
