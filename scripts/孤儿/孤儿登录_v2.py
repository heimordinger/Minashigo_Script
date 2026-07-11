"""
孤儿登录 v2 —— 升级版
=======================
基于脚本解释重写的登录流程：

① 网页跳转 → 导航到游戏页，如遇 DMM 登录页则自动输入账号
② 游戏页   → 等待游戏加载，点击 start2 确认进入
③ 进入游戏 → menu 按钮附近点击前进
④ 登录奖励 → skip / ok / 石头 按优先级处理
⑤ 完成登录 → rank 检测，脚本结束

架构沿用 FSM（状态处理函数 + 守卫）模式。
"""

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    img_dir: Path = IMG_PATH / 'minashigo' / '孤儿登录'
    entry_url: str = "https://play.games.dmm.co.jp/game/minashigo_x"
    total_timeout: float = 300.0     # 脚本总超时
    state_timeout: float = 30.0
    wait_appear: float = 5.0
    threshold: float = 0.85
    use_polling_cache: bool = True


CFG = Config()


# ═══════════════════════════════════════════════════════════════
# 守卫  ——  暂无已知异常弹窗素材，留空待补充
# ═══════════════════════════════════════════════════════════════

GUARDS = []
_guard_ts: dict[str, float] = {}


def register_guard(img_path: Path, pianyi=(0, 0), desc=""):
    GUARDS.append((img_path, pianyi, desc))


async def check_guards(browser: UserBrowser) -> bool:
    now = __import__('time').time()
    for img_path, pianyi, desc in GUARDS:
        key = str(img_path)
        if now - _guard_ts.get(key, 0) < 5.0:
            continue
        if await browser.click_image(img_path, pianyi=pianyi, threshold=CFG.threshold):
            browser.script_log(f"[守卫] {desc or img_path.name}")
            _guard_ts[key] = now
            await browser.b_sleep(0.3, 0.8)
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# 网页导航  ——  与 DO登录_v2 共用同一套逻辑，仅 URL 不同
# ═══════════════════════════════════════════════════════════════

def _extract_game_path(url: str) -> str:
    return urlparse(url).path.strip("/")


async def login(browser: UserBrowser, url: str) -> None:
    """导航到游戏页，处理 DMM 登录和错误重试"""
    game_path = _extract_game_path(url)
    login_kw = "accounts.dmm.co.jp"
    region_block_kw = "not-available-in-your-region"
    error_title_kw = ["予期せぬエラー", "エラーが発生", "error"]
    loading_title_kw = ["loading", "読み込み"]

    stable_count = 0
    region_retry = 0
    error_retry = 0
    max_region = 5
    max_error = 5

    await browser.b_sleep(1.0)
    try:
        await browser.goto(url)
        browser.script_log(f"已跳转到: {url}")
    except Exception as e:
        browser.script_log(f"初始跳转失败: {e}")

    while True:
        await browser.b_sleep(0.5)
        cur_url = (await browser.get_url) or ""
        cur_title = ((await browser.get_title) or "").lower()

        if region_block_kw in cur_url:
            region_retry += 1
            stable_count = 0
            browser.script_log(f"地区限制，第 {region_retry} 次重试")
            if region_retry >= max_region:
                raise RuntimeError("VPN 不稳定，地区限制持续存在")
            try:
                await browser.goto(url)
            except Exception:
                pass
            continue

        if game_path not in cur_url and (login_kw in cur_url or "ログイン" in cur_title):
            stable_count = 0
            error_retry = 0
            await browser.dmm_login()
            continue

        if game_path in cur_url:
            if any(k in cur_title for k in error_title_kw):
                error_retry += 1
                stable_count = 0
                browser.script_log(f"错误页（{cur_title}），第 {error_retry} 次重试")
                if error_retry >= max_error:
                    raise RuntimeError("游戏页持续错误")
                await browser.goto(url)
                continue
            if any(k in cur_title for k in loading_title_kw):
                stable_count = 0
                continue
            error_retry = 0
            stable_count += 1
            if stable_count >= 3:
                browser.script_log("进入游戏页面完成")
                break
            continue


# ═══════════════════════════════════════════════════════════════
# 状态处理函数  ——  游戏内登录流程（基于图像匹配）
# ═══════════════════════════════════════════════════════════════

StateName = Optional[str]


async def wait_game_load_state(browser: UserBrowser) -> StateName:
    """等待游戏界面加载，并发检测当前阶段"""
    candidates = {
        "done":         CFG.img_dir / 'rank',
        "skip_anim":    CFG.img_dir / 'skip',
        "close_btn":    CFG.img_dir / 'close1',
        "close_btn2":   CFG.img_dir / 'close2',
        "close_popup":  CFG.img_dir / 'ok',
        "tap_screen":   CFG.img_dir / 'menu',
        "start_dialog": CFG.img_dir / 'start2',
    }
    results = await asyncio.gather(*[
        browser.match_image(path, threshold=CFG.threshold)
        for path in candidates.values()
    ])
    for name, result in zip(candidates.keys(), results):
        if result:
            browser.script_log(f"  检测到 {name}")
            if name == "done":
                return "__exit__"
            return name

    # 石头是特殊处理：最低优先级，只有以上都不命中才检查
    if await browser.match_image(CFG.img_dir / '石头', threshold=CFG.threshold):
        return "stone"

    return None


async def start_dialog_state(browser: UserBrowser) -> StateName:
    """游戏开始确认弹窗 → 点 start2 → 等 start1 消失"""
    if await browser.click_image(CFG.img_dir / 'start2', threshold=CFG.threshold):
        browser.script_log("  点击了 start2")
        for _ in range(10):
            await browser.b_sleep(0.5)
            if not await browser.match_image(CFG.img_dir / 'start1', threshold=CFG.threshold):
                break
    return "wait_game_load"


async def tap_screen_state(browser: UserBrowser) -> StateName:
    """menu 界面 → 在按钮右下方点击前进"""
    if await browser.click_image(
            CFG.img_dir / 'menu',
            pianyi=(random.randint(50, 200), random.randint(30, 200)),
            threshold=CFG.threshold,
    ):
        browser.script_log("  点击 menu 附近前进")
        await browser.b_sleep(0.3, 0.6)
    return "wait_game_load"


async def skip_anim_state(browser: UserBrowser) -> StateName:
    """跳过登录奖励动画"""
    clicked = await browser.click_image(
        CFG.img_dir / 'skip',
        pianyi=(random.randint(-2, 2), 0),
        threshold=CFG.threshold,
    )
    if clicked:
        browser.script_log("  点击了 skip")
        await browser.b_sleep(0.3, 0.6)
        return None  # 继续点直到点不到为止
    return "wait_game_load"


async def close_popup_state(browser: UserBrowser) -> StateName:
    """点击 ok 确认弹窗"""
    if await browser.click_image(CFG.img_dir / 'ok', threshold=CFG.threshold):
        browser.script_log("  点击了 ok")
        await browser.b_sleep(0.3, 0.6)
    return "wait_game_load"


async def close_btn_state(browser: UserBrowser) -> StateName:
    """点击一类关闭按钮（close1）"""
    if await browser.click_image(CFG.img_dir / 'close1', threshold=CFG.threshold):
        browser.script_log("  点击了 close1")
        await browser.b_sleep(0.3, 0.6)
    return "wait_game_load"


async def close_btn2_state(browser: UserBrowser) -> StateName:
    """点击二类关闭按钮（close2）"""
    if await browser.click_image(CFG.img_dir / 'close2', threshold=CFG.threshold):
        browser.script_log("  点击了 close2")
        await browser.b_sleep(0.3, 0.6)
    return "wait_game_load"


async def stone_state(browser: UserBrowser) -> StateName:
    """石头界面 → 附近点击推进（最低优先级）"""
    if await browser.click_image(
            CFG.img_dir / '石头',
            pianyi=(random.randint(20, 60), random.randint(10, 30)),
            threshold=CFG.threshold,
    ):
        browser.script_log("  点击石头附近")
        await browser.b_sleep(0.3, 0.6)
    return "wait_game_load"


# ═══════════════════════════════════════════════════════════════
# 状态注册表
# ═══════════════════════════════════════════════════════════════

STATES = {
    "wait_game_load":  wait_game_load_state,
    "start_dialog":    start_dialog_state,
    "tap_screen":      tap_screen_state,
    "skip_anim":       skip_anim_state,
    "close_popup":     close_popup_state,
    "close_btn":       close_btn_state,
    "close_btn2":      close_btn2_state,
    "stone":           stone_state,
}

STATE_TIMEOUT = {
    "wait_game_load":  30,
    "start_dialog":    15,
    "tap_screen":      15,
    "skip_anim":       30,
    "close_popup":     15,
    "close_btn":       15,
    "close_btn2":      15,
    "stone":           15,
}


# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════

async def do_work(browser: UserBrowser):
    """
    登录脚本主入口。
    1. 网页导航 + DMM 登录（URL 检测）
    2. 游戏内登录流程（图像检测 FSM）
    """
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True

    # ====== 阶段 1：网页导航 ======
    browser.script_log("[孤儿登录v2] 开始网页导航")
    await login(browser, CFG.entry_url)

    # ====== 阶段 2：游戏内登录流程 ======
    state_name = "wait_game_load"
    state_enter_time = asyncio.get_event_loop().time()
    browser.script_log("[孤儿登录v2] 开始游戏内登录流程")

    while True:
        await browser.update_frame()

        if await check_guards(browser):
            continue

        timeout = STATE_TIMEOUT.get(state_name, 30)
        now = asyncio.get_event_loop().time()
        if now - state_enter_time > timeout:
            browser.script_log(f"[超时] {state_name}，回到 wait_game_load")
            state_name = "wait_game_load"
            state_enter_time = now
            continue

        handler = STATES.get(state_name)
        if handler is None:
            browser.script_log(f"[错误] 未知状态: {state_name}")
            state_name = "wait_game_load"
            state_enter_time = now
            continue

        browser.script_log(f"[{state_name}]")
        next_state = await handler(browser)

        if next_state == "__exit__":
            browser.script_log("[孤儿登录v2] ✅ 登录完成")
            break

        if next_state is not None and next_state != state_name:
            browser.script_log(f"  → {next_state}")
            state_name = next_state
            state_enter_time = now

        await browser.b_sleep(0.03, 0.08)
