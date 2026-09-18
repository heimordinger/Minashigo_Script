import os

IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")


async def do_work(browser):
    await browser.wait_image(os.path.join(IMG_DIR, "challenge.png"), timeout=3)
    await browser.click_image(os.path.join(IMG_DIR, "challenge.png"))
    await browser.wait_image(os.path.join(IMG_DIR, "settle.png"), timeout=11)
    await browser.click_image(os.path.join(IMG_DIR, "settle.png"))
    await browser.b_sleep(1)