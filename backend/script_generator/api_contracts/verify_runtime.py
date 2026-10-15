"""契约 ↔ 真实实现的漂移核验（设计 §6/§8.4）。

规则：
- 用 ``inspect.signature`` 比对真实类方法；
- 方法不存在 / 同步异步不符 / 参数增删改名 / kind 变化 / 默认值变化 → 报问题；
- ``proxied=True`` 的契约**不参与**签名核验，但必须确认它确实**不是**类上的直接属性
  （否则说明代理标注已经过时，应改成可核验契约）；
- 每个允许方法必须有语义卡（由 registry 完整性测试覆盖），每张卡必须对应真实方法。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from . import registry
from .schema import (
    ASYNC_METHOD,
    KEYWORD_ONLY,
    POSITIONAL_ONLY,
    POSITIONAL_OR_KEYWORD,
    SYNC_METHOD,
    VAR_KEYWORD,
    VAR_POSITIONAL,
    ApiContract,
    ProfileContracts,
)

_KIND_MAP = {
    inspect.Parameter.POSITIONAL_ONLY: POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD: POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY: KEYWORD_ONLY,
    inspect.Parameter.VAR_POSITIONAL: VAR_POSITIONAL,
    inspect.Parameter.VAR_KEYWORD: VAR_KEYWORD,
}


@lru_cache(maxsize=None)
def target_class(profile: str | None = None) -> type:
    """取 profile 绑定的真实执行类（延迟导入，避免测试期副作用）。

    合成 profile（``unresolved`` / ``portable_common``）不绑定真实类。
    """
    spec = registry.get_profile(registry.effective_profile(profile))
    if not spec.target_module:
        raise ImportError(
            "profile {!r} 不绑定真实执行类（合成 profile，无可核验签名）".format(
                spec.runtime_profile
            )
        )
    module = importlib.import_module(spec.target_module)
    cls = getattr(module, spec.target_class, None)
    if cls is None:
        raise ImportError(
            "profile {!r} 绑定的 {}.{} 不存在".format(
                spec.runtime_profile, spec.target_module, spec.target_class
            )
        )
    return cls


@lru_cache(maxsize=None)
def proxy_target_class(profile: str | None = None) -> type | None:
    """``__getattr__`` 代理目标类（未声明返回 None）。"""
    spec = registry.get_profile(registry.effective_profile(profile))
    if not (spec.proxy_target_module and spec.proxy_target_class):
        return None
    module = importlib.import_module(spec.proxy_target_module)
    return getattr(module, spec.proxy_target_class, None)


def is_reachable_via_proxy(profile: str | None, name: str) -> bool:
    """该方法是否只是「经 ``__getattr__`` 代理可达」（用来解释为什么仍然拒绝）。"""
    try:
        cls = proxy_target_class(profile)
    except ImportError:
        return False
    if cls is None:
        return False
    return callable(getattr(cls, str(name or ""), None))


def real_function(profile: str | None, name: str) -> Any | None:
    """取真实方法对象；不存在返回 None（类级查找**不会**触发实例 ``__getattr__`` 代理）。"""
    try:
        cls = target_class(profile)
    except ImportError:
        return None
    return getattr(cls, str(name or ""), None)


def real_signature(profile: str | None, name: str) -> inspect.Signature | None:
    fn = real_function(profile, name)
    if fn is None or not callable(fn):
        return None
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def real_call_kind(profile: str | None, name: str) -> str | None:
    fn = real_function(profile, name)
    if fn is None or not callable(fn):
        return None
    if inspect.iscoroutinefunction(fn):
        return ASYNC_METHOD
    return SYNC_METHOD


# ── 真实定义类 / 代理链解析（Phase 2，决定 3）──────────────────────────
#
# 目的：给 ticket 的 ``resolved_owner`` / ``resolution_chain`` 提供**真实事实**，
# 而不是把 Registry 里的占位 ``owner``（恒为 "browser"）抄过去。
#
# 解析顺序（每一步都可以证明，不需要猜测）：
#   ① 目标类的 **MRO**（子类覆盖优先 → Mixin/基类依次）里谁的 ``__dict__`` 有这个属性；
#   ② 没有 → 登记过的 ``__getattr__`` 代理目标类，同样走 MRO（``proxied``）；
#   ③ 还没有 → 类里有 ``__getattr__`` 但没有登记代理目标 ⇒ ``unregistered_proxy``
#      （**证明不了** owner，如实报告，不许填占位值）；
#   ④ 都没有 → ``missing``。

RESOLUTION_RESOLVED = "resolved"
RESOLUTION_PROXIED = "proxied"
RESOLUTION_UNREGISTERED_PROXY = "unregistered_proxy"
RESOLUTION_MISSING = "missing"
RESOLUTION_UNKNOWN = "unknown"

RESOLUTION_VIA_CLASS = "class"
RESOLUTION_VIA_MRO = "mro"
RESOLUTION_VIA_PROXY = "proxy"


@dataclass(frozen=True)
class MethodResolution:
    """一次"这个方法到底定义在哪里"的解析结果（只读事实）。"""

    status: str
    #: 真正定义该属性的类名；``unregistered_proxy`` / ``missing`` 时为空（**不填占位**）。
    owner: str = ""
    owner_module: str = ""
    #: 从接收者到属性的完整路径，如 ``("UserWindow", "SomeMixin", "match_image")``。
    chain: tuple[str, ...] = ()
    via: str = ""
    detail: str = ""

    @property
    def proven(self) -> bool:
        return self.status in (RESOLUTION_RESOLVED, RESOLUTION_PROXIED)


def resolve_attribute(cls: type, name: str) -> MethodResolution:
    """按一个**具体类**解析属性定义位置（子类覆盖优先，MRO 兜底）。

    ``chain`` 是**完整路径**：从接收者类一路经过 MRO 上的中间基类（Mixin 会在这里
    出现）直到真正定义该属性的类，最后是属性名本身。
    """
    key = str(name or "")
    if not key:
        return MethodResolution(RESOLUTION_UNKNOWN, detail="没有方法名")
    chain: list[str] = []
    for base in cls.__mro__:
        if base is object:
            break
        chain.append(base.__name__)
        if key in vars(base):
            chain.append(key)
            owner_module = str(getattr(base, "__module__", "") or "")
            via = (
                RESOLUTION_VIA_CLASS if base is cls else RESOLUTION_VIA_MRO
            )
            return MethodResolution(
                RESOLUTION_RESOLVED,
                owner=base.__name__,
                owner_module=owner_module,
                chain=tuple(chain),
                via=via,
                detail=(
                    ""
                    if base is cls
                    else "定义在 {}（继承/Mixin）".format(base.__name__)
                ),
            )
    chain.append(key)
    return MethodResolution(
        RESOLUTION_MISSING,
        chain=tuple(chain),
        detail="{} 的 MRO 上没有这个属性".format(cls.__name__),
    )


def resolve_method(profile: str | None, name: str) -> MethodResolution:
    """解析某个契约方法在**真实运行时**的定义位置（含登记代理链）。"""
    key = str(name or "")
    try:
        cls = target_class(profile)
    except ImportError as exc:
        return MethodResolution(RESOLUTION_UNKNOWN, detail=str(exc))
    found = resolve_attribute(cls, key)
    if found.status == RESOLUTION_RESOLVED:
        return found

    try:
        proxy = proxy_target_class(profile)
    except ImportError:
        proxy = None
    if proxy is not None:
        proxied = resolve_attribute(proxy, key)
        if proxied.status == RESOLUTION_RESOLVED:
            # 链里必须能看见"经 __getattr__ 转发"这一跳（决定：不要只写孤立目标）
            chain = (cls.__name__, "__getattr__") + proxied.chain
            return MethodResolution(
                RESOLUTION_PROXIED,
                owner=proxied.owner,
                owner_module=proxied.owner_module,
                chain=tuple(chain),
                via=RESOLUTION_VIA_PROXY,
                detail="经 {}.__getattr__ 代理到 {}".format(cls.__name__, proxy.__name__),
            )
        return MethodResolution(
            RESOLUTION_MISSING,
            chain=found.chain,
            detail="代理目标 {} 上也没有这个属性".format(proxy.__name__),
        )

    if any("__getattr__" in vars(base) for base in cls.__mro__):
        return MethodResolution(
            RESOLUTION_UNREGISTERED_PROXY,
            chain=(cls.__name__, "__getattr__", key),
            via=RESOLUTION_VIA_PROXY,
            detail=(
                "{} 有 __getattr__ 但没有登记代理目标："
                "这个方法无法从声明事实证明 owner（决定 3：不许填占位值）".format(cls.__name__)
            ),
        )
    return MethodResolution(RESOLUTION_MISSING, chain=found.chain, detail=found.detail)


def _params_of(fn: Any) -> list[inspect.Parameter]:
    """从**具体函数对象**取参数（去 ``self``/``cls``）；取不到返回空表。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params = params[1:]
    return params


def _real_parameters(profile: str | None, name: str) -> list[inspect.Parameter]:
    return _params_of(real_function(profile, name))


def _default_matches(contract_default: str | None, real_default: Any) -> tuple[bool, str]:
    if contract_default in (None, "..."):
        return True, ""
    try:
        expected = ast.literal_eval(contract_default)
    except Exception:
        return True, ""
    try:
        same = bool(expected == real_default)
    except Exception:
        same = repr(expected) == repr(real_default)
    if same:
        return True, ""
    return False, "默认值不一致：契约={!r} 真实={!r}".format(expected, real_default)


def verify_contract(profile: str | None, item: ApiContract) -> list[str]:
    """核验单个契约，返回问题列表（空 = 一致）。"""
    where = "{}.{}".format(profile or item.runtime_profile, item.name)
    problems: list[str] = []

    try:
        cls = target_class(profile or item.runtime_profile)
    except ImportError as exc:
        return ["{}: 无法导入真实类（{}）".format(where, exc)]

    fn = getattr(cls, item.name, None)
    verify_fn = fn

    if item.proxied:
        if fn is not None:
            problems.append(
                "{}: 标注为 proxied，但真实类上已能直接取到，请去掉 proxied 并改为可核验契约".format(where)
            )
            return problems
        # Phase 2.1：登记了代理目标的 proxied 契约**要**核验真实签名
        # （解析链：真实类 → __getattr__ → 代理目标类 → 其 MRO 上的定义类）。
        found = resolve_method(profile or item.runtime_profile, item.name)
        if found.status != RESOLUTION_PROXIED:
            # 没有登记代理目标（或代理目标上也没有）：保持旧行为——不在这里报错，
            # 由 `resolve_method` 的 unregistered_proxy 结果在报告里体现。
            return problems
        try:
            proxy_cls = proxy_target_class(profile or item.runtime_profile)
        except ImportError:
            proxy_cls = None
        verify_fn = getattr(proxy_cls, item.name, None) if proxy_cls is not None else None
        where = "{}（经代理 {}）".format(
            where, getattr(proxy_cls, "__name__", "?")
        )
        if verify_fn is None or not callable(verify_fn):
            return ["{}: 代理目标上不存在该方法".format(where)]

    if not item.approved:
        # 未批准条目只是"拒绝说明卡"：可能记录的是别的 profile 的能力。
        if verify_fn is None or not callable(verify_fn):
            return problems
        if not item.parameters and not item.accepts_kwargs:
            # 没登记参数表 → 不核验参数，避免用半份数据误报。
            return problems

    if verify_fn is None or not callable(verify_fn):
        return ["{}: 真实类 {} 上不存在该方法".format(where, cls.__name__)]

    real_kind = ASYNC_METHOD if inspect.iscoroutinefunction(verify_fn) else SYNC_METHOD
    if real_kind != item.call_kind:
        problems.append(
            "{}: call_kind 不一致（契约={} 真实={}）".format(where, item.call_kind, real_kind)
        )

    params = [p for p in _params_of(verify_fn) if p.kind != VAR_KEYWORD]
    real_has_var_positional = any(p.kind == VAR_POSITIONAL for p in params)
    params = [p for p in params if p.kind != VAR_POSITIONAL]
    real_has_kwargs = any(p.kind == VAR_KEYWORD for p in _params_of(verify_fn))

    if real_has_var_positional and not any(p.kind == VAR_POSITIONAL for p in item.parameters):
        problems.append("{}: 真实方法有 *args，契约未登记".format(where))
    if real_has_kwargs != bool(item.accepts_kwargs):
        problems.append(
            "{}: accepts_kwargs 不一致（契约={} 真实={}）".format(
                where, bool(item.accepts_kwargs), real_has_kwargs
            )
        )

    contract_params = [p for p in item.parameters if p.kind != VAR_POSITIONAL]
    real_names = [p.name for p in params]
    contract_names = [p.name for p in contract_params]
    if real_names != contract_names:
        missing = [n for n in real_names if n not in contract_names]
        extra = [n for n in contract_names if n not in real_names]
        problems.append(
            "{}: 参数表不一致（契约={} 真实={}；缺={} 多={}）".format(
                where, contract_names, real_names, missing, extra
            )
        )
        return problems

    for cp, rp in zip(contract_params, params):
        rkind = _KIND_MAP.get(rp.kind, str(rp.kind))
        if cp.kind != rkind:
            problems.append(
                "{}: 参数 {} 的 kind 不一致（契约={} 真实={}）".format(where, cp.name, cp.kind, rkind)
            )
        has_default = rp.default is not inspect.Parameter.empty
        if cp.required == has_default:
            problems.append(
                "{}: 参数 {} 的必填性不一致（契约 required={} 真实 has_default={}）".format(
                    where, cp.name, cp.required, has_default
                )
            )
        if has_default:
            ok, note = _default_matches(cp.default, rp.default)
            if not ok:
                problems.append("{}: 参数 {} {}".format(where, cp.name, note))
    return problems


def verify_profile(profile: str | None = None) -> list[str]:
    spec: ProfileContracts = registry.get_profile(registry.effective_profile(profile))
    if not spec.target_module:
        # 合成 profile（unresolved / portable_common）不对应真实类：其条目由
        # 各来源 profile 的漂移测试覆盖，这里没有可核验对象。
        return []
    problems: list[str] = []
    for item in spec.contracts:
        problems.extend(verify_contract(spec.runtime_profile, item))
    return problems


def verify_all() -> dict[str, list[str]]:
    return {name: verify_profile(name) for name in registry.profile_names()}


def unregistered_real_methods(profile: str | None = None) -> list[str]:
    """真实类上公开但**完全没登记**的方法（诊断用，不参与放行）。

    只用于报告"还有多少能力没写语义卡"；不构成契约放行的依据。
    """
    spec = registry.get_profile(registry.effective_profile(profile))
    cls = target_class(spec.runtime_profile)
    registered = {c.name for c in spec.contracts}
    out: list[str] = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        raw = inspect.getattr_static(cls, name, None)
        if raw is None or isinstance(raw, (property, staticmethod, classmethod)):
            continue
        if not callable(raw):
            continue
        if name not in registered:
            out.append(name)
    return sorted(out)
