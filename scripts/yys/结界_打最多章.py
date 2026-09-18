"""阴阳师 · 结界突破：每次打勋章最多的对手（协作初试版）。

前提：已打开「结界突破」个人/寮列表（本脚本不负责从庭院点进结界）。

场景优先级：
  金币.png     → 战斗结算（框外点关）
  挑战.png     → 进攻确认（点挑战进战斗）
  否则         → 数章选最高，点卡槽；空板则等待

素材：
  assets/images/yys/结界/勋章.png（数章，经 helper）
  assets/images/yys/刷999/挑战.png、金币.png（与刷关共用；可拷到结界/）
"""

from __future__ import annotations

import random
from pathlib import Path

from backend.automation.user_window import UserWindow
from core.path import IMG_PATH
from scripts.yys.结界勋章 import count_opponent_medals, pick_richest

JJ_DIR = IMG_PATH / "yys" / "结界"
FALLBACK_DIR = IMG_PATH / "yys" / "刷999"

THRESHOLD = 0.85
NAV_THRESHOLD = 0.80
JITTER = 5
# 空板等待后再数；连空多次可自行停（初试版只打日志）
EMPTY_SLEEP = (2.0, 3.5)
MAX_EMPTY_STREAK = 8

_BOX_L, _BOX_R = 0.27, 0.73
_BOX_T, _BOX_B = 0.20, 0.74
_BAN_TOP_RIGHT = (0.52, 1.0, 0.0, 0.26)
_BAN_BOTTOM_LEFT = (0.0, 0.22, 0.58, 1.0)


def _img(name: str) -> Path:
    stem = name[:-4] if name.lower().endswith(".png") else name
    for folder in (JJ_DIR, FALLBACK_DIR, IMG_PATH / "yys"):
        p = folder / f"{stem}.png"
        if p.is_file():
            return p
    return JJ_DIR / f"{stem}.png"


def _frame_wh(browser: UserWindow) -> tuple[int, int] | None:
    frame = getattr(browser, "_frame", None)
    if frame is not None:
        try:
            return int(frame.shape[1]), int(frame.shape[0])
        except Exception:
            pass
    size = getattr(browser, "_client_size", None)
    if size and len(size) == 2 and size[0] > 0 and size[1] > 0:
        return int(size[0]), int(size[1])
    return None


def _human_jitter(radius: int = 12) -> tuple[int, int]:
    sigma = max(1.0, radius / 2.2)
    dx = int(round(random.gauss(0, sigma)))
    dy = int(round(random.gauss(0, sigma)))
    return max(-radius, min(radius, dx)), max(-radius, min(radius, dy))


def _in_norm_rect(x: int, y: int, w: int, h: int, rect: tuple[float, float, float, float]) -> bool:
    x0, x1, y0, y1 = rect
    return (w * x0) <= x <= (w * x1) and (h * y0) <= y <= (h * y1)


def _settle_banned(x: int, y: int, w: int, h: int) -> bool:
    if _in_norm_rect(x, y, w, h, (_BOX_L, _BOX_R, _BOX_T, _BOX_B)):
        return True
    if _in_norm_rect(x, y, w, h, _BAN_TOP_RIGHT):
        return True
    if _in_norm_rect(x, y, w, h, _BAN_BOTTOM_LEFT):
        return True
    return False


def _settle_outside_xy(browser: UserWindow, gold_x: int, gold_y: int) -> tuple[int, int]:
    wh = _frame_wh(browser)
    if wh:
        w, h = wh
        pad = 20
        zones = [
            (8, int(w * _BOX_L) - pad, int(h * 0.28), int(h * 0.55)),
            (int(w * _BOX_R) + pad, w - 8, int(h * 0.28), int(h * 0.72)),
            (int(w * 0.38), int(w * 0.82), int(h * _BOX_B) + pad, h - 8),
            (int(w * 0.12), int(w * 0.48), 8, int(h * _BOX_T) - pad),
        ]
    else:
        gx, gy = gold_x, gold_y
        zones = [
            (gx - 450, gx - 250, gy - 40, gy + 40),
            (gx + 250, gx + 450, gy - 40, gy + 80),
            (gx - 80, gx + 200, gy + 180, gy + 280),
        ]
        w = h = None

    for _ in range(40):
        x0, x1, y0, y1 = random.choice(zones)
        if x1 <= x0 + 4 or y1 <= y0 + 4:
            continue
        x = random.randint(x0, x1 - 1)
        y = random.randint(y0, y1 - 1)
        jx, jy = _human_jitter(14)
        px, py = x + jx, y + jy
        if wh and _settle_banned(px, py, wh[0], wh[1]):
            continue
        return px, py
    if wh:
        return int(wh[0] * 0.88), int(wh[1] * 0.88)
    return gold_x + 300, gold_y + 200


async def unknown_state(browser: UserWindow) -> str | None:
    """识屏：结算 > 挑战 > 结界列表。"""
    gold = await browser.match_image(_img("金币"), threshold=NAV_THRESHOLD)
    if gold and gold.x is not None:
        browser.script_log(f"[scene] 战斗结算 score={gold.max_val}")
        return "战斗结算"

    challenge = await browser.match_image(_img("挑战"), threshold=NAV_THRESHOLD)
    if challenge and challenge.x is not None:
        browser.script_log(f"[scene] 挑战界面 score={challenge.max_val}")
        return "挑战界面"

    frame = getattr(browser, "_frame", None)
    if frame is not None:
        result = count_opponent_medals(frame)
        best = pick_richest(result)
        if best is not None and best.count > 0:
            browser.script_log(
                f"[scene] 结界列表 {result.rows}x{result.cols} "
                f"best=r{best.row}c{best.col}×{best.count}"
            )
            browser._jjtp_best = best  # type: ignore[attr-defined]
            browser._jjtp_result = result  # type: ignore[attr-defined]
            return "结界列表"
        if result.rows > 0 or result.hits:
            browser.script_log(f"[scene] 结界空板/无高章 hits={len(result.hits)}")
            return "结界空板"
        # 弱命中：仍可能在结界页但尺度不对
        if result.hits:
            return "结界空板"

    browser.script_log("[scene] 未识别（请确认已在结界突破页）")
    await browser.b_sleep(0.8, 1.4)
    return None


async def step_结界列表(browser: UserWindow) -> str | None:
    """点勋章最多的对手卡。"""
    best = getattr(browser, "_jjtp_best", None)
    if best is None:
        await browser.update_frame()
        frame = browser._frame
        if frame is None:
            return "未知"
        result = count_opponent_medals(frame)
        best = pick_richest(result)
    if best is None or best.count <= 0:
        browser.script_log("[结界列表] 无目标")
        return "结界空板"

    x, y = best.click_xy
    jx, jy = _human_jitter(JITTER)
    browser.note_state("attack_best")
    browser.script_log(
        f"[结界列表] 点 r{best.row}c{best.col} count={best.count} @({x}+{jx},{y}+{jy})"
    )
    await browser.click(x + jx, y + jy, pianyi=(0, 0), down_time=random.uniform(0.08, 0.18))
    await browser.b_sleep(0.8, 1.4)
    # 清缓存，下一轮重新数
    browser._jjtp_best = None  # type: ignore[attr-defined]
    return "未知"


async def step_结界空板(browser: UserWindow) -> str | None:
    """无可打目标：等待（手动刷新 / 等免费刷新）。"""
    n = int(getattr(browser, "_jjtp_empty_streak", 0) or 0) + 1
    browser._jjtp_empty_streak = n  # type: ignore[attr-defined]
    browser.script_log(f"[结界空板] 等待中 ({n}/{MAX_EMPTY_STREAK})")
    await browser.b_sleep(*EMPTY_SLEEP)
    if n >= MAX_EMPTY_STREAK:
        browser.script_log("[结界空板] 连续空板次数过多，回识屏（可手动刷新后再继续）")
        browser._jjtp_empty_streak = 0  # type: ignore[attr-defined]
    return "未知"


async def step_挑战界面(browser: UserWindow) -> str | None:
    ok = await browser.click_image(
        _img("挑战"),
        pianyi=JITTER,
        threshold=THRESHOLD,
        down_time=random.uniform(0.08, 0.18),
        expect="gone",
        confirm_timeout=12.0,
        stable_ms=400.0,
    )
    if not ok:
        browser.script_log("[挑战界面] 点击未确认")
        await browser.b_sleep(0.6, 1.2)
        return "未知"
    browser.script_log("[挑战界面] 已进战斗/转场")
    browser._jjtp_empty_streak = 0  # type: ignore[attr-defined]
    await browser.b_sleep(0.8, 1.5)
    return "未知"


async def step_战斗结算(browser: UserWindow) -> str | None:
    from backend.script_generator.click_confirm import confirm_after_click

    for i in range(40):
        await browser.update_frame()
        gold = await browser.match_image(_img("金币"), threshold=THRESHOLD)
        if not gold or gold.x is None:
            browser.script_log("[战斗结算] 金币已消失")
            await browser.b_sleep(0.5, 1.0)
            return "未知"
        gx, gy = int(gold.x), int(gold.y)
        tx, ty = _settle_outside_xy(browser, gx, gy)
        await browser.click(tx, ty, pianyi=(0, 0), down_time=random.uniform(0.07, 0.2))
        browser.script_log(f"[战斗结算] 框外点击 #{i + 1} @({tx},{ty})")
        gone = await confirm_after_click(
            browser,
            _img("金币"),
            expect="gone",
            timeout=2.5,
            threshold=THRESHOLD,
            stable_ms=250.0,
            poll=0.2,
        )
        if gone:
            browser.script_log("[战斗结算] 已确认金币消失")
            await browser.b_sleep(0.4, 0.8)
            return "未知"
    browser.script_log("[战斗结算] 超时仍见金币")
    return "未知"


SCENE_TO_STEP = {
    "战斗结算": "战斗结算",
    "挑战界面": "挑战界面",
    "结界列表": "结界列表",
    "结界空板": "结界空板",
}

STATES = {
    "战斗结算": step_战斗结算,
    "挑战界面": step_挑战界面,
    "结界列表": step_结界列表,
    "结界空板": step_结界空板,
}


async def do_work(browser: UserWindow):
    browser.click_confirm = "critical"
    browser.enable_pseudo_record(script_hint="结界_打最多章")
    browser.script_log("[结界] 开始循环 · 打勋章最多的对手")
    step = "未知"
    try:
        while True:
            await browser.update_frame()
            scene = await unknown_state(browser)
            if scene:
                step = SCENE_TO_STEP.get(scene, "未知")
            browser.note_state(step)
            if step == "未知":
                await browser.b_sleep(0.5, 1.0)
                continue
            handler = STATES.get(step)
            if handler is None:
                step = "未知"
                continue
            nxt = await handler(browser)
            step = nxt or "未知"
    finally:
        browser.finish_pseudo_record(status="ok")
