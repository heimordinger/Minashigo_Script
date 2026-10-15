"""可执行 API 契约（设计文档 ``design_executable_api_contract.md`` §4-§9）。

单一事实源：``registry`` 里的 :class:`~.schema.ApiContract`。
以下全部从它派生，禁止再维护第二份：

- ``api.search`` / ``api.get`` 查询结果（``render.search`` / ``render.details``）；
- 给 LLM 的短能力索引与详细文档（``render.index_text`` / ``render.detail_text``）；
- AST 调用白名单、同步/异步检查、参数检查（Phase 2 契约校验器读 registry）；
- 契约—真实实现一致性测试（``verify_runtime.verify_all``）；
- Python 能力策略（``python_policy``）。
"""

from __future__ import annotations

from . import python_policy, registry, render, validator, verify_runtime
from . import callsite, reconcile, ticket, ticket_service
from .registry import (
    LEGACY_DEFAULT_PROFILE,
    PORTABLE_PROFILE,
    PROFILE_REQUIRED_CODE,
    UNRESOLVED_PROFILE,
    ProfileRequiredError,
    approved_contracts,
    approved_names,
    contract,
    contracts_revision,
    diagnostic_contracts,
    effective_profile,
    executable_profile_names,
    get_profile,
    has_profile,
    is_approved,
    nearest_names,
    portable_reason,
    profile_names,
    profile_revision,
    receiver_types,
    revision_summary,
    verifiable_profile_names,
)
from .render import (
    allow_login_from_explanation,
    card_text,
    compact_catalog,
    contracts_block,
    detail,
    detail_text,
    details,
    index_item,
    index_text,
    lookup_text,
    rejection_text,
    search,
    signature_of,
    usage_facts_line,
)
from .schema import (
    ASYNC_METHOD,
    SYNC_METHOD,
    ApiContract,
    Parameter,
    ProfileContracts,
    ReturnContract,
)
from . import ticket, ticket_service

__all__ = [
    "ASYNC_METHOD",
    "ApiContract",
    "LEGACY_DEFAULT_PROFILE",
    "PORTABLE_PROFILE",
    "PROFILE_REQUIRED_CODE",
    "Parameter",
    "ProfileContracts",
    "ProfileRequiredError",
    "ReturnContract",
    "SYNC_METHOD",
    "UNRESOLVED_PROFILE",
    "allow_login_from_explanation",
    "approved_contracts",
    "approved_names",
    "card_text",
    "callsite",
    "compact_catalog",
    "contract",
    "contracts_block",
    "contracts_revision",
    "detail",
    "detail_text",
    "details",
    "diagnostic_contracts",
    "effective_profile",
    "executable_profile_names",
    "get_profile",
    "has_profile",
    "index_item",
    "index_text",
    "is_approved",
    "lookup_text",
    "nearest_names",
    "portable_reason",
    "profile_names",
    "profile_revision",
    "python_policy",
    "receiver_types",
    "reconcile",
    "registry",
    "rejection_text",
    "render",
    "revision_summary",
    "search",
    "signature_of",
    "ticket",
    "ticket_service",
    "usage_facts_line",
    "validator",
    "verify_runtime",
    "verifiable_profile_names",
]
