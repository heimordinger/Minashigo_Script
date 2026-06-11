from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH
from scripts.孤儿推本 import ap_recovery, mnsg_ttk, mnsg_ttk_after

img_path = IMG_PATH / 'minashigo' / '孤儿raid'
p = {"火": 1, "水": 2, "风": 3, "雷": 4, "光": 5, "暗": 6, "紧急": 7}


async def raid_battle_zd(browser: UserBrowser, *, difficulty: str):
    click_max_count = 0
    in_zd = False
    difficultys = ["hard", "extra"]
    if difficulty not in difficultys:
        raise KeyError("难度关键词不存在")
    while True:
        await browser.update_frame()
        if not in_zd:
            if (not await browser.match_image(img_path / f"1_{difficulty}") and
                    await browser.match_image(img_path / f"1_{difficulty}_ap")):
                await browser.click_image(img_path / f"1_{difficulty}_ap", pianyi=(0, 30))
                await browser.b_sleep(1)
                continue
            if await browser.match_image(img_path / "1_tzhs_0", threshold=0.92):
                return False
            if await browser.click_image(img_path / "1_zd"):
                await browser.b_sleep(3)
                continue

        if await browser.match_image(IMG_PATH / "minashigo" / "孤儿推本" / "AP恢复" / "ap_AP恢复"):
            await ap_recovery(browser)

        if await browser.click_image(img_path / "1_zd_max"):
            in_zd = True
            await browser.b_sleep(0.5)
            click_max_count -= -1
            if click_max_count >= 3:
                await browser.click_image(img_path / "1_zd_+")
                await browser.b_sleep(0.8, 1)
                click_max_count = 0
                continue

        if await browser.match_image(img_path / "1_zd_maxed", use_color_check=True):
            await browser.click_image(img_path / "1_zd_cj")
            await browser.b_sleep(2)
            return True


async def ultra(browser: UserBrowser):
    pass


async def select_battle(browser: UserBrowser, start_p, p_p):
    s_x, s_y = start_p.x, start_p.y
    global p
    await browser.click(x=s_x + p[p_p] * 85 - 165, y=s_y + 100)
    await browser.b_sleep(1)


async def do_work(browser: UserBrowser):
    #browser.use_polling_temp_cache = True
    global p
    for i, _ in list(p.items())[:6]:
        await browser.update_frame()
        s_p = await browser.match_image(img_path / "1_raid")
        if s_p:
            browser.script_log("raid主界面")
            await select_battle(browser, s_p, i)
            if not await raid_battle_zd(browser, difficulty="extra"):
                continue
            await mnsg_ttk(browser)
            while not await browser.match_image(img_path / "2_zdwl"):
                await browser.update_frame()
                await browser.b_sleep(0.5)
            await browser.click_image(img_path / "2_ok")
            await mnsg_ttk_after(browser)
            await browser.wait_image(img_path / '1_ultra')
            if not await raid_battle_zd(browser, difficulty="hard"):
                continue
            await mnsg_ttk(browser)
