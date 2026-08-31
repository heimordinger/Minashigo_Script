from backend.browser.user_browser import UserBrowser


async def do_work(browser:UserBrowser):
    from core.path import IMG_PATH
    my_path = IMG_PATH / '测试'
    await browser.update_frame()
    re = await browser.click_image(my_path / 'jjc_刷新.png', threshold=0.9, pianyi=(-200, 0))
    browser.script_log(f"点击结果: {re}")