# Few-shot: match_image_multi 返回 list[dict]；jjc_段位取 x 最大后 click(x,y)
#
# 对照：
# - match_image -> MatchResult 对象（.x/.y）或假值，一般只当 if hit:
# - match_image_multi -> [{'x','y','score'}, ...]，必须 m['x']，禁止 m.y
# - 介绍写「x 最大」→ max(matches, key=lambda m: m["x"])；不要 click_image（会点到不确定的那一个）

async def step_选择段位(browser) -> StateName:
    browser.script_log("[jjc] 选择段位（x 最大）")
    matches = await browser.match_image_multi(
        _img("jjc_段位"), threshold=CFG.threshold,
    )
    if not matches:
        return "未知"
    best = max(matches, key=lambda m: m["x"])
    browser.script_log(f"  max-x hit x={best['x']:.0f} score={best['score']:.3f}")
    await browser.click(best["x"], best["y"])
    ok = await browser.wait_image(_img("jjc_出击"), timeout=30)
    if not ok:
        return "选择段位"
    return "检查备战界面"
