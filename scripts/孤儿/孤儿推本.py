import asyncio
import random
from datetime import datetime, timedelta

from backend.browser.user_browser import UserBrowser
from core.path import IMG_PATH

img_path = IMG_PATH / "minashigo" / "孤儿推本"


async def ap_recovery(browser: UserBrowser, timeout=90, verification_time=10):
    ap_path = IMG_PATH / "minashigo" / "孤儿推本" / "AP恢复"

    browser.script_log(f"等待进入AP恢复界面，场景校验 {verification_time} 秒")
    verify_start = datetime.now()
    while datetime.now() - verify_start < timedelta(seconds=verification_time):
        if await browser.match_image(ap_path / "ap_AP恢复"):
            browser.script_log("检测到AP恢复界面")
            break
        await asyncio.sleep(0.5)  # 避免过于频繁的检测
    else:
        browser.script_log(f"等待 {verification_time} 秒后仍未检测到AP恢复界面")
        return False

    browser.script_log(f"开始执行AP恢复操作，总超时 {timeout} 秒")
    operation_start = datetime.now()
    while await browser.match_image(ap_path / "ap_AP恢复"):
        if not datetime.now() - operation_start < timedelta(seconds=timeout):
            browser.script_log("AP恢复超时")
            return False
        await browser.update_frame()
        if len(await browser.match_image_multi(img_path=ap_path / 'ap_1_sy')) == 2:
            start = await browser.match_image(img_path=ap_path / 'ap_1_sy', match_select="bottom")
            start_co = (start.x, start.y)
            if all(start_co):
                end = await browser.match_image(img_path=ap_path / 'ap_1_sy', match_select="top")
                end_co = (end.x, end.y)
                await browser.slide(coordinate1=start_co,
                                    coordinate2=end_co,
                                    hold_before=round(random.uniform(0.2, 0.5), 3),
                                    hold_after=round(random.uniform(0.1, 0.5), 3),
                                    slide_time=round(random.uniform(0.5, 1.5), 3))
                continue
        await browser.click_image(ap_path / 'ap_1_sy', use_color_check=True, match_select="bottom")
        if await browser.match_image(img_path=ap_path / 'ap_2_hf'):
            if await browser.match_image(img_path=ap_path / 'ap_2_st'):
                return False
            await browser.click_image(img_path=ap_path / 'ap_2_hf')

    return True


async def mnsg_info(browser: UserBrowser):
    ttk_path = IMG_PATH / "minashigo" / "孤儿推本" / "进入战斗"
    p_path = ttk_path / "属性"
    counts = {}

    for name, folder in browser.minashigo_attrs.items():
        counts[name] = len(await browser.match_image_multi(p_path / f"2_{folder}"))
    if not any(counts.values()):
        return False

    browser.minashigo_info["属性"] = max(counts, key=counts.get)
    browser.script_log(f"确定属性:{browser.minashigo_info['属性']}")


async def select_summon(browser: UserBrowser):
    """
    _1是未选择状态，_2是已选择状态
    因为存在官方设置的UI点击特效，如果在特效生效时间内检测的话会导致两个图片都匹配不到，因此只有匹配到了_2图片再确定已选择完
    """
    ttk_path = IMG_PATH / "minashigo" / "孤儿推本" / "进入战斗"
    if await browser.match_image(ttk_path / "属性" / f"1_{browser.minashigo_attrs[browser.minashigo_info['属性']]}_2"):
        return True
    await browser.click_image(ttk_path / "属性" / f"1_{browser.minashigo_attrs[browser.minashigo_info['属性']]}_1")
    return False


async def mnsg_ttk(browser: UserBrowser, timeout=120, verification_time=10):
    ttk_path = IMG_PATH / "minashigo" / "孤儿推本" / "进入战斗"
    Scene = ["选好友战神", "出击队伍"]
    selected_summon = False
    start_time = datetime.now()

    # 第一阶段：等待进入场景（任意一个目标场景）
    browser.script_log(f"等待进入战斗场景，最长等待 {verification_time} 秒")
    verify_start = datetime.now()
    target_scene_detected = False

    while datetime.now() - verify_start < timedelta(seconds=verification_time):
        await browser.update_frame()

        if await browser.match_image(ttk_path / "1_决定"):
            target_scene_detected = True
            browser.script_log(
                f"检测到场景: 选好友战神 (耗时: {(datetime.now() - verify_start).total_seconds():.1f}秒)")
            break
        elif await browser.match_image(ttk_path / "2_team"):
            target_scene_detected = True
            browser.script_log(f"检测到场景: 出击队伍 (耗时: {(datetime.now() - verify_start).total_seconds():.1f}秒)")
            break

        await asyncio.sleep(0.3)  # 避免过于频繁的检测

    if not target_scene_detected:
        browser.script_log(f"✗ {verification_time}秒内未检测到目标战斗场景")
        return False

    while datetime.now() < start_time + timedelta(seconds=timeout):
        await browser.update_frame()
        now_Scene = None
        if await browser.match_image(ttk_path / "1_决定"):
            now_Scene = Scene[0]

        elif await browser.match_image(ttk_path / "2_team"):
            now_Scene = Scene[1]

        browser.script_log(f"mnsg_ttk:{now_Scene}")

        if now_Scene == "选好友战神":
            need_select_summon = (
                    browser.minashigo_info['属性'] is not None
                    and not selected_summon
            )

            if need_select_summon:
                if await select_summon(browser):
                    selected_summon = True
            else:
                await browser.click_image(ttk_path / "1_决定")

        elif now_Scene == "出击队伍":
            if browser.minashigo_info['属性'] is None:
                await mnsg_info(browser)
            elif selected_summon:
                await browser.click_until_gone(ttk_path / "2_出击")
                return True
            else:
                await browser.click_image(ttk_path / "2_back")


async def mnsg_ttk_after(browser: UserBrowser, timeout=60, verification_time=5):
    """
    战后结算界面操作
    """
    p = IMG_PATH / "minashigo" / "孤儿推本" / "战斗结算"
    operated_on_retry_page = False  # 是否在“再战页”执行过操作

    # 第一阶段：等待进入结算界面
    browser.script_log(f"等待进入战斗结算界面，最长等待 {verification_time} 秒")
    verify_start = datetime.now()
    settlement_detected = False

    while datetime.now() - verify_start < timedelta(seconds=verification_time):
        await browser.update_frame()

        # 检测是否进入结算相关界面（任一特征元素）
        if (await browser.match_image(p / "2_再战") or
                await browser.match_image(p / "1_bc") or
                await browser.match_image(p / "1_next")):
            settlement_detected = True
            browser.script_log(f"检测到战斗结算界面 (耗时: {(datetime.now() - verify_start).total_seconds():.1f}秒)")
            break

        # 也可能直接回到了主菜单（战斗已结算完）
        if (await browser.match_image(IMG_PATH / "minashigo" / "孤儿推本" / "1_menu") or
                await browser.match_image(IMG_PATH / "minashigo" / "孤儿推本" / "5_gr")):
            browser.script_log("检测到已返回主菜单，无需结算操作")
            return True

        await asyncio.sleep(0.3)

    if not settlement_detected:
        browser.script_log(f"✗ {verification_time}秒内未检测到战斗结算界面")
        return False

    # 第二阶段：执行结算操作
    browser.script_log(f"开始执行结算操作，总超时 {timeout} 秒")
    operation_start = datetime.now()

    while datetime.now() - operation_start < timedelta(seconds=timeout):
        await browser.update_frame()

        # ---------- ap不足 ----------
        if await browser.match_image(p / "2_ap恢复"):
            browser.script_log("检测到AP不足，执行AP恢复")
            await ap_recovery(browser)

        # ---------- 再战页 ----------
        if await browser.match_image(p / "2_再战"):
            browser.script_log("检测到再战页面")
            clicked = (
                    await browser.click_image(p / "2_下一关")
                    or await browser.click_image(p / "2_下个故事")
                    or await browser.click_image(p / "2_next")
            )
            if clicked:
                operated_on_retry_page = True
                browser.script_log("已在再战页点击继续")
                await asyncio.sleep(3)
                continue

        # ---------- 退出条件 ----------
        # 已经在再战页操作过，再也识别不到结算元素，说明离开结算界面
        if (operated_on_retry_page
                or await browser.match_image(img_path / "1_menu")
                or await browser.match_image(img_path / "5_gr")):
            browser.script_log("检测到已离开结算界面")
            break

        # ---------- 非再战页 ----------
        if await browser.match_image(p / "1_bc"):
            browser.script_log("检测到经验值结算页面")
            await browser.click_image(p / "1_ok")
            await asyncio.sleep(0.5)
            continue

        if await browser.click_image(p / "1_next"):
            browser.script_log("点击下一项")
            await asyncio.sleep(1)
            continue

        await asyncio.sleep(0.3)

    # 检查是否超时
    elapsed = (datetime.now() - operation_start).total_seconds()
    if elapsed >= timeout:
        browser.script_log(f"结算操作超时 (已执行 {elapsed:.1f}秒)")
        return False

    browser.script_log(f"结算操作完成 (耗时: {elapsed:.1f}秒)")
    return True


async def do_work(browser: UserBrowser):
    atted = 0
    while True:
        await browser.update_frame()

        # ---------- AP恢复 ----------
        if await browser.match_image(img_path / 'AP恢复' / 'ap_AP恢复'):
            browser.script_log("AP恢复")
            if not await ap_recovery(browser):
                browser.script_log("AP药用完，体力不足以继续")
                break
            continue

        # ---------- 战后结算 ----------
        if (
                await browser.match_image(img_path / "4_bc")
                or await browser.match_image(img_path / "战斗结算" / "1_next")
                or await browser.match_image(img_path / "战斗结算" / "2_next")
        ):
            browser.script_log("战后结算")
            await mnsg_ttk_after(browser)
            continue

        # ---------- 选关 / 选战神 ----------
        if await browser.match_image(img_path / "1_menu"):
            if await browser.match_image(img_path / "3_决定"):
                browser.script_log("选战神")
                await mnsg_ttk(browser)
                continue

            browser.script_log("选关界面")
            if await browser.match_image(img_path / "1_xing", use_color_check=True):
                await browser.click_image(img_path / "1_xing", threshold=0.98, pianyi=(-100, 20),
                                          match_select="bottom")
                await asyncio.sleep(5)
                atted = 0
                continue
            else:
                if (await browser.click_image(img_path / "1_atted", pianyi=(160, 10), match_select="right") or
                        await browser.click_image(img_path / "1_atted_2", pianyi=(160, 10), match_select="right")):
                    atted -= -1
                    await asyncio.sleep(round(random.uniform(1.5, 2.5), 3))
                    if atted >= 3:
                        break
            if await browser.match_image(img_path / "1_st"):
                await browser.click_image(img_path / "1_st", pianyi=(-100, 0), match_select="bottom_left",
                                          threshold=0.88)
                await asyncio.sleep(5)
                continue

        # ---------- 进入关卡 ----------
        if await browser.match_image(img_path / "2_cancel"):
            browser.script_log("进入关卡")
            await browser.click_image(img_path / "2_出击")
            continue

        # ---------- 广告界面 ----------
        if await browser.match_image(img_path / "5_gr"):
            browser.script_log("广告界面")
            await browser.click_image(img_path / "5_x")
            continue

        # ---------- 剧情界面 ----------
        if (await browser.match_image(img_path / "6_story")
                or await browser.match_image(img_path / "6_skip")
                or await browser.match_image(img_path / "6_skip_2")):
            browser.script_log("剧情界面")
            if await browser.click_image(img_path / "6_ok") or await browser.match_image(img_path / "6_skipqr"):
                continue
            if await browser.click_image(img_path / "6_skip"):
                continue
            if await browser.click_image(img_path / "6_skip_2"):
                continue

        # ---------- 奖励界面 ----------
        if await browser.match_image(img_path / '7_bc'):
            browser.script_log("奖励界面")
            await browser.click_image(img_path / '7_ok')
            continue

        # ---------- 未识别状态，轻微等待 ----------
        browser.script_log(None)
        await asyncio.sleep(0.3)
