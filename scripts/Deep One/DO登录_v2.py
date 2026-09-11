"""
DO登录 v2 —— 升级版
=====================
基于脚本解释的登录流程（按评审优化）：

① 网页跳转 → 导航到游戏页，DMM 登录页 eager 抢填
② 游戏页   → 游戏帧识图；等加载，点 start2 确认进入
③ 进入游戏 → logo 附近连点几次前进
④ 登录奖励 → 本帧见 skip/关闭就点（不定次数）
⑤ 完成登录 → rank 出现即结束

守卫：先认 err1/err2 标识，再点对应按钮（冷却防误触）。
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from backend.automation.frame_observer import DEFAULT_SCRIPT_FPS
from backend.automation.run_recorder import analyze_frame_blackness
from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH


# 全过程伪录制（时间线+稀疏关键帧）。也可设环境变量 MINASHIGO_PSEUDO_RECORD=1
DEBUG_PSEUDO_RECORD = True
# 加载等待诊断：更密关键帧（日常可关；黑屏计时不依赖此开关）
DEBUG_WAIT_FRAMES = False
WAIT_KEYFRAME_INTERVAL_S = 1.2
# 黑屏识别：只写伪录制计时，不改变点击逻辑
DETECT_BLACK_SCREEN = True


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    img_dir: Path = IMG_PATH / "DeepOne" / "DO登录"
    entry_url: str = "https://play.games.dmm.co.jp/game/deeponer"

    state_timeout: float = 60.0
    wait_appear: float = 8.0
    wait_disappear: float = 5.0

    threshold: float = 0.85
    # 场景标识统一阈值（去掉 start1 0.6/0.95 分裂）
    nav_threshold: float = 0.85

    use_polling_cache: bool = True
    # 加载空等：长轮询；UI 已出现后略短
    wait_poll_sleep: tuple[float, float] = (1.2, 1.8)
    wait_poll_sleep_active: tuple[float, float] = (0.30, 0.50)
    act_poll_sleep: tuple[float, float] = (0.05, 0.12)
    logo_taps: int = 2
    guard_cooldown: float = 5.0
    # 加载等待中少查守卫；活跃阶段仍每轮可查（受 cooldown 限制）
    guard_interval_wait: float = 8.0
    # 网络不稳时 start1 很晚才出：先耐心等，少点唤醒、少 goto
    wake_after_sec: float = 90.0
    wake_interval: float = 60.0
    # wait_game_load 单段超时（秒）；超时只计数，达 goto_after 次才重跳转
    # 黑屏加载期间暂停该计时（见 wait_black_max_sec）
    wait_load_timeout: float = 180.0
    wait_load_goto_after: int = 4  # 约 4×180s 非黑屏空等才 goto
    # 黑屏（Now Loading 之后）最长耐心等待；超时后才恢复普通计时
    wait_black_max_sec: float = 900.0  # 15min
    # 黑屏 / Master 下载期：更长轮询 + 降频识图
    wait_poll_sleep_black: tuple[float, float] = (2.5, 3.5)
    black_match_interval: float = 2.8  # 黑屏时两次识图最小间隔
    # 游戏 API/CDN 仍有在途请求时：等同加载中（暂停超时、不唤醒、不 goto）
    pause_on_game_net: bool = True
    net_busy_match_interval: float = 2.0
    # 浏览器侧广告拦截（connect 时安装；此处仅作开关提示）
    prefer_ad_block: bool = True
    # 裁剪失败后不再周期性对齐（全视口识图已足够）
    realign_interval: float = 25.0
    realign_max_fails: int = 1
    passive_fps: float = 0.0


CFG = Config()

# 见过 start 弹窗后，wait 改为全量探针（logo/skip/关闭）
_ui_seen_start: bool = False

_DMM_LOGIN_URL_KW = ("accounts.dmm",)
_DMM_LOGIN_TITLE_KW = ("ログイン", "login")
_last_dmm_recover_ts: float = 0.0
_DMM_RECOVER_COOLDOWN = 8.0
_last_wait_match_ts: float = 0.0
_last_net_log_ts: float = 0.0


def _game_net_busy(browser: UserBrowser) -> bool:
    if not CFG.pause_on_game_net:
        return False
    try:
        return bool(browser.game_net_busy())
    except Exception:
        return False


def _game_net_count(browser: UserBrowser) -> int:
    try:
        return int(browser.game_net_inflight_count())
    except Exception:
        return 0


def _img(name: str) -> Path:
    return CFG.img_dir / (name if name.endswith(".png") else f"{name}.png")


def _url_looks_like_dmm_login(url: str) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    if any(k in u for k in _DMM_LOGIN_URL_KW):
        return True
    # 登录页常见路径；排除已带游戏 path 的地址
    if ("/login" in u or "login/signin" in u) and "deeponer" not in u:
        return True
    return False


async def _current_is_dmm_login(browser: UserBrowser) -> bool:
    """实时 URL/标题判断是否掉回 DMM 登录页。"""
    try:
        url = (await browser.get_url) or ""
    except Exception:
        url = ""
    if _url_looks_like_dmm_login(url):
        return True
    try:
        title = (await browser.get_title) or ""
    except Exception:
        title = ""
    # 标题像登录且 URL 已不是游戏页
    game_path = _extract_game_path(CFG.entry_url)
    if any(k in title for k in _DMM_LOGIN_TITLE_KW):
        if game_path not in (url or ""):
            return True
    return False


async def _recover_dmm_login_if_needed(browser: UserBrowser) -> bool:
    """掉回登录页时重新抢填；成功发起恢复返回 True。"""
    global _last_dmm_recover_ts
    if not await _current_is_dmm_login(browser):
        return False
    now = time.time()
    if now - _last_dmm_recover_ts < _DMM_RECOVER_COOLDOWN:
        return True  # 仍在登录页，上层勿当游戏加载空等
    _last_dmm_recover_ts = now
    browser.script_log("  检测到 DMM 登录页，重新填写账号…")
    try:
        await browser.dmm_login(eager=True)
    except Exception as e:
        browser.script_log(f"  dmm_login 失败: {e}")
        return True
    await browser.b_sleep(0.8, 1.5)
    browser._note_progress()
    return True


def _current_frame(browser: UserBrowser):
    return getattr(getattr(browser, "_browser", None), "_frame", None)


def _frame_is_black(browser: UserBrowser) -> bool:
    """游戏区是否基本黑屏（Now Loading 之后的加载态）。"""
    if not DETECT_BLACK_SCREEN:
        return False
    frame = _current_frame(browser)
    if frame is None:
        return False
    try:
        is_black, _ = analyze_frame_blackness(frame)
        return bool(is_black)
    except Exception:
        return False


def _probe_black_screen(browser: UserBrowser, *, force_off: bool = False) -> bool:
    """边沿记录黑屏并返回当前是否黑屏。黑屏=加载中，应暂停 wait 超时。"""
    if not DETECT_BLACK_SCREEN:
        return False
    rec = getattr(browser, "pseudo_record", None) or getattr(browser, "_pseudo", None)
    is_black = False
    edge = None
    try:
        if force_off:
            if rec is not None:
                edge = rec.note_black_frame(False, reason="force_off")
            return False
        frame = _current_frame(browser)
        if frame is None:
            return False
        is_black, metrics = analyze_frame_blackness(frame)
        if rec is not None:
            edge = rec.note_black_frame(is_black, **metrics)
    except Exception:
        return False
    if edge:
        if edge.get("edge") == "on":
            browser.script_log(
                f"  [黑屏] 开始加载（mean={edge.get('mean')} "
                f"dark={edge.get('dark_ratio')}）——暂停等待超时"
            )
        elif edge.get("edge") == "off":
            browser.script_log(
                f"  [黑屏] 结束 +{edge.get('dt_s')}s ——恢复等待超时计时"
            )
    return bool(is_black)


# ═══════════════════════════════════════════════════════════════
# 守卫  ——  先标识、再按钮
# ═══════════════════════════════════════════════════════════════

_guard_ts: dict[str, float] = {}

# (标识, 按钮, 说明)
_GUARD_PAIRS = (
    ("err1", "err1_1", "网络异常 → 重试"),
    ("err2", "err2_2", "代理下载失败 → 确认"),
)


async def check_guards(browser: UserBrowser) -> bool:
    """命中 err 标识后再点对应按钮；冷却防误触。"""
    now = time.time()
    for mark, btn, desc in _GUARD_PAIRS:
        key = btn
        if now - _guard_ts.get(key, 0) < CFG.guard_cooldown:
            continue
        if not await browser.match_image(
            _img(mark), threshold=CFG.threshold, quiet=True
        ):
            continue
        if await browser.click_image(_img(btn), threshold=CFG.threshold):
            browser.script_log(f"[守卫] {desc}")
            _guard_ts[key] = now
            browser._note_progress()
            await browser.b_sleep(0.3, 0.8)
            return True
    return False


async def _prepare_game_matching(
    browser: UserBrowser, *, retries: int = 2, log: bool = True
) -> bool:
    """进游戏页后尝试 GameCanvas 裁剪；失败则退回全视口，并关掉每帧裁剪税。"""
    from backend.browser.game_frame_capture import align_game_viewport as _align_raw

    browser.use_game_frame_capture = True
    browser._stuck.idle_limit = 86400.0
    last_mode = "full"
    for i in range(max(1, retries)):
        meta = await _align_raw(browser._browser.page)
        found = bool(meta.get("found"))
        if found and meta.get("scrolled"):
            browser.invalidate_frame()
            if log:
                browser.script_log("已滚动对齐游戏区")
        await browser.update_frame()
        frame = getattr(browser._browser, "_frame", None)
        mode = getattr(browser._browser, "_frame_capture_mode", None) or "full"
        last_mode = mode
        cropped = mode not in ("full", None)
        if frame is not None and log and (cropped or i == retries - 1 or (found and i == 0)):
            browser.script_log(
                f"  游戏帧识图 mode={mode} frame={frame.shape[1]}x{frame.shape[0]}"
                + ("" if cropped else "（裁剪未生效）")
            )
        if cropped:
            return True
        if i + 1 < retries:
            await browser.b_sleep(0.4, 0.7)
    # 裁剪持续失败：后续 update_frame 不再先走 capture_game_frame
    browser.use_game_frame_capture = False
    if log:
        browser.script_log("  裁剪未生效，改用全视口识图（停止重试对齐）")
    return False


async def _frame_is_cropped(browser: UserBrowser) -> bool:
    mode = getattr(browser._browser, "_frame_capture_mode", None) or "full"
    return mode not in ("full", None)


async def _maybe_wake_game(browser: UserBrowser) -> None:
    """加载黑屏时点击游戏区中心尝试唤醒。"""
    rect = await browser._browser.get_game_canvas_rect()
    if not rect:
        try:
            vp = browser._browser.page.viewport_size or {}
            cx = int((vp.get("width") or 960) * 0.5)
            cy = int((vp.get("height") or 540) * 0.55)
        except Exception:
            return
        browser.script_log("  尝试点击视口中部唤醒…")
        await browser.click(cx, cy)
    else:
        cx = int(rect["x"] + rect["width"] * 0.5)
        cy = int(rect["y"] + rect["height"] * 0.5)
        browser.script_log("  尝试点击游戏区唤醒…")
        await browser.click(cx, cy)
    browser._note_progress()
    await browser.b_sleep(0.8, 1.2)


async def _set_passive_wait(browser: UserBrowser, on: bool) -> None:
    try:
        if on:
            await browser.request_fps(CFG.passive_fps, key="script")
        else:
            await browser.request_fps(DEFAULT_SCRIPT_FPS, key="script")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# 网页导航
# ═══════════════════════════════════════════════════════════════

def _extract_game_path(url: str) -> str:
    return urlparse(url).path.strip("/")


async def login(browser: UserBrowser, url: str) -> None:
    """导航到游戏页；DMM 登录页 eager 抢填。"""
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
        browser.script_log(f"初始跳转失败: {e}，进入重试循环")

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
            await browser.dmm_login(eager=True)
            continue

        # URL 已是游戏 path，但仍落在登录域 / 登录标题 → 继续抢登，勿误判进游戏
        if _url_looks_like_dmm_login(cur_url) or (
            "ログイン" in ((await browser.get_title) or "")
            and game_path not in cur_url
        ):
            stable_count = 0
            error_retry = 0
            await browser.dmm_login(eager=True)
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
            if stable_count >= 2:
                browser.script_log("进入游戏页面完成")
                break
            continue


# ═══════════════════════════════════════════════════════════════
# 状态处理
# ═══════════════════════════════════════════════════════════════

StateName = Optional[str]


async def _has_start_dialog(browser: UserBrowser) -> bool:
    """start1 + start2 同屏才视为开场确认弹窗。"""
    has1, has2 = await asyncio.gather(
        browser.match_image(
            _img("start1"), threshold=CFG.nav_threshold, quiet=True
        ),
        browser.match_image(
            _img("start2"), threshold=CFG.threshold, quiet=True
        ),
    )
    return bool(has1) and bool(has2)


async def wait_game_load_state(browser: UserBrowser) -> StateName:
    """等待游戏界面。

    加载前期只盯 start1 + rank（轻量）；见过 start 后才全量（logo/skip/关闭）。
    中途掉回 DMM 登录页则重新抢登。
    """
    global _ui_seen_start

    if await _recover_dmm_login_if_needed(browser):
        _ui_seen_start = False
        return None

    if not _ui_seen_start:
        rank, start1 = await asyncio.gather(
            browser.match_image(
                _img("rank"), threshold=CFG.nav_threshold, quiet=True
            ),
            browser.match_image(
                _img("start1"), threshold=CFG.nav_threshold, quiet=True
            ),
        )
        if rank:
            browser.script_log("  检测到 rank → 登录完成")
            return "__exit__"
        if start1:
            _ui_seen_start = True
            start2 = await browser.match_image(
                _img("start2"), threshold=CFG.threshold, quiet=True
            )
            if start2:
                browser.script_log("  检测到 start_dialog")
                return "start_dialog"
            # 只有 start1：下一轮全量再确认
            return None
        return None

    rank, logo, start1, start2, skip, close = await asyncio.gather(
        browser.match_image(_img("rank"), threshold=CFG.nav_threshold, quiet=True),
        browser.match_image(_img("logo"), threshold=CFG.nav_threshold, quiet=True),
        browser.match_image(_img("start1"), threshold=CFG.nav_threshold, quiet=True),
        browser.match_image(_img("start2"), threshold=CFG.threshold, quiet=True),
        browser.match_image(_img("skip"), threshold=CFG.threshold, quiet=True),
        browser.match_image(_img("关闭"), threshold=CFG.threshold, quiet=True),
    )

    if rank:
        browser.script_log("  检测到 rank → 登录完成")
        return "__exit__"
    if start1 and start2:
        browser.script_log("  检测到 start_dialog")
        return "start_dialog"
    if logo:
        browser.script_log("  检测到 logo → tap_screen")
        return "tap_screen"

    if skip:
        if await browser.click_image(
            _img("skip"),
            pianyi=(random.randint(-2, 2), 0),
            threshold=CFG.threshold,
        ):
            browser.script_log("  点击 skip")
            await browser.b_sleep(0.12, 0.28)
            return None
    if close:
        if await browser.click_image(_img("关闭"), threshold=CFG.threshold):
            browser.script_log("  点击 关闭")
            await browser.b_sleep(0.12, 0.28)
            return None
    return None


async def start_dialog_state(browser: UserBrowser) -> StateName:
    """点 start2；点不到则留在本状态重试，不无条件回等待。"""
    if not await _has_start_dialog(browser):
        # 弹窗已消失
        return "wait_game_load"

    if await browser.click_image(_img("start2"), threshold=CFG.threshold):
        browser.script_log("  点击 start2")
        for _ in range(6):
            await browser.b_sleep(0.22, 0.38)
            if not await browser.match_image(
                _img("start1"), threshold=CFG.nav_threshold, quiet=True
            ):
                break
        return "wait_game_load"

    browser.script_log("  未见/点不到 start2，留在 start_dialog 重试")
    await browser.b_sleep(0.25, 0.4)
    return None  # 留在本状态


async def tap_screen_state(browser: UserBrowser) -> StateName:
    """logo 附近连点几次再离开。"""
    taps = 0
    for i in range(CFG.logo_taps):
        if await browser.click_image(
            _img("logo"),
            pianyi=(random.randint(5, 15), random.randint(-5, 5)),
            threshold=CFG.nav_threshold,
        ):
            taps += 1
            browser.script_log(f"  点击 logo（{i + 1}/{CFG.logo_taps}）")
            await browser.b_sleep(0.15, 0.3)
        else:
            break
    if taps == 0:
        browser.script_log("  logo 已消失，回到等待")
    return "wait_game_load"


# 保留子状态以兼容超时表；实际 skip/关闭多在 wait 里点掉
async def skip_anim_state(browser: UserBrowser) -> StateName:
    if await browser.click_image(
        _img("skip"),
        pianyi=(random.randint(-2, 2), 0),
        threshold=CFG.threshold,
    ):
        browser.script_log("  点击了 skip")
        await browser.b_sleep(0.25, 0.5)
        return None
    return "wait_game_load"


async def close_popup_state(browser: UserBrowser) -> StateName:
    if await browser.click_image(_img("关闭"), threshold=CFG.threshold):
        browser.script_log("  关闭了弹窗")
        await browser.b_sleep(0.25, 0.5)
    return "wait_game_load"


STATES = {
    "wait_game_load": wait_game_load_state,
    "start_dialog": start_dialog_state,
    "tap_screen": tap_screen_state,
    "skip_anim": skip_anim_state,
    "close_popup": close_popup_state,
}

STATE_TIMEOUT = {
    "wait_game_load": CFG.wait_load_timeout,
    "start_dialog": 45,
    "tap_screen": 30,
    "skip_anim": 60,
    "close_popup": 30,
}


# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════

async def do_work(browser: UserBrowser):
    global _ui_seen_start, _last_dmm_recover_ts, _last_wait_match_ts, _last_net_log_ts

    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True

    try:
        browser.set_ad_block_enabled(CFG.prefer_ad_block)
    except Exception:
        pass

    status = "ok"
    _ui_seen_start = False
    _last_dmm_recover_ts = 0.0
    _last_wait_match_ts = 0.0
    _last_net_log_ts = 0.0
    if DEBUG_PSEUDO_RECORD or os.getenv("MINASHIGO_PSEUDO_RECORD"):
        browser.enable_pseudo_record(
            script_name="DO登录",
            force=True,
            keyframe_min_interval_s=(
                WAIT_KEYFRAME_INTERVAL_S if DEBUG_WAIT_FRAMES else None
            ),
        )

    try:
        browser.script_log("[DO登录v2] 开始网页导航")
        await login(browser, CFG.entry_url)

        await _prepare_game_matching(browser, retries=1)
        state_name = "wait_game_load"
        state_enter_time = asyncio.get_event_loop().time()
        wait_load_timeouts = 0
        last_wait_log = 0.0
        last_wake = 0.0
        last_guard = 0.0
        last_realign = 0.0
        black_since = 0.0  # >0 表示处于黑屏加载，暂停 wait 超时
        canvas_ok = await _frame_is_cropped(browser)
        # 开局裁剪已失败则等待期不再周期性对齐
        realign_fails = 0 if canvas_ok else CFG.realign_max_fails
        last_logged_state = ""
        await _set_passive_wait(browser, True)
        try:
            st = browser.ad_block_stats()
            browser.script_log(
                f"[DO登录v2] 网络优化 ad_block={st.get('enabled')} "
                f"(已拦 {st.get('blocked')} )"
            )
        except Exception:
            pass
        browser.script_log("[DO登录v2] 开始游戏内登录流程")

        while True:
            browser.note_state(state_name)
            await browser.update_frame()
            is_black = False
            net_busy = False
            if state_name == "wait_game_load":
                is_black = _probe_black_screen(browser)
                net_busy = _game_net_busy(browser)
                now_pre = asyncio.get_event_loop().time()
                # 黑屏或 Master/CDN 在途：刷新段计时，避免空等超时 / 误 goto
                loading = is_black or net_busy
                if is_black:
                    if black_since <= 0:
                        black_since = now_pre
                    if now_pre - black_since < CFG.wait_black_max_sec:
                        state_enter_time = now_pre
                    elif now_pre - last_wait_log >= 20.0:
                        browser.script_log(
                            f"  黑屏已持续 {now_pre - black_since:.0f}s "
                            f"（上限 {CFG.wait_black_max_sec:.0f}s），"
                            f"稍后按普通超时处理"
                        )
                        last_wait_log = now_pre
                else:
                    if black_since > 0:
                        black_since = 0.0
                if net_busy:
                    state_enter_time = now_pre
                    if now_pre - _last_net_log_ts >= 20.0:
                        n = _game_net_count(browser)
                        browser.script_log(
                            f"  游戏网络在途 {n} 个请求，继续等待（不超时/不唤醒）"
                        )
                        _last_net_log_ts = now_pre
                if not loading:
                    pass
            else:
                black_since = 0.0
            if not canvas_ok:
                canvas_ok = await _frame_is_cropped(browser)

            now = asyncio.get_event_loop().time()
            # 加载空等少查守卫；其它状态或间隔到了再查
            guard_due = (
                state_name != "wait_game_load"
                or (now - last_guard) >= CFG.guard_interval_wait
            )
            if guard_due:
                last_guard = now
                if await check_guards(browser):
                    continue

            timeout = STATE_TIMEOUT.get(state_name, 30)
            # 黑屏 / 游戏网络在途：不走超时分支（避免打断 Master 下载）
            in_black_load = (
                state_name == "wait_game_load"
                and is_black
                and black_since > 0
                and (now - black_since) < CFG.wait_black_max_sec
            )
            in_net_load = state_name == "wait_game_load" and net_busy
            in_loading = in_black_load or in_net_load
            if (not in_loading) and now - state_enter_time > timeout:
                if state_name == "wait_game_load":
                    wait_load_timeouts += 1
                    browser.script_log(
                        f"[超时] wait_game_load 超过 {timeout}s"
                        f"（第 {wait_load_timeouts}/{CFG.wait_load_goto_after} 次，"
                        f"非加载空等；仍等 start1）"
                    )
                    # 超时先查是否掉登录；确认仍在游戏页则继续等，勿过早 goto
                    if await _recover_dmm_login_if_needed(browser):
                        _ui_seen_start = False
                        wait_load_timeouts = max(0, wait_load_timeouts - 1)
                    elif wait_load_timeouts >= CFG.wait_load_goto_after:
                        # 最后再确认一次：若又开始拉 Master，则取消 goto
                        if _game_net_busy(browser):
                            browser.script_log(
                                "  仍有游戏网络在途，取消重跳转，继续等待"
                            )
                            wait_load_timeouts = max(0, wait_load_timeouts - 1)
                            state_enter_time = now
                            continue
                        browser.script_log(
                            f"  已连续非加载等待约 "
                            f"{wait_load_timeouts * timeout:.0f}s 仍无界面，"
                            f"重新跳转游戏页…"
                        )
                        try:
                            await browser.goto(CFG.entry_url)
                            await browser.b_sleep(2.0, 3.0)
                            # 跳转后可能又进登录页
                            if await _recover_dmm_login_if_needed(browser):
                                _ui_seen_start = False
                            canvas_ok = await _prepare_game_matching(
                                browser, retries=1
                            )
                            realign_fails = 0 if canvas_ok else CFG.realign_max_fails
                        except Exception as e:
                            browser.script_log(f"  重跳转失败: {e}")
                        wait_load_timeouts = 0
                        black_since = 0.0
                    else:
                        browser.script_log(
                            "  继续等待 start1（未达重跳转次数，不 goto）"
                        )
                else:
                    browser.script_log(
                        f"[超时] {state_name} 超过 {timeout}s，回到 wait_game_load"
                    )
                state_name = "wait_game_load"
                state_enter_time = now
                last_logged_state = ""
                await _set_passive_wait(browser, True)
                continue

            handler = STATES.get(state_name)
            if handler is None:
                browser.script_log(f"[错误] 未知状态: {state_name}，重置")
                state_name = "wait_game_load"
                state_enter_time = now
                continue

            if state_name != last_logged_state:
                browser.script_log(f"[{state_name}]")
                last_logged_state = state_name

            # 黑屏 / 网络忙：降频识图，少抢 CPU
            skip_match = False
            if state_name == "wait_game_load" and (is_black or net_busy):
                gap = (
                    CFG.black_match_interval
                    if is_black
                    else CFG.net_busy_match_interval
                )
                if _last_wait_match_ts > 0 and (now - _last_wait_match_ts) < gap:
                    skip_match = True
            if skip_match:
                next_state = None
            else:
                next_state = await handler(browser)
                _last_wait_match_ts = now

            if next_state == "__exit__":
                _probe_black_screen(browser, force_off=True)
                browser.script_log("[DO登录v2] ✅ 登录完成")
                break

            if next_state is not None and next_state != state_name:
                browser.script_log(f"  → {next_state}")
                if state_name == "wait_game_load":
                    _probe_black_screen(browser, force_off=True)
                state_name = next_state
                state_enter_time = now
                last_logged_state = ""
                await _set_passive_wait(browser, False)
                if next_state == "start_dialog":
                    _ui_seen_start = True

            if next_state is None and state_name == "wait_game_load":
                browser._note_progress()
                await _set_passive_wait(browser, True)
                if DEBUG_WAIT_FRAMES:
                    rec = getattr(browser, "pseudo_record", None) or getattr(
                        browser, "_pseudo", None
                    )
                    frame = getattr(browser._browser, "_frame", None)
                    if rec is not None and frame is not None:
                        try:
                            rec.maybe_keyframe(frame, reason="diag")
                        except Exception:
                            pass
                if now - last_wait_log >= 12.0:
                    browser.script_log("  仍在等待游戏界面标识…")
                    last_wait_log = now
                # 裁剪未成功且未超失败预算才重对齐，避免周期性白耗
                if (
                    not canvas_ok
                    and realign_fails < CFG.realign_max_fails
                    and now - last_realign >= CFG.realign_interval
                ):
                    canvas_ok = await _prepare_game_matching(
                        browser, retries=1, log=True
                    )
                    last_realign = now
                    if not canvas_ok:
                        realign_fails += 1
                waited = now - state_enter_time
                # 黑屏 / 网络忙：不要点唤醒（可能打断下载）
                if (
                    not is_black
                    and not net_busy
                    and waited >= CFG.wake_after_sec
                    and (
                        last_wake == 0.0
                        or now - last_wake >= CFG.wake_interval
                    )
                ):
                    await _maybe_wake_game(browser)
                    last_wake = now
                    last_guard = 0.0
                if is_black and now - last_wait_log >= 20.0:
                    browser.script_log(
                        f"  黑屏加载中…已 {now - black_since:.0f}s，继续等 start1"
                    )
                    last_wait_log = now
                if is_black or net_busy:
                    sleep = CFG.wait_poll_sleep_black
                elif _ui_seen_start:
                    sleep = CFG.wait_poll_sleep_active
                else:
                    sleep = CFG.wait_poll_sleep
                await browser.b_sleep(*sleep)
            elif next_state is None and state_name == "start_dialog":
                await browser.b_sleep(*CFG.act_poll_sleep)
            else:
                await browser.b_sleep(*CFG.act_poll_sleep)
    except Exception:
        status = "error"
        raise
    finally:
        await _set_passive_wait(browser, False)
        browser.use_game_frame_capture = False
        browser.finish_pseudo_record(status=status)
