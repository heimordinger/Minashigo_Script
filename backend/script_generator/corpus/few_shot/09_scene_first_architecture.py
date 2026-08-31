# Few-shot: 先识场景、再决策行为（scene-first architecture）
#
# 1) unknown_state = 只认屏（gather 全部场景 id），禁止 click / return '未知'
# 2) STATES 键 = 任务步骤或导航辅助，不是「认到 rank 就 return 主界面」的自环
# 3) do_work 超时 / 恢复时先 unknown_state，再跑步骤 handler

async def unknown_state(browser) -> StateName:
    """场景层：我在哪？只 match，不操作。"""
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
            return name
    await browser.b_sleep(1.5, 2.5)
    return None


async def step_房间领体力(browser) -> StateName:
    """行为层：在房间界面才收奖励；不认屏、只做事。"""
    if not await browser.match_image(_img("room_logo"), threshold=CFG.nav_threshold):
        return "未知"
    if await browser.click_image(_img("room_收取奖励"), threshold=CFG.threshold):
        await browser.b_sleep(1.5, 1.5)
        return "step_房间确认"
    return "__exit__"


async def do_work(browser: UserBrowser):
    step = "未知"
    se_time = asyncio.get_event_loop().time()
    while True:
        await browser.update_frame()
        scene = await unknown_state(browser)
        if scene is None:
            step = "未知"
        elif step == "未知" and scene == "主界面":
            step = "step_房间领体力"  # 场景→步骤：在主界面则开始房间任务
        browser.note_state(step)
        handler = STATES.get(step)
        if handler is None:
            step = "未知"
            continue
        nxt = await handler(browser)
        if nxt == "__exit__":
            break
        if nxt:
            step = nxt
            se_time = asyncio.get_event_loop().time()
        await browser.b_sleep(0.05, 0.15)
