# -*- coding: utf-8 -*-
"""协作填表协议：系统发表格 → LLM 只填表 → 系统解析呈现。

字段刻意很少，避免漏填与注意力分散。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Optional

from backend.script_generator.logic_graph import LogicGraph, LogicStep, parse_step_params, step_line_for_model

# ── 标记（兼容旧 END_JSON 等笔误）──
FORM_BEGIN = "<<<COLLAB_FORM>>>"
FORM_END = "<<<END>>>"

_FORM_BLOCK = re.compile(
    r"<<<COLLAB_FORM>>>\s*(\{.*?\})\s*<<<END(?:_FORM|_JSON|_COLLAB(?:_JSON)?)?>>>",
    re.DOTALL | re.IGNORECASE,
)
_FORM_LOOSE = re.compile(r"<<<COLLAB_FORM>>>\s*(\{.*)", re.DOTALL | re.IGNORECASE)

# 兼容旧协议，迁移期仍解析
_OLD_BLOCK = re.compile(
    r"<<<COLLAB_JSON>>>\s*(\{.*?\})\s*<<<END(?:_JSON|_COLLAB(?:_JSON)?)?>>>",
    re.DOTALL | re.IGNORECASE,
)
_OLD_LOOSE = re.compile(r"<<<COLLAB_JSON>>>\s*(\{.*)", re.DOTALL | re.IGNORECASE)

TURN_VALUES = frozenset({"stop", "act", "ask"})
ACTION_VALUES = frozenset(
    {"describe_frame", "match_assets", "match_and_describe", "none", ""}
)
ASK_VALUES = frozenset(
    {"write_files", "runtime_control", "user_choice", "need_frame", ""}
)

EMPTY_FORM: dict[str, Any] = {
    "reply": "",
    "think": "",
    "plan": [],
    "done_when": "",
    "task_done": False,
    "turn": "stop",
    "action": None,
    "ask": None,
    "chips": [],
    "logic_draft": [],
    "focus_assets": [],
    "script": None,
    "script_name": None,
}

FORM_TEMPLATE_TEXT = """<<<COLLAB_FORM>>>
{
  "reply": "",
  "think": "",
  "plan": [],
  "done_when": "",
  "task_done": false,
  "turn": "stop",
  "action": null,
  "ask": null,
  "chips": [],
  "logic_draft": [],
  "focus_assets": [],
  "script": null,
  "script_name": null
}
<<<END>>>"""


def blank_form() -> dict[str, Any]:
    return json.loads(json.dumps(EMPTY_FORM, ensure_ascii=False))


def form_field_guide() -> str:
    return """填表规则（只填这一张表，不要表外再写协议）：
- 不必一次做完。先在 think 里用一两句想清楚（用户看不到 think）。
- plan：还没做完的步骤，最多 5 条，每条一句。做完的删掉。
- done_when：只写当前这一步的完成事实，用这些说法之一：
  点击窗一致、已有 do_work、已有逻辑步骤、已有画面、等用户确认。
- task_done：计划全部做完且 done_when 已经成立才为 true。
- 用户这一轮什么时候结束由你定，系统不会因为 plan 还没做完再叫你。turn=stop 就结束，以后的步骤可以留在 plan 里。turn=act 才表示这一轮还要系统接着做。
- 没完成且要系统查窗口、看图、匹配：turn=act 并填 action。系统做完会把结果写进下一张表，再由你决定结束还是继续。
- 要用户授权、选择或给画面时，把这一步写进 plan（例如「等控制窗口授权」），done_when 写「等用户确认」，turn=ask。后面还要做的步骤留在 plan 里。用户做完后会再给你填一次表，由你决定接着做还是结束。
- 系统自己能做的（查窗口、点一下、匹配）不要把 done_when 写成等用户确认。
- 只有要用户授权或选择时才 turn=ask。口头怎么说都可以，系统看表，不看措辞。
- reply：给用户看的短中文（唯一正文）。不要把 think 或 plan 抄进 reply。
- turn：stop=这一步说完 | act=系统还要做事 | ask=等用户
- action：仅 turn=act 时填其一
    describe_frame | match_assets | match_and_describe | null
- ask：仅 turn=ask 时填其一
    write_files | runtime_control | user_choice | need_frame | null
- chips：≤3 个短选项；user_choice 时必填
- logic_draft：有流程改动才填，如 [{"label":"wait 挑战","asset":"[[id]]"}]；否则 []
- focus_assets：本轮讨论的 [[id]] 列表；空=沿用焦点
- script / script_name：仅要交付可跑成品时填写；平时必须 null
  script 必须含 async def do_work(browser: UserWindow)
  只用窗口上存在的方法，例如 wait_image / click_image / b_sleep
  素材可以直接写 [[素材id]]，系统会换成真实路径；do_work 标注 / import / IMG_DIR 系统会补
  逻辑不会被本地改写。缺 do_work 或调用了不存在的接口时，会把缺口退回让你重修
  禁止 class Script、Assistant、self.wait、self.click"""


def build_form_system_prompt(*, model_name: str = "", tools_available: bool = False) -> str:
    model_line = (
        f"当前模型名：{model_name}"
        if model_name
        else "模型名未知（按用户 API 配置如实说明）"
    )
    tool_line = ""
    if tools_available:
        tool_line = (
            "本轮可以调用工具。要查窗口、改绑、跑临时脚本时先调用工具；"
            "工具结果返回后，再填同一张表。最后一条回复必须是填好的表，不要只调用工具不交表。\n\n"
        )
    return f"""你是 Minashigo「脚本生成 · 协作」助手：和用户一起把游戏自动化脚本做出来。
说话短、贴用户原话；不要念说明书，不要客服腔。

{model_line}
问「你是什么模型」时用上面的名字答。

每轮系统会发给你：【状态摘要】+ 空白表。你只填表（用 {FORM_BEGIN} … {FORM_END} 包住 JSON）。
任务可以分多次填表做完。用户能看见的只有 reply；逻辑图看 logic_draft；正式脚本看 script。think 和 plan 不要出现在 reply 里。

{tool_line}{form_field_guide()}
"""


def _loads_object(blob: str) -> dict[str, Any]:
    s = (blob or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    start = s.find("{")
    if start < 0:
        return {}
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start : i + 1])
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    return {}
    return {}


def _extract_marked(raw: str, block: re.Pattern, loose: re.Pattern) -> tuple[str, dict]:
    m = block.search(raw)
    if m:
        return (raw[: m.start()] + raw[m.end() :]).strip(), _loads_object(m.group(1))
    m2 = loose.search(raw)
    if m2:
        return raw[: m2.start()].strip(), _loads_object(m2.group(1))
    return raw.strip(), {}


def parse_collab_form(text: str) -> tuple[str, dict[str, Any]]:
    """返回 (表外残渣正文, 规范化后的 form dict)。优先新表，兼容旧 COLLAB_JSON。"""
    raw = text or ""
    outside, form = _extract_marked(raw, _FORM_BLOCK, _FORM_LOOSE)
    if not form:
        outside2, form = _extract_marked(raw, _OLD_BLOCK, _OLD_LOOSE)
        if form:
            outside = outside2
            form = migrate_legacy_meta(form, outside)
    if not form and outside.strip().startswith("{") and '"turn"' in outside:
        form = _loads_object(outside)
        if form:
            outside = str(form.get("reply") or form.get("message") or "").strip()
    form = normalize_form(form, fallback_reply=outside)
    reply = str(form.get("reply") or "").strip()
    if not reply and outside.strip() and FORM_BEGIN not in outside:
        form["reply"] = outside.strip()
        reply = form["reply"]
    return reply, form


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in ("1", "true", "yes", "done", "是")


def _normalize_plan(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.splitlines() if x.strip()]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if len(out) >= 5:
            break
        if isinstance(item, str) and item.strip():
            out.append({"step": item.strip()[:80], "done_when": ""})
        elif isinstance(item, dict):
            step = str(item.get("step") or item.get("do") or item.get("label") or "").strip()
            done = str(item.get("done_when") or "").strip()
            if step:
                out.append({"step": step[:80], "done_when": done[:80]})
    return out


_DONE_KIND_KEYS = (
    ("window_aligned", ("点击窗", "hwnd", "改绑", "窗口一致", "渲染窗")),
    ("has_do_work", ("do_work", "成品脚本", "可运行脚本", "脚本已写")),
    ("has_logic", ("逻辑步骤", "已有逻辑", "逻辑已写")),
    ("has_frame", ("已有画面", "已有截图", "已有底图")),
    ("user", ("等用户", "用户确认", "用户授权")),
)


def classify_done_when(text: str) -> str:
    """把完成条件收成系统能核对的一种；对不上则 unknown。"""
    t = (text or "").strip().lower()
    if not t:
        return ""
    for kind, keys in _DONE_KIND_KEYS:
        if any(k.lower() in t for k in keys):
            return kind
    return "unknown"


def check_done_when(kind: str, session, meta: Optional[dict] = None) -> tuple[bool, str]:
    """核对当前步。返回 (是否成立, 给模型看的差距)。"""
    meta = meta or {}
    if kind == "user":
        return False, "这一步要等用户，请 turn=ask，不要自己标完成。"
    if kind == "window_aligned":
        art = getattr(session, "artifacts", None) or {}
        insp = art.get("last_window_inspect") if isinstance(art, dict) else None
        if not isinstance(insp, dict) or not insp:
            return False, "还没有窗口检查结果。请 turn=act，系统会去查点击窗。"
        if insp.get("resolved_differs"):
            return False, "点击窗仍不一致：" + str(insp.get("human_summary") or "")[:180]
        if insp.get("ok"):
            return True, "点击窗已一致。"
        return False, str(insp.get("error") or "窗口检查失败")
    if kind == "has_do_work":
        blob = "\n".join(
            [
                str(meta.get("script") or ""),
                str(getattr(session, "generated_code", "") or ""),
            ]
        )
        if "async def do_work" in blob:
            return True, "脚本里已有 do_work。"
        return False, "还没有可运行脚本。要交付时把 script 填上，含 async def do_work。"
    if kind == "has_logic":
        steps = list(getattr(getattr(session, "logic", None), "steps", None) or [])
        draft = meta.get("logic_draft") or []
        if steps or (isinstance(draft, list) and draft):
            return True, "已有逻辑步骤。"
        return False, "还没有逻辑步骤。有流程就填 logic_draft。"
    if kind == "has_frame":
        try:
            ok = bool(getattr(session, "frame_path", None) and Path(session.frame_path).is_file())
        except Exception:
            ok = False
        if ok:
            return True, "已有画面。"
        return False, "还没有画面。请 turn=ask，ask=need_frame。"
    if kind == "unknown":
        return False, "done_when 请改成：点击窗一致、已有 do_work、已有逻辑步骤、已有画面、等用户确认。"
    return False, "请写 done_when。"


def implied_work(meta: Optional[dict], session) -> str:
    """下一步该做什么。done_when 已经成立时，改看计划正文，避免停在旧条件上。"""
    meta = meta or {}
    done_when = str(meta.get("done_when") or "").strip()
    kind = classify_done_when(done_when)
    ok = False
    if kind:
        ok, _detail = check_done_when(kind, session, meta)
    parts: list[str] = []
    plan = meta.get("plan") or []
    if isinstance(plan, list):
        for item in plan[:3]:
            if isinstance(item, dict):
                parts.append(str(item.get("step") or ""))
            else:
                parts.append(str(item))
    parts.append(str(meta.get("think") or ""))
    blob = "\n".join(parts)
    source = blob if ok else (done_when or blob)
    if kind == "user":
        source = blob or done_when
    if any(k in source for k in ("落点", "吃点击", "被挡", "遮挡", "探针", "点一下", "戳", "点结算", "实测", "看动不动")):
        return "click_probe"
    found = classify_done_when(source)
    if found:
        return found
    if any(k in source for k in ("点击窗", "hwnd", "改绑", "绑错")):
        return "window_aligned"
    return "" if ok else kind


_RESUME_CLICK_KEYS = (
    "落点",
    "吃点击",
    "被挡",
    "遮挡",
    "探针",
    "点一下",
    "戳",
    "点结算",
    "实测点",
    "看动不动",
)


def permission_resume_steps(session, key: str) -> list[str]:
    """授权后续跑时本地补哪几步。空列表表示只再填一次表，不重开工具循环。"""
    if str(key or "").strip() != "runtime_control":
        return []
    art = getattr(session, "artifacts", None)
    if not isinstance(art, dict):
        art = {}
    steps: list[str] = []
    insp = art.get("last_window_inspect")
    if isinstance(insp, dict) and insp.get("resolved_differs"):
        steps.append("retarget")
    parts = [
        str(art.get("agent_think") or ""),
        str(art.get("agent_done_when") or ""),
    ]
    plan = art.get("agent_plan") or []
    if isinstance(plan, list):
        for item in plan[:5]:
            if isinstance(item, dict):
                parts.append(str(item.get("step") or ""))
            else:
                parts.append(str(item))
    for m in reversed(getattr(session, "messages", None) or []):
        if str(m.get("role") or "") == "agent":
            parts.append(str(m.get("text") or ""))
            break
    blob = "\n".join(parts)
    if any(k in blob for k in _RESUME_CLICK_KEYS):
        steps.append("click_probe")
    return steps


_SYSTEM_STEP_KEYS = (
    "落点",
    "吃点击",
    "探针",
    "点一下",
    "戳",
    "点结算",
    "实测",
    "看动不动",
    "匹配",
    "识图",
    "改绑",
    "查窗",
    "检查窗",
)
_USER_STEP_KEYS = (
    "授权",
    "权限",
    "等用户",
    "用户确认",
    "用户选择",
    "要画面",
    "请截",
    "选一",
)


def _plan_step_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("step") or "").strip()
    return str(item or "").strip()


def first_plan_step(meta: Optional[dict]) -> str:
    plan = (meta or {}).get("plan") or []
    if isinstance(plan, list) and plan:
        return _plan_step_text(plan[0])
    return ""


def step_needs_user(step: str, done_when: str = "") -> bool:
    """这一步是等用户，而不是系统自己能做的事。"""
    step = step or ""
    if any(k in step for k in _SYSTEM_STEP_KEYS):
        return False
    if any(k in step for k in _USER_STEP_KEYS):
        return True
    # 步骤本身写了要做什么时，不要被「等用户确认」停住。
    if step:
        return False
    return classify_done_when(done_when) == "user"


def pause_ask_for_step(step: str) -> str:
    step = step or ""
    if any(k in step for k in ("画面", "截图", "底图")):
        return "need_frame"
    if any(k in step for k in ("写文件", "落盘")):
        return "write_files"
    if "临时脚本" in step:
        return "probe_script"
    if any(k in step for k in ("授权", "权限", "控制窗口", "改绑")):
        return "runtime_control"
    return "user_choice"


def hold_for_user(meta: dict, ask: str, step: str) -> None:
    """把用户交互插进计划开头并暂停。后面的步骤保留。"""
    plan: list = []
    raw = meta.get("plan") or []
    if isinstance(raw, list):
        plan = list(raw)
    first = _plan_step_text(plan[0]) if plan else ""
    label = (step or "等用户确认").strip()
    if not step_needs_user(first, ""):
        plan.insert(0, {"step": label, "done_when": "等用户确认"})
    meta["plan"] = plan[:5]
    meta["done_when"] = "等用户确认"
    meta["task_done"] = False
    meta["turn"] = "ask"
    if ask == "need_frame":
        meta["need_frame"] = True
        meta["ask"] = "need_frame"
        meta["need_permission"] = None
    else:
        meta["need_frame"] = False
        meta["need_permission"] = ask
        meta["ask"] = ask if ask in ("write_files", "runtime_control", "user_choice") else None
    chip = {
        "runtime_control": "打开控制窗口权限",
        "write_files": "打开写文件权限",
        "probe_script": "打开临时脚本权限",
        "need_frame": "开始截图",
    }.get(ask)
    if chip:
        chips = [c for c in (meta.get("chips") or []) if str(c) != chip]
        meta["chips"] = ([chip] + chips)[:3]


def surface_plan_pause(meta: Optional[dict]) -> bool:
    """当前步要用户时改成 ask。已经在等用户则保持。"""
    meta = meta or {}
    if str(meta.get("turn") or "") == "ask" or meta.get("need_permission") or meta.get("need_frame"):
        meta["task_done"] = False
        return True
    step = first_plan_step(meta)
    done_when = str(meta.get("done_when") or "").strip()
    if not step_needs_user(step, done_when):
        return False
    hold_for_user(meta, pause_ask_for_step(step), step or "等用户确认")
    return True


def advance_plan_after_user(
    plan,
    *,
    did_click: bool = False,
    did_retarget: bool = False,
) -> list:
    """用户步骤已经过去。去掉暂停步，以及本轮已经做掉的点击/改绑。"""
    out: list = []
    dropped_pause = False
    for item in plan or []:
        step = _plan_step_text(item)
        if not dropped_pause and step_needs_user(step, ""):
            dropped_pause = True
            continue
        if did_click and any(k in step for k in _RESUME_CLICK_KEYS):
            continue
        if did_retarget and any(k in step for k in ("改绑", "点击窗", "绑错")):
            continue
        if isinstance(item, dict):
            out.append(item)
        elif step:
            out.append({"step": step, "done_when": ""})
    return out[:5]


def plan_should_continue(meta: Optional[dict], session) -> tuple[bool, str]:
    """只有模型把 turn 写成 act 才再发一张表。stop / ask 由模型结束这一轮。"""
    meta = meta or {}
    if str(meta.get("turn") or "") != "act":
        return False, ""
    if meta.get("need_permission") or meta.get("need_frame"):
        return False, ""
    plan = meta.get("plan") or []
    done_when = str(meta.get("done_when") or "").strip()
    if not done_when and isinstance(plan, list) and plan:
        first_item = plan[0] if isinstance(plan[0], dict) else {}
        done_when = str(first_item.get("done_when") or "").strip() if isinstance(first_item, dict) else ""
    if not plan and not done_when:
        return False, ""
    kind = classify_done_when(done_when)
    ok, detail = check_done_when(kind, session, meta)
    first = first_plan_step(meta)
    if step_needs_user(first, done_when) and not any(k in first for k in _SYSTEM_STEP_KEYS):
        return False, ""
    if kind == "user":
        return True, (
            f"当前步「{first or '系统可做'}」不要等用户。"
            "改 done_when，继续做完。"
        )
    first = ""
    plan = meta.get("plan") or []
    if isinstance(plan, list) and plan:
        item = plan[0]
        first = str(item.get("step") if isinstance(item, dict) else item or "").strip()
    if bool(meta.get("task_done")) and ok:
        return False, ""
    if bool(meta.get("task_done")) and not ok:
        return True, "你标了 task_done，但核对没过：" + detail
    if ok and first:
        return True, (
            f"done_when 已经成立（{detail}），但计划还没做完。"
            f"下一步：{first}。请改 done_when，不要 task_done。"
        )
    tail = f" 下一步：{first}。" if first else ""
    return True, (detail or "计划还没做完。") + tail


def form_ui_state(meta: Optional[dict], session=None) -> dict[str, Any]:
    """界面按钮和状态只跟这张表走，不按本地阶段另算一套。"""
    meta = meta or {}
    plan = meta.get("plan") if isinstance(meta.get("plan"), list) else []
    step = ""
    if plan:
        item = plan[0]
        step = str(item.get("step") if isinstance(item, dict) else item or "").strip()
    turn = str(meta.get("turn") or "stop")
    asking = turn == "ask" or bool(meta.get("need_permission")) or bool(meta.get("need_frame"))
    # 只有模型写了 turn=act，界面才显示「还没做完」。stop 表示这一轮已经结束。
    unfinished = (
        turn == "act"
        and bool(plan)
        and not bool(meta.get("task_done"))
        and not asking
    )
    chips = [str(c).strip() for c in (meta.get("chips") or []) if str(c).strip()][:3]
    has_script = bool(str(meta.get("script") or "").strip()) and not collab_script_issues(
        str(meta.get("script") or ""), session
    )
    if asking:
        return {
            "mode": "ask",
            "activity": "等你操作后继续",
            "chips": chips,
            "cta_enabled": False,
            "cta_text": "",
            "continue_step": "",
        }
    if unfinished:
        if "继续" not in chips:
            chips = (["继续"] + chips)[:3]
        return {
            "mode": "continue",
            "activity": "还没做完" + (f"：{step}" if step else ""),
            "chips": chips,
            "cta_enabled": False,
            "cta_text": "",
            "continue_step": step,
        }
    if has_script:
        if "去试运行" not in chips:
            chips = (["去试运行"] + chips)[:3]
        return {
            "mode": "trial",
            "activity": "",
            "chips": chips,
            "cta_enabled": True,
            "cta_text": "去试运行",
            "continue_step": "",
        }
    return {
        "mode": "idle",
        "activity": "",
        "chips": chips,
        "cta_enabled": None,
        "cta_text": "",
        "continue_step": "",
    }


def remember_plan(session, meta: Optional[dict]) -> None:
    """计划留在会话里，下一张状态表能看见。不进用户聊天。"""
    meta = meta or {}
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    plan = meta.get("plan") or []
    if not isinstance(plan, list):
        plan = []
    session.artifacts["agent_plan"] = plan
    session.artifacts["agent_done_when"] = str(meta.get("done_when") or "")
    session.artifacts["agent_task_done"] = bool(meta.get("task_done"))
    if meta.get("task_done"):
        session.artifacts.pop("agent_plan_gap", None)
    think = str(meta.get("think") or "").strip()
    if think:
        session.artifacts["agent_think"] = think[:400]


def migrate_legacy_meta(meta: dict[str, Any], visible: str = "") -> dict[str, Any]:
    """旧 COLLAB_JSON → 新表字段。"""
    m = dict(meta or {})
    out = blank_form()
    out["reply"] = str(m.get("reply") or m.get("message") or visible or "").strip()
    turn = str(m.get("turn") or m.get("status") or "stop").strip().lower()
    aliases = {
        "done": "stop",
        "finish": "stop",
        "finished": "stop",
        "complete": "stop",
        "continue": "act",
        "action": "act",
        "wait": "ask",
        "await": "ask",
    }
    turn = aliases.get(turn, turn)
    out["turn"] = turn if turn in TURN_VALUES else "stop"
    if m.get("match_assets") and (m.get("describe_frame") or m.get("need_vision")):
        out["action"] = "match_and_describe"
    elif m.get("match_assets"):
        out["action"] = "match_assets"
    elif m.get("describe_frame") or m.get("need_vision"):
        out["action"] = "describe_frame"
    need = str(m.get("need_permission") or "").strip()
    if need in ASK_VALUES:
        out["ask"] = need
    elif m.get("need_frame"):
        out["ask"] = "need_frame"
    chips = m.get("chips") or []
    if isinstance(chips, list):
        out["chips"] = [str(c).strip() for c in chips if str(c).strip()][:3]
    if out["ask"] and out["turn"] == "stop":
        out["turn"] = "ask"
    if out["action"] and out["turn"] == "stop":
        out["turn"] = "act"
    if m.get("logic_draft"):
        out["logic_draft"] = m.get("logic_draft")
    if m.get("script"):
        out["script"] = m.get("script")
    if m.get("script_name"):
        out["script_name"] = m.get("script_name")
    return out


def normalize_form(form: Optional[dict], *, fallback_reply: str = "") -> dict[str, Any]:
    """纠错非法组合，产出干净表。"""
    out = blank_form()
    src = form if isinstance(form, dict) else {}

    reply = str(src.get("reply") or src.get("message") or "").strip()
    if not reply:
        reply = (fallback_reply or "").strip()
    out["reply"] = reply

    turn = str(src.get("turn") or "").strip().lower()
    aliases = {
        "done": "stop",
        "finish": "stop",
        "finished": "stop",
        "complete": "stop",
        "continue": "act",
        "action": "act",
        "wait": "ask",
        "await": "ask",
        "await_user": "ask",
    }
    turn = aliases.get(turn, turn)
    if turn not in TURN_VALUES:
        turn = "stop"

    action = src.get("action")
    if action is None or action is False:
        action = ""
    action = str(action).strip().lower()
    if action in ("null", "none", "nil"):
        action = ""
    if action not in ACTION_VALUES:
        if src.get("match_assets") and (src.get("describe_frame") or src.get("need_vision")):
            action = "match_and_describe"
        elif src.get("match_assets"):
            action = "match_assets"
        elif src.get("describe_frame") or src.get("need_vision"):
            action = "describe_frame"
        else:
            action = ""

    ask = src.get("ask")
    if ask is None or ask is False:
        ask = ""
    ask = str(ask).strip()
    if ask in ("null", "none", "nil"):
        ask = ""
    if not ask:
        ask = str(src.get("need_permission") or "").strip()
    if ask not in ASK_VALUES:
        ask = ""

    chips_raw = src.get("chips") or []
    chips: list[str] = []
    if isinstance(chips_raw, list):
        for c in chips_raw:
            t = str(c).strip()
            if t and t not in chips:
                chips.append(t)
            if len(chips) >= 3:
                break

    if ask and turn == "stop":
        turn = "ask"
    if action and turn == "stop":
        turn = "act"
    if turn == "act" and not action:
        action = "describe_frame"
    if turn == "ask" and not ask:
        ask = "user_choice"

    pending_ask = ""
    if turn == "act" and ask in ("write_files", "runtime_control"):
        pending_ask = ask
        ask = ""
    if turn != "act":
        action = ""
    if turn != "ask":
        ask = ""
    if pending_ask:
        out["_ask_after_act"] = pending_ask

    logic_draft = src.get("logic_draft") or []
    if not isinstance(logic_draft, list):
        logic_draft = []

    focus = src.get("focus_assets") or []
    if not isinstance(focus, list):
        focus = []
    focus_assets = [str(x).strip() for x in focus if str(x).strip()][:8]

    script = src.get("script")
    if script is not None:
        script = str(script).strip() or None
    script_name = src.get("script_name")
    if script_name is not None:
        script_name = str(script_name).strip() or None

    out["turn"] = turn
    out["action"] = action or None
    out["ask"] = ask or None
    out["chips"] = chips
    out["logic_draft"] = logic_draft
    out["focus_assets"] = focus_assets
    out["script"] = script
    out["script_name"] = script_name
    out["think"] = str(src.get("think") or "").strip()[:400]
    plan = _normalize_plan(src.get("plan"))
    out["plan"] = plan
    done_when = str(src.get("done_when") or "").strip()[:80]
    if not done_when and plan:
        done_when = plan[0].get("done_when") or ""
    out["done_when"] = done_when
    if "task_done" not in src and not plan:
        out["task_done"] = True
    else:
        out["task_done"] = _as_bool(src.get("task_done")) if "task_done" in src else False
    return out


def form_to_runtime_meta(form: dict[str, Any]) -> dict[str, Any]:
    """新表 → 运行时仍认识的 meta。"""
    f = normalize_form(form)
    turn = f["turn"]
    action = f.get("action") or ""
    ask = f.get("ask") or ""
    meta: dict[str, Any] = {
        "turn": turn,
        "chips": list(f.get("chips") or []),
        "logic_draft": list(f.get("logic_draft") or []),
        "focus_assets": list(f.get("focus_assets") or []),
        "script": f.get("script"),
        "script_name": f.get("script_name"),
        "reply": f.get("reply") or "",
        "think": f.get("think") or "",
        "plan": list(f.get("plan") or []),
        "done_when": f.get("done_when") or "",
        "task_done": bool(f.get("task_done")),
        "describe_frame": action in ("describe_frame", "match_and_describe"),
        "match_assets": action in ("match_assets", "match_and_describe"),
        "need_vision": action in ("describe_frame", "match_and_describe"),
        "need_permission": None,
        "need_frame": False,
        "analyze_frame": False,
        "select_skill": None,
        "action": action or None,
        "ask": ask or None,
    }
    if f.get("_ask_after_act"):
        meta["_ask_after_act"] = f["_ask_after_act"]
    if turn == "ask":
        if ask in ("write_files", "runtime_control"):
            meta["need_permission"] = ask
            label = {
                "write_files": "打开写文件权限",
                "runtime_control": "打开控制窗口权限",
            }.get(ask)
            chips = list(meta["chips"])
            if label and label not in chips:
                meta["chips"] = ([label] + chips)[:3]
        elif ask == "need_frame":
            meta["need_frame"] = True
    return meta


def logic_draft_to_graph(draft: list, *, title: str = "脚本草稿") -> LogicGraph:
    from backend.script_generator.logic_graph import StepBind

    steps: list[LogicStep] = []
    for item in draft or []:
        if isinstance(item, str):
            label = item.strip()
            asset = ""
        elif isinstance(item, dict):
            label = str(
                item.get("label") or item.get("do") or item.get("text") or ""
            ).strip()
            asset = str(item.get("asset") or item.get("asset_id") or "").strip()
        else:
            continue
        if not label:
            continue
        aid = ""
        m = re.search(r"\[\[([^\]]+)\]\]", asset or label)
        if m:
            aid = m.group(1).strip()
        elif asset and not asset.startswith("["):
            aid = asset
        hint = f"素材 [[{aid}]]" if aid else "草稿"
        bind = StepBind(images=[aid] if aid else [], notes="draft")
        steps.append(
            LogicStep(
                id=f"draft_{len(steps) + 1}",
                label=label[:80],
                hint=hint,
                status="draft",
                bind=bind,
                params=parse_step_params(label[:80]),
            )
        )
        if len(steps) >= 16:
            break
    return LogicGraph(title=title, architecture="scene_driven", steps=steps)


_ASSET_REF_RE = re.compile(r"(['\"]?)\[\[\s*([^\[\]\s]+?)\s*\]\]\1")
_TARGET_ANNOTATION_OK = ("UserBrowser", "UserWindow", "Browser")
_TARGET_IMPORTS: tuple[tuple[str, str, str], ...] = (
    (
        "UserBrowser",
        "backend.browser.user_browser",
        "from backend.browser.user_browser import UserBrowser",
    ),
    (
        "UserWindow",
        "backend.automation.user_window",
        "from backend.automation.user_window import UserWindow",
    ),
)


def _py_literal(value: Any) -> str:
    """字符串字面量：Windows 路径统一正斜杠，避免转义歧义。"""
    s = str(value).replace("\\", "/")
    if "'" in s:
        return '"' + s.replace('"', '\\"') + '"'
    return "'" + s + "'"


def _char_col(line: str, byte_col: int) -> int:
    """ast 的 col_offset 是 UTF-8 字节偏移，换算成字符下标。"""
    if byte_col <= 0:
        return 0
    raw = line.encode("utf-8")
    if len(raw) == len(line):
        return byte_col
    return len(raw[:byte_col].decode("utf-8", "ignore"))


def _insert_after_imports(code: str, lines: list[str]) -> str:
    """把若干顶层语句插到最后一条 import 之后（无 import 时避开头部注释）。"""
    if not lines:
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, int(getattr(node, "end_lineno", 0) or 0))
    block = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)
    src = code.splitlines(keepends=True)
    if last <= 0:
        head = 0
        for i, ln in enumerate(code.splitlines()):
            if ln.startswith("#"):
                head = i + 1
            else:
                break
        return "".join(src[:head]) + block + "".join(src[head:])
    return "".join(src[:last]) + block + "".join(src[last:])


def collab_image_dir(session) -> str:
    """协作素材目录（脚本 IMG_DIR 兜底）：优先素材区，其次底图。"""
    for a in list(getattr(session, "assets", None) or []):
        try:
            p = Path(str((a or {}).get("path") or ""))
        except Exception:
            continue
        if p.is_file():
            return str(p.parent.resolve())
    fp = str(getattr(session, "frame_path", "") or "").strip()
    if fp:
        p = Path(fp)
        if p.is_file():
            return str(p.parent.resolve())
    return ""


def _find_asset_path(session, ref: str) -> Optional[Path]:
    from backend.script_generator.collaborator.canvas import find_asset

    assets = list(getattr(session, "assets", None) or [])
    hit = find_asset(assets, asset_id=ref) or find_asset(assets, name=ref)
    if hit is None:
        return None
    p = Path(str(hit.get("path") or ""))
    return p if str(p).strip() else None


def substitute_asset_refs(code: str, session) -> tuple[str, list[str], list[str]]:
    """把脚本里的 [[asset_id]] 换成真实路径。返回 (代码, 已换 id, 未解析 id)。"""
    text = code or ""
    if not text or "[[" not in text:
        return text, [], []
    done: list[str] = []
    unresolved: list[str] = []

    def _sub(m: re.Match) -> str:
        ref = (m.group(2) or "").strip()
        path = _find_asset_path(session, ref) if ref else None
        if path is None:
            if ref and ref not in unresolved:
                unresolved.append(ref)
            return m.group(0)
        if ref not in done:
            done.append(ref)
        return _py_literal(path)

    return _ASSET_REF_RE.sub(_sub, text), done, unresolved


def _has_symbol_import(code: str, module: str, symbol: str) -> bool:
    return bool(
        re.search(
            r"^\s*(?:from\s+"
            + re.escape(module)
            + r"\s+import\s+[^\n]*\b"
            + re.escape(symbol)
            + r"\b|import\s+"
            + re.escape(module)
            + r"\b)",
            code,
            re.M,
        )
    )


def ensure_target_imports(code: str) -> tuple[str, list[str]]:
    """用到 UserBrowser / UserWindow 却没 import 时补上。"""
    todo = [
        line
        for symbol, module, line in _TARGET_IMPORTS
        if symbol in code and not _has_symbol_import(code, module, symbol)
    ]
    if not todo:
        return code, []
    return _insert_after_imports(code, todo), ["补 UserBrowser / UserWindow import"]


def ensure_img_dir(code: str, session) -> tuple[str, list[str]]:
    """试运行闸要求有 IMG_DIR 赋值；缺了就按协作素材目录补一行。"""
    if re.search(r"(?m)^\s*IMG_DIR\s*=", code):
        return code, []
    if re.search(r"(?m)^\s*from\s+\S+\s+import\s+[^\n]*\bIMG_DIR\b", code):
        return code, []
    hint = collab_image_dir(session)
    if not hint:
        return code, []
    if not re.search(
        r"(?m)^\s*(?:from\s+pathlib\s+import\s+[^\n]*\bPath\b|import\s+pathlib\b)",
        code,
    ):
        code = _insert_after_imports(code, ["from pathlib import Path"])
    return _insert_after_imports(
        code, [f"IMG_DIR = Path({_py_literal(hint)})"]
    ), ["补 IMG_DIR"]


def _fix_do_work_annotation(code: str) -> tuple[str, list[str]]:
    """do_work 首个参数标注非法（Any / 缺失）时改成 UserBrowser | UserWindow。

    与试运行闸同一条判据：标注里须含 UserBrowser / UserWindow / Browser。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    dw = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "do_work"
        ),
        None,
    )
    if dw is None or not dw.args.args:
        return code, []
    arg = dw.args.args[0]
    ann = arg.annotation
    ann_src = ast.unparse(ann) if ann is not None else ""
    if any(t in ann_src for t in _TARGET_ANNOTATION_OK):
        return code, []
    target = "UserBrowser | UserWindow"
    src = code.splitlines(keepends=True)
    if ann is not None and getattr(ann, "end_lineno", None) == ann.lineno:
        line = src[ann.lineno - 1]
        line = (
            line[: _char_col(line, ann.col_offset)]
            + target
            + line[_char_col(line, ann.end_col_offset) :]
        )
        src[ann.lineno - 1] = line
    elif ann is None and getattr(arg, "end_col_offset", None):
        line = src[arg.lineno - 1]
        end = _char_col(line, arg.end_col_offset)
        src[arg.lineno - 1] = line[:end] + ": " + target + line[end:]
    else:
        newer, n = re.subn(
            r"(async\s+def\s+do_work\s*\(\s*\w+\s*:\s*)([^,)\n]+)",
            r"\1" + target,
            code,
            count=1,
        )
        if not n:
            return code, []
        src = newer.splitlines(keepends=True)
    out = "".join(src)
    try:
        ast.parse(out)
    except SyntaxError:
        return code, []
    return out, [f"do_work 标注改为 {target}"]


def normalize_collab_script(code: str, session) -> tuple[str, list[str]]:
    """协作出码的机械归一：素材占位符 / do_work 标注 / import / IMG_DIR。

    只做不改逻辑的确定性修补；解析不了就原样返回，交给回合退回重修。
    """
    text = (code or "").strip()
    if not text:
        return text, []
    try:
        ast.parse(text)
    except SyntaxError:
        return text, []
    notes: list[str] = []
    text, replaced, _unresolved = substitute_asset_refs(text, session)
    if replaced:
        notes.append(f"素材引用换成路径（{len(replaced)} 处）")
    try:
        from backend.script_generator.agent import patch_do_work_dual_target

        text, n_patch = patch_do_work_dual_target(text)
        notes.extend(n_patch)
    except Exception:
        pass
    text, n_ann = _fix_do_work_annotation(text)
    notes.extend(n_ann)
    text, n_imp = ensure_target_imports(text)
    notes.extend(n_imp)
    text, n_img = ensure_img_dir(text, session)
    notes.extend(n_img)
    return text.strip(), notes


def collab_script_issues(code: str, session=None) -> list[str]:
    """协作脚本的交付闸：结构 + 窗口接口，并与试运行同一道闸（require_fsm=False）。"""
    text = (code or "").strip()
    if not text:
        return []
    if session is not None:
        text, _notes = normalize_collab_script(text, session)
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"语法错误: {e.msg}（第 {e.lineno} 行）"]
    issues: list[str] = []
    if session is not None:
        _code, _ok, unresolved = substitute_asset_refs(text, session)
        if unresolved:
            issues.append("素材引用没有对应素材: " + ", ".join(unresolved[:4]))
    dw = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "do_work"
        ),
        None,
    )
    if dw is None:
        return ["缺少 async def do_work"]
    browser = dw.args.args[0].arg if dw.args.args else ""
    if not browser:
        return ["do_work 缺少窗口参数"]
    allowed = _window_api_names()
    missing: list[str] = []
    for node in ast.walk(dw):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name) or owner.id != browser:
            continue
        name = node.func.attr
        if name.startswith("_") or name not in allowed:
            missing.append(name)
    issues.extend(
        f"不存在的接口 {browser}.{name}" for name in dict.fromkeys(missing)
    )
    # 与试运行页同一道闸，避免「协作说好了、点试跑说没过」
    try:
        from backend.script_generator.agent import validate_script_local

        for err in validate_script_local(
            text,
            explanation="",
            source_dir="",
            free_mode=True,
            require_fsm=False,
        ):
            if err not in issues:
                issues.append(err)
    except Exception:
        pass
    return issues


def _window_api_names() -> set[str]:
    from backend.automation.user_window import UserWindow

    names: set[str] = set()
    for name in dir(UserWindow):
        if name.startswith("_"):
            continue
        if callable(getattr(UserWindow, name, None)):
            names.add(name)
    return names


def coerce_runnable_script(code: str, session) -> str:
    """交付前做机械归一（素材引用 / 签名 / import / IMG_DIR），不改业务逻辑。

    补不掉的问题（缺 do_work、接口不存在等）仍由回合退回重修。
    """
    text, _notes = normalize_collab_script(code, session)
    return (text or "").strip()

def build_status_sheet(
    session,
    *,
    vision_prefetch: str = "",
    runtime_block: str = "",
    permissions: Optional[dict] = None,
    last_user: str = "",
) -> str:
    """每轮发给 LLM 的状态摘要（不是聊天流水账）。"""
    from pathlib import Path

    from backend.script_generator.collaborator.probe_script import normalize_permissions
    from backend.script_generator.collaborator.canvas import (
        assets_summary_for_prompt,
        overlays_summary_for_prompt,
    )

    perms = normalize_permissions(permissions)
    has_frame = False
    try:
        has_frame = bool(session.frame_path and Path(session.frame_path).is_file())
    except Exception:
        has_frame = False

    logic = getattr(session, "logic", None)
    n_steps = len(getattr(logic, "steps", None) or [])
    title = str(getattr(logic, "title", "") or "")
    goal = str(getattr(session, "goal_text", "") or "")
    phase = getattr(session, "phase", None)
    phase_s = phase.value if hasattr(phase, "value") else str(phase or "")

    asset_block = assets_summary_for_prompt(
        list(getattr(session, "assets", None) or []),
        list(getattr(session, "focused_asset_ids", None) or []),
    )
    overlay_block = overlays_summary_for_prompt(
        list(getattr(session, "overlays", None) or [])
    )
    draft_pseudo = ""
    if isinstance(getattr(session, "artifacts", None), dict):
        draft_pseudo = str(session.artifacts.get("draft_pseudo") or "").strip()

    lines = [
        "【状态摘要】",
        f"- phase: {phase_s}",
        f"- goal: {goal or '无'}",
        f"- 已有底图: {'是' if has_frame else '否'}",
        f"- 逻辑步数: {n_steps}" + (f"（{title}）" if title else ""),
        f"- 权限: write_files={perms.get('write_files')} · "
        f"runtime_control={perms.get('runtime_control')} · "
        f"probe_script={perms.get('probe_script')}",
        f"- 已有成品脚本: "
        f"{'是' if (getattr(session, 'generated_code', None) or '').strip() else '否'}",
    ]
    if last_user:
        lines.append(f"- 用户本轮：「{last_user[:200]}」")
    lines.append("")
    lines.append("【素材区】")
    lines.append(asset_block or "（空）")
    lines.append("")
    lines.append("【共同视口标注】")
    lines.append(overlay_block or "（无）")
    if (vision_prefetch or "").strip():
        lines.append("")
        lines.append("【本轮画面/匹配结果——当作你已看见】")
        lines.append(vision_prefetch.strip())
    if (runtime_block or "").strip():
        lines.append("")
        lines.append(runtime_block.strip())
    if draft_pseudo and n_steps == 0:
        lines.append("")
        lines.append("【已有伪代码草稿】")
        lines.append(draft_pseudo[:1200])
    if n_steps and logic is not None:
        lines.append("")
        lines.append("【当前逻辑步骤】")
        for i, s in enumerate(list(logic.steps)[:12], 1):
            lines.append(f"{i}. {step_line_for_model(s)}")
    art = getattr(session, "artifacts", None) or {}
    plan = art.get("agent_plan") if isinstance(art, dict) else None
    if isinstance(plan, list) and plan:
        lines.append("")
        lines.append("【你的计划】（用户看不到；没完成就更新这张表）")
        for i, item in enumerate(plan[:5], 1):
            if isinstance(item, dict):
                lines.append(f"{i}. {item.get('step') or ''}")
            else:
                lines.append(f"{i}. {item}")
        dw = str(art.get("agent_done_when") or "").strip()
        if dw:
            lines.append(f"- 当前 done_when: {dw}")
        gap = str(art.get("agent_plan_gap") or "").strip()
        if gap:
            lines.append("【计划核对】")
            lines.append(gap[:800])
        if art.get("agent_task_done"):
            lines.append("- 你上次标了 task_done，以本轮核对为准")
    lines.append("")
    lines.append("请根据以上状态填表（只输出填好的表）：")
    lines.append(FORM_TEMPLATE_TEXT)
    return "\n".join(lines)


def stream_form_preview(buf: str) -> str:
    """流式时隐藏未完成的表尾巴；若已有 reply 字段可预览。"""
    raw = buf or ""
    for mark in (FORM_BEGIN, "<<<COLLAB_JSON>>>"):
        idx = raw.find(mark)
        if idx >= 0:
            head = raw[:idx].rstrip()
            tail = raw[idx + len(mark) :]
            m = re.search(r'"reply"\s*:\s*"((?:\\.|[^"\\])*)"', tail)
            if m:
                try:
                    reply = json.loads(f'"{m.group(1)}"')
                    return str(reply)
                except Exception:
                    pass
            return head
    for mark in (FORM_BEGIN, "<<<COLLAB_JSON>>>"):
        for n in range(len(mark) - 1, 0, -1):
            if raw.endswith(mark[:n]):
                return raw[:-n].rstrip()
    return raw
