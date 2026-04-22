# scripts/DO登录.py
from enum import Enum, auto
from urllib.parse import urlparse

from backend.browser.user_browser import UserBrowser
from core.path import PROJECT_ROOT


def extract_game_path(url: str) -> str:
    return urlparse(url).path.strip("/")


async def login(browser: UserBrowser, url: str):
    ENTRY_URL = url
    FINAL_GAME_PATH = extract_game_path(url)

    LOGIN_KW = "accounts.dmm.co.jp"
    REGION_BLOCK_KW = "not-available-in-your-region"

    ERROR_TITLE_KW = ["予期せぬエラー", "エラーが発生", "error"]
    LOADING_TITLE_KW = ["loading", "読み込み"]

    stable_game_count = 0
    region_retry = 0
    error_retry = 0

    MAX_REGION_RETRY = 5
    MAX_ERROR_RETRY = 5

    browser.script_log(f"目标游戏路径: {FINAL_GAME_PATH}")

    cur_url = await browser.get_url
    if FINAL_GAME_PATH not in cur_url and LOGIN_KW not in cur_url:
        await browser.goto(ENTRY_URL)

    while True:
        await browser.b_sleep(0.5)

        cur_url = await browser.get_url
        cur_title = (await browser.get_title).lower()

        # ---------- 地区限制 ----------
        if REGION_BLOCK_KW in cur_url:
            region_retry += 1
            stable_game_count = 0

            browser.script_log(f"检测到地区限制，第 {region_retry} 次重试")

            if region_retry >= MAX_REGION_RETRY:
                raise RuntimeError("VPN 不稳定，地区限制持续存在")

            await browser.goto(ENTRY_URL)
            continue

        # ---------- 登录页 ----------
        if LOGIN_KW in cur_url or "ログイン" in cur_title:
            stable_game_count = 0
            error_retry = 0
            await browser.dmm_login(game_name="deepone")
            continue

        # ---------- 游戏页 ----------
        if FINAL_GAME_PATH in cur_url:

            if any(k in cur_title for k in ERROR_TITLE_KW):
                error_retry += 1
                stable_game_count = 0

                browser.script_log(
                    f"检测到错误页（{cur_title}），第 {error_retry} 次重试"
                )

                if error_retry >= MAX_ERROR_RETRY:
                    raise RuntimeError("游戏页持续错误，疑似网络或服务器异常")

                # ⚠️ 不要直接回 ENTRY_URL，避免循环放大
                await browser.b_sleep(2)
                await browser.reload()
                continue

            if any(k in cur_title for k in LOADING_TITLE_KW):
                stable_game_count = 0
                continue

            error_retry = 0
            stable_game_count += 1

            if stable_game_count >= 3:
                browser.script_log("进入游戏完成（状态稳定）")
                break


class DOLoginState(Enum):
    WAIT_GAME_LOAD = auto()
    INIT = auto()
    START_DIALOG = auto()
    TAP_SCREEN = auto()
    SKIP_ANIM = auto()
    CLOSE_POPUP = auto()
    CHECK_DONE = auto()
    NETWORK_ERROR = auto()
    DONE = auto()


async def do_work(browser: UserBrowser):
    img_path = PROJECT_ROOT / "assets" / "images" / "DeepOne" / "DO登录"

    await login(browser, "https://play.games.dmm.co.jp/game/deeponer")

    state = DOLoginState.WAIT_GAME_LOAD

    skip_click_count = 0
    network_retry = 0

    wait_count = 0
    MAX_WAIT_COUNT = 30

    # =========================
    # 🔥 新增：稳定性保护
    # =========================
    startup_frame = 0
    err_hit_count = 0

    while state != DOLoginState.DONE:
        await browser.update_frame()

        # ---------- 启动冷却（关键修复） ----------
        if startup_frame < 8:
            startup_frame += 1
            await browser.b_sleep(0.5)
            continue

        # ---------- 网络异常检测（防抖版） ----------
        if state not in (DOLoginState.NETWORK_ERROR, DOLoginState.DONE):

            if await browser.match_image(img_path / "err1") or \
               await browser.match_image(img_path / "err3"):
                err_hit_count += 1
            else:
                err_hit_count = 0

            if err_hit_count >= 3:
                browser.script_log("检测到稳定网络异常")
                state = DOLoginState.NETWORK_ERROR
                continue

        # ---------- WAIT ----------
        if state == DOLoginState.WAIT_GAME_LOAD:
            wait_count += 1

            if await browser.match_image(img_path / "rank"):
                browser.script_log("直接进入主界面")
                state = DOLoginState.DONE
                continue

            if await browser.match_image(img_path / "logo"):
                state = DOLoginState.TAP_SCREEN
                continue

            if await browser.match_image(img_path / "start1"):
                state = DOLoginState.START_DIALOG
                continue

            if wait_count >= MAX_WAIT_COUNT:
                browser.script_log("加载超时，重置流程")
                state = DOLoginState.START_DIALOG
                wait_count = 0

            await browser.b_sleep(0.5)
            continue

        # ---------- INIT ----------
        if state == DOLoginState.INIT:
            state = DOLoginState.WAIT_GAME_LOAD
            continue

        # ---------- START ----------
        if state == DOLoginState.START_DIALOG:
            if await browser.match_image(img_path / "start1"):
                await browser.click_image(img_path / "start2")
            state = DOLoginState.WAIT_GAME_LOAD
            continue

        # ---------- TAP ----------
        if state == DOLoginState.TAP_SCREEN:
            clicked = await browser.click_image(img_path / "logo", pianyi=(10, 0))
            state = DOLoginState.SKIP_ANIM
            continue

        # ---------- SKIP ----------
        if state == DOLoginState.SKIP_ANIM:
            clicked = await browser.click_image(
                img_path / "skip",
                max_delay=1,
                pianyi=(skip_click_count % 3 - 1, 0)
            )

            if clicked:
                skip_click_count += 1
                if skip_click_count >= 3:
                    state = DOLoginState.CLOSE_POPUP
            else:
                state = DOLoginState.CLOSE_POPUP
            continue

        # ---------- CLOSE ----------
        if state == DOLoginState.CLOSE_POPUP:
            await browser.click_image(img_path / "关闭", max_delay=1)
            state = DOLoginState.CHECK_DONE
            continue

        # ---------- NETWORK ERROR ----------
        if state == DOLoginState.NETWORK_ERROR:
            network_retry += 1

            browser.script_log(
                f"网络异常处理中 {network_retry}/5"
            )

            clicked = await browser.click_image(img_path / "err2") or \
                      await browser.click_image(img_path / "err4")

            if clicked:
                await browser.b_sleep(2)
                network_retry = 0
                err_hit_count = 0
                state = DOLoginState.WAIT_GAME_LOAD
            else:
                await browser.b_sleep(1)

                # ⚠️ 不再疯狂重试，回到 WAIT 而不是循环 error
                state = DOLoginState.WAIT_GAME_LOAD
            continue

        # ---------- DONE CHECK ----------
        if state == DOLoginState.CHECK_DONE:
            if await browser.match_image(img_path / "rank"):
                state = DOLoginState.DONE
            else:
                skip_click_count = 0
                state = DOLoginState.SKIP_ANIM
            continue