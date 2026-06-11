import asyncio

from backend.automation.user_window import UserWindow
from backend.browser.user_browser import UserBrowser
from core.path import PROJECT_ROOT


async def do_work(browser: UserW):
    from core.path import IMG_PATH
    print("进入任务(窗口点击测试.py)")
    my_path = IMG_PATH / '点击样例'
    imgs = [
            {
                'ttk_path': 'Click_case2',
                'threshold': 0.8,
                'pianyi': (0, 0),
            },
            {
                'ttk_path': 'Click_case',
                'threshold': 0.8,
                'pianyi': (0, 0),
            }
    ]
    for _ in range(5):
        await browser.update_frame()
        for img in imgs:
            await browser.click_image(
                img_path=my_path/img['ttk_path'],
                threshold=img['threshold'],
                pianyi=img['pianyi'])
        await asyncio.sleep(0.25)

    browser.script_log(msg="已完成点击样例")