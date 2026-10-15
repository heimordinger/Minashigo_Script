"""把 registry 渲染成给 LLM / 人 / 旧调用方看的文本与结构（设计 §6/§7）。

**唯一事实源**：这里的每个签名都来自 ``registry``，不得手写第二份。
``lookup_text`` / ``rejection_text`` 保持旧 ``api_catalog`` 的输出形状，
以便 facade 平滑迁移（设计 §9 阶段 A / §29「保持旧接口兼容」）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from . import python_policy, registry, verify_runtime
from .schema import ApiContract, ProfileContracts

#: 判定"这是不是一个登录/网页任务"（旧 api_catalog 的同一正则，迁到此处以免循环导入）。
_LOGIN_HINT_RE = re.compile(
    r"登录|登入|网页|dmm|goto|打开游戏页|跳转.*http|浏览器地址",
    re.I,
)


def allow_login_from_explanation(explanation: str) -> bool:
    return bool(_LOGIN_HINT_RE.search(explanation or ""))


#: ⚠️ 已删除旧 ``_RETURN_TYPE_BANNER`` / ``return_type_banner()``。
#: 它是 **profile 无关**文案，会在 desktop 的生成 prompt 里教 ``match_image_multi``
#: 和 ``browser.click(best['x'], best['y'])`` —— 而 desktop 明确禁止该方法
#: （决定 §13.4）。等价信息已经由 registry 每条契约的 ``returns.truthiness`` /
#: ``returns.fields`` / ``never_proves`` / ``guidance`` 按 profile 渲染，
#: 不需要第二份手写文案（设计 §4.1「禁止再维护一套 prompt 文本」）。


def signature_of(item: ApiContract) -> str:
    return item.qualified_signature()


def _spec(profile: str | None) -> ProfileContracts:
    """解析 profile：缺失 / 未确定 → ``portable_common`` 安全交集。

    **绝不**落 desktop/browser（决定 §13.1）。单端能力不会出现在交集里，
    需要它们的调用方必须先把 profile 确定下来。
    """
    return registry.get_profile(registry.effective_profile(profile))


def index_item(item: ApiContract) -> dict[str, Any]:
    """``api.search`` 的单项（只给索引，不给全文）。"""
    return {
        "id": item.id,
        "name": "{}.{}".format(item.owner, item.name),
        "summary": item.summary,
        "call_kind": item.call_kind,
        "signature": item.signature(),
        "approved": item.approved,
        "proxied": item.proxied,
    }


def search(
    profile: str | None = None,
    query: str = "",
    *,
    limit: int = 20,
    allow_login: bool = False,
    include_unapproved: bool = False,
) -> dict[str, Any]:
    """``api.search``：按关键词过滤的紧凑索引。"""
    spec = _spec(profile)
    needle = str(query or "").strip().lower()
    items: list[dict[str, Any]] = []
    pool = spec.contracts if include_unapproved else spec.approved_contracts(allow_login=allow_login)
    for item in pool:
        if needle:
            haystack = " ".join(
                [item.name, item.summary, item.returns.type, " ".join(item.tags), " ".join(item.semantics)]
            ).lower()
            if needle not in haystack:
                continue
        items.append(index_item(item))
    total = len(items)
    if limit and total > int(limit):
        items = items[: int(limit)]
    return {
        "runtime_profile": spec.runtime_profile,
        "contracts_revision": registry.contracts_revision(),
        "total": total,
        "items": items,
    }


def index_text(profile: str | None = None, *, allow_login: bool = False) -> str:
    """给 prompt 的紧凑索引（名字 + 同步/异步 + 签名），不含正文。"""
    spec = _spec(profile)
    lines = [
        "runtime_profile={}".format(spec.runtime_profile),
        "允许能力：Python 基础 + python_policy 白名单 + 本 profile 的 API 契约。",
        "写脚本前先批量查 api.get 取得本次要调用的全部方法；不得调用契约外的方法。",
        "可用方法（{} 个）：".format(len(spec.approved_names(allow_login=allow_login))),
    ]
    for item in spec.approved_contracts(allow_login=allow_login):
        lines.append("  - {:<6} {}".format(item.call_kind.replace("_method", ""), item.signature()))
    return "\n".join(lines)


def detail(
    profile: str | None,
    item_or_name: ApiContract | str,
    *,
    include_unapproved: bool = False,
) -> dict[str, Any] | None:
    """``api.get`` 的单项完整结构。

    默认**只返回已批准条目**（决定 §1：``api.get`` 对未批准能力返回
    ``API_NOT_APPROVED``，只有本地迁移/诊断可 ``include_unapproved=True``）。
    """
    spec = _spec(profile)
    item = item_or_name if isinstance(item_or_name, ApiContract) else spec.by_name(str(item_or_name))
    if item is None:
        return None
    if not item.approved and not include_unapproved:
        return None
    return {
        "id": item.id,
        "runtime_profile": item.runtime_profile,
        "name": "{}.{}".format(item.owner, item.name),
        "call_kind": item.call_kind,
        "approved": item.approved,
        "proxied": item.proxied,
        "summary": item.summary,
        "signature": item.signature(),
        "parameters": [
            {
                "name": p.name,
                "kind": p.kind,
                "type": p.type,
                "required": p.required,
                "default": p.default,
                "doc": p.doc,
            }
            for p in item.parameters
        ],
        "accepts_kwargs": item.accepts_kwargs,
        "returns": {
            "type": item.returns.type,
            "truthiness": item.returns.truthiness,
            "fields": list(item.returns.fields),
            "identity_check_unsafe": item.returns.identity_check_unsafe,
            "never_proves": list(item.returns.never_proves),
            "lifetime": item.returns.lifetime,
            # 机器可读事实（§10）：该不该用 len / for / mapping 取值，看这几个。
            **item.returns.machine_facts(),
        },
        "coordinate_space": item.coordinate_space,
        "semantics": list(item.semantics),
        "side_effects": list(item.side_effects),
        "preconditions": list(item.preconditions),
        "errors": list(item.errors),
        "examples_valid": list(item.examples_valid),
        "examples_invalid": list(item.examples_invalid),
        "tags": list(item.tags),
    }


def details(
    profile: str | None,
    names: Iterable[str],
    *,
    include_unapproved: bool = False,
) -> dict[str, Any]:
    """批量 ``api.get``：一次查询拿到本次生成需要的全部方法（设计 §6.2）。

    未批准能力**不返回契约正文**，而是进 ``rejected`` 并带
    ``API_NOT_APPROVED``（决定 §1：模型即使知道 contract id 也用不上）。
    """
    spec = _spec(profile)
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    rejected: list[dict[str, Any]] = []
    for name in names or ():
        key = str(name or "").strip()
        if not key:
            continue
        if "." in key:
            key = key.rsplit(".", 1)[-1]
        item = spec.by_name(key)
        if item is None:
            missing.append(key)
            continue
        if not item.approved and not include_unapproved:
            rejected.append(
                {
                    "name": key,
                    "code": "API_NOT_APPROVED",
                    "message": "{} 未获批准用于本 profile".format(key),
                    "reason": "；".join(item.semantics) or "该能力未登记为可用",
                }
            )
            continue
        found.append(detail(spec.runtime_profile, item, include_unapproved=True) or {})
    return {
        "runtime_profile": spec.runtime_profile,
        "contracts_revision": registry.contracts_revision(),
        "items": found,
        "missing": missing,
        "rejected": rejected,
    }


def detail_text(profile: str | None, item: ApiContract) -> str:
    """人/模型可读的单个契约全文。"""
    lines = ["## {}".format(item.name), signature_of(item)]
    if item.summary:
        lines.append(item.summary)
    if item.returns.type:
        lines.append("返回：{}".format(item.returns.type))
    if item.returns.truthiness:
        lines.append("真假语义：{}".format(item.returns.truthiness))
    if item.returns.fields:
        lines.append("可用字段：{}".format(", ".join(item.returns.fields)))
    if item.coordinate_space:
        lines.append("坐标系：{}".format(item.coordinate_space))
    for note in item.semantics:
        lines.append("说明：{}".format(note))
    for note in item.preconditions:
        lines.append("前置：{}".format(note))
    for note in item.side_effects:
        lines.append("副作用：{}".format(note))
    for note in item.errors:
        lines.append("错误：{}".format(note))
    for sample in item.examples_valid:
        lines.append("正例：{}".format(sample))
    for sample in item.examples_invalid:
        lines.append("反例：{}".format(sample))
    if item.guidance:
        lines.append(item.guidance)
    return "\n".join(lines)


def usage_facts_line(item: ApiContract) -> str:
    """把返回模型的**机器可读事实**写成一句话（设计 §10）。

    未知维度（``None``）**不提**：提了会被读成"确定不是"，而未知只意味着"不确定"。
    """
    facts = item.returns
    bits: list[str] = []
    if facts.iterable is not None:
        bits.append("可迭代" if facts.iterable else "不可迭代")
    if facts.sized is not None:
        bits.append("可 len()" if facts.sized else "**不可 len()**")
    if facts.mapping is not None:
        bits.append("可按名字取值（mapping）" if facts.mapping else "不是 mapping")
    if facts.nullable:
        bits.append("返回值本身可能为 None")
    if facts.nullable_fields:
        bits.append("可空字段：{}".format(", ".join(facts.nullable_fields)))
    if facts.subscript_keys:
        bits.append("可按这些键取值：{}".format(", ".join(facts.subscript_keys)))
    if facts.indexable:
        bits.append("可用整数下标取值")
    if facts.collection is True:
        bits.append("迭代得到**多个结果**")
    elif facts.collection is False and facts.iterable:
        bits.append("迭代只是**单个结果的部分**（不是多个匹配目标，数数量要换用返回集合的多目标能力）")
    return "返回用法：{}".format("；".join(bits)) if bits else ""


def card_text(profile: str | None, item: ApiContract) -> str:
    """旧 ``lookup_api`` 返回的卡片体（无 ``## name`` 前缀）。"""
    parts = [signature_of(item)]
    if item.returns.truthiness:
        parts.append("返回真假：{}".format(item.returns.truthiness))
    facts_line = usage_facts_line(item)
    if facts_line:
        parts.append(facts_line)
    if item.returns.fields:
        parts.append("可用字段：{}".format(", ".join(item.returns.fields)))
    if item.guidance:
        parts.append(item.guidance)
    else:
        for note in item.semantics:
            parts.append("说明：{}".format(note))
        for sample in item.examples_valid:
            parts.append("正例：{}".format(sample))
        for sample in item.examples_invalid:
            parts.append("反例：{}".format(sample))
    return "\n".join(parts)


def rejection_text(
    profile: str | None,
    name: str,
    *,
    allow_login: bool = False,
) -> str:
    """不允许调用的说明：给理由 + 最接近的合法能力，但**不**自动替换（设计 §10.4）。"""
    spec = _spec(profile)
    key = str(name or "").strip()
    allowed = ", ".join(spec.approved_names(allow_login=allow_login))
    item = spec.by_name(key)
    lines: list[str] = []
    if item is not None and not item.approved:
        lines.append("{!r} 不属于本 profile 的协作脚本能力面。".format(key))
        for note in item.semantics:
            lines.append("原因：{}".format(note))
    elif item is not None and item.name in spec.login_methods and not allow_login:
        lines.append("{!r} 只在登录/网页任务里允许（allow_login），当前任务不允许。".format(key))
    else:
        lines.append("{!r} 不在契约里（本 profile 未登记）。".format(key))
        if verify_runtime.real_function(spec.runtime_profile, key) is not None:
            lines.append(
                "注意：{} 在真实执行类上确实存在，但没有登记成本 profile 的协作脚本能力面。".format(key)
            )
        elif verify_runtime.is_reachable_via_proxy(spec.runtime_profile, key):
            lines.append(
                "注意：{} 可由 __getattr__ 代理到 {}，但「代理可达 ≠ 已批准」（设计 §4.3 / §20.3）。".format(
                    key, spec.proxy_target_class or "代理目标"
                )
            )
    near = registry.nearest_names(spec.runtime_profile, key, allow_login=allow_login)
    if near:
        lines.append("最接近的合法能力：{}（请自行按语义选择，不要机械替换）".format(", ".join(near)))
    lines.append("本 profile 允许的方法：{}。".format(allowed))
    lines.append("需要新能力时先在 registry 登记契约，不要在脚本里猜方法名。")
    return "\n".join(lines)


def lookup_text(profile: str | None, name: str, *, allow_login: bool = False) -> str:
    """旧 ``lookup_api`` 兼容形状。

    profile 缺失 / 未确定（``None`` / ``unresolved``）时**只按
    ``portable_common`` 安全交集回答**；命中单端能力或签名分歧的方法时返回
    结构化 ``PROFILE_REQUIRED``，**绝不**静默选 desktop/browser（决定 §13.1）。
    """
    effective = registry.effective_profile(profile)
    spec = registry.get_profile(effective)
    key = str(name or "").strip()
    if not key:
        return "lookup_api requires name (e.g. wait_image)."
    if "." in key:
        key = key.rsplit(".", 1)[-1]
    key = key.strip("() ")
    item = spec.by_name(key)
    if not registry.is_approved(effective, key, allow_login=allow_login):
        if effective == registry.PORTABLE_PROFILE:
            return (
                "{}: {} —— 当前 profile 未确定，只允许 portable_common 安全交集里的方法。\n"
                "{}".format(
                    registry.PROFILE_REQUIRED_CODE,
                    key,
                    registry.portable_reason(effective, key),
                )
            )
        allowed = ", ".join(sorted(spec.approved_names(allow_login=allow_login)))
        extra = ""
        if item is not None:
            extra = "\nKnown card (not allowed for this task):\n" + card_text(effective, item)
        return (
            "{!r} is NOT allowed in this script. Allowed methods: {}.".format(key, allowed) + extra
        )
    if item is None:
        return "No card for {!r}. Allowed: {}.".format(
            key, ", ".join(sorted(spec.approved_names(allow_login=allow_login)))
        )
    return "## {}\n{}".format(key, card_text(effective, item))


def compact_catalog(profile: str | None = None, *, allow_login: bool = False) -> str:
    spec = _spec(profile)
    names = spec.approved_names(allow_login=allow_login)
    return "\n\n".join(
        lookup_text(spec.runtime_profile, n, allow_login=allow_login) for n in names
    )


def login_line(allow_login: bool) -> str:
    if allow_login:
        return "Login APIs goto / dmm_login are available because this looks like a login/web task.\n"
    return "Do NOT call page APIs (goto, dmm_login, get_element).\n"


def contracts_block(
    profile: str | None = None,
    *,
    explanation: str = "",
    allow_login: bool | None = None,
) -> str:
    """生成/修订 prompt 共用的 API 契约全文（旧 ``api_contracts_block`` 形状）。

    **不含任何 profile 无关文案**：能力集合与返回语义都按解析出的 profile 渲染。
    """
    if allow_login is None:
        allow_login = allow_login_from_explanation(explanation or "")
    return (
        "\n## Allowed API contracts (MUST follow; do not invent methods)\n"
        + login_line(bool(allow_login))
        + compact_catalog(profile, allow_login=bool(allow_login))
        + "\n"
    )
