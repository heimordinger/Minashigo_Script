"""Small, state-specific forms used by the V4 MVP."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any


class FormError(ValueError):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("；".join(item.get("meaning", "表单错误") for item in errors))
        self.errors = errors


@dataclass(frozen=True)
class FormSpec:
    screen: str
    purpose: str
    template: dict[str, Any]
    semantics: dict[str, Any]

    def payload(self, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "screen": self.screen,
            "purpose": self.purpose,
            "context": dict(context or {}),
            "field_semantics": self.semantics,
            "form": json.loads(json.dumps(self.template, ensure_ascii=False)),
            "output_rule": "只返回填写后的 form 对象本身；不要输出 Markdown、解释或额外字段。",
        }


def requirement_form() -> FormSpec:
    return FormSpec(
        "requirement_analysis",
        "理解用户目标；不要执行动作。",
        {
            "goal": "",
            "deliverable": "",
            "previous_requirements_relationship": "none",
            "user_explicit_preference": "unspecified",
            "active_requirements": [],
            "superseded_requirement_ids": [],
            "known": [],
            "unknown": [],
            "success_criteria": [],
            "asset_bindings": [],
            "unresolved_asset_references": [],
            "material_ambiguity": "",
            "needs_user_confirmation": False,
            "confirmation_question": "",
            "confirmation_choices": [],
        },
        {
            "goal": "用户真正想完成的结果，一句话。",
            "deliverable": "最终应交付什么；纯问答可写‘文字答复’。",
            "previous_requirements_relationship": (
                "新需求与 context.previous_unfinished_requirements 的关系，只能填 "
                "none、compatible、conflict、unrelated、uncertain。没有旧需求时填 none；"
                "可并行完成的补充或无关事项都应合并执行，只有要求互斥时填 conflict。"
            ),
            "user_explicit_preference": (
                "用户是否明确指定处理范围，只能填 unspecified、old_only、new_only、both；"
                "明确指定高于关系判断。"
            ),
            "active_requirements": (
                "本主轮最终要完成的需求列表。默认纳入全部未完成旧需求和新需求；"
                "冲突时保留新需求；用户明确指定时严格按指定范围。"
            ),
            "superseded_requirement_ids": (
                "被新需求覆盖、或被用户明确放弃的旧需求 id。不要把暂未执行但仍有效的需求放进来。"
            ),
            "known": "仅列用户原话或 Runtime 已确认的信息。",
            "unknown": "完成任务仍缺少、可查询或可询问的信息。",
            "success_criteria": "怎样判断结果满足需求。",
            "asset_bindings": (
                "把 context.asset_reference_candidates 中与任务有关的素材绑定到语义角色；"
                "每项填写 asset_id、role、reason。候选只是提示，不可虚构不存在的 id。"
            ),
            "unresolved_asset_references": (
                "用户确实提到了素材、但目录和候选仍无法定位时才填写；不要把已有候选列为未解决。"
            ),
            "material_ambiguity": "只有不同答案会改变交付物时才填写，否则留空。",
            "needs_user_confirmation": "只有无法可靠判断新旧需求应如何合并时才为 true。",
            "confirmation_question": "需要确认时，用一句话指出具体冲突；否则留空。",
            "confirmation_choices": "需要确认时给出 2~3 个互斥选项；否则为空数组。",
        },
    )


_MENU = {
    "self_continue": {
        "meaning": "本地不执行动作；你整理现有信息或使用模型自身能力继续判断。",
        "use_when": "现有信息已足够推进，但还不适合提交最终回复。",
        "do_not_use_when": "需要本地素材、工具、脚本或可验证结果。",
    },
    "query_local": {
        "meaning": "只读查询当前会话素材/消息，或系统能力目录和契约。",
        "use_when": "缺少当前会话信息，或需要寻找/了解本地工具。",
        "do_not_use_when": "相同查询已有有效结果，或准备执行已取得契约的能力。",
    },
    "invoke_capability": {
        "meaning": "调用已经通过 capability.get_contract 取得完整参数表的本地工具。",
        "use_when": "工作区已有该能力完整契约，且工具能推进当前目标。",
        "do_not_use_when": "只知道工具名称/摘要，或参数仍不明确。",
    },
    "temporary_script": {
        "meaning": "现有模型能力与本地工具不足时，编写一次性 Python 脚本。",
        "use_when": "能力搜索已完成，仍没有能完成该步骤的工具。",
        "do_not_use_when": "已有工具可完成，或只是缺少工具参数。",
    },
    "load_trial_script": {
        "meaning": "把已经完成的脚本逻辑转换为当前 Runner 可执行的脚本，并交付到试运行页面等待用户测试。",
        "use_when": "用户说交付脚本、加载到试运行、让我去测试，或脚本修订后需要再次交给用户验证。",
        "do_not_use_when": "只是在探针环境验证算法、登记内部工具，或用户只需要文字回答。",
    },
    "ask_user": {
        "meaning": "向用户询问会实质改变交付结果的缺失信息。",
        "use_when": "本地查询和模型自身能力都不能消除关键歧义。",
        "do_not_use_when": "系统仍能自行查询或执行。",
    },
    "submit_response": {
        "meaning": "提交最终用户可见回复候选并结束本主轮。",
        "use_when": "需求已满足，或已能如实说明无法完成的具体原因。",
        "do_not_use_when": "仍有可执行且必要的步骤。",
    },
}


def menu_form(*, allowed: list[str] | None = None) -> FormSpec:
    choices = [name for name in (allowed or list(_MENU)) if name in _MENU]
    return FormSpec(
        "next_action",
        "从 Runtime 当前允许的菜单中选择恰好一个下一步。",
        {"action": "", "reason": ""},
        {
            "action": {"allowed": choices, "options": {name: _MENU[name] for name in choices}},
            "reason": "一句话说明这个选择如何推进当前目标；不是事实或执行结果。",
        },
    )


def query_form() -> FormSpec:
    return FormSpec(
        "query_local",
        "填写一次只读查询。用户数据范围仅限当前会话。",
        {"query": "", "arguments": {}},
        {
            "query": {
                "allowed": [
                    "session.assets",
                    "session.messages",
                    "capability.list_categories",
                    "capability.list_by_category",
                    "capability.search",
                    "capability.get",
                    "capability.get_contract",
                    "capability.get_source",
                ]
            },
            "arguments": {
                "session.assets": "可选 query/role；只返回当前会话素材。",
                "session.messages": "可选 limit，默认 12。",
                "capability.list_categories": (
                    "无需参数；首次寻找本地能力时先调用。多步骤任务先查看 skills_workflow，"
                    "若有匹配 skill 则读取正文，再按其流程进入具体工具类别。"
                ),
                "capability.list_by_category": "填写 category_id；返回该类工具的输入、输出和实际作用摘要。",
                "capability.search": "跨类别兜底搜索；必须先看过类别菜单。填写 purpose，可选 limit。",
                "capability.get": "填写已发现的 capability_id；skill 用它读取完整 SKILL.md 正文。",
                "capability.get_contract": "填写已发现的工具 capability_id；skill 不可调用。",
                "capability.get_source": "填写 capability_id，可选 max_lines。",
            },
        },
    )


def invoke_form(contract: dict[str, Any]) -> FormSpec:
    return FormSpec(
        "invoke_capability",
        "严格按已取得的完整契约填写一次本地工具调用。",
        {"capability_id": contract.get("capability_id", ""), "arguments": {}},
        {
            "capability_id": "必须与当前契约完全一致。",
            "arguments": contract.get("input_schema") or {},
            "actual_effect": contract.get("actual_effect", "执行本地工具并返回结构化结果"),
            "output": contract.get("output_summary", "工具定义的结构化结果"),
        },
    )


def temporary_script_form() -> FormSpec:
    return FormSpec(
        "temporary_script",
        "编写一次性 Python 脚本。参数从 ARGS 读取，结论赋给 result。",
        {"purpose": "", "parameters": {}, "code": ""},
        {
            "purpose": "脚本补足的具体能力缺口。",
            "parameters": "传给脚本的 JSON 对象；资源优先使用当前会话素材名或 id。",
            "code": "Python 3.11 源码；从 ARGS 取参，最终给 result 赋可 JSON 序列化值。",
        },
    )


def trial_source_form(*, tool_names: list[str]) -> FormSpec:
    return FormSpec(
        "trial_source",
        "选择要转换并交付到试运行页面的真实代码来源。",
        {"source_type": "", "source_name": "", "runtime_behavior": "", "script_name": ""},
        {
            "source_type": "当前只允许 reusable_tool。",
            "source_name": {"allowed": list(tool_names), "meaning": "必须选择实际存在的自造工具。"},
            "runtime_behavior": "说明脚本运行一次时应读取什么、输出什么、何时结束。",
            "script_name": "试运行脚本文件名，必须以 .py 结尾。",
        },
    )


def trial_script_form(
    *,
    source: dict[str, Any],
    source_request: dict[str, Any],
    runner_api_contracts: list[str] | None = None,
) -> FormSpec:
    return FormSpec(
        "trial_script",
        "把探针/自造工具源码改写成可由现有试运行 Runner 调用的完整 Python 脚本。",
        {"script": "", "handoff_message": ""},
        {
            "script": (
                "完整 Python 3.11 源码，入口统一使用 "
                "async def do_work(browser: UserBrowser | UserWindow)。文件开头必须原样包含：\n"
                "from backend.browser.user_browser import UserBrowser\n"
                "from backend.automation.user_window import UserWindow\n"
                "该联合类型是现有 Runner 交付闸的固定模板。"
                "source_tool.execution_scope=probe_only，源码只用于理解业务意图，禁止逐行复制。"
                "探针里的 cv2/np/read_image 必须改写成下方 runner_api_contracts 所列的 browser API；"
                "不可依赖探针专有全局量 ARGS、read_image、resolve_image、result；"
                "用 frame = await browser.update_frame() 读取用户当前切换到的绑定目标画面；"
                "模板素材必须使用 requested_runtime_behavior.asset_bindings/available_assets 中真实存在的 "
                "asset_id，占位路径写成 r\"[[asset_id]]\"，由本地交付桥打包替换；"
                "执行一次目标逻辑，print 结构化结果后返回，不要点击游戏。"
            ),
            "handoff_message": "加载成功后直接告诉用户的简短中文说明；提示其切换游戏界面后点击试运行。",
            "source_tool": source,
            "requested_runtime_behavior": source_request,
            "runner_api_contracts": list(runner_api_contracts or []),
        },
    )


def self_continue_form() -> FormSpec:
    return FormSpec(
        "self_continue",
        "本地本步不执行。整理现有信息，写出下一步所需的短工作结论。",
        {"working_result": "", "remaining_gap": ""},
        {
            "working_result": "只写从现有信息可得出的结论，不编造本地事实。",
            "remaining_gap": "若仍缺信息则写明；没有则留空。",
        },
    )


def ask_form() -> FormSpec:
    return FormSpec(
        "ask_user",
        "填写一条必须由用户回答的问题。",
        {"question": "", "choices": []},
        {
            "question": "只问一个会实质改变结果的问题。",
            "choices": "可选，最多 3 个短选项；无法枚举时留空。",
        },
    )


def submit_form() -> FormSpec:
    return FormSpec(
        "submit_response",
        "提交最终用户可见回复候选。",
        {"text": "", "completed": False, "basis": []},
        {
            "text": "直接给用户看的中文 Markdown。",
            "completed": "需求已满足填 true；如实报告阻塞或未知填 false。",
            "basis": "支撑回复的 Runtime 结果摘要；没有就留空，不得编造引用。",
        },
    )


def supplement_review_form() -> FormSpec:
    return FormSpec(
        "supplement_review",
        "审查当前副轮结束后收到的用户补充；先决定补充如何影响尚未执行的动作。",
        {
            "decision": "",
            "updated_goal": "",
            "updated_deliverable": "",
            "updated_success_criteria": [],
            "question": "",
            "choices": [],
            "reason": "",
        },
        {
            "decision": {
                "allowed": ["keep", "modify", "replace_requirement", "ask_user"],
                "meaning": {
                    "keep": "补充不妨碍冻结动作；吸收补充后继续执行。",
                    "modify": "冻结动作已不合适；废弃它并回到下一步菜单。",
                    "replace_requirement": "目标、交付物或成功条件改变；废弃冻结动作并重做需求分析。",
                    "ask_user": "新旧要求实质冲突且无法可靠消歧；暂停并询问用户。",
                },
            },
            "updated_goal": "补充改变目标时填写，否则留空；原始请求不会被覆盖。",
            "updated_deliverable": "补充改变交付物时填写，否则留空。",
            "updated_success_criteria": "补充新增或替换成功条件时填写，否则为空数组。",
            "question": "decision=ask_user 时填写一个必须由用户回答的问题，否则留空。",
            "choices": "可选，最多 3 项；decision 不是 ask_user 时留空。",
            "reason": "一句话说明补充为何保持、修改或替换当前动作。",
        },
    )


def _error(path: str, code: str, meaning: str) -> dict[str, str]:
    return {"path": path, "code": code, "meaning": meaning}


def validate_form(spec: FormSpec, value: Any, *, contracts: dict[str, dict] | None = None) -> dict:
    if not isinstance(value, dict):
        raise FormError([_error("$", "OBJECT_REQUIRED", "必须返回一个 JSON 对象")])
    expected = set(spec.template)
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    errors = [_error(key, "UNKNOWN_FIELD", f"不允许字段 {key}") for key in unknown]
    errors += [_error(key, "REQUIRED_FIELD_MISSING", f"缺少字段 {key}") for key in missing]
    if errors:
        raise FormError(errors)
    if spec.screen == "requirement_analysis":
        if not str(value.get("goal") or "").strip():
            errors.append(_error("goal", "EMPTY", "goal 不能为空"))
        if not str(value.get("deliverable") or "").strip():
            errors.append(_error("deliverable", "EMPTY", "deliverable 不能为空"))
        relationships = {"none", "compatible", "conflict", "unrelated", "uncertain"}
        if value.get("previous_requirements_relationship") not in relationships:
            errors.append(_error("previous_requirements_relationship", "ENUM", f"必须是 {sorted(relationships)} 之一"))
        preferences = {"unspecified", "old_only", "new_only", "both"}
        if value.get("user_explicit_preference") not in preferences:
            errors.append(_error("user_explicit_preference", "ENUM", f"必须是 {sorted(preferences)} 之一"))
        for name in (
            "active_requirements",
            "superseded_requirement_ids",
            "known",
            "unknown",
            "success_criteria",
            "asset_bindings",
            "unresolved_asset_references",
            "confirmation_choices",
        ):
            if not isinstance(value.get(name), list):
                errors.append(_error(name, "ARRAY_REQUIRED", f"{name} 必须是数组"))
        if not isinstance(value.get("needs_user_confirmation"), bool):
            errors.append(_error("needs_user_confirmation", "BOOLEAN_REQUIRED", "必须是布尔值"))
        if value.get("needs_user_confirmation"):
            if not str(value.get("confirmation_question") or "").strip():
                errors.append(_error("confirmation_question", "EMPTY", "需要确认时必须填写问题"))
            if not 2 <= len(value.get("confirmation_choices") or []) <= 3:
                errors.append(_error("confirmation_choices", "CHOICES_REQUIRED", "需要确认时必须提供 2~3 个选项"))
        for index, binding in enumerate(value.get("asset_bindings") or []):
            if not isinstance(binding, dict):
                errors.append(_error(f"asset_bindings.{index}", "OBJECT_REQUIRED", "素材绑定必须是对象"))
                continue
            if not str(binding.get("asset_id") or "").strip() or not str(binding.get("role") or "").strip():
                errors.append(_error(f"asset_bindings.{index}", "BINDING_REQUIRED", "素材绑定必须包含 asset_id 和 role"))
    elif spec.screen == "next_action":
        allowed = list(spec.semantics["action"]["allowed"])
        if value.get("action") not in allowed:
            errors.append(_error("action", "ENUM", f"action 必须是 {allowed} 之一"))
    elif spec.screen == "trial_source":
        allowed_names = set(spec.semantics["source_name"]["allowed"])
        if value.get("source_type") != "reusable_tool":
            errors.append(_error("source_type", "ENUM", "source_type 必须是 reusable_tool"))
        if value.get("source_name") not in allowed_names:
            errors.append(_error("source_name", "ENUM", "source_name 必须是实际存在的自造工具"))
        if not str(value.get("runtime_behavior") or "").strip():
            errors.append(_error("runtime_behavior", "EMPTY", "必须说明试运行行为"))
        if not str(value.get("script_name") or "").strip().lower().endswith(".py"):
            errors.append(_error("script_name", "PYTHON_FILE_REQUIRED", "script_name 必须以 .py 结尾"))
    elif spec.screen == "trial_script":
        script = str(value.get("script") or "")
        if not script.strip():
            errors.append(_error("script", "EMPTY", "script 不能为空"))
        else:
            tree = None
            try:
                tree = ast.parse(script, "<trial_script>", "exec")
            except SyntaxError as exc:
                errors.append(_error("script", "INVALID_PYTHON", f"Python 语法错误：{exc}"))
            entry = next(
                (
                    node for node in (tree.body if tree is not None else [])
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "do_work"
                ),
                None,
            )
            if entry is None or not entry.args.args:
                errors.append(_error("script", "RUNNER_ENTRY_REQUIRED", "必须定义带执行目标参数的 async def do_work(...)"))
            elif entry.args.args[0].annotation is None:
                errors.append(
                    _error(
                        "script",
                        "RUNNER_TARGET_TYPE_REQUIRED",
                        "do_work 第一个参数必须使用 UserBrowser | UserWindow 联合类型",
                    )
                )
            else:
                annotation = entry.args.args[0].annotation
                if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                    try:
                        annotation = ast.parse(annotation.value, mode="eval").body
                    except SyntaxError:
                        annotation = None
                names = {
                    node.id for node in ast.walk(annotation)
                    if isinstance(node, ast.Name)
                } if annotation is not None else set()
                if not {"UserBrowser", "UserWindow"}.issubset(names):
                    errors.append(
                        _error(
                            "script",
                            "RUNNER_TARGET_UNION_REQUIRED",
                            "入口必须统一为 async def do_work(browser: UserBrowser | UserWindow)",
                        )
                    )
            required_imports = {
                ("backend.browser.user_browser", "UserBrowser"),
                ("backend.automation.user_window", "UserWindow"),
            }
            actual_imports = {
                (node.module, alias.name)
                for node in (tree.body if tree is not None else [])
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            missing_imports = sorted(required_imports - actual_imports)
            if missing_imports:
                expected = (
                    "from backend.browser.user_browser import UserBrowser；"
                    "from backend.automation.user_window import UserWindow"
                )
                errors.append(
                    _error(
                        "script",
                        "RUNNER_TARGET_IMPORTS_REQUIRED",
                        f"必须使用 Runner 的固定类型导入：{expected}",
                    )
                )
            forbidden_modules = sorted(
                {
                    alias.name.split(".", 1)[0]
                    for node in (tree.body if tree is not None else [])
                    if isinstance(node, ast.Import)
                    for alias in node.names
                    if alias.name.split(".", 1)[0] in {"cv2", "numpy"}
                }
                | {
                    str(node.module or "").split(".", 1)[0]
                    for node in (tree.body if tree is not None else [])
                    if isinstance(node, ast.ImportFrom)
                    and str(node.module or "").split(".", 1)[0] in {"cv2", "numpy"}
                }
            )
            if forbidden_modules:
                errors.append(
                    _error(
                        "script",
                        "PROBE_MODULE_FORBIDDEN",
                        "试运行 Runner 不开放 {}；多目标图像匹配必须改用 "
                        "await browser.match_image_multi(...)".format(forbidden_modules),
                    )
                )
            forbidden = [name for name in ("ARGS", "read_image", "resolve_image") if re.search(rf"\b{name}\b", script)]
            if forbidden:
                errors.append(_error("script", "PROBE_GLOBAL_FORBIDDEN", f"试运行脚本不能依赖探针全局量：{forbidden}"))
        if not str(value.get("handoff_message") or "").strip():
            errors.append(_error("handoff_message", "EMPTY", "必须填写交接说明"))
    elif spec.screen == "query_local":
        allowed = list(spec.semantics["query"]["allowed"])
        if value.get("query") not in allowed:
            errors.append(_error("query", "ENUM", f"query 必须是 {allowed} 之一"))
        if not isinstance(value.get("arguments"), dict):
            errors.append(_error("arguments", "OBJECT_REQUIRED", "arguments 必须是对象"))
    elif spec.screen == "invoke_capability":
        capability_id = str(value.get("capability_id") or "")
        contract = (contracts or {}).get(capability_id)
        if contract is None:
            errors.append(_error("capability_id", "CONTRACT_REQUIRED", "当前主轮没有这个能力的完整契约"))
        elif not isinstance(value.get("arguments"), dict):
            errors.append(_error("arguments", "OBJECT_REQUIRED", "arguments 必须是对象"))
        else:
            schema = contract.get("input_schema") or {}
            props = schema.get("properties") or {}
            args = value["arguments"]
            for name in schema.get("required") or []:
                if name not in args:
                    errors.append(_error(f"arguments.{name}", "REQUIRED_FIELD_MISSING", f"缺少必填参数 {name}"))
            for name in args:
                if props and name not in props:
                    errors.append(_error(f"arguments.{name}", "UNKNOWN_ARGUMENT", f"契约中没有参数 {name}"))
    elif spec.screen == "temporary_script":
        if not str(value.get("purpose") or "").strip():
            errors.append(_error("purpose", "EMPTY", "purpose 不能为空"))
        if not isinstance(value.get("parameters"), dict):
            errors.append(_error("parameters", "OBJECT_REQUIRED", "parameters 必须是对象"))
        if not str(value.get("code") or "").strip():
            errors.append(_error("code", "EMPTY", "code 不能为空"))
    elif spec.screen == "ask_user":
        if not str(value.get("question") or "").strip():
            errors.append(_error("question", "EMPTY", "question 不能为空"))
        if not isinstance(value.get("choices"), list) or len(value.get("choices") or []) > 3:
            errors.append(_error("choices", "ARRAY_MAX_3", "choices 必须是最多 3 项的数组"))
    elif spec.screen == "submit_response":
        if not str(value.get("text") or "").strip():
            errors.append(_error("text", "EMPTY", "text 不能为空"))
        if not isinstance(value.get("completed"), bool):
            errors.append(_error("completed", "BOOLEAN_REQUIRED", "completed 必须是布尔值"))
        if not isinstance(value.get("basis"), list):
            errors.append(_error("basis", "ARRAY_REQUIRED", "basis 必须是数组"))
    elif spec.screen == "supplement_review":
        allowed = list(spec.semantics["decision"]["allowed"])
        if value.get("decision") not in allowed:
            errors.append(_error("decision", "ENUM", f"decision 必须是 {allowed} 之一"))
        if not isinstance(value.get("updated_success_criteria"), list):
            errors.append(_error("updated_success_criteria", "ARRAY_REQUIRED", "updated_success_criteria 必须是数组"))
        if not isinstance(value.get("choices"), list) or len(value.get("choices") or []) > 3:
            errors.append(_error("choices", "ARRAY_MAX_3", "choices 必须是最多 3 项的数组"))
        if value.get("decision") == "ask_user" and not str(value.get("question") or "").strip():
            errors.append(_error("question", "EMPTY", "decision=ask_user 时 question 不能为空"))
    if errors:
        raise FormError(errors)
    return value
