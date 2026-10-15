"""阴阳师刷关卡：双开错峰 + 新鲜帧点击事务。

识屏只提出动作候选；点击前用两张新帧复核目标仍在且位置稳定；点击后
确认目标消失。截图/匹配由 UserWindow 的进程级 vision_resource 公平串行化。
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path

from backend.automation.user_window import UserWindow
from core.path import IMG_PATH


IMG_DIR = IMG_PATH / "yys" / "刷999"
THRESHOLD = 0.85
SCAN_SLEEP = (0.35, 0.65)
WAIT_GOLD_SCAN_SLEEP = (1.0, 2.0)
METRIC_LOG_INTERVAL_S = 60.0
VERIFY_GAP = (0.12, 0.22)
VERIFY_DRIFT_PX = 24.0
CONFIRM_POLL = (0.25, 0.45)
CHALLENGE_CONFIRM_S = 8.0
GOLD_CONFIRM_S = 3.0
CHALLENGE_JITTER = 5

# 金币只有落在奖励框附近才代表结算；右上货币栏里的同图标必须拒绝。
_REWARD_L, _REWARD_R = 0.24, 0.76
_REWARD_T, _REWARD_B = 0.16, 0.80


def _img(name: str) -> Path:
    return IMG_DIR / f"{name}.png"


def _score(result) -> float:
    value = getattr(result, "score", None)
    if value is None:
        value = getattr(result, "max_val", 0.0)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def frame_size(browser) -> tuple[int, int] | None:
    frame = getattr(browser, "_frame", None)
    if frame is not None:
        try:
            return int(frame.shape[1]), int(frame.shape[0])
        except Exception:
            pass
    return None


def _gold_is_settlement(browser, result) -> bool:
    wh = frame_size(browser)
    if not wh or not result or result.x is None:
        return False
    w, h = wh
    x, y = int(result.x), int(result.y)
    return (
        w * _REWARD_L <= x <= w * _REWARD_R
        and h * _REWARD_T <= y <= h * _REWARD_B
    )


def human_jitter(radius: int) -> tuple[int, int]:
    sigma = max(1.0, radius / 2.2)
    dx = max(-radius, min(radius, int(round(random.gauss(0, sigma)))))
    dy = max(-radius, min(radius, int(round(random.gauss(0, sigma)))))
    return dx, dy


async def _match_current(
    browser, name: str, *, force_wide: bool = False, priority: int = 20
):
    """匹配当前快照；由调用方决定何时刷新，便于一帧复用多个模板。"""
    result = await browser.match_image(
        _img(name), threshold=THRESHOLD, force_wide=force_wide,
        vision_priority=priority,
    )
    if result and result.x is not None and _score(result) >= THRESHOLD:
        return result
    return None


async def _fresh_match(
    browser, name: str, *, force_wide: bool = False, priority: int = 20
):
    """强制取新帧后匹配；返回值只对这次新帧负责。"""
    await browser.update_frame(priority=priority)
    return await _match_current(
        browser, name, force_wide=force_wide, priority=priority
    )


async def _stable_match(browser, name: str, first=None):
    """两帧复核。场景变化或坐标漂移时拒绝本次点击。"""
    a = first or await _fresh_match(browser, name)
    if not a:
        return None
    await browser.b_sleep(*VERIFY_GAP)
    b = await _fresh_match(browser, name, force_wide=True, priority=0)
    if not b:
        browser.script_log(f"[{name}] 点击前复核：目标已消失，取消点击")
        return None
    drift = math.hypot(float(b.x) - float(a.x), float(b.y) - float(a.y))
    if drift > VERIFY_DRIFT_PX:
        browser.script_log(f"[{name}] 点击前复核：坐标漂移 {drift:.1f}px，取消点击")
        return None
    if name == "金币" and not _gold_is_settlement(browser, b):
        browser.script_log("[金币] 点击前复核：命中不在奖励框，取消点击")
        return None
    return b


async def _confirm_gone(browser, name: str, timeout: float) -> bool:
    """点击后只观察、不补点；连续两张新帧消失才算成功。"""
    deadline = time.monotonic() + timeout
    absent = 0
    while time.monotonic() < deadline:
        hit = await _fresh_match(browser, name, force_wide=True, priority=0)
        absent = absent + 1 if hit is None else 0
        if absent >= 2:
            browser.script_log(f"[{name}] 点击后确认：目标已消失")
            return True
        await browser.b_sleep(*CONFIRM_POLL)
    browser.script_log(f"[{name}] 点击后确认超时，退回重新识屏")
    return False


async def _confirm_gold_to_challenge(browser, timeout: float):
    """结算点击后优先用“挑战已出现”作正向确认。

    返回 ``(确认成功, 已匹配的挑战候选)``。挑战未出现时，仍以同一帧里的
    金币连续两次消失作为兜底；因此不会因为中间空白转场而贸然点击。
    """
    deadline = time.monotonic() + timeout
    gold_absent = 0
    while time.monotonic() < deadline:
        await browser.update_frame(priority=0)

        # 挑战尺度在上一轮已经稳定命中过，先走高优先级热匹配；命中即可把
        # 坐标直接传给 click_challenge 的第二帧复核。
        challenge = await _match_current(browser, "挑战", priority=0)
        if challenge:
            browser.script_log("[金币] 点击后确认：挑战已出现")
            return True, challenge

        # 与挑战共用刚才那张帧，不再重复截图。金币用完整尺度确认，避免错误
        # 窄带把“仍在”误判成“已消失”。
        gold = await _match_current(
            browser, "金币", force_wide=True, priority=0
        )
        gold_absent = gold_absent + 1 if gold is None else 0
        if gold_absent >= 2:
            browser.script_log("[金币] 点击后确认：金币已连续两帧消失")
            return True, None
        await browser.b_sleep(*CONFIRM_POLL)

    browser.script_log("[金币] 点击后确认超时，退回重新识屏")
    return False, None


def _settle_click_xy(browser) -> tuple[int, int] | None:
    """选择奖励框外、远离右上货币栏和左下按钮的安全带。"""
    wh = frame_size(browser)
    if not wh:
        return None
    w, h = wh
    zones = (
        (0.05, 0.18, 0.30, 0.55),
        (0.82, 0.95, 0.32, 0.72),
        (0.38, 0.72, 0.84, 0.94),
    )
    x0, x1, y0, y1 = random.choice(zones)
    x = random.randint(int(w * x0), int(w * x1))
    y = random.randint(int(h * y0), int(h * y1))
    dx, dy = human_jitter(10)
    return x + dx, y + dy


async def click_challenge(browser, candidate=None) -> bool:
    hit = await _stable_match(browser, "挑战", candidate)
    if not hit:
        return False
    dx, dy = human_jitter(CHALLENGE_JITTER)
    x, y = int(hit.x) + dx, int(hit.y) + dy
    await browser.click(x, y, pianyi=(0, 0), down_time=random.uniform(0.07, 0.18))
    browser.script_log(f"[挑战] 已点击新鲜坐标 ({x},{y})，等待转场确认")
    return await _confirm_gone(browser, "挑战", CHALLENGE_CONFIRM_S)


async def click_gold(browser, candidate=None):
    hit = await _stable_match(browser, "金币", candidate)
    if not hit or not _gold_is_settlement(browser, hit):
        return False, None
    point = _settle_click_xy(browser)
    if point is None:
        browser.script_log("[金币] 无法确认帧尺寸，取消点击")
        return False, None
    x, y = point
    await browser.click(x, y, pianyi=(0, 0), down_time=random.uniform(0.07, 0.18))
    browser.script_log(f"[金币] 已在奖励框外点击 ({x},{y})，等待挑战出现")
    return await _confirm_gold_to_challenge(browser, GOLD_CONFIRM_S)


async def scan_scene(browser, *, expected: str | None = None):
    """按阶段识屏；仅在启动/恢复时同时尝试两个模板。"""
    if expected is None:
        # 恢复识屏只截一次；金币与挑战在同一不可变快照上依次判断。
        await browser.update_frame(priority=20)
        gold = await _match_current(browser, "金币", priority=20)
        if gold:
            if _gold_is_settlement(browser, gold):
                return "金币", gold
            browser.script_log(
                f"[金币] 拒绝框外命中 ({int(gold.x)},{int(gold.y)}) "
                f"score={_score(gold):.3f}"
            )
        challenge = await _match_current(browser, "挑战", priority=20)
        if challenge:
            return "挑战", challenge
        return None, None

    if expected in (None, "金币"):
        # 战斗期间的普通未命中会把宽搜退避推到 112.5 秒；但进入这个阶段
        # 已经明确知道下一目标只能是金币，因此每轮都做完整尺度搜索，不能让
        # 已出现的结算画面被旧窄带缓存和退避挡住。
        gold = await _fresh_match(
            browser, "金币", force_wide=(expected == "金币"), priority=10
        )
        if gold:
            if _gold_is_settlement(browser, gold):
                return "金币", gold
            browser.script_log(
                f"[金币] 拒绝框外命中 ({int(gold.x)},{int(gold.y)}) "
                f"score={_score(gold):.3f}"
            )
        if expected == "金币":
            return None, None

    if expected in (None, "挑战"):
        challenge = await _fresh_match(browser, "挑战")
        if challenge:
            return "挑战", challenge
    return None, None


async def do_work(browser: UserWindow):
    browser.use_polling_temp_cache = False
    from backend.automation.vision_resource import (
        account_phase_delay,
        vision_resource_stats,
    )

    account = str((getattr(browser, "account", {}) or {}).get("name") or "")
    phase_s = account_phase_delay(account)
    if phase_s > 0:
        browser.script_log(f"[多开] 账号扫描相位错开 {phase_s:.2f}s")
        await browser.b_sleep(phase_s)

    browser.script_log(
        "[yys刷关卡] 启动：双开重资源错峰；点击前两帧复核；点击后消失确认"
    )
    expected: str | None = None
    next_metric_log = time.monotonic() + METRIC_LOG_INTERVAL_S
    while True:
        name, candidate = await scan_scene(browser, expected=expected)
        browser.note_state(name or (f"等待{expected}" if expected else "等待场景"))
        if name == "挑战":
            if await click_challenge(browser, candidate):
                expected = "金币"
                browser.script_log("[阶段] 挑战已确认 → 只扫描金币")
            else:
                expected = None
        elif name == "金币":
            gold_ok, challenge_candidate = await click_gold(browser, candidate)
            if gold_ok:
                expected = "挑战"
                if challenge_candidate is not None:
                    browser.script_log("[阶段] 结算已确认 → 复用挑战候选")
                    browser.note_state("挑战")
                    if await click_challenge(browser, challenge_candidate):
                        expected = "金币"
                        browser.script_log("[阶段] 挑战已确认 → 只扫描金币")
                    else:
                        expected = None
                else:
                    browser.script_log("[阶段] 结算已确认 → 只扫描挑战")
            else:
                expected = None
        else:
            await browser.b_sleep(
                *(WAIT_GOLD_SCAN_SLEEP if expected == "金币" else SCAN_SLEEP)
            )
        if time.monotonic() >= next_metric_log:
            browser.script_log(f"[多开指标] {vision_resource_stats()}")
            next_metric_log = time.monotonic() + METRIC_LOG_INTERVAL_S
