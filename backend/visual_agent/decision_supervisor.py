"""Persistent, low-frequency decision session for the visual executor.

The executor remains the only UI writer.  This module sees compact semantic
events and reviews completion proposals; it never emits clicks or coordinates.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable


SUPERVISOR_PROMPT = """你是视觉执行任务的异步决策监督层，不操作界面，也不输出点击坐标。
你的职责是理解用户真正想完成的结果、维护可验证的子目标、指出执行层遗漏，并审核候选交付。
只输出一个 JSON 对象，不要输出 Markdown 或思维过程。不能把执行层的主张当成事实；区分可见事实、推断和未知。
首次分析格式：
{"kind":"contract","goal":"...","subgoals":[{"id":"...","description":"...","done_when":["..."]}],"required_final_evidence":["..."],"ambiguities":[],"risks":[],"guidance":[]}
审核格式：
{"kind":"verdict","verdict":"approve|reject|expand|unknown","reason":"...","missing":[],"next_requirements":[],"task_patch":[]}
途中异常审计格式：
{"kind":"guidance","reason":"...","guidance":["应达到的下一结果或应避免的错误"],"interrupt":false}
冲突自检格式：
{"kind":"self_check","reason":"...","previous_claim_status":"withdraw|retain|uncertain","fresh_evidence_used":true,"evidence_assessment":["只描述新取得证据中可见的内容"],"guidance":["基于新证据的结果层要求"]}
冲突分支复核格式：
{"kind":"conflict_branch","status":"continue|resolved|unknown","reason":"...","fresh_evidence_used":true,"evidence_assessment":["本轮新证据"],"guidance":["分支未结束时的下一结果要求"]}
approve 仅表示执行记录与证据已经覆盖全部目标；单次成功不能证明“全部/打完”，选中目标也不能证明后续攻略完成。
只有必须由用户决定且无法安全查证时才用 unknown。
发生冲突时不得引用冲突前的截图或原有主张来证明自己；只能使用本次冲突后重新取得并随请求附带的新证据。"""


DECISION_FIELD_PROTOCOLS: dict[str, dict[str, dict[str, Any]]] = {
    "bootstrap": {
        "kind": {"description": "固定的首次合同类型", "type": "string", "enum": ["contract"]},
        "goal": {"description": "用户最终想达成的结果", "type": "string"},
        "subgoals": {"description": "可观测验证的子目标", "type": "array<subgoal>"},
        "subgoals[].id": {"description": "稳定的子目标 id", "type": "string"},
        "subgoals[].description": {"description": "子目标结果", "type": "string"},
        "subgoals[].done_when": {"description": "可见完成证据", "type": "array<string>"},
        "required_final_evidence": {"description": "最终交付必需证据", "type": "array<string>"},
        "ambiguities": {"description": "待验证歧义", "type": "array<string>"},
        "risks": {"description": "任务风险", "type": "array<string>"},
        "guidance": {"description": "结果层指导，不得包含坐标", "type": "array<string>"},
    },
    "progress_anomaly_review": {
        "kind": {"description": "固定的途中纠偏类型", "type": "string", "enum": ["guidance"]},
        "reason": {"description": "纠偏原因", "type": "string"},
        "guidance": {"description": "新的结果层要求", "type": "array<string>"},
        "interrupt": {"description": "是否要求执行层立即停止当前方向", "type": "boolean", "enum": [True, False]},
    },
    "completion_review": {
        "kind": {"description": "固定的完成审核类型", "type": "string", "enum": ["verdict"]},
        "verdict": {
            "description": "对候选交付的裁决",
            "type": "string", "enum": ["approve", "reject", "expand", "unknown"],
        },
        "reason": {"description": "裁决原因", "type": "string"},
        "missing": {"description": "缺失证据", "type": "array<string>"},
        "next_requirements": {"description": "后续必须满足的结果", "type": "array<string>"},
        "task_patch": {"description": "对任务合同的结构化修订；无修订填空数组", "type": "array<object>"},
    },
    "conflict_self_check": {
        "kind": {"description": "固定的冲突自检类型", "type": "string", "enum": ["self_check"]},
        "reason": {"description": "根据新证据重新检查原判断的理由", "type": "string"},
        "previous_claim_status": {
            "description": "原判断在新证据下的状态", "type": "string",
            "enum": ["withdraw", "retain", "uncertain"],
        },
        "fresh_evidence_used": {"description": "必须确认只使用冲突后新证据", "type": "boolean", "enum": [True]},
        "evidence_assessment": {"description": "新证据中直接可见的内容", "type": "array<string>"},
        "guidance": {"description": "基于新证据的结果层要求", "type": "array<string>"},
    },
    "conflict_branch_review": {
        "kind": {"description": "固定的冲突分支复核类型", "type": "string", "enum": ["conflict_branch"]},
        "status": {"description": "临时分支状态", "type": "string", "enum": ["continue", "resolved", "unknown"]},
        "reason": {"description": "仅根据本轮新证据作出的判断", "type": "string"},
        "fresh_evidence_used": {"description": "必须确认使用本轮新证据", "type": "boolean", "enum": [True]},
        "evidence_assessment": {"description": "本轮新证据中直接可见的内容", "type": "array<string>"},
        "guidance": {"description": "分支继续时执行层必须满足的下一结果", "type": "array<string>"},
    },
}


def _require_string_list(value: dict[str, Any], field: str) -> None:
    raw = value.get(field)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} 必须是字符串数组")


def _validate_decision_reply(value: dict[str, Any], request: str) -> None:
    protocol = DECISION_FIELD_PROTOCOLS.get(request)
    if protocol is None:
        raise ValueError(f"未知决策请求类型：{request}")
    expected_kind = protocol["kind"]["enum"]
    if value.get("kind") not in expected_kind:
        raise ValueError(f"kind 只能填 {expected_kind}，实际为 {value.get('kind')!r}")
    if not isinstance(value.get("reason" if request != "bootstrap" else "goal"), str):
        raise ValueError("缺少必需的文本理由或目标")
    if request == "bootstrap":
        subgoals = value.get("subgoals")
        if not isinstance(subgoals, list):
            raise ValueError("subgoals 必须是数组")
        for index, subgoal in enumerate(subgoals):
            if not isinstance(subgoal, dict):
                raise ValueError(f"subgoals[{index}] 必须是对象")
            if not isinstance(subgoal.get("id"), str) or not isinstance(subgoal.get("description"), str):
                raise ValueError(f"subgoals[{index}] 缺少字符串 id/description")
            _require_string_list(subgoal, "done_when")
        for field in ("required_final_evidence", "ambiguities", "risks", "guidance"):
            _require_string_list(value, field)
    elif request == "progress_anomaly_review":
        _require_string_list(value, "guidance")
        if not isinstance(value.get("interrupt"), bool):
            raise ValueError("interrupt 必须是 boolean")
    elif request == "conflict_self_check":
        if value.get("previous_claim_status") not in {"withdraw", "retain", "uncertain"}:
            raise ValueError("previous_claim_status 非法")
        if value.get("fresh_evidence_used") is not True:
            raise ValueError("冲突自检必须确认使用冲突后新证据")
        _require_string_list(value, "evidence_assessment")
        _require_string_list(value, "guidance")
    elif request == "conflict_branch_review":
        if value.get("status") not in {"continue", "resolved", "unknown"}:
            raise ValueError("冲突分支 status 非法")
        if value.get("fresh_evidence_used") is not True:
            raise ValueError("冲突分支复核必须使用本轮新证据")
        _require_string_list(value, "evidence_assessment")
        _require_string_list(value, "guidance")
    else:
        verdicts = protocol["verdict"]["enum"]
        if value.get("verdict") not in verdicts:
            raise ValueError(f"verdict 只能填 {verdicts}")
        for field in ("missing", "next_requirements"):
            _require_string_list(value, field)
        if not isinstance(value.get("task_patch"), list):
            raise ValueError("task_patch 必须是数组")


def _json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("决策层没有返回 JSON 对象")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("决策层结果不是对象")
    return value


def compact_execution(history: list[dict[str, Any]], *, limit: int = 24) -> dict[str, Any]:
    """Convert step chatter into a bounded evidence-oriented delta.

    Repeated waits are aggregated, while verification failures, guards and
    semantic state changes are retained.  The original history remains the
    source of truth on disk.
    """
    events: list[dict[str, Any]] = []
    waits = 0
    wait_seconds = 0.0

    def flush_waits() -> None:
        nonlocal waits, wait_seconds
        if waits:
            events.append({"type": "wait_interval", "count": waits,
                           "seconds": round(wait_seconds, 2)})
            waits = 0
            wait_seconds = 0.0

    for row in history:
        action = str(row.get("action") or "")
        if action == "wait":
            waits += 1
            wait_seconds += float(row.get("seconds") or 0)
            continue
        flush_waits()
        item = {
            "step": row.get("step"),
            "type": action or "event",
            "message": str(row.get("message") or "")[:240],
            "result": str(row.get("result") or "")[:80],
        }
        evidence = row.get("evidence")
        if isinstance(evidence, list) and evidence:
            item["evidence"] = [str(x)[:240] for x in evidence[:6]]
        events.append(item)
    flush_waits()
    dropped = max(0, len(events) - limit)
    return {"events": events[-limit:], "dropped_event_groups": dropped,
            "source_step_count": len(history)}


class DecisionSupervisor:
    """One persistent model conversation spanning one executor run."""

    def __init__(self, *, task: str, local_contract: dict[str, Any], model: dict[str, Any],
                 call_model: Callable[..., Awaitable[tuple[str, int, int]]],
                 on_log: Callable[[str], None] | None = None,
                 on_usage: Callable[[int, int], None] | None = None):
        self.task = task
        self.local_contract = dict(local_contract)
        self.model = model
        self._call_model = call_model
        self._on_log = on_log or (lambda _text: None)
        self._on_usage = on_usage or (lambda _tin, _tout: None)
        self._messages: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._bootstrap_task: asyncio.Task | None = None
        self._progress_task: asyncio.Task | None = None
        self.contract: dict[str, Any] = {}
        self.guidance: list[str] = []
        self.conflict_branch: dict[str, Any] | None = None
        self.failed = False

    def start(self) -> None:
        if self._bootstrap_task is None:
            self._bootstrap_task = asyncio.create_task(self._bootstrap())

    async def close(self) -> None:
        for task in (self._bootstrap_task, self._progress_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "ready": bool(self.contract),
            "contract": self.contract,
            "active_guidance": self.guidance[-6:],
            "conflict_takeover": self.conflict_branch,
            "failed": self.failed,
        }

    async def _turn(self, payload: dict[str, Any], *, image: str | None = None,
                    isolated: bool = False) -> dict[str, Any]:
        async with self._lock:
            request = str(payload.get("request") or "")
            payload = dict(payload)
            payload["strict_json_field_protocol"] = DECISION_FIELD_PROTOCOLS.get(request, {})
            payload["protocol_instruction"] = (
                "description 是填写注释；enum/type 必须严格遵守；"
                "不得新增枚举值，不确定内容写入对应的 ambiguities/missing。"
            )
            content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
            if image:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}})
            user_message = {"role": "user", "content": content}
            attempt_message = user_message
            for attempt in range(3):
                prior_messages = [] if isolated else self._messages
                text, tin, tout = await self._call_model(
                    provider=self.model["provider"], api_key=self.model["api_key"],
                    model=self.model["model"], api_endpoint=self.model.get("endpoint") or None,
                    messages=prior_messages + [attempt_message], system_prompt=SUPERVISOR_PROMPT,
                    max_tokens=min(max(int(self.model.get("max_tokens") or 1400), 900), 3000),
                )
                self._on_usage(int(tin or 0), int(tout or 0))
                try:
                    value = _json_object(text)
                    _validate_decision_reply(value, request)
                except Exception as exc:
                    if attempt >= 2:
                        raise ValueError(f"决策层协议修复失败：{exc}") from exc
                    attempt_message = {
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": (
                                f"上一份 JSON 未通过严格字段校验：{exc}\n"
                                f"上一份内容：{text!r}\n"
                                "不要重新分析，只按 strict_json_field_protocol 修正同一决定。"
                            ),
                        }],
                    }
                    continue
                self._messages.extend([user_message, {"role": "assistant", "content": text}])
                return value
            raise RuntimeError("决策层协议修复未返回结果")

    async def _bootstrap(self) -> None:
        try:
            value = await self._turn({
                "request": "bootstrap",
                "user_task": self.task,
                "local_safety_and_completion_hint": self.local_contract,
                "instruction": "建立结果导向、可观察验证的任务契约；不要规划点击路线。",
            })
            if value.get("kind") != "contract":
                raise ValueError("首次结果 kind 不是 contract")
            self.contract = value
            self.guidance = [str(x) for x in value.get("guidance") or [] if str(x).strip()]
            self._on_log(f"决策层任务契约已就绪：{len(value.get('subgoals') or [])} 个子目标")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed = True
            self._on_log(f"决策层初始化失败，执行层继续运行：{type(exc).__name__}: {exc}")

    async def review_completion(self, *, claimed: dict[str, Any], history: list[dict[str, Any]],
                                runtime_facts: dict[str, Any], image: str | None) -> dict[str, Any] | None:
        if self._bootstrap_task is None:
            self.start()
        try:
            await self._bootstrap_task
            if self.failed or not self.contract:
                return None
            value = await self._turn({
                "request": "completion_review",
                "user_task": self.task,
                "frozen_contract": self.contract,
                "completion_proposal": claimed,
                "compressed_execution": compact_execution(history),
                "runtime_facts": runtime_facts,
                "instruction": "逐项核对子目标。截图只证明当前画面；执行记录只证明实际记录的过程。",
            }, image=image)
            if value.get("kind") != "verdict":
                raise ValueError("审核结果 kind 不是 verdict")
            verdict = str(value.get("verdict") or "unknown").lower()
            if verdict not in {"approve", "reject", "expand", "unknown"}:
                raise ValueError(f"未知 verdict: {verdict}")
            requirements = [str(x) for x in value.get("next_requirements") or [] if str(x).strip()]
            if requirements:
                self.guidance = requirements[-6:]
            return value
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_log(f"决策层完成审核失败，回退到原验证器：{type(exc).__name__}: {exc}")
            return None

    async def review_conflict(self, *, conflict: dict[str, Any], history: list[dict[str, Any]],
                              fresh_image: str, evidence_ref: dict[str, Any]) -> dict[str, Any] | None:
        """Re-check a disputed claim against a capture made after the conflict."""
        if self._bootstrap_task is None:
            self.start()
        try:
            await self._bootstrap_task
            if self.failed or not self.contract:
                return None
            value = await self._turn({
                "request": "conflict_self_check",
                "user_task": self.task,
                "frozen_contract": self.contract,
                "conflict": conflict,
                "fresh_evidence": evidence_ref,
                "instruction": (
                    "先重新检查自己的判断。只能依据随本请求附带、且在冲突发生后重新抓取的截图；"
                    "不得用冲突前截图、旧摘要或原判断自证。新证据不足时必须填 uncertain。"
                ),
            }, image=fresh_image, isolated=True)
            guidance = [str(x) for x in value.get("guidance") or [] if str(x).strip()]
            if guidance:
                self.guidance = guidance[-6:]
            self.conflict_branch = {
                "active": True,
                "conflict": conflict,
                "guidance": guidance[-6:],
                "basis": [str(x) for x in value.get("evidence_assessment") or []],
            }
            return value
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_log(f"决策层冲突自检失败，保留冲突：{type(exc).__name__}: {exc}")
            return None

    async def review_conflict_branch(self, *, history: list[dict[str, Any]], fresh_image: str,
                                     evidence_ref: dict[str, Any]) -> dict[str, Any] | None:
        """Review one executor step while the decision layer temporarily owns the branch."""
        if not self.conflict_branch:
            return None
        try:
            value = await self._turn({
                "request": "conflict_branch_review",
                "user_task": self.task,
                "conflict_branch": self.conflict_branch,
                "fresh_evidence": evidence_ref,
                "latest_execution_event": compact_execution(history, limit=3),
                "instruction": (
                    "只用随本请求附带的新截图判断冲突分支是否完成。"
                    "resolved 后控制权返回正常执行；证据不足填 unknown，不得拿旧图自证。"
                ),
            }, image=fresh_image, isolated=True)
            status = str(value.get("status") or "unknown")
            guidance = [str(x) for x in value.get("guidance") or [] if str(x).strip()]
            if status == "resolved":
                self.conflict_branch = None
            else:
                self.conflict_branch["guidance"] = guidance[-6:]
                self.conflict_branch["basis"] = [
                    str(x) for x in value.get("evidence_assessment") or []
                ]
                if guidance:
                    self.guidance = guidance[-6:]
            return value
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_log(f"决策层冲突分支复核失败，维持临时接管：{type(exc).__name__}: {exc}")
            return None

    def notify_anomaly(self, *, anomaly: dict[str, Any], history: list[dict[str, Any]]) -> None:
        """Schedule one non-blocking audit; repeated anomalies coalesce."""
        if self.failed or (self._progress_task and not self._progress_task.done()):
            return
        self._progress_task = asyncio.create_task(self._review_progress(anomaly, list(history)))

    async def _review_progress(self, anomaly: dict[str, Any], history: list[dict[str, Any]]) -> None:
        try:
            if self._bootstrap_task is None:
                self.start()
            await self._bootstrap_task
            if self.failed or not self.contract:
                return
            value = await self._turn({
                "request": "progress_anomaly_review",
                "user_task": self.task,
                "frozen_contract": self.contract,
                "anomaly": anomaly,
                "compressed_execution": compact_execution(history),
                "instruction": "只给结果层纠偏，不给点击坐标，不直接操作界面。",
            })
            if value.get("kind") != "guidance":
                raise ValueError("异常审计结果 kind 不是 guidance")
            guidance = [str(x) for x in value.get("guidance") or [] if str(x).strip()]
            if guidance:
                self.guidance = guidance[-6:]
                self._on_log("决策层已更新途中纠偏：" + "；".join(self.guidance))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._on_log(f"决策层异常审计失败，执行层继续运行：{type(exc).__name__}: {exc}")
