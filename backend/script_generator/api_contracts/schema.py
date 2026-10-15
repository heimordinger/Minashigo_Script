"""可执行 API 契约的结构化模型（设计文档 §5）。

本模块**只定义数据结构与纯函数**，不含任何具体 API 事实。事实来源在
``desktop_user_window.py`` / ``browser_user_browser.py``，并且必须能被
``verify_runtime.py`` 用真实 ``inspect.signature`` 复核（设计 §4.1、§8.4）。

字段取舍依据设计 §5「必须明确记录而不能只写自然语言的字段」：
``call_kind`` / 完整参数表 / 是否接受 ``**kwargs`` / 返回类型与真假语义 /
坐标系（``coordinate_space``）/ 结果有效期（``lifetime``）/ 外部副作用 /
runtime profile。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ── 参数 kind（与 inspect.Parameter.kind 同名，便于逐项比对）──
POSITIONAL_ONLY = "positional_only"
POSITIONAL_OR_KEYWORD = "positional_or_keyword"
KEYWORD_ONLY = "keyword_only"
VAR_POSITIONAL = "var_positional"
VAR_KEYWORD = "var_keyword"

# ── 调用形态 ──
SYNC_METHOD = "sync_method"
ASYNC_METHOD = "async_method"

#: 未登记字面量默认值的占位符（表示"确定有默认值，但不登记具体字面量"）。
UNKNOWN_DEFAULT = "..."


@dataclass(frozen=True)
class Parameter:
    """一个方法参数。

    ``default`` 是**源码字面量形式**的字符串（如 ``"0.9"``、``"'best'"``、
    ``"True"``、``"None"``、``"(0, 0)"``），便于：
    1. 渲染出与真实源码一致的签名；
    2. 用 ``ast.literal_eval`` 与真实默认值做漂移比对（§8.4）。
    """

    name: str
    kind: str = POSITIONAL_OR_KEYWORD
    type: str = ""
    required: bool = False
    default: str | None = None
    doc: str = ""

    @property
    def is_variadic(self) -> bool:
        return self.kind in (VAR_POSITIONAL, VAR_KEYWORD)

    @property
    def default_known(self) -> bool:
        return self.default not in (None, UNKNOWN_DEFAULT)

    def render(self) -> str:
        if self.kind == VAR_POSITIONAL:
            return "*" + self.name
        if self.kind == VAR_KEYWORD:
            return "**" + self.name
        text = self.name
        if self.type:
            text += ": " + self.type
        if not self.required:
            sep = " = " if self.type else "="
            text += sep + (self.default if self.default is not None else UNKNOWN_DEFAULT)
        return text


@dataclass(frozen=True)
class ReturnContract:
    """返回值契约。

    ``truthiness`` 写清真假语义；``identity_check_unsafe`` 表示
    「用 ``is None`` / ``is not None`` 判断成功与否是不可靠的」——
    这是实况两起静默故障的同一根因（``wait_image`` 返回 ``bool`` 却被
    ``is not None`` 判、``match_image`` 的 ``MatchResult.__bool__`` 可被绕过）。

    ── 机器可读事实（Phase 1 新增，设计 §10）──

    ``iterable`` / ``sized`` / ``mapping`` / ``nullable`` 是**三态**：
    ``True`` / ``False`` / ``None``。``None`` 表示**未登记（未知）**，
    调用方**不得**把它当成"确定不是"——未知就走"不确定不拒绝"，
    而已知为 ``False`` 才能判 ``TYPE_NOT_SIZED`` 之类的确定性错误。

    事实只登记在这里，不另建类型表（设计 §10「不建立新的类型事实源」）。
    核对基准是**真实类**，不是类型名文本：
    ``core/match/match_result.py`` 的 ``MatchResult`` 有 ``__iter__``/``__getitem__``
    但没有 ``__len__`` ⇒ ``iterable=True`` + ``sized=False``。
    """

    type: str = "None"
    truthiness: str = ""
    fields: tuple[str, ...] = ()
    identity_check_unsafe: bool = False
    never_proves: tuple[str, ...] = ()
    lifetime: str = ""
    #: 可被 ``for`` / 解包使用（真实类有 ``__iter__`` 或是序列）。
    iterable: bool | None = None
    #: 可被 ``len()`` 使用（真实类有 ``__len__``）。
    sized: bool | None = None
    #: 本身是 mapping（dict 类）：可用 ``**`` / ``.keys()`` / 按名字取值语义。
    mapping: bool | None = None
    #: 返回值本身可能为 ``None``（含 ``| None`` 的返回类型）。
    nullable: bool | None = None
    #: **哪些字段**可能为 ``None``。与 ``nullable`` 相互独立：``MatchResult``
    #: 对象本身永不为 ``None``（``nullable=False``），但 ``x`` / ``y`` /
    #: ``match_success`` 等字段可以是 ``None``。
    nullable_fields: tuple[str, ...] = ()
    #: 支持整数下标（``result[0]``）—— 真实类有 ``__getitem__`` 且接受 ``int``。
    indexable: bool | None = None
    #: 支持这些**字符串键**（``result["x"]``）。空元组表示"没有登记具体键"
    #: （例如 ``dict`` 的键是动态的，只由 ``mapping`` 表达）。
    subscript_keys: tuple[str, ...] = ()
    #: 返回值本身是不是**多个结果**的集合（迭代 = 遍历多个目标）。
    #: ``list[dict]`` ⇒ True；``MatchResult`` / ``dict`` / ``bool`` / ``None`` ⇒ False。
    #: 这是"迭代 MatchResult 不等于遍历多个匹配目标"的机器可读依据（设计 §10）。
    collection: bool | None = None

    @property
    def has_fields(self) -> bool:
        return bool(self.fields)

    @property
    def facts_declared(self) -> bool:
        """是否登记了机器可读事实（全 ``None`` 表示未知，不是"都不是"）。"""
        return any(
            value is not None
            for value in (
                self.iterable,
                self.sized,
                self.mapping,
                self.nullable,
                self.indexable,
                self.collection,
            )
        )

    def iteration_semantics(self) -> str:
        """迭代这个返回值到底意味着什么（设计 §10 的关键区分）。

        - ``"multi_target"``：迭代 = 遍历**多个结果**（多目标集合，如 ``list[dict]``）；
        - ``"single_result_parts"``：迭代只是**单个结果的部分**（``MatchResult`` 的
          ``__iter__`` 吐 x/y/max_val；``frame`` 吐行）⇒ **绝不能**当成"有多个匹配目标"，
          需要数量时必须换用返回 Sized 集合的多目标能力；
        - ``"not_iterable"``：不可迭代；
        - ``"unknown"``：没登记，不得据此判定。
        """
        if self.collection is None or self.iterable is None:
            return "unknown"
        if self.collection is True:
            return "multi_target" if self.iterable else "not_iterable"
        return "single_result_parts" if self.iterable else "not_iterable"

    def machine_facts(self) -> dict[str, Any]:
        """稳定、可哈希的机器可读事实（ticket / 渲染 / 语义 revision 共用一份）。"""
        return {
            "iterable": self.iterable,
            "sized": self.sized,
            "mapping": self.mapping,
            "nullable": self.nullable,
            "nullable_fields": list(self.nullable_fields),
            "indexable": self.indexable,
            "collection": self.collection,
            "subscript_keys": list(self.subscript_keys),
        }


@dataclass(frozen=True)
class ApiContract:
    """一个 API 的完整契约。``approved`` 为 False 表示已登记但当前不允许调用。"""

    id: str
    runtime_profile: str
    name: str
    #: 本契约可以落在哪些**真实接收者类型**上（设计 §8/§20.3）。
    #: 由 Registry 按 profile 声明的 ``target_class`` 盖章；portable 交集登记两侧。
    #: 签发器**只能**从这里取候选，不得由类名/字符串推断。
    receiver_types: tuple[str, ...] = ()
    owner: str = "browser"
    call_kind: str = ASYNC_METHOD
    summary: str = ""
    parameters: tuple[Parameter, ...] = ()
    accepts_kwargs: bool = False
    returns: ReturnContract = field(default_factory=ReturnContract)
    coordinate_space: str = ""
    semantics: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    examples_valid: tuple[str, ...] = ()
    examples_invalid: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    approved: bool = True
    #: 该方法是经 ``__getattr__`` 代理到别处（如 ``_target`` / ``_browser``），
    #: 因此无法用本类签名直接核验；必须显式标注，禁止悄悄放宽（设计 §4.3/§20.3）。
    proxied: bool = False
    #: 迁移期保留的旧 ``api_catalog`` 卡片原文（facade 渲染用）。
    guidance: str = ""

    @property
    def is_async(self) -> bool:
        return self.call_kind == ASYNC_METHOD

    @property
    def is_sync(self) -> bool:
        return self.call_kind == SYNC_METHOD

    def receiver_type(self) -> str | None:
        """唯一接收者；多候选（portable）时为 ``None`` —— 调用方必须自己给出提示，
        **不得**任选一个（设计 §8：不确定就报告，不静默挑）。"""
        return self.receiver_types[0] if len(self.receiver_types) == 1 else None

    def parameter(self, name: str) -> Parameter | None:
        for item in self.parameters:
            if item.name == name:
                return item
        return None

    def keyword_names(self) -> frozenset[str]:
        """能作为关键字传入的参数名（不含 ``*args`` / ``**kwargs``）。"""
        return frozenset(
            p.name for p in self.parameters if p.kind != VAR_POSITIONAL
        )

    def positional_parameters(self) -> tuple[Parameter, ...]:
        return tuple(
            p
            for p in self.parameters
            if p.kind in (POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD)
        )

    def required_keyword_names(self) -> frozenset[str]:
        return frozenset(
            p.name
            for p in self.parameters
            if p.required and p.kind in (POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD, KEYWORD_ONLY)
        )

    def max_positional(self) -> int:
        return len(self.positional_parameters())

    def signature(self) -> str:
        """与源码一致的签名文本，如 ``click_image(img_path, threshold = 0.9, ...)``。"""
        parts: list[str] = []
        star_written = False
        for p in self.parameters:
            if p.kind == KEYWORD_ONLY and not star_written:
                parts.append("*")
                star_written = True
            parts.append(p.render())
        if self.accepts_kwargs and not any(
            p.kind == VAR_KEYWORD for p in self.parameters
        ):
            parts.append("**kwargs")
        return "{}.{}({})".format(self.owner, self.name, ", ".join(parts))

    def qualified_signature(self) -> str:
        return "{} -> {}".format(self.signature(), self.returns.type)


@dataclass(frozen=True)
class ProfileContracts:
    """一个 runtime profile 的全部契约（设计 §4.2）。"""

    runtime_profile: str
    title: str
    target_class: str
    target_module: str
    contracts: tuple[ApiContract, ...]
    #: 需要 ``allow_login`` 才放行的方法名。
    login_methods: frozenset[str] = frozenset()
    #: ``__getattr__`` 代理目标（如 ``UserWindow`` → ``Win32Target``）。
    #: 声明它只为了在拒绝消息里说清「代理可达 ≠ 已批准」，**不**用于放宽契约。
    proxy_target_module: str = ""
    proxy_target_class: str = ""
    notes: tuple[str, ...] = ()

    def by_name(self, name: str) -> ApiContract | None:
        for item in self.contracts:
            if item.name == name:
                return item
        return None

    def approved_names(self, *, allow_login: bool = False) -> tuple[str, ...]:
        names = [c.name for c in self.contracts if c.approved and c.name not in self.login_methods]
        if allow_login:
            names.extend(
                c.name for c in self.contracts if c.approved and c.name in self.login_methods
            )
        return tuple(names)

    def approved_contracts(self, *, allow_login: bool = False) -> tuple[ApiContract, ...]:
        allowed = set(self.approved_names(allow_login=allow_login))
        return tuple(c for c in self.contracts if c.name in allowed)


# ── 构造辅助（让 profile 文件保持紧凑且不易写错）──


def req(name: str, type: str = "", *, kind: str = POSITIONAL_OR_KEYWORD, doc: str = "") -> Parameter:
    return Parameter(name=name, kind=kind, type=type, required=True, default=None, doc=doc)


def opt(
    name: str,
    default: str,
    type: str = "",
    *,
    kind: str = POSITIONAL_OR_KEYWORD,
    doc: str = "",
) -> Parameter:
    return Parameter(name=name, kind=kind, type=type, required=False, default=default, doc=doc)


def kwreq(name: str, type: str = "", *, doc: str = "") -> Parameter:
    return Parameter(name=name, kind=KEYWORD_ONLY, type=type, required=True, default=None, doc=doc)


def kwopt(name: str, default: str, type: str = "", *, doc: str = "") -> Parameter:
    return Parameter(name=name, kind=KEYWORD_ONLY, type=type, required=False, default=default, doc=doc)


def iter_contract_fields() -> Iterable[str]:
    """契约中参与「语义 revision」的字段名（排除渲染文本，见 §28 兼容策略）。"""
    return (
        "id",
        "runtime_profile",
        "name",
        "receiver_types",
        "owner",
        "call_kind",
        "parameters",
        "accepts_kwargs",
        "returns",
        "coordinate_space",
        "approved",
        "proxied",
    )


def contract_semantic_payload(contract: ApiContract) -> dict[str, Any]:
    """把契约压成稳定、可哈希的语义载荷（不含 prompt 文案）。

    返回模型的**机器可读事实**与 ``receiver_types`` 都在里面：它们变了，
    ``contracts_revision()`` 就变，旧 ticket / 旧 receipt 随之失效（设计 §11）。
    """
    return {
        "id": contract.id,
        "runtime_profile": contract.runtime_profile,
        "name": contract.name,
        "receiver_types": list(contract.receiver_types),
        "owner": contract.owner,
        "call_kind": contract.call_kind,
        "accepts_kwargs": contract.accepts_kwargs,
        "approved": contract.approved,
        "proxied": contract.proxied,
        "coordinate_space": contract.coordinate_space,
        "parameters": [
            {
                "name": p.name,
                "kind": p.kind,
                "type": p.type,
                "required": p.required,
                "default": p.default,
            }
            for p in contract.parameters
        ],
        "returns": {
            "type": contract.returns.type,
            "fields": list(contract.returns.fields),
            "identity_check_unsafe": contract.returns.identity_check_unsafe,
            **contract.returns.machine_facts(),
        },
    }
