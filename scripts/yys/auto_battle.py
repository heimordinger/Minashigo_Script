async def do_work(browser: UserWindow):
    # 1. 挑战
    await browser.wait_image("挑战")
    await browser.click_image("挑战")
    await browser.b_sleep(1.0)

    # 2. 结算
    await browser.wait_image("结算")
    await browser.click_image("结算")
    await browser.b_sleep(1.0)

    # 3. 结算2
    await browser.wait_image("结算2")
    await browser.click_image("结算2")
    await browser.b_sleep(1.0)