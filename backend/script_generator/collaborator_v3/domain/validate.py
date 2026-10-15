"""Pure domain and state-transition validation."""

from __future__ import annotations

from .enums import RuntimePhase, TurnStatus
from .errors import DomainError, ErrorCode
from .models import ClaimRecord, ResourceRecord


USABLE_RESOURCE_STATUSES = {"candidate", "active", "verified"}

_TRANSITIONS: set[tuple[TurnStatus, RuntimePhase, TurnStatus, RuntimePhase]] = {
    (TurnStatus.CREATED, RuntimePhase.INTAKE, TurnStatus.RUNNING, RuntimePhase.INTAKE),
    (TurnStatus.RUNNING, RuntimePhase.INTAKE, TurnStatus.RUNNING, RuntimePhase.HYDRATE),
    (TurnStatus.RUNNING, RuntimePhase.INTAKE, TurnStatus.RUNNING, RuntimePhase.RESPOND),
    (TurnStatus.RUNNING, RuntimePhase.HYDRATE, TurnStatus.RUNNING, RuntimePhase.PLAN),
    (TurnStatus.RUNNING, RuntimePhase.PLAN, TurnStatus.RUNNING, RuntimePhase.EXECUTE),
    (TurnStatus.RUNNING, RuntimePhase.EXECUTE, TurnStatus.RUNNING, RuntimePhase.OBSERVE),
    (TurnStatus.RUNNING, RuntimePhase.OBSERVE, TurnStatus.RUNNING, RuntimePhase.REPLAN),
    (TurnStatus.RUNNING, RuntimePhase.REPLAN, TurnStatus.RUNNING, RuntimePhase.EXECUTE),
    (TurnStatus.RUNNING, RuntimePhase.VALIDATE, TurnStatus.RUNNING, RuntimePhase.REPLAN),
    (TurnStatus.RUNNING, RuntimePhase.VALIDATE, TurnStatus.RUNNING, RuntimePhase.COMMIT),
    (TurnStatus.RUNNING, RuntimePhase.COMMIT, TurnStatus.COMMITTED, RuntimePhase.RESPOND),
}


def validate_turn_transition(
    old_status: TurnStatus,
    old_phase: RuntimePhase,
    new_status: TurnStatus,
    new_phase: RuntimePhase,
) -> None:
    terminal_cancel = new_status is TurnStatus.CANCELLED and old_status not in {
        TurnStatus.COMMITTED,
        TurnStatus.CANCELLED,
        TurnStatus.ABORTED,
    }
    wait_user = (
        old_status is TurnStatus.RUNNING
        and new_status is TurnStatus.WAITING_USER
        and new_phase is RuntimePhase.WAIT_USER
    )
    resume_user = (
        old_status in {TurnStatus.WAITING_USER, TurnStatus.PAUSED}
        and new_status is TurnStatus.RUNNING
        and new_phase in {RuntimePhase.HYDRATE, RuntimePhase.REPLAN}
    )
    hard_pause = (
        old_status is TurnStatus.RUNNING
        and new_status is TurnStatus.PAUSED
        and new_phase is RuntimePhase.PAUSE
    )
    if (old_status, old_phase, new_status, new_phase) not in _TRANSITIONS and not (
        terminal_cancel or wait_user or resume_user or hard_pause
    ):
        raise DomainError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"{old_status.value}/{old_phase.value} -> {new_status.value}/{new_phase.value}",
        )


def validate_resource_for_use(resource: ResourceRecord) -> None:
    if resource.status.value not in USABLE_RESOURCE_STATUSES:
        raise DomainError(
            ErrorCode.INVALID_RESOURCE,
            f"resource {resource.id}@{resource.version} is {resource.status.value}",
        )


def validate_claim_for_assertion(claim: ClaimRecord) -> None:
    if claim.status.value != "verified":
        raise DomainError(
            ErrorCode.EVIDENCE_INSUFFICIENT,
            f"claim {claim.id} is not verified",
            recoverable=True,
        )


def looks_corrupted_text(text: str) -> bool:
    """Reject irreversible replacement-character output instead of persisting it."""
    value = str(text or "")
    if "\ufffd" in value:
        return True
    non_space = [ch for ch in value if not ch.isspace()]
    return bool(non_space) and sum(ch == "?" for ch in non_space) / len(non_space) >= 0.25


def is_placeholder_task_objective(text: str) -> bool:
    normalized = "".join(str(text or "").strip().split())
    return normalized in {
        "制作脚本",
        "生成脚本",
        "根据用户需求制作脚本",
        "根据用户需求生成脚本",
        "根据用户需求生成并校验一个可运行脚本",
    }


_META_PHRASES = (
    "缺少什么", "缺什么", "还差什么", "少了什么", "什么问题", "什么意思",
    "为什么", "然后呢", "还有呢", "接下来呢", "怎么办", "怎么用",
    "你是", "你好", "谢谢", "在吗", "继续", "接着做",
    "重试", "再试一次", "换个做法",
)


def looks_like_meta_question(text: str) -> bool:
    """追问、寒暄和流程控制话术不是任务目标。

    实测：「你先用我空格子和勋章在结界1上进行匹配…」之后用户只发了
    「缺少什么」，旧实现把这句话写成了 Task.objective，目标从此失真。
    """
    value = str(text or "").strip()
    if not value:
        return True
    if any(phrase in value for phrase in _META_PHRASES):
        return True
    if len(value) <= 12 and value.endswith(("什么", "吗", "呢", "？", "?")):
        return True
    return False
