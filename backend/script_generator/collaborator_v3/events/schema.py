"""统一事件信封（设计 §18.2）。

> ✅ **已接入生产路径（2026-09-28 复核更正）**：本模块原先标着"全仓零 import"，
> 那是 Phase 2 审计时的状态，**之后已经接线**，标记属过期信息。现状：
>
> * 写侧——``runtime/runner.py::_emit_event`` 是主轮/工作区事件的**唯一**写入口，
>   走 ``EventEnvelope.create`` → ``EventStore.append`` → SQLite ``domain_events``；
>   ``runtime/trial_runs.py::_emit`` 是试运行生命周期的出口，由
>   ``bootstrap.initialize_v3`` 把 ``EventStore(store).append`` 装成 sink；
> * 读侧——``EventStore.for_turn / for_subturn / for_run`` 目前**只有测试在用**，
>   生产读取仍走 ``sqlite_store.list_events*``。这是有意的（不为了"统一"去改
>   已经在用的读路径），但要知道：信封的读能力还没被生产消费；
> * 尚未实现——设计 §18.1 里的 ``bus.py``（本进程发布/订阅）没有做，
>   现在只有"写入 + 按需读取"，没有订阅者。

定位：**不新建第二张事件表**。既有 ``domain_events`` 就是本地事件存储，
本模块只负责给它一套结构化信封 + 规范事件名，并作为唯一的事件读写入口
（``events/store.py::EventStore``）。

两个名字并存是刻意的，也是迁移期的兼容要求：

- ``type`` 列里放的仍然是**既有字面量**（``MainTurnStarted`` / ``ActionRequested`` …）。
  观测性测试、GUI 和 runtime 日志都按这些字面量读，换名会让它们静默失效。
- :class:`EventType` 提供**规范名**（``turn.started`` / …），由
  :data:`LEGACY_EVENT_ALIASES` 映射得到，供投影、诊断摘要和新查询使用。

于是"事件名"只有一份映射表，不需要各处硬编码字符串。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

#: 信封结构版本。
EVENT_SCHEMA_VERSION = 1

#: 走到 ``domain_events.payload_json`` 时，装"payload 之外的信封字段"的保留键。
#: 这些字段在旧表里没有列，丢掉它们会让 subject / evidence_refs / trial_run_id
#: 无法回读；用保留键保存，不认识它的旧读取方只会忽略。
ENVELOPE_META_KEY = "_envelope"


class EventType(str, Enum):
    """规范事件名（设计 §18.2 的关键领域动作，先覆盖低频的）。"""

    USER_MESSAGE_RECEIVED = "user.message_received"
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    PLAN_UPDATED = "plan.updated"
    LOGIC_CONFIRMED = "logic.confirmed"
    RESOURCE_ADDED = "resource.added"
    RESOURCE_FOCUSED = "resource.focused"
    QUERY_COMPLETED = "query.completed"
    QUERY_CACHE_REPLAYED = "query.cache_replayed"
    SCRIPT_CANDIDATE_CREATED = "script.candidate_created"
    SCRIPT_VALIDATED = "script.validated"
    SCRIPT_COMMITTED = "script.committed"
    TRIAL_STARTED = "trial.started"
    TRIAL_FINISHED = "trial.finished"
    RUNTIME_EXCEPTION = "runtime.exception"
    VISION_MATCH_COMPLETED = "vision.match_completed"
    INPUT_CLICK_ATTEMPTED = "input.click_attempted"
    INPUT_CLICK_COMPLETED = "input.click_completed"
    #: 试运行结构化遥测里由 Runtime 自己写的两类（设计 §19.4：关键帧只放引用；
    #: §19.5：未知 kind 原文照放成 note）。以前它们没有规范名，
    #: `canonical_event_type()` 对**本系统自己写的事件**返回 None。
    FRAME_CAPTURED = "frame.captured"
    SCRIPT_NOTE = "script.note"
    #: 主张落库（设计 §24.3：fact / user_claim / hypothesis 分层）。
    CLAIM_RECORDED = "claim.recorded"
    #: 契约凭据（Phase 1，设计 §5/§12）：模型**看过**契约 / 本地**批准**使用。
    #: 事件只引用 id（capability_id / ticket_id），整张票存在 contract_tickets 表里。
    CONTRACT_VIEWED = "contract.viewed"
    CALL_AUTHORIZED = "call.authorized"
    #: 影子对账（Phase 3 第一阶段）：交付路径上的**只读**分级报告，不阻断交付。
    SHADOW_RECONCILED = "shadow.reconciled"
    TURN_STARTED = "turn.started"
    TURN_PAUSED = "turn.paused"
    TURN_RESUMED = "turn.resumed"
    TURN_FINISHED = "turn.finished"
    #: 校验凭证签发（设计 §14.4：事件必须能追到 validation receipt）。
    VALIDATION_RECEIPT_ISSUED = "validation.receipt_issued"
    #: 第一层任务定向闸（设计 §3，Phase A）：任务类型判定 / 改判。
    #: 判定结论同时落在 ``main_turns.task_type / intent_json / intent_revision``，
    #: 事件只作为**时间线**（谁在哪个副轮把这一轮定向成了什么、依据是哪句用户原话）。
    TURN_INTENT_CLASSIFIED = "turn.intent_classified"
    TURN_INTENT_REVISED = "turn.intent_revised"
    #: 第二层A 统一资源绑定（设计 §4，Phase B）：绑定成功 / 结构化失败。
    #: 事件里只放资源事实（id/版本/内容 hash/类型/角色/状态/用途）与诊断码，不放整张票。
    RESOURCE_BOUND = "resource.bound"
    RESOURCE_BINDING_FAILED = "resource.binding_failed"
    #: 遥测（Phase B 收口）：旧工具没在 schema 里声明 ``x-resource``、只靠参数名命中
    #: 历史命名就被兼容绑定。这是**兼容层证据**，用来推动补 schema，不是失败。
    RESOURCE_SCHEMA_LEGACY_INFERRED = "resource.schema_legacy_inferred"
    #: 遥测（Phase B4 / 设计 §4.6）：参数声明了业务语义，但资源没声明任何语义 →
    #: 无法证明"不匹配"，因此不拦（宁可放过也不误伤）。同样是**兼容层证据**。
    RESOURCE_SEMANTIC_UNVERIFIED = "resource.semantic_unverified"
    #: 模型名称/旧 id + 可重复操作配方经 Runtime 合成为最终资源参数。
    REPEATABLE_OPERATION_RESOLVED = "resource.repeatable_operation_resolved"
    #: 视觉证据闸门（Phase B2 / 设计 §5）：缺兼容视觉证据时挡住了图片事实/逻辑草稿/回答。
    VISUAL_EVIDENCE_REQUIRED = "visual.evidence_required"
    #: Phase B3 影子图：把"自由循环实际动作"与"图允许动作"的比较结果留痕。
    #: **只报告**（payload 里 ``applied=false``）：不参与路由，也不写第二份事实。
    GRAPH_SHADOW_COMPARED = "graph.shadow_compared"
    GOAL_CONTRACT_UPDATED = "goal.contract_updated"
    TOOL_RESULT_REUSED = "tool.result_reused"


#: 既有字面量 → 规范名。左侧是**已经在库里/日志里出现过的名字**，不许改；
#: 右侧是新代码分类时用的规范名。新增一个字面量就必须在这里登记。
#:
#: ⚠️ 漏登记不会报错，只会让 `canonical_event_type()` 返回 `None` ——
#: 事件在投影/诊断里变成"未知类型"。所以有护栏测试
#: （`test_collab_v3_event_literals.py`）在扫生产代码里实际发出的字面量。
LEGACY_EVENT_ALIASES: dict[str, EventType] = {
    "MainTurnStarted": EventType.TURN_STARTED,
    "MainTurnResumed": EventType.TURN_RESUMED,
    "MainTurnPaused": EventType.TURN_PAUSED,
    "MainTurnFailed": EventType.RUNTIME_EXCEPTION,
    "MainTurnCommitted": EventType.TURN_FINISHED,
    "MainTurnCancelled": EventType.TURN_FINISHED,
    "ActionRequested": EventType.PLAN_UPDATED,
    "ActionSucceeded": EventType.PLAN_UPDATED,
    "ActionFailed": EventType.PLAN_UPDATED,
    "InteractionRequested": EventType.TURN_PAUSED,
    "LogicDraftConfirmed": EventType.LOGIC_CONFIRMED,
    "ValidationReceiptIssued": EventType.VALIDATION_RECEIPT_ISSUED,
    "QueryBatchCompleted": EventType.QUERY_COMPLETED,
    "TaskObjectiveRepaired": EventType.TASK_UPDATED,
    # 以下四个规范名早就在枚举里，但**字面量没登记** → 生产里写的这四类事件
    # 一直翻译不出来（枚举有名字、映射缺一条，属于"看起来做了"）。
    "QueryCacheReplayed": EventType.QUERY_CACHE_REPLAYED,
    "ScriptCandidateCreated": EventType.SCRIPT_CANDIDATE_CREATED,
    "ScriptValidated": EventType.SCRIPT_VALIDATED,
    "ScriptCommitted": EventType.SCRIPT_COMMITTED,
    # 主张落库（runner 的 ClaimRecorded）
    "ClaimRecorded": EventType.CLAIM_RECORDED,
    # 契约凭据（Phase 1）：旧式驼峰写法也登记，避免以后有人写它却翻译不出来。
    "ContractViewed": EventType.CONTRACT_VIEWED,
    "CallAuthorized": EventType.CALL_AUTHORIZED,
    "ShadowReconciled": EventType.SHADOW_RECONCILED,
    # 任务定向闸（Phase A）：字面量 → 规范名。
    "TurnIntentClassified": EventType.TURN_INTENT_CLASSIFIED,
    "TurnIntentRevised": EventType.TURN_INTENT_REVISED,
    # 统一资源绑定（Phase B）。
    "ResourceBound": EventType.RESOURCE_BOUND,
    "ResourceBindingFailed": EventType.RESOURCE_BINDING_FAILED,
    "ResourceSchemaLegacyInferred": EventType.RESOURCE_SCHEMA_LEGACY_INFERRED,
    "ResourceSemanticUnverified": EventType.RESOURCE_SEMANTIC_UNVERIFIED,
    "RepeatableOperationResolved": EventType.REPEATABLE_OPERATION_RESOLVED,
    # 视觉证据闸门（Phase B2）。
    "VisualEvidenceRequired": EventType.VISUAL_EVIDENCE_REQUIRED,
    # Phase B3 影子图（规范名本身就是写法：graph.shadow_compared）。
    "GraphShadowCompared": EventType.GRAPH_SHADOW_COMPARED,
    "GoalContractUpdated": EventType.GOAL_CONTRACT_UPDATED,
    "ToolResultReused": EventType.TOOL_RESULT_REUSED,
}

#: 规范名本身也接受（新写入方可以直接用规范名）。
_CANONICAL: dict[str, EventType] = {item.value: item for item in EventType}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_event_type(raw: str) -> EventType | None:
    """把库里/日志里的字面量翻译成规范名；两边都不认识时返回 ``None``
    （**不猜**：未知事件保持未知，不能塞进最近似的分类）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text in _CANONICAL:
        return _CANONICAL[text]
    return LEGACY_EVENT_ALIASES.get(text)


@dataclass(frozen=True)
class EventEnvelope:
    """一条领域事件的完整结构（设计 §18.2 的字段表）。"""

    event_id: str
    event_type: str
    occurred_at: str = ""
    schema_version: int = EVENT_SCHEMA_VERSION
    task_id: str = ""
    main_turn_id: str = ""
    run_id: str = ""
    subturn_id: str = ""
    action_id: str = ""
    trial_run_id: str = ""
    subject: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    #: 写入 ``domain_events.aggregate_id`` 的值（旧行的语义，保持原样传递）。
    aggregate_id: str = ""

    # ── 构造 ──────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        event_type: str | EventType,
        payload: Mapping[str, Any] | None = None,
        *,
        event_id: str = "",
        task_id: str = "",
        main_turn_id: str = "",
        run_id: str = "",
        subturn_id: str = "",
        action_id: str = "",
        trial_run_id: str = "",
        subject: Mapping[str, Any] | None = None,
        evidence_refs: Iterable[str] = (),
        aggregate_id: str = "",
        occurred_at: str = "",
        schema_version: int = EVENT_SCHEMA_VERSION,
    ) -> "EventEnvelope":
        return cls(
            event_id=str(event_id or _new_event_id()),
            event_type=event_type.value if isinstance(event_type, EventType) else str(event_type),
            occurred_at=str(occurred_at or _now()),
            schema_version=int(schema_version),
            task_id=str(task_id or ""),
            main_turn_id=str(main_turn_id or ""),
            run_id=str(run_id or ""),
            subturn_id=str(subturn_id or ""),
            action_id=str(action_id or ""),
            trial_run_id=str(trial_run_id or ""),
            subject=dict(subject or {}),
            payload=dict(payload or {}),
            evidence_refs=tuple(str(x) for x in evidence_refs),
            aggregate_id=str(aggregate_id or ""),
        )

    # ── 判定 ──────────────────────────────────────────────────────────

    @property
    def canonical_type(self) -> EventType | None:
        return canonical_event_type(self.event_type)

    @property
    def is_legacy(self) -> bool:
        """缺层级 ID 的行只能是 legacy（设计 §28）：**不得**当成新式可信记录。

        判据只看"有没有 run/subturn 层级"，缺字段就是缺，绝不在读的时候补齐。
        """
        return not (self.run_id and (self.subturn_id or self.action_id))

    def resolve_aggregate_id(self) -> str:
        """写入旧列时用的聚合 id：显式值优先，否则按 action → turn → task 降级。"""
        return (
            self.aggregate_id
            or self.action_id
            or self.main_turn_id
            or self.task_id
            or self.event_id
        )

    # ── 序列化 ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "task_id": self.task_id,
            "main_turn_id": self.main_turn_id,
            "run_id": self.run_id,
            "subturn_id": self.subturn_id,
            "action_id": self.action_id,
            "trial_run_id": self.trial_run_id,
            "aggregate_id": self.aggregate_id,
            "subject": dict(self.subject),
            "payload": dict(self.payload),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EventEnvelope":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        values = {k: v for k, v in dict(data or {}).items() if k in known}
        values["subject"] = dict(values.get("subject") or {})
        values["payload"] = dict(values.get("payload") or {})
        values["evidence_refs"] = tuple(str(x) for x in values.get("evidence_refs") or ())
        return cls(**values)  # type: ignore[arg-type]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "EventEnvelope":
        return cls.from_dict(json.loads(text or "{}"))

    # ── 与既有 domain_events 行互转 ────────────────────────────────────

    def to_legacy_columns(self) -> dict[str, Any]:
        """映射到 ``SqliteDomainStore.append_event`` 的入参（含层级 ID 列）。"""
        payload = dict(self.payload)
        meta: dict[str, Any] = {}
        if self.trial_run_id:
            meta["trial_run_id"] = self.trial_run_id
        if self.subject:
            meta["subject"] = dict(self.subject)
        if self.evidence_refs:
            meta["evidence_refs"] = list(self.evidence_refs)
        if int(self.schema_version) != EVENT_SCHEMA_VERSION:
            meta["schema_version"] = int(self.schema_version)
        if meta:
            payload[ENVELOPE_META_KEY] = meta
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "task_id": self.task_id or None,
            "turn_id": self.main_turn_id or None,
            "aggregate_id": self.resolve_aggregate_id(),
            "run_id": self.run_id or None,
            "subturn_id": self.subturn_id or None,
            "action_id": self.action_id or None,
            "payload": payload,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EventEnvelope":
        """从 ``list_events`` 的行还原信封（包含 legacy 行）。"""
        payload = dict(row.get("payload") or {})
        meta = payload.pop(ENVELOPE_META_KEY, None)
        meta = dict(meta) if isinstance(meta, Mapping) else {}
        return cls(
            event_id=str(row.get("id") or ""),
            event_type=str(row.get("type") or ""),
            occurred_at=str(meta.get("occurred_at") or row.get("created_at") or ""),
            schema_version=int(meta.get("schema_version") or EVENT_SCHEMA_VERSION),
            task_id=str(row.get("task_id") or ""),
            main_turn_id=str(row.get("turn_id") or ""),
            run_id=str(row.get("run_id") or ""),
            subturn_id=str(row.get("subturn_id") or ""),
            action_id=str(row.get("action_id") or ""),
            trial_run_id=str(meta.get("trial_run_id") or ""),
            subject=dict(meta.get("subject") or {}),
            payload=payload,
            evidence_refs=tuple(str(x) for x in (meta.get("evidence_refs") or ())),
            aggregate_id=str(row.get("aggregate_id") or ""),
        )


def _new_event_id() -> str:
    """``evt_<16 hex>``：本地生成、只用于身份，不参与业务判断。"""
    from uuid import uuid4

    return "evt_" + uuid4().hex[:16]
