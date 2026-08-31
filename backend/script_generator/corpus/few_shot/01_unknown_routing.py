# Few-shot: unknown_state 场景路由
# 命中标识图 → 返回业务状态名；全部未命中 → sleep 后 return None（禁止 return '未知'）

async def unknown_state(browser) -> StateName:
    cs = {
        "主界面": _img("rank"),
        "出击界面": _img("出击_logo"),
        "房间界面": _img("room_logo"),
        "竞技场界面": _img("jjc_logo"),
        "塔界面": _img("ta_logo"),
    }
    rs = await asyncio.gather(*[
        browser.match_image(p, threshold=CFG.nav_threshold) for p in cs.values()
    ])
    for name, hit in zip(cs.keys(), rs):
        if hit:
            return name  # 业务态，不是 '未知'
    await browser.b_sleep(1.5, 2.5)
    return None
