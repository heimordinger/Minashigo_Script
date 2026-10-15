"""profile → 契约 registry（唯一事实源，设计 §4.1）。

对外只暴露查询函数；**不要**在这里再维护任何方法名单/签名副本。
``contracts_revision()`` 是语义 revision（不含 prompt 文案），供
``ValidationReceipt`` 绑定（设计 §14.4 / §28：文案变化不该让旧凭证失效，
签名/语义变化才失效）。

**profile 政策（2026-09-28 锁定决定 §13.1）**

- profile 是 **Runtime 事实**，不是常量：优先级为
  ① 当前试运行目标真实执行类型 → ② 当前任务绑定的执行目标 →
  ③ 「按某脚本修改」时基线脚本的 provisional profile → ④ 都没有则 ``unresolved``。
- **禁止静默默认**：``get_profile(None)`` 不再返回 desktop/browser，而是抛
  :class:`ProfileRequiredError`。历史上那句 ``name or DEFAULT_PROFILE`` 已删除。
- 无法判定具体 profile 时，唯一允许的兜底是 :data:`PORTABLE_PROFILE`
  （两端**经核实的安全交集**，不是并集）；映射统一走 :func:`effective_profile`。
- 未批准（``approved=False``）的条目只为迁移/诊断保留，默认查询**看不到**，
  需要显式 ``include_unapproved=True``；该开关不暴露给 LLM 查询、不进入 prompt、
  不作为脚本白名单。
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import replace

from .browser_user_browser import BROWSER_USER_BROWSER
from .desktop_user_window import DESKTOP_USER_WINDOW
from .schema import (
    ApiContract,
    Parameter,
    ProfileContracts,
    ReturnContract,
    contract_semantic_payload,
)

#: profile 未确定时的**显式状态**。它不是一个可执行 profile：
#: 没有任何已批准契约，禁止用于白名单或渲染。
UNRESOLVED_PROFILE = "unresolved"

#: 两端**经核实的安全交集**（决定 §13.1）。不是并集：
#: 只登记两端都存在、且 ``call_kind``/参数名/kind/必填性/返回类型兼容的方法，
#: 单端参数（``cheap``/``force_wide``/``threshold`` 等）一律从交集剔除。
PORTABLE_PROFILE = "portable_common"

#: 结构化「需要先确定 profile」标记：facade / prompt / 校验器共用同一个字面量。
PROFILE_REQUIRED_CODE = "PROFILE_REQUIRED"

#: **已废弃**，仅留作诊断与迁移参照。历史上它是 profile 缺失时的静默回退目标，
#: 现在任何地方都不得把它当作缺省值。
LEGACY_DEFAULT_PROFILE = "desktop_user_window"


class ProfileRequiredError(KeyError):
    """profile 缺失 / 未确定 / 未知。

    调用方必须显式给出具体 profile；只有在确实无法判定时才可显式使用
    :data:`PORTABLE_PROFILE`。**禁止**静默落成 desktop/browser（决定 §13.1）。
    """


def _agree(x: object, y: object) -> object:
    """两端一致才保留，否则 ``None``（未知）——交集里**不猜**单端行为。"""
    return x if x == y else None


def _merge_return(x: ReturnContract, y: ReturnContract) -> ReturnContract:
    """``portable_common`` 的返回模型：类型已由 :func:`_merge_portable` 确认相同。

    机器可读事实只在**两端一致**时保留（不一致落回 ``None`` = 未知，调用方不得据此判定），
    ``nullable_fields`` 取交集（两端都可空才算共同可空）。这样 portable 契约同样有
    完整、可判定的事实，而不是一片空白。
    """
    if x == y:
        return x
    return ReturnContract(
        type=x.type,
        truthiness="",
        fields=tuple(name for name in x.fields if name in y.fields),
        identity_check_unsafe=bool(x.identity_check_unsafe or y.identity_check_unsafe),
        never_proves=tuple(sorted(set(x.never_proves) & set(y.never_proves))),
        lifetime="",
        iterable=_agree(x.iterable, y.iterable),  # type: ignore[arg-type]
        sized=_agree(x.sized, y.sized),  # type: ignore[arg-type]
        mapping=_agree(x.mapping, y.mapping),  # type: ignore[arg-type]
        nullable=_agree(x.nullable, y.nullable),  # type: ignore[arg-type]
        nullable_fields=tuple(
            name for name in x.nullable_fields if name in y.nullable_fields
        ),
        indexable=_agree(x.indexable, y.indexable),  # type: ignore[arg-type]
        collection=_agree(x.collection, y.collection),  # type: ignore[arg-type]
        # 字符串键取交集：两端都认的键才是共同事实
        subscript_keys=tuple(
            key for key in x.subscript_keys if key in y.subscript_keys
        ),
    )


def _merge_portable(x: ApiContract, y: ApiContract) -> ApiContract | None:
    """两个 profile 的同一方法 → 安全交集契约；不兼容返回 ``None``。

    兼容判据（必须同时满足）：

    1. ``call_kind`` 相同（同步/异步不能含糊）；
    2. ``returns.type`` 相同；
    3. ``accepts_kwargs`` 相同；
    4. 共有参数的名字 ``kind`` 与必填性相同；
    5. 只在单侧出现的参数必须是**可选**的（否则任何调用都无法同时满足两端）。
    """
    if x.call_kind != y.call_kind or x.accepts_kwargs != y.accepts_kwargs:
        return None
    if (x.returns.type or "") != (y.returns.type or ""):
        return None

    xp = {p.name: p for p in x.parameters}
    yp = {p.name: p for p in y.parameters}
    params: list[Parameter] = []
    for name, px in xp.items():
        py = yp.get(name)
        if py is None:
            if px.required:
                return None
            continue  # 单端可选参数：不进入交集
        if px.kind != py.kind or px.required != py.required:
            return None
        params.append(px)
    for name, py in yp.items():
        if name in xp:
            continue
        if py.required:
            return None

    same_summary = x.summary == y.summary
    #: 交集里的 semantics 可能为空（两侧文案不同）。空卡违反设计 §8.4「每个允许方法
    #: 必须有语义卡」，所以给一条说明它为什么参数变少，而不是把语义留空。
    shared_semantics = tuple(sorted(set(x.semantics) & set(y.semantics)))
    if not shared_semantics:
        shared_semantics = (
            "两端共有的安全交集：只登记 desktop/browser 签名都兼容的参数；"
            "单端参数（如 cheap/force_wide/threshold）不在此列，需要时先确定 profile。",
        )
    return ApiContract(
        id="{}.{}".format(PORTABLE_PROFILE, x.name),
        runtime_profile=PORTABLE_PROFILE,
        name=x.name,
        owner=x.owner if x.owner == y.owner else "portable",
        call_kind=x.call_kind,
        summary=x.summary
        if same_summary
        else "{}（desktop/browser 共同能力；只登记两端签名都兼容的参数）".format(x.name),
        parameters=tuple(params),
        accepts_kwargs=x.accepts_kwargs,
        returns=_merge_return(x.returns, y.returns),
        coordinate_space=x.coordinate_space if x.coordinate_space == y.coordinate_space else "",
        semantics=shared_semantics,
        side_effects=tuple(sorted(set(x.side_effects) & set(y.side_effects))),
        preconditions=tuple(sorted(set(x.preconditions) & set(y.preconditions))),
        errors=tuple(sorted(set(x.errors) & set(y.errors))),
        examples_valid=tuple(e for e in x.examples_valid if e in y.examples_valid),
        examples_invalid=tuple(e for e in x.examples_invalid if e in y.examples_invalid),
        tags=tuple(sorted(set(x.tags) | set(y.tags))),
        approved=True,
        proxied=bool(x.proxied and y.proxied),
        guidance=x.guidance if x.guidance == y.guidance else "",
    )


def _stamp_receiver_types(
    profile: ProfileContracts, receiver_types: tuple[str, ...]
) -> ProfileContracts:
    """把**接收者类型事实**盖到 profile 的每条契约上（设计 §8）。

    来源是 profile 自己声明的 ``target_class``（``verify_runtime`` 用它做
    ``inspect.signature`` 核验，因此它本来就是被核验过的事实），不是从契约 id
    或类名字符串推断出来的。``portable_common`` 由两端交集构造，所以它盖**两个**
    候选：具体是哪一端要由调用方按真实执行目标决定，Registry 不替它挑。
    """
    if not receiver_types:
        return profile
    stamped = tuple(
        replace(
            item,
            receiver_types=item.receiver_types or tuple(receiver_types),
        )
        for item in profile.contracts
    )
    return replace(profile, contracts=stamped)


def _build_portable(
    desktop: ProfileContracts, browser: ProfileContracts
) -> ProfileContracts:
    """按安全交集构造 ``portable_common``（决定 §13.1：不是并集）。"""
    out: list[ApiContract] = []
    for name in desktop.approved_names():
        if name in desktop.login_methods:
            continue  # 登录类单端能力，不进交集
        dx = desktop.by_name(name)
        bx = browser.by_name(name)
        if dx is None or bx is None:
            continue
        if not bx.approved or name in browser.login_methods:
            continue
        merged = _merge_portable(dx, bx)
        if merged is not None:
            out.append(merged)
    return ProfileContracts(
        runtime_profile=PORTABLE_PROFILE,
        title="两端共有能力（安全交集）",
        target_class="",
        target_module="",
        contracts=tuple(out),
        notes=(
            "只在执行目标未确定时使用；它不是 desktop/browser 的并集。",
            "单端能力（match_image_multi / goto / dmm_login 等）不在此 profile。",
            "一旦脚本需要单端能力，必须先把 profile 确定下来，而不是退回这里。",
        ),
    )


_UNRESOLVED = ProfileContracts(
    runtime_profile=UNRESOLVED_PROFILE,
    title="执行目标未确定",
    target_class="",
    target_module="",
    contracts=(),
    notes=(
        "profile 未确定：没有任何已批准契约，禁止作为白名单或用于渲染。",
        "调用方应显式传入具体 profile；确实无法判定时用 portable_common。",
    ),
)

_PROFILES: dict[str, ProfileContracts] = {}


def _register(profile: ProfileContracts) -> None:
    _PROFILES[profile.runtime_profile] = profile


_register(
    _stamp_receiver_types(DESKTOP_USER_WINDOW, (DESKTOP_USER_WINDOW.target_class,))
)
_register(
    _stamp_receiver_types(BROWSER_USER_BROWSER, (BROWSER_USER_BROWSER.target_class,))
)
_register(
    _stamp_receiver_types(
        _build_portable(DESKTOP_USER_WINDOW, BROWSER_USER_BROWSER),
        (DESKTOP_USER_WINDOW.target_class, BROWSER_USER_BROWSER.target_class),
    )
)
_register(_UNRESOLVED)


def profile_names() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def has_profile(name: str) -> bool:
    return str(name or "") in _PROFILES


def executable_profile_names() -> tuple[str, ...]:
    """可用于校验/渲染的 profile（不含 ``unresolved``）。"""
    return tuple(n for n in profile_names() if n != UNRESOLVED_PROFILE)


def verifiable_profile_names() -> tuple[str, ...]:
    """绑定**真实执行类**、可做签名漂移核验的 profile。

    合成 profile（``portable_common`` 由两个真实 profile 求交集得到、``unresolved``
    没有契约）没有自己的类可核验：它们的正确性由来源 profile 的漂移测试保证。
    """
    return tuple(n for n in profile_names() if _PROFILES[n].target_module)


def get_profile(name: str | None) -> ProfileContracts:
    """按名字取 profile。

    ``name`` 为空 → :class:`ProfileRequiredError`（**绝不**静默返回 desktop/browser）。
    """
    key = str(name or "").strip()
    if not key:
        raise ProfileRequiredError(
            "未提供 runtime profile；禁止静默默认（决定 §13.1）。"
            "请显式传入具体 profile，或在确实无法判定时使用 {!r}。".format(PORTABLE_PROFILE)
        )
    if key not in _PROFILES:
        raise ProfileRequiredError(
            "未知 runtime profile {!r}；已登记：{}".format(key, ", ".join(profile_names()))
        )
    return _PROFILES[key]


def effective_profile(profile: str | None) -> str:
    """把「缺失 / 未确定」映射到**唯一允许**的兜底：``portable_common``。

    绝不映射到 desktop/browser。用于 prompt、facade 与校验器的边界。
    """
    key = str(profile or "").strip()
    if not key or key == UNRESOLVED_PROFILE:
        return PORTABLE_PROFILE
    return key


def contract(
    profile: str | None,
    name: str,
    *,
    include_unapproved: bool = False,
) -> ApiContract | None:
    """按名字取契约。

    默认**只返回已批准条目**；未批准条目需要显式 ``include_unapproved=True``
    （仅供本地迁移/诊断，不得进入 prompt 或白名单）。
    """
    item = get_profile(profile).by_name(str(name or ""))
    if item is None:
        return None
    if not item.approved and not include_unapproved:
        return None
    return item


def diagnostic_contracts(
    profile: str | None, *, allow_login: bool = False
) -> tuple[ApiContract, ...]:
    """**本地迁移/诊断专用**：含未批准条目。禁止用于 prompt 或脚本白名单。"""
    spec = get_profile(profile)
    names = {c.name for c in spec.contracts}
    if not allow_login:
        names -= set(spec.login_methods)
    return tuple(c for c in spec.contracts if c.name in names)


def approved_names(profile: str | None, *, allow_login: bool = False) -> tuple[str, ...]:
    return get_profile(profile).approved_names(allow_login=allow_login)


def approved_contracts(
    profile: str | None, *, allow_login: bool = False
) -> tuple[ApiContract, ...]:
    return get_profile(profile).approved_contracts(allow_login=allow_login)


def is_approved(profile: str | None, name: str, *, allow_login: bool = False) -> bool:
    item = contract(profile, name)
    if item is None:
        return False
    if item.name in get_profile(profile).login_methods and not allow_login:
        return False
    return True


def receiver_types(profile: str | None, name: str) -> tuple[str, ...]:
    """某个契约声明的**接收者类型候选**（签发器只能从这里取，禁止猜类名）。

    空元组表示"这个名字没有契约"或"该 profile 没有接收者事实"。
    """
    item = get_profile(profile).by_name(str(name or ""))
    return item.receiver_types if item is not None else ()


def nearest_names(
    profile: str | None,
    name: str,
    *,
    allow_login: bool = False,
    limit: int = 3,
) -> tuple[str, ...]:
    """给"写错名字"的场景找最接近的合法能力（只提示，不自动替换，设计 §10.4）。"""
    pool = list(approved_names(profile, allow_login=allow_login))
    if not pool:
        return ()
    matches = difflib.get_close_matches(str(name or ""), pool, n=max(1, int(limit)), cutoff=0.5)
    return tuple(matches)


def portable_reason(profile: str | None, name: str) -> str:
    """解释某个名字为什么不能用于 ``portable_common``（用于 PROFILE_REQUIRED 文案）。"""
    key = str(name or "")
    desktop = LEGACY_DEFAULT_PROFILE
    browser = BROWSER_USER_BROWSER.runtime_profile
    desktop_ok = is_approved(desktop, key, allow_login=True)
    browser_ok = is_approved(browser, key, allow_login=True)
    if desktop_ok and not browser_ok:
        return "{} 只有 desktop（UserWindow）提供；必须先确定 profile".format(key)
    if browser_ok and not desktop_ok:
        return "{} 只有 browser（UserBrowser）提供；必须先确定 profile".format(key)
    if desktop_ok and browser_ok:
        return "{} 两端签名/语义不兼容，不能进入安全交集；必须先确定 profile".format(key)
    return "{} 不在任何已批准契约里".format(key)


def _canonical_payload() -> dict:
    return {
        "profiles": [
            {
                "runtime_profile": p.runtime_profile,
                "target_class": p.target_class,
                "target_module": p.target_module,
                "login_methods": sorted(p.login_methods),
                "contracts": [
                    contract_semantic_payload(c)
                    for c in sorted(p.contracts, key=lambda c: c.name)
                ],
            }
            for p in sorted(_PROFILES.values(), key=lambda p: p.runtime_profile)
        ]
    }


def _digest(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def contracts_revision() -> str:
    """全部 profile 的语义 revision（稳定、可跨进程复现）。"""
    return _digest(_canonical_payload())


def profile_revision(profile: str | None = None) -> str:
    target = get_profile(profile).runtime_profile
    return _digest(
        {
            "profile": target,
            "contracts": [
                contract_semantic_payload(c)
                for c in sorted(get_profile(target).contracts, key=lambda c: c.name)
            ],
        }
    )


def revision_summary() -> dict[str, str]:
    out = {"contracts": contracts_revision()}
    for name in profile_names():
        out[name] = profile_revision(name)
    return out
