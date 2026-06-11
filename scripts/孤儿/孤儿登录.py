# scripts/孤儿/孤儿登录.py
from enum import Enum, auto
from urllib.parse import urlparse
import random

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
                browser.script_log(f"检测到错误页（{cur_title}），第 {error_retry} 次重试")
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


class OrphanLoginState(Enum):
    WAIT_GAME_LOAD = auto()
    START_DIALOG = auto()
    TAP_SCREEN = auto()
    SKIP_ANIM = auto()
    CLOSE_POPUP = auto()
    CHECK_DONE = auto()
    NETWORK_ERROR = auto()
    DONE = auto()


async def do_work(browser: UserBrowser):
    await login(browser, "https://play.games.dmm.co.jp/game/minashigo_x")
    img_path = PROJECT_ROOT / "assets" / "images" / "minashigo" / "孤儿登录"

    state = OrphanLoginState.WAIT_GAME_LOAD
    wait_count = 0
    MAX_WAIT_COUNT = 30

    # ---------- 审核机制：连续无进展则重新检测状态 ----------
    stall_count = 0
    MAX_STALL = 10

    while state != OrphanLoginState.DONE:
        await browser.update_frame()

        # ========== 审核机制（基于上一轮计数） ==========
        if stall_count >= MAX_STALL:
            stall_count = 0
            browser.script_log(f"[审核] 连续{MAX_STALL}次无进展（当前状态={state.name}），重新检测界面")

            await browser.update_frame()
            if await browser.match_image(img_path / "rank") or await browser.match_image(img_path / "石头") or await browser.match_image(img_path / "menu"):
                browser.script_log("[审核] → 检测到主界面")
                state = OrphanLoginState.DONE; continue
            if await browser.match_image(img_path / "skip"):
                browser.script_log("[审核] → 检测到skip")
                state = OrphanLoginState.SKIP_ANIM; continue
            if await browser.match_image(img_path / "ok"):
                browser.script_log("[审核] → 检测到ok")
                state = OrphanLoginState.CLOSE_POPUP; continue
            if await browser.match_image(img_path / "start2"):
                browser.script_log("[审核] → 检测到start2")
                state = OrphanLoginState.START_DIALOG; continue
            if await browser.match_image(img_path / "start1"):
                browser.script_log("[审核] → 检测到start1")
                state = OrphanLoginState.START_DIALOG; continue

            browser.script_log(f"[审核] 未检测到任何特征图，保持 state={state.name}")

        stall_count += 1

        # ========== 网络异常检测 ==========
        if state == OrphanLoginState.WAIT_GAME_LOAD and await browser.match_image(img_path / "start1"):
            browser.script_log("检测到登录提示")
            state = OrphanLoginState.START_DIALOG; stall_count = 0; continue

        if state == OrphanLoginState.START_DIALOG:
            if await browser.match_image(img_path / "start2"):
                clicked = await browser.click_image(img_path / "start2")
                if clicked:
                    browser.script_log("点击 start2 进入")
                    state = OrphanLoginState.TAP_SCREEN; stall_count = 0; continue
            await browser.b_sleep(0.3)
            continue

        # ---------- TAP_SCREEN：等待加载完成 ----------
        if state == OrphanLoginState.TAP_SCREEN:
            wait_count += 1
            if wait_count > MAX_WAIT_COUNT:
                browser.script_log("等待超时，重新检测状态")
                state = OrphanLoginState.WAIT_GAME_LOAD; stall_count = 0
                wait_count = 0
                continue

            if await browser.match_image(img_path / "skip"):
                browser.script_log("检测到跳过按钮")
                state = OrphanLoginState.SKIP_ANIM; stall_count = 0; continue

            if await browser.match_image(img_path / "ok"):
                browser.script_log("检测到确认弹窗")
                state = OrphanLoginState.CLOSE_POPUP; stall_count = 0; continue

            if await browser.match_image(img_path / "menu") or await browser.match_image(img_path / "石头") or await browser.match_image(img_path / "rank"):
                browser.script_log("已进入主界面")
                state = OrphanLoginState.DONE; stall_count = 0; continue

            await browser.b_sleep(0.5)
            continue

        # ---------- SKIP_ANIM：跳过动画/演出 ----------
        if state == OrphanLoginState.SKIP_ANIM:
            if await browser.click_image(img_path / "skip", threshold=0.85):
                stall_count = 0; continue
            browser.script_log("跳过完成")
            state = OrphanLoginState.TAP_SCREEN; stall_count = 0; continue

        # ---------- CLOSE_POPUP：关闭弹窗 ----------
        if state == OrphanLoginState.CLOSE_POPUP:
            if await browser.match_image(img_path / "ok"):
                await browser.click_image(img_path / "ok")
                browser.script_log("关闭弹窗")
                state = OrphanLoginState.TAP_SCREEN; stall_count = 0; continue

        # ---------- CHECK_DONE：确认是否完成 ----------
        if state == OrphanLoginState.CHECK_DONE or state == OrphanLoginState.TAP_SCREEN:
            if await browser.match_image(img_path / "石头") or await browser.match_image(img_path / "rank"):
                browser.script_log("登录完成")
                state = OrphanLoginState.DONE; stall_count = 0; continue
            if await browser.match_image(img_path / "menu"):
                await browser.click_image(img_path / "menu", pianyi=(random.randint(50, 200), random.randint(30, 200)))
                continue

        await browser.b_sleep(0.3)
