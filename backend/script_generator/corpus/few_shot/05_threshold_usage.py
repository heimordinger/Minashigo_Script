# Few-shot: 阈值分工 — 场景 id 用 nav；按钮用 threshold / icon_threshold

async def unknown_state(browser) -> StateName:
    cs = {"主界面": _img("rank"), "出击界面": _img("sortie_logo")}
    rs = await asyncio.gather(*[
        browser.match_image(p, threshold=CFG.nav_threshold) for p in cs.values()
    ])
    for name, hit in zip(cs.keys(), rs):
        if hit:
            return name
    await browser.b_sleep(1.5, 2.5)
    return None


async def click_small_toggle(browser) -> bool:
    # 小图标 / 与背景接近 → icon_threshold；普通按钮 → threshold
    return await browser.click_image(
        _img("tiny_toggle"),
        threshold=CFG.icon_threshold,
    )
