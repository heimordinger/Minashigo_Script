# Few-shot: 帧新鲜度由运行时保证；update_frame 可选；可用 request_fps 提频
# click / b_sleep 会 invalidate；match_image 前 ensure。wait_image 仍适合转场确认。

async def claim_then_check_ok(browser) -> StateName:
    browser.script_log("[claim]")
    # 可选：本脚本需要高于默认观察频率时声明（任务结束 runner 会 release script）
    # await browser.request_fps(30)
    if await browser.click_image(_img("room_收取奖励"), threshold=CFG.threshold):
        await browser.b_sleep(1.5, 1.5)
        # 不必强制 update_frame；若要显式同帧连判可写一行
        if await browser.match_image(_img("room_ok"), threshold=CFG.threshold):
            await browser.click_image(_img("room_ok"), threshold=CFG.threshold)
            await browser.b_sleep(0.5, 0.8)
            return "房间领体力"
        return "房间领体力"
    # 转场确认：用 wait_image
    if await browser.click_image(_img("room"), threshold=CFG.threshold):
        ok = await browser.wait_image(_img("room_logo"), timeout=15)
        if not ok:
            return "未知"
        return "房间领体力"
    return None
