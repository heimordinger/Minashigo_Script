"""只读对账：源码调用点 ↔ Registry 契约 ↔ 本地 ticket（Phase 2）。

**只报告，不拒绝**（Phase 2 明确不做生产硬闸；拒收属 Phase 3）。

对账一个调用点要做四件事：

1. 接收者来源能不能证明（``callsite.CallSite.receiver_kind``）；
2. 方法名在契约表里吗、本 profile 批准了吗（registry）；
3. 真实定义类/代理链能不能证明（``verify_runtime.resolve_method``，决定 3）；
4. 有没有覆盖它的有效 ticket（可选传入；没有就是"需要 ticket"）。

第 4 步之外都是**静态事实**，所以这个模块可以在没有任何 ticket 的历史脚本上跑，
用来评估"上闸之后会有多少调用点需要凭据、有多少根本证明不了"。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import callsite, registry, validator, verify_runtime
from .ticket import ContractTicket

#: 调用点结论。``ok`` 才表示"契约、接收者、owner、凭据四者齐备"。
VERDICT_OK = "ok"
VERDICT_TICKET_MISSING = "ticket_missing"
VERDICT_TICKET_EXPIRED = "ticket_expired"
VERDICT_TICKET_PROFILE_MISMATCH = "ticket_profile_mismatch"
VERDICT_CALL_MISMATCH = "call_mismatch"
VERDICT_PROFILE_MISMATCH = "profile_mismatch"
VERDICT_RECEIVER_UNRESOLVED = "receiver_unresolved"
VERDICT_NOT_APPROVED = "not_approved"
VERDICT_UNREGISTERED = "unregistered"
VERDICT_OWNER_UNPROVEN = "owner_unproven"

# ── 影子闸的判断等级（Phase 3，人工决定 四）────────────────────────────
#
# 只有未来正式硬闸时，REJECTION_CANDIDATE_GRADES 里的四个才是**确定拒绝候选**；
# needs_profile 必须先解析 Runtime 目标；unresolved_receiver 需要先看误报数据。
GRADE_CONFIRMED = "confirmed"
GRADE_PORTABLE_CONFIRMED = "portable_confirmed"
GRADE_NEEDS_PROFILE = "needs_profile"
GRADE_MISSING_TICKET = "missing_ticket"
GRADE_EXPIRED_TICKET = "expired_ticket"
GRADE_MISMATCH = "mismatch"
GRADE_UNRESOLVED_RECEIVER = "unresolved_receiver"
GRADE_NOT_APPROVED = "not_approved"
#: **不计入分级**：这条调用根本不是本地 API 调用（纯 Python 的 list/str 方法等）。
#: 它既没有进入能力面的资格，也不该被当成"未登记 API"——否则 `hits.append(...)`、
#: `'{}'.format(...)` 会被判成 not_approved，将来第一阶段硬闸会误拒每一个正常脚本。
GRADE_NOT_APPLICABLE = "not_applicable"

GRADE_ORDER: tuple[str, ...] = (
    GRADE_CONFIRMED,
    GRADE_PORTABLE_CONFIRMED,
    GRADE_NEEDS_PROFILE,
    GRADE_MISSING_TICKET,
    GRADE_EXPIRED_TICKET,
    GRADE_MISMATCH,
    GRADE_UNRESOLVED_RECEIVER,
    GRADE_NOT_APPROVED,
)

#: 未来硬闸的**候选**拒绝等级（本阶段只分级，绝不拒绝交付）。
REJECTION_CANDIDATE_GRADES: frozenset[str] = frozenset(
    {
        GRADE_MISSING_TICKET,
        GRADE_EXPIRED_TICKET,
        GRADE_MISMATCH,
        GRADE_NOT_APPROVED,
    }
)


@dataclass(frozen=True)
class CallSiteVerdict:
    """一个调用点的对账结论（只读）。"""

    site: callsite.CallSite
    verdict: str
    #: 影子闸等级（人工决定 四）：confirmed / portable_confirmed / needs_profile /
    #: missing_ticket / expired_ticket / mismatch / unresolved_receiver / not_approved。
    grade: str = GRADE_MISSING_TICKET
    #: 该名字对应的契约在**当前** profile 里是否已批准。
    registered: bool = False
    approved: bool = False
    #: 同一个名字在**注解推出的** profile 里是否批准（用来发现"写错 profile"）。
    approved_in_annotation: bool | None = None
    resolution_status: str = ""
    resolved_owner: str = ""
    chain: tuple[str, ...] = ()
    ticket_id: str = ""
    ticket_status: str = ""
    ticket_code: str = ""
    reasons: tuple[str, ...] = ()
    #: 很可能不是本地 API 调用（接收者与入口无关却撞上了契约名）→ 误报候选。
    false_positive_candidate: bool = False

    def to_row(self) -> dict[str, Any]:
        site = self.site
        return {
            "node_id": site.node_id,
            "path": site.path,
            "span": site.span,
            "line": site.line,
            "method": site.method,
            "receiver": site.receiver_expr or site.receiver_root,
            "receiver_kind": site.receiver_kind,
            "alias_chain": list(site.alias_chain),
            "is_await": site.is_await,
            "positional": site.positional,
            "keywords": list(site.keywords),
            "verdict": self.verdict,
            "grade": self.grade,
            "registered": self.registered,
            "approved": self.approved,
            "approved_in_annotation": self.approved_in_annotation,
            "resolution_status": self.resolution_status,
            "resolved_owner": self.resolved_owner,
            "chain": list(self.chain),
            "ticket_id": self.ticket_id,
            "ticket_status": self.ticket_status,
            "ticket_code": self.ticket_code,
            "reasons": list(self.reasons),
            "false_positive_candidate": self.false_positive_candidate,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    """一次源码对账的完整只读报告。"""

    path: str
    source_sha256: str
    runtime_profile: str
    profile_source: str
    contract_revision: str
    annotation_profile: str
    verdicts: tuple[CallSiteVerdict, ...] = ()
    counters: Mapping[str, int] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        """整份脚本的结论：任一调用点不 ok 就是 ``needs_attention``（**不**代表拒绝）。"""
        return "ok" if all(v.verdict == VERDICT_OK for v in self.verdicts) else "needs_attention"

    def of(self, verdict: str) -> tuple[CallSiteVerdict, ...]:
        return tuple(v for v in self.verdicts if v.verdict == verdict)

    def of_grade(self, grade: str) -> tuple[CallSiteVerdict, ...]:
        return tuple(v for v in self.verdicts if v.grade == grade)

    def samples(self, verdict: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return [v.to_row() for v in self.of(verdict)[:limit]]

    def grade_samples(self, grade: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return [v.to_row() for v in self.of_grade(grade)[:limit]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "source_sha256": self.source_sha256,
            "runtime_profile": self.runtime_profile,
            "profile_source": self.profile_source,
            "annotation_profile": self.annotation_profile,
            "contract_revision": self.contract_revision,
            "verdict": self.verdict,
            "counters": dict(self.counters),
            "grades": dict(self.grade_counts),
            "sites": [v.to_row() for v in self.verdicts],
        }

    @property
    def grade_counts(self) -> "Counter[str]":
        """八级直方图（影子闸的核心产物；``not_applicable`` 不计入）。"""
        counts: Counter[str] = Counter({grade: 0 for grade in GRADE_ORDER})
        counts.update(
            v.grade for v in self.verdicts if v.grade in GRADE_ORDER
        )
        return counts

    @property
    def not_applicable_count(self) -> int:
        """不是本地 API 调用、因此没有分级的调用点数。"""
        return sum(1 for v in self.verdicts if v.grade == GRADE_NOT_APPLICABLE)

    def summary(self) -> dict[str, Any]:
        """给统计用的稳定形状（含用户要求的五项指标与八级直方图）。"""
        local = max(1, int(self.counters.get("local_api_sites", 0)))
        determinable = int(self.counters.get("determinable", 0))
        unticketed = int(self.counters.get("ticket_missing", 0))
        grades = self.grade_counts
        return {
            "path": self.path,
            "runtime_profile": self.runtime_profile,
            "annotation_profile": self.annotation_profile,
            "call_sites": int(self.counters.get("call_sites", 0)),
            "api_named_sites": int(self.counters.get("api_named_sites", 0)),
            "local_api_sites": int(self.counters.get("local_api_sites", 0)),
            "determinable": determinable,
            "determinable_rate": round(determinable / local, 4),
            "receiver_unresolved": int(self.counters.get("receiver_unresolved", 0)),
            "receiver_unresolved_rate": round(
                int(self.counters.get("receiver_unresolved", 0)) / local, 4
            ),
            # 口径（人工决定 二）：只能叫"未经过 ticket 签发流程的调用点比例"，
            # **不得**叫错误率 / 幻觉率 / 硬闸拒绝率 / 需要修复比例。
            "unticketed_sites": unticketed,
            "unticketed_rate": round(unticketed / max(1, determinable), 4),
            "unticketed_rate_label": "未经过 ticket 签发流程的调用点比例（不等于错误率或拒绝率）",
            "ticket_missing": unticketed,
            "profile_mismatch": int(self.counters.get("profile_mismatch", 0))
            + int(self.counters.get("ticket_profile_mismatch", 0)),
            "profile_mismatch_rate": round(
                (
                    int(self.counters.get("profile_mismatch", 0))
                    + int(self.counters.get("ticket_profile_mismatch", 0))
                )
                / local,
                4,
            ),
            "false_positive_candidates": int(self.counters.get("false_positive_candidates", 0)),
            "grades": {grade: int(grades.get(grade, 0)) for grade in GRADE_ORDER},
            "not_applicable": self.not_applicable_count,
            "rejection_candidates": sum(
                int(grades.get(grade, 0)) for grade in REJECTION_CANDIDATE_GRADES
            ),
        }


def reconcile_source(
    source: str,
    *,
    path: str = "",
    runtime_profile: str | None = None,
    profile_source: str = "",
    tickets: Iterable[ContractTicket] = (),
    annotation_profile: str | None = None,
    contract_revision: str = "",
) -> ReconciliationReport:
    """对账一份源码（只读）。

    ``runtime_profile`` 给 ``None`` ⇒ 用 ``portable_common`` 交集语义（**不猜** desktop/browser）。
    ``tickets`` 是可选的：给了就核验覆盖情况，没给就只报静态事实。
    """
    effective = registry.effective_profile(runtime_profile)
    spec = registry.get_profile(effective)
    # ``annotation_profile`` 保留**注解推出的原始结果**（可能是 ``unresolved``）：
    # 它是"脚本作者以为的目标"，与执行目标不一致时才说明 profile 写错了。
    annotation = (
        str(annotation_profile)
        if annotation_profile
        else validator.profile_for_code(source)
    )
    annotation_effective = (
        registry.effective_profile(annotation) if annotation else registry.UNRESOLVED_PROFILE
    )

    by_capability: dict[str, ContractTicket] = {}
    for ticket in tickets:
        by_capability.setdefault(ticket.capability_id, ticket)
        by_capability.setdefault(
            "{}.{}".format(registry.PORTABLE_PROFILE, ticket.requested_method), ticket
        )

    sites = callsite.extract_calls(
        source, path=path, entry_names=callsite.entry_target_names(source) or callsite.ENTRY_TARGET_NAMES
    )
    verdicts: list[CallSiteVerdict] = []
    for site in sites:
        verdicts.append(
            _verdict_for(
                site,
                spec=spec,
                effective=effective,
                annotation_effective=annotation_effective,
                by_capability=by_capability,
            )
        )

    counters = Counter(v.verdict for v in verdicts)
    counters["call_sites"] = len(verdicts)
    # 只按**方法名**匹配会命中的点（含"接收者根本与入口无关"的那些）。
    api_named = [v for v in verdicts if v.registered or v.approved_in_annotation]
    counters["api_named_sites"] = len(api_named)
    # 真正的候选本地 API 调用：方法名在契约表里 **且** 接收者可证明来自入口执行器。
    local = [v for v in api_named if v.site.receiver_resolved]
    counters["local_api_sites"] = len(local)
    # 误报候选：名字撞上了契约，但接收者与入口无关（self/属性链/裸名字）。
    counters["false_positive_candidates"] = len(api_named) - len(local)
    # "可确定匹配" = 接收者可证明 + 契约已登记且本 profile 批准（"能对上契约"）。
    # 真实定义类是否可证明是**另一个**计数器（portable/合成 profile 必然证明不了）。
    determinable = [v for v in local if v.registered and v.approved]
    counters["determinable"] = len(determinable)
    counters["owner_proven"] = sum(
        1
        for v in determinable
        if v.resolution_status
        in (verify_runtime.RESOLUTION_RESOLVED, verify_runtime.RESOLUTION_PROXIED)
    )
    counters["ticket_provided"] = sum(
        1 for v in verdicts if v.ticket_id and v.ticket_status == "ok"
    )
    counters["grade_confirmed"] = sum(1 for v in verdicts if v.grade == GRADE_CONFIRMED)
    counters["grade_portable_confirmed"] = sum(
        1 for v in verdicts if v.grade == GRADE_PORTABLE_CONFIRMED
    )
    counters["grade_needs_profile"] = sum(
        1 for v in verdicts if v.grade == GRADE_NEEDS_PROFILE
    )
    counters["grade_rejection_candidates"] = sum(
        1 for v in verdicts if v.grade in REJECTION_CANDIDATE_GRADES
    )
    counters["grade_not_applicable"] = sum(
        1 for v in verdicts if v.grade == GRADE_NOT_APPLICABLE
    )
    return ReconciliationReport(
        path=path,
        source_sha256=hashlib.sha256((source or "").encode("utf-8")).hexdigest(),
        runtime_profile=effective,
        profile_source=str(profile_source or ""),
        contract_revision=str(contract_revision or registry.contracts_revision()),
        annotation_profile=str(annotation or registry.UNRESOLVED_PROFILE),
        verdicts=tuple(verdicts),
        counters=dict(counters),
    )


def _verdict_for(
    site: callsite.CallSite,
    *,
    spec,
    effective: str,
    annotation_effective: str,
    by_capability: Mapping[str, ContractTicket],
) -> CallSiteVerdict:
    item = spec.by_name(site.method)
    approved = bool(item is not None and item.approved)
    approved_in_annotation: bool | None = None
    if annotation_effective not in (registry.UNRESOLVED_PROFILE, registry.PORTABLE_PROFILE):
        annotation_spec = registry.get_profile(annotation_effective)
        annotation_item = annotation_spec.by_name(site.method)
        approved_in_annotation = bool(annotation_item is not None and annotation_item.approved)
    else:
        # 注解推不出具体 profile 时，用"任一端批准过"来判定这是不是本地 API
        approved_in_annotation = any(
            registry.is_approved(profile, site.method, allow_login=True)
            for profile in registry.verifiable_profile_names()
        )

    resolution = verify_runtime.resolve_method(effective, site.method)
    ticket = by_capability.get("{}.{}".format(effective, site.method))
    if ticket is None:
        ticket = by_capability.get("{}.{}".format(registry.PORTABLE_PROFILE, site.method))
    registered_anywhere = any(
        registry.get_profile(profile).by_name(site.method) is not None
        for profile in registry.profile_names()
    )
    # 只有"API 候选"才进分级：名字在契约表里、或接收者确实归到了入口执行器。
    # 纯 Python 调用（`hits.append` / `'{}'.format` …）一律 not_applicable。
    api_candidate = bool(
        approved or registered_anywhere or site.receiver_resolved or approved_in_annotation
    )

    reasons: list[str] = []
    # 合成 profile（portable_common / unresolved）没有真实类：owner **无法判定**，
    # 这不是"证明不了"（人工决定 三：不得把 portable owner 不可证明当成幻觉）。
    synthetic = resolution.status == verify_runtime.RESOLUTION_UNKNOWN

    if not approved:
        # 不在当前 profile 的契约表里 / 未批准：区分"根本没登记"与"登记了但不允许"
        if registered_anywhere:
            verdict = VERDICT_NOT_APPROVED
            reasons.append(
                "{} 未在当前 profile（{}）批准".format(site.method, effective)
            )
            if approved_in_annotation and annotation_effective != effective:
                reasons.append("注解 profile（{}）里是批准的 → profile 不一致".format(annotation_effective))
        else:
            verdict = VERDICT_UNREGISTERED
            reasons.append("{} 不在任何契约里".format(site.method))
    elif not site.receiver_resolved:
        verdict = VERDICT_RECEIVER_UNRESOLVED
        reasons.append("接收者来源无法证明（{}）".format(site.receiver_kind))
    elif not resolution.proven and not synthetic:
        verdict = VERDICT_OWNER_UNPROVEN
        reasons.append(resolution.detail or "真实定义类无法证明")
    elif ticket is None:
        verdict = VERDICT_TICKET_MISSING
        reasons.append("没有覆盖该调用点的 ticket（模型可经 api.get/get_many 补查一次）")
    else:
        mismatch = str(ticket.runtime_profile) != str(effective)
        if mismatch:
            verdict = VERDICT_TICKET_PROFILE_MISMATCH
            reasons.append(
                "凭据 profile（{}）与当前（{}）不一致".format(ticket.runtime_profile, effective)
            )
        elif not ticket.verify_integrity():
            verdict = VERDICT_TICKET_EXPIRED
            reasons.append("凭据完整性校验失败")
        elif ticket.revoked_at:
            verdict = VERDICT_TICKET_EXPIRED
            reasons.append("凭据已被本地失效：{}".format(ticket.revoked_reason))
        elif str(ticket.contract_revision) != str(registry.contracts_revision()):
            verdict = VERDICT_TICKET_EXPIRED
            reasons.append("凭据绑定的契约版本已过期")
        else:
            # 有票、版本也对：再核对**这次调用**与票是否一致（owner / 参数 / 接收者）
            shape = _call_mismatch_reasons(site, item, ticket, resolution)
            if shape:
                verdict = VERDICT_CALL_MISMATCH
                reasons.extend(shape)
            else:
                verdict = VERDICT_OK

    # 误报候选的统一口径：**名字撞上契约，但接收者与入口无关**。
    # （只按方法名做匹配的实现，会把这些点当成"本地 API 调用"。）
    false_positive = bool(
        (item is not None or approved_in_annotation) and not site.receiver_resolved
    )
    grade = _grade_for(
        verdict=verdict,
        site=site,
        effective=effective,
        approved_in_annotation=approved_in_annotation,
        resolution=resolution,
        portable=bool(ticket is not None and ticket.is_portable())
        or effective == registry.PORTABLE_PROFILE,
    )
    if not api_candidate:
        grade = GRADE_NOT_APPLICABLE
        reasons.append("不是本地 API 调用（纯 Python 方法），不计入分级")

    return CallSiteVerdict(
        site=site,
        verdict=verdict,
        grade=grade,
        registered=item is not None,
        approved=approved,
        approved_in_annotation=approved_in_annotation,
        resolution_status=resolution.status,
        resolved_owner=resolution.owner,
        chain=resolution.chain,
        ticket_id=ticket.ticket_id if ticket is not None else "",
        ticket_status="ok" if ticket is not None and ticket.verify_integrity() else "",
        ticket_code="",
        reasons=tuple(reasons),
        false_positive_candidate=false_positive,
    )


def _call_mismatch_reasons(site, item, ticket: ContractTicket, resolution) -> list[str]:
    """**这次调用**与凭据/契约是否对得上：接收者、owner、参数（设计 §9 第 4/5 步）。

    参数形状用 Registry 的契约（与凭据同一份快照来源；凭据版本不符时上面已经先判过期），
    这样"必填/位置/关键字"的判定不需要猜。
    """
    reasons: list[str] = []
    if str(ticket.requested_method) != str(site.method):
        reasons.append(
            "凭据方法 {} ≠ 调用 {}".format(ticket.requested_method, site.method)
        )
    if ticket.receiver_type and resolution.owner and ticket.resolved_owner:
        if ticket.resolved_owner != resolution.owner:
            reasons.append(
                "owner 不符：凭据={} 实际={}".format(ticket.resolved_owner, resolution.owner)
            )
    if ticket.receiver_type and resolution.owner and not ticket.resolved_owner:
        reasons.append("凭据没有记录真实定义类（owner unresolved）")

    if item is None:
        return reasons
    allowed = set(item.keyword_names())
    accepts_kwargs = bool(item.accepts_kwargs)
    for name in site.keywords:
        if name not in allowed and not accepts_kwargs:
            reasons.append("参数 {} 不在契约里".format(name))
    positional = item.positional_parameters()
    if site.positional > len(positional) and not any(
        p.kind == "var_positional" for p in item.parameters
    ):
        reasons.append(
            "位置参数过多：传了 {} 个，契约最多 {}".format(site.positional, len(positional))
        )
    for index, param in enumerate(positional):
        if index >= site.positional and param.required and param.name not in site.keywords:
            reasons.append("必填参数 {} 未传".format(param.name))
    for param in item.parameters:
        if param.kind == "keyword_only" and param.required and param.name not in site.keywords:
            reasons.append("必填关键字参数 {} 未传".format(param.name))
    return reasons


def _grade_for(
    *,
    verdict: str,
    site,
    effective: str,
    approved_in_annotation: bool | None,
    resolution,
    portable: bool,
) -> str:
    """把细分结论映射成影子闸的八级（人工决定 四）。"""
    if verdict == VERDICT_OK:
        return GRADE_PORTABLE_CONFIRMED if portable else GRADE_CONFIRMED
    if verdict == VERDICT_TICKET_MISSING:
        return GRADE_MISSING_TICKET
    if verdict in (VERDICT_TICKET_EXPIRED, VERDICT_TICKET_PROFILE_MISMATCH):
        return GRADE_EXPIRED_TICKET
    if verdict == VERDICT_CALL_MISMATCH:
        return GRADE_MISMATCH
    if verdict in (VERDICT_RECEIVER_UNRESOLVED, VERDICT_OWNER_UNPROVEN):
        # owner 证明不了也归"无法静态证明"，与"接收者来源无法证明"同类
        return GRADE_UNRESOLVED_RECEIVER
    if verdict in (VERDICT_UNREGISTERED, VERDICT_NOT_APPROVED):
        # 单端能力（只在一侧批准）在目标未确定时属于"需要 Runtime 目标"
        single_sided = _single_sided(verdict, site.method, effective)
        return GRADE_NEEDS_PROFILE if single_sided else GRADE_NOT_APPROVED
    return GRADE_NOT_APPROVED


def _single_sided(_verdict: str, method: str, effective: str) -> bool:
    owners = [
        profile
        for profile in registry.verifiable_profile_names()
        if registry.is_approved(profile, method, allow_login=True)
    ]
    if len(owners) != 1:
        return False
    return effective in (registry.PORTABLE_PROFILE, registry.UNRESOLVED_PROFILE) or effective not in owners
