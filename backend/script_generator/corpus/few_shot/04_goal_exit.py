# Few-shot: 业务目标达成才 __exit__；导航成功禁止 __exit__
#
# 用语：
# - 「本步骤结束」= 辅助/导航成功 → return 业务态名（如 '主界面'），继续当前任务
# - 「本任务完成」= 当前 run_task 业务做完 → return '__exit__'（多任务时 do_work 接下一个）
# - 整段脚本目标（如登录出现 rank）→ 也可 __exit__（单任务 FSM）
# - 介绍里「执行@返回主界面」= return '返回主界面'（走状态表），不是再写一套回家逻辑

# —— 多任务：导航成功 = 本步骤结束，绝不是本任务完成 ——
async def go_home_state(browser) -> StateName:
    browser.script_log("[go_home]")
    if await browser.match_image(_img("rank"), threshold=CFG.nav_threshold):
        browser.script_log("  already home -> 本步骤结束，进入主界面业务态")
        return "主界面"  # NOT __exit__
    if await browser.click_image(_img("home"), threshold=CFG.threshold):
        ok = await browser.wait_image(_img("rank"), timeout=30)
        if ok:
            return "主界面"
        return "未知"
    return "未知"  # 禁止 return None，否则会卡在辅助态不重路由


# —— 返回出击：点不到出击.png → @返回主界面（房间里没有出击键）——
async def go_sortie_state(browser) -> StateName:
    browser.script_log("[go_sortie]")
    if await browser.match_image(_img("出击_logo"), threshold=CFG.nav_threshold):
        return "出击界面"
    if await browser.click_image(_img("出击"), threshold=CFG.threshold):
        ok = await browser.wait_image(_img("出击_logo"), timeout=30)
        if not ok:
            return "未知"
        return "出击界面"
    # 介绍原文：点击出击.png，如果没匹配到则执行@返回主界面
    return "返回主界面"


# —— 多任务：房间业务真正做完 = 本任务 __exit__ ——
async def room_done_example(browser) -> StateName:
    if not await browser.match_image(_img("room_收取奖励"), threshold=CFG.threshold):
        if not await browser.match_image(_img("room_ok"), threshold=CFG.threshold):
            browser.script_log("  无奖励可领 -> 本任务完成")
            return "__exit__"  # 只结束当前 run_task
    return None


# —— 单任务登录：出现 rank = 整段目标达成 → __exit__ 可以 ——
async def login_finish_state(browser) -> StateName:
    if await browser.match_image(_img("rank"), threshold=CFG.nav_threshold):
        browser.script_log("  login goal met -> __exit__")
        return "__exit__"
    return None
