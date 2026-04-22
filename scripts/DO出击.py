import asyncio

from classes.Browser import Browser
from core.path import do_img_path
from time import sleep
do_chuji_path = do_img_path / 'DO出击'


async def do_chuji(browser: Browser):
    my_state = '无'
    browser.screenshot()
    if all(browser.image_match(
            Target_path=do_chuji_path / '出击',
            threshold=0.96
    )):
        a = browser.image_match(
            Target_path=do_chuji_path / '出击',
            threshold=0.96
        )
        await browser.click(a[0],a[1])
        sleep(1)
        browser.screenshot()
        while True:
            if all(browser.image_match(
                    Target_path=do_chuji_path / '体力不足',
                    threshold=0.99
            )):
                my_state = '体力不足'

            if all(browser.image_match(
                    Target_path=do_chuji_path / '连续出击',
                    threshold=0.99
            )):
                my_state = '连续出击'

            if all(browser.image_match(
                Target_path=do_chuji_path/'进度条',
                threshold=0.99
            )) or all(browser.image_match(
                Target_path=do_chuji_path/'auto',
                threshold=0.99
            )) or all(browser.image_match(
                Target_path=do_chuji_path/'结算rank',
                threshold=0.99
            )) or all(browser.image_match(
                Target_path=do_chuji_path/'完成连续出击'
            )):
                my_state = '结束'

            print('当前状态:', my_state)
            match my_state:
                case '体力不足':
                    await chiyao(browser)
                case '连续出击':
                    await lianxvchuji(browser)
                case '结束':
                    await jieshu(browser)
                    break
            my_state = '无'
            await wait_to_click(
                browser = browser,
                Target_path=do_chuji_path/'出击',
                threshold=0.96,
                max_iterations= 10,
            )

async def chiyao(browser: Browser):
    await wait_to_click(
        browser = browser,
        Target_path = do_chuji_path/'加号',
        threshold=0.9,
        match_order = 1
    )
    while True:
        await wait_to_click(
            browser = browser,
            Target_path = do_chuji_path/'+5',
            match_order = 1
        )
        if browser.image_match(
            Target_path= do_chuji_path/'使用药剂'
        ):
            break
    await wait_to_click(
        browser = browser,
        Target_path = do_chuji_path/'使用药剂'
    )

async def wait_to_click(
    browser: Browser,
    Target_path = None,
    threshold = 0.99,
    match_order = 1,
    pianyi = (0, 0),
    max_iterations = 0,
    delay = 1.0,
) -> bool:
    """
    等待并点击目标图片，支持最大尝试次数限制。

    Args:
        browser: 浏览器实例
        Target_path: 目标图片路径
        threshold: 匹配阈值（0~1）
        match_order: 匹配顺序（如多个匹配结果时选择第几个）
        pianyi: 点击偏移量 (x, y)
        max_iterations: 最大尝试次数（0=无限）
        delay: 每次尝试间隔时间（秒）

    Returns:
        bool: 是否成功点击（True=成功，False=失败）
    """
    if Target_path is None:
        return False

    count = 0
    while True:
        # 如果 max_iterations > 0 且达到最大次数，退出
        if max_iterations > 0 and count >= max_iterations:
            return False

        browser.screenshot()
        a = browser.image_match(
            Target_path=Target_path,
            threshold=threshold,
            match_order=match_order,
            pianyi=pianyi,
        )

        if all(a):  # 如果匹配成功
            await browser.click(a[0], a[1])
            return True

        count += 1
        if delay > 0:  # 避免频繁请求，增加延迟
            await asyncio.sleep(delay)

async def lianxvchuji(browser: Browser):
    await wait_to_click(
        browser = browser,
        Target_path=do_chuji_path/'max',
        max_iterations = 10,
    )
    sleep(0.3)
    await wait_to_click(
        browser = browser,
        Target_path=do_chuji_path/'确定',
        max_iterations = 10,
    )

async def do_work(browser: Browser):
    await do_chuji(browser)


async def jieshu(browser: Browser):
    target_paths = [
        do_chuji_path / 'jieshu_ok',
        do_chuji_path / 'jieshu_次he'
    ]
    while True:
        for path in target_paths:
            a = browser.image_match(
                Target_path=path,
                threshold=0.99,
            )

            if a[0] and a[1] :
                await browser.click(a[0], a[1])

        if all(browser.image_match(
            Target_path=do_chuji_path/'jieshu_menu',
            threshold=0.99
        )):
            break