from user_window import UserWindow

CHALLENGE = [[asset_3b7b8c42]]
RESULT = [[asset_ae313466]]
RESULT2 = [[asset_472dac5a]]


async def do_work(browser: UserWindow):
    # 进挑战
    await browser.wait_image(CHALLENGE)
    await browser.click_image(CHALLENGE)
    await browser.b_sleep(1.0)

    # 结算
    await browser.wait_image(RESULT)
    await browser.click_image(RESULT)
    await browser.b_sleep(1.0)

    # 结算2
    await browser.wait_image(RESULT2)
    await browser.click_image(RESULT2)
    await browser.b_sleep(1.0)