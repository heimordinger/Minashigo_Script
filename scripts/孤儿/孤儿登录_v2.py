"""
孤儿登录 v2 —— 升级版
=======================
基于脚本解释（按 DO登录 同款性能路径）：

① 网页跳转 → 导航到游戏页，DMM 登录页 eager 抢填
② 游戏页   → 全视口/裁剪识图；等加载，点 start2 确认进入
③ 进入游戏 → menu 附近点击前进
④ 登录奖励 → skip / ok / close1 / close2；石头最低优先
⑤ 完成登录 → rank 出现即结束

伪录制：黑屏段计入 black，对比脚本改动看 effective。
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
    img_dir: Path = IMG_PATH / "minashigo" / "孤儿登录"
    entry_url: str = "https://play.games.dmm.co.jp/game/minashigo_x"

    state_timeout: float = 60.0
    wait_appear: float = 8.0
    threshold: float = 0.85
    nav_threshold: float = 0.85

    use_polling_cache: bool = True
    # 加载空等：长轮询；UI 已出现后略短
    wait_poll_sleep: tuple[float, float] = (1.2, 1.8)
    wait_poll_sleep_active: tuple[float, float] = (0.30, 0.50)
    act_poll_sleep: tuple[float, float] = (0.05, 0.12)
    menu_taps: int = 2
    guard_cooldown: float = 5.0
    guard_interval_wait: float = 8.0
    wake_after_sec: float = 40.0
    wake_interval: float = 40.0
    realign_interval: float = 25.0
    realign_max_fails: int = 1
    passive_fps: float = 0.0


CFG = Config()

# 见过 start 弹窗后，wait 改为全量探针
_ui_seen_start: bool = False


def _img(name: str) -> Path:
    return CFG.img_dir / (name if name.endswith(".png") else f"{name}.png")


def _probe_black_screen(browser: UserBrowser, *, force_off: bool = False) -> None:
    """边沿记录黑屏；不参与业务流程。"""
    if not DETECT_BLACK_SCREEN:
        return
    rec = getattr(browser, "pseudo_record", None) or getattr(browser, "_pseudo", None)
    if rec is None:
        return
    edge = None
    try:
        if force_off:
            edge = rec.note_black_frame(False, reason="force_off")
        else:
            frame = getattr(getattr(browser, "_browser", None), "_frame", None)
            if frame is None:
                return
            is_black, metrics = analyze_frame_blackness(frame)
            edge = rec.note_black_frame(is_black, **metrics)
    except Exception:
        return
    if not edge:
        return
    if edge.get("edge") == "on":
        browser.script_log(
            f"  [黑屏] 开始（mean={edge.get('mean')} dark={edge.get('dark_ratio')}）"
        )
    elif edge.get("edge") == "off":
        browser.script_log(f"  [黑屏] 结束 +{edge.get('dt_s')}s")


# ═══════════════════════════════════════════════════════════════
# 守卫（暂无素材，接口保留）
# ═══════════════════════════════════════════════════════════════

_guard_ts: dict[str, float] = {}
_GUARD_PAIRS: tuple[tuple[str, str, str], ...] = ()


async def check_guards(browser: UserBrowser) -> bool:
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
    browser: UserBrowser, *, retries: int = 1, log: bool = True
) -> bool:
    """进游戏页后尝试 GameCanvas 裁剪；失败则全视口并关掉每帧裁剪税。"""
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
        if frame is not None and log and (
            cropped or i == retries - 1 or (found and i == 0)
        ):
            browser.script_log(
                f"  游戏帧识图 mode={mode} frame={frame.shape[1]}x{frame.shape[0]}"
                + ("" if cropped else "（裁剪未生效）")
            )
        if cropped:
            return True
        if i + 1 < retries:
            await browser.b_sleep(0.4, 0.7)
    browser.use_game_frame_capture = False
    if log:
        browser.script_log("  裁剪未生效，改用全视口识图（停止重试对齐）")
    return False


async def _frame_is_cropped(browser: UserBrowser) -> bool:
    mode = getattr(browser._browser, "_frame_capture_mode", None) or "full"
    return mode not in ("full", None)


async def _maybe_wake_game(browser: UserBrowser) -> None:
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

    加载前期只盯 start2/start1 + rank；见过 start 后全量探测。
    """
    global _ui_seen_start

    if not _ui_seen_start:
        rank, start2 = await asyncio.gather(
            browser.match_image(
                _img("rank"), threshold=CFG.nav_threshold, quiet=True
            ),
            browser.match_image(
                _img("start2"), threshold=CFG.threshold, quiet=True
            ),
        )
        if rank:
            browser.script_log("  检测到 rank → 登录完成")
            return "__exit__"
        if start2:
            _ui_seen_start = True
            browser.script_log("  检测到 start_dialog")
            return "start_dialog"
        return None

    # 全量：rank > start > menu > skip/ok/close > 石头
    rank, start2, menu, skip, ok, close1, close2 = await asyncio.gather(
        browser.match_image(_img("rank"), threshold=CFG.nav_threshold, quiet=True),
        browser.match_image(_img("start2"), threshold=CFG.threshold, quiet=True),
        browser.match_image(_img("menu"), threshold=CFG.nav_threshold, quiet=True),
        browser.match_image(_img("skip"), threshold=CFG.threshold, quiet=True),
        browser.match_image(_img("ok"), threshold=CFG.threshold, quiet=True),
        browser.match_image(_img("close1"), threshold=CFG.threshold, quiet=True),
        browser.match_image(_img("close2"), threshold=CFG.threshold, quiet=True),
    )

    if rank:
        browser.script_log("  检测到 rank → 登录完成")
        return "__exit__"
    if start2:
        browser.script_log("  检测到 start_dialog")
        return "start_dialog"
    if menu:
        browser.script_log("  检测到 menu → tap_screen")
        return "tap_screen"
    if skip:
        browser.script_log("  检测到 skip")
        return "skip_anim"
    if ok:
        browser.script_log("  检测到 ok")
        return "close_popup"
    if close1:
        browser.script_log("  检测到 close1")
        return "close_btn"
    if close2:
        browser.script_log("  检测到 close2")
        return "close_btn2"

    if await browser.match_image(_img("石头"), threshold=CFG.threshold, quiet=True):
        browser.script_log("  检测到 石头")
        return "stone"
    return None


async def start_dialog_state(browser: UserBrowser) -> StateName:
    if not await _has_start_dialog(browser):
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
    return None


async def tap_screen_state(browser: UserBrowser) -> StateName:
    taps = 0
    for i in range(CFG.menu_taps):
        if await browser.click_image(
            _img("menu"),
            pianyi=(random.randint(50, 200), random.randint(30, 200)),
            threshold=CFG.nav_threshold,
        ):
            taps += 1
            browser.script_log(f"  点击 menu 附近（{i + 1}/{CFG.menu_taps}）")
            await browser.b_sleep(0.15, 0.3)
        else:
            break
    if taps == 0:
        browser.script_log("  menu 已消失，回到等待")
    return "wait_game_load"


async def skip_anim_state(browser: UserBrowser) -> StateName:
    if await browser.click_image(
        _img("skip"),
        pianyi=(random.randint(-2, 2), 0),
        threshold=CFG.threshold,
    ):
        browser.script_log("  点击 skip")
        await browser.b_sleep(0.12, 0.28)
        return None
    return "wait_game_load"


async def close_popup_state(browser: UserBrowser) -> StateName:
    if await browser.click_image(_img("ok"), threshold=CFG.threshold):
        browser.script_log("  点击 ok")
        await browser.b_sleep(0.12, 0.28)
    return "wait_game_load"


async def close_btn_state(browser: UserBrowser) -> StateName:
    if await browser.click_image(_img("close1"), threshold=CFG.threshold):
        browser.script_log("  点击 close1")
        await browser.b_sleep(0.12, 0.28)
    return "wait_game_load"


async def close_btn2_state(browser: UserBrowser) -> StateName:
    if await browser.click_image(_img("close2"), threshold=CFG.threshold):
        browser.script_log("  点击 close2")
        await browser.b_sleep(0.12, 0.28)
    return "wait_game_load"


async def stone_state(browser: UserBrowser) -> StateName:
    if await browser.click_image(
        _img("石头"),
        pianyi=(random.randint(20, 60), random.randint(10, 30)),
        threshold=CFG.threshold,
    ):
        browser.script_log("  点击石头附近")
        await browser.b_sleep(0.12, 0.28)
    return "wait_game_load"


STATES = {
    "wait_game_load": wait_game_load_state,
    "start_dialog": start_dialog_state,
    "tap_screen": tap_screen_state,
    "skip_anim": skip_anim_state,
    "close_popup": close_popup_state,
    "close_btn": close_btn_state,
    "close_btn2": close_btn2_state,
    "stone": stone_state,
}

STATE_TIMEOUT = {
    "wait_game_load": 90,
    "start_dialog": 45,
    "tap_screen": 30,
    "skip_anim": 60,
    "close_popup": 30,
    "close_btn": 30,
    "close_btn2": 30,
    "stone": 30,
}


# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════

async def do_work(browser: UserBrowser):
    global _ui_seen_start

    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True

    status = "ok"
    _ui_seen_start = False
    if DEBUG_PSEUDO_RECORD or os.getenv("MINASHIGO_PSEUDO_RECORD"):
        browser.enable_pseudo_record(
            script_name="孤儿登录",
            force=True,
            keyframe_min_interval_s=(
                WAIT_KEYFRAME_INTERVAL_S if DEBUG_WAIT_FRAMES else None
            ),
        )

    try:
        browser.script_log("[孤儿登录v2] 开始网页导航")
        await login(browser, CFG.entry_url)

        await _prepare_game_matching(browser, retries=1)
        state_name = "wait_game_load"
        state_enter_time = asyncio.get_event_loop().time()
        wait_load_timeouts = 0
        last_wait_log = 0.0
        last_wake = 0.0
        last_guard = 0.0
        last_realign = 0.0
        canvas_ok = await _frame_is_cropped(browser)
        realign_fails = 0 if canvas_ok else CFG.realign_max_fails
        last_logged_state = ""
        await _set_passive_wait(browser, True)
        browser.script_log("[孤儿登录v2] 开始游戏内登录流程")

        while True:
            browser.note_state(state_name)
            await browser.update_frame()
            if state_name == "wait_game_load":
                _probe_black_screen(browser)
            if not canvas_ok:
                canvas_ok = await _frame_is_cropped(browser)

            now = asyncio.get_event_loop().time()
            guard_due = (
                state_name != "wait_game_load"
                or (now - last_guard) >= CFG.guard_interval_wait
            )
            if guard_due:
                last_guard = now
                if await check_guards(browser):
                    continue

            timeout = STATE_TIMEOUT.get(state_name, 30)
            if now - state_enter_time > timeout:
                if state_name == "wait_game_load":
                    wait_load_timeouts += 1
                    browser.script_log(
                        f"[超时] wait_game_load 超过 {timeout}s"
                        f"（第 {wait_load_timeouts} 次）"
                    )
                    if wait_load_timeouts >= 3:
                        browser.script_log("  多次等待无果，重新跳转游戏页…")
                        try:
                            await browser.goto(CFG.entry_url)
                            await browser.b_sleep(2.0, 3.0)
                            canvas_ok = await _prepare_game_matching(
                                browser, retries=1
                            )
                            realign_fails = (
                                0 if canvas_ok else CFG.realign_max_fails
                            )
                        except Exception as e:
                            browser.script_log(f"  重跳转失败: {e}")
                        wait_load_timeouts = 0
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

            next_state = await handler(browser)

            if next_state == "__exit__":
                _probe_black_screen(browser, force_off=True)
                browser.script_log("[孤儿登录v2] ✅ 登录完成")
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
                if waited >= CFG.wake_after_sec and (
                    last_wake == 0.0 or now - last_wake >= CFG.wake_interval
                ):
                    await _maybe_wake_game(browser)
                    last_wake = now
                    last_guard = 0.0
                sleep = (
                    CFG.wait_poll_sleep_active
                    if _ui_seen_start
                    else CFG.wait_poll_sleep
                )
                await browser.b_sleep(*sleep)
            elif next_state is None and state_name in (
                "start_dialog",
                "skip_anim",
            ):
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
