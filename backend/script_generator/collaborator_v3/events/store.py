"""事件的唯一读写入口（设计 §18.1/§18.2）。

> ✅ **已接入生产路径（2026-09-28 复核更正）**：本模块原先标着"除本包内部外全仓零 import"，
> 那是 Phase 2 审计时的状态，**之后已经接线**，标记属过期信息。现状：
>
> * 写侧已接入两处——``runtime/runner.py::_emit_event``（主轮/工作区事件的唯一写入口）
>   与 ``runtime/trial_runs.py::_emit``（经 ``bootstrap.initialize_v3`` 装入的 sink）；
> * 读侧（``for_turn`` / ``for_subturn`` / ``for_run``）**目前只有测试在用**，
>   生产读取仍走 ``sqlite_store.list_events*``；这是有意保留的，不是遗漏。
>
> 一句话：**不新建第二张事件表，也不要各处自己 INSERT**。
> ``EventStore`` 把 :class:`EventEnvelope` 落到既有的 ``domain_events``（含层级 ID 列），
> 并把行还原成信封，legacy 行保持 legacy、绝不补齐字段冒充新记录。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..stores.sqlite_store import SqliteDomainStore
from .schema import EventEnvelope, EventType


class EventStore:
    """围绕 ``domain_events`` 的薄封装（append / 下钻读取）。"""

    def __init__(self, domain_store: SqliteDomainStore) -> None:
        self.domain = domain_store

    # ── 写 ────────────────────────────────────────────────────────────

    def append(self, envelope: EventEnvelope) -> EventEnvelope:
        """写一条事件；返回写下去的那个信封（便于调用方继续串联）。"""
        self.domain.append_event(**envelope.to_legacy_columns())
        return envelope

    def emit(
        self,
        event_type: str | EventType,
        payload: Mapping[str, Any] | None = None,
        **ids: Any,
    ) -> EventEnvelope:
        """便捷入口：``emit("trial.started", {"run": ...}, task_id=..., trial_run_id=...)``。"""
        return self.append(EventEnvelope.create(event_type, payload, **ids))

    # ── 读 ────────────────────────────────────────────────────────────

    def for_turn(self, turn_id: str) -> list[EventEnvelope]:
        return self._wrap(self.domain.list_events(turn_id=turn_id))

    def for_subturn(self, subturn_id: str) -> list[EventEnvelope]:
        return self._wrap(self.domain.list_events_for_subturn(subturn_id))

    def for_run(self, run_id: str) -> list[EventEnvelope]:
        return self._wrap(self.domain.list_events_for_run(run_id))

    @staticmethod
    def _wrap(rows: Iterable[Mapping[str, Any]]) -> list[EventEnvelope]:
        return [EventEnvelope.from_row(row) for row in rows]
