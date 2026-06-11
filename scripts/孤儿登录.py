# scripts/孤儿登录.py
import random
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
    cur_url = browser.url
    if FINAL_GAME_PATH not in cur_url and LOGIN_KW not in cur_url:
        await browser.goto(ENTRY_URL)

    while True:
        await browser.b_sleep(0.5)

        cur_url = browser.url
        cur_title = (browser.title).lower()

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
            await browser.dmm_login(game_name="minashigo")
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


async def do_work(browser: UserBrowser):
    await login(browser, "https://play.games.dmm.co.jp/game/minashigo_x")
    img_path = PROJECT_ROOT / "assets" / "images" / "minashigo" / "孤儿登录"

    while True:
        await browser.update_frame()

        # ---------- 第一阶段：启动登录确认 ----------
        if await browser.match_image(img_path / "start1"):
            if await browser.match_image(img_path / "start2"):
                await browser.click_image(img_path / "start2")
            continue

        # ---------- 日常奖励弹窗 ----------
        # ok.png 与 start2 很像，但只应在游戏内出现
        if await browser.match_image(img_path / "ok"):
            browser.script_log("检测到日常奖励确认按钮")
            await browser.click_image(img_path / "ok")
            continue

        # ---------- 游戏主界面 ----------
        if await browser.match_image(img_path / "menu"):
            # 点击任意位置进入
            random_x = random.randint(50, 200)
            random_y = random.randint(30, 200)
            await browser.click_image(img_path / "menu", pianyi=(random_x, random_y))
            continue

        # ---------- 跳过签到 / 动画 ----------
        if await browser.click_image(img_path / "skip", threshold=0.85):
            continue

        # ---------- 任务完成判定 ----------
        if (
                await browser.match_image(img_path / "石头")
                or await browser.match_image(img_path / "rank")
        ):
            browser.script_log("登录完成")
            print("登录完成")
            break
