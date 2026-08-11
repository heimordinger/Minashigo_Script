import asyncio

from backend.browser.user_browser import UserBrowser
from core.path import PROJECT_ROOT


async def do_work(browser:UserBrowser):
    from core.path import IMG_PATH
    print("进入任务(点击样例.py)")
    my_path = IMG_PATH / '点击样例'
    url = f"file://{str(PROJECT_ROOT)}/click.html"
    imgs = [
            {
                'ttk_path': 'Click_case',
                'threshold': 0.8,
                'pianyi': (0, 0),
            }
    ]
    if browser.url != url:
        await browser.goto(f"file:///{PROJECT_ROOT}/click.html")
    for _ in range(5):
        dpr = await browser.page.evaluate("window.devicePixelRatio")
        vp = await browser.page.evaluate("({w: window.innerWidth, h: window.innerHeight})")
        browser.script_log(f"DPR={dpr}  Viewport={vp['w']}×{vp['h']}")

        await browser.update_frame()
        for img in imgs:
            # 先查坐标再点，方便对比
            match = await browser.match_image(
                my_path / img['ttk_path'],
                threshold=img['threshold'])
            if match:
                browser.script_log(f"  匹配位置: ({match.x}, {match.y}) score={match.max_val:.3f}")
            result = await browser.click_image(
                img_path=my_path/img['ttk_path'],
                threshold=img['threshold'],
                pianyi=img['pianyi'])
            browser.script_log(f"  点击返回: {result}")

        # 读取页面显示的实际点击坐标
        page_x = await browser.page.evaluate("document.getElementById('click-coords').textContent")
        page_y = await browser.page.evaluate("document.getElementById('current-button').textContent")
        browser.script_log(f"  页面显示: {page_x}  {page_y}")
        await asyncio.sleep(0.25)

        # await browser.update_frame()
        # result = await browser.match_text(text="点")
        # if result["x"] is None:
        #     print("未识别到文字，最大匹配度:",result["max_val"])
        # else:
        #     await browser.click(result["x"],result["y"])
        # await browser.b_sleep(0.5)

    browser.script_log(msg="已完成点击样例")