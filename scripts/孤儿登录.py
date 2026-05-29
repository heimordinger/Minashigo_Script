# scripts/孤儿登录.py
import random
from backend.browser.user_browser import UserBrowser
from core.path import PROJECT_ROOT


async def login(browser: UserBrowser, url: str):
    ENTRY_URL = url
    REGION_BLOCK_KW = "not-available-in-your-region"
    ERROR_TITLE_KW = ["予期せぬエラー", "エラーが発生", "error"]
    LOADING_TITLE_KW = ["loading", "読み込み"]

    stable_game_count = 0
    region_retry = 0
    error_retry = 0
    MAX_REGION_RETRY = 5
    MAX_ERROR_RETRY = 5

    browser.script_log("开始登录流程（元素检测）")

    # 初始导航
    await browser.goto(ENTRY_URL)

    while True:
        await browser.b_sleep(0.5)
        page = browser.page
        cur_title = (browser.title or "").lower()

        # ---------- 地区限制 ----------
        if REGION_BLOCK_KW in page.url:
            region_retry += 1
            stable_game_count = 0
            browser.script_log(f"检测到地区限制，第 {region_retry} 次重试")
            if region_retry >= MAX_REGION_RETRY:
                raise RuntimeError("VPN 不稳定，地区限制持续存在")
            await browser.goto(ENTRY_URL)
            continue

        # ---------- 游戏页（iframe 加载）----------
        try:
            has_game_frame = await page.locator('#game_frame').count() > 0
        except Exception:
            await browser.b_sleep(0.5)
            continue
        if has_game_frame:
            if any(k in cur_title for k in LOADING_TITLE_KW):
                continue

            if any(k in cur_title for k in ERROR_TITLE_KW):
                error_retry += 1
                stable_game_count = 0
                browser.script_log(f"检测到错误页（{cur_title}），第 {error_retry} 次重试")
                if error_retry >= MAX_ERROR_RETRY:
                    raise RuntimeError("游戏页持续错误")
                await browser.goto(ENTRY_URL)
                continue

            error_retry = 0
            stable_game_count += 1
            if stable_game_count >= 3:
                browser.script_log("进入游戏完成（检测到游戏 iframe）")
                break
            continue

        # ---------- 登录表单 ----------
        login_id = page.locator('#login_id')
        password = page.locator('#password')
        try:
            has_login_form = (
                (await login_id.count() > 0 and await login_id.is_visible(timeout=0))
                or (await password.count() > 0 and await password.is_visible(timeout=0))
            )
        except Exception:
            await browser.b_sleep(0.5)
            continue
        if has_login_form:
            stable_game_count = 0
            error_retry = 0
            region_retry = 0
            browser.script_log("检测到登录表单")
            await browser.dmm_login()
            # 登录后等待页面跳转（最长 10s），避免立即重检测
            for _ in range(20):
                await browser.b_sleep(0.5)
                try:
                    if await page.locator('#game_frame').count() > 0:
                        break
                except Exception:
                    pass
                try:
                    still_login = (
                        (await login_id.count() > 0 and await login_id.is_visible(timeout=0))
                        or (await password.count() > 0 and await password.is_visible(timeout=0))
                    )
                except Exception:
                    still_login = False
                if not still_login:
                    break
            continue

        # ---------- 错误页兜底 ----------
        if any(k in cur_title for k in ERROR_TITLE_KW):
            error_retry += 1
            stable_game_count = 0
            browser.script_log(f"检测到错误页（{cur_title}），第 {error_retry} 次重试")
            if error_retry >= MAX_ERROR_RETRY:
                raise RuntimeError("页面持续错误")
            await browser.goto(ENTRY_URL)
            continue

        stable_game_count = 0


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
        if await browser.match_image(img_path / "ok"):
            browser.script_log("检测到日常奖励确认按钮")
            await browser.click_image(img_path / "ok")
            continue

        # ---------- 游戏主界面 ----------
        if await browser.match_image(img_path / "menu"):
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
