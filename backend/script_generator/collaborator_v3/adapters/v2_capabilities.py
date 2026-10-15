"""Compatibility bridge for the existing V2 tool catalogue.

Importing V2 is delayed until sync is explicitly requested.  V3 never treats
the legacy catalogue as state; it takes a versioned schema snapshot instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from ..domain.contracts import CapabilityRecord
from ..ports.executor import ExecutionRequest, ExecutionResult
from ..stores.sqlite_store import SqliteDomainStore

#: Phase B4（设计 §4.8）：工具执行阶段的稳定失败码。
TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
#: Phase B4（设计 §4.8）：结果校验阶段的稳定失败码。
TOOL_RESULT_INVALID = "TOOL_RESULT_INVALID"

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def validate_tool_output(
    output: Mapping[str, Any] | None, schema: Mapping[str, Any] | None
) -> tuple[bool, list[str], str]:
    """按声明的结果契约校验工具输出（设计 §4.8 第四阶段）。

    返回 ``(是否合格, 不合格字段, 说明)``。只做**声明式、保守**的检查：
    必填字段存在且非空、声明了类型的字段类型对得上；没声明的宽松放行。
    """
    spec = dict(schema or {})
    if not spec or spec.get("additionalProperties") is True:
        return True, [], ""
    data = dict(output or {})
    invalid: list[str] = []
    expected_type = str(spec.get("type") or "")
    if expected_type and expected_type in _TYPE_CHECKS:
        if not isinstance(data, _TYPE_CHECKS[expected_type]):
            return False, ["<root>"], f"结果不是 {expected_type}"
    properties = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
    for field in spec.get("required") or ():
        name = str(field)
        if name not in data or data.get(name) is None:
            invalid.append(name)
            continue
        declared = str((properties.get(name) or {}).get("type") or "")
        checks = _TYPE_CHECKS.get(declared)
        if checks and not isinstance(data.get(name), checks):
            invalid.append(name)
    if invalid:
        return False, invalid, f"结果缺字段或类型不符：{sorted(set(invalid))}"
    return True, [], ""


_EXTERNAL_SIDE_EFFECT_TOOLS = {
    "message_click_probe",
    "retarget_click_window",
}
_MODIFY_TOOLS = {
    "commit_repeated_regions",
    "show_asset_in_viewport",
    "supersede_resource",
    "delete_custom_tool",
}
_CREATE_TOOLS = {"crop_asset", "mark_on_image", "define_tool", "run_probe_script"}


def _permission_actions(name: str) -> tuple[str, ...]:
    if name in _EXTERNAL_SIDE_EFFECT_TOOLS:
        return ("external_side_effect",)
    if name in _MODIFY_TOOLS:
        return ("modify",)
    if name in _CREATE_TOOLS:
        return ("create_reversible",)
    return ()


def _version_for(name: str, parameters: dict[str, Any], *, result: dict | None = None,
                 completion: dict | None = None) -> str:
    raw = json.dumps(
        {"name": name, "parameters": parameters, "result": result or {}, "completion": completion or {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def capability_from_openai_tool(tool: dict[str, Any]) -> CapabilityRecord:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    name = str(fn.get("name") or "").strip()
    parameters = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
    #: Phase B4（设计 §4.8）：结果契约（``x-result``）跟函数声明走。没声明就保持宽松，
    #: **声明了就必须满足** —— 否则执行边界报 TOOL_RESULT_INVALID，结果不进证据、进不了 answer。
    result_contract = fn.get("x-result") if isinstance(fn.get("x-result"), dict) else {}
    search_terms = tuple(
        str(value).strip()
        for value in (fn.get("x-search-terms") or ())
        if str(value).strip()
    )
    if not name:
        raise ValueError("legacy tool has no function name")
    from backend.script_generator.resource_binding import (
        RESOURCE_SCHEMA_KEY,
        declared_resource_params,
        legacy_inferred_resource_params,
        resource_id_param_schema,
    )

    resource_params = declared_resource_params(parameters)
    legacy_inferred = tuple(
        name for name in legacy_inferred_resource_params(parameters) if name not in resource_params
    )
    #: Phase B4（设计 §4.5/§4.6）：每个资源参数的业务语义（材料化目标 / 角色 / 用途），
    #: 直接来自工具 schema 自己的 ``x-resource`` 声明 —— 不用第二份表。
    properties = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    semantics: dict[str, dict[str, Any]] = {}
    for param in (*resource_params, *legacy_inferred):
        blob = (properties.get(str(param)) or {}).get(RESOURCE_SCHEMA_KEY)
        if isinstance(blob, dict):
            semantics[str(param)] = {
                str(key): value
                for key, value in blob.items()
                if key
                in {
                    "materialize_as",
                    "role",
                    "roles",
                    "use",
                    "semantic",
                    "semantic_tags",
                    "base_types",
                    "version_required",
                }
            }
    return CapabilityRecord(
        id=f"cap_v2_{name}",
        kind="tool",
        name=name,
        version=_version_for(name, parameters, result=result_contract,
                             completion=dict(fn.get("x-completion") or {})),
        input_schema=parameters,
        output_schema=result_contract or {"type": "object", "additionalProperties": True},
        permission_actions=_permission_actions(name),
        cost_class="local" if "vision" not in name and "describe" not in name else "medium",
        availability="available",
        scope="global",
        metadata={
            "adapter": "v2_tool_resolver",
            "description": str(fn.get("description") or ""),
            "legacy_name": name,
            # Phase B（设计 §4）「资源参数 schema」：
            # - `resource_params`：**权威**声明（schema 里的 x-resource）；
            # - `legacy_inferred_params`：旧工具没声明、只靠参数名命中历史命名的那部分，
            #   Runtime 会兼容绑定并记 RESOURCE_SCHEMA_LEGACY_INFERRED 遥测。
            "resource_params": list(resource_params),
            "legacy_inferred_params": list(legacy_inferred),
            "resource_param_schemas": {
                str(param): resource_id_param_schema()
                for param in (*resource_params, *legacy_inferred)
            },
            "resource_param_semantics": semantics,
            "search_terms": list(search_terms),
            "catalog_source": "v2_tool_resolver",
            "completion": dict(fn.get("x-completion") or {}),
        },
    )


def sync_v2_capabilities(store: SqliteDomainStore) -> list[CapabilityRecord]:
    from backend.script_generator.collaborator.llm_turn import all_collab_tools

    capabilities: list[CapabilityRecord] = []
    for tool in all_collab_tools():
        try:
            capability = capability_from_openai_tool(tool)
        except (TypeError, ValueError):
            continue
        store.put_capability(capability)
        capabilities.append(capability)
    store.reconcile_capability_catalog(
        id_prefix="cap_v2_",
        current={(item.id, item.version) for item in capabilities},
    )
    return capabilities


class LegacyResolverExecutor:
    """Execute an already-authorized V2 resolver behind the V3 Executor port.

    Phase B4（设计 §4.3–§4.8）：这里是**唯一执行边界**。模型给的资源表达不可信，
    工具也不许自己再解析一遍 —— 所以调用顺序固定为

    ``模型表达 → ResourceResolver → Binder（已在 runner 完成）→ handle → 真实实参 → 工具``。

    - 参数里的 ``[[asset_x]]`` / ``resource://…`` / ``__resources__\\…`` 一律在**工具启动前**
      换成 handle 派生出的真实路径；换不掉就返回
      ``RESOURCE_ARGUMENT_NOT_MATERIALIZED``（``stage=argument_materialization``、
      ``tool_started=false``），绝不把占位表达丢给旧工具去猜；
    - 失败不再是裸 ``TOOL_FAILURE``：一律带 ``stage`` / ``tool_started`` / ``retryable``
      与 ``failed_parameter``（有的话）。
    """

    def __init__(
        self,
        resolver: Callable[[str, dict[str, Any]], str | dict[str, Any]],
        *,
        on_success: Callable[[ExecutionRequest, dict[str, Any]], None] | None = None,
    ) -> None:
        self.resolver = resolver
        self.on_success = on_success
        self.requests: list[ExecutionRequest] = []

    @staticmethod
    def _materialization_failure(
        request: ExecutionRequest, unmaterialized: list[dict[str, Any]]
    ) -> ExecutionResult:
        from backend.script_generator.resource_binding import (
            RESOURCE_ARGUMENT_NOT_MATERIALIZED,
            STAGE_ARGUMENT_MATERIALIZATION,
        )

        first = dict(unmaterialized[0])
        parameter = str(first.get("parameter") or "")
        return ExecutionResult(
            handle_id=f"legacy_{request.action_id}",
            status="failed",
            error_code=RESOURCE_ARGUMENT_NOT_MATERIALIZED,
            recoverable=True,
            output={
                "status": "failed",
                "code": RESOURCE_ARGUMENT_NOT_MATERIALIZED,
                "stage": STAGE_ARGUMENT_MATERIALIZATION,
                "tool_started": False,
                "retryable": True,
                "failed_parameter": parameter,
                "unmaterialized": unmaterialized[:4],
                "message": (
                    f"资源已绑定，但参数 {parameter or '?'} 仍是资源占位表达"
                    f"（{first.get('reason') or 'unmaterialized'}）"
                ),
                "suggested_action": "用绑定后的真实路径重新调用（或改传资源 id 让 Runtime 绑定）",
            },
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        from backend.script_generator.resource_binding import (
            STAGE_TOOL_EXECUTION,
            STAGE_TOOL_RESULT_VALIDATION,
            materialize_resource_arguments,
        )

        self.requests.append(request)
        legacy_name = request.metadata.get("legacy_name") or request.capability_id.removeprefix("cap_v2_")
        # Phase B4（设计 §4.5）：把绑定的 handle 变成工具真正消费得到的实参。
        arguments, materialized, unmaterialized = materialize_resource_arguments(
            request.arguments,
            handles=request.bound_resources,
            semantics=request.metadata.get("resource_param_semantics") or {},
        )
        if unmaterialized:
            return self._materialization_failure(request, unmaterialized)
        # Phase B（设计 §4.2）：绑定事实仍一并交给工具（``_bound_resources``），
        # 但工具**不依赖**它 —— 真实路径已经在参数里了。
        if request.bound_resources:
            arguments["_bound_resources"] = {
                str(getattr(item, "resource_id", "") or ""): (
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                )
                for item in request.bound_resources
            }
        try:
            raw = self.resolver(str(legacy_name), arguments)
            data = json.loads(raw) if isinstance(raw, str) else raw
            data = data if isinstance(data, dict) else {"result": data}
        except Exception as exc:
            return ExecutionResult(
                handle_id=f"legacy_{request.action_id}",
                status="failed",
                error_code=TOOL_EXECUTION_FAILED,
                recoverable=True,
                output={
                    "status": "failed",
                    "code": TOOL_EXECUTION_FAILED,
                    "stage": STAGE_TOOL_EXECUTION,
                    "tool_started": True,
                    "retryable": True,
                    "error": str(exc),
                    "message": f"工具执行抛异常：{exc}",
                },
            )
        ok = data.get("ok", True) is not False
        if materialized:
            data.setdefault("_materialized", materialized)
        if ok:
            # Phase B4（设计 §4.8）**结果校验**：声明了 x-result 的工具，结果必须满足它。
            # 不合格 = 没有可信证据 → 不进证据、也就进不了 answer。
            valid, invalid_fields, message = validate_tool_output(
                data, request.output_schema or {}
            )
            if not valid:
                return ExecutionResult(
                    handle_id=f"legacy_{request.action_id}",
                    status="failed",
                    error_code=TOOL_RESULT_INVALID,
                    recoverable=True,
                    output={
                        "status": "failed",
                        "code": TOOL_RESULT_INVALID,
                        "stage": STAGE_TOOL_RESULT_VALIDATION,
                        "tool_started": True,
                        "retryable": True,
                        "invalid_fields": invalid_fields,
                        "message": message,
                        "suggested_action": "换参数/换工具重新取证；不要把未经校验的结果当结论",
                    },
                )
            if self.on_success is not None:
                self.on_success(request, data)
            return ExecutionResult(
                handle_id=f"legacy_{request.action_id}",
                status="succeeded",
                output=data,
            )
        code = str(data.get("error_code") or TOOL_EXECUTION_FAILED)
        return ExecutionResult(
            handle_id=f"legacy_{request.action_id}",
            status="failed",
            output={
                **data,
                "status": "failed",
                "code": code,
                "stage": STAGE_TOOL_EXECUTION,
                "tool_started": True,
                "retryable": bool(data.get("retryable", True)),
                "message": str(data.get("error") or data.get("message") or "工具执行失败"),
            },
            error_code=code,
            recoverable=True,
        )

    def cancel(self, handle_id: str) -> bool:
        # The current V2 resolver is synchronous and cannot be interrupted.
        return False
