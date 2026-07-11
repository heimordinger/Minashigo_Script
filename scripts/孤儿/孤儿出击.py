"""
孤儿出击 v1
=============
这是一个会被频繁调用的工具脚本。

功能：
1. 队伍属性检测（积分制，结果全局缓存）
2. 助战选取（根据队伍属性勾选对应列）
3. 出击（点击出击按钮进入战斗）

流程：
  场景1（助战）→ 确认（1_jd）→ 场景2（出击）→ 出击（2_cj）
"""

import asyncio
import cv2
import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import ImageFont, ImageDraw, Image
from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH, PROJECT_ROOT


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    img_dir: Path = IMG_PATH / 'minashigo' / '孤儿出击'
    attr_dir: Path = IMG_PATH / 'minashigo' / '孤儿出击' / '属性'

    threshold: float = 0.85        # 通用匹配阈值
    nav_threshold: float = 0.75    # 导航图阈值（略低，给缩放留余量）
    attr_threshold: float = 0.8    # 属性图标阈值
    state_timeout: float = 20.0
    use_polling_cache: bool = True


CFG = Config()


def _img(name: str) -> Path:
    """场景图片路径（自动补 .png）"""
    return CFG.img_dir / (name if name.endswith('.png') else name + '.png')


def _attr(name: str) -> Path:
    """属性图片路径（自动补 .png）"""
    return CFG.attr_dir / (name if name.endswith('.png') else name + '.png')


# 属性列表（按解释中的顺序）
ALL_ATTRIBUTES = ["light", "lightning", "fire", "water", "wind", "dark"]

ATTR_CN = {
    "light": "光", "lightning": "雷", "fire": "火",
    "water": "水", "wind": "风", "dark": "暗",
}


# ═══════════════════════════════════════════════════════════════
# 队伍属性缓存  ——  全局变量，避免重复检测
# ═══════════════════════════════════════════════════════════════

_team_attribute_cache: dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════
# 属性检测  ——  在场景2扫描属性图标，积分制确定队伍属性
# ═══════════════════════════════════════════════════════════════

async def _detect_team_attribute(browser: UserBrowser) -> str:
    """
    在场景 2（出击界面）检测队伍属性。

    直接调用底层 matcher.image_multi 获取所有属性图标的匹配位置，
    然后统计每个属性的出现次数和位置，用积分制确定队伍属性。
    """
    import numpy as np

    browser.script_log("[属性检测] 截取画面中…")
    await browser.update_frame()
    frame = browser._browser._frame
    if frame is not None:
        browser.script_log(f"[属性检测] 画面尺寸 {frame.shape[1]}×{frame.shape[0]}")
    browser.script_log("[属性检测] 开始匹配6种属性")

    TH = 0.55  # 多匹配用低阈值捕获所有实例

    all_matches = {}  # { attr: [(x, score), ...] }
    for attr in ALL_ATTRIBUTES:
        # 加载模板
        templ_path = str(_attr(f'2_{attr}'))
        templ_bgr = cv2.imdecode(np.fromfile(templ_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if templ_bgr is None:
            continue

        results = browser._browser.matcher.match(
            target=frame,
            template=templ_bgr,
            match_type="image_multi",
            threshold=TH,
            min_dist=4,   # 属性图标间距小，默认20会合并相邻图标
        )
        if results:
            # 过滤掉高阈值 < 0.8 的低质量匹配
            filtered = [r for r in results if r['score'] >= 0.8]
            if filtered:
                all_matches[attr] = [(r['x'], r['y'], r['score']) for r in filtered]

    # 逐属性输出详情
    for attr in ALL_ATTRIBUTES:
        if attr in all_matches:
            positions = ', '.join(f"x={x}({s:.3f})" for x, y, s in all_matches[attr])
            browser.script_log(f"  {ATTR_CN.get(attr, attr)} ×{len(all_matches[attr])}: {positions}")
        else:
            browser.script_log(f"  {ATTR_CN.get(attr, attr)} ×0")

    total_count = sum(len(v) for v in all_matches.values())
    browser.script_log(f"[属性检测] 共匹配到 {total_count} 个图标")

    # ── 调试：保存标注截图（用 PIL 支持中文）──
    if frame is not None:
        _dbg = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        _dbg_pil = Image.fromarray(_dbg)
        _draw = ImageDraw.Draw(_dbg_pil)
        try:
            _font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
        except Exception:
            _font = ImageFont.load_default()
        _colors = {"light":"lime","thunder":"blue","fire":"red","water":"cyan","wind":"magenta","dark":"yellow"}
        for attr in ALL_ATTRIBUTES:
            if attr not in all_matches:
                continue
            color = _colors.get(attr, "white")
            for x, y, s in all_matches[attr]:
                _draw.rectangle([(x-15, y-8), (x+15, y+8)], outline=color, width=2)
                _draw.text((x-15, y-18), f"{ATTR_CN.get(attr,attr)} {s:.2f}", fill=color, font=_font)
        _dbg_pil.save(str(PROJECT_ROOT / "screenshots" / "attr_debug.png"))
        browser.script_log("[属性检测] 调试截图已保存到 screenshots/attr_debug.png")

    if not all_matches:
        raise RuntimeError("属性检测失败：确认当前在出击界面")

    # 找出所有匹配中最右侧的一个
    all_positions = []  # (attr, x)
    for attr, matches in all_matches.items():
        for x, _, _ in matches:
            all_positions.append((attr, x))
    rightmost_attr = max(all_positions, key=lambda p: p[1])[0]

    # 积分：先算数量（每个图标1分），最右侧属性再加2分
    scores = {}
    for attr, matches in all_matches.items():
        scores[attr] = len(matches)  # 每个图标1分
    scores[rightmost_attr] += 2     # 最右侧属性额外+2

    winner = max(scores, key=lambda a: scores[a])

    idx_detail = '  '.join(
        f"{ATTR_CN.get(a,a)}[{scores[a]}分]"
        for a in sorted(scores, key=lambda a: -scores[a])
    )
    browser.script_log(
        f"[属性检测] 积分: {idx_detail}"
        f"  最右侧: {ATTR_CN.get(rightmost_attr, rightmost_attr)}(x={max(all_positions, key=lambda p: p[1])[1]})"
        f"  → {ATTR_CN.get(winner, winner)}"
    )
    return winner


# ═══════════════════════════════════════════════════════════════
# 获取/缓存队伍属性
# ═══════════════════════════════════════════════════════════════

async def _get_team_attribute(browser: UserBrowser) -> str:
    """获取队伍属性：优先从缓存读取，否则检测并缓存。"""
    account_name = browser.account.get("name", "")
    cached = _team_attribute_cache.get(account_name)
    if cached:
        browser.script_log(f"[属性] 使用缓存: {ATTR_CN.get(cached, cached)}")
        return cached

    # ── 先刷一帧，确认当前在哪个场景 ──
    await browser.update_frame()
    on_scene1 = await browser.match_image(_img('1_jd'), threshold=CFG.nav_threshold)
    on_scene2 = await browser.match_image(_img('2_cj'), threshold=CFG.nav_threshold)
    browser.script_log(f"[属性] 场景1(1_jd)={'✓' if on_scene1 else '✗'}  场景2(2_cj)={'✓' if on_scene2 else '✗'}")
    if on_scene1 and not on_scene2:
        browser.script_log("[属性] 在场景1，先进入场景2检测属性")
        await browser.click_image(_img('1_jd'), threshold=CFG.nav_threshold)
        await browser.b_sleep(1.5, 2.0)

    # ── 在场景2 检测属性 ──
    attr = await _detect_team_attribute(browser)
    browser.script_log(f"[属性] 检测成功: {ATTR_CN.get(attr, attr)}")

    if account_name:
        _team_attribute_cache[account_name] = attr
    return attr


# ═══════════════════════════════════════════════════════════════
# 状态处理函数
# ═══════════════════════════════════════════════════════════════

StateName = Optional[str]


async def ensure_scene1_state(browser: UserBrowser, team_attr: str) -> StateName:
    """确认在场景1（助战选择界面），属于状态机的入口检查。"""
    if await browser.match_image(_img('1_jd'), threshold=CFG.nav_threshold):
        # 检查目标属性是否已选取（用颜色检测区分选中/未选中）
        if await browser.match_image(_attr(f'1_{team_attr}_2'), threshold=CFG.threshold, use_color_check=True):
            browser.script_log(f"  目标属性已选取，直接确认")
            return "confirm_jd"
        # 未选取 → 进选择状态
        return "select_support"

    # 诊断：看看当前看到了什么
    for name in ('2_cj', '2_back'):
        if await browser.match_image(_img(name), threshold=CFG.nav_threshold):
            browser.script_log(f"  ⚠ 当前不在场景1，检测到 {name}")
            break
    else:
        browser.script_log(f"  ⚠ 不在场景1，且未检测到场景2标识")
    return None


async def select_support_state(browser: UserBrowser, team_attr: str) -> StateName:
    """在场景1中选取对应的助战栏"""
    # 用颜色检测区分：已选取 = 高亮，未选取 = 灰底
    if await browser.match_image(_attr(f'1_{team_attr}_2'), threshold=CFG.threshold, use_color_check=True):
        browser.script_log(f"  {ATTR_CN.get(team_attr, team_attr)} 已选取")
        return "confirm_jd"

    # 点击未选取状态的助战栏
    if await browser.click_image(_attr(f'1_{team_attr}_1'), threshold=CFG.threshold):
        browser.script_log(f"  点击 {ATTR_CN.get(team_attr, team_attr)} 助战栏")
        await browser.b_sleep(0.3, 0.6)
        return "select_support"  # 再检查一次

    return None


async def confirm_jd_state(browser: UserBrowser, team_attr: str) -> StateName:
    """点击 1_jd 确认，进入场景2"""
    if await browser.click_image(_img('1_jd'), threshold=CFG.nav_threshold):
        browser.script_log("  点击确认")
        await browser.b_sleep(0.8, 1.5)
        return "do_sortie"
    return None


async def do_sortie_state(browser: UserBrowser, team_attr: str) -> StateName:
    """在场景2中点击出击按钮"""
    if await browser.click_image(_img('2_cj'), threshold=CFG.nav_threshold):
        browser.script_log("  出击！")
        await browser.b_sleep(0.5, 1.0)
        return "__exit__"

    # 出击按钮没找到 → 可能还在加载，或者回到了场景1
    if await browser.match_image(_img('1_jd'), threshold=CFG.nav_threshold):
        return "confirm_jd"
    return None


# ============================================================
# 状态机执行
# ============================================================

# 顺序状态流转
STATE_FLOW = [
    ("ensure_scene1",  ensure_scene1_state),
    ("select_support", select_support_state),
    ("confirm_jd",     confirm_jd_state),
    ("do_sortie",      do_sortie_state),
]

STATE_TIMEOUT = {
    "ensure_scene1":  20,
    "select_support": 15,
    "confirm_jd":     15,
    "do_sortie":      15,
}


# ═══════════════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════════════

async def select_support_and_sorite(
    browser: UserBrowser,
    team_attribute: Optional[str] = None,
) -> None:
    """
    助战选取 → 出击 主逻辑。
    可被其他脚本直接调用。

    Args:
        browser: 浏览器实例
        team_attribute: 已知的队伍属性，如果提供则跳过检测
    """
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True

    # ── 1. 确定队伍属性 ──
    attr = team_attribute
    if attr is None:
        attr = await _get_team_attribute(browser)
    else:
        browser.script_log(f"[属性] 外部指定: {ATTR_CN.get(attr, attr)}")

    # ── 2. 确保在场景1（助战界面）──
    #     点一次 2_back 返回，后续交给状态机确认场景
    browser.script_log("[出击] 返回场景1")
    if not await browser.match_image(_img('1_jd'), threshold=CFG.nav_threshold):
        await browser.click_image(_img('2_back'), pianyi=(0, 0), threshold=CFG.nav_threshold)
        browser.script_log("  尝试点了 2_back")
        await browser.b_sleep(1.0, 1.5)

    # ── 3. 按顺序执行状态流 ──
    state_name = "ensure_scene1"
    state_enter_time = asyncio.get_event_loop().time()

    while state_name != "__exit__":
        await browser.update_frame()

        # 超时检测
        timeout = STATE_TIMEOUT.get(state_name, 20)
        now = asyncio.get_event_loop().time()
        if now - state_enter_time > timeout:
            browser.script_log(f"[超时] {state_name}，重新开始")
            state_name = "ensure_scene1"
            state_enter_time = now
            continue

        # 查找当前状态的处理函数
        handler = None
        for name, fn in STATE_FLOW:
            if name == state_name:
                handler = fn
                break

        if handler is None:
            raise RuntimeError(f"未知状态: {state_name}")

        browser.script_log(f"[{state_name}]")
        next_state = await handler(browser, attr)

        if next_state == "__exit__":
            browser.script_log("[孤儿出击] ✅ 出击完成")
            break

        if next_state and next_state != state_name:
            browser.script_log(f"  → {next_state}")
            state_name = next_state
            state_enter_time = now

        await browser.b_sleep(0.05, 0.1)


# ═══════════════════════════════════════════════════════════════
# 系统入口
# ═══════════════════════════════════════════════════════════════

async def do_work(browser: UserBrowser):
    """系统调度入口（不带外部属性参数时，自动检测）"""
    await select_support_and_sorite(browser)
