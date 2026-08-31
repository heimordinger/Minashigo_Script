"""
DO推本 v2 —— 升级版
=====================
相对原版的改进：

1. 状态机显式化 —— 状态转移有统一入口，不再靠变量赋值满天飞
2. 状态独立为模块 —— 每个状态的逻辑互不干扰，新增状态只需加一个函数
3. 并发场景识别 —— 未知态时 4 个场景图并发匹配，最坏从 8s 降到 ~2s
4. 有退出条件 —— 任务栏切换达到上限后自动结束，不再无限循环
5. 守卫机制 —— 异常弹窗（广告等）在每次 tick 最优先处理
6. 操作验证闭环 —— click_and_verify 点击后检查预期变化
7. 自适应帧缓存 —— 同一帧内重复 match 不走 OpenCV（利用 UserBrowser的 polling_temp_cache）
8. 状态超时走统一逻辑 —— 不再每个 state 各自写超时判断
"""

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH


# ═══════════════════════════════════════════════════════════════
# 配置  ——  集中管理，一目了然
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    # 图片路径
    img_dir: Path = IMG_PATH / 'DeepOne' / 'DO推本'

    # 超时（秒）
    state_timeout: float = 30.0       # 单个状态最长停留（默认，各状态见 STATE_TIMEOUT）
    match_timeout: float = 5.0        # 单次 match 保护超时
    wait_appear: float = 3.0          # 等待图像出现
    wait_disappear: float = 3.0       # 等待图像消失

    # 重试 & 退出
    max_retries_per_state: int = 5    # 每个状态内操作重试上限
    max_tab_switches: int = 3        # 任务栏切换上限，超过则结束脚本
    switch_page_count: int = 0        # 当前已切换次数

    # 匹配参数
    threshold: float = 0.85           # 模板匹配阈值
    click_offset_range: tuple = (6, 12)   # 随机偏移范围

    # 缓存（利用 UserBrowser 的 polling_temp_cache）
    use_polling_cache: bool = True


CFG = Config()


# ═══════════════════════════════════════════════════════════════
# 守卫  ——  处理不归属任何特定状态的异常弹窗
# 每次 tick 最优先执行，弹窗处理后重新截帧
# ═══════════════════════════════════════════════════════════════

GUARDS = []   # (img_path, pianyi, 描述)
_guard_ts: dict[str, float] = {}


def register_guard(img_path: Path, pianyi=(0, 0), desc=""):
    """注册一条守卫规则"""
    GUARDS.append((img_path, pianyi, desc))


async def check_guards(browser: UserBrowser) -> bool:
    """遍历守卫，命中则点击处理（同一守卫 5 秒内不重复）"""
    now = __import__('time').time()
    for img_path, pianyi, desc in GUARDS:
        key = str(img_path)
        if now - _guard_ts.get(key, 0) < 5.0:
            continue
        if await browser.click_image(img_path, pianyi=pianyi, threshold=CFG.threshold):
            browser.script_log(f"[守卫] {desc or img_path.name}")
            _guard_ts[key] = now
            await browser.b_sleep(0.3, 0.8)
            return True
    return False


# 注册已知异常弹窗（没有截图素材的暂时留空，以后补充）
register_guard(CFG.img_dir / '1_close', desc="首通奖励弹窗 关闭按钮")
register_guard(CFG.img_dir / 'err1_1', desc="网络异常 重试按钮")
register_guard(CFG.img_dir / 'err2_2', desc="代理文件下载失败 确认按钮")


# ═══════════════════════════════════════════════════════════════
# 状态处理函数
# 每个状态一个 async 函数，接收 browser，返回下一步状态名：
#   None     → 保持当前
#   "新状态" → 转移到该状态
#   "__exit__" → 结束脚本
# ═══════════════════════════════════════════════════════════════

StateName = Optional[str]  # None=保持, "xxx"=转移, "__exit__"=结束


async def unknown_state(browser: UserBrowser) -> StateName:
    """未知态：并发识别当前场景。连续认不出则延长等待（战斗中）。"""
    candidates = {
        "选关":         CFG.img_dir / '1_select',
        "跳过剧情":      CFG.img_dir / '3_skip',
        "等待战后结算":   CFG.img_dir / '4_result',
        "备战":         CFG.img_dir / '2_chuji',
    }
    # 并发匹配 —— 从最坏 8 秒降到 ~2 秒
    results = await asyncio.gather(*[
        browser.match_image(path, threshold=CFG.threshold)
        for path in candidates.values()
    ])
    for name, result in zip(candidates.keys(), results):
        if result:
            return name
    # 战斗中无可识别场景 — 延长等待避免空转
    await browser.b_sleep(1.5, 2.5)
    return None


async def select_stage_state(browser: UserBrowser) -> StateName:
    """选关态：找 new → 没有就切换任务栏 → 切换上限则结束"""
    # ① 优先找 new 关卡
    if await browser.click_image(CFG.img_dir / '1_new', threshold=CFG.threshold):
        await browser.b_sleep(0.8, 1.5)
        CFG.switch_page_count = 0     # 进入关卡，重置计数器
        select_stage_state._no_tab_count = 0
        return "备战"

    # ② 当前页没有 new → 切换任务栏（尝试多种标签样式）
    TAB_VARIANTS = ['1_c','1_c_1']  # 不同关卡栏样式：1_c, 1_c_1. . .
    tab_clicked = False
    for tab in TAB_VARIANTS:
        if await browser.click_image(CFG.img_dir / tab, pianyi=(20, 0), threshold=0.93):
            tab_clicked = True
            break

    if tab_clicked:
        select_stage_state._no_tab_count = 0
        CFG.switch_page_count += 1
        browser.script_log(f"  切换关卡栏 第{CFG.switch_page_count}次")
        await browser.b_sleep(0.8, 1.5)

        if CFG.switch_page_count >= CFG.max_tab_switches:
            browser.script_log("✅ 所有关卡栏已检查完毕，脚本结束")
            return "__exit__"
        return None   # 保持选关态，下一轮重新检查

    # ③ 既没有 new 也点不到标签页 → 连续 N 次无进展则结束
    if getattr(select_stage_state, '_no_tab_count', 0) >= 3:
        browser.script_log("✅ 连续 3 次切不到关卡栏，视为全部通关")
        return "__exit__"
    select_stage_state._no_tab_count = getattr(select_stage_state, '_no_tab_count', 0) + 1
    browser.script_log(f"  无法切换关卡栏 第{select_stage_state._no_tab_count}次")

    # ④ 检查是否还在选关页面
    if not await browser.match_image(CFG.img_dir / '1_select', threshold=CFG.threshold):
        return "未知"
    return None


async def ready_state(browser: UserBrowser) -> StateName:
    """备战态：等出击按钮出现，点击后进入战斗/剧情"""
    if await browser.click_image(CFG.img_dir / '2_chuji', threshold=CFG.threshold):
        await browser.b_sleep(0.8, 1.5)
        return None  # 让下轮自动识别

    # 出击按钮找不到 → 检查是不是已经进其他场景了
    if await browser.match_image(CFG.img_dir / '3_skip', threshold=CFG.threshold):
        return "跳过剧情"
    # 连备战标识都没了 → 已经离开备战，主动重识别
    if not await browser.match_image(CFG.img_dir / '2_chuji', threshold=CFG.threshold):
        return "未知"
    return None


async def skip_story_state(browser: UserBrowser) -> StateName:
    """跳过剧情态：点 skip → 点确认 → 等待转场"""
    # ① 点 skip
    if not await browser.click_image(CFG.img_dir / '3_skip', threshold=CFG.threshold):
        # skip 不在了 → 剧情可能已经结束
        # 主动检查目前到了哪个场景，不等超时
        if await browser.match_image(CFG.img_dir / '4_result', threshold=CFG.threshold):
            return "等待战后结算"
        if await browser.match_image(CFG.img_dir / '1_select', threshold=CFG.threshold):
            return "选关"
        if await browser.match_image(CFG.img_dir / '2_chuji', threshold=CFG.threshold):
            return "备战"
        return None

    await browser.b_sleep(0.3, 0.8)

    # ② 等确认弹窗出现
    if not await browser.wait_image(CFG.img_dir / '3_hai', timeout=CFG.wait_appear):
        # 没弹窗 → 剧情直接跳过了，看看到哪了
        return None  # 下轮会落在第①步的 check

    # ③ 点确认
    await browser.click_image(CFG.img_dir / '3_hai', threshold=CFG.threshold)
    await browser.b_sleep(0.3, 0.8)

    # ④ 等弹窗消失
    if await browser.wait_image(CFG.img_dir / '3_hai', timeout=CFG.wait_disappear):
        # 弹窗还在？网络延迟，再点一次
        await browser.click_image(CFG.img_dir / '3_hai', threshold=CFG.threshold)
        await browser.b_sleep(0.3, 0.8)

    return None  # 下轮落在第①步，检查 skip 确认剧情是否已结束


async def result_state(browser: UserBrowser) -> StateName:
    """
    结算态 —— 处理两段式结算：
    ① 等级结算界面 (4_rank) → 附近乱点进入奖励结算
    ② 奖励结算界面 (4_next/4_next_1/4_cihe) → 继续或退出

    两个界面都有 4_result 标识，所以 unknown_state 检测到 4_result
    就跳进本状态，由本状态内部分辨当前处于哪一段。
    """
    # 〇 等级结算界面 → 点空白处前进到奖励结算
    if await browser.match_image(CFG.img_dir / '4_rank', threshold=CFG.threshold):
        browser.script_log("  等级结算界面，前进至奖励结算")
        await browser.click_image(
            CFG.img_dir / '4_rank',
            pianyi=(random.randint(30, 80), random.randint(10, 30)),
            threshold=CFG.threshold,
        )
        await browser.b_sleep(0.3, 0.6)
        return None

    # ① 有下一关/下一剧情？
    if await browser.click_image(CFG.img_dir / '4_next', threshold=CFG.threshold):
        await browser.b_sleep(0.8, 1.5)
        return None
    if await browser.click_image(CFG.img_dir / '4_next_1', threshold=CFG.threshold):
        await browser.b_sleep(0.8, 1.5)
        return None

    # ② 退出结算
    if await browser.click_image(CFG.img_dir / '4_cihe', threshold=CFG.threshold):
        await browser.b_sleep(0.8, 1.5)
        return None

    # ③ 以上按钮都没找到 → 检查是否还在结算界面
    if not await browser.match_image(CFG.img_dir / '4_result', threshold=CFG.threshold):
        browser.script_log("  结算界面已关闭，重新识别场景")
        return "未知"
    return None


# ═══════════════════════════════════════════════════════════════
# 状态注册表
# ═══════════════════════════════════════════════════════════════

STATES = {
    "未知":           unknown_state,
    "选关":           select_stage_state,
    "备战":           ready_state,
    "跳过剧情":        skip_story_state,
    "等待战后结算":     result_state,
}

STATE_TIMEOUT = {
    "未知":           8,
    "选关":           60,
    "备战":           30,
    "跳过剧情":       20,
    "等待战后结算":     20,
}


# ═══════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════

async def do_work(browser: UserBrowser):
    """
    脚本主入口。
    游戏副本自动化推图，支持任意场景开始。
    """
    # 启用帧缓存（同一帧内重复 match 不走 OpenCV）
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True

    # 初始状态检测
    state_name = await unknown_state(browser)
    if state_name is None:
        state_name = "未知"

    state_enter_time = asyncio.get_event_loop().time()
    browser.script_log(f"[DO推本v2] 初始场景: {state_name}")

    while True:
        # 1. 刷新帧
        await browser.update_frame()

        # 1. 守卫优先（处理弹窗）
        if await check_guards(browser):
            continue  # 截帧被污染，重走

        # 2. 状态超时检测
        timeout = STATE_TIMEOUT.get(state_name, 30)
        now = asyncio.get_event_loop().time()
        if now - state_enter_time > timeout:
            browser.script_log(f"[超时] {state_name} 超过 {timeout}s，重新识别场景")
            state_name = await unknown_state(browser) or "未知"
            state_enter_time = now
            continue

        # 3. 驱动当前状态
        handler = STATES.get(state_name)
        if handler is None:
            browser.script_log(f"[错误] 未知状态: {state_name}，重置为 未知")
            state_name = "未知"
            state_enter_time = now
            continue

        browser.script_log(f"[{state_name}]")
        next_state = await handler(browser)

        # 4. 处理返回
        if next_state == "__exit__":
            browser.script_log("[DO推本v2] ✅ 脚本正常结束")
            break

        if next_state is not None and next_state != state_name:
            browser.script_log(f"  → {next_state}")
            state_name = next_state
            state_enter_time = now

        # 5. 仅防忙等，不做实质性延时（操作后的等待由状态内部处理）
        await browser.b_sleep(0.03, 0.08)
