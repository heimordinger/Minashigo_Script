"""Thin GUI-facing facade.  It owns no domain truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..domain.enums import RuntimePhase, TurnStatus
from ..domain.errors import ErrorCode
from ..domain.models import MainTurnRecord, ResourceRef, ViewportSnapshot
from ..runtime.runner import ActionRunner


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_turn_id() -> str:
    """后端签发的**层级主轮 ID**（设计 §14.2）。

    新记录使用 ``mt_<UTCstamp>_<hex>``；``ids`` 形状校验失败时退回旧
    ``turn_<uuid>`` 形式（老记录仍然可读，且不伪造成新式可信 ID）。
    """
    try:
        from ..runtime import ids as id_gen

        return id_gen.new_main_turn_id()
    except Exception:  # noqa: BLE001
        return f"turn_{uuid4().hex}"


@dataclass(frozen=True)
class ViewUpdate:
    task_id: str | None
    turn_id: str
    status: str
    status_text: str
    messages: tuple[dict[str, Any], ...] = ()
    interaction: dict[str, Any] | None = None
    trial_page_ready: bool = False
    checkpoint_id: str | None = None
    resume_token: str | None = None
    logic_draft: tuple[dict[str, Any], ...] = ()
    final_response: dict[str, Any] | None = None
    pause_reason: str | None = None


class V3RuntimeFacade:
    def __init__(self, runner: ActionRunner) -> None:
        self.runner = runner

    def send_message(
        self,
        text: str,
        *,
        task_id: str | None = None,
        viewport_revision: int = 1,
        base: ResourceRef | None = None,
        overlays: tuple[ResourceRef, ...] = (),
        zoom: float = 1.0,
        pan: tuple[float, float] = (0.0, 0.0),
        selections: tuple[ResourceRef, ...] = (),
        session_asset_ids: tuple[str, ...] = (),
        execution_target: dict[str, Any] | None = None,
        baseline_script: str = "",
        change_policy: str = "",
    ) -> ViewUpdate:
        turn_id = _new_turn_id()
        snapshot = ViewportSnapshot(
            id=f"snapshot_{uuid4().hex}", turn_id=turn_id, revision=viewport_revision,
            base=base, overlays=overlays, zoom=zoom, pan_x=pan[0], pan_y=pan[1],
            selections=selections, captured_at=_now(),
        )
        self.runner.store.put_viewport_snapshot(snapshot)
        turn = MainTurnRecord(
            id=turn_id, task_id=task_id, user_event_id=f"ue_{uuid4().hex}",
            status=TurnStatus.CREATED, phase=RuntimePhase.INTAKE,
            active_snapshot_id=snapshot.id, started_at=_now(), updated_at=_now(),
        )
        result = self.runner.run(
            turn=turn,
            user_text=text,
            session_asset_ids=session_asset_ids,
            execution_target=execution_target,
            # "和哪一版比"与"限制怎么改"是两个独立事实（设计 §6.2/§15）；
            # 只在明确知道参考脚本时由调用方给出，不知道就传空，由 Runtime 自己取上次交付。
            baseline_script=baseline_script,
            change_policy=change_policy,
        )
        return self._to_view_update(turn_id, result)

    def resume(
        self,
        *,
        turn_id: str,
        text: str,
        resume_token: str | None = None,
        session_asset_ids: tuple[str, ...] = (),
        option_id: str = "",
        execution_target: dict[str, Any] | None = None,
        baseline_script: str = "",
        change_policy: str = "",
    ) -> ViewUpdate:
        result = self.runner.resume(
            turn_id=turn_id,
            user_text=text,
            resume_token=resume_token,
            session_asset_ids=session_asset_ids,
            option_id=option_id,
            execution_target=execution_target,
            baseline_script=baseline_script,
            change_policy=change_policy,
        )
        return self._to_view_update(turn_id, result)

    def _to_view_update(self, turn_id: str, result: dict[str, Any]) -> ViewUpdate:
        turn = self.runner.store.get_turn(turn_id)
        status = str(result.get("status") or turn.status.value)
        final = result.get("final_response") if isinstance(result.get("final_response"), dict) else {}
        text = str(final.get("text") or "")
        if not text:
            message = str(result.get("message") or "")
            if status == "waiting_user":
                text = str((result.get("interaction") or {}).get("question") or "需要你的输入")
            elif status == "failed":
                text = f"本轮执行中断：{message or result.get('reason') or '未知错误'}"
            elif status == "paused":
                if result.get("reason") == "TOOL_NO_PROGRESS":
                    text = "内部查询重复，已安全停止本轮；下次发送会从任务事实重新规划。"
                elif result.get("reason") == ErrorCode.RUNTIME_ERROR.value:
                    text = f"本轮执行中断：{message or '内部错误'}"
                elif message and result.get("reason") not in (
                    ErrorCode.BUDGET_EXCEEDED.value,
                    None,
                ):
                    # 收口的领域错误：说清原因，不要谎报"已完成"。
                    text = f"这一步没走通（{result.get('reason')}）：{message}"
                else:
                    text = "已保存当前进度，确认后可继续。"
            else:
                text = "本轮已完成。"
        return ViewUpdate(
            task_id=turn.task_id,
            turn_id=turn_id,
            status=status,
            status_text=text,
            messages=({"role": "agent", "text": text},),
            interaction=result.get("interaction"),
            trial_page_ready=bool(final.get("trial_page_ready")),
            checkpoint_id=result.get("checkpoint_id"),
            resume_token=result.get("resume_token"),
            logic_draft=tuple(result.get("logic_draft") or ()),
            final_response=final,
            pause_reason=str(result.get("reason") or "") or None,
        )
