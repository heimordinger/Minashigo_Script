from backend.browser.user_browser import UserBrowser


async def do_work(browser:UserBrowser):
    browser.script_log(await browser.rect())