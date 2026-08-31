# Few-shot: 多任务 — 每个 TASK_*_STATES 自带导航 + 唯一入口点击
#
# CRITICAL — 枢纽入口禁止 return 自身：
# 「出击界面」绑定 enter_arena_state 时，match 到 出击_logo 必须点本任务入口
# （jjc/ta），再 return 下一业务态；禁止 return '出击界面'（会空转死循环）。

TASK_ROOM_STATES = {
    "未知": unknown_state,
    "返回主界面": go_home_state,
    "主界面": enter_room_state,      # 只点房间入口
    "房间领体力": claim_room_state,
}
TASK_ROOM_TIMEOUT = {"未知": 180, "返回主界面": 90, "主界面": 30, "房间领体力": 60}

TASK_ARENA_STATES = {
    "未知": unknown_state,
    "返回出击界面": go_sortie_state,
    "出击界面": enter_arena_state,  # 只点竞技场入口，不要顺带点爬塔
    "竞技场": arena_fight_state,
}
TASK_ARENA_TIMEOUT = {"未知": 180, "返回出击界面": 90, "出击界面": 30, "竞技场": 120}


async def enter_arena_state(browser) -> StateName:
    """出击枢纽：点 jjc；已在枢纽也要点，绝不能 return '出击界面'。"""
    browser.script_log("[arena][entry]")
    if await browser.match_image(_img("jjc_logo"), threshold=CFG.nav_threshold):
        return "竞技场"
    # 已在出击界面 / 尚在出击界面：都走入口点击
    if await browser.click_image(_img("jjc"), threshold=CFG.threshold):
        ok = await browser.wait_image(_img("jjc_logo"), timeout=30)
        return "竞技场" if ok else "未知"
    if not await browser.match_image(_img("出击_logo"), threshold=CFG.nav_threshold):
        return "未知"
    return None  # 仍在出击界面，下一轮再试点；禁止 return '出击界面'


def _task_entry_state(states: dict, task_name: str = "") -> str:
    """竞技场/塔优先返回出击界面；房间优先返回主界面。"""
    if task_name in ("arena", "tower") and "返回出击界面" in states:
        return "返回出击界面"
    if "返回主界面" in states:
        return "返回主界面"
    if "返回出击界面" in states:
        return "返回出击界面"
    return "未知"

async def run_task(browser, task_name: str, states: dict, timeouts: dict):
    state_name = _task_entry_state(states, task_name)
    # 启动时应先 unknown_state bootstrap，已在目标界面则直达业务步
    # ... FSM loop ...

async def do_work(browser: UserBrowser):
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True
    tasks = [
        ("room", TASK_ROOM_STATES, TASK_ROOM_TIMEOUT),
        ("arena", TASK_ARENA_STATES, TASK_ARENA_TIMEOUT),
    ]
    for name, st, to in tasks:
        for attempt in range(3):
            result = await run_task(browser, name, st, to)
            if result == "__task_failed__":
                browser.script_log(f"[{name}] failed ({attempt+1}/3)")
                await browser.b_sleep(1.0, 2.0)
                continue
            browser.script_log(f"[{name}] done")
            break
        else:
            browser.script_log(f"[{name}] abort after retries")
            break
