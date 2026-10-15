"""Strict callable adapter for existing provider clients."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ..domain.enums import ActionType
from ..domain.errors import DomainError, ErrorCode
from ..domain.models import ActionEnvelope
from ..ports.model import ModelReply, ModelRequest


#: Orient 允许的键（设计 §16）。**只有这些** —— 任何自由思维链字段都会被拒，
#: 这是"不保存也不展示推理独白"的机械保证。
#:
#: ``orientation`` 已从模型协议移出。这里只为旧客户端保留解析兼容；
#: Runtime 不再将其当作任务类型事实，也不因缺失而退回重交。
ORIENTATION_KEYS: frozenset[str] = frozenset(
    {
        "task_type",
        "intent_quote",
        "interpreted_goal",
        "requested_changes",
        "must_preserve",
        "known_facts",
        "unknowns",
        "material_ambiguities",
        "can_act_now",
        "next_action_reason",
    }
)

#: 列表型字段（元素必须是字符串）。
_ORIENTATION_LIST_KEYS = (
    "requested_changes",
    "must_preserve",
    "known_facts",
    "unknowns",
    "material_ambiguities",
)


def parse_orientation(value: Any) -> dict[str, Any]:
    """校验并归一化旧协议 ``orientation``（兼容读取，不再必填）。

    设计 §16：Runtime 只校验**必要字段与一致性**，不保存自由形式思维链。
    因此这里做两件事：拒绝未知键（挡掉 CoT 夹带）、把列表字段统一成字符串列表。
    缺失时返回空字典；任务定向由 Runtime 本地完成。
    """
    if value in (None, {}, ""):
        return {}
    if not isinstance(value, dict):
        raise DomainError(
            ErrorCode.MODEL_PROTOCOL_ERROR, "orientation must be an object"
        )
    unknown = sorted(set(value) - ORIENTATION_KEYS)
    if unknown:
        raise DomainError(
            ErrorCode.MODEL_PROTOCOL_ERROR,
            "orientation 只能包含约定字段（不要夹带推理独白）",
            {"unknown": unknown, "allowed": sorted(ORIENTATION_KEYS)},
        )
    out: dict[str, Any] = {}
    goal = value.get("interpreted_goal")
    if goal is not None:
        out["interpreted_goal"] = str(goal)
    # 任务定向（设计 §3）：标量字段，空串按"没给"处理。
    for key in ("task_type", "intent_quote"):
        if key not in value:
            continue
        text = str(value.get(key) or "").strip()
        if text:
            out[key] = text
    for key in _ORIENTATION_LIST_KEYS:
        if key not in value:
            continue
        raw = value.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raise DomainError(
                ErrorCode.MODEL_PROTOCOL_ERROR, f"orientation.{key} must be a list"
            )
        out[key] = [str(x) for x in raw if str(x).strip()]
    if "can_act_now" in value:
        out["can_act_now"] = bool(value.get("can_act_now"))
    if "next_action_reason" in value:
        out["next_action_reason"] = str(value.get("next_action_reason") or "")
    return out


def parse_action_envelope(raw: str | dict[str, Any]) -> ActionEnvelope:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise DomainError(ErrorCode.MODEL_PROTOCOL_ERROR, "model response is not JSON") from exc
    if not isinstance(data, dict):
        raise DomainError(ErrorCode.MODEL_PROTOCOL_ERROR, "model response must be one object")
    common = {
        "protocol_version", "action_id", "type", "payload",
        "expected_task_revision", "expected_viewport_revision",
        # 设计 §16：同一次响应里先给结构化 orientation，再给一个 action；
        # **不额外增加一次模型调用**。
        "orientation",
    }
    action_name = str(data.get("type") or "")
    flat_by_type: dict[str, frozenset[str]] = {
        "query": frozenset({"name", "arguments", "queries"}),
        "act": frozenset({
            "capability_id", "capability_version", "arguments", "input_resources",
            "permission_grant_ids", "environment_fingerprint", "target_fingerprint",
            "idempotency_key",
        }),
        "update_plan": frozenset({"task_draft", "plan", "requirement_contract"}),
        "propose_claim": frozenset({
            "key", "subject_class", "value", "proposer", "resource_id",
            "evidence_refs", "verification_plan", "missing_evidence",
        }),
        "request_interaction": frozenset({
            "reason_code", "question", "reason", "options", "allow_free_text",
            "blocking", "policy_context", "logic_draft",
        }),
        "validate_script": frozenset({"script", "script_name", "change_justification"}),
        "finish": frozenset({
            "text", "outcome", "task_patch", "completion", "final_response",
            "idempotency_key",
        }),
    }
    semantic_fields = frozenset({"goal_contract", "requirement_contract", "step_intent"})
    allowed = common | set(flat_by_type.get(action_name, frozenset())) | semantic_fields
    if set(data) - allowed:
        raise DomainError(
            ErrorCode.MODEL_PROTOCOL_ERROR,
            "model response contains text or unknown top-level fields",
            {"unknown": sorted(set(data) - allowed)},
        )
    try:
        payload = dict(data.get("payload") or {})
        for key in flat_by_type.get(action_name, frozenset()) | semantic_fields:
            if key not in data:
                continue
            if key == "text":
                final_response = dict(payload.get("final_response") or {})
                if "text" in final_response and final_response["text"] != data[key]:
                    raise DomainError(
                        ErrorCode.MODEL_PROTOCOL_ERROR,
                        "conflicting flattened field and payload field",
                        {"field": "final_response.text"},
                    )
                final_response["text"] = data[key]
                payload["final_response"] = final_response
                continue
            if key in payload and payload[key] != data[key]:
                raise DomainError(
                    ErrorCode.MODEL_PROTOCOL_ERROR,
                    "conflicting flattened field and payload field",
                    {"field": key},
                )
            payload[key] = data[key]
        return ActionEnvelope(
            protocol_version=int(data.get("protocol_version", 1)),
            action_id=str(data.get("action_id") or f"a_{uuid4().hex}"),
            type=ActionType(str(data["type"])),
            payload=payload,
            expected_task_revision=data.get("expected_task_revision"),
            expected_viewport_revision=data.get("expected_viewport_revision"),
            orientation=parse_orientation(data.get("orientation")),
        )
    except DomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError(ErrorCode.MODEL_PROTOCOL_ERROR, "invalid action envelope") from exc


class CallableLogicModel:
    def __init__(
        self,
        invoke_fn: Callable[[ModelRequest], str | dict[str, Any]],
        *,
        model_id: str,
        model_version: str,
        available_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._invoke_fn = invoke_fn
        self._model_id = model_id
        self._model_version = model_version
        self._available_fn = available_fn or (lambda: True)

    @property
    def available(self) -> bool:
        return bool(self._available_fn())

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_version(self) -> str:
        return self._model_version

    def invoke(self, request: ModelRequest) -> ModelReply:
        if not self.available:
            raise DomainError(ErrorCode.MODEL_UNAVAILABLE, "logic model unavailable", recoverable=True)
        action = parse_action_envelope(self._invoke_fn(request))
        return ModelReply(action=action, model_id=self.model_id, model_version=self.model_version)
