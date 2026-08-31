# Few-shot: 点击触发转场后必须 wait_image 确认下一场景

async def ready_state(browser) -> StateName:
    browser.script_log("[ready] try sortie")
    if await browser.click_image(_img("2_chuji"), threshold=CFG.threshold):
        browser.script_log("  clicked, waiting next scene")
        ok = await browser.wait_image(_img("3_skip"), timeout=STATE_TIMEOUT.get("备战", 30))
        if not ok:
            browser.script_log("  transition timeout -> unknown")
            return "未知"
        return "跳过剧情"
    if not await browser.match_image(_img("2_chuji"), threshold=CFG.nav_threshold):
        return "未知"
    return None
