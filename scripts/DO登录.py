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

    ERROR_TITLE_KW = [
        "予期せぬエラー",
        "エラーが発生",
        "error"
    ]

    LOADING_TITLE_KW = [
        "loading",
        "読み込み"
    ]

    stable_game_count = 0
    region_retry = 0
    error_retry = 0

    MAX_REGION_RETRY = 5
    MAX_ERROR_RETRY = 5

    browser.script_log(f"目标游戏路径: {FINAL_GAME_PATH}")

    # 初始跳转
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
        if FINAL_GAME_PATH not in cur_url and (LOGIN_KW in cur_url or "ログイン" in cur_title):
            stable_game_count = 0
            error_retry = 0
            await browser.dmm_login()
            continue

        # ---------- 最终游戏路径 ----------
        if FINAL_GAME_PATH in cur_url:
            if any(k in cur_title for k in ERROR_TITLE_KW):
                error_retry += 1
                stable_game_count = 0

                browser.script_log(
                    f"检测到错误页（{cur_title}），第 {error_retry} 次重试"
                )

                if error_retry >= MAX_ERROR_RETRY:
                    raise RuntimeError("游戏页持续错误，疑似网络或服务器异常")

                await browser.goto(ENTRY_URL)
                continue

            if any(k in cur_title for k in LOADING_TITLE_KW):
                stable_game_count = 0
                continue

            error_retry = 0
            stable_game_count += 1

            if stable_game_count >= 3:
                browser.script_log("进入游戏完成（状态稳定）")
                break

            continue


class DOLoginState(Enum):
    WAIT_GAME_LOAD = auto()  # 等待游戏加载（新增状态）
    INIT = auto()  # 初始
    START_DIALOG = auto()  # 第一次登录提示框
    TAP_SCREEN = auto()  # 点击任意位置进入
    SKIP_ANIM = auto()  # 跳过签到/演出
    CLOSE_POPUP = auto()  # 关闭弹窗
    CHECK_DONE = auto()  # 判断是否已进入主界面
    NETWORK_ERROR = auto()  # 网络异常 / 需要重连
    DONE = auto()  # 完成


async def do_work(browser: UserBrowser):
    img_path = PROJECT_ROOT / "assets" / "images" / "DeepOne" / "DO登录"

    # 先执行基础登录流程，确保进入游戏页面
    await login(browser, "https://play.games.dmm.co.jp/game/deeponer")

    state = DOLoginState.WAIT_GAME_LOAD  # 从等待加载开始
    skip_click_count = 0
    network_retry = 0
    wait_count = 0
    MAX_NETWORK_RETRY = 5
    MAX_WAIT_COUNT = 30  # 最大等待次数（约15秒）

    while state != DOLoginState.DONE:
        await browser.update_frame()

        # ========== 【最高优先级】网络异常检测 ==========
        if state not in (DOLoginState.NETWORK_ERROR, DOLoginState.DONE):
            if await browser.match_image(img_path / "err1"):
                browser.script_log("检测到网络异常弹窗")
                state = DOLoginState.NETWORK_ERROR
                continue
            if await browser.match_image(img_path / "err3"):
                browser.script_log("检测到网络异常弹窗")
                state = DOLoginState.NETWORK_ERROR
                continue

        # ---------- WAIT_GAME_LOAD：等待游戏界面加载 ----------
        if state == DOLoginState.WAIT_GAME_LOAD:
            wait_count += 1

            # 检查是否已经进入主界面（直接完成的情况）
            if await browser.match_image(img_path / "rank"):
                browser.script_log("游戏已直接进入主界面")
                state = DOLoginState.DONE
                continue

            # 检查是否出现logo（第二阶段界面）
            if await browser.match_image(img_path / "logo"):
                browser.script_log("检测到游戏logo，开始登录流程")
                state = DOLoginState.TAP_SCREEN
                continue

            # 检查是否出现start1（第一阶段登录提示）
            if await browser.match_image(img_path / "start1"):
                browser.script_log("检测到初始登录提示")
                state = DOLoginState.START_DIALOG
                continue

            # 超时处理
            if wait_count >= MAX_WAIT_COUNT:
                browser.script_log("等待游戏加载超时，重新从START_DIALOG开始")
                state = DOLoginState.START_DIALOG

            await browser.b_sleep(0.5)
            continue

        # ---------- INIT（保留作为备选）----------
        if state == DOLoginState.INIT:
            browser.script_log("DO登录：INIT")
            # 直接进入等待加载状态
            state = DOLoginState.WAIT_GAME_LOAD
            continue

        # ---------- 第一次登录提示 ----------
        if state == DOLoginState.START_DIALOG:
            if await browser.match_image(img_path / "start1"):
                await browser.click_image(img_path / "start2")
                browser.script_log("点击了登录开始按钮")
                state = DOLoginState.WAIT_GAME_LOAD  # 点击后继续等待
            else:
                # 如果找不到start1，可能已经进入后续阶段
                browser.script_log("未检测到start1，尝试检测logo")
                state = DOLoginState.WAIT_GAME_LOAD
            await browser.b_sleep(0.5)
            continue

        # ---------- 点击任意位置（logo界面）----------
        if state == DOLoginState.TAP_SCREEN:
            browser.script_log("尝试点击logo进入游戏")
            clicked = await browser.click_image(
                img_path / "logo",
                pianyi=(10, 0)
            )

            if clicked:
                #browser.script_log("已点击logo，等待游戏加载")
                state = DOLoginState.SKIP_ANIM
            else:
                # 如果点不到logo，可能已经进入了后续阶段
                #browser.script_log("未检测到logo，尝试检测skip")
                state = DOLoginState.SKIP_ANIM
            continue

        # ---------- 跳过签到 / 演出 ----------
        if state == DOLoginState.SKIP_ANIM:
            clicked = await browser.click_image(
                img_path / "skip",
                max_delay=1,  # 减少等待时间
                pianyi=(skip_click_count % 3 - 1, 0)
            )

            if clicked:
                skip_click_count += 1
                browser.script_log(f"点击跳过第 {skip_click_count} 次")
                if skip_click_count >= 3:  # 减少尝试次数
                    state = DOLoginState.CLOSE_POPUP
            else:
                # 找不到skip就直接进入下一步
                #browser.script_log("未检测到skip按钮")
                state = DOLoginState.CLOSE_POPUP
            continue

        # ---------- 关闭弹窗 ----------
        if state == DOLoginState.CLOSE_POPUP:
            clicked = await browser.click_image(
                img_path / "关闭",
                max_delay=1
            )
            if clicked:
                browser.script_log("关闭了弹窗")
            state = DOLoginState.CHECK_DONE
            continue

        # ---------- 网络异常处理 ----------
        if state == DOLoginState.NETWORK_ERROR:
            network_retry += 1
            browser.script_log(
                f"检测到网络异常，尝试重连 {network_retry}/{MAX_NETWORK_RETRY}"
            )

            if network_retry >= MAX_NETWORK_RETRY:
                browser.script_log("网络异常重连次数过多，重置计数继续尝试")
                network_retry = 0

            clicked = await browser.click_image(img_path / "err2") or await browser.click_image(img_path / "err4")


            if clicked:
                browser.script_log("已点击网络重连按钮")
                await browser.b_sleep(2.0)
                network_retry = 0
                state = DOLoginState.WAIT_GAME_LOAD  # 重连后回到等待状态
            else:
                #browser.script_log("未找到重连按钮")
                await browser.b_sleep(1.0)
                state = DOLoginState.WAIT_GAME_LOAD

            continue

        # ---------- 判断是否完成 ----------
        if state == DOLoginState.CHECK_DONE:
            if await browser.match_image(img_path / "rank"):
                browser.script_log("DO登录完成，已进入主界面")
                state = DOLoginState.DONE
            else:
                #browser.script_log("未检测到主界面，继续跳过流程")
                skip_click_count = 0  # 重置计数
                state = DOLoginState.SKIP_ANIM
            continue