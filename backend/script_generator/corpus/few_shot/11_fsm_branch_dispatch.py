# Few-shot: FSM 分支 — handler return 值必须可路由（场景名≠步骤名时要 resolve）
#
# 问题：helper return「主界面」但 STATES 无「主界面」→ handler None → 分支断裂。
# 解法 A：STATES 注册场景桩（主界面→下一步）；解法 B：run_task 内 SCENE_TO_STEP 统一 resolve。

# 每任务：场景名（unknown / 辅助成功）→ 该任务第一步业务态
TASK1_SCENE_TO_STEP = {
    "主界面": "进入房间",
    "房间界面": "房间领体力",
}
TASK2_SCENE_TO_STEP = {
    "主界面": "返回主界面",
    "出击界面": "进入竞技场",
    "竞技场界面": "检查次数",
}
TASK3_SCENE_TO_STEP = {
    "主界面": "返回主界面",
    "出击界面": "进入塔",
    "塔界面": "检查塔次数",
}


def _resolve_state(nxt: str, states: dict, scene_map: dict) -> Optional[str]:
    """场景名优先 SCENE_TO_STEP，再步骤名直用。"""
    if not nxt:
        return None
    step = scene_map.get(nxt)
    if step and step in states:
        return step
    if nxt in states:
        return nxt
    return None


async def stub_主界面_to_room(browser) -> StateName:
    """场景桩：已在主界面 → 本任务入口步（禁止只 match rank return 主界面自环）。"""
    return "进入房间"


async def stub_出击界面_to_arena(browser) -> StateName:
    return "进入竞技场"


async def stub_room_to_claim(browser) -> StateName:
    return "房间领体力"


TASK1_STATES = {
    "未知": unknown_state_scene_only,
    "返回主界面": helper_返回主界面,
    "主界面": stub_主界面_to_room,       # helper 成功 return 主界面 时必需
    "房间界面": stub_room_to_claim,          # 或 resolve → 房间领体力
    "进入房间": task1_进入房间,
    "房间领体力": task1_房间领体力,
}


async def run_task(browser, task_name: str, states: dict, timeouts: dict, scene_map: dict):
    state_name = _task_entry_state(states)
    se_time = asyncio.get_event_loop().time()
    start = se_time
    while True:
        await browser.update_frame()
        if await check_guards(browser):
            continue
        now = asyncio.get_event_loop().time()
        if now - start > CFG.total_timeout:
            return "__task_failed__"
        # 超时分支：先识场景再 resolve，禁止直接 __task_failed__
        if now - se_time > timeouts.get(state_name, 180):
            scene = await unknown_state_scene_only(browser)
            resolved = _resolve_state(scene or "未知", states, scene_map)
            if resolved:
                state_name = resolved
                se_time = now
                continue
            state_name = "未知"
            se_time = now
            continue
        handler = states.get(state_name)
        if handler is None:
            scene = await unknown_state_scene_only(browser)
            resolved = _resolve_state(scene or state_name, states, scene_map)
            if resolved:
                state_name = resolved
                se_time = now
                continue
            return "__task_failed__"
        nxt = await handler(browser)
        if nxt == "__exit__":
            return "__done__"
        resolved = _resolve_state(nxt, states, scene_map)
        if resolved:
            state_name = resolved
            se_time = now
        elif nxt is None:
            pass  # 保持当前态
        await browser.b_sleep(0.05, 0.15)
