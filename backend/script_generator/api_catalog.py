"""Short UserBrowser API contract cards for generate-time lookup.

Models see signatures and do/don't — not full UserBrowser source.
Coordinate click(x,y) is allowed ONLY with coords from match_image_multi.
"""

from __future__ import annotations

import json
import re

ALLOWED_METHODS = (
    "match_image",
    "match_image_multi",
    "click_image",
    "click",
    "wait_image",
    "b_sleep",
    "update_frame",
    "request_fps",
    "release_fps",
    "script_log",
    "note_state",
    "note_progress",
)

LOGIN_METHODS = frozenset({"goto", "dmm_login"})

_LOGIN_HINT_RE = re.compile(
    r"登录|登入|网页|dmm|goto|打开游戏页|跳转.*http|浏览器地址",
    re.I,
)

_CARDS: dict[str, str] = {
    "match_image": (
        "await browser.match_image(path, threshold=...) -> falsy OR MatchResult\n"
        "MatchResult has ATTRIBUTES: .x .y .max_val (object, NOT a dict).\n"
        "Usually only use as boolean: if await browser.match_image(...):\n"
        "DO: scene ids (logo/rank/*_logo) use CFG.nav_threshold.\n"
        "DO: buttons use CFG.threshold or CFG.icon_threshold.\n"
        "DON'T: confuse with match_image_multi (that returns list[dict]).\n"
        "DON'T: use_color_check unless the explanation asks."
    ),
    "match_image_multi": (
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
    "click": (
        "await browser.click(x, y, pianyi=(0,0)) -> None\n"
        "DO: ONLY with x,y from match_image_multi dicts "
        "(max-y / max-x selection).\n"
        "DON'T: hardcode magic numbers.\n"
        "DON'T: use for normal single-template buttons "
        "(use click_image instead)."
    ),
    "click_image": (
        "await browser.click_image(path, threshold=CFG.threshold, "
        "pianyi=(0,0)) -> bool\n"
        "DO: single-template buttons; if scene changes, wait_image / "
        "match_image the next id in the SAME state fn.\n"
        "DON'T: use to select among several multi hits (use match_image_multi "
        "+ click).\n"
        "NOTE: successful click invalidates frame; b_sleep also invalidates."
    ),
    "wait_image": (
        "await browser.wait_image(path, timeout=seconds, threshold=0.9) "
        "-> True if seen else False\n"
        "timeout<=0 means wait forever — prefer STATE_TIMEOUT.get(state, 30) "
        "on single_fsm; on multi_task use that task's TASK_*_TIMEOUT.get(...) "
        "or a numeric timeout (shared handlers may merge into STATE_TIMEOUT).\n"
        "DO: after a scene-changing click; if False, return '未知'.\n"
        "DON'T: busy-loop match_image yourself for the same purpose."
    ),
    "b_sleep": (
        "await browser.b_sleep(lo, hi=None)  # seconds; if hi given, "
        "uniform random in [lo,hi]\n"
        "DO: unknown_state miss path: b_sleep(1.5, 2.5) then return None.\n"
        "NOTE: marks the screenshot cache stale; next match_image will refresh.\n"
        "DON'T: time.sleep / asyncio.sleep in scripts."
    ),
    "update_frame": (
        "await browser.update_frame()  # force one fresh screenshot + clear "
        "polling match cache\n"
        "OPTIONAL now: runtime auto-refreshes after click/b_sleep and before "
        "match when stale.\n"
        "DO: when you need an explicit same-tick multi-template snapshot, "
        "or debugging.\n"
        "DON'T: spam update_frame before every single match."
    ),
    "request_fps": (
        "await browser.request_fps(hz, key='script')  # demand continuous "
        "observe Hz\n"
        "Runtime takes max of demands (hard cap 60). Idle => 0 capture.\n"
        "DO: only when this script needs higher than default observe rate.\n"
        "DON'T: forget that runner releases 'script' on do_work exit."
    ),
    "release_fps": (
        "await browser.release_fps(key='script')  # drop a prior request_fps\n"
        "Usually unnecessary — TaskController releases on script end."
    ),
    "script_log": (
        "browser.script_log(msg: str)  # sync; appears in trial log\n"
        "DO: log state entry and key click/wait outcomes."
    ),
    "note_state": (
        "browser.note_state(name: str)  # sync; report current FSM state\n"
        "DO: call every main-loop / run_task iteration (stuck detection)."
    ),
    "note_progress": (
        "browser.note_progress()  # optional manual progress heartbeat"
    ),
    "goto": (
        "await browser.goto(url: str, retries=3)  # LOGIN / web navigation ONLY\n"
        "DO: open the game/login page before image FSM.\n"
        "DON'T: use in daily/sortie image scripts."
    ),
    "dmm_login": (
        "await browser.dmm_login(timeout=30_000)  # milliseconds; LOGIN ONLY\n"
        "Fills DMM email/password from the account and submits.\n"
        "DON'T: scrape the login form with match_image."
    ),
}


def allow_login_from_explanation(explanation: str) -> bool:
    return bool(_LOGIN_HINT_RE.search(explanation or ""))


def lookup_api(name: str, *, allow_login: bool = False) -> str:
    key = (name or "").strip()
    if not key:
        return "lookup_api requires name (e.g. wait_image)."
    # tolerate browser.wait_image
    if "." in key:
        key = key.rsplit(".", 1)[-1]
    key = key.strip("() ")
    allowed = set(ALLOWED_METHODS)
    if allow_login:
        allowed |= LOGIN_METHODS
    card = _CARDS.get(key)
    if key not in allowed:
        listed = ", ".join(sorted(allowed))
        extra = f"\nKnown card (not allowed for this task):\n{card}" if card else ""
        return (
            f"'{key}' is NOT allowed in this script. Allowed methods: {listed}."
            f"{extra}"
        )
    if not card:
        return f"No card for '{key}'. Allowed: {', '.join(sorted(allowed))}."
    return f"## {key}\n{card}"


def compact_catalog(*, allow_login: bool = False) -> str:
    names = list(ALLOWED_METHODS)
    if allow_login:
        names.extend(sorted(LOGIN_METHODS))
    parts = [lookup_api(n, allow_login=allow_login) for n in names]
    return "\n\n".join(parts)


def api_return_types_banner() -> str:
    """短对照条：放进 system / revise，避免 match vs multi 搞混。"""
    return (
        "## Return-type contrast (CRITICAL)\n"
        "- match_image -> MatchResult object (.x .y) or falsy — usually `if hit:`\n"
        "- match_image_multi -> list[dict] with keys 'x','y','score' — "
        "use m['y'], NEVER m.y\n"
        "- max-y / max-x among hits: max(...); then browser.click(best['x'], "
        "best['y'])\n"
        "- click_image: single-template button only; not for picking one of "
        "several multi hits\n"
    )


def api_contracts_block(*, explanation: str = "", allow_login: bool | None = None) -> str:
    """生成/修订 prompt 共用的 API 契约全文。"""
    if allow_login is None:
        allow_login = allow_login_from_explanation(explanation or "")
    login_line = (
        "Login APIs goto / dmm_login are available because this looks like a login/web task.\n"
        if allow_login
        else "Do NOT call page APIs (goto, dmm_login, get_element).\n"
    )
    return (
        "\n## Allowed API contracts (MUST follow; do not invent methods)\n"
        + api_return_types_banner()
        + login_line
        + compact_catalog(allow_login=allow_login)
        + "\n"
    )


def generate_tool_hint(*, allow_login: bool, supports_tools: bool) -> str:
    """始终附带完整 API 卡片；支持 tools 时额外允许 lookup_api 复查。"""
    block = api_contracts_block(allow_login=allow_login)
    if supports_tools:
        return (
            block
            + "\n## API lookup (optional)\n"
            "If still unsure about a method, call lookup_api with "
            '{"name": "wait_image"} (or another allowed name).\n'
        )
    return block


def openai_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "lookup_api",
                "description": (
                    "Look up the short contract card for one allowed UserBrowser method "
                    "(signature, return value, typical do/don't). Use when unsure."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Method name, e.g. wait_image, match_image_multi, click"
                            ),
                        }
                    },
                    "required": ["name"],
                },
            },
        }
    ]


def claude_tools() -> list[dict]:
    return [
        {
            "name": "lookup_api",
            "description": (
                "Look up the short contract card for one allowed UserBrowser method "
                "(signature, return value, typical do/don't). Use when unsure."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Method name, e.g. wait_image, match_image_multi, click"
                        ),
                    }
                },
                "required": ["name"],
            },
        }
    ]


def parse_lookup_name(arguments) -> str:
    """Normalize tool arguments -> method name string."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        s = arguments.strip()
        if not s:
            return ""
        try:
            arguments = json.loads(s)
        except Exception:
            return s
    if isinstance(arguments, dict):
        return str(
            arguments.get("name")
            or arguments.get("method")
            or arguments.get("api")
            or ""
        ).strip()
    return str(arguments).strip()


def dumps_tool_result(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
