"""**公共** ticket 服务：签发 / 查询 / 失效 / 评估（Phase 1）。

V2 与 V3 **必须**共用这一个实现（用户决定 9：不得为两条路径各写一份签发逻辑）：

* V3 在 ``api.get`` / ``api.get_many`` 里用它；
* V2 在 ``lookup_api`` 里用它（服务从进程默认登记处取，见
  ``collaborator_v3.runtime.ticket_store``）。

职责边界：

* 它**只**从 ``api_contracts`` registry 取事实（``render.details`` 渲染 + ``build_ticket``
  快照），不接受任何"由 LLM 给定字段"的构造入口；
* 事件表只写**引用**：``CONTRACT_VIEWED`` 记"看过哪些契约 + schema hash"，
  ``CALL_AUTHORIZED`` 记"批准了哪些 ticket_id"；整张票只存在 ``contract_tickets`` 表里
  （设计 §12：凭据 / 权限 / 执行事件三者不合并）。
* profile 无法确定到**单一接收者**时只记 VIEW、不签发，理由用既有 ``PROFILE_REQUIRED``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from . import registry, render, validator
from .ticket import (
    ContractTicket,
    TicketEvaluation,
    build_ticket,
    evaluate_ticket,
    new_ticket_id,
)

#: 规范事件名（``collaborator_v3.events.schema.EventType`` 里有同名成员）。
EVENT_CONTRACT_VIEWED = "contract.viewed"
EVENT_CALL_AUTHORIZED = "call.authorized"

#: profile 来源标签：调用方给了具体 profile / 只能退回 portable 安全交集。
PROFILE_SOURCE_EXPLICIT = "explicit"
PROFILE_SOURCE_UNRESOLVED = "unresolved"

#: 一次查询最多签发多少张票（与 V3 ``api.get_many`` 的上限一致）。
MAX_VIEW_CONTRACTS = 16


class TicketStorePort(Protocol):
    """ticket 持久化端口（V3 由 ``SqliteDomainStore`` 实现，测试可用内存实现）。"""

    def put_ticket(self, row: Mapping[str, Any]) -> None: ...

    def get_ticket(self, ticket_id: str) -> Mapping[str, Any] | None: ...

    def list_tickets(
        self,
        *,
        main_turn_id: str = "",
        capability_id: str = "",
        include_revoked: bool = False,
        limit: int = 50,
    ) -> list[Mapping[str, Any]]: ...

    def revoke_ticket(self, ticket_id: str, *, reason: str, at: str = "") -> bool: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_names(names: Iterable[str] | str) -> list[str]:
    """归一化：去空白、去重（保序）、丢弃空项。"""
    items = [names] if isinstance(names, str) else list(names or ())
    out: list[str] = []
    for item in items:
        key = str(item or "").strip()
        if key and key not in out:
            out.append(key)
    return out[:MAX_VIEW_CONTRACTS]


@dataclass(frozen=True)
class ContractViewResult:
    """一次"查看契约（并尽量签发）"的结果。"""

    ok: bool
    runtime_profile: str
    profile_source: str
    contract_revision: str
    #: 给模型的契约正文（来自 ``render.details``，Registry 渲染的唯一形状）。
    contracts: tuple[dict[str, Any], ...]
    #: 本次签发的票（可能为空：未批准 / 目标未确定到具体接收者 → 见 portable 规则）。
    tickets: tuple[ContractTicket, ...] = ()
    missing: tuple[str, ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()
    #: portable 状态下被"安全交集"挡掉的**单端能力**：必须报 ``PROFILE_REQUIRED``，
    #: 不得静默选 desktop/browser（决定 1）。
    profile_required: tuple[dict[str, Any], ...] = ()
    receiver_type: str = ""
    view_event_id: str = ""
    #: 空串表示签发了（或没有可签的）；``PROFILE_REQUIRED`` 表示"能看但不能签"。
    skipped_reason: str = ""

    @property
    def ticket_ids(self) -> tuple[str, ...]:
        return tuple(item.ticket_id for item in self.tickets)

    def ticket_id(self) -> str:
        """单条查询场景的便捷取值（0 或 1 张）。"""
        return self.tickets[0].ticket_id if self.tickets else ""


@dataclass(frozen=True)
class ReconcileOutcome:
    """一张票在"目标已确定"之后的核验/重签结论（决定 1）。"""

    ticket_id: str
    #: verified | reissued | kept_portable | expired | mismatch | missing
    status: str
    code: str = ""
    reason: str = ""
    new_ticket_id: str = ""


class TicketService:
    """签发/查询/失效。构造后即可被 V2 与 V3 共用。"""

    def __init__(
        self,
        *,
        store: TicketStorePort,
        events: Callable[..., Any] | None = None,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
        revision: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        #: 事件出口（通常是 ``EventStore.emit``）；为 None 时只落库不发事件。
        self.events = events
        self._clock = clock or _utc_now
        self._id_factory = id_factory or new_ticket_id
        self._revision = revision or registry.contracts_revision

    def bound_to(self, store: object) -> bool:
        """本服务是否绑在给定后端库上（V3 据此复用进程默认服务，而不是另造一个）。

        端口实现可能把真库放在 ``.store`` 属性里（如 SQLite 适配器），因此两种形状都认。
        """
        return getattr(self.store, "store", self.store) is store

    # ── 签发 ──

    def view_contracts(
        self,
        *,
        names: Iterable[str] | str,
        profile: str | None = None,
        profile_source: str = "",
        main_turn_id: str = "",
        subturn_id: str = "",
        run_id: str = "",
        action_id: str = "",
        task_id: str = "",
        environment_revision: str = "",
        permission_id: str = "",
        permission_scope: Iterable[str] = (),
        resource_scope: Iterable[str] = (),
        receiver_hint: str = "",
    ) -> ContractViewResult:
        """记录"模型看过这些契约"，并在能确定单一接收者时签发 ticket。

        ``receiver_hint`` 只在契约有两个候选（portable 交集）时才用得上；它**必须**
        命中候选集合，否则不签发 —— 这里绝不允许"挑一个看起来对的"。
        """
        keys = _clean_names(names)
        effective = registry.effective_profile(profile)
        source = str(profile_source or "").strip() or (
            PROFILE_SOURCE_UNRESOLVED
            if effective == registry.PORTABLE_PROFILE
            else PROFILE_SOURCE_EXPLICIT
        )
        revision = self._revision()
        rendered = render.details(effective, keys)
        items = tuple(dict(item) for item in rendered.get("items") or ())
        missing = tuple(str(x) for x in rendered.get("missing") or ())
        rejected = tuple(dict(x) for x in rendered.get("rejected") or ())
        profile_required = self._profile_required(effective, missing)

        profile_spec = registry.get_profile(effective)
        contracts: list[tuple[dict[str, Any], Any]] = []
        for payload in items:
            item = profile_spec.by_name(str(payload.get("name", "")).rsplit(".", 1)[-1])
            if item is not None:
                contracts.append((payload, item))

        portable = effective == registry.PORTABLE_PROFILE
        receiver_type = "" if portable else self._single_receiver(
            [item for _, item in contracts], receiver_hint
        )

        view_event_id = self._emit(
            EVENT_CONTRACT_VIEWED,
            {
                "capability_ids": [str(payload["id"]) for payload, _ in contracts],
                "contract_revision": revision,
                "runtime_profile": effective,
                "profile_source": source,
                "receiver_type": receiver_type,
                # §5.1：记录"当时提供给模型的 schema hash"，事后可核对模型看到的是哪一版。
                "schema_hashes": {
                    str(payload["id"]): _sha256(payload) for payload, _ in contracts
                },
                "missing": list(missing),
                "rejected": [str(x.get("name", "")) for x in rejected],
                "profile_required": [str(x.get("name", "")) for x in profile_required],
            },
            main_turn_id=main_turn_id,
            subturn_id=subturn_id,
            run_id=run_id,
            action_id=action_id,
            task_id=task_id,
        )

        tickets: tuple[ContractTicket, ...] = ()
        skipped = ""
        if contracts:
            if not portable and not receiver_type:
                # 具体 profile 但接收者定不下来：不猜，按"需要先确定 profile"处理
                skipped = registry.PROFILE_REQUIRED_CODE
            else:
                issued = [
                    build_ticket(
                        contract=item,
                        receiver_type=receiver_type,
                        contract_revision=revision,
                        runtime_profile=effective,
                        profile_source=source,
                        main_turn_id=main_turn_id,
                        view_event_id=view_event_id,
                        environment_revision=environment_revision,
                        permission_id=permission_id,
                        permission_scope=tuple(permission_scope),
                        resource_scope=tuple(resource_scope),
                        resolution_chain=self._resolution_chain(effective, item.name, receiver_type),
                        resolved_owner=self._resolved_owner(effective, item.name),
                        issued_at=self._clock(),
                        ticket_id=self._id_factory(),
                    )
                    for _, item in contracts
                ]
                for ticket in issued:
                    self.store.put_ticket(ticket.to_row())
                tickets = tuple(issued)
                self._emit(
                    EVENT_CALL_AUTHORIZED,
                    {
                        "ticket_ids": [t.ticket_id for t in tickets],
                        "capability_ids": [t.capability_id for t in tickets],
                        "contract_revision": revision,
                        "runtime_profile": effective,
                        "receiver_type": receiver_type,
                        "portable": bool(portable),
                        "permission_id": str(permission_id or ""),
                    },
                    main_turn_id=main_turn_id,
                    subturn_id=subturn_id,
                    run_id=run_id,
                    action_id=action_id,
                    task_id=task_id,
                )

        return ContractViewResult(
            ok=True,
            runtime_profile=effective,
            profile_source=source,
            contract_revision=revision,
            contracts=items,
            tickets=tickets,
            missing=missing,
            rejected=rejected,
            profile_required=profile_required,
            receiver_type=receiver_type,
            view_event_id=view_event_id,
            skipped_reason=skipped,
        )

    @staticmethod
    def _resolution(profile: str, name: str):
        """真实定义类与解析链（Phase 2 的 ``verify_runtime.resolve_method``）。

        证明不了就返回 ``("", None)`` —— **不填占位值**（决定 3）。
        """
        try:
            from . import verify_runtime

            found = verify_runtime.resolve_method(profile, name)
        except Exception:  # noqa: BLE001 - 解析失败按"未证实"处理
            return "", None
        if not found.proven:
            return "", None
        return found.owner, found.chain

    def _resolved_owner(self, profile: str, name: str) -> str:
        return self._resolution(profile, name)[0]

    def _resolution_chain(
        self, profile: str, name: str, receiver_type: str
    ) -> tuple[str, ...] | None:
        chain = self._resolution(profile, name)[1]
        if chain:
            return tuple(chain)
        # 未证实定义类：保留"接收者 → 属性"这一跳（这是事实，不是类名断言）
        return ("{}.{}".format(receiver_type, name),) if receiver_type else ()

    @staticmethod
    def _profile_required(
        effective: str, missing: Iterable[str]
    ) -> tuple[dict[str, Any], ...]:
        """portable 状态下被"安全交集"挡掉的名字：单端能力 ⇒ ``PROFILE_REQUIRED``。

        判断只认 Registry：某个名字若在 desktop/browser 任一侧被批准、却不在交集里，
        就属于"必须先确定 profile"；两边都没有的名字仍算 ``missing``（真的不存在）。
        """
        if effective != registry.PORTABLE_PROFILE:
            return ()
        out: list[dict[str, Any]] = []
        for key in missing:
            owners = [
                profile
                for profile in registry.verifiable_profile_names()
                if registry.is_approved(profile, key, allow_login=True)
            ]
            if not owners:
                continue
            out.append(
                {
                    "name": str(key),
                    "code": registry.PROFILE_REQUIRED_CODE,
                    "message": registry.portable_reason(effective, key),
                    "profiles": owners,
                }
            )
        return tuple(out)

    @staticmethod
    def _single_receiver(contracts: list[Any], receiver_hint: str) -> str:
        """能确定**唯一**接收者才返回它；否则空串（调用方据此只记 VIEW）。"""
        if not contracts:
            return ""
        candidates: set[str] = set()
        for item in contracts:
            declared = set(item.receiver_types)
            if not declared:
                return ""
            if len(declared) == 1:
                candidates |= declared
                continue
            hint = str(receiver_hint or "").strip()
            if hint and hint in declared:
                candidates.add(hint)
                continue
            return ""
        return candidates.pop() if len(candidates) == 1 else ""

    # ── 查询 ──

    def get_ticket(self, ticket_id: str) -> ContractTicket | None:
        row = self.store.get_ticket(str(ticket_id))
        return ContractTicket.from_row(row) if row is not None else None

    def list_tickets(
        self,
        *,
        main_turn_id: str = "",
        capability_id: str = "",
        include_revoked: bool = False,
        limit: int = 50,
    ) -> tuple[ContractTicket, ...]:
        rows = self.store.list_tickets(
            main_turn_id=main_turn_id,
            capability_id=capability_id,
            include_revoked=include_revoked,
            limit=limit,
        )
        return tuple(ContractTicket.from_row(row) for row in rows)

    def verify(
        self,
        ticket_id: str,
        *,
        presented: Mapping[str, Any] | None = None,
        contract_revision: str | None = None,
        runtime_profile: str | None = None,
        environment_revision: str | None = None,
        permission_revoked: bool = False,
    ) -> TicketEvaluation:
        """校验一张票。**必须**按 id 从本地库取行 —— 不接受外部传进来的 ticket 对象。"""
        ticket = self.get_ticket(ticket_id)
        return evaluate_ticket(
            ticket,
            contract_revision=(
                self._revision() if contract_revision is None else str(contract_revision)
            ),
            runtime_profile=(
                ticket.runtime_profile
                if runtime_profile is None and ticket is not None
                else str(runtime_profile or "")
            ),
            environment_revision=environment_revision,
            permission_revoked=permission_revoked,
            presented=presented,
        )

    # ── 失效 ──

    def invalidate(self, ticket_id: str, *, reason: str) -> bool:
        return bool(self.store.revoke_ticket(str(ticket_id), reason=str(reason or "")))

    def reconcile_for_target(
        self,
        ticket_ids: Iterable[str],
        *,
        runtime_profile: str,
        receiver_hint: str = "",
        main_turn_id: str = "",
        environment_revision: str = "",
    ) -> tuple[ReconcileOutcome, ...]:
        """目标确定后核验/重签（决定 1）。

        - 票绑定的 profile 与目标一致 → ``verified``（或按失败原因 ``expired``/``mismatch``）；
        - 票是 **portable**、目标是具体 profile → 用具体 profile 的同名契约**重签**，
          并把旧票本地失效（保留行供审计）→ ``reissued``；
        - 目标 profile 里没有这条已批准契约 → ``kept_portable`` + ``PROFILE_REQUIRED``
          （**不猜**，也不假装有效）；
        - 具体 profile 的票不能在两个具体 profile 之间"迁移" → ``expired``。
        """
        effective = registry.effective_profile(runtime_profile)
        out: list[ReconcileOutcome] = []
        for raw_id in ticket_ids:
            ticket_id = str(raw_id or "")
            ticket = self.get_ticket(ticket_id)
            if ticket is None:
                out.append(
                    ReconcileOutcome(
                        ticket_id,
                        "missing",
                        validator.API_TICKET_MISSING,
                        "本地没有这张凭据",
                    )
                )
                continue
            if ticket.runtime_profile == effective:
                verdict = self.verify(
                    ticket.ticket_id, contract_revision=self._revision(), runtime_profile=effective
                )
                status = "verified" if verdict.ok else (
                    "expired" if verdict.code == validator.API_TICKET_EXPIRED else "mismatch"
                )
                out.append(
                    ReconcileOutcome(ticket.ticket_id, status, verdict.code, verdict.reason)
                )
                continue
            if not ticket.is_portable():
                out.append(
                    ReconcileOutcome(
                        ticket.ticket_id,
                        "expired",
                        validator.API_TICKET_EXPIRED,
                        "凭据绑定 {}，当前目标 {}；必须重新查询契约".format(
                            ticket.runtime_profile, effective
                        ),
                    )
                )
                continue
            out.append(
                self._reissue_portable(
                    ticket,
                    runtime_profile=effective,
                    receiver_hint=receiver_hint,
                    main_turn_id=main_turn_id,
                    environment_revision=environment_revision,
                )
            )
        return tuple(out)

    def reconcile_portable_for_turn(
        self,
        *,
        main_turn_id: str,
        runtime_profile: str,
        receiver_hint: str = "",
        environment_revision: str = "",
    ) -> tuple[ReconcileOutcome, ...]:
        """把某个主轮里所有 portable 票按新目标核验/重签（runner 写目标时调用）。"""
        portable = [
            ticket.ticket_id
            for ticket in self.list_tickets(main_turn_id=main_turn_id)
            if ticket.is_portable()
        ]
        if not portable:
            return ()
        return self.reconcile_for_target(
            portable,
            runtime_profile=runtime_profile,
            receiver_hint=receiver_hint,
            main_turn_id=main_turn_id,
            environment_revision=environment_revision,
        )

    def _reissue_portable(
        self,
        ticket: ContractTicket,
        *,
        runtime_profile: str,
        receiver_hint: str,
        main_turn_id: str,
        environment_revision: str,
    ) -> ReconcileOutcome:
        item = registry.contract(runtime_profile, ticket.requested_method)
        if item is None:
            return ReconcileOutcome(
                ticket.ticket_id,
                "kept_portable",
                registry.PROFILE_REQUIRED_CODE,
                "当前目标 {} 没有已批准的 {}；保留 portable 语义".format(
                    runtime_profile, ticket.requested_method
                ),
            )
        receiver = self._single_receiver([item], receiver_hint)
        if not receiver:
            return ReconcileOutcome(
                ticket.ticket_id,
                "kept_portable",
                registry.PROFILE_REQUIRED_CODE,
                "当前目标 {} 无法确定 {} 的接收者".format(runtime_profile, item.name),
            )
        fresh = build_ticket(
            contract=item,
            receiver_type=receiver,
            contract_revision=self._revision(),
            runtime_profile=runtime_profile,
            profile_source=ticket.profile_source,
            main_turn_id=ticket.main_turn_id or main_turn_id,
            view_event_id=ticket.view_event_id,
            environment_revision=environment_revision or ticket.environment_revision,
            permission_id=ticket.permission_id,
            permission_scope=ticket.permission_scope,
            resource_scope=ticket.resource_scope,
            # 重签的也是**正式凭据**：真实定义类与解析链必须一起补上（决定 3）
            resolution_chain=self._resolution_chain(runtime_profile, item.name, receiver),
            resolved_owner=self._resolved_owner(runtime_profile, item.name),
            issued_at=self._clock(),
            ticket_id=self._id_factory(),
        )
        self.store.put_ticket(fresh.to_row())
        reason = "已重签为具体 profile：{}（{}）".format(runtime_profile, fresh.ticket_id)
        self.store.revoke_ticket(ticket.ticket_id, reason=reason)
        self._emit(
            EVENT_CALL_AUTHORIZED,
            {
                "ticket_ids": [fresh.ticket_id],
                "capability_ids": [fresh.capability_id],
                "contract_revision": fresh.contract_revision,
                "runtime_profile": runtime_profile,
                "receiver_type": receiver,
                "portable": False,
                "supersedes": ticket.ticket_id,
                "permission_id": fresh.permission_id,
            },
            main_turn_id=fresh.main_turn_id,
            action_id="reissue",
        )
        return ReconcileOutcome(
            ticket.ticket_id, "reissued", "", reason, new_ticket_id=fresh.ticket_id
        )

    def invalidate_stale(
        self,
        *,
        contract_revision: str | None = None,
        runtime_profile: str | None = None,
        environment_revision: str | None = None,
        limit: int = 500,
    ) -> tuple[ContractTicket, ...]:
        """把"事实已经变了"的票批量本地失效（设计 §11）。

        只按**调用方给出的维度**判定：没给的维度不动（避免拿空值当"变了"）。
        """
        target_revision = self._revision() if contract_revision is None else str(contract_revision)
        revoked: list[ContractTicket] = []
        moment = self._clock()
        for ticket in self.list_tickets(limit=limit):
            reasons: list[str] = []
            if contract_revision is not None and ticket.contract_revision != target_revision:
                reasons.append(
                    "契约版本变化：{} -> {}".format(ticket.contract_revision, target_revision)
                )
            if runtime_profile is not None and ticket.runtime_profile != str(runtime_profile):
                reasons.append(
                    "执行 profile 变化：{} -> {}".format(ticket.runtime_profile, runtime_profile)
                )
            if (
                environment_revision is not None
                and ticket.environment_revision != str(environment_revision)
            ):
                reasons.append(
                    "环境版本变化：{} -> {}".format(
                        ticket.environment_revision, environment_revision
                    )
                )
            if not reasons:
                continue
            reason = "；".join(reasons)
            if self.store.revoke_ticket(ticket.ticket_id, reason=reason):
                revoked.append(
                    replace(ticket, revoked_at=moment, revoked_reason=reason)
                )
        return tuple(revoked)

    # ── 内部 ──

    def _emit(self, event_type: str, payload: dict[str, Any], **ids: Any) -> str:
        """写一条时间线事件，返回它的 ``event_id``（没有出口时返回空串）。"""
        if self.events is None:
            return ""
        envelope = self.events(event_type, payload, **ids)
        return str(getattr(envelope, "event_id", "") or "")


# ── 进程级默认服务（V2 的 lookup_api 走这里；V3 直接用自己那个 store）──

_SERVICE: TicketService | None = None


def set_default_service(service: TicketService | None) -> None:
    global _SERVICE
    _SERVICE = service


def default_service() -> TicketService | None:
    return _SERVICE


def clear_default_service() -> None:
    set_default_service(None)
