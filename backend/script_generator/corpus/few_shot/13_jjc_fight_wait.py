# Few-shot: 竞技场 — 点击 jjc_出击 后须长时间等待战斗/过场 loading，禁止短 sleep 后信 jjc_logo

STEP_TIMEOUT_JJC_FIGHT = 480.0


async def step_竞技场等待战斗(browser) -> StateName:
    """出击后独立等待步：名称含「等待」供 stuck_guard 识别。"""
    browser.script_log(
        f"[jjc] 已出击，等待战斗/加载（最多 {STEP_TIMEOUT_JJC_FIGHT:.0f}s）…"
    )
    deadline = asyncio.get_event_loop().time() + 60.0
    left_prep = False
    while asyncio.get_event_loop().time() < deadline:
        await browser.update_frame()
        if not await browser.match_image(_img("jjc_出击"), threshold=CFG.threshold):
            left_prep = True
            break
        await browser.b_sleep(0.6, 1.0)
    if not left_prep:
        return None

    fight_start = asyncio.get_event_loop().time()
    end = fight_start + STEP_TIMEOUT_JJC_FIGHT
    min_fight = 15.0
    while asyncio.get_event_loop().time() < end:
        await browser.update_frame()
        if await browser.match_image(_img("jjc_ok"), threshold=CFG.threshold):
            return "竞技场结算"
        if await browser.match_image(_img("jjc_结算"), threshold=CFG.nav_threshold):
            return "竞技场结算"
        elapsed = asyncio.get_event_loop().time() - fight_start
        if elapsed >= min_fight:
            if (
                await browser.match_image(_img("jjc_logo"), threshold=CFG.nav_threshold)
                and not await browser.match_image(_img("jjc_出击"), threshold=CFG.threshold)
                and await browser.match_image(_img("jjc_段位"), threshold=CFG.threshold)
            ):
                return "检查次数"
        await browser.b_sleep(1.5, 2.5)
    return None


async def step_竞技场出击(browser) -> StateName:
    if not await browser.click_image(_img("jjc_出击"), threshold=CFG.threshold):
        return None
    return "竞技场等待战斗"


async def step_刷新倍率(browser) -> StateName:
    """jjc_刷新 偏移 (-200,0) 点倍率区，直到 jjc_倍率 出现。"""
    if await browser.match_image(_img("jjc_倍率"), threshold=CFG.threshold):
        return "选择段位"
    if await browser.click_image(
        _img("jjc_刷新"), pianyi=(-200, 0), threshold=CFG.threshold
    ):
        await browser.b_sleep(0.3, 0.5)
        return "刷新倍率"
    return None


TASK_JJC_STATES = {
    "竞技场出击": step_竞技场出击,
    "竞技场等待战斗": step_竞技场等待战斗,
    "竞技场结算": step_竞技场结算,
}
