"""``browser_user_browser`` runtime profile：绑定 ``UserBrowser``（设计 §4.2）。

事实基线：``backend/browser/user_browser.py`` 的真实签名。**不可与 desktop 取并集**：

| 方法 | desktop (UserWindow) | browser (UserBrowser) |
| --- | --- | --- |
| ``match_image`` | 有 ``cheap``/``force_wide``/``vision_priority`` | 无，但多 ``quiet`` 在前 |
| ``match_image_multi`` | **不存在** | 存在，返回 ``list[dict]`` |
| ``wait_image`` | **无** ``threshold`` | 有 ``threshold=0.9`` |
| ``update_frame`` | ``*, priority=20`` | ``crop_game_canvas=False`` |
| ``goto`` / ``dmm_login`` | 不存在 | 存在（登录门控） |

``guidance`` 字段保存迁移期旧 ``api_catalog`` 卡片原文，供 facade 渲染保持兼容；
Phase 7 清理重复事实源时随本文件一起作为唯一来源。
"""

from __future__ import annotations

from .schema import (
    ASYNC_METHOD,
    SYNC_METHOD,
    ApiContract,
    ProfileContracts,
    ReturnContract,
    kwopt,
    opt,
    req,
)

_PROFILE = "browser_user_browser"

_GOTO_CARD = (
    "await browser.goto(url: str, retries=3)  # LOGIN / web navigation ONLY\n"
    "DO: open the game/login page before image FSM.\n"
    "DON'T: use in daily/sortie image scripts."
)
_DMM_LOGIN_CARD = (
    "await browser.dmm_login(timeout=30_000)  # milliseconds; LOGIN ONLY\n"
    "Fills DMM email/password from the account and submits.\n"
    "DON'T: scrape the login form with match_image."
)

CONTRACTS: tuple[ApiContract, ...] = (
    ApiContract(
        id=_PROFILE + ".match_image",
        runtime_profile=_PROFILE,
        name="match_image",
        call_kind=ASYNC_METHOD,
        summary="在最新截图里找一个模板；返回假值或 MatchResult。",
        parameters=(
            req("img_path", "str | Path"),
            opt("threshold", "0.9", "float"),
            opt("use_color_check", "False", "bool"),
            opt("match_select", "'best'", "str"),
            opt("quiet", "False", "bool"),
            opt("match_mode", "'image'", "str"),
            opt("pixel_tol", "8.0", "float"),
            opt("use_hotspot_roi", "None", "bool | None"),
        ),
        returns=ReturnContract(
            type="MatchResult | falsy",
            iterable=True, sized=False, mapping=False, nullable=False,
            indexable=True, collection=False,
            subscript_keys=("x", "y", "score", "max_val"),
            nullable_fields=("x", "y", "match_success"),
            truthiness="用真假判断（`if hit:`），不要用 `is not None`。",
            fields=("x", "y", "max_val", "score", "match_success"),
            identity_check_unsafe=True,
            lifetime="绑定调用当时的帧；点击/睡眠后失效。",
        ),
        coordinate_space="浏览器视口 CSS 坐标。",
        semantics=("浏览器 profile 没有 cheap/force_wide。",),
        side_effects=("可能触发截图。",),
        examples_valid=("if await browser.match_image(_img('logo'), threshold=CFG.nav_threshold):",),
        examples_invalid=("await browser.match_image('a.png', force_wide=True)  # browser 没有 force_wide",),
        tags=("vision", "match", "core"),
        guidance=(
            "await browser.match_image(path, threshold=...) -> falsy OR MatchResult\n"
            "MatchResult has ATTRIBUTES: .x .y .max_val (object, NOT a dict).\n"
            "Usually only use as boolean: if await browser.match_image(...):\n"
            "DO: scene ids (logo/rank/*_logo) use CFG.nav_threshold.\n"
            "DO: buttons use CFG.threshold or CFG.icon_threshold.\n"
            "DON'T: confuse with match_image_multi (that returns list[dict]).\n"
            "DON'T: use_color_check unless the explanation asks."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".match_image_multi",
        runtime_profile=_PROFILE,
        name="match_image_multi",
        call_kind=ASYNC_METHOD,
        summary="取多个命中，每项是 dict（'x'/'y'/'score'）。",
        parameters=(
            req("img_path", "str | Path"),
            opt("threshold", "0.9", "float"),
            opt("use_color_check", "False", "bool"),
            opt("match_mode", "'image'", "str"),
            opt("pixel_tol", "8.0", "float"),
        ),
        returns=ReturnContract(
            type="list[dict]",
            iterable=True, sized=True, mapping=False, nullable=False,
            indexable=True, collection=True,
            truthiness="空列表表示没命中；每项用 `m['y']` / `m['x']` / `m['score']`。",
            fields=("x", "y", "score"),
            never_proves=("命中的是同一个目标",),
        ),
        coordinate_space="浏览器视口 CSS 坐标。",
        semantics=("多目标里挑一个（如 max-y）时用；挑完用 `click(x, y)` 点过去。",),
        side_effects=("可能触发截图。",),
        examples_valid=("best = max(await browser.match_image_multi(_img('段位')), key=lambda m: m['y'])",),
        examples_invalid=("for m in matches: print(m.y)  # dict 没有属性访问",),
        tags=("vision", "match", "browser-only"),
        guidance=(
            "await browser.match_image_multi(path, threshold=...) -> list[dict]\n"
            "Each item is a DICT with keys 'x','y','score' (CSS coords).\n"
            "CRITICAL: use m['y'] / m['x'] / m['score'] — NEVER m.y / m.x "
            "(AttributeError).\n"
            "Pick max-y (e.g. 段位) then click that hit:\n"
            "  matches = await browser.match_image_multi(_img('jjc_段位'), "
            "threshold=CFG.threshold)\n"
            "  if not matches: return '未知'  # or None\n"
            "  best = max(matches, key=lambda m: m['y'])\n"
            "  await browser.click(best['x'], best['y'])\n"
            "DON'T: treat items as MatchResult objects.\n"
            "DON'T: click_image after multi when you need a specific hit "
            "(click_image picks one template match, not your max-y)."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".click_image",
        runtime_profile=_PROFILE,
        name="click_image",
        call_kind=ASYNC_METHOD,
        summary="找图 → 偏移 → 点击；可选 expect 确认。",
        parameters=(
            req("img_path", "str | Path"),
            opt("pianyi", "(0, 0)", "tuple[int, int]"),
            opt("down_time", "0.12", "float"),
            opt("threshold", "0.9", "float"),
            opt("use_color_check", "False", "bool"),
            opt("match_select", "'best'", "str"),
            opt("max_delay", "None", "float | None"),
            opt("match_mode", "'image'", "str"),
            opt("pixel_tol", "8.0", "float"),
            opt("expect", "'none'", "str"),
            opt("appear_path", "None", "str | Path | None"),
            opt("confirm_timeout", "8.0", "float"),
            opt("stable_ms", "350.0", "float"),
        ),
        returns=ReturnContract(
            type="bool",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False,
            truthiness="True = 点到；False = 没找到或确认失败。必须检查返回值。",
            identity_check_unsafe=True,
            never_proves=("网络请求成功",),
        ),
        coordinate_space="浏览器视口 CSS 坐标 + pianyi 偏移。",
        semantics=("单模板按钮用这个；多命中选择用 match_image_multi + click。",),
        side_effects=("真实点击；成功后帧失效。",),
        examples_valid=("await browser.click_image(_img('start'), expect='appear', appear_path=_img('home'))",),
        examples_invalid=("await browser.click_image('a.png', force_wide=True)",),
        tags=("vision", "click", "core"),
        guidance=(
            "await browser.click_image(path, threshold=CFG.threshold, "
            "pianyi=(0,0), expect='none', appear_path=None) -> bool\n"
            "DO: single-template buttons; if scene changes, wait_image / "
            "match_image the next id in the SAME state fn, OR set "
            "expect='appear' + appear_path / expect='gone'.\n"
            "DON'T: use to select among several multi hits (use match_image_multi "
            "+ click).\n"
            "NOTE: successful click invalidates frame; b_sleep also invalidates. "
            "Default expect='none' (no confirm wait)."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".click_image_effect",
        runtime_profile=_PROFILE,
        name="click_image_effect",
        call_kind=ASYNC_METHOD,
        summary="点击模板并用点击后区域变化判断视觉反馈。",
        parameters=(
            req("img_path", "str | Path"),
            opt("pianyi", "(0, 0)", "tuple[int, int]"),
            opt("down_time", "0.12", "float"),
            opt("threshold", "0.9", "float"),
            opt("use_color_check", "False", "bool"),
            opt("match_select", "'best'", "str"),
            opt("max_delay", "None", "float | None"),
            opt("match_mode", "'image'", "str"),
            opt("pixel_tol", "8.0", "float"),
            opt("interval", "0.15", "float"),
            opt("effect_threshold", "0.75", "float"),
        ),
        returns=ReturnContract(
            type="bool",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False,
            truthiness="True 仅表示点击后该区域变得不像原模板；False 表示没找到/无法判定/仍然相像。",
            identity_check_unsafe=True,
            never_proves=("场景切换成功",),
        ),
        semantics=("不要把它当作转场证明。",),
        side_effects=("真实点击。",),
        examples_valid=("changed = await browser.click_image_effect(_img('btn'))",),
        examples_invalid=(),
        tags=("vision", "click", "effect"),
        guidance=(
            "await browser.click_image_effect(path, interval=0.15, "
            "effect_threshold=0.75, threshold=CFG.threshold, pianyi=(0,0)) -> bool\n"
            "Find and click one template, then capture only its original region. "
            "True means the post-click template score fell below effect_threshold; "
            "False means not found, unverifiable, or still visually similar.\n"
            "DO: use for buttons whose local appearance should change after click.\n"
            "DON'T: treat it as proof of a scene transition or network success; use "
            "expect='appear'/'gone' or wait_image for those cases."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".click",
        runtime_profile=_PROFILE,
        name="click",
        call_kind=ASYNC_METHOD,
        summary="按坐标点击一次。",
        parameters=(
            req("x", "int"),
            req("y", "int"),
            opt("down_time", "0.12", "float"),
            opt("pianyi", "(0, 0)", "tuple[int, int]"),
        ),
        returns=ReturnContract(
            type="None",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False,
            truthiness="没有返回值，不能用来判断点击是否生效。",
            never_proves=("点击已生效",),
        ),
        coordinate_space="浏览器视口 CSS 坐标。",
        semantics=("只与 match_image_multi 的命中坐标配合使用。",),
        side_effects=("真实点击。",),
        examples_valid=("await browser.click(best['x'], best['y'])",),
        examples_invalid=("await browser.click(100, 200)  # 硬编码魔数",),
        tags=("click", "raw"),
        guidance=(
            "await browser.click(x, y, pianyi=(0,0)) -> None\n"
            "DO: ONLY with x,y from match_image_multi dicts "
            "(max-y / max-x selection).\n"
            "DON'T: hardcode magic numbers.\n"
            "DON'T: use for normal single-template buttons "
            "(use click_image instead)."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".wait_image",
        runtime_profile=_PROFILE,
        name="wait_image",
        call_kind=ASYNC_METHOD,
        summary="等待模板出现。",
        parameters=(
            req("img_path", "str | Path"),
            opt("timeout", "0", "float"),
            opt("threshold", "0.9", "float", doc="仅浏览器 profile 有"),
        ),
        returns=ReturnContract(
            type="bool",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False,
            truthiness="True = 出现；False = 超时。必须检查返回值。",
            identity_check_unsafe=True,
        ),
        semantics=("timeout<=0 表示一直等；用 STATE_TIMEOUT 里的值。",),
        side_effects=("内部反复截图。",),
        examples_valid=("if not await browser.wait_image(_img('home'), timeout=STATE_TIMEOUT['unknown_state']): return '未知'",),
        examples_invalid=(),
        tags=("vision", "wait", "core"),
        guidance=(
            "await browser.wait_image(path, timeout=seconds, threshold=0.9) "
            "-> True if seen else False\n"
            "timeout<=0 means wait forever — prefer STATE_TIMEOUT.get(state, 30) "
            "on single_fsm; on multi_task use that task's TASK_*_TIMEOUT.get(...) "
            "or a numeric timeout (shared handlers may merge into STATE_TIMEOUT).\n"
            "DO: after a scene-changing click; if False, return '未知'.\n"
            "DON'T: busy-loop match_image yourself for the same purpose."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".b_sleep",
        runtime_profile=_PROFILE,
        name="b_sleep",
        call_kind=ASYNC_METHOD,
        summary="可中断睡眠。",
        parameters=(
            req("seconds", "float"),
            opt("upper_limit", "None", "float | None"),
            opt("step", "0.05", "float"),
            kwopt("invalidate", "True", "bool"),
        ),
        returns=ReturnContract(type="None",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False, truthiness="没有返回值。"),
        semantics=("不要用 time.sleep / asyncio.sleep。",),
        side_effects=("标记截图缓存失效。",),
        examples_valid=("await browser.b_sleep(1.5, 2.5)",),
        examples_invalid=("asyncio.sleep(1)",),
        tags=("timing",),
        guidance=(
            "await browser.b_sleep(lo, hi=None)  # seconds; if hi given, "
            "uniform random in [lo,hi]\n"
            "DO: unknown_state miss path: b_sleep(1.5, 2.5) then return None.\n"
            "NOTE: marks the screenshot cache stale; next match_image will refresh.\n"
            "DON'T: time.sleep / asyncio.sleep in scripts."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".update_frame",
        runtime_profile=_PROFILE,
        name="update_frame",
        call_kind=ASYNC_METHOD,
        summary="强制拉一帧并清空轮询缓存。",
        parameters=(
            opt("save_screenshot", "False", "bool"),
            opt("crop_game_canvas", "False", "bool"),
        ),
        returns=ReturnContract(type="frame | None",
            iterable=True, sized=True, mapping=False, nullable=True,
            indexable=True, collection=False, truthiness="不用于业务判断。"),
        semantics=("日常 match 会自动 ensure。",),
        side_effects=("刷新帧。",),
        examples_valid=("await browser.update_frame()",),
        examples_invalid=(),
        tags=("vision", "frame"),
        guidance=(
            "await browser.update_frame()  # force one fresh screenshot + clear "
            "polling match cache\n"
            "OPTIONAL now: runtime auto-refreshes after click/b_sleep and before "
            "match when stale.\n"
            "DO: when you need an explicit same-tick multi-template snapshot, "
            "or debugging.\n"
            "DON'T: spam update_frame before every single match."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".invalidate_frame",
        runtime_profile=_PROFILE,
        name="invalidate_frame",
        call_kind=SYNC_METHOD,
        summary="把当前帧标记为失效（同步）。",
        parameters=(),
        returns=ReturnContract(type="None",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False, truthiness="没有返回值。"),
        semantics=("点击后要判定新画面时先失效再匹配。",),
        side_effects=("当前帧失效。",),
        examples_valid=("browser.invalidate_frame()",),
        examples_invalid=("await browser.invalidate_frame()",),
        tags=("vision", "frame"),
        guidance=(
            "browser.invalidate_frame()  # sync; mark current frame stale\n"
            "DO: right before a match that must judge the frame AFTER a click "
            "(avoid acting on a stale frame and misclicking)."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".request_fps",
        runtime_profile=_PROFILE,
        name="request_fps",
        call_kind=ASYNC_METHOD,
        summary="声明期望截图频率。",
        parameters=(req("fps", "int | float"), kwopt("key", "'script'", "str")),
        returns=ReturnContract(type="Any", truthiness="不用于业务判断。"),
        semantics=("多需求取最大，硬上限 60。",),
        side_effects=("提高截图频率。",),
        examples_valid=("await browser.request_fps(15)",),
        examples_invalid=(),
        tags=("perf",),
        guidance=(
            "await browser.request_fps(hz, key='script')  # demand continuous "
            "observe Hz\n"
            "Runtime takes max of demands (hard cap 60). Idle => 0 capture.\n"
            "DO: only when this script needs higher than default observe rate.\n"
            "DON'T: forget that runner releases 'script' on do_work exit."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".release_fps",
        runtime_profile=_PROFILE,
        name="release_fps",
        call_kind=ASYNC_METHOD,
        summary="释放先前声明的频率。",
        parameters=(opt("key", "'script'", "str"),),
        returns=ReturnContract(type="Any", truthiness="不用于业务判断。"),
        semantics=("通常不需要手动调用。",),
        side_effects=(),
        examples_valid=("await browser.release_fps('script')",),
        examples_invalid=(),
        tags=("perf",),
        guidance=(
            "await browser.release_fps(key='script')  # drop a prior request_fps\n"
            "Usually unnecessary — TaskController releases on script end."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".script_log",
        runtime_profile=_PROFILE,
        name="script_log",
        call_kind=SYNC_METHOD,
        summary="写一行脚本日志。",
        parameters=(req("msg", "str"),),
        returns=ReturnContract(type="None",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False, truthiness="没有返回值。"),
        semantics=("同步方法，不要 await。",),
        side_effects=("写日志。",),
        examples_valid=("browser.script_log('state=home')",),
        examples_invalid=("await browser.script_log('x')",),
        tags=("log",),
        guidance=(
            "browser.script_log(msg: str)  # sync; appears in trial log\n"
            "DO: log state entry and key click/wait outcomes."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".note_state",
        runtime_profile=_PROFILE,
        name="note_state",
        call_kind=SYNC_METHOD,
        summary="上报当前状态名。",
        parameters=(req("name", "str"),),
        returns=ReturnContract(type="None",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False, truthiness="没有返回值。"),
        semantics=("同步方法，不要 await。",),
        side_effects=("影响卡住检测。",),
        examples_valid=("browser.note_state('home')",),
        examples_invalid=("await browser.note_state('home')",),
        tags=("state", "log"),
        guidance=(
            "browser.note_state(name: str)  # sync; report current FSM state\n"
            "DO: call every main-loop / run_task iteration (stuck detection)."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".note_progress",
        runtime_profile=_PROFILE,
        name="note_progress",
        call_kind=SYNC_METHOD,
        summary="手动进展心跳。",
        parameters=(),
        returns=ReturnContract(type="None",
            iterable=False, sized=False, mapping=False, nullable=False,
            indexable=False, collection=False, truthiness="没有返回值。"),
        semantics=("同步方法，不要 await。",),
        side_effects=("重置卡住计时。",),
        examples_valid=("browser.note_progress()",),
        examples_invalid=(),
        tags=("state",),
        guidance="browser.note_progress()  # optional manual progress heartbeat",
    ),
    ApiContract(
        id=_PROFILE + ".enable_pseudo_record",
        runtime_profile=_PROFILE,
        name="enable_pseudo_record",
        call_kind=SYNC_METHOD,
        summary="开启伪录制。",
        parameters=(
            opt("script_name", "'script'", "str"),
            kwopt("save_keyframes", "True", "bool"),
            kwopt("force", "False", "bool"),
            kwopt("keyframe_min_interval_s", "None", "float | None"),
        ),
        returns=ReturnContract(type="PseudoRecorder | None",
            iterable=False, sized=False, mapping=False, nullable=True,
            indexable=False, collection=False, truthiness="返回录制器；未开启为 None。"),
        semantics=("同步方法，不要 await。",),
        side_effects=("落盘时间线与关键帧。",),
        examples_valid=("browser.enable_pseudo_record(script_name='script', force=True)",),
        examples_invalid=(),
        tags=("telemetry",),
        guidance=(
            "browser.enable_pseudo_record(script_name=..., force=True)  # sync\n"
            "Starts timeline+keyframe recording under screenshots/pseudo_record/.\n"
            "DO: call once at do_work start when measuring performance.\n"
            "DON'T: remove during optimize; instrumentation is always allowed."
        ),
    ),
    ApiContract(
        id=_PROFILE + ".finish_pseudo_record",
        runtime_profile=_PROFILE,
        name="finish_pseudo_record",
        call_kind=SYNC_METHOD,
        summary="结束伪录制。",
        parameters=(kwopt("status", "'ok'", "str"),),
        returns=ReturnContract(type="dict | None",
            iterable=True, sized=True, mapping=True, nullable=True,
            indexable=False, collection=False, truthiness="返回摘要；未开启为 None。"),
        semantics=("同步方法，不要 await。",),
        side_effects=("写 summary.json。",),
        examples_valid=("browser.finish_pseudo_record(status='ok')",),
        examples_invalid=(),
        tags=("telemetry",),
        guidance=(
            "browser.finish_pseudo_record(status='ok'|'error')  # sync\n"
            "Flushes summary.json / timeline; call in finally."
        ),
    ),
    # ── 登录/网页门控（需 allow_login）──
    ApiContract(
        id=_PROFILE + ".dmm_login",
        runtime_profile=_PROFILE,
        name="dmm_login",
        call_kind=ASYNC_METHOD,
        summary="填表登录 DMM（登录任务专用）。",
        parameters=(
            kwopt("timeout", "30000", "int", doc="毫秒"),
            kwopt("eager", "False", "bool"),
        ),
        returns=ReturnContract(type="Any", truthiness="不用于业务判断。"),
        semantics=("只在登录/网页任务里允许（allow_login）。",),
        side_effects=("填表并提交登录。",),
        examples_valid=("await browser.dmm_login(timeout=30_000)",),
        examples_invalid=(),
        tags=("login",),
        guidance=_DMM_LOGIN_CARD,
    ),
    ApiContract(
        id=_PROFILE + ".goto",
        runtime_profile=_PROFILE,
        name="goto",
        call_kind=ASYNC_METHOD,
        summary="打开网页地址（经 __getattr__ 代理到浏览器对象）。",
        parameters=(req("url", "str"), opt("retries", "3", "int")),
        returns=ReturnContract(type="Any", truthiness="不用于业务判断。"),
        semantics=(
            "代理方法：UserBrowser 上不直接定义，经 __getattr__ → self._browser → "
            "Browser（→ NavigationMixin.goto）解析；签名由登记代理目标核验。",
        ),
        side_effects=("页面导航。",),
        examples_valid=("await browser.goto('https://example.com')",),
        examples_invalid=(),
        tags=("login", "proxy"),
        proxied=True,
        guidance=_GOTO_CARD,
    ),
)

BROWSER_USER_BROWSER = ProfileContracts(
    runtime_profile=_PROFILE,
    title="浏览器脚本",
    target_class="UserBrowser",
    target_module="backend.browser.user_browser",
    contracts=CONTRACTS,
    login_methods=frozenset({"goto", "dmm_login"}),
    # Phase 2.1：`UserBrowser.__getattr__`（user_browser.py:222）把属性转发到
    # `self._browser`（真实类型 `backend.browser.browser.Browser`）。这里登记**真实目标类**，
    # 由它自己的 MRO 继续解析（如 `goto` → `NavigationMixin.goto`）；
    # 不要写孤立 Mixin，也不要为某个方法写特例。
    proxy_target_module="backend.browser.browser",
    proxy_target_class="Browser",
    notes=(
        "只在确实运行在浏览器脚本环境时使用（Phase 0 §1.1：account['_target']='browser'）。",
        "match_image_multi 仅此 profile 有；wait_image 仅此 profile 有 threshold。",
        "goto 为 __getattr__ 代理方法（→ Browser → NavigationMixin）：代理可达本身仍不等于批准，"
        "未登记的 Browser 内部方法依旧拿不到凭据。",
    ),
)
