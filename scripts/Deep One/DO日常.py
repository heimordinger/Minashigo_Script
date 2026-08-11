"""DeepOne 日常（手写草稿）：房间领体力 / 竞技场 / 爬塔。"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

SCRIPT_PATH = IMG_PATH / "DeepOne" / "DO日常"
THRESHOLD = 0.9
NAV_THRESHOLD = 0.85
JJC_END_THRESHOLD = 0.99
# 次数耗尽图易误匹配，阈值要高
TA_CISHU_THRESHOLD = 0.99
MAX_ARENA_ROUNDS = 20
MAX_TOWER_ROUNDS = 20
# 单步最长停留；超时则回退上一步
STEP_TIMEOUT = 45.0
# 回主界面 / 回出击：含多次点击与 loading
STEP_TIMEOUT_NAV = 90.0
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
    while i < len(steps):
        name, fn, timeout = _unpack_step(steps[i], step_timeout)
        browser.script_log(
            f"{tag}步骤 {i + 1}/{len(steps)}「{name}」（限时 {timeout:.0f}s）"
        )
        try:
            ok = await asyncio.wait_for(fn(browser), timeout=timeout)
        except asyncio.TimeoutError:
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
        except asyncio.TimeoutError:
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


class StepTimer:
    """步骤内轮询用计时器。"""

    def __init__(self, name: str, timeout: float):
        self.name = name
        self.deadline = time.monotonic() + timeout

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


async def _wait_img(
    browser: UserBrowser,
    name: str,
    *,
    timeout: float,
    threshold: float = THRESHOLD,
) -> bool:
    """带阈值的等待出现（不依赖 UserBrowser.wait_image 的 threshold 参数）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await browser.update_frame()
        if await browser.match_image(_img(name), threshold=threshold):
            return True
        await browser.b_sleep(0.45, 0.6)
    return False


# ── 导航辅助 ───────────────────────────────────────────────


async def 返回主界面(browser: UserBrowser) -> bool:
    """点 home 回主界面；加载较慢时会多次点击并按 nav 阈值等待。"""
    for attempt in range(1, 4):
        await browser.update_frame()
        if await browser.match_image(_img("rank"), threshold=NAV_THRESHOLD):
            browser.script_log("已在主界面")
            return True

        clicked = await browser.click_image(_img("home"), threshold=THRESHOLD)
        if clicked:
            browser.script_log(f"点击 home（第 {attempt}/3 次），等待主界面…")
        else:
            browser.script_log(f"未找到 home.png（第 {attempt}/3 次）")
            await browser.b_sleep(1.0, 1.5)
            continue

        if await _wait_img(browser, "rank", timeout=25, threshold=NAV_THRESHOLD):
            browser.script_log("已回到主界面")
            return True
        browser.script_log(f"第 {attempt} 次等待 rank.png 超时，重试")

    browser.script_log("多次尝试仍未回到主界面")
    return False


async def 返回出击界面(browser: UserBrowser) -> bool:
    for attempt in range(1, 4):
        await browser.update_frame()
        if await browser.match_image(_img("出击_logo"), threshold=NAV_THRESHOLD):
            browser.script_log("已在出击界面")
            return True

        if await browser.click_image(_img("出击"), threshold=THRESHOLD):
            browser.script_log(f"点击出击（第 {attempt}/3 次），等待出击界面…")
            if await _wait_img(
                browser, "出击_logo", timeout=25, threshold=NAV_THRESHOLD
            ):
                browser.script_log("已进入出击界面")
                return True
            browser.script_log(f"第 {attempt} 次等待 出击_logo 超时")
            continue

        browser.script_log("未找到 出击.png，先回主界面再试")
        if not await 返回主界面(browser):
            return False
        await browser.update_frame()
        if await browser.click_image(_img("出击"), threshold=THRESHOLD):
            if await _wait_img(
                browser, "出击_logo", timeout=25, threshold=NAV_THRESHOLD
            ):
                return True

    browser.script_log("多次尝试仍未进入出击界面")
    return False


# ── 房间 ───────────────────────────────────────────────────


async def _room_go_main(browser: UserBrowser) -> bool:
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
    """领取房间奖励。

    AP 满时：点领取后短暂弹出 room_ap上限 气泡，不会出 room_ok。
    因此点击后要在短窗口内同时盯「上限气泡 / ok 窗 / 领取按钮仍在」。
    """
    browser.script_log("已在房间，开始领取奖励")
    await browser.b_sleep(0.8, 1.2)
    await browser.update_frame()

    if not await browser.match_image(_img("room_收取奖励"), threshold=THRESHOLD):
        browser.script_log("没有 room_收取奖励.png，任务完成")
        return True

    if not await browser.click_image(_img("room_收取奖励"), threshold=THRESHOLD):
        browser.script_log("点击领取奖励失败")
        return False
    browser.script_log("已点击领取奖励，等待 ok 或 AP 上限提示…")

    # 气泡很短，点击后立刻高频轮询（约 3s）
    toast_deadline = time.monotonic() + 3.0
    while time.monotonic() < toast_deadline:
        await browser.update_frame()
        if await browser.match_image(_img("room_ap上限"), threshold=NAV_THRESHOLD):
            browser.script_log("检测到 AP 上限提示，无法领取，任务结束")
            return True
        if await browser.match_image(_img("room_ok"), threshold=THRESHOLD):
            break
        await browser.b_sleep(0.25, 0.35)

    await browser.update_frame()
    # 说明：点完后若领取按钮还在且没有 ok，多半是上限/未领成 → 直接结束
    if (
        not await browser.match_image(_img("room_ok"), threshold=THRESHOLD)
        and await browser.match_image(_img("room_收取奖励"), threshold=THRESHOLD)
    ):
        browser.script_log("领取后仍见收取按钮且无 ok，视为无法领取，任务结束")
        return True

    clicked_ok = False
    timer = StepTimer("room_ok", 12.0)
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("room_ap上限"), threshold=NAV_THRESHOLD):
            browser.script_log("检测到 AP 上限提示，任务结束")
            return True
        if await browser.click_image(_img("room_ok"), threshold=THRESHOLD):
            browser.script_log("点击 room_ok")
            clicked_ok = True
            await browser.b_sleep(0.8, 1.2)
            await browser.update_frame()
            if await browser.match_image(_img("room_ok"), threshold=THRESHOLD):
                await browser.click_image(_img("room_ok"), threshold=THRESHOLD)
                await browser.b_sleep(0.5, 0.8)
            break
        await browser.b_sleep(0.5, 0.8)

    if not clicked_ok:
        browser.script_log("未出现 room_ok（可能已关闭或被上限提示拦截）")
    browser.script_log("领取奖励完成")
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
    for _ in range(12):
        await browser.update_frame()
        if await browser.match_image(_img("jjc_倍率"), threshold=THRESHOLD):
            browser.script_log("倍率已满")
            return
        if await browser.click_image(
            _img("jjc_刷新"), pianyi=(-20, 0), threshold=THRESHOLD
        ):
            browser.script_log("点击提高倍率")
            await browser.b_sleep(0.35, 0.55)
        else:
            await browser.b_sleep(0.3, 0.5)
    browser.script_log("未能确认满倍率，继续尝试出击")


async def _click_max_x_duanwei(browser: UserBrowser) -> bool:
    """优先点击 x 最大的 jjc_段位（最靠右）。"""
    await browser.update_frame()
    matches = await browser.match_image_multi(_img("jjc_段位"), threshold=THRESHOLD)
    if not matches:
        browser.script_log("未找到 jjc_段位.png")
        return False
    best = max(matches, key=lambda m: m["x"])
    browser.script_log(f"点击段位 x={best['x']:.0f}, y={best['y']:.0f}")
    await browser.click(best["x"], best["y"])
    return True


async def _handle_jjc_result(browser: UserBrowser) -> bool:
    """处理结算：有 ok/结算再点；禁止对不存在的图反复 click（会触发动作循环检测）。"""
    timer = StepTimer("jjc_result", STEP_TIMEOUT_BATTLE)
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("jjc_ok"), threshold=THRESHOLD):
            if await browser.click_image(_img("jjc_ok"), threshold=THRESHOLD):
                browser.script_log("点击 jjc_ok（段位奖励）")
                await browser.b_sleep(0.5, 0.8)
            continue
        if await browser.match_image(_img("jjc_结算"), threshold=NAV_THRESHOLD):
            m = await browser.match_image(_img("jjc_结算"), threshold=NAV_THRESHOLD)
            if m and m.x is not None:
                ox = random.randint(40, 120)
                oy = random.randint(30, 90)
                browser.script_log("结算界面，随机点击退出")
                await browser.click(m.x + ox, m.y + oy)
                await browser.b_sleep(0.8, 1.2)
            continue
        # 回到列表：有 logo、能看到段位、且编队出击已消失
        if (
            await browser.match_image(_img("jjc_logo"), threshold=NAV_THRESHOLD)
            and not await browser.match_image(_img("jjc_出击"), threshold=THRESHOLD)
            and await browser.match_image(_img("jjc_段位"), threshold=THRESHOLD)
        ):
            browser.script_log("已回到竞技场列表")
            return True
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

    # ① 先确认离开编队界面（jjc_出击 消失）
    leave = StepTimer("leave_prep", 60.0)
    left_prep = False
    while not leave.expired():
        await browser.update_frame()
        if not await browser.match_image(_img("jjc_出击"), threshold=THRESHOLD):
            left_prep = True
            browser.script_log("已离开编队界面，进入战斗/加载…")
            break
        await browser.b_sleep(0.6, 1.0)
    if not left_prep:
        browser.script_log("出击后仍停在编队界面（jjc_出击 未消失）")
        return False

    # ② 最短战斗时间，避免 loading 瞬间误判
    min_fight = 15.0
    fight_start = time.monotonic()
    timer = StepTimer("jjc_fight", STEP_TIMEOUT_JJC_FIGHT)
    last_log = time.monotonic()
    while not timer.expired():
        await browser.update_frame()
        # 结算相关才算真正打完
        if await browser.match_image(_img("jjc_ok"), threshold=THRESHOLD):
            browser.script_log("战斗结束：出现 jjc_ok")
            return True
        if await browser.match_image(_img("jjc_结算"), threshold=NAV_THRESHOLD):
            browser.script_log("战斗结束：出现 jjc_结算")
            return True
        # 回到列表：要过最短时间，且编队按钮仍不在、段位可见
        elapsed = time.monotonic() - fight_start
        if elapsed >= min_fight:
            if (
                await browser.match_image(_img("jjc_logo"), threshold=NAV_THRESHOLD)
                and not await browser.match_image(_img("jjc_出击"), threshold=THRESHOLD)
                and await browser.match_image(_img("jjc_段位"), threshold=THRESHOLD)
            ):
                browser.script_log("战斗结束：已回到竞技场列表")
                return True
        now = time.monotonic()
        if now - last_log >= 30:
            left = max(0.0, timer.deadline - now)
            browser.script_log(f"  仍在战斗/加载中… 剩余约 {left:.0f}s")
            last_log = now
        await browser.b_sleep(1.5, 2.5)
    browser.script_log("长时间等待战斗结束超时")
    return False


async def _jjc_one_battle(browser: UserBrowser) -> bool:
    """在已处于竞技场的前提下打一轮；耗尽则也算成功。"""
    await browser.update_frame()
    if await browser.match_image(_img("jjc_end"), threshold=JJC_END_THRESHOLD):
        browser.script_log("jjc_end：次数已耗尽")
        return True

    await _raise_jjc_multiplier(browser)
    if not await _click_max_x_duanwei(browser):
        return False
    if not await _wait_img(browser, "jjc_出击", timeout=20, threshold=THRESHOLD):
        browser.script_log("未进入编队界面")
        return False
    if not await browser.click_image(_img("jjc_出击"), threshold=THRESHOLD):
        browser.script_log("点击 jjc_出击 失败")
        return False

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
    """次数是否耗尽。高阈值；若仍能切难度/出击则不视为耗尽。"""
    m = await browser.match_image(_img("ta_cishu"), threshold=TA_CISHU_THRESHOLD)
    if not m:
        return False
    # 还能操作选难度/塔内出击时，当作误匹配
    if await browser.match_image(_img("ta_biancheng"), threshold=THRESHOLD):
        browser.script_log(
            f"ta_cishu 分数={getattr(m, 'max_val', m)}，但可见 ta_biancheng，忽略耗尽"
        )
        return False
    if await browser.match_image(_img("ta_nandu"), threshold=THRESHOLD):
        browser.script_log(
            f"ta_cishu 分数={getattr(m, 'max_val', m)}，但可见 ta_nandu，忽略耗尽"
        )
        return False
    if await browser.match_image(_img("ta_chuji"), threshold=THRESHOLD):
        browser.script_log(
            f"ta_cishu 分数={getattr(m, 'max_val', m)}，但可见 ta_chuji，忽略耗尽"
        )
        return False
    browser.script_log(f"确认次数耗尽 ta_cishu score={getattr(m, 'max_val', m)}")
    return True


async def _tower_go_sortie(browser: UserBrowser) -> bool:
    return await 返回出击界面(browser)


async def _tower_enter_select(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await browser.match_image(_img("ta_logo"), threshold=NAV_THRESHOLD):
        browser.script_log("已在塔界面")
        return True
    if not await browser.click_image(_img("ta"), threshold=THRESHOLD):
        browser.script_log("未找到 ta.png")
        return False
    browser.script_log("点击 ta，等待塔界面")
    ok = await _wait_img(browser, "ta_logo", timeout=20, threshold=NAV_THRESHOLD)
    if ok:
        browser.script_log("已进入塔界面")
    return ok


async def _switch_tower_difficulty(browser: UserBrowser) -> bool:
    browser.script_log("开始调整塔难度…")
    for i in range(12):
        await browser.update_frame()
        if await browser.match_image(_img("ta_nandu"), threshold=THRESHOLD):
            browser.script_log("已看到目标难度 ta_nandu")
            break
        ox = random.randint(0, 30)
        oy = random.randint(-150, -120)
        if await browser.click_image(
            _img("ta_biancheng"), pianyi=(ox, oy), threshold=THRESHOLD
        ):
            browser.script_log(f"切换难度 #{i + 1} offset=({ox},{oy})")
            await browser.b_sleep(0.15, 0.35)
        else:
            browser.script_log("未找到 ta_biancheng，稍后重试")
            await browser.b_sleep(0.4, 0.7)
    else:
        browser.script_log("未能切到目标难度 ta_nandu")
        return False

    if not await browser.click_image(_img("ta_nandu"), threshold=THRESHOLD):
        browser.script_log("点击 ta_nandu 失败")
        return False
    browser.script_log("已点目标难度，等待确认框")

    if await _wait_img(browser, "ta_tiaozhan", timeout=8, threshold=THRESHOLD):
        if await browser.click_image(_img("ta_tiaozhan"), threshold=THRESHOLD):
            browser.script_log("点击挑战确认")
            await browser.b_sleep(1.0, 1.5)
    else:
        browser.script_log("未出现 ta_tiaozhan，可能已直接进塔")

    if await _wait_img(browser, "ta_chuji", timeout=25, threshold=THRESHOLD):
        browser.script_log("已进入塔内关卡")
        return True
    browser.script_log("进入塔内超时（未见 ta_chuji）")
    return False


async def _run_tower_auto(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if not await browser.click_image(_img("ta_chuji"), threshold=THRESHOLD):
        browser.script_log("未找到 ta_chuji.png")
        return False
    browser.script_log("点击塔内出击，等待出击界面")

    entered = False
    timer = StepTimer("tower_sortie_ui", STEP_TIMEOUT)
    while not timer.expired():
        await browser.update_frame()
        if (
            await browser.match_image(_img("ta_auto1"), threshold=THRESHOLD)
            or await browser.match_image(_img("ta_auto2"), threshold=THRESHOLD)
            or await browser.match_image(_img("ta_chuji_chuji"), threshold=THRESHOLD)
        ):
            entered = True
            break
        await browser.b_sleep(0.5, 0.8)
    if not entered:
        browser.script_log("未进入塔出击界面")
        return False

    await browser.update_frame()
    if await browser.match_image(_img("ta_auto2"), threshold=THRESHOLD):
        browser.script_log("auto 已开启")
    elif await browser.click_image(_img("ta_auto1"), threshold=THRESHOLD):
        browser.script_log("点击开启 auto")
        await browser.b_sleep(0.3, 0.5)

    if not await browser.click_image(_img("ta_chuji_chuji"), threshold=THRESHOLD):
        if not await browser.click_image(_img("ta_chuji_chuji2"), threshold=THRESHOLD):
            browser.script_log("未找到塔出击按钮")
            return False
    browser.script_log("已开始挑战，等待 auto 完成（奖励窗口）")

    hold = 0.0
    timer = StepTimer("tower_reward", STEP_TIMEOUT_BATTLE)
    while not timer.expired():
        await browser.update_frame()
        if await browser.match_image(_img("ta_jiangli"), threshold=NAV_THRESHOLD):
            hold += 1.0
            if hold >= 5.0:
                browser.script_log("奖励窗口已稳定，点击 ok")
                await browser.click_image(_img("ta_ok"), threshold=THRESHOLD)
                await browser.b_sleep(0.8, 1.2)
                return True
            await browser.b_sleep(0.9, 1.1)
            continue
        hold = 0.0
        await browser.b_sleep(1.0, 1.5)

    browser.script_log("等待塔奖励超时")
    return False


async def _tower_one_round(browser: UserBrowser) -> bool:
    await browser.update_frame()
    if await _tower_times_exhausted(browser):
        browser.script_log("本轮开始前次数已耗尽")
        return True

    in_stage = await browser.match_image(_img("ta_chuji"), threshold=THRESHOLD)
    on_nandu = await browser.match_image(_img("ta_nandu"), threshold=THRESHOLD)
    on_biancheng = await browser.match_image(_img("ta_biancheng"), threshold=THRESHOLD)

    if in_stage and not on_biancheng and not on_nandu:
        browser.script_log("已在塔内，直接出击/auto")
    else:
        if not await _switch_tower_difficulty(browser):
            return False

    if not await _run_tower_auto(browser):
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


# ── 入口 ───────────────────────────────────────────────────


async def do_work(browser: UserBrowser):
    """日常入口：房间 → 竞技场 → 爬塔。

    任务级同样走回退：当前任务失败会重做上一任务；上一任务仍失败则抛异常。
    """
    browser.use_polling_temp_cache = True
    await run_step_chain(
        browser,
        [
            ("房间领体力", 房间领取奖励),
            ("竞技场", 竞技场),
            ("爬塔", 爬塔),
        ],
        step_timeout=STEP_TIMEOUT_BATTLE * MAX_ARENA_ROUNDS,
        label="日常",
    )
    browser.script_log("===== 日常全部完成 =====")
