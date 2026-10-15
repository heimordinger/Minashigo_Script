"""自主调参与安全范围（设计 §6/§19.5）。

模型应当**自己**调阈值/超时/重试参数，而不是把专业参数抛给用户；但自主必须是有边界的：

- 每个参数有安全范围（floor/ceiling）；
- 有**单次最大变化**，防止一步跳到极端；
- 有**最大尝试次数**，防止无限试；
- 每次调整都要**复测**才能声称有效（没复测只能说"已调整，等待验证"）；
- 防止靠无限降低阈值制造误命中 —— 阈值下限硬性钳制，并且要求同时参考
  分数间隔、位置稳定性、点击视觉效果与预期场景。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

#: 参数安全范围与单次最大变化。``(floor, ceiling, max_step)``
PARAM_RANGES: dict[str, tuple[float, float, float]] = {
    # 阈值下限刻意不低：低于这个值"命中"基本没有信息量（误命中）
    "threshold": (0.55, 0.99, 0.10),
    "nav_threshold": (0.60, 0.99, 0.10),
    "icon_threshold": (0.55, 0.99, 0.10),
    "effect_threshold": (0.40, 0.99, 0.10),
    "timeout": (0.5, 120.0, 15.0),
    "confirm_timeout": (0.5, 60.0, 10.0),
    "stable_ms": (0.0, 2000.0, 500.0),
    "retries": (0.0, 5.0, 1.0),
    "max_delay": (0.0, 10.0, 2.0),
    "sleep": (0.1, 30.0, 5.0),
}

#: 单次调整允许改的参数个数（一次只动一两个，便于归因）。
MAX_PARAMS_PER_STEP = 2
#: 同一参数的最大尝试次数（含首次），防止无限试。
DEFAULT_MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class TuningAttempt:
    """一次调参尝试（每步都要留痕：改了什么、依据什么、结果如何）。"""

    parameter: str
    before: float
    after: float
    reason_code: str = ""
    evidence_refs: tuple[str, ...] = ()
    verified: bool = False
    note: str = ""


@dataclass
class TuningState:
    """某个参数的历史与尝试计数。"""

    attempts: dict[str, int] = field(default_factory=dict)
    history: list[TuningAttempt] = field(default_factory=list)

    def count(self, parameter: str) -> int:
        return int(self.attempts.get(str(parameter), 0))

    def record(self, attempt: TuningAttempt) -> None:
        self.attempts[attempt.parameter] = self.count(attempt.parameter) + 1
        self.history.append(attempt)

    def mark_verified(self) -> int:
        """把"还没复测过"的调整标成已复测；返回本次标记条数。

        什么时候调用：拿到**覆盖当前交付物**的试运行证据时（见
        ``ActionRunner._retest_obligation_gate``）。以前全仓没有任何地方把这个
        标记设为 True，于是 `verified` 是死字段、`tuning_summary()` 永远报
        `verified: false`（哪怕这一版明明已经复测过）—— 审计轨迹是错的。

        ⚠️ **不改尝试计数**：那是"试了几次"的安全计数器，不因为复测成功就回退。
        """
        marked = 0
        updated: list[TuningAttempt] = []
        for attempt in self.history:
            if attempt.verified:
                updated.append(attempt)
                continue
            updated.append(replace(attempt, verified=True))
            marked += 1
        self.history = updated
        return marked

    def to_dict(self) -> dict[str, Any]:
        """可持久化形状（存进主轮检查点，见 ``TurnWorkspace.runtime_snapshot``）。"""
        return {
            "attempts": {str(k): int(v) for k, v in self.attempts.items()},
            "history": [
                {
                    "parameter": str(a.parameter),
                    "before": float(a.before),
                    "after": float(a.after),
                    "reason_code": str(a.reason_code or ""),
                    "evidence_refs": [str(x) for x in (a.evidence_refs or ())],
                    "verified": bool(a.verified),
                    "note": str(a.note or ""),
                }
                for a in self.history
            ],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "TuningState":
        """从持久化形状恢复；**坏数据一律跳过**（绝不因为一条脏记录炸掉恢复）。

        ⚠️ 尝试计数是**安全计数器**：它必须跨 ``pause → resume`` 存活，
        否则"最大尝试次数"可以被"回答一次提问再继续"无限绕开（每段重新计数）。
        """
        data = payload if isinstance(payload, dict) else {}
        attempts: dict[str, int] = {}
        raw_attempts = data.get("attempts")
        if isinstance(raw_attempts, dict):
            for key, value in raw_attempts.items():
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if count > 0:
                    attempts[str(key)] = count
        history: list[TuningAttempt] = []
        raw_history = data.get("history")
        if isinstance(raw_history, list):
            for row in raw_history:
                if not isinstance(row, dict):
                    continue
                try:
                    refs = row.get("evidence_refs")
                    history.append(
                        TuningAttempt(
                            parameter=str(row.get("parameter") or ""),
                            before=float(row.get("before") or 0.0),
                            after=float(row.get("after") or 0.0),
                            reason_code=str(row.get("reason_code") or ""),
                            evidence_refs=tuple(
                                str(x) for x in (refs if isinstance(refs, list) else [])
                            ),
                            verified=bool(row.get("verified")),
                            note=str(row.get("note") or ""),
                        )
                    )
                except (TypeError, ValueError):
                    continue
        return cls(attempts=attempts, history=history)


@dataclass(frozen=True)
class TuningDecision:
    """调参判定：给不给改、改成多少、还缺什么。"""

    ok: bool
    parameter: str
    value: float | None
    clamped: bool = False
    reasons: tuple[str, ...] = ()
    requires_retest: bool = True
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "parameter": self.parameter,
            "value": self.value,
            "clamped": self.clamped,
            "reasons": list(self.reasons),
            "requires_retest": self.requires_retest,
            "message": self.message,
        }


def clamp_value(parameter: str, value: float) -> tuple[float, bool]:
    """把值钳进安全范围；返回 ``(值, 是否钳过)``。未知参数不钳（但要报）。"""
    key = str(parameter or "").strip()
    spec = PARAM_RANGES.get(key)
    if spec is None:
        return float(value), False
    floor, ceiling, _step = spec
    clamped = min(max(float(value), floor), ceiling)
    return clamped, clamped != float(value)


def propose_tuning(
    parameter: str,
    target: float,
    *,
    current: float | None = None,
    state: TuningState | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reason_code: str = "",
    evidence_refs: Iterable[str] = (),
    max_params_this_step: int = MAX_PARAMS_PER_STEP,
    params_changed_this_step: Iterable[str] = (),
) -> TuningDecision:
    """判定一次自主调参是否可以执行，以及最终取什么值。

    拒绝的情形（都要给出可操作的 message，而不是笼统"不允许"）：

    - 参数未登记安全范围；
    - 已达最大尝试次数；
    - 单次变化超过该参数的 ``max_step``（会建议"分几步走"）;
    - 本次执行段已经改够参数个数。
    """
    key = str(parameter or "").strip()
    spec = PARAM_RANGES.get(key)
    if spec is None:
        return TuningDecision(
            ok=False,
            parameter=key,
            value=None,
            reasons=("PARAM_NOT_REGISTERED",),
            message=(
                "{} 没有登记安全范围，不能自主调；请先在 PARAM_RANGES 登记，"
                "或让用户确认".format(key)
            ),
        )

    state = state or TuningState()
    used = state.count(key)
    if used >= int(max_attempts):
        return TuningDecision(
            ok=False,
            parameter=key,
            value=None,
            reasons=("MAX_ATTEMPTS_REACHED",),
            message=(
                "{} 已尝试 {} 次（上限 {}）：不要再盲试，"
                "应改用诊断查询确认根因或询问用户".format(key, used, max_attempts)
            ),
        )

    changed = {str(x) for x in params_changed_this_step or ()}
    if key not in changed and len(changed) >= max(1, int(max_params_this_step)):
        return TuningDecision(
            ok=False,
            parameter=key,
            value=None,
            reasons=("TOO_MANY_PARAMS_THIS_STEP",),
            message=(
                "本次执行段已经改过 {}，一次只动 {} 个参数才能归因".format(
                    ", ".join(sorted(changed)), max_params_this_step
                )
            ),
        )

    floor, ceiling, max_step = spec
    value, clamped = clamp_value(key, float(target))
    reasons: list[str] = []
    if clamped:
        reasons.append("CLAMPED_TO_SAFE_RANGE")
    if current is not None:
        delta = abs(float(value) - float(current))
        if delta > max_step + 1e-9:
            return TuningDecision(
                ok=False,
                parameter=key,
                value=None,
                clamped=clamped,
                reasons=("STEP_TOO_LARGE",),
                message=(
                    "{} 单次最多改 {}（想从 {} 改到 {}）。请分步调整，"
                    "每步之后复测".format(key, max_step, current, value)
                ),
            )
    return TuningDecision(
        ok=True,
        parameter=key,
        value=value,
        clamped=clamped,
        reasons=tuple(reasons),
        requires_retest=True,
        message=(
            "已调整 {} → {}（安全范围 {}~{}）；**必须复测**后才能声称有效".format(
                key, value, floor, ceiling
            )
        ),
    )


def lower_threshold_is_unsafe(
    parameter: str, current: float, target: float, *, score_margin: float | None = None
) -> tuple[bool, str]:
    """降低阈值的额外护栏（设计 §6.4）。

    即使没越界，也要检查"是不是在靠降阈值凑命中"：分数间隔太小说明模板本身
    区分度不足，降阈值只会制造误命中。
    """
    key = str(parameter or "").strip()
    if not key.endswith("threshold") and key != "threshold":
        return False, ""
    if float(target) >= float(current):
        return False, ""
    if score_margin is not None and float(score_margin) < 0.05:
        return True, (
            "best 与 second-best 只差 {:.3f}：模板区分度不足，降阈值会制造误命中。"
            "先修模板/搜索区域，不要继续降阈值".format(float(score_margin))
        )
    floor = PARAM_RANGES.get(key, (0.0, 1.0, 0.1))[0]
    if float(target) <= floor:
        return True, (
            "{} 已到安全下限 {}：再降就没有判别力了".format(key, floor)
        )
    return False, ""


def requires_retest_before_claiming_fixed(
    attempts: Iterable[TuningAttempt], *, has_new_trial: bool
) -> tuple[bool, str]:
    """没有新试运行证据时，不能声称"已修复"（设计 §4/§19.5）。"""
    made = [a for a in attempts or () if not a.verified]
    if has_new_trial:
        return False, ""
    if made:
        return True, (
            "已调整 {} 但还没有新版本复测：只能说「已修改并通过静态校验，等待复测」".format(
                ", ".join(sorted({a.parameter for a in made}))
            )
        )
    return False, ""


def tuning_summary(state: Mapping[str, Any] | TuningState) -> dict[str, Any]:
    """排障用摘要。"""
    if isinstance(state, TuningState):
        attempts, history = state.attempts, state.history
    else:
        attempts = dict(state.get("attempts") or {})
        history = list(state.get("history") or [])
    return {
        "attempts": dict(attempts),
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "safe_ranges": {
            k: {"floor": v[0], "ceiling": v[1], "max_step": v[2]}
            for k, v in PARAM_RANGES.items()
        },
        "history": [
            {
                "parameter": a.parameter,
                "before": a.before,
                "after": a.after,
                "reason_code": a.reason_code,
                "verified": a.verified,
            }
            for a in history[-20:]
        ],
    }
