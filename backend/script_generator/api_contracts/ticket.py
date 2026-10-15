"""契约凭据（ticket）：本模块只做**纯值对象 + 纯判定**，不落库、不发事件。

设计依据（§5/§6/§11/§12）：

* 身份只认 Registry ID：``capability_id == ApiContract.id``，不造第二套命名；
* ticket 是**签发时刻的契约快照** + ``contract_revision`` 绑定。它不维护自己的方法表：
  版本一变就失效（``API_TICKET_EXPIRED``），而不是继续被信任；
* **完整性 = 内容摘要 + "本地那一行才是权威"**。摘要算法是公开的，所以摘要本身
  不防伪造；防伪造靠的是"必须能在本地库里查到这一行，且行内字段与请示回来的字段一致"。
  LLM 自己拼一张票（哪怕摘要自洽）查不到行 ⇒ ``API_TICKET_MISSING``；
  拿真票改字段 ⇒ 摘要对不上 ⇒ ``API_TICKET_MISMATCH``。
  这样 Phase 1 不需要引入密钥管理；将来若要离线校验再升级成 HMAC（不影响字段语义）。
* **为什么不现在上 HMAC（人工决定 2）**：当前信任边界就是本地 SQLite。
  校验一律按 ``ticket_id`` 读权威记录并**重新计算摘要**，从不信任 LLM 提交的载荷；
  随机 id + 只读字段 + 库内唯一行 + 内容摘要已经覆盖"模型编造/篡改凭据"这一威胁。
  只有出现**导出凭据**、**脱离数据库校验**或**跨设备传递**时才需要 HMAC 与密钥管理 ——
  现在加只会扩大密钥生命周期与部署复杂度，不会提高 API 幻觉识别效果。
* ``resolved_owner`` 必须来自**真实解析**；默认空串 = unresolved。
  ``ApiContract.owner`` 是占位值（恒为 ``browser``），**不得**当作"定义该方法的类"写进凭据（决定 3）。
* 权限**只存引用与作用域摘要**，事实本体仍在 ``permission_grants``（§12：三者不合并）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from typing import Any, Mapping

from . import registry, validator
from .schema import ApiContract

#: ticket id 形状：``ct_<16 hex>``。它**不是**层级 ID（task/turn/run/...），
#: 所以不登记进 ``runtime/ids.py`` 的 ID_KINDS，只做自己的形状校验。
TICKET_ID_PREFIX = "ct_"
_TICKET_ID_HEX = 16

TICKET_SCHEMA_VERSION = 1
INTEGRITY_ALGORITHM = "sha256"
INTEGRITY_PREFIX = INTEGRITY_ALGORITHM + ":"

TICKET_STATUS_OK = "ok"
TICKET_STATUS_MISSING = "missing"
TICKET_STATUS_EXPIRED = "expired"
TICKET_STATUS_MISMATCH = "mismatch"

#: 参与完整性计算的字段（顺序即摘要顺序）。``revoked_*`` 是**本地事后**写上的状态，
#: 不进摘要：失效不改写"签发时的事实"，只让判定结果变化。
_PAYLOAD_FIELDS: tuple[str, ...] = (
    "ticket_id",
    "schema_version",
    "main_turn_id",
    "view_event_id",
    "contract_revision",
    "capability_id",
    "receiver_type",
    "requested_method",
    "resolved_owner",
    "resolution_chain",
    "runtime_profile",
    "profile_source",
    "call_kind",
    "parameters_required",
    "parameters_allowed",
    "return_model",
    "permission_id",
    "permission_scope",
    "resource_scope",
    "environment_revision",
    "issued_at",
)

#: ``presented`` 比对时**不允许**被外部指定的字段：身份与本地状态。
_PRESENTED_READONLY = frozenset({"ticket_id", "integrity", "revoked_at", "revoked_reason"})


def new_ticket_id() -> str:
    """后端签发的新 ticket id（前端与 LLM 只能引用，不能生成）。"""
    return TICKET_ID_PREFIX + secrets.token_hex(_TICKET_ID_HEX // 2)


def is_ticket_id(value: object) -> bool:
    """形状校验（只判形状，不代表有效：有效性必须查本地库）。"""
    text = str(value or "").strip()
    if not text.startswith(TICKET_ID_PREFIX):
        return False
    tail = text[len(TICKET_ID_PREFIX) :]
    return len(tail) == _TICKET_ID_HEX and all(c in "0123456789abcdef" for c in tail)


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(payload: Mapping[str, Any]) -> str:
    raw = _canonical(payload)
    return INTEGRITY_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractTicket:
    """一张契约凭据。字段与设计 §6 一一对应。"""

    ticket_id: str
    schema_version: int
    main_turn_id: str
    view_event_id: str
    contract_revision: str
    capability_id: str
    receiver_type: str
    requested_method: str
    resolved_owner: str
    resolution_chain: tuple[str, ...]
    runtime_profile: str
    profile_source: str
    call_kind: str
    parameters_required: tuple[str, ...]
    parameters_allowed: tuple[str, ...]
    return_model: Mapping[str, Any]
    #: 关联的权限事实 id（**不复制**权限内容，设计 §12）。
    permission_id: str = ""
    permission_scope: tuple[str, ...] = ()
    resource_scope: tuple[str, ...] = ()
    environment_revision: str = ""
    issued_at: str = ""
    integrity: str = ""
    #: 本地事后失效（保留行，方便审计"当时签发过、后来为什么失效"）。
    revoked_at: str = ""
    revoked_reason: str = ""

    # ── 判定 ──

    def is_portable(self) -> bool:
        """是不是**交集凭据**（执行目标未确定时签发）。

        portable 票只证明"这条能力在两端交集里、模型看过它的契约"；
        执行目标一旦确定，必须按具体 profile 重新核验或重签（决定 1）。
        """
        return not self.receiver_type and self.runtime_profile == registry.PORTABLE_PROFILE

    # ── 完整性 ──

    def payload(self) -> dict[str, Any]:
        """参与摘要的内容（稳定、可复算）。"""
        return {
            "ticket_id": self.ticket_id,
            "schema_version": int(self.schema_version),
            "main_turn_id": self.main_turn_id,
            "view_event_id": self.view_event_id,
            "contract_revision": self.contract_revision,
            "capability_id": self.capability_id,
            "receiver_type": self.receiver_type,
            "requested_method": self.requested_method,
            "resolved_owner": self.resolved_owner,
            "resolution_chain": list(self.resolution_chain),
            "runtime_profile": self.runtime_profile,
            "profile_source": self.profile_source,
            "call_kind": self.call_kind,
            "parameters_required": list(self.parameters_required),
            "parameters_allowed": list(self.parameters_allowed),
            "return_model": dict(self.return_model),
            "permission_id": self.permission_id,
            "permission_scope": list(self.permission_scope),
            "resource_scope": list(self.resource_scope),
            "environment_revision": self.environment_revision,
            "issued_at": self.issued_at,
        }

    def compute_integrity(self) -> str:
        return _digest(self.payload())

    def verify_integrity(self) -> bool:
        return bool(self.integrity) and secrets.compare_digest(
            self.integrity, self.compute_integrity()
        )

    # ── 序列化 ──

    def to_dict(self) -> dict[str, Any]:
        payload = self.payload()
        payload.update(
            {
                "integrity": self.integrity,
                "revoked_at": self.revoked_at,
                "revoked_reason": self.revoked_reason,
            }
        )
        return payload

    def to_row(self) -> dict[str, Any]:
        """落库行（列名与 ``sqlite_store._TICKET_COLUMNS`` 一致，JSON 列已序列化）。"""
        return {
            "ticket_id": self.ticket_id,
            "schema_version": int(self.schema_version),
            "main_turn_id": self.main_turn_id,
            "view_event_id": self.view_event_id,
            "contract_revision": self.contract_revision,
            "capability_id": self.capability_id,
            "receiver_type": self.receiver_type,
            "requested_method": self.requested_method,
            "resolved_owner": self.resolved_owner,
            "resolution_chain_json": _canonical(list(self.resolution_chain)),
            "runtime_profile": self.runtime_profile,
            "profile_source": self.profile_source,
            "call_kind": self.call_kind,
            "parameters_required_json": _canonical(list(self.parameters_required)),
            "parameters_allowed_json": _canonical(list(self.parameters_allowed)),
            "return_model_json": _canonical(dict(self.return_model)),
            "permission_id": self.permission_id,
            "permission_scope_json": _canonical(list(self.permission_scope)),
            "resource_scope_json": _canonical(list(self.resource_scope)),
            "environment_revision": self.environment_revision,
            "issued_at": self.issued_at,
            "integrity": self.integrity,
            "revoked_at": self.revoked_at,
            "revoked_reason": self.revoked_reason,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ContractTicket":
        def _text(key: str) -> str:
            value = row.get(key)
            return "" if value is None else str(value)

        def _items(key: str) -> tuple[str, ...]:
            raw = row.get(key)
            if not raw:
                return ()
            loaded = json.loads(raw) if isinstance(raw, str) else raw
            return tuple(str(item) for item in loaded or ())

        raw_model = row.get("return_model_json") or "{}"
        model = json.loads(raw_model) if isinstance(raw_model, str) else dict(raw_model)
        return cls(
            ticket_id=_text("ticket_id"),
            schema_version=int(row.get("schema_version") or 0),
            main_turn_id=_text("main_turn_id"),
            view_event_id=_text("view_event_id"),
            contract_revision=_text("contract_revision"),
            capability_id=_text("capability_id"),
            receiver_type=_text("receiver_type"),
            requested_method=_text("requested_method"),
            resolved_owner=_text("resolved_owner"),
            resolution_chain=_items("resolution_chain_json"),
            runtime_profile=_text("runtime_profile"),
            profile_source=_text("profile_source"),
            call_kind=_text("call_kind"),
            parameters_required=_items("parameters_required_json"),
            parameters_allowed=_items("parameters_allowed_json"),
            return_model=model,
            permission_id=_text("permission_id"),
            permission_scope=_items("permission_scope_json"),
            resource_scope=_items("resource_scope_json"),
            environment_revision=_text("environment_revision"),
            issued_at=_text("issued_at"),
            integrity=_text("integrity"),
            revoked_at=_text("revoked_at"),
            revoked_reason=_text("revoked_reason"),
        )


def build_ticket(
    *,
    contract: ApiContract,
    receiver_type: str,
    contract_revision: str,
    runtime_profile: str,
    profile_source: str,
    main_turn_id: str = "",
    view_event_id: str = "",
    environment_revision: str = "",
    permission_id: str = "",
    permission_scope: tuple[str, ...] = (),
    resource_scope: tuple[str, ...] = (),
    resolution_chain: tuple[str, ...] | None = None,
    resolved_owner: str = "",
    issued_at: str = "",
    ticket_id: str = "",
) -> ContractTicket:
    """按 Registry 契约签发一张票。

    ``receiver_type`` **必须**是契约声明的候选之一（一般只有一个）；portable 契约有两个
    候选时，调用方必须按真实执行目标给出其中一个 —— 猜不出来就不许签，这里直接拒。

    ``portable=True``（``receiver_type=""``）时签的是**交集凭据**：只对
    ``portable_common`` 的契约允许，且必须在执行目标确定后重新核验/重签（决定 1）。

    ``resolved_owner`` 必须来自**真实解析**（Phase 2 的 ``verify_runtime.resolve_method``）；
    默认空串 = unresolved。**不要**把 ``ApiContract.owner`` 传进来：那是占位值
    （恒为 ``browser``），不是"真正定义该方法的类"（决定 3）。
    """
    key = str(receiver_type or "").strip()
    portable = not key
    if portable:
        if contract.runtime_profile != registry.PORTABLE_PROFILE:
            raise ValueError(
                "只有 {} 的契约可以签 portable ticket，{} 不行".format(
                    registry.PORTABLE_PROFILE, contract.id
                )
            )
    elif key not in tuple(contract.receiver_types):
        raise ValueError(
            "receiver_type {!r} 不在契约 {} 声明的候选 {} 里".format(
                receiver_type, contract.id, list(contract.receiver_types)
            )
        )
    if resolution_chain is None:
        resolution_chain = ("{}.{}".format(key, contract.name),) if key else ()
    draft = ContractTicket(
        ticket_id=str(ticket_id or new_ticket_id()),
        schema_version=TICKET_SCHEMA_VERSION,
        main_turn_id=str(main_turn_id or ""),
        view_event_id=str(view_event_id or ""),
        contract_revision=str(contract_revision or ""),
        capability_id=contract.id,
        receiver_type=key,
        requested_method=contract.name,
        resolved_owner=str(resolved_owner or ""),
        resolution_chain=tuple(resolution_chain),
        runtime_profile=str(runtime_profile or contract.runtime_profile),
        profile_source=str(profile_source or ""),
        call_kind=contract.call_kind,
        parameters_required=tuple(sorted(contract.required_keyword_names())),
        parameters_allowed=tuple(sorted(contract.keyword_names())),
        return_model={
            "type": contract.returns.type,
            "truthiness": contract.returns.truthiness,
            "fields": list(contract.returns.fields),
            "identity_check_unsafe": bool(contract.returns.identity_check_unsafe),
            "never_proves": list(contract.returns.never_proves),
            "lifetime": contract.returns.lifetime,
            **contract.returns.machine_facts(),
        },
        permission_id=str(permission_id or ""),
        permission_scope=tuple(permission_scope),
        resource_scope=tuple(resource_scope),
        environment_revision=str(environment_revision or ""),
        issued_at=str(issued_at or ""),
        integrity="",
    )
    return replace(draft, integrity=draft.compute_integrity())


def presented_matches(
    ticket: ContractTicket, presented: Mapping[str, Any]
) -> tuple[bool, str]:
    """把"请示回来的字段"与本地那一行逐项比对。

    只比对**库里有的事实**；``ticket_id`` / ``integrity`` / ``revoked_*`` 属于身份与
    本地状态，不接受外部指定（在 ``_PRESENTED_READONLY`` 里直接拒）。
    """
    facts = ticket.payload()
    for name, value in dict(presented or {}).items():
        if name in _PRESENTED_READONLY:
            return False, "{} 不允许由调用方指定".format(name)
        if name not in facts:
            return False, "未知字段 {}".format(name)
        expected = facts[name]
        actual = list(value) if isinstance(expected, list) else value
        if actual != expected:
            return False, "{} 与本地凭据不符：{!r} != {!r}".format(name, actual, expected)
    return True, ""


def evaluate_ticket(
    ticket: ContractTicket | None,
    *,
    contract_revision: str,
    runtime_profile: str,
    environment_revision: str | None = None,
    permission_revoked: bool = False,
    presented: Mapping[str, Any] | None = None,
) -> TicketEvaluation:
    """判定一张票当前是否仍然可用（设计 §11：失效由版本与作用域决定，不只看时间）。

    顺序固定：**先证身份与完整性，再比版本，最后比 profile**。任何一步不过都返回
    稳定诊断码，调用方（Phase 3 的硬闸）据此决定拒绝还是要求重查。
    """
    if ticket is None:
        return TicketEvaluation(
            TICKET_STATUS_MISSING,
            validator.API_TICKET_MISSING,
            "本地没有这张凭据：必须先查询契约取得当前 ticket",
        )
    if not ticket.verify_integrity():
        return TicketEvaluation(
            TICKET_STATUS_MISMATCH,
            validator.API_TICKET_MISMATCH,
            "凭据完整性校验失败（字段被改过，或不是本地签发的）",
            ticket,
        )
    if presented:
        matches, why = presented_matches(ticket, presented)
        if not matches:
            return TicketEvaluation(
                TICKET_STATUS_MISMATCH,
                validator.API_TICKET_MISMATCH,
                "凭据字段与本地记录不符：{}".format(why),
                ticket,
            )
    if ticket.revoked_at:
        return TicketEvaluation(
            TICKET_STATUS_EXPIRED,
            validator.API_TICKET_EXPIRED,
            "凭据已被本地失效：{}".format(ticket.revoked_reason or "未注明原因"),
            ticket,
        )
    if permission_revoked:
        return TicketEvaluation(
            TICKET_STATUS_EXPIRED,
            validator.API_TICKET_EXPIRED,
            "关联的权限已被撤销，凭据随之失效",
            ticket,
        )
    if str(contract_revision or "") != ticket.contract_revision:
        return TicketEvaluation(
            TICKET_STATUS_EXPIRED,
            validator.API_TICKET_EXPIRED,
            "契约版本已变化：{} -> {}".format(
                ticket.contract_revision, contract_revision
            ),
            ticket,
        )
    if environment_revision is not None and str(environment_revision) != ticket.environment_revision:
        return TicketEvaluation(
            TICKET_STATUS_EXPIRED,
            validator.API_TICKET_EXPIRED,
            "环境版本已变化：{} -> {}".format(
                ticket.environment_revision, environment_revision
            ),
            ticket,
        )
    if str(runtime_profile or "") != ticket.runtime_profile:
        return TicketEvaluation(
            TICKET_STATUS_MISMATCH,
            validator.API_TICKET_MISMATCH,
            "执行 profile 与凭据不符：{} != {}".format(
                runtime_profile, ticket.runtime_profile
            ),
            ticket,
        )
    return TicketEvaluation(TICKET_STATUS_OK, "", "", ticket)


@dataclass(frozen=True)
class TicketEvaluation:
    """一次校验的结论（``ok=False`` 时 ``code`` 是稳定诊断码）。"""

    status: str
    code: str
    reason: str
    ticket: ContractTicket | None = None

    @property
    def ok(self) -> bool:
        return self.status == TICKET_STATUS_OK

