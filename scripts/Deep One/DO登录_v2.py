"""
DO登录 v2 —— 升级版
=====================
基于脚本解释重写的登录流程：

① 网页跳转 → 导航到游戏页，如遇 DMM 登录页则自动输入账号
② 游戏页   → 等待游戏加载，点击 start2 确认进入
③ 进入游戏 → logo 附近点击前进
④ 登录奖励 → skip/关闭 按钮处理
⑤ 完成登录 → rank 检测，脚本结束

架构沿用 FSM（状态处理函数 + 守卫）模式，与 DO推本_v2 一致。
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
    # 路径
    img_dir: Path = IMG_PATH / 'DeepOne' / 'DO登录'

    # 入口 URL
    entry_url: str = "https://play.games.dmm.co.jp/game/deeponer"

    # 超时（秒）
    total_timeout: float = 300.0     # 脚本总超时
    state_timeout: float = 30.0
    wait_appear: float = 5.0
    wait_disappear: float = 3.0

    # 匹配参数
    threshold: float = 0.85

    # 缓存
    use_polling_cache: bool = True


CFG = Config()


# ═══════════════════════════════════════════════════════════════
# 守卫  ——  处理网络异常 / 代理下载失败等弹窗
# ═══════════════════════════════════════════════════════════════

GUARDS = []


def register_guard(img_path: Path, pianyi=(0, 0), desc=""):
    GUARDS.append((img_path, pianyi, desc))


_guard_ts: dict[str, float] = {}  # guard 冷却时间戳


async def check_guards(browser: UserBrowser) -> bool:
    """遍历守卫，命中则点击处理并返回 True（同一守卫 5 秒内不重复处理）"""
    now = __import__('time').time()
    for img_path, pianyi, desc in GUARDS:
        key = str(img_path)
        if now - _guard_ts.get(key, 0) < 5.0:
            continue  # 冷却中，跳过
        if await browser.click_image(img_path, pianyi=pianyi, threshold=CFG.threshold):
            browser.script_log(f"[守卫] {desc or img_path.name}")
            _guard_ts[key] = now
            await browser.b_sleep(0.3, 0.8)
            return True
    return False


register_guard(CFG.img_dir / 'err1_1', desc="网络异常 重试按钮")
# register_guard(CFG.img_dir / 'err2_2', desc="代理下载失败 确认按钮")  # 游戏内误触，暂时移除


# ═══════════════════════════════════════════════════════════════
# 网页导航  ——  URL 检测 + DMM 登录（与 FSM 分离，因为检测手段不同）
# ═══════════════════════════════════════════════════════════════

def _extract_game_path(url: str) -> str:
    """从完整 URL 中提取游戏路径"""
    return urlparse(url).path.strip("/")


async def login(browser: UserBrowser, url: str) -> None:
    """
    导航到游戏页，处理 DMM 登录和错误重试。
    返回时表示已进入游戏页面（但游戏内容可能还在加载）。
    """
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

    # 初始跳转（先等浏览器就绪）
    await browser.b_sleep(1.0)

    # 不管当前在哪个页面，直接跳转
    try:
        await browser.goto(url)
        browser.script_log(f"已跳转到: {url}")
    except Exception as e:
        browser.script_log(f"初始跳转失败: {e}，进入重试循环")

    while True:
        await browser.b_sleep(0.5)
        cur_url = (await browser.get_url) or ""
        cur_title = ((await browser.get_title) or "").lower()

        # 地区限制
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

        # DMM 登录页
        if game_path not in cur_url and (login_kw in cur_url or "ログイン" in cur_title):
            stable_count = 0
            error_retry = 0
            await browser.dmm_login()
            continue

        # 游戏页
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
    """等待游戏界面加载，检测当前处于哪个阶段"""
    # 先快速低阈值扫描主要标识，减少启动等待
    for fast_img, fast_name in [
        (CFG.img_dir / 'start1', "start_dialog"),
        (CFG.img_dir / 'logo', "tap_screen"),
        (CFG.img_dir / 'rank', "__exit__"),
    ]:
        if await browser.match_image(fast_img, threshold=0.6):
            browser.script_log(f"  检测到 {fast_name}")
            if fast_name == "__exit__":
                return "__exit__"
            return fast_name

    # 全量并发匹配（标准阈值）
    candidates = {
        "done":         CFG.img_dir / 'rank',
        "tap_screen":   CFG.img_dir / 'logo',
        "start_dialog": CFG.img_dir / 'start1',
        "skip_anim":    CFG.img_dir / 'skip',
        "close_popup":  CFG.img_dir / '关闭',
    }
    results = await asyncio.gather(*[
        browser.match_image(path, threshold=0.95 if name == "start_dialog" else CFG.threshold)
        for name, path in candidates.items()
    ])
    for name, result in zip(candidates.keys(), results):
        if result:
            browser.script_log(f"  检测到 {name}")
            if name == "done":
                return "__exit__"
            return name
    return None


async def start_dialog_state(browser: UserBrowser) -> StateName:
    """游戏开始确认弹窗 → 点 start2 → 等 start1 消失"""
    # 先查坐标再点，便于调试
    match = await browser.match_image(CFG.img_dir / 'start2', threshold=CFG.threshold)
    if match:
        browser.script_log(f"  start2 匹配位置: ({match.x}, {match.y}) score={match.max_val:.3f}")
        await browser.click_image(CFG.img_dir / 'start2', threshold=CFG.threshold)
        # 等待 start1 不再可见（转场动画结束后会自动消失）
        for _ in range(10):
            await browser.b_sleep(0.5)
            if not await browser.match_image(CFG.img_dir / 'start1', threshold=0.95):
                break
    return "wait_game_load"


async def tap_screen_state(browser: UserBrowser) -> StateName:
    """logo 界面 → 附近点击前进"""
    if await browser.click_image(
            CFG.img_dir / 'logo',
            pianyi=(random.randint(5, 15), random.randint(-5, 5)),
            threshold=CFG.threshold,
    ):
        browser.script_log("  点击 logo 前进")
        await browser.b_sleep(0.3, 0.6)
    return "wait_game_load"


async def skip_anim_state(browser: UserBrowser) -> StateName:
    """跳过登录奖励 / 演出动画"""
    clicked = await browser.click_image(
        CFG.img_dir / 'skip',
        pianyi=(random.randint(-2, 2), 0),
        threshold=CFG.threshold,
    )
    if clicked:
        browser.script_log("  点击了 skip")
        await browser.b_sleep(0.3, 0.6)
        return None  # 继续点 skip，直到点不到为止
    # skip 点不到了 → 可能是"关闭"或已经结束了
    return "wait_game_load"


async def close_popup_state(browser: UserBrowser) -> StateName:
    """关闭弹窗"""
    if await browser.click_image(CFG.img_dir / '关闭', threshold=CFG.threshold):
        browser.script_log("  关闭了弹窗")
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
}

STATE_TIMEOUT = {
    "wait_game_load":  30,
    "start_dialog":    15,
    "tap_screen":      15,
    "skip_anim":       30,
    "close_popup":     15,
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
    total_start = asyncio.get_event_loop().time()

    # 启用帧缓存
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True

    # ====== 阶段 1：网页导航 ======
    browser.script_log("[DO登录v2] 开始网页导航")
    await login(browser, CFG.entry_url)

    # ====== 阶段 2：游戏内登录流程 ======
    state_name = "wait_game_load"
    state_enter_time = asyncio.get_event_loop().time()
    browser.script_log("[DO登录v2] 开始游戏内登录流程")

    while True:
        # 总超时
        if asyncio.get_event_loop().time() - total_start > CFG.total_timeout:
            browser.script_log(f"[DO登录v2] ❌ 总超时 {CFG.total_timeout}s，登录未完成")
            raise TimeoutError(f"登录超时 {CFG.total_timeout}s")

        browser.note_state(state_name)
        await browser.update_frame()

        # 守卫优先（网络异常 / 代理下载失败）
        if await check_guards(browser):
            continue

        # 状态超时
        timeout = STATE_TIMEOUT.get(state_name, 30)
        now = asyncio.get_event_loop().time()
        if now - state_enter_time > timeout:
            browser.script_log(f"[超时] {state_name} 超过 {timeout}s，回到 wait_game_load")
            state_name = "wait_game_load"
            state_enter_time = now
            continue

        # 驱动状态
        handler = STATES.get(state_name)
        if handler is None:
            browser.script_log(f"[错误] 未知状态: {state_name}，重置")
            state_name = "wait_game_load"
            state_enter_time = now
            continue

        browser.script_log(f"[{state_name}]")
        next_state = await handler(browser)

        if next_state == "__exit__":
            browser.script_log("[DO登录v2] ✅ 登录完成")
            break

        if next_state is not None and next_state != state_name:
            browser.script_log(f"  → {next_state}")
            state_name = next_state
            state_enter_time = now

        # 仅防忙等，不做实质性延时（操作后的等待由状态内部处理）
        await browser.b_sleep(0.03, 0.08)
