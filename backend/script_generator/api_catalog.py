"""旧 API 卡片入口 —— 兼容 facade（设计 §9 阶段 A、§29）。

⚠️ **事实源已经不在本文件**：API 契约单一事实源是
``backend/script_generator/api_contracts``（registry + render）。
本模块只保留旧函数名与返回形状，内部全部转发，供既有的生成/修订/优化 prompt
继续使用；Phase 7 清理重复事实源时再评估删除。

历史遗留：这里的卡片曾与 ``agent.ALLOWED_BROWSER_METHODS``、
``graph.plan_schema._ALLOWED_BROWSER_METHODS`` 三份名单互相矛盾
（如 ``invalidate_frame`` 在此被判"不允许"、在别处允许）。现在统一取自 registry。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .api_contracts import registry, render
from .api_contracts import ticket_service
from .execution_profile import profile_from_target

#: ⚠️ 模块级 ``RUNTIME_PROFILE`` 已**删除**。
#: 旧值是硬编码的 ``browser_user_browser``，与交付闸那边硬编码的 desktop 方向相反，
#: 且都不看任务真正的执行目标（决定 §13.1：清除两个相反的默认）。
#: 现在旧调用方必须显式传 ``profile=``；无法判定时由 ``render`` 落到
#: ``portable_common`` 安全交集，对单端能力返回结构化 ``PROFILE_REQUIRED``。
_LEGACY_PROFILE = registry.PORTABLE_PROFILE


def _legacy_spec():
    """旧模块常量用的 profile：安全交集（不是某个具体执行目标）。"""
    return registry.get_profile(_LEGACY_PROFILE)


#: 旧元组形状：**安全交集**里已批准的方法名。
#: 不再等于 browser 的方法表 —— 需要 browser 专有能力的调用方必须显式传 profile。
ALLOWED_METHODS: tuple[str, ...] = _legacy_spec().approved_names()

#: 旧集合形状：需要 allow_login 才放行的方法名。
LOGIN_METHODS: frozenset[str] = frozenset(_legacy_spec().login_methods)

#: 旧 ``_CARDS`` 形状：方法名 → 卡片正文（由 registry 渲染）。
_CARDS: dict[str, str] = {
    item.name: render.card_text(_LEGACY_PROFILE, item)
    for item in _legacy_spec().approved_contracts(allow_login=True)
}


def allow_login_from_explanation(explanation: str) -> bool:
    return render.allow_login_from_explanation(explanation)


def lookup_api(
    name: str,
    *,
    allow_login: bool = False,
    profile: str | None = None,
    execution_target: Mapping[str, Any] | None = None,
    service=None,
) -> str:
    """旧接口。``profile`` 缺失 → 只按 ``portable_common`` 回答，
    单端/签名分歧的方法返回 ``PROFILE_REQUIRED``（禁止静默选 desktop/browser）。

    ``execution_target`` 是**权威执行目标事实**（决定 1，形状见
    ``execution_profile.resolve_execution_target``）：给了就由它决定 profile、
    来源与目标版本；没给就走 portable 语义（**绝不**默认 desktop/browser）。

    另：这次"查看契约"由**本地** ticket 服务记录（``CONTRACT_VIEWED`` + 尽量签发 ticket）。
    V2 与 V3 走同一个 ``TicketService``（用户决定 9）；留痕失败**不**影响返回值
    （Phase 1 不接交付硬闸）。
    """
    text = render.lookup_text(profile, name, allow_login=allow_login)
    record_contract_view(
        name, profile=profile, service=service, execution_target=execution_target
    )
    return text


def record_contract_view(
    name: str,
    *,
    profile: str | None = None,
    service=None,
    execution_target: Mapping[str, Any] | None = None,
    main_turn_id: str = "",
    subturn_id: str = "",
    run_id: str = "",
    action_id: str = "",
    task_id: str = "",
):
    """把"模型查看过这个契约"写进本地时间线，并在能确定接收者时签发 ticket。

    返回 ``ContractViewResult``；没有登记服务、或记录过程出错时返回 ``None``
    （**故意**不抛：留痕是加固，不是闸门）。
    """
    target = service or ticket_service.default_service()
    if target is None:
        return None
    source_profile, profile_source, target_revision = profile_from_target(execution_target)
    if source_profile is None and profile:
        # 调用方直接给了具体 profile（老调用形状）：只认它，依旧不猜
        source_profile, profile_source = profile, ""
    try:
        return target.view_contracts(
            names=(name,),
            profile=source_profile,
            profile_source=profile_source,
            main_turn_id=main_turn_id,
            subturn_id=subturn_id,
            run_id=run_id,
            action_id=action_id,
            task_id=task_id,
            environment_revision=target_revision,
        )
    except Exception:  # noqa: BLE001 - 见 docstring：留痕失败不得打断生成
        return None


def compact_catalog(*, allow_login: bool = False, profile: str | None = None) -> str:
    return render.compact_catalog(profile, allow_login=allow_login)


def api_return_types_banner() -> str:
    """**已废弃**：旧 profile 无关对照条已删除（它会教 desktop 用 match_image_multi）。

    保留这个名字只为兼容旧调用方，返回空串；能力/返回语义由
    ``api_contracts_block()`` 按 profile 渲染。
    """
    return ""


def api_contracts_block(
    *,
    explanation: str = "",
    allow_login: bool | None = None,
    profile: str | None = None,
) -> str:
    """生成/修订 prompt 共用的 API 契约全文。"""
    return render.contracts_block(profile, explanation=explanation, allow_login=allow_login)


def generate_tool_hint(
    *, allow_login: bool, supports_tools: bool, profile: str | None = None
) -> str:
    """始终附带完整 API 卡片；支持 tools 时额外允许 lookup_api 复查。"""
    block = api_contracts_block(allow_login=allow_login, profile=profile)
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
