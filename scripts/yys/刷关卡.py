"""阴阳师 yys：按 assets/images/yys/脚本介绍.txt 循环刷关。

场景：
  挑战.png → 挑战界面（点挑战进战斗）
  金币.png → 战斗结算（奖励框外随机点直到消失）

点击：挑战带小抖动；结算在框外安全区随机落点 + 仿人手抖动。
窗口模式：do_work 标注为 UserWindow。
"""

from __future__ import annotations

import random
from pathlib import Path

from backend.automation.user_window import UserWindow
from core.path import IMG_PATH

IMG_DIR = IMG_PATH /"yys" / "刷999" 
THRESHOLD = 0.85
NAV_THRESHOLD = 0.8
JITTER = 5  # 挑战等：小幅度抖动 (-5,-5)~(5,5)
# 结算屏：奖励框约 0.27~0.73W × 0.20~0.74H；点框外即可关闭
_BOX_L, _BOX_R = 0.27, 0.73
_BOX_T, _BOX_B = 0.20, 0.74
# 用户标注禁点：右上跑马灯、左下角图标（含抖动余量）
_BAN_TOP_RIGHT = (0.52, 1.0, 0.0, 0.26)   # x0,x1,y0,y1 归一化
_BAN_BOTTOM_LEFT = (0.0, 0.22, 0.58, 1.0)


def _img(name: str) -> Path:
    stem = name[:-4] if name.lower().endswith(".png") else name
    return IMG_DIR / f"{stem}.png"


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
    """仿人手：高斯抖动，截断到 ±radius。"""
    sigma = max(1.0, radius / 2.2)
    dx = int(round(random.gauss(0, sigma)))
    dy = int(round(random.gauss(0, sigma)))
    return max(-radius, min(radius, dx)), max(-radius, min(radius, dy))


def _in_norm_rect(x: int, y: int, w: int, h: int, rect: tuple[float, float, float, float]) -> bool:
    x0, x1, y0, y1 = rect
    return (w * x0) <= x <= (w * x1) and (h * y0) <= y <= (h * y1)


def _settle_banned(x: int, y: int, w: int, h: int) -> bool:
    """奖励框 / 右上跑马灯 / 左下图标 → 禁点。"""
    if _in_norm_rect(x, y, w, h, (_BOX_L, _BOX_R, _BOX_T, _BOX_B)):
        return True
    if _in_norm_rect(x, y, w, h, _BAN_TOP_RIGHT):
        return True
    if _in_norm_rect(x, y, w, h, _BAN_BOTTOM_LEFT):
        return True
    return False


def _settle_outside_xy(browser: UserWindow, gold_x: int, gold_y: int) -> tuple[int, int]:
    """框外安全带随机取点；避开右上跑马灯与左下角图标。"""
    wh = _frame_wh(browser)
    if wh:
        w, h = wh
        pad = 20
        # 左中（抬高，避开左下圈）、右中、下中偏右（「点击屏幕继续」附近）、上偏左
        zones = [
            (8, int(w * _BOX_L) - pad, int(h * 0.28), int(h * 0.55)),
            (int(w * _BOX_R) + pad, w - 8, int(h * 0.28), int(h * 0.72)),
            (int(w * 0.38), int(w * 0.82), int(h * _BOX_B) + pad, h - 8),
            (int(w * 0.12), int(w * 0.48), 8, int(h * _BOX_T) - pad),
        ]
    else:
        w = max(gold_x * 2, 900)
        h = max(gold_y * 2, 500)
        gx, gy = gold_x, gold_y
        zones = [
            (gx - 450, gx - 250, gy - 40, gy + 40),
            (gx + 250, gx + 450, gy - 40, gy + 80),
            (gx - 80, gx + 200, gy + 180, gy + 280),
            (gx - 200, gx - 20, gy - 220, gy - 140),
        ]

    valid: list[tuple[int, int, int, int, int]] = []
    for x0, x1, y0, y1 in zones:
        if x1 > x0 + 4 and y1 > y0 + 4:
            area = (x1 - x0) * (y1 - y0)
            valid.append((area, x0, x1, y0, y1))
    if not valid:
        return gold_x + 280, gold_y + random.randint(-40, 40)

    for _ in range(40):
        total = sum(a for a, *_ in valid)
        r = random.uniform(0, total)
        acc = 0.0
        x0 = x1 = y0 = y1 = 0
        for area, ax0, ax1, ay0, ay1 in valid:
            acc += area
            if r <= acc:
                x0, x1, y0, y1 = ax0, ax1, ay0, ay1
                break
        x = random.randint(x0, x1 - 1)
        y = random.randint(y0, y1 - 1)
        jx, jy = _human_jitter(14)
        px, py = x + jx, y + jy
        if wh and _settle_banned(px, py, wh[0], wh[1]):
            continue
        return px, py

    # 兜底：右下安全点
    if wh:
        return int(wh[0] * 0.88), int(wh[1] * 0.88)
    return gold_x + 300, gold_y + 200


async def unknown_state(browser: UserWindow) -> str | None:
    """场景层：只识屏，不点击。结算优先于挑战。"""
    gold = await browser.match_image(_img("金币"), threshold=NAV_THRESHOLD)
    if gold and gold.x is not None:
        browser.script_log(f"[scene] 战斗结算 score={gold.max_val}")
        return "战斗结算"
    challenge = await browser.match_image(_img("挑战"), threshold=NAV_THRESHOLD)
    if challenge and challenge.x is not None:
        browser.script_log(f"[scene] 挑战界面 score={challenge.max_val}")
        return "挑战界面"
    g = getattr(gold, "max_val", None) if gold else None
    c = getattr(challenge, "max_val", None) if challenge else None
    browser.script_log(f"[scene] 未识别 gold={g} challenge={c} (需>={NAV_THRESHOLD})")
    await browser.b_sleep(0.8, 1.5)
    return None


async def step_挑战界面(browser: UserWindow) -> str | None:
    """点挑战进入战斗；expect=gone 拦住空点击。"""
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
        browser.script_log("[挑战界面] 点击未确认（挑战仍在/未命中）→ 疑似空点击")
        await browser.b_sleep(0.6, 1.2)
        return "未知"
    browser.script_log("[挑战界面] 已确认挑战消失，进入战斗/转场")
    await browser.b_sleep(0.8, 1.5)
    return "未知"


async def step_战斗结算(browser: UserWindow) -> str | None:
    """奖励框外随机点，每次用 expect=gone 确认金币消失。"""
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
        browser.script_log(f"[战斗结算] 框外点击 #{i+1} @({tx},{ty}) gold=({gx},{gy})")
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
    browser.script_log("[战斗结算] 超时仍见金币，回识屏")
    return "未知"


SCENE_TO_STEP = {
    "挑战界面": "挑战界面",
    "战斗结算": "战斗结算",
}

STATES = {
    "挑战界面": step_挑战界面,
    "战斗结算": step_战斗结算,
}


async def do_work(browser: UserWindow):
    browser.click_confirm = "critical"
    browser.script_log("[yys刷关卡] 开始循环（click_confirm=critical，转场 gone）")
    step = "未知"
    while True:
        await browser.update_frame()
        scene = await unknown_state(browser)
        if scene:
            step = SCENE_TO_STEP.get(scene, "未知")
        browser.note_state(step)
        if step == "未知":
            await browser.b_sleep(0.6, 1.2)
            continue
        handler = STATES.get(step)
        if handler is None:
            step = "未知"
            continue
        nxt = await handler(browser)
        if nxt == "__exit__":
            break
        step = nxt or "未知"
        await browser.b_sleep(0.05, 0.15)
