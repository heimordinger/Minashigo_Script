"""V4 MVP main-turn loop: Runtime shows one form, the model fills it."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .asset_refs import compact_asset_index, resolve_asset_references
from .protocol import (
    FormError,
    FormSpec,
    ask_form,
    invoke_form,
    menu_form,
    query_form,
    requirement_form,
    self_continue_form,
    submit_form,
    supplement_review_form,
    temporary_script_form,
    trial_script_form,
    trial_source_form,
    validate_form,
)
from .query import SessionQueryGateway, contract_for
from .trace_log import V4TraceLog


MAX_SUBTURNS = 15  # 降低副轮上限，配合计划模式
MAX_FORM_REPAIRS = 3
MAX_NO_PROGRESS = 5
MAX_CONTEXT_TOKENS = 800_000  # 主轮 token 预算硬限制（80万）
MAX_RESULT_SIZE_CHARS = 2000  # 单个结果最大字符数


def _json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Normalize legacy tool results before they enter V4's JSON protocol."""
    if _depth > 20:
        return "<maximum serialization depth reached>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_safe(value.value, _depth=_depth + 1)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, _depth=_depth + 1) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _json_safe(to_dict(), _depth=_depth + 1)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value), _depth=_depth + 1)
    return str(value)


def _system_prompt() -> str:
    path = Path(__file__).with_name("prompts") / "system_prompt_v1.md"
    text = path.read_text(encoding="utf-8")
    marker = "---"
    return text.split(marker, 1)[-1].strip() if marker in text else text.strip()


def _extract_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise FormError([{"path": "$", "code": "INVALID_JSON", "meaning": "输出不是可解析的 JSON 对象"}])
        try:
            value = json.loads(text[start : end + 1])
        except Exception as exc:
            raise FormError([{"path": "$", "code": "INVALID_JSON", "meaning": f"JSON 解析失败：{exc}"}]) from exc
    if not isinstance(value, dict):
        raise FormError([{"path": "$", "code": "OBJECT_REQUIRED", "meaning": "必须返回 JSON 对象"}])
    return value


@dataclass
class TurnState:
    turn_id: str
    user_text: str
    messages: list[dict[str, str]] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    subturns: int = 0
    repairs: int = 0
    no_progress: int = 0
    last_fingerprint: str = ""
    asset_index: list[dict[str, Any]] = field(default_factory=list)
    asset_candidates: list[dict[str, Any]] = field(default_factory=list)
    trace_log: V4TraceLog | None = None
    supplements: list[dict[str, Any]] = field(default_factory=list)
    pending_supplements: list[dict[str, Any]] = field(default_factory=list)
    pending_action: dict[str, Any] = field(default_factory=dict)
    requirement_replacements: int = 0
    previous_requirements: list[dict[str, Any]] = field(default_factory=list)

    def context(self) -> dict[str, Any]:
        # 轻量化 recent_runtime_results：只保留最近 3 个，并截断大结果
        lightweight_results = []
        for result in self.results[-3:]:  # 只保留最近 3 个副轮的结果
            if not isinstance(result, dict):
                lightweight_results.append(result)
                continue

            # 检查结果大小
            result_json = json.dumps(result, ensure_ascii=False)
            if len(result_json) > MAX_RESULT_SIZE_CHARS:
                # 大结果：只保留摘要
                truncated = {
                    "type": result.get("type"),
                    "_truncated": True,
                    "_original_size": len(result_json),
                    "summary": f"结果过大（{len(result_json)} 字符），已截断。如需完整内容，使用 query_local 查询。",
                }
                # 保留关键字段
                if result.get("type") == "query_result":
                    request = result.get("request", {})
                    truncated["request"] = {
                        "query": request.get("query"),
                        "arguments": request.get("arguments", {}),
                    }
                    query_result = result.get("result", {})
                    truncated["result"] = {
                        "ok": query_result.get("ok"),
                        "query": query_result.get("query"),
                        "returned": query_result.get("returned", 0),
                        # 不包含完整的 items 列表
                    }
                elif result.get("type") == "capability_result":
                    truncated["capability_name"] = result.get("capability_name")
                    truncated["arguments"] = result.get("arguments", {})
                    cap_result = result.get("result", {})
                    truncated["result"] = {
                        "ok": cap_result.get("ok"),
                        "error": cap_result.get("error"),
                        # 不包含完整的视觉识别结果
                    }
                lightweight_results.append(truncated)
            else:
                lightweight_results.append(result)

        context = {
            "main_turn_id": self.turn_id,
            "user_request": self.user_text,
            "goal": self.analysis.get("goal", ""),
            "deliverable": self.analysis.get("deliverable", ""),
            "success_criteria": self.analysis.get("success_criteria", []),
            "available_assets": self.asset_index,
            "asset_reference_candidates": self.asset_candidates,
            "previous_unfinished_requirements": self.previous_requirements,
            "requirement_merge_policy": (
                "优先遵从用户本轮明确指定的处理范围；否则新旧需求不冲突时全部纳入，"
                "冲突时以新需求覆盖旧需求；只有无法可靠判断时才请求用户确认。"
            ),
            "asset_policy": (
                "available_assets 只是轻量目录。先绑定候选，再按需查询或检查素材内容；"
                "目录或高置信候选已存在时，不要要求用户重新提供同一素材。"
            ),
            "recent_runtime_results": _json_safe(lightweight_results),
            "known_contracts": [
                {"capability_id": key, "name": value.get("name"), "version": value.get("version")}
                for key, value in self.contracts.items()
            ],
            "supplements": _json_safe(self.supplements[-8:]),
            "budget": {"used": self.subturns, "limit": MAX_SUBTURNS},
        }
        if self.pending_supplements:
            context["pending_supplements"] = _json_safe(self.pending_supplements)
            context["pending_action"] = _json_safe(self.pending_action)
        return context


def _capability_result_name(state: TurnState, item: dict[str, Any]) -> str:
    name = str(item.get("capability_name") or "").strip()
    if name:
        return name
    contract = state.contracts.get(str(item.get("capability_id") or "")) or {}
    return str(contract.get("name") or "").strip()


def _inspected_asset_ids(state: TurnState, *, verify_only: bool = False) -> set[str]:
    inspected: set[str] = set()
    for item in state.results:
        if not isinstance(item, dict) or item.get("type") != "capability_result":
            continue
        if _capability_result_name(state, item) != "inspect_asset":
            continue
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        if verify_only and str(args.get("mode") or "").strip().lower() != "verify":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if not result.get("ok"):
            continue
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        asset_id = str(evidence.get("resource_id") or args.get("asset_id") or "").strip()
        if asset_id:
            inspected.add(asset_id)
    return inspected


def _match_candidates_for(
    state: TurnState, *, source_asset_id: str, reference_asset_ids: set[str]
) -> list[dict[str, Any]]:
    """Return usable local-match candidates tied to this exact source/reference pair."""
    for item in reversed(state.results):
        if not isinstance(item, dict) or item.get("type") != "capability_result":
            continue
        if _capability_result_name(state, item) != "match_template_multi":
            continue
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        template_id = str(result.get("template_asset_id") or result.get("asset_id") or args.get("asset_id") or "")
        target_id = str(result.get("target_asset_id") or args.get("target_asset_id") or "")
        if template_id not in reference_asset_ids or target_id != source_asset_id:
            continue
        if not result.get("ok") or result.get("ambiguous") or result.get("saturated"):
            continue
        return [match for match in (result.get("matches") or []) if isinstance(match, dict)]
    return []


def _mark_center(mark: dict[str, Any]) -> tuple[float, float] | None:
    rect = mark.get("rect")
    if isinstance(rect, list) and len(rect) == 4:
        try:
            return ((float(rect[0]) + float(rect[2])) / 2, (float(rect[1]) + float(rect[3])) / 2)
        except (TypeError, ValueError):
            return None
    try:
        return float(mark["x"]), float(mark["y"])
    except (KeyError, TypeError, ValueError):
        return None


def _candidate_center(candidate: dict[str, Any]) -> tuple[float, float, float]:
    box = candidate.get("box") if isinstance(candidate.get("box"), dict) else {}
    width = max(1.0, float(box.get("w") or 1.0))
    height = max(1.0, float(box.get("h") or 1.0))
    if candidate.get("x") is not None and candidate.get("y") is not None:
        return float(candidate["x"]), float(candidate["y"]), max(width, height)
    return (
        float(box.get("x") or 0.0) + width / 2,
        float(box.get("y") or 0.0) + height / 2,
        max(width, height),
    )


def _visual_mark_invoke_refusal(
    state: TurnState, capability_name: str, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    """Enforce evidence gates for reference-based visual marking."""
    if capability_name != "mark_on_image":
        return None
    source_id = str(arguments.get("asset_id") or "").strip()
    if not source_id:
        return {
            "type": "runtime_refusal",
            "error": "MARK_SOURCE_REQUIRED",
            "meaning": "标图工作流必须明确填写原图 asset_id，不能依赖共同视口的隐式底图。",
        }
    bindings = state.analysis.get("asset_bindings") or []
    bound_ids = {
        str(item.get("asset_id") or "").strip()
        for item in bindings
        if isinstance(item, dict) and str(item.get("asset_id") or "").strip()
    }
    reference_ids = bound_ids - {source_id}
    if not reference_ids:
        return None
    inspected = _inspected_asset_ids(state)
    missing = sorted(({source_id} | reference_ids) - inspected)
    if missing:
        return {
            "type": "runtime_refusal",
            "error": "REFERENCE_ASSET_NOT_INSPECTED",
            "meaning": "必须先分别 inspect_asset 原图和用户提供的目标参考图，确认场景与正例特征后才能绘制。",
            "missing_asset_ids": missing,
        }
    candidates = _match_candidates_for(
        state, source_asset_id=source_id, reference_asset_ids=reference_ids
    )
    if not candidates:
        return {
            "type": "runtime_refusal",
            "error": "VERIFIED_CANDIDATES_REQUIRED",
            "meaning": (
                "当前只有视觉粗定位，不能直接绘制。请先用参考素材对原图执行 "
                "match_template_multi，并取得未截断、非 ambiguous 的本地候选。"
            ),
        }
    marks = [item for item in arguments.get("marks") or [] if isinstance(item, dict)]
    if len(marks) != len(candidates):
        return {
            "type": "runtime_refusal",
            "error": "MARK_CANDIDATE_COUNT_MISMATCH",
            "meaning": "标记数量必须与本地验证候选数量一致，不能把整排粗框当成一个目标或漏画。",
            "mark_count": len(marks),
            "candidate_count": len(candidates),
        }
    unused = list(candidates)
    for index, mark in enumerate(marks, start=1):
        center = _mark_center(mark)
        if center is None:
            return {
                "type": "runtime_refusal",
                "error": "MARK_NOT_GROUNDED",
                "meaning": f"第 {index} 个标记没有可验证的中心坐标。",
            }
        best_index = -1
        best_distance = float("inf")
        for candidate_index, candidate in enumerate(unused):
            cx, cy, side = _candidate_center(candidate)
            distance = ((center[0] - cx) ** 2 + (center[1] - cy) ** 2) ** 0.5
            tolerance = max(4.0, side * 0.35)
            if distance <= tolerance and distance < best_distance:
                best_distance = distance
                best_index = candidate_index
        if best_index < 0:
            return {
                "type": "runtime_refusal",
                "error": "MARK_NOT_GROUNDED",
                "meaning": f"第 {index} 个标记不对应任何本地验证候选，禁止提交裸猜坐标。",
            }
        unused.pop(best_index)
    return None


def _visual_submit_refusal(state: TurnState, response: dict[str, Any]) -> dict[str, Any] | None:
    if not response.get("completed"):
        return None
    latest_annotation_id = ""
    for item in state.results:
        if not isinstance(item, dict) or item.get("type") != "capability_result":
            continue
        if _capability_result_name(state, item) != "mark_on_image":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if result.get("ok"):
            latest_annotation_id = str(result.get("asset_id") or "").strip()
    if not latest_annotation_id:
        return None
    if latest_annotation_id not in _inspected_asset_ids(state, verify_only=True):
        return {
            "type": "runtime_refusal",
            "error": "ANNOTATION_OUTPUT_NOT_VERIFIED",
            "meaning": (
                "标注图已生成但尚未复核。请对新产物调用 inspect_asset(mode=verify)，"
                "确认无漏标、错标和系统性偏移后再提交。"
            ),
            "asset_id": latest_annotation_id,
        }
    return None


def _checkpoint_state(
    state: TurnState, *, question: str, choices: list[str], question_id: str
) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "waiting_user",
        "turn_id": state.turn_id,
        "question_id": question_id,
        "question": str(question or ""),
        "choices": [str(item) for item in choices[:3]],
        "user_text": state.user_text,
        "messages": _json_safe(state.messages),
        "analysis": _json_safe(state.analysis),
        "results": _json_safe(state.results),
        "contracts": _json_safe(state.contracts),
        "subturns": state.subturns,
        "repairs": state.repairs,
        "no_progress": state.no_progress,
        "last_fingerprint": state.last_fingerprint,
        "asset_candidates": _json_safe(state.asset_candidates),
        "supplements": _json_safe(state.supplements),
        "requirement_replacements": state.requirement_replacements,
        "previous_requirements": _json_safe(state.previous_requirements),
    }


def _restore_waiting_state(raw: Any, *, trace_log: V4TraceLog | None) -> TurnState | None:
    if not isinstance(raw, dict) or raw.get("status") != "waiting_user":
        return None
    turn_id = str(raw.get("turn_id") or "")
    if not turn_id.startswith("v4_"):
        return None
    messages = raw.get("messages")
    if not isinstance(messages, list):
        return None
    state = TurnState(
        turn_id=turn_id,
        user_text=str(raw.get("user_text") or ""),
        messages=[
            {"role": str(item.get("role") or ""), "content": str(item.get("content") or "")}
            for item in messages
            if isinstance(item, dict) and str(item.get("role") or "") in {"user", "assistant"}
        ],
        analysis=dict(raw.get("analysis") or {}) if isinstance(raw.get("analysis"), dict) else {},
        results=list(raw.get("results") or []) if isinstance(raw.get("results"), list) else [],
        contracts=dict(raw.get("contracts") or {}) if isinstance(raw.get("contracts"), dict) else {},
        subturns=max(0, int(raw.get("subturns") or 0)),
        repairs=max(0, int(raw.get("repairs") or 0)),
        no_progress=max(0, int(raw.get("no_progress") or 0)),
        last_fingerprint=str(raw.get("last_fingerprint") or ""),
        asset_candidates=list(raw.get("asset_candidates") or [])
        if isinstance(raw.get("asset_candidates"), list)
        else [],
        trace_log=trace_log,
        supplements=list(raw.get("supplements") or [])
        if isinstance(raw.get("supplements"), list)
        else [],
        requirement_replacements=max(0, int(raw.get("requirement_replacements") or 0)),
        previous_requirements=list(raw.get("previous_requirements") or [])
        if isinstance(raw.get("previous_requirements"), list)
        else [],
    )
    return state


def _save_waiting_checkpoint(
    session, state: TurnState, *, question: str, choices: list[str]
) -> str:
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    question_id = f"vq_{uuid4().hex[:12]}"
    session.artifacts["v4_waiting_turn"] = _checkpoint_state(
        state,
        question=question,
        choices=choices,
        question_id=question_id,
    )
    return question_id


def _clear_waiting_checkpoint(session) -> None:
    if isinstance(session.artifacts, dict):
        session.artifacts.pop("v4_waiting_turn", None)


def _unfinished_requirements(session) -> list[dict[str, Any]]:
    """Return compact, still-actionable contracts; never replay old transcripts."""
    artifacts = session.artifacts if isinstance(session.artifacts, dict) else {}
    raw = artifacts.get("v4_unfinished_requirements")
    if not isinstance(raw, list):
        return []
    active_ids = {
        str(item.get("id") or item.get("asset_id") or "")
        for item in list(getattr(session, "assets", None) or [])
        if isinstance(item, dict)
    }
    cards: list[dict[str, Any]] = []
    for item in raw[-8:]:
        if not isinstance(item, dict) or item.get("status") not in {"pending", "active", "blocked"}:
            continue
        card = dict(item)
        card["asset_bindings"] = [
            binding
            for binding in list(card.get("asset_bindings") or [])
            if isinstance(binding, dict) and str(binding.get("asset_id") or "") in active_ids
        ]
        cards.append(_json_safe(card))
    return cards


def _update_unfinished_requirements(session, state: TurnState, *, status: str) -> None:
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    previous = _unfinished_requirements(session)
    inherited_ids = {
        str(item.get("requirement_id") or "") for item in state.previous_requirements if isinstance(item, dict)
    }
    superseded = {
        str(item) for item in list(state.analysis.get("superseded_requirement_ids") or []) if str(item).strip()
    }
    retained = [
        item
        for item in previous
        if str(item.get("requirement_id") or "") not in inherited_ids | superseded
    ]
    if status in {"incomplete", "paused", "waiting_trial_result"}:
        active = [str(item).strip() for item in list(state.analysis.get("active_requirements") or []) if str(item).strip()]
        if not active:
            active = [str(state.analysis.get("goal") or state.user_text).strip()]
        evidence = []
        for result in state.results[-20:]:
            if not isinstance(result, dict) or result.get("type") not in {"query_result", "capability_result", "script_result"}:
                continue
            evidence.append(
                {
                    "type": result.get("type"),
                    "capability_id": result.get("capability_id", ""),
                    "request": _json_safe(result.get("request") or {}),
                    "result": _json_safe(result.get("result") or {}),
                }
            )
        retained.append(
            {
                "requirement_id": f"req_{state.turn_id}",
                "source_turn_id": state.turn_id,
                "status": "blocked" if status == "paused" else "active" if status == "waiting_trial_result" else "pending",
                "goal": str(state.analysis.get("goal") or state.user_text),
                "deliverable": str(state.analysis.get("deliverable") or ""),
                "active_requirements": active,
                "success_criteria": list(state.analysis.get("success_criteria") or []),
                "remaining_gaps": list(state.analysis.get("unknown") or []),
                "asset_bindings": _json_safe(state.analysis.get("asset_bindings") or []),
                "evidence": evidence[-8:],
            }
        )
    session.artifacts["v4_unfinished_requirements"] = retained[-8:]


def _apply_supplement_revision(state: TurnState, review: dict[str, Any]) -> None:
    """Apply explicit requirement revisions without overwriting the original request."""
    goal = str(review.get("updated_goal") or "").strip()
    deliverable = str(review.get("updated_deliverable") or "").strip()
    criteria = review.get("updated_success_criteria")
    if goal:
        state.analysis["goal"] = goal
    if deliverable:
        state.analysis["deliverable"] = deliverable
    if isinstance(criteria, list) and criteria:
        state.analysis["success_criteria"] = [str(item) for item in criteria if str(item).strip()]


def _supplement_question_result(
    session, state: TurnState, review: dict[str, Any], total_in: int, total_out: int
) -> tuple[str, dict[str, Any], int, int]:
    question = str(review.get("question") or "请确认补充后的最终要求。").strip()
    chips = [str(item) for item in review.get("choices") or []][:3]
    question_id = _save_waiting_checkpoint(
        session, state, question=question, choices=chips
    )
    _save_trace(session, state, status="waiting_user")
    if state.trace_log:
        state.trace_log.event(
            "main_turn_waiting_user_after_supplement",
            turn_id=state.turn_id,
            subturns=state.subturns,
            question=question,
        )
    return (
        question,
        {
            "turn": "ask",
            "ask": "user_choice",
            "chips": chips,
            "plan": [],
            "task_done": False,
            "v4_resume_main_turn": True,
            "v4_main_turn_id": state.turn_id,
            "v4_question_id": question_id,
            "_trace": [f"V4 主轮 {state.turn_id} · 补充后等待确认"],
        },
        total_in,
        total_out,
    )


def _supplement_replacement_pause(
    session, state: TurnState, total_in: int, total_out: int
) -> tuple[str, dict[str, Any], int, int]:
    question = "你连续多次改变了任务目标，请确认最终要求后继续。"
    question_id = _save_waiting_checkpoint(
        session, state, question=question, choices=[]
    )
    _save_trace(session, state, status="waiting_user")
    if state.trace_log:
        state.trace_log.event(
            "main_turn_paused_supplement_replacements",
            level="WARNING",
            turn_id=state.turn_id,
            subturns=state.subturns,
            replacements=state.requirement_replacements,
        )
    return (
        "你连续多次补充并改变了任务目标。为避免继续按过期目标执行，请先确认最终要求。",
        {
            "turn": "ask",
            "ask": "user_choice",
            "chips": [],
            "plan": [],
            "task_done": False,
            "v4_resume_main_turn": True,
            "v4_main_turn_id": state.turn_id,
            "v4_question_id": question_id,
            "_trace": [f"V4 主轮 {state.turn_id} · 补充连续改写目标，已暂停"],
        },
        total_in,
        total_out,
    )


async def _call_form(
    *,
    state: TurnState,
    spec: FormSpec,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: str | None,
    max_tokens: int | None,
    on_partial=None,
) -> tuple[dict[str, Any], int, int]:
    from backend.script_generator.agent import call_llm

    repair_errors: list[dict[str, str]] = []
    total_in = total_out = 0
    accumulated_input_tokens = 0  # 跟踪累积的 input tokens
    for attempt in range(1, MAX_FORM_REPAIRS + 1):
        if state.subturns >= MAX_SUBTURNS:
            raise RuntimeError(f"V4 主轮达到 {MAX_SUBTURNS} 次副轮上限")

        # B 方案：检查主轮 token 预算
        if accumulated_input_tokens > MAX_CONTEXT_TOKENS:
            raise RuntimeError(
                f"V4 主轮上下文超过预算限制：已使用 {accumulated_input_tokens:,} tokens，"
                f"超过硬限制 {MAX_CONTEXT_TOKENS:,} tokens。"
                f"这可能是因为 recent_runtime_results 累积过多，"
                f"或单个副轮返回的结果过大。"
            )

        context = state.context()
        if spec.screen != "requirement_analysis":
            # The directory is an opening-page aid, not baggage repeated on
            # every later form. Bound ids remain in the analysis result.
            context.pop("available_assets", None)
            context.pop("asset_reference_candidates", None)
            context.pop("asset_policy", None)
        payload = spec.payload(context=context)
        if repair_errors:
            payload["repair"] = {
                "attempt": attempt,
                "max_attempts": MAX_FORM_REPAIRS,
                "errors": repair_errors,
                "instruction": "按错误路径修正同一张 form；不要改变结构。",
            }
        user_message = json.dumps(payload, ensure_ascii=False)
        if state.trace_log:
            state.trace_log.event(
                "form_request",
                turn_id=state.turn_id,
                subturn=state.subturns + 1,
                screen=spec.screen,
                context_keys=sorted(payload.get("context", {}).keys()),
                prompt_chars=len(user_message),
                payload=payload,
                repair=payload.get("repair"),
            )
        state.messages.append({"role": "user", "content": user_message})
        try:
            raw, inp, out = await call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=list(state.messages),
                system_prompt=_system_prompt(),
                on_partial=None,  # 表单 JSON 不直接流到聊天气泡
                max_tokens=max_tokens,
            )
        except Exception as exc:
            if state.trace_log:
                state.trace_log.exception(
                    "llm_call_failed", exc, turn_id=state.turn_id, subturn=state.subturns + 1, screen=spec.screen
                )
            raise
        total_in += int(inp or 0)
        total_out += int(out or 0)
        accumulated_input_tokens += int(inp or 0)  # 累积 input tokens
        state.subturns += 1
        state.messages.append({"role": "assistant", "content": str(raw or "")})
        if state.trace_log:
            state.trace_log.event(
                "form_response",
                turn_id=state.turn_id,
                subturn=state.subturns,
                screen=spec.screen,
                input_tokens=int(inp or 0),
                output_tokens=int(out or 0),
                accumulated_input_tokens=accumulated_input_tokens,  # 添加累积追踪
                token_budget_used_percent=round(accumulated_input_tokens / MAX_CONTEXT_TOKENS * 100, 1),  # 预算使用百分比
                raw=str(raw or ""),
            )
        try:
            parsed = _extract_object(raw)
            if spec.screen == "requirement_analysis" and not state.previous_requirements:
                # Backward compatibility for providers that cached the pre-inheritance
                # opening form.  Once unfinished cards exist, the merge fields are
                # mandatory and malformed replies are repaired normally.
                parsed.setdefault("previous_requirements_relationship", "none")
                parsed.setdefault("user_explicit_preference", "unspecified")
                parsed.setdefault("active_requirements", [str(parsed.get("goal") or state.user_text)])
                parsed.setdefault("superseded_requirement_ids", [])
                parsed.setdefault("needs_user_confirmation", False)
                parsed.setdefault("confirmation_question", "")
                parsed.setdefault("confirmation_choices", [])
            if spec.screen == "invoke_capability":
                requested_id = str(parsed.get("capability_id") or "")
                if requested_id and requested_id not in state.contracts:
                    requested_contract = contract_for(requested_id)
                    if requested_contract is not None:
                        # The model changed its mind after entering an invoke
                        # screen. Expose the newly requested contract on the
                        # next repair page; never execute it sight-unseen.
                        state.contracts[requested_id] = requested_contract
                        spec = invoke_form(requested_contract)
                        repair_errors = [
                            {
                                "path": "capability_id",
                                "code": "CONTRACT_SWITCHED",
                                "meaning": (
                                    f"已加载 {requested_id} 的完整契约；"
                                    "请按新契约重新填写同一张调用表。"
                                ),
                            }
                        ]
                        state.repairs += 1
                        if state.trace_log:
                            state.trace_log.event(
                                "invoke_contract_switched",
                                level="WARNING",
                                turn_id=state.turn_id,
                                subturn=state.subturns,
                                capability_id=requested_id,
                            )
                        continue
            value = validate_form(spec, parsed, contracts=state.contracts)
            if spec.screen == "requirement_analysis":
                known_asset_ids = {str(item.get("asset_id") or "") for item in state.asset_index}
                invalid_ids = sorted(
                    {
                        str(item.get("asset_id") or "")
                        for item in (value.get("asset_bindings") or [])
                        if isinstance(item, dict) and str(item.get("asset_id") or "") not in known_asset_ids
                    }
                )
                if invalid_ids:
                    raise FormError(
                        [
                            {
                                "path": "asset_bindings",
                                "code": "UNKNOWN_ASSET_ID",
                                "meaning": f"素材 id 不在当前目录中：{invalid_ids}",
                            }
                        ]
                    )
                known_requirement_ids = {
                    str(item.get("requirement_id") or "")
                    for item in state.previous_requirements
                    if isinstance(item, dict)
                }
                invalid_requirement_ids = sorted(
                    {
                        str(item)
                        for item in (value.get("superseded_requirement_ids") or [])
                        if str(item) not in known_requirement_ids
                    }
                )
                if invalid_requirement_ids:
                    raise FormError(
                        [
                            {
                                "path": "superseded_requirement_ids",
                                "code": "UNKNOWN_REQUIREMENT_ID",
                                "meaning": f"旧需求 id 不在当前未完成任务卡中：{invalid_requirement_ids}",
                            }
                        ]
                    )
            state.repairs = 0
            if state.trace_log:
                state.trace_log.event(
                    "form_valid", turn_id=state.turn_id, subturn=state.subturns, screen=spec.screen, value=value
                )
            return value, total_in, total_out
        except FormError as exc:
            repair_errors = exc.errors
            state.repairs += 1
            if state.trace_log:
                state.trace_log.event(
                    "form_invalid",
                    level="WARNING",
                    turn_id=state.turn_id,
                    subturn=state.subturns,
                    screen=spec.screen,
                    repair_attempt=attempt,
                    errors=repair_errors,
                )
    diagnostic = {
        "turn_id": state.turn_id,
        "screen": spec.screen,
        "errors": repair_errors,
        "subturns": state.subturns,
        "messages": state.messages[-6:],
    }
    if state.trace_log:
        state.trace_log.event("form_repair_exhausted", level="ERROR", **diagnostic)
    log_hint = f"，详见 {state.trace_log.path}" if state.trace_log and state.trace_log.path else ""
    raise RuntimeError(
        f"V4 表单连续 {MAX_FORM_REPAIRS} 次不合法，已停止本轮"
        f"（页面：{spec.screen}；主轮：{state.turn_id}{log_hint}）"
    )


def _fingerprint(state: TurnState) -> str:
    return json.dumps(
        {
            "results": state.results[-3:],
            "contracts": sorted(state.contracts),
            "assets": len(getattr(state, "_assets", []) or []),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


async def run_v4_turn(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: str | None,
    max_tokens: int | None,
    session,
    permissions: dict[str, Any] | None = None,
    vision_assist: dict[str, Any] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_partial=None,
    trace_log: V4TraceLog | None = None,
    supplement_box=None,
) -> tuple[str, dict[str, Any], int, int]:
    """Run one user main turn and return the existing GUI result shape."""
    from backend.script_generator.collaborator.llm_turn import make_tool_resolver
    from backend.script_generator.collaborator.skills.registry import default_registry

    def emit(text: str) -> None:
        if on_status:
            on_status(text)

    user_text = next(
        (str(m.get("text") or "") for m in reversed(session.messages or []) if str(m.get("role") or "") == "user"),
        "",
    )
    waiting = (session.artifacts or {}).get("v4_waiting_turn") if isinstance(session.artifacts, dict) else None
    state = _restore_waiting_state(waiting, trace_log=trace_log)
    resumed = state is not None
    if state is None:
        state = TurnState(turn_id=f"v4_{uuid4().hex}", user_text=user_text, trace_log=trace_log)
        state.previous_requirements = _unfinished_requirements(session)
    from backend.script_generator.collaborator.resource_registry import active_session_assets

    setattr(state, "_assets", active_session_assets(session))
    state.asset_index = compact_asset_index(session)
    if not resumed:
        state.asset_candidates = resolve_asset_references(user_text, state.asset_index)
    else:
        active_ids = {str(item.get("asset_id") or "") for item in state.asset_index}
        state.asset_candidates = [
            item for item in state.asset_candidates
            if str(item.get("asset_id") or "") in active_ids
        ]
        if isinstance(state.analysis, dict):
            state.analysis["asset_bindings"] = [
                item for item in list(state.analysis.get("asset_bindings") or [])
                if isinstance(item, dict) and str(item.get("asset_id") or "") in active_ids
            ]
        for recorded in state.results:
            if not isinstance(recorded, dict) or recorded.get("type") != "query_result":
                continue
            request = recorded.get("request") if isinstance(recorded.get("request"), dict) else {}
            result = recorded.get("result") if isinstance(recorded.get("result"), dict) else {}
            if request.get("query") != "session.assets" or not isinstance(result.get("items"), list):
                continue
            result["items"] = [
                item for item in result["items"]
                if isinstance(item, dict) and str(item.get("id") or "") in active_ids
            ]
            result["returned"] = len(result["items"])
        state.results.append(
            {
                "type": "asset_scope_refreshed",
                "result": {
                    "authoritative": True,
                    "current_assets": list(state.asset_index),
                    "note": "素材区当前状态已刷新；旧副轮里已删除的素材引用无效。",
                },
            }
        )
        waiting_d = waiting if isinstance(waiting, dict) else {}
        response = {
            "type": "user_response",
            "main_turn_id": state.turn_id,
            "question_id": str(waiting_d.get("question_id") or ""),
            "question": str(waiting_d.get("question") or ""),
            "answer": user_text,
        }
        state.messages.append(
            {"role": "user", "content": json.dumps(response, ensure_ascii=False)}
        )
        state.results.append(response)
    if trace_log:
        trace_log.event(
            "main_turn_resumed" if resumed else "main_turn_started",
            turn_id=state.turn_id,
            provider=provider,
            model=model,
            asset_index=state.asset_index,
            asset_candidates=state.asset_candidates,
            limits={"subturns": MAX_SUBTURNS, "form_repairs": MAX_FORM_REPAIRS, "no_progress": MAX_NO_PROGRESS},
        )
        if isinstance(session.artifacts, dict):
            session.artifacts["v4_runtime_log_path"] = str(trace_log.path or "")
    gateway = SessionQueryGateway(session)
    if resumed:
        gateway.category_menu_shown = any(
            isinstance(item, dict)
            and item.get("type") == "query_result"
            and isinstance(item.get("request"), dict)
            and item["request"].get("query") == "capability.list_categories"
            for item in state.results
        )
        # Discovery is part of the same main turn.  An ask_user checkpoint must
        # not forget which summaries were already shown, otherwise the resumed
        # turn is forced to repeat category navigation before reading a skill or
        # tool contract.
        for item in state.results:
            if not isinstance(item, dict) or item.get("type") != "query_result":
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            for found in list(result.get("items") or []):
                if isinstance(found, dict) and found.get("capability_id"):
                    gateway.discovered_capability_ids.add(str(found["capability_id"]))
            for key in ("capability", "contract"):
                found = result.get(key)
                if isinstance(found, dict) and found.get("capability_id"):
                    gateway.discovered_capability_ids.add(str(found["capability_id"]))
    resolver = make_tool_resolver(
        default_registry(),
        session,
        primary_vision={
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "api_endpoint": api_endpoint,
        },
        vision_assist=vision_assist,
        permissions=permissions,
        on_status=emit,
    )
    total_in = total_out = 0

    async def fill(spec: FormSpec) -> dict[str, Any]:
        nonlocal total_in, total_out
        value, inp, out = await _call_form(
            state=state,
            spec=spec,
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            max_tokens=max_tokens,
            on_partial=on_partial,
        )
        total_in += inp
        total_out += out
        return value

    async def review_supplements(*, pending_action: dict[str, Any]) -> dict[str, Any] | None:
        """At a completed-subturn boundary, let supplements supersede the frozen action."""
        if supplement_box is None:
            return None
        final_review: dict[str, Any] | None = None
        while True:
            incoming = supplement_box.drain_context()
            if not incoming:
                break
            unique: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in incoming:
                text = str(item.get("text") or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                unique.append({"text": text, "context": _json_safe(item.get("context") or {})})
            if not unique:
                continue
            state.pending_supplements = unique
            state.pending_action = _json_safe(pending_action)
            state.supplements.extend(unique)
            state.results.append(
                {"type": "user_supplement", "items": unique, "after_subturn": state.subturns}
            )
            if trace_log:
                trace_log.event(
                    "supplement_received",
                    turn_id=state.turn_id,
                    subturn=state.subturns,
                    pending_action=state.pending_action,
                    items=unique,
                    duplicate_count=max(0, len(incoming) - len(unique)),
                )
            emit("V4 · 审查用户补充…")
            review = await fill(supplement_review_form())
            final_review = review
            _apply_supplement_revision(state, review)
            state.results.append(
                {"type": "supplement_review", "items": unique, "result": review}
            )
            if trace_log:
                trace_log.event(
                    "supplement_injected",
                    turn_id=state.turn_id,
                    subturn=state.subturns,
                    decision=review.get("decision"),
                    review=review,
                )
            state.pending_supplements = []
            state.pending_action = {}
            # A supplement submitted during this review is deliberately handled
            # at the boundary immediately following the review subturn.
        return final_review

    async def supplement_outcome(
        *, pending_action: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        review = await review_supplements(pending_action=pending_action)
        if not review:
            return "keep", None
        decision = str(review.get("decision") or "modify")
        if decision == "replace_requirement":
            state.requirement_replacements += 1
            if state.requirement_replacements >= 3:
                return "pause_replacements", review
        return decision, review

    if resumed:
        emit(f"V4 · 恢复主轮（{state.subturns}/{MAX_SUBTURNS}）…")
        if trace_log:
            trace_log.event(
                "user_response_injected",
                turn_id=state.turn_id,
                subturns=state.subturns,
                question_id=str((waiting or {}).get("question_id") or "") if isinstance(waiting, dict) else "",
                answer=user_text,
            )
    else:
        emit("V4 · 理解需求…")
        state.analysis = await fill(requirement_form())
        decision, review = await supplement_outcome(
            pending_action={"screen": "requirement_analysis", "result": state.analysis}
        )
        if decision == "ask_user":
            return _supplement_question_result(session, state, review or {}, total_in, total_out)
        if decision == "pause_replacements":
            return _supplement_replacement_pause(session, state, total_in, total_out)
        if decision in {"modify", "replace_requirement"}:
            state.analysis = await fill(requirement_form())
        state.results.append({"type": "requirement_analysis", "result": state.analysis})
        if state.analysis.get("needs_user_confirmation"):
            question = str(state.analysis.get("confirmation_question") or "请确认新旧需求的处理方式。")
            choices = [str(item) for item in list(state.analysis.get("confirmation_choices") or [])][:3]
            question_id = _save_waiting_checkpoint(session, state, question=question, choices=choices)
            _save_trace(session, state, status="waiting_user")
            return (
                question,
                {
                    "turn": "ask",
                    "ask": "user_choice",
                    "chips": choices,
                    "plan": [],
                    "task_done": False,
                    "v4_resume_main_turn": True,
                    "v4_main_turn_id": state.turn_id,
                    "v4_question_id": question_id,
                    "_trace": [f"V4 主轮 {state.turn_id} · 等待确认需求继承"],
                },
                total_in,
                total_out,
            )

    while state.subturns < MAX_SUBTURNS:
        emit(f"V4 · 选择下一步（{state.subturns}/{MAX_SUBTURNS}）…")
        allowed = list(_allowed_actions(state))
        choice = await fill(menu_form(allowed=allowed))
        action = str(choice["action"])
        decision, review = await supplement_outcome(
            pending_action={"screen": "next_action", "choice": choice}
        )
        if decision == "ask_user":
            return _supplement_question_result(session, state, review or {}, total_in, total_out)
        if decision == "pause_replacements":
            return _supplement_replacement_pause(session, state, total_in, total_out)
        if decision == "replace_requirement":
            state.analysis = await fill(requirement_form())
            state.results.append({"type": "requirement_revision", "result": state.analysis})
            continue
        if decision == "modify":
            continue
        if trace_log:
            trace_log.event(
                "menu_selected", turn_id=state.turn_id, subturn=state.subturns, allowed=allowed, choice=choice
            )
        before = _fingerprint(state)

        if action == "query_local":
            request = await fill(query_form())
            decision, review = await supplement_outcome(
                pending_action={"action": action, "request": request}
            )
            if decision == "ask_user":
                return _supplement_question_result(session, state, review or {}, total_in, total_out)
            if decision == "pause_replacements":
                return _supplement_replacement_pause(session, state, total_in, total_out)
            if decision == "replace_requirement":
                state.analysis = await fill(requirement_form())
                state.results.append({"type": "requirement_revision", "result": state.analysis})
                continue
            if decision == "modify":
                continue
            result = gateway.execute(str(request["query"]), dict(request["arguments"]))
            if trace_log:
                trace_log.event(
                    "query_completed", turn_id=state.turn_id, subturn=state.subturns, request=request, result=_json_safe(result)
                )
            if request["query"] == "capability.get_contract" and result.get("ok"):
                contract = dict(result.get("contract") or {})
                if contract.get("capability_id"):
                    state.contracts[str(contract["capability_id"])] = contract
            state.results.append({"type": "query_result", "request": request, "result": _json_safe(result)})
        elif action == "invoke_capability":
            # Let the model select only among contracts already loaded in this main turn.
            if not state.contracts:
                state.results.append({"type": "runtime_refusal", "error": "CONTRACT_REQUIRED"})
            else:
                # If several are loaded, the form still validates the selected id against the map.
                seed = next(iter(state.contracts.values()))
                request = await fill(invoke_form(seed))
                decision, review = await supplement_outcome(
                    pending_action={"action": action, "request": request}
                )
                if decision == "ask_user":
                    return _supplement_question_result(session, state, review or {}, total_in, total_out)
                if decision == "pause_replacements":
                    return _supplement_replacement_pause(session, state, total_in, total_out)
                if decision == "replace_requirement":
                    state.analysis = await fill(requirement_form())
                    state.results.append({"type": "requirement_revision", "result": state.analysis})
                    continue
                if decision == "modify":
                    continue
                capability_id = str(request["capability_id"])
                contract = state.contracts[capability_id]
                refusal = _visual_mark_invoke_refusal(
                    state,
                    str(contract.get("name") or ""),
                    dict(request["arguments"]),
                )
                if refusal is not None:
                    state.results.append(refusal)
                    if trace_log:
                        trace_log.event(
                            "capability_refused",
                            level="WARNING",
                            turn_id=state.turn_id,
                            subturn=state.subturns,
                            capability_id=capability_id,
                            refusal=refusal,
                        )
                    continue
                if trace_log:
                    trace_log.event(
                        "capability_started",
                        turn_id=state.turn_id,
                        subturn=state.subturns,
                        capability_id=capability_id,
                        capability_name=contract.get("name"),
                        arguments=request["arguments"],
                    )
                try:
                    raw = resolver(str(contract["name"]), dict(request["arguments"]))
                except Exception as exc:
                    if trace_log:
                        trace_log.exception(
                            "capability_failed", exc, turn_id=state.turn_id, capability_id=capability_id
                        )
                    raise
                try:
                    result = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    result = {"ok": False, "raw": str(raw)}
                from backend.script_generator.collaborator.asset_ops import register_result_images

                published = register_result_images(session, result)
                if published and isinstance(result, dict):
                    result["new_assets"] = list(result.get("new_assets") or []) + published
                state.results.append(
                    {
                        "type": "capability_result",
                        "capability_id": capability_id,
                        "capability_name": str(contract.get("name") or ""),
                        "arguments": _json_safe(dict(request["arguments"])),
                        "result": _json_safe(result),
                    }
                )
                if trace_log:
                    trace_log.event(
                        "capability_completed", turn_id=state.turn_id, capability_id=capability_id, result=_json_safe(result)
                    )
                setattr(state, "_assets", active_session_assets(session))
        elif action == "temporary_script":
            request = await fill(temporary_script_form())
            decision, review = await supplement_outcome(
                pending_action={"action": action, "request": request}
            )
            if decision == "ask_user":
                return _supplement_question_result(session, state, review or {}, total_in, total_out)
            if decision == "pause_replacements":
                return _supplement_replacement_pause(session, state, total_in, total_out)
            if decision == "replace_requirement":
                state.analysis = await fill(requirement_form())
                state.results.append({"type": "requirement_revision", "result": state.analysis})
                continue
            if decision == "modify":
                continue
            generated_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
            code = "ARGS = " + repr(dict(request["parameters"])) + "\n" + str(request["code"])
            if trace_log:
                trace_log.event(
                    "temporary_script_started",
                    turn_id=state.turn_id,
                    subturn=state.subturns,
                    purpose=request["purpose"],
                    parameters=request["parameters"],
                    code=code,
                    generated_at=generated_at,
                )
            try:
                raw = resolver(
                    "run_probe_script",
                    {"code": code, "script_id": f"v4_temporary:{request['purpose']}"},
                )
            except Exception as exc:
                if trace_log:
                    trace_log.exception("temporary_script_failed", exc, turn_id=state.turn_id, code=code)
                raise
            try:
                result = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                result = {"ok": False, "raw": str(raw)}
            from backend.script_generator.collaborator.asset_ops import register_result_images

            published = register_result_images(session, result)
            if published and isinstance(result, dict):
                result["new_assets"] = list(result.get("new_assets") or []) + published
            state.results.append(
                {
                    "type": "temporary_script_result",
                    "purpose": request["purpose"],
                    "generated_at": generated_at,
                    "result": _json_safe(result),
                }
            )
            if trace_log:
                trace_log.event(
                    "temporary_script_completed", turn_id=state.turn_id, purpose=request["purpose"], result=_json_safe(result)
                )
            setattr(state, "_assets", active_session_assets(session))
        elif action == "load_trial_script":
            from backend.script_generator.collaborator.collab_form import collab_script_issues
            from backend.script_generator.collaborator.reusable_tools import find_tool_def, load_tool_defs
            from backend.script_generator.api_contracts.render import lookup_text

            tool_names = [str(item.get("name") or "") for item in load_tool_defs() if str(item.get("name") or "")]
            source_request = await fill(trial_source_form(tool_names=tool_names))
            source = find_tool_def(str(source_request["source_name"]))
            if not isinstance(source, dict):
                state.results.append(
                    {
                        "type": "runtime_refusal",
                        "error": "TRIAL_SOURCE_NOT_FOUND",
                        "meaning": "所选自造工具已不存在，请重新选择真实代码来源。",
                    }
                )
                continue
            source_for_model = {
                "name": source.get("name"),
                "description": source.get("description"),
                "parameters": source.get("parameters"),
                "code": source.get("code"),
                "execution_scope": "probe_only",
                "copy_policy": "禁止把源码或其 cv2/numpy/read_image/ARGS 调用直接复制到试运行脚本。",
                "delivery_translation": {
                    "multi_template_match": "await browser.match_image_multi(template_path, threshold=...)",
                    "current_frame": "await browser.update_frame()；返回值已经是当前画面，不要再次 imdecode",
                    "asset_path": "使用 requested_runtime_behavior 中的真实 asset_id 占位路径",
                    "dynamic_grouping": (
                        "若需求要求行列数不固定，先按 y 自适应聚类成行，再按每行相邻 x 间距的断层"
                        "划分卡片；禁止复制源码中的 mid_x 左右二分或固定行列数量。"
                    ),
                },
            }
            trial_request_context = dict(source_request)
            trial_request_context["asset_bindings"] = _json_safe(state.analysis.get("asset_bindings") or [])
            trial_request_context["available_assets"] = _json_safe(state.asset_index)
            handoff = await fill(
                trial_script_form(
                    source=source_for_model,
                    source_request=trial_request_context,
                    runner_api_contracts=[
                        lookup_text(None, "update_frame"),
                        lookup_text(None, "match_image_multi"),
                        lookup_text(None, "script_log"),
                    ],
                )
            )
            generated_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
            script = str(handoff["script"])
            issues = collab_script_issues(script)
            if issues:
                state.results.append(
                    {
                        "type": "runtime_refusal",
                        "error": "TRIAL_SCRIPT_INVALID",
                        "meaning": "试运行脚本未通过协作脚本检查，请修正后重新交付。",
                        "issues": list(issues),
                    }
                )
                continue
            state.results.append(
                {
                    "type": "trial_handoff",
                    "source_type": "reusable_tool",
                    "source_name": source_request["source_name"],
                    "script_name": source_request["script_name"],
                    "created_at": (
                        str((session.artifacts.get("trial_handoff") or {}).get("created_at") or "")
                        if isinstance(session.artifacts, dict)
                        and str((session.artifacts.get("trial_handoff") or {}).get("script_name") or "")
                        == str(source_request["script_name"])
                        else ""
                    ) or generated_at,
                    "updated_at": generated_at,
                    "workflow_status": "waiting_trial_result",
                }
            )
            if not isinstance(session.artifacts, dict):
                session.artifacts = {}
            session.artifacts["workflow_status"] = "waiting_trial_result"
            session.artifacts["trial_handoff"] = _json_safe(state.results[-1])
            meta = {
                "turn": "stop",
                "plan": [],
                "task_done": False,
                "workflow_status": "waiting_trial_result",
                "script": script,
                "script_name": str(source_request["script_name"]),
                "open_trial": True,
                "trial_handoff": _json_safe(state.results[-1]),
                "_trace": [f"V4 主轮 {state.turn_id} · 已交付到试运行"],
            }
            _clear_waiting_checkpoint(session)
            _save_trace(session, state, status="waiting_trial_result")
            if trace_log:
                trace_log.event(
                    "trial_script_handoff",
                    turn_id=state.turn_id,
                    source_name=source_request["source_name"],
                    script_name=source_request["script_name"],
                )
            return str(handoff["handoff_message"]), meta, total_in, total_out
        elif action == "self_continue":
            result = await fill(self_continue_form())
            decision, review = await supplement_outcome(
                pending_action={"action": action, "result": result}
            )
            if decision == "ask_user":
                return _supplement_question_result(session, state, review or {}, total_in, total_out)
            if decision == "pause_replacements":
                return _supplement_replacement_pause(session, state, total_in, total_out)
            if decision == "replace_requirement":
                state.analysis = await fill(requirement_form())
                state.results.append({"type": "requirement_revision", "result": state.analysis})
                continue
            if decision == "modify":
                continue
            state.results.append({"type": "model_work", "result": result})
        elif action == "ask_user":
            request = await fill(ask_form())
            decision, review = await supplement_outcome(
                pending_action={"action": action, "request": request}
            )
            if decision == "ask_user":
                return _supplement_question_result(session, state, review or {}, total_in, total_out)
            if decision == "pause_replacements":
                return _supplement_replacement_pause(session, state, total_in, total_out)
            if decision == "replace_requirement":
                state.analysis = await fill(requirement_form())
                state.results.append({"type": "requirement_revision", "result": state.analysis})
                continue
            if decision == "modify":
                continue
            if _asks_user_to_reprovide_known_asset(str(request["question"]), state):
                state.results.append(
                    {
                        "type": "runtime_refusal",
                        "error": "KNOWN_ASSET_MUST_BE_USED_FIRST",
                        "meaning": "当前会话已有唯一高置信素材候选；先使用或检查该素材，不要让用户重复提供。",
                        "asset_candidates": state.asset_candidates,
                    }
                )
                continue
            chips = [str(x) for x in request.get("choices") or []][:3]
            question_id = _save_waiting_checkpoint(
                session, state, question=str(request["question"]), choices=chips
            )
            meta = {
                "turn": "ask",
                "ask": "user_choice",
                "chips": chips,
                "plan": [],
                "task_done": False,
                "v4_resume_main_turn": True,
                "v4_main_turn_id": state.turn_id,
                "v4_question_id": question_id,
                "_trace": [f"V4 主轮 {state.turn_id} · {state.subturns} 副轮"],
            }
            _save_trace(session, state, status="waiting_user")
            if trace_log:
                trace_log.event("main_turn_waiting_user", turn_id=state.turn_id, question=request["question"])
            return str(request["question"]), meta, total_in, total_out
        elif action == "submit_response":
            response = await fill(submit_form())
            decision, review = await supplement_outcome(
                pending_action={"action": action, "response": response}
            )
            if decision == "ask_user":
                return _supplement_question_result(session, state, review or {}, total_in, total_out)
            if decision == "pause_replacements":
                return _supplement_replacement_pause(session, state, total_in, total_out)
            if decision == "replace_requirement":
                state.analysis = await fill(requirement_form())
                state.results.append({"type": "requirement_revision", "result": state.analysis})
                continue
            if decision == "modify":
                continue
            refusal = _visual_submit_refusal(state, response)
            if refusal is not None:
                state.results.append(refusal)
                if trace_log:
                    trace_log.event(
                        "submission_refused",
                        level="WARNING",
                        turn_id=state.turn_id,
                        subturn=state.subturns,
                        refusal=refusal,
                    )
                continue
            meta = {
                "turn": "stop",
                "plan": [],
                "task_done": bool(response["completed"]),
                "_trace": [f"V4 主轮 {state.turn_id} · {state.subturns} 副轮"],
            }
            _clear_waiting_checkpoint(session)
            final_status = "completed" if response["completed"] else "incomplete"
            _save_trace(session, state, status=final_status)
            if trace_log:
                trace_log.event(
                    "main_turn_completed", turn_id=state.turn_id, subturns=state.subturns, response=response
                )
            return str(response["text"]), meta, total_in, total_out

        after = _fingerprint(state)
        state.no_progress = state.no_progress + 1 if before == after else 0
        if state.no_progress >= MAX_NO_PROGRESS:
            break

    _clear_waiting_checkpoint(session)
    _save_trace(session, state, status="paused")
    if trace_log:
        trace_log.event(
            "main_turn_paused", level="WARNING", turn_id=state.turn_id, subturns=state.subturns, no_progress=state.no_progress
        )
    return (
        "本轮已达到 V4 的副轮或无进展上限，已保存诊断信息并暂停。",
        {
            "turn": "stop",
            "plan": [],
            "task_done": False,
            "_trace": [f"V4 主轮 {state.turn_id} 已暂停 · {state.subturns} 副轮"],
        },
        total_in,
        total_out,
    )


def _allowed_actions(state: TurnState) -> list[str]:
    allowed = ["self_continue", "query_local", "temporary_script", "ask_user", "submit_response"]
    try:
        from backend.script_generator.collaborator.reusable_tools import load_tool_defs

        if load_tool_defs():
            allowed.insert(3, "load_trial_script")
    except Exception:
        pass
    if state.contracts:
        allowed.insert(2, "invoke_capability")
    if state.subturns >= MAX_SUBTURNS - 5:
        return [name for name in allowed if name in {"ask_user", "submit_response"}]
    # A unique high-confidence reference should be consumed before asking the
    # user to upload or identify the same asset again.
    high_ids = {
        str(item.get("asset_id") or "")
        for item in state.asset_candidates
        if item.get("confidence_level") == "high"
    }
    bound_ids = {
        str(item.get("asset_id") or "")
        for item in (state.analysis.get("asset_bindings") or [])
        if isinstance(item, dict)
    }
    if len(high_ids) == 1 and not (high_ids & bound_ids):
        allowed = [name for name in allowed if name != "ask_user"]
    return allowed


def _asks_user_to_reprovide_known_asset(question: str, state: TurnState) -> bool:
    high = [item for item in state.asset_candidates if item.get("confidence_level") == "high"]
    if not high:
        return False
    normalized = re.sub(r"\s+", "", str(question or "").casefold())
    request_words = ("提供", "上传", "发来", "发一下", "放到", "拖到", "切换到", "哪张图", "哪个素材")
    object_words = ("图片", "图像", "截图", "素材", "共同视口", "文件")
    return any(word in normalized for word in request_words) and any(word in normalized for word in object_words)


def _save_trace(session, state: TurnState, *, status: str) -> None:
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    session.artifacts["v4_last_turn"] = {
        "turn_id": state.turn_id,
        "status": status,
        "user_text": state.user_text,
        "analysis": state.analysis,
        "subturns": state.subturns,
        "results": _json_safe(state.results[-20:]),
    }
    if status in {"completed", "incomplete", "paused", "waiting_trial_result"}:
        _update_unfinished_requirements(session, state, status=status)
