# Few-shot: 最小多任务结构范式（与具体业务无关）
#
# 前置（由生成模板提供，本片段不重复）：import asyncio；CFG；_img；GUARDS/check_guards；
#   UserBrowser；IMG_DIR。
#
# 用途：照此骨架填空 —— 替换场景名、图片名、任务步名、任务数量。
# 保留：场景层 / SCENE_TO_STEP / resolve / bootstrap / run_task / do_work。
#
# 数据流：
#   unknown_state → 场景名
#   SCENE_TO_STEP + STATES → 步骤名
#   handler → 下一步 | __exit__ | None(保持)
#   do_work → run_task(task1) → run_task(task2) …

from typing import Optional

StateName = Optional[str]


# ── 1. 场景层：只认屏，不点击 ─────────────────────────────────────

async def unknown_state(browser) -> StateName:
    """介绍「场景标识」→ 每项 id 图 match(nav_threshold) → return 场景名。"""
    cs = {
        "场景甲": _img("id_alpha"),
        "场景乙": _img("id_beta"),
        "枢纽界面": _img("id_hub"),
    }
    rs = await asyncio.gather(*[
        browser.match_image(p, threshold=CFG.nav_threshold) for p in cs.values()
    ])
    for name, hit in zip(cs.keys(), rs):
        if hit:
            browser.script_log(f"[scene] {name}")
            return name
    await browser.b_sleep(1.5, 2.5)
    return None  # 禁止 return '未知'


# ── 2. 路由：场景名与步骤名之间的桥 ───────────────────────────────

def _resolve_state(
    nxt: str,
    states: dict,
    scene_map: dict,
) -> Optional[str]:
    """场景名优先 SCENE_TO_STEP，再步骤名直用；避免场景键误绑 __exit__ handler。"""
    if not nxt:
        return None
    step = scene_map.get(nxt)
    if step and step in states:
        return step
    if nxt in states:
        return nxt
    return None


TASK_ALPHA_SCENE_TO_STEP = {
    "枢纽界面": "进入甲",
    "场景甲": "业务甲",
}
TASK_BETA_SCENE_TO_STEP = {
    "枢纽界面": "返回枢纽",
    "场景乙": "业务乙",
}


# ── 3. 导航辅助：本步骤结束 → return 业务态名，禁止 __exit__ ─────

async def helper_返回枢纽(browser) -> StateName:
    browser.script_log("[nav] 返回枢纽")
    await browser.update_frame()
    if await browser.match_image(_img("id_mark"), threshold=CFG.nav_threshold):
        return "枢纽界面"
    if await browser.click_image(_img("btn_home"), threshold=CFG.threshold):
        ok = await browser.wait_image(
            _img("id_mark"), timeout=30, threshold=CFG.nav_threshold,
        )
        return "枢纽界面" if ok else "未知"
    return "未知"


# ── 4. 场景桩：已在某场景 → return 下一步，禁止 return 自身空转 ───

async def stub_枢纽_to_alpha(browser) -> StateName:
    return "进入甲"


async def stub_场景甲_to_work(browser) -> StateName:
    return "业务甲"


async def stub_枢纽_to_beta(browser) -> StateName:
    return "进入乙"


async def stub_场景乙_to_work(browser) -> StateName:
    return "业务乙"


# ── 5. 业务步：须 click/wait/match，禁止仅 log + return None ─────

async def step_进入甲(browser) -> StateName:
    browser.script_log("[alpha] 进入甲")
    if await browser.click_image(_img("btn_to_alpha"), threshold=CFG.threshold):
        ok = await browser.wait_image(
            _img("id_alpha"), timeout=30, threshold=CFG.nav_threshold,
        )
        return "业务甲" if ok else "未知"
    return None


async def step_业务甲(browser) -> StateName:
    browser.script_log("[alpha] 业务甲")
    await browser.update_frame()
    if await browser.match_image(_img("done_alpha"), threshold=CFG.threshold):
        return "__exit__"
    if await browser.click_image(_img("act_alpha"), threshold=CFG.threshold):
        await browser.b_sleep(0.3, 0.5)
        await browser.update_frame()
        return "业务甲"
    return None


async def step_进入乙(browser) -> StateName:
    browser.script_log("[beta] 进入乙")
    if await browser.click_image(_img("btn_to_beta"), threshold=CFG.threshold):
        ok = await browser.wait_image(
            _img("id_beta"), timeout=30, threshold=CFG.nav_threshold,
        )
        return "业务乙" if ok else "未知"
    return None


async def step_业务乙(browser) -> StateName:
    browser.script_log("[beta] 业务乙")
    await browser.update_frame()
    if await browser.match_image(_img("done_beta"), threshold=CFG.threshold):
        return "__exit__"
    if await browser.click_image(_img("act_beta"), threshold=CFG.threshold):
        await browser.b_sleep(0.3, 0.5)
        await browser.update_frame()
        return "业务乙"
    return None


# ── 6. 每任务独立状态表 + 超时 + 场景映射 ─────────────────────────

TASK_ALPHA_STATES = {
    "未知": unknown_state,
    "返回枢纽": helper_返回枢纽,
    "枢纽界面": stub_枢纽_to_alpha,
    "场景甲": stub_场景甲_to_work,
    "进入甲": step_进入甲,
    "业务甲": step_业务甲,
}
TASK_ALPHA_TIMEOUT = {
    "未知": 180,
    "返回枢纽": 90,
    "枢纽界面": 30,
    "场景甲": 30,
    "进入甲": 45,
    "业务甲": 60,
}

TASK_BETA_STATES = {
    "未知": unknown_state,
    "返回枢纽": helper_返回枢纽,
    "枢纽界面": stub_枢纽_to_beta,
    "场景乙": stub_场景乙_to_work,
    "进入乙": step_进入乙,
    "业务乙": step_业务乙,
}
TASK_BETA_TIMEOUT = {
    "未知": 180,
    "返回枢纽": 90,
    "枢纽界面": 30,
    "场景乙": 30,
    "进入乙": 45,
    "业务乙": 60,
}


# ── 7. 多任务入口：按 task_name 选起跑导航态 ───────────────────────

def _task_entry_state(states: dict, task_name: str = "") -> str:
    """介绍 @返回枢纽 / @返回主界面 等 → 对应键必须在 states 内。"""
    if task_name == "beta" and "返回枢纽" in states:
        return "返回枢纽"
    if "返回枢纽" in states:
        return "返回枢纽"
    return "未知"


async def _bootstrap_state(
    browser,
    states: dict,
    scene_map: dict,
) -> Optional[str]:
    """启动先识场景再 resolve，避免已在目标界面仍跑错导航。"""
    scene = await unknown_state(browser)
    if scene is None:
        return None
    return _resolve_state(scene, states, scene_map)


# 非场景标识；多界面常驻 → 可见则说明非过场 loading
NAV_CHROME = ("home",)


async def _has_nav_chrome(browser) -> bool:
    for stem in NAV_CHROME:
        if await browser.match_image(_img(stem), threshold=CFG.threshold):
            return True
    return False


# ── 8. run_task：单任务 FSM（超时先识场景，禁止空等后 task_failed）──

async def run_task(
    browser,
    task_name: str,
    states: dict,
    timeouts: dict,
    scene_map: dict,
) -> str:
    boot = await _bootstrap_state(browser, states, scene_map)
    state_name = boot if boot and boot != "未知" else _task_entry_state(states, task_name)
    se_time = asyncio.get_event_loop().time()
    start = se_time
    while True:
        await browser.update_frame()
        if await check_guards(browser):
            continue
        now = asyncio.get_event_loop().time()
        if now - start > CFG.total_timeout:
            return "__task_failed__"
        if now - se_time > timeouts.get(state_name, 180):
            scene = await unknown_state(browser)
            if scene is None:
                await browser.update_frame()
                if await _has_nav_chrome(browser):
                    browser.script_log("[scene] 无标识但有导航按钮，非过场")
                    se_time = now
                    await browser.b_sleep(0.8, 1.2)
                    continue
                browser.script_log("[scene] 无标识且无导航按钮，视为过场，保持状态")
                se_time = now
                await browser.b_sleep(0.8, 1.2)
                continue
            resolved = _resolve_state(scene, states, scene_map)
            if resolved and resolved != "未知":
                state_name = resolved
                se_time = now
                continue
            if state_name == "未知" or resolved == "未知":
                state_name = _task_entry_state(states, task_name)
                se_time = now
                continue
            browser.script_log("[scene] 未映射场景，保持状态")
            se_time = now
            await browser.b_sleep(0.8, 1.2)
            continue
        handler = states.get(state_name)
        if handler is None:
            scene = await unknown_state(browser)
            resolved = _resolve_state(scene or state_name, states, scene_map)
            if resolved and resolved != "未知":
                state_name = resolved
                se_time = now
                continue
            return "__task_failed__"
        nxt = await handler(browser)
        if nxt == "__exit__":
            return "__done__"
        resolved = _resolve_state(nxt, states, scene_map)
        if resolved and resolved != "未知":
            state_name = resolved
            se_time = now
        elif resolved == "未知" or (state_name == "未知" and not resolved):
            state_name = _task_entry_state(states, task_name)
            se_time = now
        elif not resolved and nxt:
            state_name = _task_entry_state(states, task_name)
            se_time = now
        elif nxt is None:
            pass
        await browser.b_sleep(0.05, 0.15)


# ── 9. do_work：顺序执行子任务 ───────────────────────────────────

async def do_work(browser: UserBrowser):
    if CFG.use_polling_cache:
        browser.use_polling_temp_cache = True
    tasks = [
        ("alpha", TASK_ALPHA_STATES, TASK_ALPHA_TIMEOUT, TASK_ALPHA_SCENE_TO_STEP),
        ("beta", TASK_BETA_STATES, TASK_BETA_TIMEOUT, TASK_BETA_SCENE_TO_STEP),
    ]
    for name, st, to, sm in tasks:
        for attempt in range(3):
            result = await run_task(browser, name, st, to, sm)
            if result == "__task_failed__":
                browser.script_log(f"[{name}] failed ({attempt + 1}/3)")
                await browser.b_sleep(1.0, 2.0)
                continue
            browser.script_log(f"[{name}] done")
            break
        else:
            browser.script_log(f"[{name}] abort")
            break
