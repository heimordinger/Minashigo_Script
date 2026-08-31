"""DeepOne 日常（手写草稿）：房间领体力 / 竞技场 / 爬塔 / 每日关卡 / 领取礼物 / 任务奖励。"""
from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from backend.automation.frame_observer import DEFAULT_SCRIPT_FPS
from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

SCRIPT_PATH = IMG_PATH / "DeepOne" / "DO日常"
# 全过程伪录制（时间线+稀疏关键帧）。也可设环境变量 MINASHIGO_PSEUDO_RECORD=1
DEBUG_PSEUDO_RECORD = True
THRESHOLD = 0.9
NAV_THRESHOLD = 0.85
JJC_END_THRESHOLD = 0.99
JJC_REFRESH_OFFSET = (-200, 0)
# 段位提升 / 结算 OK：像素级匹配（1:1），压假阳性
JJC_OK_THRESHOLD = 0.95
JJC_OK_PIXEL_FALLBACK = 0.90  # 像素模式次档；仍严于旧相关匹配
JJC_OK_PIXEL_TOL = 10.0  # 单通道平均绝对差上限 (0~255)
JJC_OK_USE_COLOR = False  # 像素匹配已比色，不必再开颜色校验
JJC_OK_MATCH_MODE = "pixel"
JJC_RANKUP_THRESHOLD = 0.8
# 段位标题条误匹配 FANZA 顶栏（y_frac≈0.064）；真弹窗标题 y_frac≈0.10–0.15（边界测试 0.9987@0.147）
JJC_RANKUP_Y_FRAC_MIN = 0.10
JJC_RANKUP_Y_FRAC_MAX = 0.55
# 只见 rankup 标题：主动点 OK + 位置兜底，不单 passive 等 visible
RANKUP_OK_MAX_WAIT_SEC = 25.0
JJC_TOUCH_THRESHOLD = 0.82
# 被动等待（战斗/AUTO）：停后台截图，轮询加长，减少空匹配
PASSIVE_OBS_FPS = 0.0  # 0=停 observer；循环内主动 update_frame
JJC_FIGHT_POLL_SLEEP = (3.2, 4.5)
TOWER_AUTO_IDLE_SLEEP = (3.5, 5.0)
TOWER_AUTO_BANNER_SLEEP = (3.0, 4.5)
NAV_POLL_SLEEP = (0.7, 1.1)
# 次数耗尽图易误匹配，阈值要高
TA_CISHU_THRESHOLD = 0.99
# 塔出击界面 AUTO / 出撃：UI 改版后花纹模板分偏低，单独阈值
TA_AUTO_THRESHOLD = 0.82
TA_SORTIE_BTN_THRESHOLD = 0.8
# AUTO 灰/蓝两态靠颜色区分；use_color_check 在灰度定位后再比 BGR
TA_AUTO_USE_COLOR = True
# AUTO进行中横幅（断续出现）；近期见过则禁止点奖励
TA_AUTO_BANNER_THRESHOLD = 0.8
TA_AUTO_BANNER_RECENT_SEC = 10.0
# 最终关：横幅消失且奖励框持续稳定后才点 OK（中间关 auto 约 4s 内会自己关）
TA_FINAL_REWARD_QUIET_SEC = 6.0
TA_FINAL_REWARD_HOLD_SEC = 3.0
# 每日关卡：开扫荡窗 / 点确认 / 点 OK
MEIRI_SWEEP_TIMEOUT = 90.0
MEIRI_SKIP_CONFIRM_INTERVAL = (3.0, 5.0)
# 领取礼物
GIFT_TIMEOUT = 60.0
TASK_REWARD_TIMEOUT = 120.0
# 一括受け取り 亮/暗态：灰度定位后再比色，区分 claim / end
TASK_CLAIM_USE_COLOR = True
# 相对「出撃」按钮中心点到 AUTO 按钮（模板失败时的兜底）
TOWER_AUTO_OFFSET_FROM_SORTIE = (-110, 0)
# AUTO 切换 / 出撃：游戏 GUI 有动画，点太快会丢操作（已略收紧）
TA_AUTO_TOGGLE_DELAY = (0.35, 0.55)
TA_AUTO_SETTLE_DELAY = (0.5, 0.8)
TA_SORTIE_CLICK_RETRIES = 3
TA_SORTIE_CONFIRM_SLEEP = (0.7, 1.1)
# 进塔选关 / 等 logo：短轮询代替固定 2s×3
TA_ENTER_POLL = (0.55, 0.85)
TA_ENTER_TIMEOUT = 8.0
MAX_ARENA_ROUNDS = 20
MAX_TOWER_ROUNDS = 20
# 单步最长停留；超时则回退上一步
STEP_TIMEOUT = 45.0
# 回主界面 / 回出击：含多次点击与 loading
STEP_TIMEOUT_NAV = 120.0
# 导航：过场期间 passive 等待；home/出击 点击冷却（秒）
NAV_HOME_CLICK_COOLDOWN = 15.0
NAV_SORTIE_CLICK_COOLDOWN = 12.0
# 过场日志节流（秒）；同一 tag 间隔内不重复刷
TRANSITION_LOG_INTERVAL = 8.0
# 战斗结算点击 / 爬塔领奖等
STEP_TIMEOUT_BATTLE = 180.0
# jjc 出击后：战斗动画 + 过场 loading，需长时间持续等待
STEP_TIMEOUT_JJC_FIGHT = 480.0
# 回退成功后，同一当前步最多再试几次，避免「上一步永远成功、当前步永远失败」死循环
MAX_STEP_RETRIES = 3

StepFn = Callable[[UserBrowser], Awaitable[bool]]
# (名称, 函数) 或 (名称, 函数, 该步超时秒数)
StepItem = tuple[str, StepFn] | tuple[str, StepFn, float]


class DailyStepError(RuntimeError):
    """步骤失败且回退上一步仍失败。"""


def _img(name: str) -> Path:
    return SCRIPT_PATH / (name if name.endswith(".png") else f"{name}.png")


def _unpack_step(item: StepItem, default_timeout: float) -> tuple[str, StepFn, float]:
    if len(item) == 3:
        name, fn, timeout = item  # type: ignore[misc]
        return name, fn, float(timeout)
    name, fn = item  # type: ignore[misc]
    return name, fn, default_timeout


class StepTimer:
    """步骤内轮询计时器；过场期间可 pause，不计入限时。"""

    def __init__(self, name: str, timeout: float):
        self.name = name
        self.timeout = timeout
        self._start = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self._start > self.timeout

    def remaining(self) -> float:
        return max(0.0, self.timeout - (time.monotonic() - self._start))

    def pause_for_transition(self) -> None:
        """过场期间重置起点（等同 run_task 的 se_time = now）。"""
        self._start = time.monotonic()


@asynccontextmanager
async def _passive_observe(browser: UserBrowser, *, fps: float = PASSIVE_OBS_FPS):
    """被动等待：压低/关闭后台截图，结束恢复脚本默认 FPS。"""
    try:
        await browser.request_fps(fps, key="script")
        yield
    finally:
        try:
            await browser.request_fps(DEFAULT_SCRIPT_FPS, key="script")
        except Exception:
            pass


# 步超时遇过场时最多续等次数（防止无限循环）
_MAX_TRANSITION_STEP_RETRIES = 30


async def run_step_chain(
    browser: UserBrowser,
    steps: list[StepItem],
    *,
    step_timeout: float = STEP_TIMEOUT,
    label: str = "",
) -> bool:
    """按顺序执行步骤。

    - 某步超时或返回 False → 回退执行上一步
    - 上一步再失败/超时 → 抛 DailyStepError
    - 全部成功 → True
    - 单步可写 (name, fn, timeout) 覆盖默认超时
    """
    if not steps:
        return True
    tag = f"[{label}] " if label else ""
    i = 0
    retries_at_i = 0
    transition_retries = 0
    while i < len(steps):
        name, fn, timeout = _unpack_step(steps[i], step_timeout)
        browser.script_log(
            f"{tag}步骤 {i + 1}/{len(steps)}「{name}」（限时 {timeout:.0f}s）"
        )
        try:
            ok = await asyncio.wait_for(fn(browser), timeout=timeout)
            transition_retries = 0
        except asyncio.TimeoutError:
            await browser.update_frame()
            if await _is_transition(browser):
                probe = await _probe_scene(browser)
                transition_retries += 1
                if transition_retries <= _MAX_TRANSITION_STEP_RETRIES:
                    browser.script_log(
                        f"{tag}「{name}」步限时到但在过场，不计超时"
                        f"（续等 {transition_retries}/{_MAX_TRANSITION_STEP_RETRIES}）"
                        f" | {probe.format_flags()}"
                    )
                    await browser.b_sleep(0.8, 1.2)
                    continue
            transition_retries = 0
            browser.script_log(f"{tag}「{name}」停留超过 {timeout:.0f}s")
            ok = False
        except DailyStepError:
            raise
        except Exception as e:
            browser.script_log(f"{tag}「{name}」异常: {type(e).__name__}: {e}")
            ok = False

        if ok:
            i += 1
            retries_at_i = 0
            continue

        retries_at_i += 1
        if retries_at_i > MAX_STEP_RETRIES:
            raise DailyStepError(
                f"{tag}步骤「{name}」连续失败 {MAX_STEP_RETRIES} 次"
            )

        # 第一步无上一步：直接重试；其后失败则先回退上一步
        if i == 0:
            browser.script_log(
                f"{tag}「{name}」失败，重试"
                f"（第 {retries_at_i}/{MAX_STEP_RETRIES} 次）"
            )
            await browser.b_sleep(1.0, 2.0)
            continue

        prev_name, prev_fn, prev_timeout = _unpack_step(steps[i - 1], step_timeout)
        browser.script_log(
            f"{tag}「{name}」失败 → 回退「{prev_name}」"
            f"（第 {retries_at_i}/{MAX_STEP_RETRIES} 次）"
        )
        try:
            prev_ok = await asyncio.wait_for(prev_fn(browser), timeout=prev_timeout)
            transition_retries = 0
        except asyncio.TimeoutError:
            await browser.update_frame()
            if await _is_transition(browser):
                transition_retries += 1
                if transition_retries <= _MAX_TRANSITION_STEP_RETRIES:
                    browser.script_log(
                        f"{tag}回退「{prev_name}」限时到但在过场，不计超时"
                    )
                    await browser.b_sleep(0.8, 1.2)
                    continue
            transition_retries = 0
            browser.script_log(f"{tag}回退「{prev_name}」超时")
            prev_ok = False
        except Exception as e:
            browser.script_log(
                f"{tag}回退「{prev_name}」异常: {type(e).__name__}: {e}"
            )
            prev_ok = False

        if not prev_ok:
            raise DailyStepError(
                f"{tag}步骤「{name}」失败，回退「{prev_name}」仍失败"
            )
        browser.script_log(f"{tag}回退成功，重试「{name}」")
    return True


async def _wait_img(
    browser: UserBrowser,
    name: str,
    *,
    timeout: float,
    threshold: float = THRESHOLD,
    extend_on_transition: bool = True,
) -> bool:
    """带阈值的等待出现。

    extend_on_transition=False：过场不续时（点段位后等编队等应用）。
    """
    step_start = time.monotonic()
    last_transition_log = 0.0
    while time.monotonic() - step_start < timeout:
        await browser.update_frame()
        if await browser.match_image(_img(name), threshold=threshold, quiet=True):
            return True
        if extend_on_transition and await _is_transition(browser):
            step_start = time.monotonic()
            last_transition_log = await _log_if_transition(
                browser, f"等待 {name}", last_transition_log
            )
        await browser.b_sleep(0.35, 0.5)
    return False


# 脚本介绍中的场景标识；全未命中且无 home → 过场
SCENE_MARKERS = (
    "rank",
    "出击_logo",
    "jjc_logo",
    "room_logo",
    "ta_logo",
    "meiri_logo",
    "meiri_skip_title",
    "menu_logo",
    "gift_logo",
)


def _scene_probe_specs() -> list[tuple]:
    """并行场景探测模板列表（顺序与 SceneProbe 字段对应）。"""
    return [
        (_img("home"), THRESHOLD),
        (_img("rank"), NAV_THRESHOLD),
        (_img("出击_logo"), NAV_THRESHOLD),
        (_img("jjc_logo"), NAV_THRESHOLD),
        (_img("room_logo"), NAV_THRESHOLD),
        (_img("ta_logo"), NAV_THRESHOLD),
        (_img("meiri_logo"), NAV_THRESHOLD),
        (_img("meiri_skip_title"), NAV_THRESHOLD),
        (_img("menu_logo"), NAV_THRESHOLD),
        (_img("gift_logo"), NAV_THRESHOLD),
        (_img("jjc_结算"), NAV_THRESHOLD),
        (_img("jjc_touch"), JJC_TOUCH_THRESHOLD),
        (
            _img("jjc_ok"),
            JJC_OK_THRESHOLD,
            {
                "use_color_check": JJC_OK_USE_COLOR,
                "match_mode": JJC_OK_MATCH_MODE,
                "pixel_tol": JJC_OK_PIXEL_TOL,
            },
        ),
    ]


@dataclass
class SceneProbe:
    """一次截图后并行识图得到的场景快照。"""

    home: bool = False
    rank: bool = False
    sortie_logo: bool = False
    jjc_logo: bool = False
    room_logo: bool = False
    ta_logo: bool = False
    meiri_logo: bool = False
    meiri_sweeping: bool = False
    menu_logo: bool = False
    gift_logo: bool = False
    jjc_result: bool = False
    jjc_touch: bool = False
    jjc_ok: bool = False
    jjc_rankup: bool = False

    @property
    def any_scene_marker(self) -> bool:
        return (
            self.rank
            or self.sortie_logo
            or self.jjc_logo
            or self.room_logo
            or self.ta_logo
            or self.meiri_logo
            or self.meiri_sweeping
            or self.menu_logo
            or self.gift_logo
        )

    @property
    def jjc_settlement(self) -> bool:
        return (
            self.jjc_result
            or self.jjc_touch
            or self.jjc_ok
            or self.jjc_rankup
        )

    @property
    def is_transition(self) -> bool:
        return not (self.home or self.any_scene_marker or self.jjc_settlement)

    def format_flags(self) -> str:
        """探针摘要，便于日志里看过场识别依据。"""
        def m(ok: bool) -> str:
            return "✓" if ok else "×"

        settle = (
            self.jjc_result
            or self.jjc_touch
            or self.jjc_ok
            or self.jjc_rankup
        )
        return (
            f"home{m(self.home)} rank{m(self.rank)} "
            f"出击{m(self.sortie_logo)} jjc{m(self.jjc_logo)} "
            f"room{m(self.room_logo)} ta{m(self.ta_logo)} "
            f"meiri{m(self.meiri_logo)} sweep{m(self.meiri_sweeping)} "
            f"menu{m(self.menu_logo)} gift{m(self.gift_logo)} "
            f"结算{m(settle)}"
        )


async def _probe_scene(browser: UserBrowser) -> SceneProbe:
    """并行匹配 home / 场景标识 / 竞技场结算 UI。"""
    parallel_results, rankup = await asyncio.gather(
        browser.match_images_parallel(_scene_probe_specs(), quiet=True),
        _jjc_rankup_visible(browser),
    )
    flags = [bool(r) for r in parallel_results]
    probe = SceneProbe(
        home=flags[0],
        rank=flags[1],
        sortie_logo=flags[2],
        jjc_logo=flags[3],
        room_logo=flags[4],
        ta_logo=flags[5],
        meiri_logo=flags[6],
        meiri_sweeping=flags[7],
        menu_logo=flags[8],
        gift_logo=flags[9],
        jjc_result=flags[10],
        jjc_touch=flags[11],
        jjc_ok=flags[12],
        jjc_rankup=rankup,
    )
    if not probe.jjc_ok and JJC_OK_MATCH_MODE == "pixel":
        probe.jjc_ok = bool(
            await browser.match_image(
                _img("jjc_ok"),
                threshold=JJC_OK_PIXEL_FALLBACK,
                match_mode=JJC_OK_MATCH_MODE,
                pixel_tol=JJC_OK_PIXEL_TOL,
                quiet=True,
            )
        )
    elif not probe.jjc_ok and JJC_OK_USE_COLOR:
        probe.jjc_ok = bool(
            await browser.match_image(
                _img("jjc_ok"),
                threshold=JJC_OK_THRESHOLD,
                use_color_check=False,
                quiet=True,
            )
        )
    return probe


async def _handle_sortie_limit_popup(browser: UserBrowser) -> bool:
    """部分账号点击出击后会弹出「出撃制限」；见 出击限制 后点 出击限制_出击。"""
    if not await browser.match_image(_img("出击限制"), threshold=NAV_THRESHOLD):
        return False
    browser.script_log("出现出撃制限弹窗，点击确认出撃")
    if await browser.click_image(_img("出击限制_出击"), threshold=THRESHOLD):
        await browser.b_sleep(0.8, 1.2)
        return True
    browser.script_log("出击限制_出击 点击失败")
    return False


async def _after_sortie_click(browser: UserBrowser) -> None:
    """出击点击后短暂等待并处理出撃制限弹窗（全游戏出击点通用）。"""
    for _ in range(4):
        await browser.b_sleep(0.5, 0.8)
        await browser.update_frame()
        if await _handle_sortie_limit_popup(browser):
            return
        if not await browser.match_image(_img("出击限制"), threshold=NAV_THRESHOLD):
            return


async def _has_nav_chrome(browser: UserBrowser) -> bool:
    return (await _probe_scene(browser)).home


async def _is_jjc_settlement_ui(browser: UserBrowser) -> bool:
    """竞技场结算/奖励弹窗：可交互 UI，不是 passive 过场。"""
    return (await _probe_scene(browser)).jjc_settlement


async def _is_transition(browser: UserBrowser, probe: SceneProbe | None = None) -> bool:
    """无场景标识且无 home → 视为过场 loading（不认 loading 美术图）。"""
    if probe is None:
        return (await _probe_scene(browser)).is_transition
    return probe.is_transition


async def _log_if_transition(
    browser: UserBrowser,
    tag: str,
    last_log: float,
    *,
    interval: float = TRANSITION_LOG_INTERVAL,
    probe: SceneProbe | None = None,
) -> float:
    """处于过场时打日志（节流），附带场景探针明细。"""
    if probe is None:
        probe = await _probe_scene(browser)
    if not probe.is_transition:
        return last_log
    now = time.monotonic()
    if now - last_log >= interval:
        browser.script_log(f"[过场识别] {tag} | {probe.format_flags()}")
        return now
    return last_log


async def _prepare_game_matching(browser: UserBrowser) -> None:
    """GameCanvas 裁剪识图（抗页面滚动；热区走 game: 分桶）。"""
    browser.use_game_frame_capture = True
    await browser.align_game_viewport()
    await browser.update_frame()
    frame = getattr(browser._browser, "_frame", None)
    mode = getattr(browser._browser, "_frame_capture_mode", None) or "full"
    if frame is not None:
        browser.script_log(
            f"游戏帧识图 mode={mode} frame={frame.shape[1]}x{frame.shape[0]}"
        )
    else:
        browser.script_log("游戏帧准备失败：无可用帧")


# ── 导航辅助 ───────────────────────────────────────────────


async def 返回主界面(browser: UserBrowser) -> bool:
    """点 home 回主界面；长过场时 passive 等待，不在 loading 中判失败。"""
    timer = StepTimer("go_main", STEP_TIMEOUT_NAV)
    last_log = 0.0
    last_home_click = 0.0
    while not timer.expired():
        await browser.update_frame()
        # 轻量导航：只盯 rank / home，不做 11 路场景探针
        has_rank, has_home = await asyncio.gather(
            browser.match_image(_img("rank"), threshold=NAV_THRESHOLD, quiet=True),
            browser.match_image(_img("home"), threshold=THRESHOLD, quiet=True),
        )
        if has_rank:
            browser.script_log("已在主界面")
            return True

        now = time.monotonic()
        if has_home and now - last_home_click >= NAV_HOME_CLICK_COOLDOWN:
            if await browser.click_image(_img("home"), threshold=THRESHOLD):
                browser.script_log("点击 home，等待 rank…")
                last_home_click = now
                await browser.b_sleep(*NAV_POLL_SLEEP)
                continue

        if not has_home and not has_rank:
            timer.pause_for_transition()
            if now - last_log >= TRANSITION_LOG_INTERVAL:
                browser.script_log("[过场识别] 导航→主界面 | 轻量(无 rank/home)")
                last_log = now
            await browser.b_sleep(*NAV_POLL_SLEEP)
            continue

        if now - last_log >= 10.0:
            browser.script_log("  等待 home / rank 出现…")
            last_log = now
        await browser.b_sleep(*NAV_POLL_SLEEP)

    browser.script_log(f"等待回主界面超时（{STEP_TIMEOUT_NAV:.0f}s）")
    return False


async def 返回出击界面(browser: UserBrowser) -> bool:
    """进入出击界面；长过场 passive 等待，不在 loading 中反复判失败。"""
    timer = StepTimer("go_sortie", STEP_TIMEOUT_NAV)
    last_log = 0.0
    last_home_click = 0.0
    last_sortie_click = 0.0
    while not timer.expired():
        await browser.update_frame()
        has_sortie, has_home, has_rank = await asyncio.gather(
            browser.match_image(_img("出击_logo"), threshold=NAV_THRESHOLD, quiet=True),
            browser.match_image(_img("home"), threshold=THRESHOLD, quiet=True),
            browser.match_image(_img("rank"), threshold=NAV_THRESHOLD, quiet=True),
        )
        if has_sortie:
            browser.script_log("已在出击界面")
            return True

        now = time.monotonic()
        # 标识未命中：优先尝试点击可见的出击按钮（不必先回主界面）
        if now - last_sortie_click >= NAV_SORTIE_CLICK_COOLDOWN:
            if await browser.click_image(_img("出击"), threshold=THRESHOLD):
                browser.script_log("尝试点击出击…")
                last_sortie_click = now
                await browser.b_sleep(*NAV_POLL_SLEEP)
                await browser.update_frame()
                if await browser.match_image(
                    _img("出击_logo"), threshold=NAV_THRESHOLD, quiet=True
                ):
                    browser.script_log("已进入出击界面")
                    return True
                continue

        if not has_home and not has_rank and not has_sortie:
            timer.pause_for_transition()
            if now - last_log >= TRANSITION_LOG_INTERVAL:
                browser.script_log("[过场识别] 导航→出击界面 | 轻量(无 chrome)")
                last_log = now
            await browser.b_sleep(*NAV_POLL_SLEEP)
            continue

        if not has_rank and has_home:
            if now - last_home_click >= NAV_HOME_CLICK_COOLDOWN:
                if await browser.click_image(_img("home"), threshold=THRESHOLD):
                    browser.script_log("未在主界面，点击 home 再进出击…")
                    last_home_click = now
                    await browser.b_sleep(*NAV_POLL_SLEEP)
                continue

        if now - last_log >= 10.0:
            browser.script_log("  等待出击界面…")
            last_log = now
        await browser.b_sleep(*NAV_POLL_SLEEP)

    browser.script_log(f"等待出击界面超时（{STEP_TIMEOUT_NAV:.0f}s）")
    return False


async def 打开菜单(browser: UserBrowser) -> bool:
    """打开游戏菜单；战斗中通常无 menu 按钮，点不到则失败。"""
    await browser.update_frame()
    if await browser.match_image(_img("menu_logo"), threshold=NAV_THRESHOLD):
        browser.script_log("菜单已打开")
        return True
    if not await browser.click_image(_img("menu"), threshold=THRESHOLD):
        browser.script_log("未找到 menu.png（可能在战斗/过场中）")
        return False
    browser.script_log("点击 menu，等待菜单…")
    if await _wait_img(browser, "menu_logo", timeout=10, threshold=NAV_THRESHOLD):
        browser.script_log("已打开菜单")
        return True
    browser.script_log("点击 menu 后未见 menu_logo")
    return False


# ── 房间 ───────────────────────────────────────────────────


async def _room_go_main(browser: UserBrowser) -> bool:
    """优先尝试 room 直接进入；失败再回主界面。"""
    await browser.update_frame()
    if await browser.match_image(_img("room_logo"), threshold=NAV_THRESHOLD):
        browser.script_log("已在房间，跳过回主界面")
        return True
    if await browser.click_image(_img("room"), threshold=THRESHOLD):
        browser.script_log("尝试点击 room 进入房间…")
        if await _wait_img(browser, "room_logo", timeout=15, threshold=NAV_THRESHOLD):
            browser.script_log("直接进入房间，跳过回主界面")
            return True
    return await 返回主界面(browser)


async def _room_enter(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await browser.match_image(_img("room_logo"), threshold=NAV_THRESHOLD):
        return True
    if not await browser.click_image(_img("room"), threshold=THRESHOLD):
        browser.script_log("未找到 room.png")
        return False
    browser.script_log("点击 room，等待房间界面")
    return await _wait_img(browser, "room_logo", timeout=15, threshold=NAV_THRESHOLD)


async def _room_claim(browser: UserBrowser) -> bool:
    """领取房间奖励（对齐脚本介绍：点收取等 1.5s；无 ok 累计 3 次则结束）。"""
    browser.script_log("已在房间，开始领取奖励")
    await browser.update_frame()

    if await browser.match_image(_img("room_ap上限"), threshold=NAV_THRESHOLD):
        browser.script_log("AP 上限，任务结束")
        return True

    # 仅有 ok 弹窗时先关掉
    if await browser.match_image(_img("room_ok"), threshold=THRESHOLD):
        while await browser.match_image(_img("room_ok"), threshold=THRESHOLD):
            await browser.click_image(_img("room_ok"), threshold=THRESHOLD)
            await browser.b_sleep(0.5, 0.8)
            await browser.update_frame()
        if not await browser.match_image(_img("room_收取奖励"), threshold=THRESHOLD):
            browser.script_log("关 ok 后无收取按钮，任务完成")
            return True

    if not await browser.match_image(_img("room_收取奖励"), threshold=THRESHOLD):
        browser.script_log("无 room_收取奖励，任务完成")
        return True

    max_no_ok = 3
    for attempt in range(1, max_no_ok + 1):
        await browser.update_frame()
        if await browser.match_image(_img("room_ap上限"), threshold=NAV_THRESHOLD):
            browser.script_log("AP 上限，任务结束")
            return True

        if not await browser.click_image(_img("room_收取奖励"), threshold=THRESHOLD):
            browser.script_log("点击 room_收取奖励 失败")
            return False

        browser.script_log(f"已点收取，等待 ok（{attempt}/{max_no_ok}）")
        await browser.b_sleep(1.5, 1.5)
        await browser.update_frame()

        if await browser.match_image(_img("room_ap上限"), threshold=NAV_THRESHOLD):
            browser.script_log("AP 上限提示，任务结束")
            return True

        if await browser.match_image(_img("room_ok"), threshold=THRESHOLD):
            while await browser.match_image(_img("room_ok"), threshold=THRESHOLD):
                await browser.click_image(_img("room_ok"), threshold=THRESHOLD)
                await browser.b_sleep(0.5, 0.8)
                await browser.update_frame()
            browser.script_log("领取完成")
            return True

        if attempt >= max_no_ok:
            browser.script_log("无 room_ok 累计 3 次，任务结束")
            return True

    return True


async def 房间领取奖励(browser: UserBrowser) -> bool:
    return await run_step_chain(
        browser,
        [
            ("回主界面", _room_go_main, STEP_TIMEOUT_NAV),
            ("进入房间", _room_enter, STEP_TIMEOUT),
            ("领取奖励", _room_claim, STEP_TIMEOUT),
        ],
        step_timeout=STEP_TIMEOUT,
        label="房间",
    )


# ── 竞技场 ─────────────────────────────────────────────────


async def _jjc_go_sortie(browser: UserBrowser) -> bool:
    return await 返回出击界面(browser)


async def _jjc_enter(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await browser.match_image(_img("jjc_logo"), threshold=NAV_THRESHOLD):
        return True
    if not await browser.click_image(_img("jjc"), threshold=THRESHOLD):
        browser.script_log("未找到 jjc.png")
        return False
    browser.script_log("点击 jjc，等待竞技场")
    return await _wait_img(browser, "jjc_logo", timeout=20, threshold=NAV_THRESHOLD)


async def _raise_jjc_multiplier(browser: UserBrowser) -> None:
    """点刷新提倍率；连续点不到就跳过，避免 stuck 空转。"""
    miss = 0
    for _ in range(8):
        await browser.update_frame()
        if await browser.match_image(_img("jjc_倍率"), threshold=THRESHOLD, quiet=True):
            browser.script_log("倍率已满")
            return
        if await browser.click_image(
            _img("jjc_刷新"), pianyi=JJC_REFRESH_OFFSET, threshold=THRESHOLD
        ):
            miss = 0
            browser.script_log("点击提高倍率")
            await browser.b_sleep(0.35, 0.55)
        else:
            miss += 1
            if miss >= 3:
                browser.script_log("jjc_刷新 连续未命中，跳过提倍率")
                return
            await browser.b_sleep(0.25, 0.4)
    browser.script_log("未能确认满倍率，继续尝试出击")


async def _click_jjc_opponent(browser: UserBrowser) -> bool:
    """点对手列表段位：按 y 分行，优先中间行；过滤左半屏误匹配。"""
    await browser.update_frame()
    matches = await browser.match_image_multi(_img("jjc_段位"), threshold=THRESHOLD)
    if not matches:
        browser.script_log("未找到 jjc_段位.png")
        return False

    fw = 1910
    if browser._frame is not None:
        fw = int(browser._frame.shape[1])
    right = [m for m in matches if float(m["x"]) >= fw * 0.35]
    pool = right or list(matches)
    pool.sort(key=lambda m: float(m["y"]))

    rows: list[dict] = []
    for m in pool:
        if not rows or abs(float(m["y"]) - float(rows[-1]["y"])) > 40:
            rows.append(m)
        elif float(m["x"]) > float(rows[-1]["x"]):
            rows[-1] = m

    if len(rows) >= 3:
        best = rows[len(rows) // 2]
        pick = "中行"
    elif len(rows) == 2:
        best = rows[1]
        pick = "下行"
    else:
        best = rows[0]
        pick = "单行"

    browser.script_log(
        f"点击段位({pick}/{len(rows)}) x={best['x']:.0f}, y={best['y']:.0f}"
    )
    await browser.click(best["x"], best["y"])
    return True


async def _jjc_rankup_visible(browser: UserBrowser) -> bool:
    """段位提升弹窗：标题条「アリーナランクアップ」；排除顶栏误匹配。"""
    if browser._frame is None:
        await browser.update_frame()
    fh = browser._frame.shape[0]
    y_min = fh * JJC_RANKUP_Y_FRAC_MIN
    y_max = fh * JJC_RANKUP_Y_FRAC_MAX
    matches = await browser.match_image_multi(
        _img("jjc_rankup"), threshold=JJC_RANKUP_THRESHOLD
    )
    return any(y_min <= m["y"] <= y_max for m in matches)


async def _jjc_result_visible(browser: UserBrowser) -> bool:
    """战斗 RESULT 结算条（胜负通用）。"""
    return bool(
        await browser.match_image(_img("jjc_结算"), threshold=NAV_THRESHOLD)
    )


async def _jjc_touch_visible(browser: UserBrowser) -> bool:
    """LOSE 等 Touch 退出条。"""
    return bool(
        await browser.match_image(_img("jjc_touch"), threshold=JJC_TOUCH_THRESHOLD)
    )


async def _jjc_ok_visible(browser: UserBrowser) -> bool:
    """结算/段位 OK：默认像素级匹配，保证精确。"""
    for th in (JJC_OK_THRESHOLD, JJC_OK_PIXEL_FALLBACK):
        m = await browser.match_image(
            _img("jjc_ok"),
            threshold=th,
            use_color_check=JJC_OK_USE_COLOR,
            match_mode=JJC_OK_MATCH_MODE,
            pixel_tol=JJC_OK_PIXEL_TOL,
        )
        if m:
            return True
    return False


async def _click_jjc_ok(browser: UserBrowser) -> bool:
    """结算/段位 OK：像素级点击。"""
    for th in (JJC_OK_THRESHOLD, JJC_OK_PIXEL_FALLBACK):
        if await browser.click_image(
            _img("jjc_ok"),
            threshold=th,
            use_color_check=JJC_OK_USE_COLOR,
            match_mode=JJC_OK_MATCH_MODE,
            pixel_tol=JJC_OK_PIXEL_TOL,
        ):
            return True
    return False


async def _click_jjc_rankup_ok_fallback(browser: UserBrowser) -> bool:
    """段位弹窗 OK 在底部居中；识图失败时的兜底点击。"""
    if browser._frame is None:
        await browser.update_frame()
    fh, fw = browser._frame.shape[:2]
    cx = fw // 2
    cy = int(fh * 0.88)
    browser.script_log(f"兜底点击段位弹窗 OK 区域 ({cx},{cy})")
    await browser.click(cx, cy)
    await browser.b_sleep(0.6, 0.9)
    return True


async def _dismiss_jjc_result(browser: UserBrowser) -> bool:
    """随机点击 RESULT 结算区域退出（说明默认路径）。"""
    m = await browser.match_image(_img("jjc_结算"), threshold=NAV_THRESHOLD)
    if not m or not m.match_success:
        return False
    ox = random.randint(40, 120)
    oy = random.randint(30, 90)
    browser.script_log("战斗结算（RESULT），随机点击退出")
    await browser.click(m.x + ox, m.y + oy)
    await browser.b_sleep(0.8, 1.2)
    return True


async def _dismiss_jjc_touch(browser: UserBrowser) -> bool:
    m = await browser.match_image(_img("jjc_touch"), threshold=JJC_TOUCH_THRESHOLD)
    if not m or not m.match_success:
        return False
    ox = random.randint(-30, 30)
    oy = random.randint(-8, 8)
    browser.script_log("结算 Touch 条，点击退出")
    await browser.click(m.x + ox, m.y + oy)
    await browser.b_sleep(0.8, 1.2)
    return True


async def _jjc_at_arena_list(browser: UserBrowser) -> bool:
    logo, chuji, duanwei = await asyncio.gather(
        browser.match_image(_img("jjc_logo"), threshold=NAV_THRESHOLD, quiet=True),
        browser.match_image(_img("jjc_出击"), threshold=THRESHOLD, quiet=True),
        browser.match_image(_img("jjc_段位"), threshold=THRESHOLD, quiet=True),
    )
    return bool(logo) and not bool(chuji) and bool(duanwei)


async def _handle_jjc_result(browser: UserBrowser) -> bool:
    """处理结算：默认 RESULT/Touch 随机点；仅无 RESULT 且有段位提升弹窗时才点 jjc_ok。"""
    timer = StepTimer("jjc_result", STEP_TIMEOUT_BATTLE)
    last_transition_log = 0.0
    rankup_ok_hold = 0.0
    while not timer.expired():
        await browser.update_frame()

        (
            has_result,
            has_touch,
            has_ok,
            has_rankup,
            at_list,
            has_logo,
        ) = await asyncio.gather(
            _jjc_result_visible(browser),
            _jjc_touch_visible(browser),
            _jjc_ok_visible(browser),
            _jjc_rankup_visible(browser),
            _jjc_at_arena_list(browser),
            browser.match_image(_img("jjc_logo"), threshold=NAV_THRESHOLD, quiet=True),
        )
        has_logo = bool(has_logo)

        # ① 默认：RESULT 结算（说明主路径）
        if has_result:
            rankup_ok_hold = 0.0
            await _dismiss_jjc_result(browser)
            continue

        # ② LOSE 等 Touch 条
        if has_touch:
            rankup_ok_hold = 0.0
            await _dismiss_jjc_touch(browser)
            continue

        # ③ 奖励 OK（含段位提升；优先于 rankup 判断，避免顶栏误匹配挡住点击）
        if has_ok:
            rankup_ok_hold = 0.0
            if await _click_jjc_ok(browser):
                tag = "段位提升" if has_rankup else "奖励弹窗"
                browser.script_log(f"点击 jjc_ok（{tag}）")
                await browser.b_sleep(0.5, 0.8)
            else:
                browser.script_log("jjc_ok 已识别但点击失败，下轮重试")
            continue

        # ④ 段位提升：有 title 就主动点 OK，不单等 has_ok 变 True
        if has_rankup and not has_ok:
            if at_list or has_logo:
                browser.script_log(
                    "已在竞技场（列表或 jjc_logo），忽略 rankup 标题"
                )
                return True

            rankup_ok_hold += 1.0

            if await _click_jjc_ok(browser):
                browser.script_log("点击 jjc_ok（段位提升）")
                rankup_ok_hold = 0.0
                await browser.b_sleep(0.5, 0.8)
                continue

            # 每约 2s 试一次底部兜底（灰 OK 模板常匹配不到）
            if rankup_ok_hold >= 2.0 and int(rankup_ok_hold) % 2 == 0:
                await _click_jjc_rankup_ok_fallback(browser)
                if await _jjc_at_arena_list(browser):
                    browser.script_log("兜底点击后已回竞技场列表")
                    return True
                continue

            if rankup_ok_hold < RANKUP_OK_MAX_WAIT_SEC:
                if last_transition_log == 0.0 or time.monotonic() - last_transition_log >= 8.0:
                    browser.script_log("段位提升弹窗，尝试点击 jjc_ok…")
                    last_transition_log = time.monotonic()
                await browser.b_sleep(0.8, 1.2)
                continue

            browser.script_log(
                f"  {RANKUP_OK_MAX_WAIT_SEC:.0f}s 内未能关闭段位弹窗，继续等待回列表"
            )
            rankup_ok_hold = 0.0

        if has_logo:
            browser.script_log("已回到竞技场（jjc_logo）")
            return True

        # ⑤ 回到竞技场列表
        if at_list:
            browser.script_log("已回到竞技场列表")
            return True
        # 结算等待：不用全量场景探针，有 logo/列表即可
        await browser.b_sleep(0.8, 1.2)
    browser.script_log("等待结算回竞技场超时")
    return False


async def _wait_jjc_fight_end(browser: UserBrowser) -> bool:
    """出击后等待战斗结束。

    不能一看到 jjc_logo 就当结束——出击后短时间内 logo 仍可能在，
    会误判并进入结算空转（反复 click 失败的 jjc_ok）。
    """
    browser.script_log(
        f"已出击，长时间等待战斗/加载（最多 {STEP_TIMEOUT_JJC_FIGHT:.0f}s）…"
    )

    async with _passive_observe(browser):
        # ① 确认离开编队：只盯 jjc_出击 / 结算
        leave = StepTimer("leave_prep", 60.0)
        left_prep = False
        leave_log = 0.0
        while not leave.expired():
            await browser.update_frame()
            await _handle_sortie_limit_popup(browser)
            has_result, has_touch, has_ok = await asyncio.gather(
                _jjc_result_visible(browser),
                _jjc_touch_visible(browser),
                _jjc_ok_visible(browser),
            )
            if has_result or has_touch or has_ok:
                left_prep = True
                browser.script_log("已离开编队界面（出现结算）…")
                break
            if not await browser.match_image(_img("jjc_出击"), threshold=THRESHOLD):
                left_prep = True
                browser.script_log("已离开编队界面，进入战斗/加载…")
                break
            now = time.monotonic()
            if now - leave_log >= 10.0:
                browser.script_log(
                    f"  等待离开编队… 剩余约 {leave.remaining():.0f}s"
                )
                leave_log = now
            await browser.b_sleep(0.8, 1.2)
        if not left_prep:
            browser.script_log("出击后仍停在编队界面（jjc_出击 未消失）")
            return False

        # ② 最短战斗时间；轮询放稀，少做像素 OK / 列表探测
        min_fight = 15.0
        fight_start = time.monotonic()
        timer = StepTimer("jjc_fight", STEP_TIMEOUT_JJC_FIGHT)
        last_log = time.monotonic()
        loop_i = 0
        while not timer.expired():
            await browser.update_frame()
            loop_i += 1
            elapsed = time.monotonic() - fight_start

            has_result, has_touch = await asyncio.gather(
                _jjc_result_visible(browser),
                _jjc_touch_visible(browser),
            )
            if has_result:
                browser.script_log("战斗结束：出现 jjc_结算")
                return True
            if has_touch:
                browser.script_log("战斗结束：出现 Touch 结算")
                return True

            # 像素 OK 较贵：隔轮或过最短战后才查
            if loop_i % 2 == 0 or elapsed >= min_fight:
                if await _jjc_ok_visible(browser):
                    browser.script_log("战斗结束：出现 jjc_ok")
                    return True

            # 列表探测更贵：最短战后每 3 轮一次
            if elapsed >= min_fight and loop_i % 3 == 0:
                if await _jjc_at_arena_list(browser):
                    browser.script_log("战斗结束：已回到竞技场列表")
                    return True

            timer.pause_for_transition()
            now = time.monotonic()
            if now - last_log >= 30:
                browser.script_log(f"  仍在战斗/加载中… 已等 {elapsed:.0f}s")
                last_log = now
            await browser.b_sleep(*JJC_FIGHT_POLL_SLEEP)

    browser.script_log("长时间等待战斗结束超时")
    return False


async def _jjc_one_battle(browser: UserBrowser) -> bool:
    """在已处于竞技场的前提下打一轮；耗尽则也算成功。"""
    await browser.update_frame()
    if await browser.match_image(_img("jjc_end"), threshold=JJC_END_THRESHOLD):
        browser.script_log("jjc_end：次数已耗尽")
        return True

    await _raise_jjc_multiplier(browser)
    if not await _click_jjc_opponent(browser):
        return False
    # 点错时尽快失败回退，不要干等 20s / 过场续时
    if not await _wait_img(
        browser,
        "jjc_出击",
        timeout=7.0,
        threshold=THRESHOLD,
        extend_on_transition=False,
    ):
        browser.script_log("未进入编队界面")
        return False
    if not await browser.click_image(_img("jjc_出击"), threshold=THRESHOLD):
        browser.script_log("点击 jjc_出击 失败")
        return False
    await _after_sortie_click(browser)

    if not await _wait_jjc_fight_end(browser):
        return False
    return await _handle_jjc_result(browser)


async def 竞技场(browser: UserBrowser) -> bool:
    # 先确保进入竞技场（失败可回退出击）
    await run_step_chain(
        browser,
        [
            ("回出击界面", _jjc_go_sortie, STEP_TIMEOUT_NAV),
            ("进入竞技场", _jjc_enter, STEP_TIMEOUT),
        ],
        step_timeout=STEP_TIMEOUT,
        label="竞技场",
    )

    for round_i in range(1, MAX_ARENA_ROUNDS + 1):
        await browser.update_frame()
        if await browser.match_image(_img("jjc_end"), threshold=JJC_END_THRESHOLD):
            browser.script_log("jjc_end：竞技场完成")
            return True

        browser.script_log(f"[竞技场] 第 {round_i} 轮")
        # 打一轮含战斗动画/加载，单独给更长超时；失败回退「进入竞技场」
        await run_step_chain(
            browser,
            [
                ("进入竞技场", _jjc_enter, STEP_TIMEOUT),
                (
                    "打一轮",
                    _jjc_one_battle,
                    STEP_TIMEOUT_JJC_FIGHT + STEP_TIMEOUT_BATTLE,
                ),
            ],
            step_timeout=STEP_TIMEOUT,
            label="竞技场轮次",
        )

    browser.script_log(f"竞技场超过 {MAX_ARENA_ROUNDS} 轮，停止")
    return False


# ── 爬塔 ───────────────────────────────────────────────────


async def _tower_times_exhausted(browser: UserBrowser) -> bool:
    """出现 ta_cishu → 次数耗尽，爬塔直接结束。"""
    m = await browser.match_image(_img("ta_cishu"), threshold=TA_CISHU_THRESHOLD)
    if not m:
        return False
    browser.script_log(f"ta_cishu：次数耗尽 score={getattr(m, 'max_val', m)}")
    return True


async def _tower_go_sortie(browser: UserBrowser) -> bool:
    return await 返回出击界面(browser)


async def _tower_enter_select(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await browser.match_image(_img("ta_logo"), threshold=NAV_THRESHOLD, quiet=True):
        browser.script_log("已在塔界面")
        return True
    if not await browser.click_image(_img("ta"), threshold=THRESHOLD):
        browser.script_log("未找到 ta.png")
        return False
    browser.script_log("点击 ta，等待塔界面")
    # 短轮询，避免固定 2s×3 空等
    if await _wait_img(
        browser,
        "ta_logo",
        timeout=TA_ENTER_TIMEOUT,
        threshold=NAV_THRESHOLD,
        extend_on_transition=False,
    ):
        browser.script_log("已进入塔界面")
        return True
    browser.script_log(f"{TA_ENTER_TIMEOUT:.0f}s 内未见 ta_logo")
    return False


async def _switch_tower_difficulty(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await _tower_times_exhausted(browser):
        browser.script_log("调整难度前次数已耗尽，跳过切难度")
        return False

    browser.script_log("开始调整塔难度…")
    for i in range(12):
        await browser.update_frame()
        if await _tower_times_exhausted(browser):
            browser.script_log("切难度过程中次数已耗尽")
            return False
        if await browser.match_image(_img("ta_nandu"), threshold=THRESHOLD, quiet=True):
            browser.script_log("已看到目标难度 ta_nandu")
            break
        ox = random.randint(0, 30)
        oy = random.randint(-150, -120)
        if await browser.click_image(
            _img("ta_biancheng"), pianyi=(ox, oy), threshold=THRESHOLD
        ):
            browser.script_log(f"切换难度 #{i + 1} offset=({ox},{oy})")
            await browser.b_sleep(0.12, 0.28)
        else:
            browser.script_log("未找到 ta_biancheng，稍后重试")
            await browser.b_sleep(0.3, 0.5)
    else:
        browser.script_log("未能切到目标难度 ta_nandu")
        return False

    if await _tower_times_exhausted(browser):
        browser.script_log("点难度前次数已耗尽")
        return False

    if not await browser.click_image(_img("ta_nandu"), threshold=THRESHOLD):
        browser.script_log("点击 ta_nandu 失败")
        return False
    browser.script_log("已点目标难度，等待确认框")

    if await _wait_img(
        browser,
        "ta_tiaozhan",
        timeout=5.0,
        threshold=THRESHOLD,
        extend_on_transition=False,
    ):
        if await browser.click_image(_img("ta_tiaozhan"), threshold=THRESHOLD):
            browser.script_log("点击挑战确认")
            await browser.b_sleep(0.45, 0.7)
    else:
        browser.script_log("未出现 ta_tiaozhan，可能已直接进塔")

    if await _wait_img(
        browser,
        "ta_chuji",
        timeout=12.0,
        threshold=THRESHOLD,
        extend_on_transition=False,
    ):
        browser.script_log("已进入塔内关卡")
        return True
    if await _tower_times_exhausted(browser):
        browser.script_log("进入塔内超时，且次数已耗尽")
    else:
        browser.script_log("进入塔内超时（未见 ta_chuji）")
    return False


async def _match_tower_auto(
    browser: UserBrowser,
    stem: str,
    *,
    threshold: float = TA_AUTO_THRESHOLD,
) -> bool:
    m = await browser.match_image(
        _img(stem),
        threshold=threshold,
        use_color_check=TA_AUTO_USE_COLOR,
    )
    return bool(m)


async def _click_tower_auto(
    browser: UserBrowser,
    stem: str,
    *,
    threshold: float = TA_AUTO_THRESHOLD,
) -> bool:
    return await browser.click_image(
        _img(stem),
        threshold=threshold,
        use_color_check=TA_AUTO_USE_COLOR,
    )


async def _find_tower_sortie_btn(browser: UserBrowser):
    """塔出击界面右下「出撃」按钮（多模板 + 较低阈值）。"""
    await browser.update_frame()
    for stem in ("ta_chuji_chuji2", "ta_chuji_chuji"):
        m = await browser.match_image(_img(stem), threshold=TA_SORTIE_BTN_THRESHOLD)
        if m and m.match_success:
            return m
    return None


async def _tower_auto_is_on(browser: UserBrowser) -> bool:
    """AUTO 已开启（亮蓝态模板 + 颜色校验）。"""
    if await _match_tower_auto(browser, "ta_auto2"):
        return True
    return await _match_tower_auto(browser, "ta_chuji_auto")


async def _tower_auto_toggled_off_to_on(browser: UserBrowser, was_off: bool) -> bool:
    """点击后确认 AUTO 已切换。"""
    if await _tower_auto_is_on(browser):
        return True
    if was_off and not await _match_tower_auto(browser, "ta_auto1"):
        browser.script_log("灰色 AUTO 已消失，视为切换成功")
        return True
    return False


async def _ensure_tower_auto_on(browser: UserBrowser) -> bool:
    """确保塔出击界面 AUTO 已打开（先模板，再相对出撃偏移点击）。"""
    await browser.update_frame()
    if await _tower_auto_is_on(browser):
        browser.script_log("塔 AUTO 已开启")
        return True

    for attempt in range(1, 4):
        await browser.update_frame()
        if await _tower_auto_is_on(browser):
            browser.script_log("塔 AUTO 已开启")
            return True

        was_off = await _match_tower_auto(browser, "ta_auto1")
        clicked = False
        for stem in ("ta_auto1", "ta_chuji_auto"):
            if await _click_tower_auto(browser, stem):
                browser.script_log(f"点击 {stem} 打开 AUTO（{attempt}/3）")
                clicked = True
                break

        if not clicked:
            sortie = await _find_tower_sortie_btn(browser)
            if sortie:
                ox, oy = TOWER_AUTO_OFFSET_FROM_SORTIE
                ax, ay = sortie.x + ox, sortie.y + oy
                browser.script_log(
                    f"未匹配 AUTO 模板，按出撃相对位置点击 ({ax:.0f},{ay:.0f})"
                    f"（{attempt}/3）"
                )
                await browser.click(ax, ay)
                clicked = True
            else:
                browser.script_log(f"未找到 AUTO / 出撃 按钮（{attempt}/3）")

        if clicked:
            await browser.b_sleep(*TA_AUTO_TOGGLE_DELAY)
            if await _tower_auto_toggled_off_to_on(browser, was_off):
                browser.script_log("塔 AUTO 已成功打开")
                return True
            await browser.b_sleep(*TA_ENTER_POLL)

    browser.script_log("多次尝试仍未能打开塔 AUTO")
    return False


async def _click_tower_sortie(browser: UserBrowser) -> bool:
    """AUTO 就绪后稍等 GUI 稳定，再点出撃（带重试）。"""
    await browser.b_sleep(*TA_AUTO_SETTLE_DELAY)
    for attempt in range(1, TA_SORTIE_CLICK_RETRIES + 1):
        await browser.update_frame()
        clicked = False
        sortie = await _find_tower_sortie_btn(browser)
        if sortie:
            browser.script_log(
                f"点击塔出撃 ({sortie.x:.0f},{sortie.y:.0f})"
                f"（{attempt}/{TA_SORTIE_CLICK_RETRIES}）"
            )
            await browser.click(sortie.x, sortie.y)
            clicked = True
        elif await browser.click_image(
            _img("ta_chuji_chuji"), threshold=TA_SORTIE_BTN_THRESHOLD
        ):
            browser.script_log(
                f"点击 ta_chuji_chuji（{attempt}/{TA_SORTIE_CLICK_RETRIES}）"
            )
            clicked = True
        elif await browser.click_image(
            _img("ta_chuji_chuji2"), threshold=TA_SORTIE_BTN_THRESHOLD
        ):
            browser.script_log(
                f"点击 ta_chuji_chuji2（{attempt}/{TA_SORTIE_CLICK_RETRIES}）"
            )
            clicked = True

        if not clicked:
            browser.script_log(
                f"未找到塔出撃按钮（{attempt}/{TA_SORTIE_CLICK_RETRIES}）"
            )
            await browser.b_sleep(*TA_ENTER_POLL)
            continue

        await _after_sortie_click(browser)

        # 短确认：横幅出现或出撃按钮消失
        await browser.b_sleep(*TA_SORTIE_CONFIRM_SLEEP)
        await browser.update_frame()
        if await _tower_auto_banner_visible(browser):
            browser.script_log("出撃已生效（AUTO进行中）")
            return True
        if not await _find_tower_sortie_btn(browser):
            browser.script_log("出撃已生效（离开出击准备界面）")
            return True
        browser.script_log("出撃可能未响应，稍后重试…")
        await browser.b_sleep(*TA_ENTER_POLL)
    return False


async def _tower_auto_banner_visible(browser: UserBrowser) -> bool:
    return bool(
        await browser.match_image(
            _img("ta_auto_banner"), threshold=TA_AUTO_BANNER_THRESHOLD
        )
    )


async def _tower_reward_popup_visible(browser: UserBrowser) -> bool:
    jiangli, ok = await asyncio.gather(
        browser.match_image(_img("ta_jiangli"), threshold=NAV_THRESHOLD, quiet=True),
        browser.match_image(_img("ta_ok"), threshold=THRESHOLD, quiet=True),
    )
    return bool(jiangli) or bool(ok)


async def _wait_tower_auto_finish(browser: UserBrowser) -> bool:
    """等塔 AUTO 全程结束。

    - AUTO进行中 / 横幅近期出现过 → 绝不点击
    - 中间关奖励框 auto 约 4s 内会自己关，hold 不够不会误点
    - 最后一关 auto 结束、横幅不再出现且奖励框稳定 → 才点 ta_ok
    - 被动等待停后台截图、拉长轮询，只盯横幅/奖励
    """
    last_banner_at = 0.0
    reward_hold = 0.0
    last_log = 0.0
    timer = StepTimer("tower_auto_run", STEP_TIMEOUT_JJC_FIGHT)
    browser.script_log(
        f"塔 AUTO 运行中，被动等待（最多 {STEP_TIMEOUT_JJC_FIGHT:.0f}s，"
        f"横幅近期 {TA_AUTO_BANNER_RECENT_SEC:.0f}s 内不点奖励）"
    )

    async with _passive_observe(browser):
        while not timer.expired():
            await browser.update_frame()
            now = time.monotonic()

            if await _tower_auto_banner_visible(browser):
                last_banner_at = now
                reward_hold = 0.0
                timer.pause_for_transition()
                if now - last_log >= 30.0:
                    browser.script_log("  塔 AUTO进行中（横幅可见）")
                    last_log = now
                await browser.b_sleep(*TOWER_AUTO_BANNER_SLEEP)
                continue

            banner_recent = (
                last_banner_at > 0.0
                and (now - last_banner_at) < TA_AUTO_BANNER_RECENT_SEC
            )
            has_reward = await _tower_reward_popup_visible(browser)

            if banner_recent:
                reward_hold = 0.0
                timer.pause_for_transition()
                if now - last_log >= 30.0:
                    browser.script_log("  AUTO可能仍在进行（横幅刚消失），不点奖励")
                    last_log = now
                await browser.b_sleep(*TOWER_AUTO_BANNER_SLEEP)
                continue

            if not has_reward:
                reward_hold = 0.0
                timer.pause_for_transition()
                if now - last_log >= 35.0:
                    browser.script_log("  塔 AUTO 运行中（等待横幅/奖励）…")
                    last_log = now
                await browser.b_sleep(*TOWER_AUTO_IDLE_SLEEP)
                continue

            reward_hold += 1.5
            quiet_after_banner = (
                last_banner_at == 0.0
                or (now - last_banner_at) >= TA_FINAL_REWARD_QUIET_SEC
            )
            if (
                quiet_after_banner
                and reward_hold >= TA_FINAL_REWARD_HOLD_SEC
                and await browser.match_image(_img("ta_ok"), threshold=THRESHOLD)
            ):
                browser.script_log("AUTO 已结束，最终奖励框稳定，点击 OK")
                await browser.click_image(_img("ta_ok"), threshold=THRESHOLD)
                await browser.b_sleep(0.8, 1.2)
                return True

            if reward_hold >= 2.0 and now - last_log >= 20.0:
                browser.script_log("  中间关奖励框出现，等待 auto 自行关闭…")
                last_log = now
            await browser.b_sleep(1.2, 1.8)

    browser.script_log("等待塔 AUTO 完成超时")
    return False


async def _run_tower_auto(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if not await browser.click_image(_img("ta_chuji"), threshold=THRESHOLD):
        browser.script_log("未找到 ta_chuji.png")
        return False
    browser.script_log("点击塔内出击，等待出击界面")

    entered = False
    # 轻量轮询 AUTO/出撃按钮，不做全量场景探针续时
    timer = StepTimer("tower_sortie_ui", 18.0)
    while not timer.expired():
        await browser.update_frame()
        if (
            await _match_tower_auto(browser, "ta_auto1")
            or await _match_tower_auto(browser, "ta_auto2")
            or await _match_tower_auto(browser, "ta_chuji_auto")
            or await _find_tower_sortie_btn(browser)
        ):
            entered = True
            break
        await browser.b_sleep(*TA_ENTER_POLL)
    if not entered:
        browser.script_log("未进入塔出击界面")
        return False

    if not await _ensure_tower_auto_on(browser):
        return False

    if not await _click_tower_sortie(browser):
        browser.script_log("塔出撃点击失败")
        return False
    browser.script_log("已点击出撃，交由游戏 AUTO 运行")

    return await _wait_tower_auto_finish(browser)


async def _tower_one_round(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await _tower_times_exhausted(browser):
        browser.script_log("本轮开始前次数已耗尽")
        return True

    in_stage, on_nandu, on_biancheng = await asyncio.gather(
        browser.match_image(_img("ta_chuji"), threshold=THRESHOLD, quiet=True),
        browser.match_image(_img("ta_nandu"), threshold=THRESHOLD, quiet=True),
        browser.match_image(_img("ta_biancheng"), threshold=THRESHOLD, quiet=True),
    )

    if in_stage and not on_biancheng and not on_nandu:
        browser.script_log("已在塔内，直接出击/auto")
    else:
        if not await _switch_tower_difficulty(browser):
            await browser.update_frame()
            if await _tower_times_exhausted(browser):
                browser.script_log("切难度失败但次数已耗尽，视为完成")
                return True
            return False

    if await _tower_times_exhausted(browser):
        browser.script_log("出击前次数已耗尽")
        return True

    if not await _run_tower_auto(browser):
        await browser.update_frame()
        if await _tower_times_exhausted(browser):
            browser.script_log("出击流程失败但次数已耗尽，视为完成")
            return True
        return False

    await browser.b_sleep(0.8, 1.2)
    await browser.update_frame()
    return True


async def 爬塔(browser: UserBrowser) -> bool:
    await run_step_chain(
        browser,
        [
            ("回出击界面", _tower_go_sortie, STEP_TIMEOUT_NAV),
            ("进入选塔", _tower_enter_select, STEP_TIMEOUT),
        ],
        step_timeout=STEP_TIMEOUT,
        label="爬塔",
    )

    for round_i in range(1, MAX_TOWER_ROUNDS + 1):
        await browser.update_frame()
        if await _tower_times_exhausted(browser):
            browser.script_log("ta_cishu：爬塔完成")
            return True

        browser.script_log(f"[爬塔] 第 {round_i} 轮")
        await run_step_chain(
            browser,
            [
                ("进入选塔", _tower_enter_select, STEP_TIMEOUT),
                (
                    "打一轮塔",
                    _tower_one_round,
                    STEP_TIMEOUT_JJC_FIGHT + STEP_TIMEOUT_BATTLE,
                ),
            ],
            step_timeout=STEP_TIMEOUT,
            label="爬塔轮次",
        )

        await browser.update_frame()
        if await _tower_times_exhausted(browser):
            browser.script_log("ta_cishu：爬塔完成")
            return True

    browser.script_log(f"爬塔超过 {MAX_TOWER_ROUNDS} 轮，停止")
    return False


# ── 每日关卡 ───────────────────────────────────────────────


async def _meiri_go_sortie(browser: UserBrowser) -> bool:
    return await 返回出击界面(browser)


async def _meiri_enter(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await browser.match_image(_img("meiri_logo"), threshold=NAV_THRESHOLD):
        browser.script_log("已在每日关卡界面")
        return True
    for attempt in range(1, 4):
        await browser.update_frame()
        if await browser.match_image(_img("meiri_logo"), threshold=NAV_THRESHOLD):
            browser.script_log("已进入每日关卡")
            return True
        if not await browser.click_image(_img("meiri"), threshold=THRESHOLD):
            browser.script_log(f"未找到 meiri.png（{attempt}/3）")
            await browser.b_sleep(1.0, 1.5)
            continue
        browser.script_log(f"点击 meiri（{attempt}/3），等待 meiri_logo…")
        await browser.b_sleep(2.0, 2.0)
    await browser.update_frame()
    if await browser.match_image(_img("meiri_logo"), threshold=NAV_THRESHOLD):
        browser.script_log("已进入每日关卡")
        return True
    browser.script_log("三次进入每日关卡失败")
    return False


async def _meiri_sweep(browser: UserBrowser) -> bool:
    """开扫荡窗 → 循环确认扫荡 → 点 OK 直到消失。

    见 meiri_skip_attked 时提前进入 OK 阶段。
    失败提示（无可挑战等）仍带一括スキップ标题、只有 OK 无确认键 → 直接点 OK。
    """
    timer = StepTimer("meiri_sweep", MEIRI_SWEEP_TIMEOUT)

    # 1) 点 meiri_skip 直到出现扫荡标题
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("meiri_skip_title"), threshold=NAV_THRESHOLD):
            browser.script_log("已打开扫荡窗（meiri_skip_title）")
            break
        if await browser.click_image(_img("meiri_skip"), threshold=THRESHOLD):
            browser.script_log("点击 meiri_skip，等待扫荡窗…")
            await browser.b_sleep(0.6, 1.0)
        else:
            await browser.b_sleep(0.8, 1.2)
    else:
        browser.script_log("未能打开扫荡窗（未见 meiri_skip_title）")
        return False

    # 2) 标题存在时循环点确认；attked / 标题消失 / 仅剩 OK → 进 OK
    next_click_at = 0.0
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("meiri_skip_attked"), threshold=NAV_THRESHOLD):
            browser.script_log("出现 meiri_skip_attked，进入点 OK")
            break
        if not await browser.match_image(_img("meiri_skip_title"), threshold=NAV_THRESHOLD):
            browser.script_log("扫荡标题消失，进入点 OK")
            break

        has_skip, has_ok = await asyncio.gather(
            browser.match_image(
                _img("meiri_skip_skip"), threshold=THRESHOLD, quiet=True
            ),
            browser.match_image(_img("meiri_ok"), threshold=THRESHOLD, quiet=True),
        )
        # 「无可挑战」等：标题还在，确认键没了，只剩 OK
        if has_ok and not has_skip:
            browser.script_log("扫荡窗仅剩 OK（可能无可扫荡），进入点 OK")
            break

        now = time.monotonic()
        if now >= next_click_at:
            if has_skip and await browser.click_image(
                _img("meiri_skip_skip"), threshold=THRESHOLD
            ):
                browser.script_log("点击扫荡确认 meiri_skip_skip")
            lo, hi = MEIRI_SKIP_CONFIRM_INTERVAL
            next_click_at = now + random.uniform(lo, hi)
        await browser.b_sleep(0.4, 0.7)
    else:
        browser.script_log("扫荡确认阶段超时")
        return False

    # 3) 点 meiri_ok 直到消失
    saw_ok = False
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("meiri_ok"), threshold=THRESHOLD):
            saw_ok = True
            if await browser.click_image(_img("meiri_ok"), threshold=THRESHOLD):
                browser.script_log("点击 meiri_ok")
            await browser.b_sleep(0.5, 0.9)
            continue

        if saw_ok:
            browser.script_log("meiri_ok 已消失，每日关卡完成")
            return True

        if await browser.match_image(_img("meiri_logo"), threshold=NAV_THRESHOLD):
            if not await browser.match_image(
                _img("meiri_skip_title"), threshold=NAV_THRESHOLD
            ):
                browser.script_log("已回每日列表，视为完成")
                return True

        await browser.b_sleep(0.5, 0.8)

    browser.script_log("每日关卡扫荡超时")
    return False


async def 每日关卡(browser: UserBrowser) -> bool:
    """出击 → 每日关卡 → 快速扫荡 → OK 关闭。"""
    return await run_step_chain(
        browser,
        [
            ("回出击界面", _meiri_go_sortie, STEP_TIMEOUT_NAV),
            ("进入每日关卡", _meiri_enter, STEP_TIMEOUT),
            ("扫荡", _meiri_sweep, MEIRI_SWEEP_TIMEOUT),
        ],
        step_timeout=STEP_TIMEOUT,
        label="每日关卡",
    )


# ── 领取礼物 ───────────────────────────────────────────────


async def 领取礼物(browser: UserBrowser) -> bool:
    """打开菜单进礼物 → 一键领取 → 点 OK 直到消失。"""
    await browser.update_frame()
    on_gift = await browser.match_image(_img("gift_logo"), threshold=NAV_THRESHOLD)

    if not on_gift:
        if not await 打开菜单(browser):
            return False
        timer = StepTimer("enter_gift", 30.0)
        while not timer.expired():
            await browser.update_frame()
            if await browser.match_image(_img("gift_logo"), threshold=NAV_THRESHOLD):
                browser.script_log("已进入礼物界面")
                on_gift = True
                break
            if await browser.click_image(_img("menu_gift"), threshold=THRESHOLD):
                browser.script_log("点击 menu_gift…")
                await browser.b_sleep(0.6, 1.0)
                continue
            # menu_gift 已消失：等 gift_logo
            browser.script_log("menu_gift 已消失，等待 gift_logo…")
            if await _wait_img(browser, "gift_logo", timeout=8, threshold=NAV_THRESHOLD):
                on_gift = True
            break
        if not on_gift:
            await browser.update_frame()
            on_gift = await browser.match_image(
                _img("gift_logo"), threshold=NAV_THRESHOLD
            )
        if not on_gift:
            browser.script_log("未能进入礼物界面")
            return False

    # 一键领取
    await browser.update_frame()
    if await browser.click_image(_img("gift_claim"), threshold=THRESHOLD):
        browser.script_log("点击 gift_claim")
        await browser.b_sleep(0.8, 1.2)
    else:
        browser.script_log("无 gift_claim，视为已领完")
        return True

    # 点 gift_ok 直到消失
    timer = StepTimer("gift_ok", GIFT_TIMEOUT)
    saw_ok = False
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("gift_ok"), threshold=THRESHOLD):
            saw_ok = True
            if await browser.click_image(_img("gift_ok"), threshold=THRESHOLD):
                browser.script_log("点击 gift_ok")
            await browser.b_sleep(0.5, 0.9)
            continue
        if saw_ok:
            browser.script_log("gift_ok 已消失，礼物领取完成")
            return True
        await browser.b_sleep(0.4, 0.7)

    if not saw_ok:
        browser.script_log("未见 gift_ok，视为领取完成")
        return True
    browser.script_log("等待 gift_ok 消失超时")
    return False


# ── 任务奖励领取 ───────────────────────────────────────────


async def _task_end_visible(browser: UserBrowser) -> bool:
    return bool(
        await browser.match_image(
            _img("task_end"),
            threshold=THRESHOLD,
            use_color_check=TASK_CLAIM_USE_COLOR,
            quiet=True,
        )
    )


async def _dismiss_task_ok(browser: UserBrowser) -> None:
    """点掉领取后的 OK 弹窗（没有也不阻塞太久）。"""
    timer = StepTimer("task_ok", 25.0)
    saw_ok = False
    wait_start = time.monotonic()
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("task_ok"), threshold=THRESHOLD):
            saw_ok = True
            if await browser.click_image(_img("task_ok"), threshold=THRESHOLD):
                browser.script_log("点击 task_ok")
            await browser.b_sleep(0.5, 0.9)
            continue
        if saw_ok:
            browser.script_log("task_ok 已消失")
            return
        # 尚未出现：稍等，可能本轮无弹窗
        if time.monotonic() - wait_start >= 3.0:
            return
        await browser.b_sleep(0.35, 0.55)


async def 任务奖励领取(browser: UserBrowser) -> bool:
    """回主界面进任务页 → 循环一键领取直到 task_end。"""
    await browser.update_frame()
    on_task = await browser.match_image(
        _img("task_logo"), threshold=NAV_THRESHOLD, quiet=True
    )

    if not on_task:
        if not await 返回主界面(browser):
            return False
        timer = StepTimer("enter_task", 30.0)
        while not timer.expired():
            await browser.update_frame()
            if await browser.match_image(
                _img("task_logo"), threshold=NAV_THRESHOLD, quiet=True
            ):
                browser.script_log("已进入任务页面")
                on_task = True
                break
            if await browser.click_image(_img("task"), threshold=THRESHOLD):
                browser.script_log("点击 task…")
                await browser.b_sleep(0.6, 1.0)
                continue
            browser.script_log("task 已消失，等待 task_logo…")
            if await _wait_img(
                browser, "task_logo", timeout=8, threshold=NAV_THRESHOLD
            ):
                on_task = True
            break
        if not on_task:
            await browser.update_frame()
            on_task = await browser.match_image(
                _img("task_logo"), threshold=NAV_THRESHOLD, quiet=True
            )
        if not on_task:
            browser.script_log("未能进入任务页面")
            return False

    claim_rounds = 0
    timer = StepTimer("task_reward", TASK_REWARD_TIMEOUT)
    while not timer.expired():
        await browser.update_frame()
        if await _task_end_visible(browser):
            browser.script_log(
                f"task_end：任务奖励已领完"
                + (f"（共领取 {claim_rounds} 轮）" if claim_rounds else "")
            )
            return True

        if await browser.click_image(
            _img("task_claim"),
            threshold=THRESHOLD,
            use_color_check=TASK_CLAIM_USE_COLOR,
        ):
            claim_rounds += 1
            browser.script_log(f"点击 task_claim（第 {claim_rounds} 轮）")
            await browser.b_sleep(0.8, 1.2)
            await _dismiss_task_ok(browser)
            continue

        # 未见亮态 claim：再确认一次 end，避免动画空窗误判
        await browser.b_sleep(0.45, 0.7)
        await browser.update_frame()
        if await _task_end_visible(browser):
            browser.script_log(
                f"task_end：任务奖励已领完"
                + (f"（共领取 {claim_rounds} 轮）" if claim_rounds else "")
            )
            return True
        if claim_rounds == 0:
            browser.script_log("无 task_claim，视为已领完")
            return True
        browser.script_log(
            f"task_claim 已消失（已领 {claim_rounds} 轮），视为完成"
        )
        return True

    browser.script_log(
        f"任务奖励领取超时（已领 {claim_rounds} 轮，限时 {TASK_REWARD_TIMEOUT:.0f}s）"
    )
    return False


# ── 入口 ───────────────────────────────────────────────────


async def do_work(browser: UserBrowser):
    """日常入口：房间 → 竞技场 → 爬塔 → 每日关卡 → 领取礼物 → 任务奖励。"""
    browser.use_polling_temp_cache = True
    status = "ok"
    if DEBUG_PSEUDO_RECORD or os.getenv("MINASHIGO_PSEUDO_RECORD"):
        browser.enable_pseudo_record(script_name="DO日常", force=True)
    try:
        await _prepare_game_matching(browser)
        await run_step_chain(
            browser,
            [
                ("房间领体力", 房间领取奖励, STEP_TIMEOUT_NAV + STEP_TIMEOUT * 2),
                (
                    "竞技场",
                    竞技场,
                    (STEP_TIMEOUT_JJC_FIGHT + STEP_TIMEOUT_BATTLE) * MAX_ARENA_ROUNDS,
                ),
                (
                    "爬塔",
                    爬塔,
                    (STEP_TIMEOUT_JJC_FIGHT + STEP_TIMEOUT_BATTLE) * MAX_TOWER_ROUNDS,
                ),
                (
                    "每日关卡",
                    每日关卡,
                    STEP_TIMEOUT_NAV + STEP_TIMEOUT + MEIRI_SWEEP_TIMEOUT,
                ),
                (
                    "领取礼物",
                    领取礼物,
                    STEP_TIMEOUT + GIFT_TIMEOUT,
                ),
                (
                    "任务奖励领取",
                    任务奖励领取,
                    STEP_TIMEOUT_NAV + TASK_REWARD_TIMEOUT,
                ),
            ],
            step_timeout=STEP_TIMEOUT,
            label="日常",
        )
        browser.script_log("===== 日常全部完成 =====")
    except DailyStepError as e:
        status = "error"
        browser.script_log(f"日常中断: {e}")
        raise
    except Exception:
        status = "error"
        raise
    finally:
        browser.use_game_frame_capture = False
        browser.finish_pseudo_record(status=status)
