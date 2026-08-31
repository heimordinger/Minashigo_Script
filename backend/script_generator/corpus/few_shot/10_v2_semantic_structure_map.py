# Few-shot: v2 介绍语义 → 结构（推本 scene_fsm + 日常 run_task 骨架）
#
# 推本：unknown 命中即业务态名；超时重识。日常：unknown 只 return 场景名；run_task 分任务分步。

async def unknown_state_scene_only(browser) -> StateName:
    """场景层：介绍「可作为…标识图」→ 只 return 场景名，不 click。"""
    cs = {
        "主界面": _img("rank"),
        "出击界面": _img("出击_logo"),
        "房间界面": _img("room_logo"),
        "竞技场界面": _img("jjc_logo"),
        "塔界面": _img("ta_logo"),
    }
    rs = await asyncio.gather(*[
        browser.match_image(p, threshold=CFG.nav_threshold) for p in cs.values()
    ])
    for name, hit in zip(cs.keys(), rs):
        if hit:
            browser.script_log(f"[scene] {name}")
            return name  # 场景名，不是 task2_竞技场
    await browser.b_sleep(1.5, 2.5)
    return None


async def unknown_state_push_v2(browser) -> StateName:
    """推本 v2：介绍场景顺序 (1)选关 (2)备战 → 命中 return 业务态 handler 名。"""
    cs = {
        "选关": _img("1_select"),
        "跳过剧情": _img("3_skip"),
        "等待战后结算": _img("4_result"),
        "备战": _img("2_chuji"),
    }
    rs = await asyncio.gather(*[
        browser.match_image(p, threshold=CFG.threshold) for p in cs.values()
    ])
    for name, hit in zip(cs.keys(), rs):
        if hit:
            return name
    await browser.b_sleep(1.5, 2.5)
    return None


def _task_entry_state(states: dict) -> str:
    """介绍「执行@返回出击界面」→ run_task 从辅助态起跑，不是硬编码 未知。"""
    if "返回出击界面" in states:
        return "返回出击界面"
    if "返回主界面" in states:
        return "返回主界面"
    return "未知"


async def run_task(browser, task_name: str, states: dict, timeouts: dict):
    state_name = _task_entry_state(states)
    # ... FSM loop: handler return __exit__ 仅本任务完成


TASK2_STATES = {
    "未知": unknown_state_scene_only,
    "返回出击界面": helper_返回出击界面,
    "进入竞技场": task2_进入竞技场,
    "检查次数": task2_检查次数,
    "刷新倍率": task2_刷新倍率,
    "战斗结算": task2_战斗结算,
    # 禁止 task2_竞技场 单体函数包办以上全部步骤
}


async def do_work_daily_v2(browser: UserBrowser):
    """日常：仅 run_task 顺序；scene 恢复在 run_task 内消费 unknown 结果。"""
    tasks = [
        ("room", TASK1_STATES, TASK1_TIMEOUT),
        ("arena", TASK2_STATES, TASK2_TIMEOUT),
        ("tower", TASK3_STATES, TASK3_TIMEOUT),
    ]
    for name, st, to in tasks:
        result = await run_task(browser, name, st, to)
        if result != "__done__":
            break


async def do_work_push_v2(browser: UserBrowser):
    """推本 v2：单循环；超时 → unknown_state() 重识。"""
    state_name = await unknown_state_push_v2(browser) or "未知"
    # on timeout: state_name = await unknown_state_push_v2(browser) or "未知"
