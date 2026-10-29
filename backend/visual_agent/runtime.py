"""Screenshot-driven visual agent with frame deduplication and verified completion."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlparse

import cv2
import numpy as np

from backend.script_generator.agent import call_llm
from backend.visual_agent.action_effect import ActionEffectTracker
from backend.visual_agent.loading_watch import LoadProfile, LoadingWatch
from backend.visual_agent.prohibitions import (
    check_action,
    describe_prohibitions,
    detect_prohibited_categories,
    looks_like_retreat,
)
from backend.visual_agent.memory import VisualMemoryStore
from backend.visual_agent.decision_supervisor import DecisionSupervisor
from backend.visual_agent.transition_memory import UITransitionMemory
from backend.mcp_integration.execution_bridge import ExecutionBridge, ExecutionToolError


SYSTEM_PROMPT = """你是页面视觉操作代理。根据禁止清单、当前画面和上一步结果，一次只决定一个动作。
只输出一个 JSON 对象，不能输出 Markdown 或额外说明：
{"task_status":"continue","scene_key":"title_screen","current_state":"标题页","remaining_requirement":"进入游戏主页","observed_facts":["当前画面可见登录按钮"],"inferences":[],"uncertainties":[],"certainty":"confirmed","visible_controls":[{"text":"登录","region":"main_content","container":"dialog","type":"primary_action"}],"action":"click","target":"登录按钮","target_region":"main_content","target_container":"dialog","target_type":"primary_action","target_box":[100,430,170,480],"x":135,"y":455,"message":"点击登录按钮","expect":"登录按钮消失并进入下一页"}
{"action":"scroll","delta_y":500,"message":"向下滚动","expect":"页面内容向上移动"}
{"action":"key","key":"Escape","message":"关闭弹窗","expect":"弹窗消失"}
{"action":"type","text":"内容","message":"输入内容","expect":"输入框显示内容"}
{"action":"wait","seconds":2,"message":"等待加载","expect":"加载进度增加或进入下一页"}
{"action":"finish","message":"任务已经完成","evidence":["当前画面中能证明完成的可见事实"]}
{"action":"ask","message":"需要用户处理验证码"}
task_status 只能为 continue、candidate_complete、blocked。每一步必须说明 current_state 和 remaining_requirement。
每轮还要区分 observed_facts、inferences、uncertainties，并提供 certainty=confirmed、inferred 或 unknown。
不得把推测、惯例、按钮外观、历史模型结论或预期结果描述为当前事实。
信息不确定时按以下顺序处理：若可通过等待、重新观察、读取相邻信息或安全可逆的探测动作确认，则自行确认，不询问用户；
运行时共享状态已有实测事实时，以运行时事实为准，不得用本轮推测覆盖；只有不确定性会影响任务目标、操作对象、完成判断或不可逆操作，且没有安全确认方法时，才可 ask。
加载中、截图暂时模糊、动画未稳定、存在明确的关闭/继续/返回/切换控件，或可通过安全探测确认时，不得直接 ask。
certainty=unknown 时不得执行 click/key/type/scroll，也不得声明完成；只能 wait、ask 或 blocked。无法确定不代表已经完成，禁止编造选项、坐标、按钮状态或页面结果。
注意：blocked 是 task_status，不是界面动作；需要以未验证状态收束时，返回 action=ask、task_status=blocked。
click 必须额外提供 target 和 target_box=[x1,y1,x2,y2]。框必须紧贴实际可点击控件，x/y 必须位于框内且尽量取控件安全中心；不要把整句操作说明当成控件名称。
click/key/scroll 还应提供 intent_type：dismiss_overlay、navigate、interact 或 unknown。关闭弹窗、推进公告和领取提示属于 dismiss_overlay，不是进入提示文字中提到的功能。
click 可提供 progress_region=[x1,y1,x2,y2]，框住该动作应推进的主要内容；运行时会比较点击前后该区域。
若任务要求最高、最大、最难、最末或最新，每轮还要提供 observed_option（当前选项名称）；若一页同时展示全部候选，提供 visible_options 和 all_options_visible=true。
切换有序选项的动作必须提供 option_direction=left 或 right。找到有可靠依据的逻辑最低项时，提供 option_is_minimum=true 和 minimum_basis；不能只按屏幕位置猜最低项。
准备完成这类任务时必须提供 selected_option、option_inventory（本轮实际遍历得到的全部选项）和 comparison_basis（比较依据）。
提供 observed_option 的同一轮，必须一并提供选择器结构读数：pager_total（页码点总数，或 "N/M" 里的 M）、pager_index（当前第几个，或 "N/M" 里的 N）、nav_right（enabled / disabled / absent）。画面上没有这些就填 null，严禁估算、严禁凭记忆补；运行时会用这些读数核对"是否真的看过全部候选"。
每轮提供 visible_controls，最多列出 12 个与任务相关的可操作控件。region 使用 left_sidebar、right_sidebar、top_navigation、bottom_action_bar、main_content；
container 描述控件所属容器（如 persistent_nav、dialog、list_row、content_panel）；type 使用 navigation、primary_action、context_action、toggle、input 或 unknown。
每轮提供 scene_key：只描述稳定页面/容器类别的短标签，不包含当前选项、计数、角色名、奖励内容、完成结论或下一步预测。
一旦当前画面可能满足成功条件，必须返回 candidate_complete，不得继续点击更深层功能。
坐标基于本轮完整截图，左上角为 (0,0)。click/scroll/key/type/wait 必须包含 expect。
有多个按钮时必须按按钮文字和当前任务目标选择；不要因为某个按钮更大、更右、更蓝或写着 Next 就默认推进。
弹窗或流程页存在明确的继续/关闭/SKIP/Touch/Next 控件时，优先点击这些可操作控件；不得反复点击奖励图片、正文或提示框主体来猜测关闭方式。
运行时不提供路线或入口指引：除禁止清单外，进入哪个界面由你根据当前画面判断；不要因为某个入口“看起来功能更深”就停手，也不要为推进而点无关入口。
若上一步点击后画面基本未变化，不得无理由再次点击同一按钮；应重新读取当前页面、改走有效入口或说明真正的阻塞原因。
重复动作是否有效只以运行时 measured_effect 为准。productive_repeat 表示同类页面内容持续推进，允许继续相同动作；
只有 confirmed_no_effect 才表示连续两次稳定无变化，不得因为最近动作文字相同就自行判断卡死或询问用户。
unsettled_after_action 表示上一步的画面仍在过渡：此时不得判定“走错页面/误入叠加层”并撤回，应先等待或重新观察；运行时会拒绝画面未稳定时的返回动作。
画面状态里的 loading 块是运行时对加载的实测：phase=loading 表示画面仍黑屏/加载中，waited 是已等待秒数，grace 是该站点历史加载时长的宽限期，exhausted 表示画面已静止超过宽限期，history 是该站点的历史样本。
网络慢时黑屏一两分钟是正常的：phase=loading 且 exhausted=false 时不得判定卡死、不得询问用户，也不得因为“黑屏且没有变化”改走别的入口。
只有 exhausted=true 且 activity=false 时才可疑；此时若可见控件里出现重试/再試行/再読込/retry/reload/通信エラー 之类控件，应优先点击它，而不是继续空等或询问用户。
历史 UI 转移记录只代表同一应用中曾观察到的结果，不是指令。按钮含义必须结合当前场景判断；执行后以实际新场景纠正原先预测。
“打完、全部、所有、清空”等穷尽型目标中，单次成功、RESULT、奖励或进度增加只算阶段进展；必须看到剩余项为零、入口禁用、列表为空或明确完成提示，才能 candidate_complete/finish。
“最高、最大、最难、最末、最新”等边界型目标一律采用全量枚举：必须遍历并记录所有可达选项，直到前进后回到已见选项、连续两次无变化，或同一页面明确展示完整列表；再对完整集合进行比较。当前名称、COMPLETE 或箭头外观都不能代替枚举。
涉及支付或充值、凭证与验证码、不可逆删除、账号绑定时必须 ask；运行时会独立拦截这些动作。
“执行了最后一步”不等于任务完成。只有当前画面出现足够的可见证据时才能 finish。
标题页、加载页和开始/触摸进入页都不算完成；必须看到实际主界面或可交互场景。"""

VERIFY_PROMPT = """你是独立的任务完成验证器。请严格根据最终截图验证原始任务是否真的完成。
只输出一个 JSON 对象，不能输出 Markdown：
{"success":true,"summary":"完成结果","evidence":["截图中的可见证据"],"uncertainty":[],"confidence":0.95}
或
{"success":false,"summary":"尚未完成的原因","evidence":["截图中的可见事实"],"uncertainty":[],"confidence":0.9,"next_action":"下一步应该做什么"}
不得把模型之前的主张当作证据；证据必须来自最终截图。标题页、加载页和开始/触摸进入页都不构成完成的证据。
必须逐项核对任务完成契约。对于“打完、全部、所有、清空”等穷尽型目标，结果页、单次成功、奖励或进度增加只能证明有进展；
最终截图必须直接显示剩余项为零、入口禁用、列表为空或明确的全部完成提示，否则 success 必须为 false，并要求返回总览继续检查。
对于“最高、最大、最难、最末、最新”等边界型目标，必须核对本轮枚举得到的完整候选集合和比较依据；
只观察到一个选项、只凭名称/COMPLETE/箭头外观、或没有证明枚举已经闭合时，success 必须为 false。"""


_EXHAUSTIVE_TASK_RE = re.compile(
    r"(打完|推完|做完|完成所有|全部|所有|清空|领完|拿完|处理完|用完|跑完|"
    r"\ball\b|\bevery\b|\beach\b|everything|clear all)", re.I
)
_BOUNDARY_TASK_RE = re.compile(
    r"(最高|最低|最大|最小|最难|最简单|最末|最后一个|最新|最旧|"
    r"highest|lowest|maximum|minimum|max\b|min\b|latest|oldest|hardest|easiest)", re.I
)
_COMPOSITE_TASK_RE = re.compile(
    r"(然后|然后再|接着|之后|以后|最后|再进入|再完成|并进入|并完成|并领取|"
    r"\bthen\b|after that|finally|and then)", re.I
)

ALLOWED_ACTIONS = {"click", "scroll", "key", "type", "wait", "finish", "ask"}
TASK_STATUSES = {"continue", "candidate_complete", "blocked"}
LEDGER_RESOLUTION_CHOICES = {
    "accept_closure", "discriminating_probe", "stop_unverified",
}

# Single source of truth for every closed-choice field in the executor reply.
# The same table is shown to the model and enforced by _parse_action.
ACTION_FIELD_PROTOCOL: dict[str, dict[str, Any]] = {
    "action": {
        "description": "本轮唯一界面动作；blocked 是状态而不是动作",
        "type": "string", "enum": sorted(ALLOWED_ACTIONS), "required": True,
    },
    "task_status": {
        "description": "任务状态；blocked 时使用 action=ask",
        "type": "string", "enum": sorted(TASK_STATUSES), "default": "continue",
    },
    "certainty": {
        "description": "当前界面结论的证据级别",
        "type": "string", "enum": ["confirmed", "inferred", "unknown"],
    },
    "intent_type": {
        "description": "动作意图；关闭遮挡层不属于导航",
        "type": "string", "enum": ["dismiss_overlay", "navigate", "interact", "unknown"],
    },
    "option_direction": {
        "description": "有序选择器的推进方向；非选择器动作填 null",
        "type": ["string", "null"], "enum": ["left", "right", None],
    },
    "nav_right": {
        "description": "截图中右向导航控件的可见状态；看不清填 null",
        "type": ["string", "null"], "enum": ["enabled", "disabled", "absent", None],
    },
    "resolution_choice": {
        "description": "仅在运行时报告枚举分歧时填写的处置选项",
        "type": ["string", "null"],
        "enum": sorted(LEDGER_RESOLUTION_CHOICES) + [None],
    },
    "visible_controls[].region": {
        "description": "控件所在的标准屏幕区域",
        "type": "string",
        "enum": ["left_sidebar", "right_sidebar", "top_navigation", "bottom_action_bar", "main_content"],
    },
    "visible_controls[].container": {
        "description": "控件所属的标准容器；无法归类时填 unknown",
        "type": "string",
        "enum": ["persistent_nav", "dialog", "list_row", "content_panel", "overlay", "form", "unknown"],
    },
    "visible_controls[].type": {
        "description": "控件的交互类型",
        "type": "string",
        "enum": ["navigation", "primary_action", "context_action", "toggle", "input", "unknown"],
    },
    "target_region": {
        "description": "click 目标所在的标准屏幕区域",
        "type": "string",
        "enum": ["left_sidebar", "right_sidebar", "top_navigation", "bottom_action_bar", "main_content"],
    },
    "target_container": {
        "description": "click 目标所属的标准容器",
        "type": "string",
        "enum": ["persistent_nav", "dialog", "list_row", "content_panel", "overlay", "form", "unknown"],
    },
    "target_type": {
        "description": "click 目标的交互类型",
        "type": "string",
        "enum": ["navigation", "primary_action", "context_action", "toggle", "input", "unknown"],
    },
    "pager_total": {
        "description": "当前截图明确可数的总页数；看不清填 null，禁止估算",
        "type": ["integer", "null"], "minimum": 1,
    },
    "pager_index": {
        "description": "当前截图明确可见的一基页序号；看不清填 null",
        "type": ["integer", "null"], "minimum": 1,
    },
    "search.stop": {
        "description": "有序搜索的收口原因",
        "type": "string", "enum": ["wrap", "unavailable", "single"],
    },
    "search.pager_total": {
        "description": "搜索交付时当前截图可读的总数；看不清填 null",
        "type": ["integer", "null"], "minimum": 1,
    },
    "all_options_visible": {
        "description": "当前一屏是否明确展示了全部候选",
        "type": "boolean", "enum": [True, False],
    },
    "option_is_minimum": {
        "description": "当前项是否有可见证据表明为逻辑最小项",
        "type": "boolean", "enum": [True, False],
    },
}

ACTION_PROTOCOL_ANNOTATION = json.dumps(
    ACTION_FIELD_PROTOCOL, ensure_ascii=False, separators=(",", ":")
)

# 枚举账本的失速闸门：连续多少步没有新增候选就提醒，提醒几次后交回用户。
_LEDGER_STALL_LIMIT = 5
# 闭环之后「没有新增候选」是恒真的常态，不能再拿它算停滞；改数闭环后又点了几次箭头。
_LEDGER_POST_CLOSURE_LIMIT = 3

_COMPLETION_STANDARDS = {
    "state_goal": "最小完成原则；出现能直接证明目标已完成的可见证据即可收束",
    "exhaustive": "穷尽型完成原则；阶段成功后必须回到总览确认剩余项为零",
    "boundary_optimum": "极值型完成原则；先闭合枚举、记录全部候选并给出比较依据，再选目标项",
}


def build_task_contract(task: str) -> dict[str, Any]:
    """Return what must never happen, plus a completion standard.

    The contract deliberately carries **no route guidance**: it never states
    where to click, which entry is preferred, or which page counts as "too
    deep".  Those decisions belong to the decision model and to runtime
    evidence.  The only hard constraint is the prohibition list, and that list
    is expressed in domain-neutral behaviour categories, so it holds for any
    application instead of for one particular game.
    """
    text = str(task or "")
    lowered = text.lower()
    extremum = bool(_BOUNDARY_TASK_RE.search(lowered))
    exhaustive = bool(_EXHAUSTIVE_TASK_RE.search(lowered))
    composite = bool(_COMPOSITE_TASK_RE.search(lowered))
    completion_mode = (
        "boundary_optimum" if extremum
        else "exhaustive" if exhaustive
        else "state_goal"
    )
    if completion_mode == "boundary_optimum":
        success_conditions = [
            "本轮已经遍历并记录全部可达选项，而不是只观察当前选项",
            "枚举已经闭合：切换后回到已见选项、连续两次无变化，或当前页面明确展示完整候选列表",
            "对完整候选集合给出明确比较依据，并选择比较结果中的目标项",
            "选项名称、COMPLETE/完成标记和箭头外观不能代替全量枚举与比较",
        ]
    elif completion_mode == "exhaustive":
        success_conditions = [
            "最终总览或列表中可见剩余项为零、目标入口禁用、列表为空或明确的全部完成提示",
            "单次成功、结果页、奖励或进度增加只能证明阶段进展，不能证明穷尽型目标完成",
        ]
    else:
        success_conditions = ["截图中出现能够直接证明用户目标已经完成的可见证据"]
    if composite:
        success_conditions.append(
            "任务包含多个先后阶段：中间阶段完成只算进展，必须继续到最后一个阶段"
        )
        if completion_mode == "boundary_optimum":
            success_conditions.append(
                "选出目标项后还必须实际完成任务的后续阶段；停在选择页不算完成"
            )
    return {
        "prohibited": detect_prohibited_categories(text),
        "completion_mode": completion_mode,
        "composite": composite,
        "success_conditions": success_conditions,
        "completion_standard": _COMPLETION_STANDARDS[completion_mode],
    }


def _parse_json(text: str) -> dict[str, Any]:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.I | re.S)
    try:
        value = json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise ValueError("模型没有返回 JSON")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return value


def _to_int(value: Any) -> int:
    """Best-effort int for model-supplied readings; anything unusable becomes 0."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _validate_enum_field(value: dict[str, Any], field: str) -> None:
    if field not in value:
        return
    allowed = ACTION_FIELD_PROTOCOL[field]["enum"]
    if value[field] not in allowed:
        raise ValueError(f"{field} 只能填 {allowed}，实际为 {value[field]!r}")


def _validate_nullable_positive_int(value: dict[str, Any], field: str) -> None:
    if field not in value or value[field] is None:
        return
    raw = value[field]
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"{field} 必须是 >= 1 的整数或 null，实际为 {raw!r}")


def _validate_visible_controls(value: dict[str, Any]) -> None:
    controls = value.get("visible_controls")
    if controls is None:
        return
    if not isinstance(controls, list):
        raise ValueError("visible_controls 必须是数组")
    if len(controls) > 12:
        raise ValueError("visible_controls 最多 12 项")
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            raise ValueError(f"visible_controls[{index}] 必须是对象")
        for child in ("region", "container", "type"):
            key = f"visible_controls[].{child}"
            if child not in control:
                raise ValueError(f"visible_controls[{index}] 缺少 {child}")
            allowed = ACTION_FIELD_PROTOCOL[key]["enum"]
            if control[child] not in allowed:
                raise ValueError(
                    f"visible_controls[{index}].{child} 只能填 {allowed}，"
                    f"实际为 {control[child]!r}"
                )


def _validate_string_list_if_present(value: dict[str, Any], field: str) -> None:
    if field not in value:
        return
    raw = value[field]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} 必须是字符串数组")


def _validate_search(value: dict[str, Any]) -> None:
    if "search" not in value:
        return
    search = value["search"]
    if not isinstance(search, dict):
        raise ValueError("search 必须是对象")
    for field in ("method", "start_item", "stop", "stop_evidence"):
        if not isinstance(search.get(field), str) or not search[field].strip():
            raise ValueError(f"search.{field} 必须是非空字符串")
    visited = search.get("visited")
    if not isinstance(visited, list) or not all(isinstance(item, str) for item in visited):
        raise ValueError("search.visited 必须是字符串数组")
    allowed = ACTION_FIELD_PROTOCOL["search.stop"]["enum"]
    if search["stop"] not in allowed:
        raise ValueError(f"search.stop 只能填 {allowed}，实际为 {search['stop']!r}")
    if "pager_total" not in search:
        raise ValueError("search 缺少 pager_total；看不清应填 null")
    pager_total = search["pager_total"]
    if (pager_total is not None and (
        isinstance(pager_total, bool) or not isinstance(pager_total, int) or pager_total < 1
    )):
        raise ValueError("search.pager_total 必须是 >= 1 的整数或 null")


def _parse_action(text: str) -> dict[str, Any]:
    value = _parse_json(text)
    name = str(value.get("action") or "").lower()
    # blocked describes a terminal state, not a UI operation.  Models often
    # use it as both; normalize that harmless form to the needs-user path.
    if name == "blocked":
        name = "ask"
        value["task_status"] = "blocked"
    if name not in ALLOWED_ACTIONS:
        raise ValueError(f"不支持的动作类型：{name or '<empty>'}")
    value["action"] = name
    for field in (
        "certainty", "intent_type", "option_direction", "nav_right",
        "resolution_choice", "target_region", "target_container", "target_type",
        "all_options_visible", "option_is_minimum",
    ):
        _validate_enum_field(value, field)
    _validate_nullable_positive_int(value, "pager_total")
    _validate_nullable_positive_int(value, "pager_index")
    if value.get("pager_total") is not None and value.get("pager_index") is not None:
        if value["pager_index"] > value["pager_total"]:
            raise ValueError(
                f"pager_index={value['pager_index']} 不能大于 pager_total={value['pager_total']}"
            )
    _validate_visible_controls(value)
    for field in (
        "observed_facts", "inferences", "uncertainties", "evidence",
        "visible_options", "option_inventory",
    ):
        _validate_string_list_if_present(value, field)
    _validate_search(value)
    status = str(
        value.get("task_status")
        or ("candidate_complete" if name == "finish" else "continue")
    ).lower()
    if status not in TASK_STATUSES:
        raise ValueError(f"task_status 只能填 {sorted(TASK_STATUSES)}，实际为 {status!r}")
    value["task_status"] = status
    if name == "click":
        missing = [key for key in ("x", "y") if key not in value]
        if missing:
            raise ValueError("click 缺少坐标字段：" + ", ".join(missing))
        try:
            float(value["x"])
            float(value["y"])
        except (TypeError, ValueError) as exc:
            raise ValueError("click 的 x/y 必须是数字") from exc
        box = value.get("target_box")
        if box is not None:
            if not isinstance(box, list) or len(box) != 4:
                raise ValueError("click 的 target_box 必须是 [x1,y1,x2,y2]")
            try:
                value["target_box"] = [float(item) for item in box]
            except (TypeError, ValueError) as exc:
                raise ValueError("click 的 target_box 必须全部是数字") from exc
    return value


def _load_visual_skill(name: str) -> str:
    """Read a SKILL.md body, dropping its YAML frontmatter."""
    path = Path(__file__).with_name("skills") / name / "SKILL.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return re.sub(r"^---\s*.*?\s*---\s*", "", raw, count=1, flags=re.S).strip()


def _small_gray(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)


def analyze_frame(frame: np.ndarray, previous: np.ndarray | None = None) -> dict[str, Any]:
    """Cheap local visual analysis used before deciding whether to upload a frame."""
    current = _small_gray(frame)
    result: dict[str, Any] = {
        "black_ratio": round(float(np.mean(current < 14)), 4),
        "mean_luma": round(float(np.mean(current)), 2),
        "changed_ratio": 1.0,
        "mean_delta": 1.0,
        "same": False,
    }
    if previous is None:
        return result
    old = _small_gray(previous)
    diff = cv2.absdiff(current, old)
    changed_ratio = float(np.mean(diff > 18))
    mean_delta = float(np.mean(diff)) / 255.0
    result.update(
        changed_ratio=round(changed_ratio, 4),
        mean_delta=round(mean_delta, 4),
        same=changed_ratio < 0.004 and mean_delta < 0.006,
    )
    return result


class VisualAgentRuntime:
    def __init__(self, controller, *, account: str, target: str, task: str,
                 model: dict[str, Any], on_event: Callable[[str, Any], None] | None = None,
                 max_steps: int | None = None, report_root: Path | str | None = None,
                 transition_memory_path: Path | str | None = None,
                 load_profile_path: Path | str | None = None,
                 visual_memory_path: Path | str | None = None,
                 visual_memory_enabled: bool = True,
                 decision_layer_enabled: bool = False,
                 same_frame_timeout: float = 10.0,
                 same_frame_poll: float = 0.35):
        self.controller, self.account, self.target_kind = controller, account, target
        self._execution = ExecutionBridge(controller)
        self.task, self.model = task, model
        self.on_event = on_event or (lambda _kind, _data: None)
        # None 表示由完成条件、用户停止或安全闸门收束，不设人工步数上限。
        # 显式整数仍供测试和诊断场景使用，避免故障用例无限运行。
        self.max_steps = None if max_steps is None else max(1, int(max_steps))
        self._same_frame_timeout = max(0.0, float(same_frame_timeout))
        self._same_frame_poll = max(0.01, float(same_frame_poll))
        self.report_root = Path(report_root) if report_root else Path.home() / ".minashigo" / "visual_agent_runs"
        memory_path = (
            Path(transition_memory_path) if transition_memory_path
            else Path(__file__).resolve().parents[2] / "user_data" / "ui_transition_memory.json"
        )
        self._transition_memory = UITransitionMemory(memory_path)
        self._run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self._visual_memory: VisualMemoryStore | None = None
        self._pending_episode_id = ""
        if visual_memory_enabled:
            shadow_path = (
                Path(visual_memory_path) if visual_memory_path
                else (
                    Path(report_root) / "_memory" / "visual_memory.sqlite3"
                    if report_root
                    else Path(__file__).resolve().parents[2] / "user_data" / "visual_memory.sqlite3"
                )
            )
            try:
                self._visual_memory = VisualMemoryStore(
                    shadow_path, evidence_root=shadow_path.parent / "visual_memory_evidence"
                )
            except Exception as exc:
                # Memory is observational. It must never prevent task execution.
                self._emit("log", f"影子记忆初始化失败，已跳过：{type(exc).__name__}: {exc}")
        self._load_profile = LoadProfile(
            Path(load_profile_path) if load_profile_path
            else Path(__file__).resolve().parents[2] / "user_data" / "visual_agent_load_profile.json"
        )
        self._loading: LoadingWatch | None = None
        self._loading_scope = ""
        self._effect_tracker = ActionEffectTracker()
        self._stopped = False
        self._usage = {"input": 0, "output": 0}
        self._contract = build_task_contract(task)
        self._decision_supervisor: DecisionSupervisor | None = None
        if decision_layer_enabled:
            self._decision_supervisor = DecisionSupervisor(
                task=task, local_contract=self._contract, model=model, call_model=call_llm,
                on_log=lambda text: self._emit("log", text, layer="decision"),
                on_usage=self._record_decision_usage,
            )
        # Selector readings are accumulated per step and can never be retracted
        # at delivery time, which is what makes the delivery check worth having.
        self._pager_total = 0
        self._pager_total_untrusted = False
        self._selector_conflicts: list[str] = []
        self._observed_options: list[str] = []
        self._selector_readings: list[dict[str, Any]] = []
        # Only extremum tasks get the traversal procedure; everything else stays lean.
        self._boundary_task = self._contract.get("completion_mode") == "boundary_optimum"
        self._search_skill = _load_visual_skill("ordered-search") if self._boundary_task else ""
        # 枚举账本：状态留在运行时，每轮投影进 prompt。对话历史会被截断到几步，
        # 所以模型「不能忘」的东西只能重投影，不能指望它记得前 N 轮说过什么。
        self._steps_since_new_option = 0
        self._option_repeat_counts: dict[str, int] = {}
        self._last_reading: dict[str, Any] = {}
        self._ledger_scene = ""
        self._ledger_armed = False
        self._ledger_nudges = 0
        self._ledger_resolution_context: dict[str, Any] | None = None
        self._pending_decision_conflict: dict[str, Any] | None = None
        # 收口提示：停下之前先告诉模型「这一侧怎么收」——实况里它不是不会枚举，
        # 是以为必须找到第 7 项，所以一直不收。
        self._last_effect_class = ""
        self._stall_no_change = 0
        self._closure_hint = ""
        # 闭环是「同向推进后回到已见项」，靠方向+读数变化判定，不靠停滞凑数。
        self._pending_direction = ""
        self._prior_direction = ""
        self._closed = False
        self._closure_evidence = ""
        self._closure_announced = False
        self._clicks_since_closure = 0
        # 停止理由本身就是结构证据：回到已见项 ⇒ 环形轮盘；方法点不动 ⇒ 非环形。
        self._gui_shape = ""
        self._gui_shape_basis = ""
        self._last_frame: np.ndarray | None = None
        self._last_click: dict[str, Any] = {}
        self._history: list[dict[str, Any]] = []
        self._model_trace_seq = 0

    def _record_decision_usage(self, tin: int, tout: int) -> None:
        self._usage["input"] += int(tin or 0)
        self._usage["output"] += int(tout or 0)
        self._emit("usage", {"input": int(tin or 0), "output": int(tout or 0), "layer": "decision"})

    def stop(self):
        self._stopped = True

    def _emit(self, kind, data, *, layer="execution"):
        # Log lines carry their source layer so the GUI can colour them without
        # every caller passing a tag; other event kinds keep their old shape.
        if kind == "log":
            data = {"text": str(data), "layer": layer}
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    def _target(self):
        browsers = getattr(self.controller, "_browser_instances", {})
        windows = getattr(self.controller, "_window_instances", {})
        if self.target_kind == "window":
            runtime, kind = windows.get(self.account), "window"
        elif self.target_kind == "browser":
            runtime, kind = browsers.get(self.account), "browser"
        else:
            ctrl = getattr(self.controller, "_task_ctrls", {}).get(self.account)
            acc = getattr(ctrl, "account", {}) if ctrl else {}
            if acc.get("_target") == "window":
                runtime = windows.get(self.account) or browsers.get(self.account)
                kind = "window" if windows.get(self.account) else "browser"
            else:
                runtime = browsers.get(self.account) or windows.get(self.account)
                kind = "browser" if browsers.get(self.account) else "window"
        if runtime is None:
            raise RuntimeError("当前账号没有可用的浏览器或窗口目标")
        return getattr(runtime, "_obj", runtime), kind

    async def _observe(self, runtime, kind):
        frame = await (
            runtime.update_game_frame()
            if kind == "browser" and hasattr(runtime, "update_game_frame")
            else runtime.update_frame()
        )
        if frame is None:
            raise RuntimeError("截图失败")
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if not ok:
            raise RuntimeError("截图编码失败")
        meta = {"width": int(frame.shape[1]), "height": int(frame.shape[0]), "kind": kind}
        if kind == "browser":
            browser = getattr(runtime, "_browser", runtime)
            page = getattr(browser, "page", None)
            page_url = str(getattr(page, "url", "") or "")
            hostname = urlparse(page_url).hostname or "browser"
            meta["application_scope"] = f"browser:{hostname.lower()}"
            origin = getattr(browser, "_frame_origin_css", (0, 0)) or (0, 0)
            meta.update(origin=[float(origin[0]), float(origin[1])], scale_x=1.0, scale_y=1.0)
            try:
                from backend.browser.game_frame_capture import inspect_game_viewport
                geometry = await inspect_game_viewport(browser.page)
                box = (geometry.get("game_frame") or {}).get("bounds") or {}
                meta["scale_x"] = float(box.get("width") or meta["width"]) / meta["width"]
                meta["scale_y"] = float(box.get("height") or meta["height"]) / meta["height"]
            except Exception:
                pass
        else:
            title = str(getattr(runtime, "title", "") or getattr(runtime, "window_title", "") or self.account)
            normalized_title = re.sub(r"\s+", " ", title).strip().lower()[:120]
            meta["application_scope"] = f"window:{normalized_title}"
        return frame, base64.b64encode(encoded.tobytes()).decode("ascii"), meta

    async def _call_json(self, *, system_prompt: str, prompt: str,
                         image: str | None, purpose: str,
                         parser: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        request = prompt
        for attempt in range(3):
            content: list[dict[str, Any]] = [{"type": "text", "text": request}]
            if image:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}})
            text, tin, tout = await call_llm(
                provider=self.model["provider"], api_key=self.model["api_key"],
                model=self.model["model"], api_endpoint=self.model.get("endpoint") or None,
                messages=[{"role": "user", "content": content}],
                system_prompt=system_prompt,
                max_tokens=min(int(self.model.get("max_tokens") or 1024), 2048),
            )
            self._usage["input"] += int(tin or 0)
            self._usage["output"] += int(tout or 0)
            self._emit("usage", {"input": tin, "output": tout, "layer": "execution"})
            trace_dir = self._save_model_trace(
                purpose=purpose, attempt=attempt + 1, prompt=request,
                system_prompt=system_prompt, image=image, raw_response=text,
            )
            try:
                parsed = parser(text)
                self._finish_model_trace(trace_dir, parsed)
                return parsed
            except Exception as exc:
                if attempt >= 2:
                    raise ValueError(f"{purpose}协议修复失败：{exc}") from exc
                self._emit("log", f"{purpose}未返回合法 JSON，正在要求模型纠正（{attempt + 1}/2）")
                request = (
                    f"你刚才的回复无法解析：{text!r}\n错误：{exc}\n"
                    "不要重新分析，也不要输出解释。请把同一个决定改写为符合系统协议的单个 JSON 对象。"
                )
                image = None
        raise RuntimeError(f"{purpose}失败")

    def _save_model_trace(self, *, purpose: str, attempt: int, prompt: str,
                          system_prompt: str, image: str | None,
                          raw_response: str) -> Path | None:
        """Persist the exact model input image and raw output for local audit."""
        try:
            self._model_trace_seq += 1
            safe = re.sub(r"[^\w\-]+", "_", purpose).strip("_") or "model"
            trace_dir = (
                Path(self.report_root) / "_model_traces" / self._run_id
                / f"{self._model_trace_seq:04d}_{safe}_try{attempt}"
            )
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / "request.json").write_text(json.dumps({
                "purpose": purpose,
                "attempt": attempt,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "has_image": bool(image),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            (trace_dir / "raw_response.txt").write_text(
                str(raw_response or ""), encoding="utf-8"
            )
            if image:
                (trace_dir / "model_input.jpg").write_bytes(base64.b64decode(image))
            self._emit("log", f"模型审计记录：{trace_dir}")
            return trace_dir
        except Exception as exc:
            self._emit("log", f"模型审计记录保存失败：{type(exc).__name__}: {exc}")
            return None

    def _finish_model_trace(self, trace_dir: Path | None, parsed: dict[str, Any]) -> None:
        if trace_dir is None:
            return
        try:
            (trace_dir / "parsed.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            image_path = trace_dir / "model_input.jpg"
            if not image_path.exists() or str(parsed.get("action") or "") != "click":
                return
            canvas = cv2.imdecode(
                np.frombuffer(image_path.read_bytes(), dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if canvas is None:
                return
            box = parsed.get("target_box")
            if isinstance(box, list) and len(box) == 4:
                x1, y1, x2, y2 = (int(round(float(v))) for v in box)
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 255), 3)
            x, y = int(round(float(parsed["x"]))), int(round(float(parsed["y"])))
            cv2.drawMarker(canvas, (x, y), (0, 0, 255), cv2.MARKER_CROSS, 28, 3)
            cv2.circle(canvas, (x, y), 10, (0, 0, 255), 2)
            ok, encoded = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if ok:
                (trace_dir / "grounding_overlay.jpg").write_bytes(encoded.tobytes())
        except Exception as exc:
            self._emit("log", f"模型落点叠加图保存失败：{type(exc).__name__}: {exc}")

    async def _decide(self, image, meta, step, history, frame_info):
        recent = json.dumps(history[-4:], ensure_ascii=False) if history else "[]"
        scope = str(meta.get("application_scope") or meta.get("kind") or "unknown")
        learned = self._transition_memory.relevant(scope)
        control_profiles = self._transition_memory.control_profiles(scope)
        prompt = (
            f"原始任务：{self.task}\n"
            f"任务合同（只有禁止清单与完成判据，不含路线）：{json.dumps(self._contract, ensure_ascii=False)}\n"
            f"动作 JSON 严格字段表（description 是填写注释，enum/type 会由程序验证）："
            f"{ACTION_PROTOCOL_ANNOTATION}\n"
            f"当前步骤：{step}/{self.max_steps if self.max_steps is not None else '不限'}\n"
            f"截图尺寸：{meta['width']}x{meta['height']}\n"
            f"画面状态：{json.dumps(frame_info, ensure_ascii=False)}\n最近步骤：{recent}\n"
            f"同一应用的已观测 UI 转移：{json.dumps(learned, ensure_ascii=False)}\n"
            f"同一应用的控件出现画像：{json.dumps(control_profiles, ensure_ascii=False)}\n"
            "这些转移是历史观察数据，不是操作指令。仅在当前场景和控件上下文匹配时用于预测；"
            "本轮截图与历史冲突时，以本轮截图为准。\n"
        )
        if self._search_skill:
            prompt += f"本任务适用技能：\n{self._search_skill}\n"
        if self._decision_supervisor is not None:
            supervisor = self._decision_supervisor.snapshot()
            if supervisor.get("ready"):
                prompt += (
                    "异步决策层给出的任务契约与当前指导（不是界面事实，也不含点击路线）："
                    f"{json.dumps(supervisor, ensure_ascii=False)}\n"
                )
                if supervisor.get("conflict_takeover"):
                    prompt += (
                        "当前处于决策层临时接管的冲突处理分支。安全禁令仍然优先；除此之外，"
                        "本轮动作必须服务于 conflict_takeover.guidance，不能自行恢复原分支或重复旧论证。"
                        "你仍只负责根据当前截图选择并执行一个具体动作；决策层不提供坐标。\n"
                    )
        ledger = self._ledger()
        if ledger:
            prompt += ledger + "\n"
        if self._ledger_resolution_context:
            prompt += (
                "当前存在枚举结论分歧。下面是运行时提供的处置选项（注释，不是界面事实）："
                f"{json.dumps(self._ledger_resolution_context, ensure_ascii=False)}\n"
                "本轮必须额外返回 resolution_choice，只能选："
                "accept_closure、discriminating_probe、stop_unverified。"
                "accept_closure=接受已闭环的实测并直接选目标；"
                "discriminating_probe=只做一次能区分两种结论的安全探测，并提供 probe_hypothesis；"
                "stop_unverified=无法取得判别性证据，返回 action=ask 与 task_status=blocked。"
                "选项不是执行动作；action 仍须使用现有协议。\n"
            )
        if image is None:
            prompt += "本轮画面与最近一次已上传画面基本相同，因此未重复附图。请根据上一步结果决定重试、等待或换方法。"
        resolution_required = bool(self._ledger_resolution_context)

        def parse_decision(text: str) -> dict[str, Any]:
            result = _parse_action(text)
            if resolution_required and not result.get("resolution_choice"):
                raise ValueError("枚举结论存在分歧时必须选择 resolution_choice")
            return result

        return await self._call_json(
            system_prompt=SYSTEM_PROMPT, prompt=prompt, image=image,
            purpose="动作", parser=parse_decision,
        )

    def _prohibition_guard(self, action: dict[str, Any]) -> dict[str, Any] | None:
        """Refuse actions that touch a prohibited behaviour class.

        The guard knows nothing about routes or about which application is being
        driven: it only refuses payment, credential, destructive-account and
        external-grant actions, which is the sole hard constraint of the
        contract.
        """
        return check_action(action)

    def _set_gui_shape(self, shape: str, basis: str) -> None:
        """The stop reason *is* the structural evidence, so it is worth reporting.

        ① 推进后回到已见项 ⇒ 环形轮盘（一定有 wrap 边）
        ② 推进再也动不了 ⇒ 非环形（有端点）

        只记第一次：同一个选择器不会既是环又是线。
        """
        if self._gui_shape:
            return
        self._gui_shape = shape
        self._gui_shape_basis = basis
        label = "环形轮盘" if shape == "ring" else "非环形"
        self._emit("log", f"GUI 形态：{label}（依据：{basis}）")

    @staticmethod
    def _click_direction(action: dict[str, Any], meta: dict[str, Any]) -> str:
        """Which arrow a click moved: from where it landed, text only as fallback.

        The model never fills option_direction (0 of 210 recorded transitions),
        so the arrow's side of the screen is the dependable signal.  Direction
        matters because "back to a seen item" means closure mid-sweep, and
        merely a return trip when the model reverses — the skill treats those
        two very differently.
        """
        if str(action.get("action") or "") != "click":
            return ""
        try:
            ratio = float(action.get("x")) / max(1.0, float(meta.get("width") or 0))
        except (TypeError, ValueError):
            ratio = -1.0
        if ratio >= 0.75:
            return "right"
        if 0.0 <= ratio <= 0.25:
            return "left"
        text = " ".join(str(action.get(key) or "") for key in ("target", "message"))
        right = bool(re.search(r"右|right", text, re.I))
        left = bool(re.search(r"左|left", text, re.I))
        if right and not left:
            return "right"
        if left and not right:
            return "left"
        return ""

    def _note_selector_reading(self, action: dict[str, Any], *, moved: bool = False,
                               direction: str = "") -> None:
        """Accumulate structural readings a selector shows about itself.

        The pager tells us how many candidates exist; the observed option name
        tells us which one the model is looking at.  Both are recorded before
        any delivery decision, so a late "I did see all six" cannot overwrite an
        earlier "6 total, 3 seen".

        ``moved`` means this reading sits on a step that also moved the
        selector, so "read a candidate we have already seen" is evidence of a
        step that gained nothing.  ``direction`` is the move *about to be*
        issued; the move that produced this reading is the one pending from the
        previous step.
        """
        step_direction = self._pending_direction
        self._pending_direction = direction
        total = _to_int(action.get("pager_total"))
        index = _to_int(action.get("pager_index"))
        option = str(action.get("observed_option") or "").strip()
        arrows = str(action.get("nav_right") or "").strip().lower()
        if total or index or option or arrows:
            self._selector_readings.append({
                "pager_total": total or None,
                "pager_index": index or None,
                "nav_right": arrows or None,
                "observed_option": option or None,
            })
        if total and self._pager_total and total != self._pager_total:
            self._mark_selector_conflict(
                f"pager_total 在同一场景内不一致：{self._pager_total} 与 {total}"
            )
        elif total and not self._pager_total_untrusted:
            self._pager_total = total
        if not option:
            # 没报读数就只按「有移动」计数：一旦上过读数，沉默也算没长进。
            if moved and self._ledger_armed:
                self._count_move()
            if step_direction:
                self._prior_direction = step_direction
            return
        previous_option = str(self._last_reading.get("option") or "")
        previous_index = _to_int(self._last_reading.get("index"))
        if (option and previous_option and option != previous_option
                and index and previous_index and index == previous_index
                and step_direction):
            self._mark_selector_conflict(
                f"选项已从 {previous_option} 变为 {option}，但 pager_index 仍为 {index}"
            )
        repeated = option in self._observed_options
        start_item = self._observed_options[0] if self._observed_options else ""
        just_closed = False
        if (not self._closed and repeated and option == start_item
                and option != previous_option and step_direction
                and step_direction == self._prior_direction
                and len(self._observed_options) >= 2):
            # 同一方向推进，回到这轮的起点 S：技能里的闭环，只记第一次。
            self._closed = True
            just_closed = True
            self._closure_evidence = (
                f"{previous_option or '上一项'} → {option}（{step_direction} 方向推进回到起点）"
            )
            if total and total != len(self._observed_options):
                self._mark_selector_conflict(
                    f"闭环实测为 {len(self._observed_options)} 项，但 pager_total 自报为 {total}"
                )
            self._set_gui_shape("ring", f"同向推进回到已见项：{self._closure_evidence}")
        if not repeated:
            self._observed_options.append(option)
            self._steps_since_new_option = 0
            self._stall_no_change = 0
            # 有进展就把收口提示和提醒次数清掉，否则之后每次停顿都会直接到第 3 次。
            self._closure_hint = ""
            self._ledger_nudges = 0
            self._closure_announced = False
        elif moved and not just_closed:
            self._count_move()
        self._option_repeat_counts[option] = int(self._option_repeat_counts.get(option) or 0) + 1
        self._last_reading = {"option": option, "index": index or None}
        self._ledger_armed = True
        if step_direction:
            self._prior_direction = step_direction

    def _mark_selector_conflict(self, reason: str) -> None:
        if reason not in self._selector_conflicts:
            self._selector_conflicts.append(reason)
        self._pager_total_untrusted = True
        self._pager_total = 0

    def _count_move(self) -> None:
        """闭环之后「没有新增候选」是恒真的：改数闭环后又点了几次箭头。"""
        if self._closed:
            self._clicks_since_closure += 1
        else:
            self._steps_since_new_option += 1

    def _sync_ledger_scene(self, action: dict[str, Any]) -> None:
        """A different scene is a different selector: the stall count restarts."""
        if not self._boundary_task:
            return
        scene = self._memory_state(action)
        if self._ledger_scene and scene != self._ledger_scene:
            self._steps_since_new_option = 0
            self._closed = False
            self._closure_evidence = ""
            self._closure_announced = False
            self._clicks_since_closure = 0
            self._closure_hint = ""
            self._pending_direction = ""
            self._prior_direction = ""
            self._pager_total = 0
            self._pager_total_untrusted = False
            self._selector_conflicts.clear()
        self._ledger_scene = scene

    def _ledger(self) -> str:
        """One-line projection of the traversal ledger into the decision prompt.

        Deliberately constant-size: the point of a projection is that a long run
        costs the same as a short one, unlike a conversation that grows.
        """
        if not self._boundary_task:
            return ""
        seen = list(self._observed_options)
        if not seen:
            return (
                "枚举账本（运行时实测，不得改写）：尚未读到候选名。"
                "若当前在选项页，本轮必须用 observed_option 报出画面上的这一项。"
            )
        head = f"…（前 {len(seen) - 10} 项略）" if len(seen) > 10 else ""
        shape = {"ring": "环形轮盘", "linear": "非环形"}.get(self._gui_shape, "未知")
        if self._closed:
            after = (
                f"（闭环后已点 {self._clicks_since_closure} 次箭头，仍未选中）"
                if self._clicks_since_closure else ""
            )
            return (
                f"枚举账本（运行时实测，不得改写）：闭环成立——{self._closure_evidence}，"
                f"本轮已读 {len(seen)} 项：{'、'.join(seen[-10:])}；形态={shape}{after}。"
                "若画面确无未读候选，先点选目标项（选中后如有确认框，按确定），"
                "确认已选中再交付（search.stop=wrap）；若还有未读候选，先点开它把名字读出来。"
            )
        last = str(self._last_reading.get("option") or "")
        repeats = int(self._option_repeat_counts.get(last) or 0)
        wrap = f"；该项是第 {repeats} 次出现" if repeats >= 2 else ""
        index = self._last_reading.get("index")
        hint = ""
        if self._closure_hint == "wrap":
            hint = "；运行时判定：已回到已见项，可按 stop=wrap 收口"
        elif self._closure_hint == "unavailable":
            hint = "；运行时判定：该侧已到端点，需反向取另一侧端点（stop=unavailable）"
        return (
            f"枚举账本（运行时实测，不得改写）：已见候选 {head}{'、'.join(seen[-10:])}"
            f"（共 {len(seen)}）；最近读数 {last or 'null'}{wrap}"
            f"；上一步实测={self._last_effect_class or 'null'}"
            f"；pager_total={'untrusted' if self._pager_total_untrusted else (self._pager_total or 'null')}"
            f"，pager_index={index or 'null'}"
            f"；selector_conflicts={self._selector_conflicts[-2:] or []}"
            f"；本场景内已连续 {self._steps_since_new_option} 步没有新增候选"
            f"；形态={shape}{hint}"
        )

    def _ledger_gap(self, action: dict[str, Any]) -> str:
        """Mid-run stall check: the skill only bites at delivery, this does not.

        A selector whose every step changes half the screen looks "productive"
        to pixel-based guards, so the counter that matters is candidate novelty,
        not frame difference.  The message has to say *how this side closes*:
        in the recorded run the model did not fail to enumerate, it failed to
        stop, because it assumed an unread seventh candidate had to exist.
        """
        if not self._boundary_task or not self._ledger_armed:
            return ""
        if str(action.get("action") or "") not in {"click", "key", "scroll"}:
            return ""
        # Once traversal is closed, the guard must stop *navigation*, not the
        # requested selection itself.  A target-card click has no direction;
        # rejecting every click here made the instruction "select it now"
        # impossible to follow.
        if self._closed and str(action.get("action") or "") == "click":
            direction = str(action.get("option_direction") or "").strip().lower()
            label = " ".join(
                str(action.get(key) or "") for key in ("target", "message")
            ).strip().lower()
            navigation_words = (
                "箭头", "切换", "下一个", "上一个",
                "arrow", "next", "previous", "prev",
            )
            if label and direction not in {"left", "right"} and not any(
                word in label for word in navigation_words
            ):
                return ""
        if self._closed and not self._closure_announced:
            self._closure_announced = True
            return (
                f"枚举已经闭环（{self._closure_evidence}），共 {len(self._observed_options)} 项。"
                "若画面上确实没有未读候选，本轮请先点选目标项（弹确认框就按确定），"
                "确认已选中再交付（search.stop=wrap）；"
                "若你认为还有未读候选，就直接点它看名字，不要重复点同一个箭头。"
            )
        if self._closed:
            if self._clicks_since_closure < _LEDGER_POST_CLOSURE_LIMIT:
                return ""
            return (
                f"闭环已经成立（{self._closure_evidence}），但你之后又点了 "
                f"{self._clicks_since_closure} 次箭头，目标项还没被选中。"
                "再点箭头不会产生新信息：本轮直接点目标项（弹确认框按确定），不要点箭头。"
            )
        if self._steps_since_new_option < _LEDGER_STALL_LIMIT:
            return ""
        seen = "、".join(self._observed_options[-10:]) or "无"
        last = str(self._last_reading.get("option") or "null")
        head = (
            f"枚举账本显示已连续 {self._steps_since_new_option} 步没有新增候选"
            f"（已见 {len(self._observed_options)} 项：{seen}；最近读数仍是 {last}）。"
        )
        if self._stall_no_change >= 2:
            self._closure_hint = "unavailable"
            self._set_gui_shape("linear", "同一箭头连续两次推进后画面无变化（有端点）")
            return head + (
                "最近几步画面没有变化，说明这一侧已经到底。"
                "按技能 3b：从端点反向走到另一侧端点，"
                "给出 search 记录（stop=unavailable，endpoints 两个）后再交付；不要再点这个箭头。"
            )
        self._closure_hint = "wrap"
        return head + (
            "这些步只是回到了已见候选，运行时接受 stop=wrap 的交付。"
            "本轮请先点选目标项（弹确认框就按确定），确认已选中再交付，不必再找下一项；"
            "若你确有证据还有未记录的候选，就把它的名字写出来。"
        )


    def _search_record_gap(self, search: dict[str, Any]) -> str:
        """Validate the traversal record the ordered-search skill requires.

        Everything checked here is structural (counts, endpoints, stop reason),
        never a candidate name: the failure this guards is "stopped early and
        called it complete", which names cannot expose.
        """
        stop = str(search.get("stop") or "").strip().lower()
        visited = list(dict.fromkeys(
            str(item).strip() for item in (search.get("visited") or []) if str(item).strip()
        ))
        endpoints = list(dict.fromkeys(
            str(item).strip() for item in (search.get("endpoints") or []) if str(item).strip()
        ))
        excluded = search.get("excluded") or []
        total = _to_int(search.get("pager_total"))

        if stop == "single":
            if len(visited) > 1:
                return f"搜索记录自相矛盾：stop=single 却记录了 {len(visited)} 个候选"
            return ""
        if stop == "wrap":
            if len(visited) < 2:
                return f"闭环至少需要 2 个不同候选，当前只记录了 {len(visited)} 个"
            if excluded:
                return (
                    "闭环之前就排除了反向搜索方法；线性结构会因此漏掉起点另一侧的候选，"
                    "请先补走反方向再交付"
                )
            if total and len(visited) < total:
                return self._pager_mismatch(total, len(visited))
            return ""
        if stop == "unavailable":
            if len(endpoints) < 2:
                return (
                    f"只证明了单侧到底（endpoints 只有 {len(endpoints)} 个）；"
                    "必须从该端点反向走到底，拿到另一侧端点后才算完整"
                )
            if excluded:
                return "线形结构下两个方向覆盖不同候选，不能排除反向搜索方法"
            if total and len(visited) < total:
                return self._pager_mismatch(total, len(visited))
            return ""
        return "交付记录缺少合法的 stop 原因（wrap / unavailable / single）"

    def _pager_mismatch(self, total: int, seen: int) -> str:
        """A proven closure outranks a self-reported pager count.

        The dots are read off one screenshot by the same model that reads the
        names, and they can include locked or decorative slots — the recorded
        run reported 8 dots over 6 readable difficulties, which blocked delivery
        forever.  Once the runtime has watched the sweep return to its start,
        that is the stronger witness: disagree, but do not hold the run hostage.
        """
        if self._closed:
            self._emit(
                "log",
                f"pager_total={total} 与实测闭环的 {seen} 项不一致；以实测为准放行交付",
            )
            return ""
        return f"画面上有 {total} 个候选，闭环只覆盖了 {seen} 个"

    def _selector_gap(self, action: dict[str, Any]) -> str:
        """Reject a delivery contradicted by its traversal record or pager count.

        Deliberately fail-open: no record and no reading (null) never blocks, so
        a UI without pager dots and a model that ignores the skill cost nothing.
        Only a self-contradiction blocks, and the message names the exact gap.
        """
        search = action.get("search")
        if isinstance(search, dict):
            gap = self._search_record_gap(search)
            if gap:
                return gap
            # A structurally valid record is the richer witness, so it wins over
            # the cruder per-step tally.  A large disagreement is worth a log
            # line (anti-retcon signal) but not worth blocking a correct run.
            visited = [str(item).strip() for item in (search.get("visited") or []) if str(item).strip()]
            if len(visited) > len(self._observed_options) + 1:
                self._emit(
                    "log",
                    f"搜索记录与逐轮读数不一致：记录 {len(visited)} 项，"
                    f"逐轮上报 {len(self._observed_options)} 项",
                )
            return ""
        total = max(self._pager_total, _to_int(action.get("pager_total")))
        observed = self._observed_options
        if total <= 0 or not observed or total <= len(observed):
            return ""
        names = "、".join(observed) or "无"
        return (
            f"选择器显示共有 {total} 个候选项，但本轮只观测到 {len(observed)} 个（{names}）；"
            f"还差 {total - len(observed)} 个未查看，逐项看完之前不能交付"
        )

    async def _verify(self, image: str, claimed: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            f"原始任务：{self.task}\n"
            f"任务完成契约：{json.dumps(self._contract, ensure_ascii=False)}\n"
            f"执行代理声称：{claimed.get('message') or '任务完成'}\n"
            f"代理给出的证据：{json.dumps(claimed.get('evidence') or [], ensure_ascii=False)}\n"
            "请只根据本次最终截图验证。"
        )

        def parse_verdict(text: str) -> dict[str, Any]:
            value = _parse_json(text)
            if not isinstance(value.get("success"), bool):
                raise ValueError("验证结果缺少布尔值 success")
            evidence = value.get("evidence")
            if not isinstance(evidence, list):
                raise ValueError("验证结果缺少 evidence 数组")
            value["confidence"] = max(0.0, min(1.0, float(value.get("confidence") or 0)))
            return value

        return await self._call_json(
            system_prompt=VERIFY_PROMPT, prompt=prompt, image=image,
            purpose="完成验证", parser=parse_verdict,
        )

    @staticmethod
    def _frame_fingerprint(frame: np.ndarray) -> str:
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (16, 16), interpolation=cv2.INTER_AREA)
        quantized = (gray // 32).astype(np.uint8)
        return hashlib.sha1(quantized.tobytes()).hexdigest()[:16]

    @staticmethod
    def _is_transient_state(action: dict[str, Any]) -> bool:
        description = " ".join(str(action.get(key) or "") for key in (
            "current_state", "message", "expect"
        )).lower()
        return any(token in description for token in (
            "加载", "載入", "loading", "黑屏", "过场", "過場", "动画中", "通信中",
        ))

    def _remember_transition(self, pending: dict[str, Any], action: dict[str, Any],
                             frame: np.ndarray, frame_info: dict[str, Any]) -> None:
        after_state = self._memory_state(action)
        row = self._transition_memory.observe(
            scope=pending["scope"],
            before_state=pending["before_state"],
            before_fingerprint=pending["before_fingerprint"],
            control=pending["control"],
            action=pending["action"],
            after_state=after_state,
            after_fingerprint=self._frame_fingerprint(frame),
            changed=not bool(frame_info.get("same")),
            before_value=str(pending.get("before_option") or ""),
            after_value=str(action.get("observed_option") or ""),
        )
        if row:
            effect = str(((row.get("outcomes") or [{}])[-1]).get("effect") or "observed")
            self._emit(
                "log",
                f"已记录 UI 转移：{row.get('before_state') or 'unknown'} / "
                f"{pending['control'].get('label') or pending['action']} → "
                f"{after_state}（{effect}）",
            )

    def _begin_shadow_episode(self, *, step: int, scope: str, action: dict[str, Any],
                              control: dict[str, Any], before_state: str,
                              frame: np.ndarray) -> str:
        if self._visual_memory is None:
            return ""
        try:
            episode_id = self._visual_memory.begin_episode(
                run_id=self._run_id, step=step, scope=scope, task=self.task,
                action=action, control=control, before_scene=before_state,
                before_fingerprint=self._frame_fingerprint(frame), before_frame=frame,
            )
            self._pending_episode_id = episode_id
            return episode_id
        except Exception as exc:
            self._emit("log", f"影子记忆写入失败，执行不受影响：{type(exc).__name__}: {exc}")
            return ""

    def _resolve_shadow_episode(self, pending: dict[str, Any], action: dict[str, Any],
                                frame: np.ndarray, stable: bool) -> None:
        episode_id = str(pending.get("episode_id") or "")
        if not episode_id or self._visual_memory is None:
            return
        try:
            row = self._visual_memory.resolve_episode(
                episode_id, after_scene=self._memory_state(action),
                after_fingerprint=self._frame_fingerprint(frame), after_frame=frame,
                stable=bool(stable), measured_effect=pending.get("measured_effect"),
            )
            self._emit(
                "log",
                "影子记忆已记录："
                f"{row.get('before_scene') or 'unknown'} → {row.get('after_scene') or 'unknown'}"
                f"（{row.get('effect_result') or 'unknown'}；不参与当前决策）",
            )
        except Exception as exc:
            self._emit("log", f"影子记忆结算失败，执行不受影响：{type(exc).__name__}: {exc}")
        finally:
            if self._pending_episode_id == episode_id:
                self._pending_episode_id = ""

    def _censor_shadow_episode(self, reason: str) -> None:
        episode_id = self._pending_episode_id
        if not episode_id or self._visual_memory is None:
            return
        try:
            self._visual_memory.censor_episode(episode_id, reason)
        except Exception as exc:
            self._emit("log", f"影子记忆中止标记失败：{type(exc).__name__}: {exc}")
        finally:
            self._pending_episode_id = ""

    def _recommended_action_wait(self, pending: dict[str, Any]) -> float:
        if self._visual_memory is None:
            return 0.35
        try:
            return self._visual_memory.recommended_wait(
                scope=pending["scope"], before_scene=pending["before_state"],
                control=pending["control"], action_kind=pending["action"],
            )
        except Exception as exc:
            self._emit("log", f"动作等待记忆读取失败，使用短等待：{type(exc).__name__}: {exc}")
            return 0.35

    def _learn_action_wait(self, pending: dict[str, Any], *, usable: bool) -> None:
        if self._visual_memory is None or pending.get("wait_labeled"):
            return
        try:
            recommended = self._visual_memory.observe_wait(
                scope=pending["scope"], before_scene=pending["before_state"],
                control=pending["control"], action_kind=pending["action"],
                waited=float(pending.get("wait_seconds") or 0.35), usable=usable,
            )
            pending["wait_labeled"] = True
            self._emit(
                "log",
                f"动作等待已学习：本次 {float(pending.get('wait_seconds') or 0):.2f}s"
                f"（{'足够' if usable else '不足'}），下次建议 {recommended:.2f}s",
            )
        except Exception as exc:
            self._emit("log", f"动作等待学习失败，执行不受影响：{type(exc).__name__}: {exc}")

    @staticmethod
    def _memory_state(action: dict[str, Any]) -> str:
        scene_key = str(action.get("scene_key") or "").strip()
        if scene_key:
            return scene_key[:60]
        current = str(action.get("current_state") or "unknown").strip()
        return re.split(r"[，,；;。]", current, maxsplit=1)[0].strip()[:60] or "unknown"

    def _remember_visible_controls(self, action: dict[str, Any], meta: dict[str, Any]) -> None:
        controls = action.get("visible_controls")
        visible = [item for item in controls if isinstance(item, dict)] if isinstance(controls, list) else []
        if str(action.get("action") or "") == "click":
            clicked = self._transition_memory.control_signature(
                action, (int(meta["width"]), int(meta["height"]))
            )
            visible.append({
                "text": clicked.get("label"), "region": clicked.get("region"),
                "container": clicked.get("container"), "type": clicked.get("declared_type"),
            })
        self._transition_memory.observe_controls(
            scope=str(meta.get("application_scope") or meta.get("kind") or "unknown"),
            state=self._memory_state(action), controls=visible,
        )

    async def _verify_candidate(self, frame: np.ndarray, image: str,
                                claimed: dict[str, Any], history: list[dict[str, Any]]):
        self._emit("status", "正在验证任务结果")
        if self._decision_supervisor is not None:
            review = await self._decision_supervisor.review_completion(
                claimed=claimed, history=history, image=image,
                runtime_facts={
                    "observed_options": list(self._observed_options),
                    "selector_closed": self._closed,
                    "closure_evidence": self._closure_evidence,
                    "gui_shape": self._gui_shape,
                },
            )
            if review is not None and review.get("verdict") != "approve":
                reason = str(review.get("reason") or "决策层认为任务尚未完整完成")
                missing = [str(x) for x in review.get("missing") or []]
                requirements = [str(x) for x in review.get("next_requirements") or []]
                summary = reason + (("；缺少：" + "、".join(missing)) if missing else "")
                event = {
                    "success": False, "summary": summary,
                    "evidence": [], "uncertainty": missing,
                    "confidence": 0.0, "decision_verdict": review.get("verdict"),
                    "next_requirements": requirements,
                }
                self._emit("log", f"决策层未批准候选交付：{summary}")
                return None, event
        verdict = await self._verify(image, claimed)
        report_path = self._save_report(frame, verdict, history)
        event = {
            "success": bool(verdict.get("success")),
            "summary": str(verdict.get("summary") or claimed.get("message") or ""),
            "evidence": [str(x) for x in verdict.get("evidence") or []],
            "uncertainty": [str(x) for x in verdict.get("uncertainty") or []],
            "confidence": float(verdict.get("confidence") or 0),
            "report_path": report_path,
        }
        self._emit("evidence", event)
        if verdict.get("success"):
            return {
                "status": "finished", "message": event["summary"],
                "gui_shape": self._gui_shape or None, **event,
            }, event
        return None, event

    async def _execute(self, runtime, kind, action, meta):
        name = str(action.get("action") or "").lower()
        message = str(action.get("message") or name)
        if name == "click":
            x, y = float(action["x"]), float(action["y"])
            if not (0 <= x < meta["width"] and 0 <= y < meta["height"]):
                raise ValueError("模型点击坐标超出截图范围")
            page_x, page_y = x, y
            if kind == "browser":
                page_x = meta["origin"][0] + x * meta["scale_x"]
                page_y = meta["origin"][1] + y * meta["scale_y"]
            self._last_click = {"shot": (x, y), "page": (page_x, page_y), "kind": kind}
            self._emit(
                "log",
                f"点击落点：截图({x:.0f},{y:.0f}) → 页面({page_x:.0f},{page_y:.0f})｜"
                f"origin={tuple(meta.get('origin') or (0, 0))}｜"
                f"scale=({float(meta.get('scale_x') or 1):.3f},{float(meta.get('scale_y') or 1):.3f})｜"
                f"截图 {meta['width']}x{meta['height']}",
            )
            box = action.get("target_box")
            used_bbox = False
            if isinstance(box, list) and len(box) == 4:
                x1, y1, x2, y2 = (int(round(float(v))) for v in box)
                try:
                    result = await self._execution.click_bbox_local(
                        self.account, x1, y1, x2, y2,
                        screenshot_width=int(meta["width"]),
                        screenshot_height=int(meta["height"]),
                        target=kind,
                    )
                    used_bbox = bool(result.get("success"))
                    self._emit(
                        "log",
                        f"统一执行层 bbox 点击：{'成功' if used_bbox else '未命中'} · "
                        f"{result.get('result')}",
                    )
                except ExecutionToolError as exc:
                    self._emit("log", f"统一执行层 bbox 点击不可用，回退坐标点击：{exc}")
                except Exception as exc:
                    self._emit(
                        "log",
                        f"统一执行层 bbox 点击异常，回退坐标点击：{type(exc).__name__}: {exc}",
                    )
            if not used_bbox:
                await self._execution.invoke_local(
                    self.account, kind, "click",
                    x=int(round(page_x)), y=int(round(page_y)), pianyi=(0, 0),
                )
            if kind == "browser":
                await self._probe_hit(runtime, page_x, page_y)
        elif name == "scroll":
            delta = max(-1200, min(1200, int(action.get("delta_y") or 0)))
            kwargs = {"delta": delta} if kind == "window" else {"delta_y": delta}
            await self._execution.invoke_local(self.account, kind, "scroll", **kwargs)
        elif name == "key":
            await self._execution.invoke_local(
                self.account, kind,
                "key_click" if kind == "window" else "press_key",
                key=str(action.get("key") or "Escape"),
            )
        elif name == "type":
            await self._execution.invoke_local(
                self.account, kind, "type_text",
                text=str(action.get("text") or "")[:1000],
            )
        elif name == "wait":
            await asyncio.sleep(max(.2, min(float(action.get("seconds") or 1), 5.0)))
        else:
            raise ValueError(f"不支持的模型动作: {name}")
        return message

    async def _probe_hit(self, runtime, page_x: float, page_y: float) -> None:
        """Zero-token check of what sat under the cursor.

        "clicked but nothing happened" has two causes that need different fixes:
        the event never reached the page, or it reached the page somewhere else.
        elementFromPoint separates them; the annotated frame in _save_click_probe
        shows it.
        """
        try:
            browser = getattr(runtime, "_browser", runtime)
            page = getattr(browser, "page", None)
            if page is None:
                return
            hit = await page.evaluate(
                "([x, y]) => { const el = document.elementFromPoint(x, y);"
                " if (!el) return 'none';"
                " return el.tagName + (el.id ? '#' + el.id : '')"
                " + (el.className && typeof el.className === 'string'"
                " ? '.' + el.className.split(' ')[0] : ''); }",
                [float(page_x), float(page_y)],
            )
            self._emit("log", f"落点命中：{hit}")
        except Exception as exc:
            self._emit("log", f"落点探测失败：{type(exc).__name__}")

    def _save_click_probe(self, frame: np.ndarray) -> str:
        """Draw the click point onto the frame so "点空" is visible, not inferred."""
        point = (self._last_click or {}).get("shot")
        if not point:
            return ""
        try:
            out_dir = Path(self.report_root) / "_clicks"
            out_dir.mkdir(parents=True, exist_ok=True)
            canvas = frame.copy()
            x, y = int(point[0]), int(point[1])
            cv2.circle(canvas, (x, y), 20, (0, 0, 255), 2)
            cv2.drawMarker(canvas, (x, y), (0, 255, 255), cv2.MARKER_CROSS, 26, 2)
            path = out_dir / f"{datetime.now().strftime('%H%M%S-%f')}_miss.jpg"
            cv2.imwrite(str(path), canvas, [cv2.IMWRITE_JPEG_QUALITY, 85])
            self._emit("log", f"点空证据已保存：{path}")
            return str(path)
        except Exception:
            return ""

    def _save_report(self, frame: np.ndarray, verdict: dict[str, Any], history: list[dict[str, Any]]) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_dir = self.report_root / stamp
        run_dir.mkdir(parents=True, exist_ok=True)
        screenshot = run_dir / "final.jpg"
        cv2.imwrite(str(screenshot), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        report = {
            "task": self.task,
            "account": self.account,
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "success" if verdict.get("success") else "unverified",
            "summary": str(verdict.get("summary") or ""),
            "evidence": [str(x) for x in verdict.get("evidence") or []],
            "uncertainty": [str(x) for x in verdict.get("uncertainty") or []],
            "confidence": float(verdict.get("confidence") or 0),
            "final_screenshot": screenshot.name,
            "steps": history,
            "selector_readings": list(self._selector_readings),
            "gui_shape": {
                "shape": self._gui_shape or None,
                "basis": self._gui_shape_basis or None,
            },
            "ledger": {
                "observed_options": list(self._observed_options),
                "option_repeat_counts": dict(self._option_repeat_counts),
                "steps_since_new_option": self._steps_since_new_option,
                "nudges": self._ledger_nudges,
            },
            "usage": dict(self._usage),
        }
        report_path = run_dir / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(report_path)

    async def run(self):
        """Always return a structured result and preserve evidence on failures."""
        try:
            if self._decision_supervisor is not None:
                self._decision_supervisor.start()
                self._emit("log", "决策层已与执行层并行启动；执行层仍是唯一界面操作者")
            return await self._run_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc or '未提供错误详情'}"
            self._emit("log", f"视觉执行异常：{detail}")
            report_path = ""
            if self._last_frame is not None:
                verdict = {
                    "success": False,
                    "summary": f"执行异常：{detail}",
                    "evidence": ["已保存异常发生前的最后画面"],
                    "uncertainty": ["任务可能已经部分完成，需要人工查看最后截图"],
                    "confidence": 0.0,
                }
                report_path = self._save_report(self._last_frame, verdict, self._history)
                self._emit("evidence", {**verdict, "report_path": report_path})
            return {
                "status": "error", "message": f"执行异常：{detail}",
                "report_path": report_path,
                "last_state": self._history[-1] if self._history else {},
            }
        finally:
            if self._decision_supervisor is not None:
                await self._decision_supervisor.close()
            self._effect_tracker.close()
            self._censor_shadow_episode("runtime_ended_before_stable_observation")
            if self._visual_memory is not None:
                try:
                    self._visual_memory.close()
                except Exception:
                    pass

    async def _run_loop(self):
        runtime, kind = self._target()
        history: list[dict[str, Any]] = []
        self._history = history
        previous_frame: np.ndarray | None = None
        uploaded_frame: np.ndarray | None = None
        previous_action: dict[str, Any] | None = None
        pending_transition: dict[str, Any] | None = None
        forbidden_actions: set[str] = set()
        self._emit("status", "正在观察游戏画面")
        prohibited_labels = describe_prohibitions(self._contract.get("prohibited") or [])
        self._emit(
            "log",
            "执行约束：禁止 "
            + (prohibited_labels or "无额外项（仅通用安全规则）")
            + f" · 完成判据：{self._contract['completion_mode']}",
        )

        step = 0
        while self.max_steps is None or step < self.max_steps:
            step += 1
            if self._stopped:
                self._censor_shadow_episode("user_stopped")
                return {"status": "stopped", "message": "任务已停止"}
            previous_name = str((previous_action or {}).get("action") or "")
            frame, image, meta = await self._observe(runtime, kind)
            takeover_was_active = bool(
                self._decision_supervisor is not None
                and self._decision_supervisor.snapshot().get("conflict_takeover")
            )
            if self._pending_decision_conflict is not None and self._decision_supervisor is not None:
                conflict = self._pending_decision_conflict
                self._pending_decision_conflict = None
                evidence_dir = Path(self.report_root) / "_conflict_evidence" / self._run_id
                evidence_dir.mkdir(parents=True, exist_ok=True)
                evidence_path = evidence_dir / f"step_{step:04d}_fresh.jpg"
                evidence_bytes = base64.b64decode(image)
                evidence_path.write_bytes(evidence_bytes)
                evidence_ref = {
                    "captured_after_conflict": True,
                    "capture_step": step,
                    "path": str(evidence_path),
                    "byte_size": len(evidence_bytes),
                    "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                }
                self._emit("log", f"冲突后已重新取证：{evidence_path}", layer="decision")
                review = await self._decision_supervisor.review_conflict(
                    conflict=conflict,
                    history=history,
                    fresh_image=image,
                    evidence_ref=evidence_ref,
                )
                if review is not None:
                    status = str(review.get("previous_claim_status") or "uncertain")
                    assessment = "；".join(str(x) for x in review.get("evidence_assessment") or [])
                    history.append({
                        "step": step,
                        "action": "decision_conflict_self_check",
                        "result": status,
                        "message": str(review.get("reason") or ""),
                        "evidence": [str(evidence_path)] + ([assessment] if assessment else []),
                    })
                    self._emit(
                        "log",
                        f"决策层冲突自检：{status}"
                        + (f" · 新证据：{assessment}" if assessment else ""),
                        layer="decision",
                    )
            if (
                takeover_was_active
                and self._decision_supervisor is not None
                and previous_name in {"click", "key", "type", "scroll"}
            ):
                branch_dir = Path(self.report_root) / "_conflict_evidence" / self._run_id
                branch_dir.mkdir(parents=True, exist_ok=True)
                branch_path = branch_dir / f"step_{step:04d}_branch.jpg"
                branch_bytes = base64.b64decode(image)
                branch_path.write_bytes(branch_bytes)
                branch_review = await self._decision_supervisor.review_conflict_branch(
                    history=history,
                    fresh_image=image,
                    evidence_ref={
                        "captured_after_branch_action": True,
                        "capture_step": step,
                        "path": str(branch_path),
                        "byte_size": len(branch_bytes),
                        "sha256": hashlib.sha256(branch_bytes).hexdigest(),
                    },
                )
                if branch_review is not None:
                    branch_status = str(branch_review.get("status") or "unknown")
                    branch_seen = [str(x) for x in branch_review.get("evidence_assessment") or []]
                    history.append({
                        "step": step,
                        "action": "decision_conflict_branch_review",
                        "result": branch_status,
                        "message": str(branch_review.get("reason") or ""),
                        "evidence": [str(branch_path)] + branch_seen,
                    })
                    self._emit(
                        "log",
                        f"决策层冲突分支复核：{branch_status}"
                        + ("；临时接管结束" if branch_status == "resolved" else "；继续临时接管"),
                        layer="decision",
                    )
            # A regular action now performs one observation only.  Loading and
            # transient semantic scenes are handled below; expensive multi-frame
            # settling is reserved for final verification paths.
            stable_after_action = True
            self._last_frame = frame.copy()
            delta = analyze_frame(frame, previous_frame)
            upload_delta = analyze_frame(frame, uploaded_frame)
            frame_info: dict[str, Any] = dict(delta)
            frame_info["possibly_transitioning"] = bool(
                previous_frame is not None
                and previous_name in {"click", "key", "type", "scroll"}
                and float(delta.get("changed_ratio") or 0) >= 0.35
            )
            if previous_frame is not None and previous_name in {"click", "key", "type", "scroll"}:
                measured_effect = self._effect_tracker.evaluate(
                    before=previous_frame, after=frame, action=previous_action or {},
                    stable=stable_after_action,
                )
                frame_info["measured_effect"] = measured_effect
                frame_info["action_result"] = measured_effect["classification"]
                self._emit(
                    "log",
                    f"上一步实测：{measured_effect['classification']} · "
                    f"内容变化 {float((measured_effect.get('content_region') or {}).get('changed_ratio') or 0) * 100:.2f}% · "
                    f"连续无效果 {measured_effect['consecutive_no_effects']}",
                )
                self._last_effect_class = str(measured_effect["classification"])
                if self._last_effect_class in {"suspected_no_effect", "confirmed_no_effect"}:
                    # 画面没变 = 这一侧可能到底了；和「回到已见项」是两种收口方式。
                    self._stall_no_change += 1
                    if str((previous_action or {}).get("action") or "") == "click":
                        self._save_click_probe(frame)
                if (
                    self._last_effect_class == "confirmed_no_effect"
                    and self._decision_supervisor is not None
                ):
                    self._decision_supervisor.notify_anomaly(
                        anomaly={
                            "type": "confirmed_no_effect",
                            "previous_action": {
                                key: (previous_action or {}).get(key)
                                for key in ("action", "target", "message", "expect", "current_state")
                            },
                            "measurement": measured_effect,
                        },
                        history=history,
                    )
                if measured_effect["same_action_forbidden"]:
                    forbidden_actions.add(str(measured_effect["action_key"]))
                history_index = (previous_action or {}).get("_history_index")
                if isinstance(history_index, int) and 0 <= history_index < len(history):
                    history[history_index]["measured_effect"] = measured_effect
                    history[history_index]["result"] = measured_effect["classification"]
                if pending_transition is not None:
                    pending_transition["measured_effect"] = measured_effect
                    pending_transition["effect_stable"] = bool(stable_after_action)
            frame_info["previous_action"] = str((previous_action or {}).get("action") or "none")
            if previous_action and "measured_effect" not in frame_info:
                frame_info["action_result"] = (
                    "画面发生变化，动作可能已生效" if not delta["same"]
                    else "画面基本未变化，动作可能未生效或仍在等待"
                )
            self._emit(
                "log",
                f"第 {step} 步：画面变化 {delta['changed_ratio'] * 100:.2f}% · "
                f"黑屏比例 {delta['black_ratio'] * 100:.1f}%",
            )

            # Black screens are loading, not evidence of a hang: wait locally
            # (zero model calls) against this application's measured load time.
            app_scope = str(meta.get("application_scope") or meta.get("kind") or "unknown")
            if self._loading is None or self._loading_scope != app_scope:
                self._loading = LoadingWatch(self._load_profile, app_scope)
                self._loading_scope = app_scope
            loading = self._loading.observe(
                black_ratio=float(delta["black_ratio"]),
                changed=not bool(delta.get("same")),
            )
            frame_info["loading"] = {
                "phase": loading["phase"],
                "waited": loading["waited"],
                "grace": loading["grace"],
                "stalled": loading["stalled"],
                "activity": loading["activity"],
                "exhausted": loading["exhausted"],
                "history": loading["history"],
            }
            if loading["just_finished"] is not None:
                loaded_stats = loading["history"]
                self._emit(
                    "log",
                    f"加载结束：等待 {loading['just_finished']:.1f}s（{app_scope} 已记 "
                    f"{loaded_stats['samples']} 次，p50={loaded_stats['p50']}s "
                    f"p95={loaded_stats['p95']}s）",
                )
            if loading["phase"] == "loading" and not loading["exhausted"]:
                if loading["just_entered"] or loading["waits"] % 5 == 1:
                    self._emit(
                        "log",
                        f"加载中（{app_scope} 宽限 {loading['grace']:.0f}s）：已等 "
                        f"{loading['waited']:.1f}s，本地等待，不调用模型",
                    )
                await asyncio.sleep(loading["backoff"])
                previous_frame = frame
                previous_action = {
                    "action": "wait", "message": "本地等待加载", "expect": "出现可交互画面",
                }
                continue
            if loading["announce_exhausted"]:
                self._emit(
                    "log",
                    f"加载已静止 {loading['stalled']:.0f}s，超过该站点宽限 "
                    f"{loading['grace']:.0f}s；改由模型判断继续等待还是点击重试",
                )

            # Executor calls are stateless.  A text-only turn cannot see our
            # cached frame and therefore has to guess grounding coordinates.
            # Wait locally while the frame is unchanged, then force-upload.
            if uploaded_frame is not None and upload_delta["same"]:
                waited_same = 0.0
                self._emit(
                    "log",
                    f"画面与最近上传帧基本相同；本地短暂等待，累计最多 "
                    f"{self._same_frame_timeout:.1f}s，不调用模型",
                )
                while waited_same < self._same_frame_timeout and not self._stopped:
                    delay = min(self._same_frame_poll, self._same_frame_timeout - waited_same)
                    if delay <= 0:
                        break
                    await asyncio.sleep(delay)
                    waited_same += delay
                    fresh_frame, fresh_image, fresh_meta = await self._observe(runtime, kind)
                    fresh_delta = analyze_frame(fresh_frame, uploaded_frame)
                    frame, image, meta = fresh_frame, fresh_image, fresh_meta
                    self._last_frame = frame.copy()
                    upload_delta = fresh_delta
                    if not fresh_delta["same"]:
                        self._emit(
                            "log",
                            f"本地等待 {waited_same:.2f}s 后画面发生变化，立即上传新截图",
                        )
                        break
                if upload_delta["same"]:
                    self._emit(
                        "log",
                        f"同画面累计等待达到 {waited_same:.2f}s；强制上传当前截图给模型",
                    )
                frame_info.update(upload_delta)
                frame_info["same_frame_local_wait"] = {
                    "waited": round(waited_same, 3),
                    "timeout": self._same_frame_timeout,
                    "changed": not bool(upload_delta["same"]),
                }

            decision_image = image
            uploaded_frame = frame.copy()
            self._emit("log", "本轮上传完整截图给模型")

            action = await self._decide(decision_image, meta, step, history, frame_info)
            if pending_transition is not None:
                # 点击后的加载页、黑屏和过场只是中间态。保留 pending，直到模型
                # 识别出稳定场景；否则会把“点击某入口 → 加载页”错误学成最终语义。
                if not self._is_transient_state(action):
                    self._learn_action_wait(pending_transition, usable=True)
                    self._remember_transition(pending_transition, action, frame, frame_info)
                    self._resolve_shadow_episode(
                        pending_transition, action, frame,
                        bool(pending_transition.get("effect_stable", stable_after_action)),
                    )
                    pending_transition = None
                else:
                    self._learn_action_wait(pending_transition, usable=False)
            name = str(action.get("action") or "").lower()
            message = str(action.get("message") or name)
            task_status = str(action.get("task_status") or "continue")
            self._remember_visible_controls(action, meta)
            if name in {"click", "key", "type", "scroll"}:
                proposed_key = self._effect_tracker.action_key(
                    action, (int(meta["width"]), int(meta["height"]))
                )
                if proposed_key in forbidden_actions:
                    reason = (
                        "运行时已确认该场景中的同一动作连续两次稳定无变化，"
                        "已拒绝再次执行；请重新观察并选择不同的安全动作"
                    )
                    history.append({
                        "step": step, "action": "repeat_guard", "result": "rejected",
                        "message": reason, "action_key": proposed_key,
                    })
                    if self._decision_supervisor is not None:
                        self._decision_supervisor.notify_anomaly(
                            anomaly={
                                "type": "runtime_action_rejected",
                                "reason": reason,
                                "action_key": proposed_key,
                                "proposed_action": action,
                            },
                            history=history,
                        )
                    self._emit("reply", reason)
                    previous_frame = frame
                    uploaded_frame = frame.copy()
                    previous_action = {"action": "verify", "message": reason}
                    continue
            self._sync_ledger_scene(action)
            self._note_selector_reading(
                action,
                moved=name in {"click", "key", "scroll"},
                direction=self._click_direction(action, meta),
            )
            conflict_takeover = bool(
                self._decision_supervisor is not None
                and self._decision_supervisor.snapshot().get("conflict_takeover")
            )
            ledger_gap = "" if conflict_takeover else self._ledger_gap(action)
            if ledger_gap:
                self._ledger_nudges += 1
                self._ledger_resolution_context = {
                    "conflict": ledger_gap,
                    "runtime_closure": self._closure_evidence or None,
                    "observed_options": list(self._observed_options),
                    "pager_total": None if self._pager_total_untrusted else (self._pager_total or None),
                    "selector_conflicts": list(self._selector_conflicts[-3:]),
                    "choices": {
                        "accept_closure": "闭环证据可复核且比较依据足够时，停止导航并选中目标项",
                        "discriminating_probe": "候选数仍冲突时，只做一次会产生不同可见结果的安全探测",
                        "stop_unverified": "没有可判别的可见证据时以未验证状态收束",
                    },
                }
                history.append({
                    "step": step, "action": "ledger_guard", "result": "rejected",
                    "message": ledger_gap,
                    "observed_options": list(self._observed_options),
                    "steps_without_new_option": self._steps_since_new_option,
                })
                self._emit("log", f"枚举账本提醒：{ledger_gap}")
                if self._decision_supervisor is not None:
                    self._pending_decision_conflict = {
                        "type": "semantic_progress_conflict",
                        "reason": ledger_gap,
                        "decision_or_proposed_claim": action,
                        "runtime_claim": {
                            "selector_closed": self._closed,
                            "closure_evidence": self._closure_evidence,
                            "observed_options": list(self._observed_options),
                        },
                        "evidence_rule": "不得使用本次冲突之前的截图证明任一方；下一轮必须重新取证",
                        "created_at_step": step,
                    }
                    self._emit("log", "检测到结论冲突；下一轮将重新抓取现场证据后要求决策层自检", layer="decision")
                self._emit("reply", ledger_gap)
                previous_frame = frame
                uploaded_frame = frame.copy()
                previous_action = {"action": "verify", "message": ledger_gap}
                continue
            self._ledger_resolution_context = None
            # ask/blocked 的最终 needs_user 结果会由界面统一显示；这里再发 reply
            # 会让同一句询问在聊天区出现两次。
            if name != "ask" and task_status != "blocked":
                self._emit("reply", message)

            if task_status == "blocked" and name != "ask":
                return {"status": "needs_user", "message": message or "执行模型判断需要用户确认"}
            prohibition = self._prohibition_guard(action)
            if prohibition:
                reason = (
                    f"触发禁止行为（{prohibition['label']}）："
                    f"控件或输入含“{prohibition['hit']}”"
                )
                history.append({
                    "step": step, "action": "prohibition_guard", "result": "blocked",
                    "message": reason, "category": prohibition["category"],
                    "hit": prohibition["hit"], "field": prohibition["field"],
                })
                self._emit("log", f"禁止行为已拦截：{reason}")
                return {
                    "status": "needs_user",
                    "message": reason + "。该动作不会被执行；需要你本人确认后手动完成。",
                }
            last_effect = str((frame_info.get("measured_effect") or {}).get("classification") or "")
            if (
                (last_effect == "unsettled_after_action" or frame_info.get("possibly_transitioning"))
                and str(action.get("action") or "") in {"click", "key"}
                and looks_like_retreat(action)
            ):
                # A frame that is still transitioning cannot prove "this is the
                # wrong page": the model is usually looking at an animation of
                # the page it just asked for, and backing out throws that away.
                reason = (
                    "上一步画面仍在过渡，此时撤回会丢掉已经推进的进度；"
                    "先等待画面稳定或重新观察再决定"
                )
                history.append({
                    "step": step, "action": "retreat_guard", "result": "rejected",
                    "message": reason,
                })
                self._emit("reply", reason + "。")
                previous_frame = frame
                uploaded_frame = frame.copy()
                previous_action = {"action": "wait", "message": reason, "expect": "画面稳定"}
                await asyncio.sleep(0.8)
                continue
            candidate = task_status == "candidate_complete"
            selector_gap = (
                self._selector_gap(action) if (candidate or name == "finish") else ""
            )
            if selector_gap:
                history.append({
                    "step": step, "action": "selector_gap_guard", "result": "rejected",
                    "message": selector_gap,
                    "pager_total": self._pager_total,
                    "observed_options": list(self._observed_options),
                })
                self._emit("log", f"交付校验未通过：{selector_gap}")
                self._emit("reply", selector_gap + "。")
                previous_frame = frame
                uploaded_frame = frame.copy()
                previous_action = {"action": "verify", "message": selector_gap}
                continue
            if name == "finish":
                # 硬完成闸门通过后，才允许截取新鲜终帧交给独立验证器。
                final_frame, final_image, _ = await self._observe(runtime, kind)
                self._last_frame = final_frame.copy()
                result, evidence_event = await self._verify_candidate(final_frame, final_image, action, history)
                if result:
                    return result
                history.append({
                    "action": "verify", "result": "failed",
                    "message": evidence_event["summary"],
                    "evidence": evidence_event["evidence"],
                })
                self._emit("reply", f"完成验证未通过：{evidence_event['summary']}，继续执行。")
                previous_frame = final_frame
                uploaded_frame = final_frame.copy()
                previous_action = {"action": "verify", "message": evidence_event["summary"]}
                self._emit("status", "验证未通过，继续执行")
                continue
            if candidate:
                reason = "执行模型判断当前画面可能已经满足任务目标"
                self._emit("log", f"动作前刹车：{reason}；暂停输入并进行完成验证")
                claimed = {
                    "message": reason,
                    "evidence": [str(action.get("current_state") or message)],
                }
                result, evidence_event = await self._verify_candidate(frame, image, claimed, history)
                if result:
                    return result
                history.append({
                    "action": "candidate_verify", "result": "unverified",
                    "message": reason, "evidence": evidence_event["evidence"],
                })
                previous_frame = frame
                uploaded_frame = frame.copy()
                previous_action = {"action": "verify", "message": evidence_event["summary"]}
                self._emit("reply", f"完成验证未通过：{evidence_event['summary']}，继续观察但不执行刚才的动作。")
                self._emit("status", "验证未通过，继续观察")
                continue

            if name == "ask":
                return {"status": "needs_user", "message": message}

            record = {
                "step": step, "action": name, "message": message,
                "expect": str(action.get("expect") or "未提供"),
                "result": "pending" if name in {"click", "key", "type", "scroll"} else "completed",
            }
            history.append(record)
            self._emit("log", f"第 {step} 步：{name} · {message} · 预期：{record['expect']}")
            previous_frame = frame
            previous_action = action
            previous_action["_history_index"] = len(history) - 1
            if name in {"click", "key", "type", "scroll"}:
                control = self._transition_memory.control_signature(
                    action, (int(meta["width"]), int(meta["height"]))
                )
                pending_transition = {
                    "scope": str(meta.get("application_scope") or meta.get("kind") or "unknown"),
                    "before_state": self._memory_state(action),
                    "before_fingerprint": self._frame_fingerprint(frame),
                    "control": control,
                    "action": name,
                    "before_option": str(action.get("observed_option") or "").strip(),
                    "option_direction": str(action.get("option_direction") or "").strip().lower(),
                    "intent": " ".join(str(action.get(key) or "") for key in (
                        "target", "message", "expect"
                    )),
                }
                pending_transition["episode_id"] = self._begin_shadow_episode(
                    step=step, scope=pending_transition["scope"], action=action,
                    control=control, before_state=pending_transition["before_state"],
                    frame=frame,
                )
                pending_transition["wait_seconds"] = self._recommended_action_wait(
                    pending_transition
                )
            await self._execute(runtime, kind, action, meta)
            await asyncio.sleep(
                float((pending_transition or {}).get("wait_seconds") or 0.35)
                if name in {"click", "key", "type", "scroll"} else 0.2
            )

        self._censor_shadow_episode("diagnostic_step_limit")
        return {"status": "limit", "message": f"已达到诊断步数上限 {self.max_steps}"}
