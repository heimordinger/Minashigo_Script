# -*- coding: utf-8 -*-
"""协作填表协议：系统发表格 → LLM 只填表 → 系统解析呈现。

字段刻意很少，避免漏填与注意力分散。
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime
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
# 内部自动续跑被预算暂停时给用户的一键续跑入口（见 form_ui_state）
_RESUME_CHIP = "接着做完"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

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
    "intro": None,
    "script": None,
    "script_name": None,
    "answer": None,
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
  "intro": null,
  "script": null,
  "script_name": null,
  "answer": null
}
<<<END>>>"""


def blank_form() -> dict[str, Any]:
    return json.loads(json.dumps(EMPTY_FORM, ensure_ascii=False))


def form_field_guide() -> str:
    return """填表规则（只填这一张表，不要表外再写协议）：
- 不必一次做完。先在 think 里用一两句想清楚（用户看不到 think）。
- plan：还没做完的步骤，最多 5 条，每条一句。做完的删掉。
  每步可带 needs 数组，写清这一步靠什么完成：
  "tool:crop_asset"（要用的工具，必须在工具表里存在）/ "permission:write_files"（要授权）
  / "user:确认"（要用户动手）。工具表里没有的能力**自己补上**：
  用 run_probe_script 写脚本做出来，再用 define_tool 登记成 "tool:你自己起的名字"；
  不要因为「现在没有这个工具」就把步骤推给用户。要用未授权能力必须 turn=ask + 授权芯片。
- done_when：只写当前这一步的完成事实，用这些说法之一：
  点击窗一致、已有 do_work、已有逻辑步骤、已有画面、已有裁剪素材、已有素材检查、已有匹配结果、已有标注图、等用户确认。
  「已有裁剪素材 / 已有匹配结果 / 已有标注图」系统会去核对产物：没有产物不算完成。
- 裁剪成功只说明文件存在，不说明内容正确；依赖裁剪内容的下一步应先用 inspect_asset 复核。
- 「素材区」不是一张自动可见的图。用户让你看素材区、比较素材或找共同元素时：先调用
  list_session_assets 获取准确清单，再对全部相关素材调用 compare_assets；不要只 inspect_asset
  一张就声称翻过/比较过整个素材区。需要跨图结论时，问题中提到的每张图都必须在 asset_ids 里。
- 用户明确说「素材区里的图片」时，不要直接 describe_current_frame：要么 inspect_asset / compare_assets
  查看素材原图，要么先 show_asset_in_viewport 把指定素材切到共同视口，再查看/讨论。用户说“展示、
  切到、放到共同视口”时应主动切换；这只是本地视图变化，不代表用户发了新消息。
- compare_assets 一次最多 9 张；更多时按语义/名称分组比较，再汇总结论。列表里的名称、焦点、
  匹配分数不等于已经看过图片内容；只有 inspect_asset / compare_assets 的检查记录算看过。
- 匹配必须区分模板与目标；禁止整张截图匹配自身，并记录目标素材作为证据。
- 同一模板要找多个卡片/按钮/格子时直接调用 match_template_multi，并给 expected_count（已知时）；
  不要把单点 match_template 当成多目标，也不要为此临时手写 cv2 探针。若返回 ambiguous、
  saturated 或 count_warning，先调整模板/阈值/min_distance/search_region，不得把候选数当目标数。
- 语义目标裁剪必须先调用 describe_current_frame/inspect_asset 定位，随后把返回的 grounding_id
  作为 crop_asset.vision_candidate_id；禁止自己猜坐标。裁剪后必须 inspect_asset(mode=verify)，
  verified=true 才能把它用于 match_template。若 verified=false，重新定位，不得硬继续。
- task_done：计划全部做完且 done_when 已经成立才为 true。
- 手不够用就自己造：想做的事在工具表里**没有对应工具**时，不要停下来反问用户，
  也不要把做不到的步骤写进 plan。先用 run_probe_script 写一段脚本把它做出来；
  跑通后用 define_tool 登记成可复用工具（传 name/description/parameters/code，
  脚本里参数从 ARGS 取、结论赋给 result），之后每轮工具表都会带上它。
  同理，用户说「在图上标出来 / 圈一下 / 标出这些位置」时直接用 mark_on_image，
  不要手写 cv2 探针，也不要因为「没有标注工具」就回一句做不到。
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
- 没有 focus 这回事：系统不会替你预选素材。要哪张就用 list_session_assets 查出来、
  用 asset_id 点名，别指望省略参数会有默认值。
- 所有素材、工具、技能和证据都在资源库中版本化。需要资源先用 search_resources；调用时使用
  精确 resource_id/asset_id。superseded/invalid/archived 资源禁止继续使用，也不要从旧对话猜路径。
- 新裁剪默认是 candidate，必须 inspect_asset 复核；verified 后才能当稳定模板。
- intro：用户确认逻辑后，把流程写成「生成用介绍」，交给经典生成器出码（不用你写代码）。
  分段写全，素材一律写成 `素材名.png`（用素材区里的名字）：
    目的：一句话
    场景标识：每行 `素材.png`：这是哪个界面
    图片说明：每行 `素材.png`：按钮/标识的含义
    任务流程：（1）步骤名  然后换行写编号小步，指向要点的 `素材.png`
    特殊规则：（可选）异常/循环/完成条件
  平时 null；只有走到「写出码介绍」这一步才填。
- script / script_name：仅要交付可跑成品时填写；平时必须 null
  script 必须含 async def do_work(browser: UserWindow)
  只用窗口上存在的方法，例如 wait_image / click_image / b_sleep
  素材可以直接写 [[素材id]]，系统会换成真实路径；do_work 标注 / import / IMG_DIR 系统会补
  逻辑不会被本地改写。缺 do_work 或调用了不存在的接口时，会把缺口退回让你重修
  禁止 class Script、Assistant、self.wait、self.click
- answer：兼容字段，**只有用户明确报告当前图片的观察结果时才填**，其余必须 null。
  系统只会把它绑定到当时那张图片，作为“用户说法”留档；它不是标准答案，不能套到
  其他图片，也不能覆盖工具/视觉证据。必须先独立测量再对照；不一致就如实报告并核验，
  不能为了迎合 answer 调参。规则说明（如“每张卡固定 5 个槽”）不要填进 answer。
  没有可靠证据时回答“未知”，禁止编造“标准答案”。填空字符串等于没填。"""


def build_form_system_prompt(
    *, model_name: str = "", tools_available: bool = False, primary_visual: bool = False
) -> str:
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
    visual_line = (
        "当前主模型支持视觉；共同视口不会自动附图，需要看时调用 "
        "describe_current_frame / inspect_asset，工具会优先使用当前主模型自身视觉。"
        "不要声称自己必须依赖辅助识图模型。"
        if primary_visual
        else
        "当前主模型未声明视觉能力；需要看图时调用 describe_current_frame / inspect_asset，"
        "系统会在可用时调用独立辅助识图。"
    )
    return f"""你是 Minashigo「脚本生成 · 协作」助手：和用户一起把游戏自动化脚本做出来。
说话短、贴用户原话；不要念说明书，不要客服腔。

{model_line}
问「你是什么模型」时用上面的名字答。
{visual_line}

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


# 正文里是否真的带了填表块（新表或旧 COLLAB_JSON）。
def collab_form_present(text: str) -> bool:
    raw = text or ""
    return any(p.search(raw) for p in (_FORM_BLOCK, _FORM_LOOSE, _OLD_BLOCK, _OLD_LOOSE))


# 英文推理独白信号：第一人称自问自答 / 检查内部结构时的口头语。
_REASONING_MARK_RE = re.compile(
    r"\b(let me|let's|lets|i can see|i'?ll|i will|i need to|i should|i want to|"
    r"i'?m going to|now let|checking|looking at|the issue|the problem|"
    r"that'?s why|this is exactly|not threshold)\b",
    re.IGNORECASE,
)
# 提到内部实现细节的词：出现即说明这是模型在自言自语，不是对用户说话。
_INTERNAL_TOKEN_RE = re.compile(
    r"(`|```|\.py\b|\.png\b|\.json\b|_PATH\b|_ID\b|asset_id|list_assets|"
    r"list_session_assets|session asset|probe\b|threshold|hits\b|returns|"
    r"\bdict\b|\bNone\b|\bargs\b|\bARGS\b)",
    re.IGNORECASE,
)
# 给用户回话时常见的短语：命中就按"是给用户看的"放过。
_ADDRESSED_TO_USER_RE = re.compile(r"\blet me know\b", re.IGNORECASE)


def looks_like_reasoning(text: str) -> bool:
    """这段表外正文是不是模型的内心独白（不该当 reply 上屏）。

    真实会话里模型的英文 CoT 会整段漏到表外，例如
    ``FRAME_PATH may not be defined here. Let me check available variables…``
    ``I can see earlier test products…``
    ```list_assets()` in the probe returns something else. Let me just print…``
    这些既不是给用户的答复（用户说中文），也不含任何结论，必须丢掉。

    判据用"推理句式 + 内部细节"双信号，避免误伤正常的英文回话
    （例如 ``I'll crop it for you.`` 只有句式、没有内部细节，不算独白）。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 带表块的正文交给填表逻辑，不在这里判
    if FORM_BEGIN in t or "<<<COLLAB_JSON>>>" in t:
        return False
    if _ADDRESSED_TO_USER_RE.search(t):
        return False
    marks = {m.group(0).lower() for m in _REASONING_MARK_RE.finditer(t)}
    if not marks:
        return False
    internal = bool(_INTERNAL_TOKEN_RE.search(t))
    # 中文占比够高 → 大概是在跟用户说话（用户用中文交流）。但独白里也会夹
    # 大量中文素材名（"空格子匹配17点.png"），所以这时要更多佐证才判独白。
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    if cjk >= max(4, len(t) // 20):
        return len(marks) >= 2 and internal
    # 纯英文/英文为主：一个推理句式 + 提到内部实现细节就够
    return len(marks) >= 2 or internal


def parse_collab_form(text: str) -> tuple[str, dict[str, Any]]:
    """返回 (表外残渣正文, 规范化后的 form dict)。优先新表，兼容旧 COLLAB_JSON。"""
    raw = text or ""
    outside, form = _extract_marked(raw, _FORM_BLOCK, _FORM_LOOSE)
    # 表外裸正文只有在「像给用户看的话」时才能兜底成 reply：模型的推理独白
    # 整段漏到这里时直接丢掉，否则等于把内心独白当答复上屏。
    fallback = "" if looks_like_reasoning(outside) else str(outside or "").strip()
    if not form:
        outside2, form = _extract_marked(raw, _OLD_BLOCK, _OLD_LOOSE)
        if form:
            outside2 = "" if looks_like_reasoning(outside2) else str(outside2 or "").strip()
            outside = outside2
            fallback = outside2
            form = migrate_legacy_meta(form, outside)
    if not form and fallback.startswith("{") and '"turn"' in fallback:
        form = _loads_object(fallback)
        if form:
            fallback = str(form.get("reply") or form.get("message") or "").strip()
    form = normalize_form(form, fallback_reply=fallback)
    reply = str(form.get("reply") or "").strip()
    if not reply and fallback:
        form["reply"] = fallback
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
                entry: dict[str, Any] = {"step": step[:80], "done_when": done[:80]}
                # needs：这一步要用的工具 / 权限 / 用户操作，供能力契约核对
                needs = item.get("needs")
                if isinstance(needs, str):
                    needs = [needs]
                if isinstance(needs, list):
                    clean = [str(n).strip()[:60] for n in needs if str(n).strip()][:4]
                    if clean:
                        entry["needs"] = clean
                out.append(entry)
    return out


_DONE_KIND_KEYS = (
    ("has_inspect", ("素材检查", "视觉复核", "裁剪已检查", "内容已核对")),
    ("has_mark", ("标注图", "图上标记", "已标出", "圈出", "画好标记", "标注已画")),
    ("has_crop", ("裁剪", "新素材", "裁好", "切掉", "只留")),
    ("has_match", ("匹配结果", "匹配命中", "命中点", "叠加框", "试匹配")),
    ("window_aligned", ("点击窗", "hwnd", "改绑", "窗口一致", "渲染窗")),
    ("has_do_work", ("do_work", "成品脚本", "可运行脚本", "脚本已写")),
    ("has_logic", ("逻辑步骤", "已有逻辑", "逻辑已写")),
    ("has_frame", ("已有画面", "已有截图", "已有底图")),
    ("user", ("等用户", "用户确认", "用户授权")),
)


def task_done_evidence_gap(meta: Optional[dict], session) -> str:
    """task_done 必须有交付物：脚本或逻辑步骤。空口说完成不算。"""
    meta = meta or {}
    if not bool(meta.get("task_done")):
        return ""
    script = str(
        meta.get("script") or getattr(session, "generated_code", "") or ""
    ).strip()
    steps = list(getattr(getattr(session, "logic", None), "steps", None) or [])
    if script or steps:
        return ""
    # 纯聊天轮（没有任何工作痕迹）不强制交付
    if not (
        list(getattr(session, "assets", None) or [])
        or list(getattr(session, "overlays", None) or [])
        or getattr(session, "frame_path", None)
    ):
        return ""
    # 分型：写过 logic_draft 但没落地成逻辑步骤 ≠ "什么都没做"。
    # 只报"没有交付物"会让模型去重写别的，打偏；要说清是草稿没落地、为什么没落地。
    art = getattr(session, "artifacts", None)
    art = art if isinstance(art, dict) else {}
    draft = art.get("logic_draft")
    if isinstance(draft, list) and draft:
        dropped = art.get("logic_draft_dropped") if isinstance(art.get("logic_draft_dropped"), list) else []
        why = (
            f"其中 {len(dropped)} 条的素材 id 在素材区里不存在，已被忽略："
            + "、".join(str(x) for x in dropped[:4])
            + "。"
            if dropped
            else ""
        )
        return (
            "你标了 task_done，但 logic_draft 没落地成逻辑步骤（逻辑步骤表是空的）。"
            + why
            + "把每条草稿补成 {\"label\": \"…\", \"asset\": \"[[现有素材id]]\"}；"
            "素材 id 必须是素材区里现存的，也可以不绑素材。"
        )
    return (
        "你标了 task_done，但既没有可运行脚本、也没有逻辑步骤——没有交付物不算完成。"
        "要么现在交付，要么说清还差什么、被什么挡住。"
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
    # 用户说法不是 ground truth，不能作为全局完成闸。完成条件只认本轮产物/实测。
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
    if kind == "has_crop":
        # 裁剪类完成条件必须真有产物：裁剪记录，或素材区里新增的「裁剪/探针」组素材
        art = getattr(session, "artifacts", None) or {}
        last_crop = art.get("last_crop") if isinstance(art, dict) else None
        fresh = _new_since_baseline(session, "has_crop")
        baseline = art.get("plan_step_baseline") if isinstance(art, dict) else None
        if baseline and not fresh:
            return False, (
                "还没有*本步*的裁剪产物（素材区里旧的那些不算）。要裁剪就调用 crop_asset"
                "（素材 id + 归一化矩形），它会自动把结果加进素材区；不要只在文字里说裁剪。"
            )
        if isinstance(last_crop, dict) and last_crop.get("path"):
            return True, f"已有裁剪素材：{last_crop.get('name') or last_crop.get('path')}"
        groups = {
            str((a or {}).get("group") or "")
            for a in (getattr(session, "assets", None) or [])
            if isinstance(a, dict)
        }
        if {"裁剪", "探针"} & groups:
            return True, "素材区里已有裁剪/探针产出的素材。"
        return False, (
            "还没有裁剪产物。要裁剪就调用 crop_asset（素材 id + 归一化矩形），"
            "它会自动把结果加进素材区；不要只在文字里说裁剪。"
        )
    if kind == "has_inspect":
        art = getattr(session, "artifacts", None) or {}
        inspected = art.get("last_asset_inspect") if isinstance(art, dict) else None
        if (
            isinstance(inspected, dict)
            and inspected.get("text")
            and inspected.get("structured")
            and inspected.get("verified") is True
        ):
            return True, f"已有素材检查：{inspected.get('asset_id') or inspected.get('path')}"
        if isinstance(inspected, dict) and inspected.get("structured") and inspected.get("verified") is False:
            return False, "素材复核明确未通过；必须重新定位和裁剪。"
        if isinstance(inspected, dict) and inspected.get("text"):
            return False, "识图返回未通过结构化校验，不能作为定位证据；请重新调用 inspect_asset。"
        return False, "还没有素材视觉复核。请调用 inspect_asset 检查指定素材。"
    if kind == "has_match":
        overlays = [
            o
            for o in (getattr(session, "overlays", None) or [])
            if isinstance(o, dict) and str(o.get("kind") or "") == "match"
        ]
        art = getattr(session, "artifacts", None) or {}
        baseline = art.get("plan_step_baseline") if isinstance(art, dict) else None
        fresh = _new_since_baseline(session, "has_match")
        if baseline and not fresh:
            return False, (
                "还没有*本步*的匹配结果（旧的那些不算）。请调用 match_template"
                "（用 asset_id 指名模板）。"
            )
        if overlays:
            return True, f"已有 {len(overlays)} 个匹配结果。"
        return False, "还没有匹配结果。请调用 match_template（用 asset_id 指名模板）。"
    if kind == "has_mark":
        # 标注产物在素材区（role=annotation / group=标注），或本会话刚画过
        art = getattr(session, "artifacts", None) or {}
        baseline = art.get("plan_step_baseline") if isinstance(art, dict) else None
        fresh = _new_since_baseline(session, "has_mark")
        marks = [
            a
            for a in (getattr(session, "assets", None) or [])
            if isinstance(a, dict)
            and (
                str(a.get("role") or "") == "annotation"
                or str(a.get("group") or "") == "标注"
            )
        ]
        # 换步之后，older 的标注图不能顶掉本步的完成条件：否则"再画一版"会被
        # 判定成"已经成立"，系统反复要求模型改 done_when，模型却无事可做。
        if baseline and not fresh:
            return False, (
                f"素材区里那 {len(marks)} 张标注图是**本步之前**的，不算这一步的产物。"
                "要重画就真的调用 mark_on_image / write_files 的探针把新图产出来；"
                "只写在 reply 里不算。"
            )
        if marks:
            return True, f"已有标注图：{marks[-1].get('name') or marks[-1].get('id')}"
        return False, (
            "还没有标注产物。要标记就调用 mark_on_image（marks 给 rect 或 x/y，"
            "原图像素坐标）；不要只在文字里说标好了。"
        )
    if kind == "unknown":
        return False, (
            "done_when 请改成系统能核对的说法：点击窗一致 / 已有 do_work / 已有逻辑步骤 / "
            "已有画面 / 已有裁剪素材 / 已有素材检查 / 已有匹配结果 / 已有标注图 / 等用户确认。"
        )
    return False, "请写 done_when。"


_PERM_LABELS_FOR_MODEL = {
    "probe_script": "临时脚本",
    "write_files": "写文件",
    "runtime_control": "控制窗口",
}


def _plan_items(meta: Optional[dict]) -> list[dict[str, Any]]:
    plan = (meta or {}).get("plan")
    if not isinstance(plan, list):
        return []
    out: list[dict[str, Any]] = []
    for it in plan:
        if isinstance(it, dict):
            out.append(it)
        elif isinstance(it, str) and it.strip():
            out.append({"step": it.strip()})
    return out


def plan_contract_gaps(
    meta: Optional[dict],
    *,
    tool_names: Optional[list[str]] = None,
    permissions: Optional[dict] = None,
) -> list[str]:
    """检查 plan 里的承诺有没有对应的手：工具存不存在、权限有没有给。

    plan 每项可写 `needs`: ["tool:crop_asset", "permission:write_files", "user:确认"]。
    返回给人话的缺口；空列表表示承诺都兑现得了。
    """
    tools = {str(t) for t in (tool_names or [])}
    perms = dict(permissions or {})
    gaps: list[str] = []
    for i, item in enumerate(_plan_items(meta), 1):
        needs = item.get("needs")
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list):
            continue
        label = str(item.get("step") or "").strip() or f"第{i}步"
        for raw in needs:
            need = str(raw or "").strip()
            if not need:
                continue
            low = need.lower()
            if low.startswith("tool:"):
                name = need.split(":", 1)[1].strip()
                if tools and name not in tools:
                    gaps.append(
                        f"第{i}步「{label}」声称要做的动作需要工具 {name}，"
                        f"但系统当前没有这个工具"
                    )
            elif low.startswith("permission:"):
                key = need.split(":", 1)[1].strip()
                if key not in _PERM_LABELS_FOR_MODEL:
                    gaps.append(f"第{i}步「{label}」写了无法识别的权限 {key}")
                elif not perms.get(key):
                    gaps.append(
                        f"第{i}步「{label}」需要「{_PERM_LABELS_FOR_MODEL[key]}」权限，"
                        f"但当前没授权"
                    )
            elif low.startswith("user:"):
                continue
            else:
                gaps.append(
                    f"第{i}步「{label}」的 needs 写法不认识：{need}"
                    "（用 tool:名字 / permission:键 / user:说明）"
                )
    return gaps


def contract_gap_note(gaps: list[str]) -> str:
    """把缺口整理成给模型看的硬约束。"""
    if not gaps:
        return ""
    lines = ["【能力契约：这些承诺现在兑现不了】"]
    lines.extend(f"- {g}" for g in gaps[:5])
    lines.append(
        "处理方式：需要授权的，改成 turn=ask 并给对应授权芯片"
        "（write_files → 打开写文件权限，runtime_control → 打开控制窗口权限）；"
        "系统没有的工具，别急着推给用户——先用 run_probe_script 把这件事做出来，"
        "再用 define_tool 登记成工具，然后才写 tool:那个名字；"
        "确实要用户动手的才写成 user:。"
        "禁止用别的动作假装推进这一步。"
    )
    return "\n".join(lines)


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
    # 空口完成优先拦：标了 task_done 但没有交付物，就不接受这一轮的结束
    # （不看 turn —— 因为它通常同时写了 turn=stop）
    evidence_gap = task_done_evidence_gap(meta, session)
    if evidence_gap:
        return True, evidence_gap
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
    # 兼容旧会话的 stall_note；新版自动续跑预算不会再写它或接管用户流程。
    stalled = bool(str(meta.get("stall_note") or "").strip())
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
    if unfinished and not stalled:
        chip = "继续这一步"
        if chip not in chips and "继续" not in chips:
            chips = ([chip] + chips)[:3]
        return {
            "mode": "continue",
            "activity": "还没做完" + (f"：{step}" if step else ""),
            "chips": chips,
            "cta_enabled": False,
            "cta_text": "",
            "continue_step": step,
        }
    if stalled:
        return {
            "mode": "idle",
            "activity": "",
            "chips": chips,
            "cta_enabled": False,
            "cta_text": "",
            "continue_step": "",
        }
    # 内部自动续跑因预算暂停：turn 已收成 stop（不打断用户），但计划还挂着。
    # 不留入口的话用户只能自己敲"继续"（实况敲了 8 次）——补一个一键续跑芯片。
    auto_pause = (getattr(session, "artifacts", None) or {}).get("last_auto_pause") if session is not None else None
    if isinstance(auto_pause, dict):
        paused_plan = auto_pause.get("plan") if isinstance(auto_pause.get("plan"), list) else []
        if paused_plan and not meta.get("task_done"):
            if _RESUME_CHIP not in chips:
                chips = ([_RESUME_CHIP] + chips)[:3]
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
    prev_plan = session.artifacts.get("agent_plan") or []
    session.artifacts["agent_plan"] = plan
    session.artifacts["agent_done_when"] = str(meta.get("done_when") or "")
    _refresh_plan_step_baseline(session, prev_plan, plan)
    # 没有交付物的 task_done 不算数：不然会话被标成"已完成"，用户再给素材也不会被接着做
    task_done_ok = bool(meta.get("task_done")) and not task_done_evidence_gap(meta, session)
    session.artifacts["agent_task_done"] = task_done_ok
    if task_done_ok:
        session.artifacts.pop("agent_plan_gap", None)
    think = str(meta.get("think") or "").strip()
    if think:
        session.artifacts["agent_think"] = think[:400]


def _plan_first_step(plan) -> str:
    if isinstance(plan, list) and plan:
        item = plan[0]
        return str(item.get("step") if isinstance(item, dict) else item or "").strip()
    return ""


def _product_fingerprint(session) -> dict[str, Any]:
    """本步开始时的产物快照（用于判断"这是新产物还是以前的旧账"）。"""
    assets = [a for a in (getattr(session, "assets", None) or []) if isinstance(a, dict)]

    def _ids(*roles: str) -> list[str]:
        return sorted(
            str(a.get("id") or "")
            for a in assets
            if str(a.get("role") or "") in roles or str(a.get("group") or "") in roles
        )

    overlays = [o for o in (getattr(session, "overlays", None) or []) if isinstance(o, dict)]
    art = getattr(session, "artifacts", None)
    art = art if isinstance(art, dict) else {}
    crop = art.get("last_crop")
    return {
        "annotation_ids": _ids("annotation", "标注"),
        "crop_ids": _ids("裁剪"),
        "match_overlay_ids": sorted(
            str(o.get("id") or "") for o in overlays if str(o.get("kind") or "") == "match"
        ),
        "crop_path": str(crop.get("path") or "") if isinstance(crop, dict) else "",
    }


def _refresh_plan_step_baseline(session, prev_plan, plan) -> None:
    """换了当前步（plan[0] 文案变了）就重取基线。

    起因：模型写「用 combined_mark 再画一版勋章红框+空格蓝框」，done_when 写
    「已有标注图」，而素材区里还躺着**一小时前**那张标注图 → 完成条件当场成立，
    系统每轮都回「done_when 已经成立，请改 done_when」，模型没东西可改，于是
    每轮只回一句「我这就出图」，8 轮烧完照样什么都没产出。
    """
    if not isinstance(getattr(session, "artifacts", None), dict):
        return
    cur_step = _plan_first_step(plan)
    prev_step = _plan_first_step(prev_plan)
    stored = session.artifacts.get("plan_step_baseline")
    if cur_step and (cur_step != prev_step or not isinstance(stored, dict)):
        session.artifacts["plan_step_baseline"] = {
            "step": cur_step,
            "at": _now_iso(),
            **_product_fingerprint(session),
        }
    elif not cur_step:
        session.artifacts.pop("plan_step_baseline", None)


def _new_since_baseline(session, kind: str) -> str:
    """返回"本步开始后新增的产物"说明；没有则空串。"""
    art = getattr(session, "artifacts", None)
    if not isinstance(art, dict):
        return ""
    baseline = art.get("plan_step_baseline")
    if not isinstance(baseline, dict):
        return ""
    cur = _product_fingerprint(session)
    if kind == "has_mark":
        old = set(baseline.get("annotation_ids") or [])
        new = [i for i in cur["annotation_ids"] if i not in old]
        return f"（本步新画的标注图 {len(new)} 张）" if new else ""
    if kind == "has_crop":
        old = set(baseline.get("crop_ids") or [])
        new = [i for i in cur["crop_ids"] if i not in old]
        if new:
            return f"（本步新增裁剪素材 {len(new)} 张）"
        if cur["crop_path"] and cur["crop_path"] != str(baseline.get("crop_path") or ""):
            return "（本步有新的裁剪记录）"
        return ""
    if kind == "has_match":
        old = set(baseline.get("match_overlay_ids") or [])
        new = [i for i in cur["match_overlay_ids"] if i not in old]
        return f"（本步新增匹配框 {len(new)} 个）" if new else ""
    return ""


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

    script = src.get("script")
    if script is not None:
        script = str(script).strip() or None
    script_name = src.get("script_name")
    if script_name is not None:
        script_name = str(script_name).strip() or None
    intro = src.get("intro")
    if intro is not None:
        intro = str(intro).strip() or None

    out["turn"] = turn
    out["action"] = action or None
    out["ask"] = ask or None
    out["chips"] = chips
    out["logic_draft"] = logic_draft
    out["script"] = script
    out["script_name"] = script_name
    out["intro"] = intro
    answer = src.get("answer")
    out["answer"] = (str(answer).strip()[:600] or None) if answer is not None else None
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
        "script": f.get("script"),
        "script_name": f.get("script_name"),
        "intro": f.get("intro"),
        "answer": f.get("answer"),
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


_ASSET_REF_IN_TEXT_RE = re.compile(r"\[\[\s*([^\[\]\s]+?)\s*\]\]")


def prune_dangling_draft_refs(
    draft: list, known_ids: set[str] | None = None
) -> tuple[list, list[str]]:
    """把 logic_draft 里指向"素材区已不存在"的 id 剔掉。

    返回 (清理后的草稿, 被剔除的 id 列表)。
    实况会话里 ``[[asset_c2af2860]]`` 已被移出素材区却仍留在草稿里，
    一路带进逻辑步骤 / 生成脚本就是"素材解析不出来"。
    """
    if not isinstance(draft, list):
        return [], []
    if known_ids is None:
        return list(draft), []

    def _usable(m: "re.Match[str]") -> str:
        return m.group(0) if m.group(1) in known_ids else ""

    def _clean_text(text: str) -> str:
        return re.sub(r"\s{2,}", " ", _ASSET_REF_IN_TEXT_RE.sub(_usable, text)).strip()

    all_dropped: list[str] = []
    out: list = []
    for item in draft:
        item_dropped: list[str] = []
        if isinstance(item, str):
            item_dropped = [
                m.group(1)
                for m in _ASSET_REF_IN_TEXT_RE.finditer(item)
                if m.group(1) not in known_ids
            ]
            out.append(_clean_text(item) if item_dropped else item)
            all_dropped.extend(item_dropped)
            continue
        if not isinstance(item, dict):
            out.append(item)
            continue
        entry = dict(item)
        for key in ("asset", "asset_id"):
            raw = str(entry.get(key) or "").strip()
            if not raw:
                continue
            ref = _ASSET_REF_IN_TEXT_RE.search(raw)
            aid = ref.group(1).strip() if ref else raw
            if aid and aid not in known_ids:
                item_dropped.append(aid)
                entry.pop(key, None)
        label = str(entry.get("label") or entry.get("text") or "")
        for m in _ASSET_REF_IN_TEXT_RE.finditer(label):
            if m.group(1) not in known_ids:
                item_dropped.append(m.group(1))
        if item_dropped:
            entry["label"] = _clean_text(label)
        out.append(entry)
        all_dropped.extend(item_dropped)
    seen: set[str] = set()
    uniq = [d for d in all_dropped if not (d in seen or seen.add(d))]
    return out, uniq


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
#: 素材引用的公开别名（Phase B：统一 ResourceBinder 的脚本侧入口也要用同一份正则，
#: 不允许第二套 token 语法）。
ASSET_REF_RE = _ASSET_REF_RE
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


#: 同上：路径字面量的公开别名（材料化侧统一走 ResourceBinder 时共用）。
py_literal = _py_literal


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
    """把脚本里的 [[asset_id]] 换成真实路径。返回 (代码, 已换 id, 未解析 id)。

    Phase B（设计 §4.2）：解析**统一走** :class:`ResourceBinder`（与工具调用侧同一个
    实现、同一套诊断码）；失败的引用原样留在文本里，由 artifact gate 按码阻断，
    **不猜替代品、也不删掉**。诊断明细见 :func:`substitute_asset_refs_detailed`。
    """
    text, done, failures = substitute_asset_refs_detailed(code, session)
    return text, done, [failure.ref for failure in failures]


def substitute_asset_refs_detailed(code: str, session):
    """同上，但返回结构化失败清单（``BindingFailure``），供产物闸门报码。"""
    from backend.script_generator.resource_binding import (
        ResourceBinder,
        SessionResourceSource,
        resolve_script_tokens,
    )

    if not str(code or "") or "[[" not in str(code or ""):
        return code or "", [], []
    binder = ResourceBinder(SessionResourceSource(session), origin="script_token")
    text, bound, failures = resolve_script_tokens(code, binder)
    return text, [item.resource_id for item in bound], failures


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


def _revision_evidence(logs: Optional[list[str]], *, limit: int = 18) -> str:
    """从试运行日志挑出有用的证据行（错误/状态/游标），不整段粘贴。"""
    lines: list[str] = []
    for raw in list(logs or []):
        s = str(raw or "").strip()
        if not s:
            continue
        low = s.lower()
        keep = (
            "error" in low
            or "traceback" in low
            or "异常" in s
            or "失败" in s
            or "警告" in s
            or "[warn" in low
            or s.startswith("[状态]")
            or s.startswith("[MINASHIGO_STEP]")
        )
        if keep:
            lines.append(s[:200])
    if not lines:
        return ""
    return "\n".join(lines[-max(1, limit):])


def build_step_revision_request(
    step: Any,
    session: Any,
    *,
    logs: Optional[list[str]] = None,
    user_note: str = "",
) -> tuple[str, bool]:
    """点某一步「这一步不对」时，替用户写成一条现象级修订请求。

    返回 ``(text, ready)``：
    - ``ready=True``：手上有证据（试跑日志/脚本），可以直接发进对话让 Agent 改；
    - ``ready=False``：没有证据，text 是让用户补一句现象的引导。

    原则（design_collaborative_v2 §4.2）：用户不用写技术反馈，
    点一下就把「哪一步 + 什么现象 + 最近日志」带进修订上下文。
    """
    if step is None:
        return "哪一步不对？点右侧步骤卡上的「这一步不对」，我按那一步改。", False

    sid = str(getattr(step, "id", "") or "")
    label = str(getattr(step, "label", "") or "")
    hint = str(getattr(step, "hint", "") or "")
    when = str(getattr(step, "when", "") or "")
    try:
        idx = list(getattr(getattr(session, "logic", None), "steps", None) or []).index(step) + 1
    except Exception:
        idx = 0
    if not idx:
        steps = list(getattr(getattr(session, "logic", None), "steps", None) or [])
        idx = next(
            (i for i, s in enumerate(steps, 1) if getattr(s, "id", "") == sid),
            0,
        )

    where = f"第 {idx} 步" if idx else "这一步"
    title = f"{where}「{label}」" if label else where
    has_script = bool(str(getattr(session, "generated_code", "") or "").strip())
    evidence = _revision_evidence(logs)
    note = str(user_note or "").strip()

    if not has_script and not evidence and not note:
        return (
            f"{title}不对。\n"
            "说一句现象就够了（例如「点歪了 / 没反应 / 认错界面」），"
            "或跑一次试运行让我拿到日志再改。",
            False,
        )

    parts = [
        f"不对：{title}。",
        "保持其它步骤不变，只修这一步。",
    ]
    if hint:
        parts.append(f"（原说明：{hint[:120]}）")
    if when:
        parts.append(f"（原条件：{when[:80]}）")
    if note:
        parts.append(f"现象：{note[:200]}")
    if evidence:
        parts.append("最近一次试运行日志（节选）：\n" + evidence)
    return "\n".join(parts), True


#: 契约校验用的 runtime profile；None = 由脚本注解自动判定
#: （``validator.profile_for_code``：明确注解优先，否则默认 desktop，设计 §4.2）。
_CONTRACT_PROFILE: Optional[str] = None


def collab_contract_issues(
    code: str,
    session=None,
    *,
    profile: Optional[str] = None,
    allow_login: bool = False,
) -> list:
    """协作脚本的**结构化**契约校验（设计 §7/§8）。

    - 对**模型原稿**跑一次（这样才能抓到裸 ``[[asset]]`` 这类"归一后就消失"的写法）；
    - 有 session 时再对**机械归一后的产物**跑一次（残留 token / 参数问题）。
    - profile 判定走 ``validator.profile_for_code``：明确注解优先，否则默认 desktop。
    """
    from backend.script_generator.api_contracts import validator

    raw = (code or "").strip()
    if not raw:
        return []
    target_profile = validator.profile_for_code(raw, explicit=profile)
    if session is None:
        # 没有会话就无法机械归一，只能按原稿判（含入口检查）。
        issues = list(
            validator.validate_source(raw, profile=target_profile, allow_login=allow_login)
        )
    else:
        # 有会话：机械归一是官方修复通道（补注解/import/IMG_DIR），所以
        # 除"裸 token 写法"以外一律以**归一后的产物**为准，避免把可修复的稿子判死、
        # 也避免同一条问题在归一前后各报一次。
        try:
            normalized, _notes = normalize_collab_script(raw, session)
        except Exception:
            normalized = raw
        issues = list(validator.asset_token_source_issues(raw))
        issues.extend(
            validator.validate_artifact(
                normalized or raw, profile=target_profile, allow_login=allow_login
            )
        )
    seen = set()
    out = []
    for item in issues:
        key = (item.code, item.line, item.symbol, item.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def collab_script_issues(
    code: str, session=None, *, profile: Optional[str] = None
) -> list[str]:
    """协作脚本的交付闸：结构 + 窗口接口，并与试运行同一道闸（require_fsm=False）。

    ``profile`` 是 **Runtime 事实**，调用方应尽量显式传入（决定 §13.1）。
    未传时由 ``validator.profile_for_code`` 从入口注解取 **provisional** 值：
    注解唯一 → 该 profile；Union / 无注解 / 无法解析 → ``unresolved``，
    再经 ``registry.effective_profile`` 落到 ``portable_common`` 安全交集
    —— **不再**静默落 desktop。
    """
    text = (code or "").strip()
    if not text:
        return []
    from backend.script_generator.api_contracts import validator

    # 契约校验（结构化；事实源 = api_contracts registry）先跑，用**原稿**。
    explicit = profile if profile is not None else _CONTRACT_PROFILE
    contract_issues = collab_contract_issues(code or "", session, profile=explicit)
    contract_strings = validator.legacy_strings(contract_issues)
    if session is not None:
        text, _notes = normalize_collab_script(text, session)
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"语法错误: {e.msg}（第 {e.lineno} 行）"]
    issues: list[str] = list(contract_strings)
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
        if not any("do_work" in item for item in issues):
            issues.append("缺少 async def do_work")
        return issues
    browser = dw.args.args[0].arg if dw.args.args else ""
    if not browser:
        return issues + ["do_work 缺少窗口参数"]
    # ⚠️ 这里曾有第二套 API 裁决：`_window_api_names()`（dir(UserWindow) 名称表）与
    # `_browser_call_signature_issues()`（UserWindow 专属签名检查）。两者都是
    # **可漂移的第二份事实源**，且永远以 UserWindow 为中心 → 会误拒合法浏览器脚本
    # （实况：合法 browser 脚本在交付闸被报"不存在的接口 browser.match_image_multi"，
    # 而同一脚本过 `collab_contract_issues(profile=browser)` 是 0 条）。
    # 按用户决定（2026-09-28）**删除**，方法/参数/await/返回值一律由
    # `api_contracts.validator` 按 profile 判定（见上面的 contract_issues）。
    # 与试运行页同一道闸，避免「协作说好了、点试跑说没过」
    try:
        from backend.script_generator.agent import validate_script_local

        for err in validate_script_local(
            text,
            explanation="",
            source_dir="",
            free_mode=True,
            require_fsm=False,
            profile=explicit if explicit is not None else None,
        ):
            if err not in issues:
                issues.append(err)
    except Exception:
        pass
    return issues


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
    capability_block: str = "",
    permissions: Optional[dict] = None,
    last_user: str = "",
) -> str:
    """每轮发给 LLM 的状态摘要（不是聊天流水账）。"""
    from pathlib import Path

    from backend.script_generator.collaborator.probe_script import normalize_permissions
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

    session_assets = list(getattr(session, "assets", None) or [])
    try:
        from backend.script_generator.collaborator.resource_registry import active_asset_ids

        active_ids = active_asset_ids(session)
        session_assets = [a for a in session_assets if str(a.get("id") or "") in active_ids]
    except Exception:
        pass
    n_assets = len(session_assets)
    n_overlays = len(list(getattr(session, "overlays", None) or []))
    viewport_identity = "无"
    if has_frame:
        viewport_path = Path(str(getattr(session, "frame_path", "") or ""))
        viewport_identity = viewport_path.name
        for asset in session_assets:
            try:
                same_path = Path(str(asset.get("path") or "")).resolve() == viewport_path.resolve()
            except Exception:
                same_path = str(asset.get("path") or "") == str(viewport_path)
            if same_path:
                viewport_identity = (
                    f"[[{asset.get('id') or ''}]]「{asset.get('name') or viewport_path.name}」"
                )
                break
    draft_pseudo = ""
    if isinstance(getattr(session, "artifacts", None), dict):
        draft_pseudo = str(session.artifacts.get("draft_pseudo") or "").strip()
    inspected_ids: set[str] = set()
    art_for_inspection = getattr(session, "artifacts", None) or {}
    if isinstance(art_for_inspection, dict):
        for item in list(art_for_inspection.get("asset_inspection_log") or []):
            if not isinstance(item, dict):
                continue
            if item.get("asset_id"):
                inspected_ids.add(str(item["asset_id"]))
            inspected_ids.update(str(x) for x in (item.get("asset_ids") or []) if x)
    # 只数"还在素材区"的那些：日志里留着已删除素材的 id 时，
    # 会写出「已实际检查 10/17 张」这种分子比真实覆盖还大的假账（实况出现过 23/8）。
    live_ids = {
        str(a.get("id"))
        for a in (getattr(session, "assets", None) or [])
        if isinstance(a, dict) and a.get("id")
    }
    covered = len(inspected_ids & live_ids) if live_ids else len(inspected_ids)
    stale = len(inspected_ids - live_ids) if live_ids else 0

    lines = [
        "【状态摘要】",
        f"- phase: {phase_s}",
        f"- goal: {goal or '无'}",
        f"- 已有底图: {'是' if has_frame else '否'}",
        f"- 共同视口: 本地可用（素材 {n_assets}，标记 {n_overlays}）；"
        "需要时主动调用查看/素材工具，不随回合推送内容",
        f"- 共同视口当前底图: {viewport_identity}",
        f"- 素材视觉覆盖: 已实际检查 {covered}/{n_assets} 张"
        + (f"（另有 {stale} 个 id 已不在素材区，不计入）" if stale else "")
        + "；名称/缩略图目录不算看过，跨图结论必须用 compare_assets",
        f"- 逻辑步数: {n_steps}" + (f"（{title}）" if title else ""),
        f"- 权限: write_files={perms.get('write_files')} · "
        f"runtime_control={perms.get('runtime_control')} · "
        f"probe_script={perms.get('probe_script')}",
        f"- 已有成品脚本: "
        f"{'是' if (getattr(session, 'generated_code', None) or '').strip() else '否'}",
    ]
    # 素材区清单直接进摘要：模型看不到界面，不给它清单就只能反问用户
    # 「你说的是哪张」。超过 40 张时给前 40 条并提示用 list_session_assets。
    try:
        from backend.script_generator.collaborator.canvas import (
            assets_summary_for_prompt,
        )

        if session_assets:
            lines.append("- 素材区（用 [[id]] 或 user 名点名）：")
            lines.append(
                "  " + assets_summary_for_prompt(session_assets).replace("\n", "\n  ")
            )
    except Exception:
        pass
    try:
        from backend.script_generator.collaborator.resource_registry import workspace_block

        lines.append("")
        lines.append(workspace_block(session))
    except Exception:
        pass
    if last_user:
        lines.append(f"- 用户本轮：「{last_user[:200]}」")
        if "素材区" in last_user or "素材里的" in last_user or "素材中" in last_user:
            lines.append(
                "- 本轮指代约束：用户说的是素材区，不是当前共同视口；"
                "先 list/inspect/compare 素材，或明确调用 show_asset_in_viewport 后再看共同视口"
            )
    # 当前图片的用户说法与实测：作用域隔离；用户说法不具有权威性
    try:
        from backend.script_generator.collaborator.tool_sanity import (
            ground_truth_block,
            measurements_block,
        )

        gt_block = ground_truth_block(session)
        measured_block = measurements_block(session)
    except Exception:
        gt_block = measured_block = ""
    if (vision_prefetch or "").strip():
        lines.append("")
        lines.append("【本轮画面/匹配结果——当作你已看见】")
        lines.append(vision_prefetch.strip())
    if (runtime_block or "").strip():
        lines.append("")
        lines.append(runtime_block.strip())
    if (capability_block or "").strip():
        lines.append("")
        lines.append(capability_block.strip())
    if gt_block:
        lines.append("")
        lines.append(gt_block)
    if measured_block:
        lines.append("")
        lines.append(measured_block)
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
            lines.append("【计划核对（上一轮的记录，可能已过时）】")
            lines.append(gap[:800])
            lines.append(
                "- 若本轮工具结果与上面这条冲突（例如它说素材文件不存在、"
                "而本轮 match_template 已命中），以本轮结果为准，不要再重复旧的结论。"
            )
        if art.get("agent_task_done"):
            lines.append("- 你上次标了 task_done，以本轮核对为准")
    contract_gap = str(art.get("contract_gap") or "").strip() if isinstance(art, dict) else ""
    if contract_gap:
        lines.append("")
        lines.append(contract_gap)
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
