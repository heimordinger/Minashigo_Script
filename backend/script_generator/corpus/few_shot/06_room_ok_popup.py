# Few-shot: 领取奖励 — 弹窗循环；点收取后无确认窗则计数，达限 __exit__

async def room_claim_state(browser) -> StateName:
    browser.script_log("[room][claim]")
    await browser.update_frame()
    if await browser.match_image(_img("room_ap上限"), threshold=CFG.nav_threshold):
        browser.script_log("  AP 上限，本任务完成")
        return "__exit__"

    if await browser.match_image(_img("room_ok"), threshold=CFG.threshold):
        browser.script_log("  仅有 room_ok 弹窗，先关闭")
        for _ in range(5):
            if not await browser.click_image(_img("room_ok"), threshold=CFG.threshold):
                break
            await browser.b_sleep(0.5, 0.8)
            await browser.update_frame()
            if not await browser.match_image(_img("room_ok"), threshold=CFG.threshold):
                break
        await browser.update_frame()
        if not await browser.match_image(_img("room_收取奖励"), threshold=CFG.threshold):
            browser.script_log("  关 ok 后无收取，本任务完成")
            return "__exit__"

    if not await browser.match_image(_img("room_收取奖励"), threshold=CFG.threshold):
        browser.script_log("  无收取按钮且无 ok，本任务完成")
        return "__exit__"

    max_no_ok = 3
    for attempt in range(1, max_no_ok + 1):
        if not await browser.click_image(_img("room_收取奖励"), threshold=CFG.threshold):
            return None
        browser.script_log(f"  已点收取，等待 ok ({attempt}/{max_no_ok})")
        await browser.b_sleep(1.5, 1.5)
        await browser.update_frame()
        if await browser.match_image(_img("room_ok"), threshold=CFG.threshold):
            while await browser.match_image(_img("room_ok"), threshold=CFG.threshold):
                await browser.click_image(_img("room_ok"), threshold=CFG.threshold)
                await browser.b_sleep(0.5, 0.8)
                await browser.update_frame()
            browser.script_log("  无 ok，本任务完成")
            return "__exit__"
        if attempt >= max_no_ok:
            browser.script_log("  无 ok 累计达上限，本任务完成")
            return "__exit__"
    return None
