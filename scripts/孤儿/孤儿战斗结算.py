"""
孤儿战斗结算
=============
处理战斗结束后的结算界面，按 mode 选择按钮优先级。

mode:
  raid  — 贡献榜关闭 → next
  event — ok(额外奖励) → 下一关 > next
  story — ok(额外奖励) → 下一关 > 下一个故事 > next
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    img_dir: Path = IMG_PATH / 'minashigo' / '孤儿战斗结算'
    threshold: float = 0.85
    timeout: float = 60.0     # 结算总超时，超过视为失败
    click_sleep: float = 0.5  # 点击后的等待


CFG = Config()


def _img(name: str) -> Path:
    return CFG.img_dir / (name if name.endswith('.png') else name + '.png')


# ═══════════════════════════════════════════════════════════════
# 各模式按钮优先级
# ═══════════════════════════════════════════════════════════════

# (图片名, 描述)
MODE_BUTTONS = {
    "raid": [
        ("raid_gx_close", "关闭贡献榜"),
        ("next", "下一步"),
    ],
    "event": [
        ("ok", "关闭额外奖励"),
        ("下一关", "下一关"),
        ("next", "下一步"),
    ],
    "story": [
        ("ok", "关闭额外奖励"),
        ("下一关", "下一关"),
        ("下一个故事", "下一个故事"),
        ("next", "下一步"),
    ],
}

# 兜底模式：通用的按钮顺序
FALLBACK_BUTTONS = [
    ("下一关", "下一关"),
    ("下一个故事", "下一个故事"),
    ("raid_gx_close", "关闭"),
    ("ok", "确认"),
    ("next", "下一步"),
]


# ═══════════════════════════════════════════════════════════════
# 结算处理
# ═══════════════════════════════════════════════════════════════

async def handle_battle_result(
    browser: UserBrowser,
    mode: str = "auto",
) -> None:
    """
    处理战斗结算界面，直到所有按钮消失。

    Args:
        browser: 浏览器实例
        mode: 战斗模式 — raid / event / story / auto
    """
    buttons = MODE_BUTTONS.get(mode, FALLBACK_BUTTONS)
    browser.script_log(f"[战斗结算] 模式: {mode}  按钮数: {len(buttons)}")

    enter_time = asyncio.get_event_loop().time()
    consecutive_empty = 0  # 连续空转计数

    while True:
        # 总超时
        if asyncio.get_event_loop().time() - enter_time > CFG.timeout:
            browser.script_log("[战斗结算] ⚠ 超时，强制退出")
            break

        await browser.update_frame()

        clicked_any = False
        for img_name, desc in buttons:
            if await browser.click_image(_img(img_name), threshold=CFG.threshold):
                browser.script_log(f"  {desc}")
                await browser.b_sleep(CFG.click_sleep, CFG.click_sleep + 0.3)
                clicked_any = True
                break  # 每轮只点一个按钮

        if clicked_any:
            consecutive_empty = 0
            continue

        # 没点到任何按钮 → 场景可能已结束
        consecutive_empty += 1
        if consecutive_empty >= 3:
            browser.script_log("[战斗结算] ✅ 结算完成")
            break

        await browser.b_sleep(0.3)


# ═══════════════════════════════════════════════════════════════
# 系统入口（独立测试用）
# ═══════════════════════════════════════════════════════════════

async def do_work(browser: UserBrowser):
    """调试入口"""
    # await handle_battle_result(browser, mode="auto")
    await handle_battle_result(browser, mode="story")
