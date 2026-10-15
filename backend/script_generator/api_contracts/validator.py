"""契约校验器：AST 能力/await/参数/import policy/素材 token（设计 §7、§8.1、§8.2）。

设计要点：
- 校验事实**全部来自 registry**（方法是否允许、参数表、必填、同步异步、返回值语义），
  不在这里维护第二份名单；
- 返回**结构化 issue**（``ContractIssue``），同时能渲染成旧的 ``list[str]`` 形状，
  保证既有调用方与测试不被破坏（设计 §7 末段）；
- 不试图实现完整类型检查器：变量传播判断不可靠的场景留给 import smoke 与运行期
  （设计 §8.1 末段），但**所有能确定的违约一律阻断**。
- 旧 ``validate_script_local``（agent.py）里的 ~70 条业务规则**继续保留**，
  由 ``collab_script_issues`` 叠加调用；本模块不重复实现它们。
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

from . import python_policy, registry
from .schema import (
    ASYNC_METHOD,
    KEYWORD_ONLY,
    POSITIONAL_ONLY,
    POSITIONAL_OR_KEYWORD,
    SYNC_METHOD,
    VAR_KEYWORD,
    VAR_POSITIONAL,
    ApiContract,
    ReturnContract,
)

# ── issue codes（稳定 API；散文可变，见 errors.py 的同款约定）──
SYNTAX_ERROR = "SYNTAX_ERROR"
ENTRY_MISSING = "ENTRY_MISSING"
ENTRY_ANNOTATION = "ENTRY_ANNOTATION"
MODULE_SIDE_EFFECT = "MODULE_SIDE_EFFECT"
API_UNKNOWN = "API_UNKNOWN"
API_NOT_APPROVED = "API_NOT_APPROVED"
API_LOGIN_ONLY = "API_LOGIN_ONLY"
API_UNKNOWN_KEYWORD = "API_UNKNOWN_KEYWORD"
API_MISSING_ARGUMENT = "API_MISSING_ARGUMENT"
API_TOO_MANY_POSITIONAL = "API_TOO_MANY_POSITIONAL"
API_AWAIT_REQUIRED = "API_AWAIT_REQUIRED"
API_AWAIT_FORBIDDEN = "API_AWAIT_FORBIDDEN"
API_IDENTITY_CHECK = "API_IDENTITY_CHECK"
API_RETURN_FIELD = "API_RETURN_FIELD"
IMPORT_NOT_ALLOWED = "IMPORT_NOT_ALLOWED"
BUILTIN_NOT_ALLOWED = "BUILTIN_NOT_ALLOWED"
#: Python 能力策略：文件系统副作用 / os 全类 / pathlib 危险方法（决定：禁 os，pathlib 只留纯路径）。
POLICY_NOT_ALLOWED = "POLICY_NOT_ALLOWED"
#: 未定义的名字（设计 §8.1 要求；此前完全缺失）。
UNDEFINED_NAME = "UNDEFINED_NAME"
#: 接收者无法归属到执行器/别名，但方法名是单端契约能力 → 无法确认契约，拒绝并说明。
API_RECEIVER_UNVERIFIED = "API_RECEIVER_UNVERIFIED"
API_LITERAL_TYPE = "API_LITERAL_TYPE"
#: 契约凭据（ticket）的三种状态：既有码表达不了，才新增（Phase 1；设计 §9/§11）。
#: 复用原则：接收者未证实仍用 :data:`API_RECEIVER_UNVERIFIED`，参数错误仍用上面三个
#: ``API_*`` 参数码，权限作用域仍用 ``PERMISSION_SCOPE_MISMATCH`` —— 不建同义双码。
API_TICKET_MISSING = "API_TICKET_MISSING"
API_TICKET_EXPIRED = "API_TICKET_EXPIRED"
API_TICKET_MISMATCH = "API_TICKET_MISMATCH"
#: **返回值接口**冲突码：由 ``ReturnContract`` 的机器可读事实直接判定（设计 §10）。
#: Phase 2 只提供判定函数与事实，生产闸门在 Phase 3 接线。
TYPE_NOT_SIZED = "TYPE_NOT_SIZED"
TYPE_NOT_MULTI_TARGET = "TYPE_NOT_MULTI_TARGET"
ASSET_TOKEN_NOT_STRING = "ASSET_TOKEN_NOT_STRING"
ASSET_TOKEN_LEFT = "ASSET_TOKEN_LEFT"
ENTRY_NOT_CALLABLE = "ENTRY_NOT_CALLABLE"
UNBOUNDED_LOOP = "UNBOUNDED_LOOP"


# ── 返回值接口判定（纯函数，设计 §10）──────────────────────────────────
#
# Phase 2 只提供"事实 + 判定"，**不**在交付路径上拒绝（硬闸属 Phase 3）。
# 两个函数的共同原则：``None``（未登记）**不报** —— 未知不等于"确定不是"。


def len_conflict(returns: ReturnContract) -> str:
    """对返回值用 ``len()`` 是否冲突；``sized is False`` 才报 ``TYPE_NOT_SIZED``。"""
    return TYPE_NOT_SIZED if returns.sized is False else ""


def multi_target_conflict(returns: ReturnContract) -> str:
    """把迭代当成"遍历多个匹配目标"是否冲突（``MatchResult`` 就是这种）。

    只有"可迭代但**不是**多结果集合"才报；``Any``（未知）与真正的多目标集合都不报。
    """
    return (
        TYPE_NOT_MULTI_TARGET
        if returns.iteration_semantics() == "single_result_parts"
        else ""
    )

#: ``do_work`` 首参允许的标注名（与 controller/task_controller.py:49 一致）
VALID_TARGET_TYPES = frozenset({"UserBrowser", "UserWindow", "Browser"})

#: 执行类在脚本里的常见变量名（首参名之外的兜底）。
DEFAULT_TARGET_NAMES = ("browser", "win")

_TOKEN_RE = None


def _token_regex():
    """素材 token 正则（复用 collab_form 的唯一定义，延迟导入避免环）。"""
    global _TOKEN_RE
    if _TOKEN_RE is None:
        from ..collaborator.collab_form import _ASSET_REF_RE

        _TOKEN_RE = _ASSET_REF_RE
    return _TOKEN_RE


@dataclass(frozen=True)
class ContractIssue:
    """一条结构化校验 issue。``render()`` 给出旧的字符串形状。"""

    code: str
    message: str
    line: int = 0
    symbol: str = ""
    contract_id: str = ""
    allowed_parameters: tuple[str, ...] = ()
    repair_hint: str = ""
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "line": self.line,
            "symbol": self.symbol,
            "message": self.message,
            "contract_id": self.contract_id,
            "allowed_parameters": list(self.allowed_parameters),
            "repair_hint": self.repair_hint,
            "severity": self.severity,
        }

    def render(self) -> str:
        text = self.message
        if self.repair_hint:
            text = "{}（改法：{}）".format(text, self.repair_hint)
        return text


def legacy_strings(issues: Iterable[ContractIssue]) -> list[str]:
    """旧调用方要的 ``list[str]``。"""
    return [i.render() for i in issues if i.severity != "info"]


# ── AST 辅助 ──


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _single_assignment_values(tree: ast.AST) -> dict[str, ast.AST]:
    """只出现一次赋值的变量名 → 赋值右值（多次赋值时无法可靠传播，直接放弃）。"""
    counts: Counter[str] = Counter()
    values: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    counts[target.id] += 1
                    values.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            counts[node.target.id] += 1
            values.setdefault(node.target.id, []).append(node.value)
    return {name: items[0] for name, items in values.items() if counts[name] == 1}


def _awaited_call(node: ast.AST) -> ast.Call | None:
    """``x`` 若是 ``await f(...)``，返回内层 Call。"""
    if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
        return node.value
    if isinstance(node, ast.Call):
        return node
    return None


def _annotation_names(annotation: ast.AST | None) -> set[str] | None:
    """解析首参标注为名字集合（支持 ``UserBrowser | UserWindow`` / ``"..."``）。"""
    if annotation is None:
        return None
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        text = annotation.value.replace(" ", "")
        return {p for p in text.split("|") if p} or None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = _annotation_names(annotation.left) or set()
        right = _annotation_names(annotation.right) or set()
        return left | right or None
    if isinstance(annotation, ast.Name):
        return {annotation.id}
    if isinstance(annotation, ast.Attribute):
        return {annotation.attr}
    if isinstance(annotation, ast.Subscript):  # Optional[...] / Union[...]
        inner = annotation.slice
        if isinstance(inner, ast.Tuple):
            out: set[str] = set()
            for elt in inner.elts:
                out |= _annotation_names(elt) or set()
            return out or None
        return _annotation_names(inner)
    return None


def _find_entry(tree: ast.AST, entry_name: str) -> ast.AST | None:
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entry_name:
            return node
    return None


# ── 素材 token（设计 §8.2）──


def bare_asset_tokens(tree: ast.AST) -> list[tuple[str, int]]:
    """找出裸 token 写法：``[[asset_x]]`` 会被解析成两层单元素 List。

    只接受字符串 token（``"[[asset_x]]"``）——裸形式虽然当前替换器也能处理，
    但它是"恰好合法的 AST 列表表达式"，必须专门报错（设计 §8.2）。
    """
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) != 1:
            continue
        inner = node.elts[0]
        if not isinstance(inner, ast.List) or len(inner.elts) != 1:
            continue
        ref = inner.elts[0]
        if isinstance(ref, ast.Name):
            out.append((ref.id, getattr(node, "lineno", 0)))
        elif isinstance(ref, ast.Constant) and isinstance(ref.value, str):
            out.append((ref.value, getattr(node, "lineno", 0)))
    return out


def left_asset_tokens(code: str) -> list[str]:
    """替换/打包之后仍残留的 token（设计 §8.2：最终产物不得再有未解析 token）。"""
    found: list[str] = []
    for match in _token_regex().finditer(code or ""):
        ref = (match.group(2) or "").strip()
        if ref and ref not in found:
            found.append(ref)
    return found


def asset_token_source_issues(code: str) -> list[ContractIssue]:
    """**源码专有**的裸 token 检查（归一之后就看不到裸写法了，所以要单独跑一遍）。

    只接受字符串 token（``"[[asset_x]]"``）；裸 ``[[asset_x]]`` 虽然当前替换器也能处理，
    但它是"恰好合法的 AST 列表表达式"，必须专门报错（设计 §8.2）。
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    out: list[ContractIssue] = []
    for ref, lineno in bare_asset_tokens(tree):
        out.append(
            ContractIssue(
                code=ASSET_TOKEN_NOT_STRING,
                message="素材引用必须是字符串: [[{}]]（第 {} 行）；裸写法会被当成列表表达式".format(
                    ref, lineno
                ),
                line=lineno,
                symbol=ref,
                repair_hint='写成 "[[{}]]"（带引号），或直接用 IMG_DIR / 文件名'.format(ref),
            )
        )
    return out


# ── 主校验 ──


def validate_source(
    code: str,
    *,
    profile: str | None = None,
    allow_login: bool = False,
    entry_name: str = "do_work",
    require_entry: bool = True,
    check_imports: bool = True,
    check_module_side_effects: bool = True,
    check_undefined_names: bool = True,
) -> list[ContractIssue]:
    """校验模型源码（素材替换**之前**的稿子）。

    ``profile`` 缺失/未确定（``None`` 或 ``unresolved``）→ 落到
    ``portable_common`` 安全交集（决定 §13.1）。**绝不**静默落 desktop/browser。
    """
    spec = registry.get_profile(registry.effective_profile(profile))
    profile_name = spec.runtime_profile
    text = code or ""
    issues: list[ContractIssue] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [
            ContractIssue(
                code=SYNTAX_ERROR,
                message="语法错误: {}（第 {} 行）".format(exc.msg, exc.lineno or 0),
                line=int(exc.lineno or 0),
                repair_hint="先修语法，再谈契约",
            )
        ]

    # 1) 入口
    entry = _find_entry(tree, entry_name)
    if entry is None:
        if require_entry:
            issues.append(
                ContractIssue(
                    code=ENTRY_MISSING,
                    message="缺少 async def {}(browser)".format(entry_name),
                    repair_hint="必须提供 `async def {}(browser: UserWindow)` 入口".format(entry_name),
                )
            )
    else:
        if not isinstance(entry, ast.AsyncFunctionDef):
            issues.append(
                ContractIssue(
                    code=ENTRY_MISSING,
                    message="{} 必须是 async 函数".format(entry_name),
                    line=getattr(entry, "lineno", 0),
                    symbol=entry_name,
                    repair_hint="改成 `async def {}(browser: UserWindow)`".format(entry_name),
                )
            )
        args = list(getattr(entry.args, "posonlyargs", [])) + list(entry.args.args)
        if not args:
            issues.append(
                ContractIssue(
                    code=ENTRY_ANNOTATION,
                    message="{} 缺少参数，需要 (browser: UserBrowser | UserWindow)".format(entry_name),
                    line=getattr(entry, "lineno", 0),
                    symbol=entry_name,
                )
            )
        else:
            names = _annotation_names(args[0].annotation)
            if names is None:
                issues.append(
                    ContractIssue(
                        code=ENTRY_ANNOTATION,
                        message="{} 的第一个参数缺少类型标注".format(entry_name),
                        line=getattr(entry, "lineno", 0),
                        symbol=entry_name,
                        repair_hint="标注成 UserWindow（桌面）或 UserBrowser|UserWindow",
                    )
                )
            elif not names.issubset(VALID_TARGET_TYPES):
                issues.append(
                    ContractIssue(
                        code=ENTRY_ANNOTATION,
                        message="{} 的参数类型标注必须是 UserBrowser 或 UserWindow，不支持: {}".format(
                            entry_name, ", ".join(sorted(names - VALID_TARGET_TYPES)) or ", ".join(sorted(names))
                        ),
                        line=getattr(entry, "lineno", 0),
                        symbol=entry_name,
                    )
                )

    # 2) 顶层副作用（import smoke 不能执行副作用，设计 §8.3）
    if check_module_side_effects:
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                issues.append(
                    ContractIssue(
                        code=MODULE_SIDE_EFFECT,
                        message="模块顶层出现函数调用（第 {} 行）；导入即执行，会污染 import smoke".format(
                            getattr(node, "lineno", 0)
                        ),
                        line=getattr(node, "lineno", 0),
                        repair_hint="把顶层调用移进 do_work 或改成常量赋值",
                    )
                )

    # 3) import / Python policy
    if check_imports:
        for module, lineno in python_policy.iter_imported_modules(tree):
            allowed, reason = python_policy.module_policy(module)
            if not allowed:
                issues.append(
                    ContractIssue(
                        code=IMPORT_NOT_ALLOWED,
                        message="不允许 import {}（第 {} 行）；{}".format(module, lineno, reason),
                        line=lineno,
                        symbol=module,
                        repair_hint="只用 python_policy 白名单里的模块与项目模块",
                    )
                )

    parents = _parent_map(tree)
    assigned = _single_assignment_values(tree)
    alias_map = python_policy.import_alias_map(tree)
    target_names = set(DEFAULT_TARGET_NAMES)
    if entry is not None:
        args = list(getattr(entry.args, "posonlyargs", [])) + list(entry.args.args)
        if args:
            target_names.add(args[0].arg)
    # 入口参数的简单别名（bb = browser / cc = bb）；重新赋值即失效。
    receiver_alias, invalidated = _receiver_aliases(tree, target_names)
    target_names -= invalidated

    # 4) 逐个 Call 检查
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # 4a) 被禁内建
        if isinstance(func, ast.Name):
            allowed, reason = python_policy.name_policy(func.id)
            if not allowed:
                issues.append(
                    ContractIssue(
                        code=BUILTIN_NOT_ALLOWED,
                        message="不允许调用 {}（第 {} 行）：{}".format(func.id, node.lineno, reason),
                        line=node.lineno,
                        symbol=func.id,
                    )
                )
            continue

        if not isinstance(func, ast.Attribute):
            continue

        # 4a-2) Python 能力策略（禁 os / pathlib 只留纯路径；含 import alias）
        issues.extend(_policy_issues_for_call(node, func, alias_map=alias_map))

        # 4b) browser.<method> —— 接收者可以是入口参数本身，也可以是它的简单别名
        receiver = _resolve_receiver(func.value, receiver_alias, target_names)
        if not receiver:
            # 无法归属：不把"分析不了"当成"已验证"。
            issues.extend(
                _receiver_unverified_issues(
                    node, func, alias=receiver_alias, targets=target_names
                )
            )
            continue
        issues.extend(
            _check_browser_call(
                node,
                func,
                receiver=receiver,
                parents=parents,
                profile_name=profile_name,
                allow_login=allow_login,
            )
        )

    # 5) 素材 token：裸写法专门报错
    issues.extend(asset_token_source_issues(text))

    # 5b) 无界循环：Fake Runtime smoke 会挂死，静态先拦（只拦常量条件且没有 break 的）
    issues.extend(unbounded_loop_issues(tree))

    # 6) 返回值语义：身份判断 / 字段访问
    issues.extend(_check_return_semantics(tree, parents=parents, assigned=assigned, profile_name=profile_name))

    # 7) 未定义名字（设计 §8.1；覆盖注解里的类型名）
    if check_undefined_names:
        issues.extend(undefined_name_issues(tree))

    return _dedupe(issues)


def _dotted_name(node: ast.AST) -> str:
    """把 ``a.b.c`` 形式的表达式还原成点分名（其它形态返回空串）。"""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _resolved_dotted(node: ast.AST, alias_map: dict[str, str]) -> str:
    """解析 import 别名后的点分名；无法确定返回空串。

    ``system_os.system`` → ``os.system``（``import os as system_os``）；
    ``P`` → ``pathlib.Path``（``from pathlib import Path as P``）。
    """
    dotted = _dotted_name(node)
    if not dotted:
        return ""
    head, _, tail = dotted.partition(".")
    resolved = alias_map.get(head, head)
    return resolved + ("." + tail if tail else "")


def _policy_issues_for_call(
    node: ast.Call,
    func: ast.Attribute,
    *,
    alias_map: dict[str, str],
) -> list[ContractIssue]:
    """Python 能力策略：禁 ``os`` 全类；``pathlib`` 只留纯路径能力。

    两步判定（用户决定）：

    1. **方法名本身**即高风险（``write_text``/``unlink``/``glob`` …）→ 直接拒绝，
       因为"无法确定接收者时宁可拒绝高风险调用，不静默放行"；
    2. 接收者能解析出全名时 → ``os.*`` 一律拒绝；解析为 ``pathlib``/``Path`` 的
       歧义方法名（``replace``/``open``/``rename`` …）也拒绝。

    有歧义的名字不做纯名字拒绝，否则 ``"a-b".replace(...)`` 会被误判成文件操作。
    """
    name = func.attr
    allowed, reason = python_policy.method_name_policy(name)
    if not allowed:
        return [
            ContractIssue(
                code=POLICY_NOT_ALLOWED,
                message="不允许调用 .{}()（第 {} 行）：{}".format(name, node.lineno, reason),
                line=node.lineno,
                symbol=name,
                repair_hint="素材请用 [[asset_id]] token，由 materializer 解析；不要自行读写/扫描文件",
            )
        ]
    owner = _resolved_dotted(func.value, alias_map)
    if not owner:
        return []
    allowed, reason = python_policy.attr_policy(owner + "." + name)
    if allowed:
        return []
    return [
        ContractIssue(
            code=POLICY_NOT_ALLOWED,
            message="不允许调用 {}.{}()（第 {} 行）：{}".format(owner, name, node.lineno, reason),
            line=node.lineno,
            symbol="{}.{}".format(owner, name),
            repair_hint="纯路径处理请使用 pathlib 的构造与属性；文件读写交给 materializer",
        )
    ]


def _receiver_aliases(
    tree: ast.AST, targets: set[str]
) -> tuple[dict[str, str], set[str]]:
    """入口参数的**简单别名**跟踪（决定 §5：别名绕过是阻断项）。

    返回 ``(别名表, 失效的目标名)``：

    - 只跟踪 ``x = y``（``y`` 是名字）与 ``a = b = y`` 这类简单赋值链；
    - ``y`` 不是已知接收者/别名 → ``x`` 的别名**失效**（重新赋值即失效）；
    - ``bb = browser`` → ``{"bb": "browser"}``；``cc = bb`` → ``{"cc": "browser"}``（链式）；
    - tuple unpack（``a, b = ...``）与属性赋值**不解析**（不猜），
      由 :func:`_receiver_unverified_issues` 兜底提示"无法确认"。
    """
    alias: dict[str, str] = {}
    invalidated: set[str] = set()
    assigns: list[ast.AST] = [
        n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
    ]
    assigns.sort(key=lambda n: getattr(n, "lineno", 0))
    for node in assigns:
        value = getattr(node, "value", None)
        if isinstance(node, ast.Assign):
            tgt_nodes: list[ast.AST] = list(node.targets)
        else:
            tgt_nodes = [node.target] if node.target is not None else []
        root = ""
        if isinstance(value, ast.Name):
            if value.id in alias:
                root = alias[value.id]
            elif value.id in targets and value.id not in invalidated:
                root = value.id
        for tgt in tgt_nodes:
            if not isinstance(tgt, ast.Name):
                continue  # tuple unpack / 属性赋值：不猜
            name = tgt.id
            if not root:
                alias.pop(name, None)
                if name in targets:
                    invalidated.add(name)  # 执行器参数被重新赋值 → 不再是执行器
                continue
            if name in targets:
                continue
            alias[name] = root
    return alias, invalidated


def _resolve_receiver(node: ast.AST, alias: dict[str, str], targets: set[str]) -> str:
    """把调用接收者解析回**执行器的根名字**；无法确定返回空串。"""
    if isinstance(node, ast.Name):
        if node.id in alias:
            return alias[node.id]
        if node.id in targets:
            return node.id
    return ""


@lru_cache(maxsize=1)
def _all_contract_method_names() -> frozenset[str]:
    """所有 profile 里登记过的契约方法名（含未批准），用于识别"形状像执行器调用"。"""
    names: set[str] = set()
    for pname in registry.profile_names():
        try:
            spec = registry.get_profile(pname)
        except Exception:  # noqa: BLE001
            continue
        names.update(c.name for c in spec.contracts)
    return frozenset(names)


@lru_cache(maxsize=1)
def _single_sided_method_names() -> frozenset[str]:
    """**单端**能力名（只在 desktop 或只在 browser 批准）。

    这些名字在普通对象上几乎不出现，因此在"接收者无法解析"时按**阻断**处理；
    共有能力只给 warning，避免误伤恰好同名的普通方法。
    """
    desktop = set(registry.approved_names(registry.LEGACY_DEFAULT_PROFILE, allow_login=True))
    browser = set(
        registry.approved_names(
            registry.BROWSER_USER_BROWSER.runtime_profile, allow_login=True
        )
    )
    return frozenset(desktop ^ browser)


def _receiver_unverified_issues(
    node: ast.Call, func: ast.Attribute, *, alias: dict[str, str], targets: set[str]
) -> list[ContractIssue]:
    """接收者无法归属到执行器/别名时的兜底：**不把"无法分析"说成"已验证"**。"""
    if not isinstance(func.value, ast.Name):
        return []
    name = func.attr
    known = _all_contract_method_names()
    if name not in known:
        return []
    if func.value.id in alias or func.value.id in targets:
        return []
    single_sided = name in _single_sided_method_names()
    return [
        ContractIssue(
            code=API_RECEIVER_UNVERIFIED,
            message=(
                "{}.{}() 的接收者 {!r} 无法归属到执行器或其别名（第 {} 行）".format(
                    func.value.id, name, func.value.id, node.lineno
                )
            ),
            line=node.lineno,
            symbol="{}.{}".format(func.value.id, name),
            severity="error" if single_sided else "warning",
            repair_hint=(
                "只用入口参数直接调用（如 browser.{0}()），或显式简单赋值 bb = browser；"
                "别名不能被重新赋值，tuple unpack 暂不支持".format(name)
            ),
        )
    ]


def undefined_name_issues(tree: ast.AST) -> list[ContractIssue]:
    """收集**未定义**的名字（设计 §8.1 要求，此前完全缺失）。

    保守策略：模块内任何位置出现过的绑定（import/赋值/参数/def/class/except/
    comprehension 目标）都算已定义 —— 宁可少报，也不把能跑的脚本判死。
    出现 ``from x import *`` 时直接放弃（无法可靠分析，不猜）。
    注解里的名字也算 Load：``def do_work(browser: UserWindow)`` 缺 import 时
    在函数定义那一刻就会 ``NameError``。
    """
    import builtins

    defined: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return []
                defined.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)

    out: list[ContractIssue] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
            continue
        # 与旧检查（``agent._find_undefined_name_loads``）对齐：跳过 dunder。
        # ``__name__`` / ``__file__`` / ``__doc__`` 是模块全局，不在 ``dir(builtins)`` 里，
        # 不跳过就会把 `if __name__ == "__main__":` 误报成未定义。
        if node.id.startswith("__"):
            continue
        if node.id in defined or node.id in seen:
            continue
        seen.add(node.id)
        out.append(
            ContractIssue(
                code=UNDEFINED_NAME,
                message="未定义的名字 {}（第 {} 行）；运行时会 NameError".format(
                    node.id, node.lineno
                ),
                line=node.lineno,
                symbol=node.id,
                repair_hint="补 import 或先定义；注解里的名字在函数定义时就会被求值",
            )
        )
    return out


def _check_browser_call(
    node: ast.Call,
    func: ast.Attribute,
    *,
    receiver: str,
    parents: dict[ast.AST, ast.AST],
    profile_name: str,
    allow_login: bool,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    name = func.attr
    spec = registry.get_profile(profile_name)
    item = spec.by_name(name)
    symbol = "{}.{}".format(receiver, name)

    if item is None:
        near = registry.nearest_names(profile_name, name, allow_login=allow_login)
        hint = "本 profile 没有这个方法"
        if near:
            hint += "；最接近的合法能力：{}（按语义自行选择，不要机械替换）".format(", ".join(near))
        hint += "；契约外的能力必须先登记"
        issues.append(
            ContractIssue(
                code=API_UNKNOWN,
                message="不存在的接口 {}（第 {} 行）".format(symbol, node.lineno),
                line=node.lineno,
                symbol=symbol,
                repair_hint=hint,
            )
        )
        return issues

    if item.name in spec.login_methods and not allow_login:
        issues.append(
            ContractIssue(
                code=API_LOGIN_ONLY,
                message="{} 只在登录/网页任务里允许（第 {} 行）".format(symbol, node.lineno),
                line=node.lineno,
                symbol=symbol,
                contract_id=item.id,
                repair_hint="本任务不是登录任务，不要调用页面 API",
            )
        )
        return issues

    if not item.approved:
        reasons = "；".join(item.semantics) or "未纳入协作脚本能力面"
        issues.append(
            ContractIssue(
                code=API_NOT_APPROVED,
                message="{} 不属于本 profile 的协作脚本能力面（第 {} 行）".format(symbol, node.lineno),
                line=node.lineno,
                symbol=symbol,
                contract_id=item.id,
                repair_hint=reasons,
            )
        )
        return issues

    # 关键字参数
    known = set(item.keyword_names())
    for kw in node.keywords:
        if kw.arg is None:
            continue
        if kw.arg not in known and not item.accepts_kwargs:
            issues.append(
                ContractIssue(
                    code=API_UNKNOWN_KEYWORD,
                    message="{}() 没有参数 {!r}（第 {} 行）；可用: {}".format(
                        symbol, kw.arg, node.lineno, ", ".join(sorted(known))
                    ),
                    line=node.lineno,
                    symbol=symbol,
                    contract_id=item.id,
                    allowed_parameters=tuple(sorted(known)),
                    repair_hint="只用契约里登记的参数名",
                )
            )

    # 位置参数数量
    max_positional = item.max_positional()
    if len(node.args) > max_positional and not item.accepts_kwargs:
        issues.append(
            ContractIssue(
                code=API_TOO_MANY_POSITIONAL,
                message="{}() 位置参数过多（第 {} 行，最多 {} 个）".format(
                    symbol, node.lineno, max_positional
                ),
                line=node.lineno,
                symbol=symbol,
                contract_id=item.id,
                allowed_parameters=tuple(sorted(known)),
            )
        )

    # 必填参数
    provided: set[str] = set()
    positional_params = list(item.positional_parameters())
    for index, _arg in enumerate(node.args):
        if index < len(positional_params):
            provided.add(positional_params[index].name)
    for kw in node.keywords:
        if kw.arg:
            provided.add(kw.arg)
    for param in item.parameters:
        if param.kind in (VAR_POSITIONAL, VAR_KEYWORD):
            continue
        if param.required and param.name not in provided:
            issues.append(
                ContractIssue(
                    code=API_MISSING_ARGUMENT,
                    message="{}() 缺少必填参数 {!r}（第 {} 行）".format(symbol, param.name, node.lineno),
                    line=node.lineno,
                    symbol=symbol,
                    contract_id=item.id,
                    allowed_parameters=tuple(sorted(known)),
                    repair_hint="必填参数：{}".format(
                        ", ".join(
                            p.name for p in item.parameters if p.required and not p.is_variadic
                        )
                    ),
                )
            )

    # 常量类型（设计 §8.1「明确常量的类型可静态判断时必须匹配」）
    issues.extend(_literal_type_issues(node, item, symbol, positional_params))

    # await 语义
    parent = parents.get(node)
    awaited = isinstance(parent, ast.Await)
    if item.call_kind == ASYNC_METHOD and not awaited:
        # 作为别的调用的实参（如 asyncio.gather(...)）视为显式调度，不报
        if not isinstance(parent, ast.Call):
            issues.append(
                ContractIssue(
                    code=API_AWAIT_REQUIRED,
                    message="{}() 是 async 方法，必须 await（第 {} 行）".format(symbol, node.lineno),
                    line=node.lineno,
                    symbol=symbol,
                    contract_id=item.id,
                    repair_hint="写成 `await {}(...)`".format(symbol),
                )
            )
    elif item.call_kind == SYNC_METHOD and awaited:
        issues.append(
            ContractIssue(
                code=API_AWAIT_FORBIDDEN,
                message="{}() 不是 async 方法，不能 await（第 {} 行）".format(symbol, node.lineno),
                line=node.lineno,
                symbol=symbol,
                contract_id=item.id,
                repair_hint="去掉 await",
            )
        )
    return issues


def _check_return_semantics(
    tree: ast.AST,
    *,
    parents: dict[ast.AST, ast.AST],
    assigned: dict[str, ast.AST],
    profile_name: str,
) -> list[ContractIssue]:
    """返回值语义：``is not None`` 误判 + 访问未登记字段。

    只做**能确定**的判断：变量在本文件内只被赋值一次，且右值是契约调用。
    """
    issues: list[ContractIssue] = []
    spec = registry.get_profile(profile_name)
    origin: dict[str, ast.Call] = {}
    for name, value in assigned.items():
        call = _awaited_call(value)
        if call is None or not isinstance(call.func, ast.Attribute):
            continue
        item = spec.by_name(call.func.attr)
        if item is not None:
            origin[name] = call

    def contract_of(call: ast.Call | None) -> ApiContract | None:
        if call is None or not isinstance(call.func, ast.Attribute):
            return None
        return spec.by_name(call.func.attr)

    for node in ast.walk(tree):
        # a) identity 判断：x is None / x is not None / (await f()) is not None
        if isinstance(node, ast.Compare) and any(
            isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops
        ):
            left = node.left
            call: ast.Call | None = None
            label = ""
            if isinstance(left, ast.Await):
                call = _awaited_call(left)
                label = ast.unparse(left)
            elif isinstance(left, ast.Name):
                call = origin.get(left.id)
                label = left.id
            item = contract_of(call)
            if item is not None and item.returns.identity_check_unsafe:
                issues.append(
                    ContractIssue(
                        code=API_IDENTITY_CHECK,
                        message=(
                            "{} 的返回值不能用 `is None` / `is not None` 判断成功与否（第 {} 行）".format(
                                item.name, getattr(node, "lineno", 0)
                            )
                        ),
                        line=getattr(node, "lineno", 0),
                        symbol=item.name,
                        contract_id=item.id,
                        repair_hint="用真假判断：`if {}:` / `if not {}:`".format(label, label),
                    )
                )

        # b) 字段访问：只查读属性（不查方法调用）
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue  # 方法调用（如 hit.to_dict()）不按字段处理
            if node.attr.startswith("_"):
                continue
            call = origin.get(node.value.id)
            item = contract_of(call)
            if item is None:
                continue
            fields = set(item.returns.fields)
            if fields and node.attr not in fields:
                issues.append(
                    ContractIssue(
                        code=API_RETURN_FIELD,
                        message="{} 的返回值没有字段 {!r}（第 {} 行）".format(
                            item.name, node.attr, getattr(node, "lineno", 0)
                        ),
                        line=getattr(node, "lineno", 0),
                        symbol="{}.{}".format(item.name, node.attr),
                        contract_id=item.id,
                        allowed_parameters=tuple(sorted(fields)),
                        repair_hint="可用字段：{}".format(", ".join(sorted(fields))),
                    )
                )
    return issues


def unbounded_loop_issues(tree: ast.AST) -> list[ContractIssue]:
    """常量条件的 while 且循环体内没有 break → Fake Runtime smoke 会挂死。

    只拦"能确定"的写法：``while True:`` / ``while 1:``。带 ``break`` 的放行
    （实况脚本常见 ``while True: ... if done: break``）。
    """
    out: list[ContractIssue] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.While):
            continue
        test = node.test
        is_const_true = (
            isinstance(test, ast.Constant) and bool(test.value) and not isinstance(test.value, str)
        )
        if not is_const_true:
            continue
        has_break = any(
            isinstance(sub, ast.Break) and not _inside_nested_loop(sub, node)
            for sub in ast.walk(node)
        )
        if has_break:
            continue
        out.append(
            ContractIssue(
                code=UNBOUNDED_LOOP,
                message="`while {}` 没有 break（第 {} 行）；假运行冒烟会挂死，真跑也只能靠外部停".format(
                    ast.unparse(test), getattr(node, "lineno", 0)
                ),
                line=getattr(node, "lineno", 0),
                repair_hint="给循环加退出条件（轮次上限 / 抖动上限 / break）",
            )
        )
    return out


def _inside_nested_loop(node: ast.AST, outer: ast.While) -> bool:
    """``node`` 是否位于外层循环内部的另一个循环里（那 break 就不算外层的退出）。"""
    for sub in ast.walk(outer):
        if sub is outer or not isinstance(sub, (ast.While, ast.For)):
            continue
        if any(child is node for child in ast.walk(sub)):
            return True
    return False


def _declared_is_str(param) -> bool:
    declared = (param.type or "").strip().lower()
    if not declared:
        return False
    return declared.split("|")[0].strip() == "str"


def _literal_type_issues(
    node: ast.Call,
    item: ApiContract,
    symbol: str,
    positional_params: list,
) -> list[ContractIssue]:
    """只标**能确定**的类型错：字面量 str ↔ 声明的非 str（以及反向）。

    刻意不判 int/float/bool 之间的混用（``threshold=1`` 对 float 参数是合法写法），
    避免误报把正常脚本拦下——真正的引号错误（``threshold="0.9"``）才是高发项。
    """
    out: list[ContractIssue] = []
    pairs: list[tuple[Any, Any]] = []
    for index, arg in enumerate(node.args):
        if index < len(positional_params):
            pairs.append((arg, positional_params[index]))
    for kw in node.keywords:
        if kw.arg:
            param = item.parameter(kw.arg)
            if param is not None:
                pairs.append((kw.value, param))
    for value_node, param in pairs:
        if not isinstance(value_node, ast.Constant) or isinstance(value_node.value, bool):
            continue
        value = value_node.value
        if value is None:
            continue
        declared_str = _declared_is_str(param)
        is_str = isinstance(value, str)
        if declared_str == is_str:
            continue
        out.append(
            ContractIssue(
                code=API_LITERAL_TYPE,
                message="{}() 的参数 {!r} 类型不对（第 {} 行）：契约声明 {}，传的是 {!r}".format(
                    symbol,
                    param.name,
                    getattr(value_node, "lineno", 0),
                    param.type or "?",
                    value,
                ),
                line=getattr(value_node, "lineno", 0),
                symbol="{}.{}".format(item.name, param.name),
                contract_id=item.id,
                repair_hint=(
                    "去掉引号改成数字/布尔"
                    if is_str
                    else "加引号写成字符串（或改用契约里的字符串取值）"
                ),
            )
        )
    return out


def _dedupe(issues: list[ContractIssue]) -> list[ContractIssue]:
    """按 (code, line, symbol, message) 去重。

    注意必须带 message：同一行可能有两个不同的坏参数/坏字段，只用
    (code, line, symbol) 会把后一个吞掉（实测 click_image 同时写错两个参数时）。
    """
    seen: set[tuple[str, int, str, str]] = set()
    out: list[ContractIssue] = []
    for item in issues:
        key = (item.code, item.line, item.symbol, item.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def validate_artifact(
    code: str,
    *,
    profile: str | None = None,
    allow_login: bool = False,
    check_imports: bool = True,
) -> list[ContractIssue]:
    """校验**最终产物**（素材替换/打包之后）：同一套 AST 检查 + 残留 token。"""
    issues = validate_source(
        code,
        profile=profile,
        allow_login=allow_login,
        check_imports=check_imports,
    )
    leftovers = left_asset_tokens(code)
    if leftovers:
        issues.append(
            ContractIssue(
                code=ASSET_TOKEN_LEFT,
                message="最终产物里仍有未解析的素材引用: {}".format(", ".join(leftovers[:6])),
                symbol=", ".join(leftovers[:6]),
                repair_hint="素材引用必须能在当前会话素材区解析；先确认素材存在",
            )
        )
    return issues


def issues_by_code(issues: Iterable[ContractIssue]) -> dict[str, int]:
    """排障用：按 code 统计。"""
    counter: Counter[str] = Counter(i.code for i in issues)
    return dict(sorted(counter.items()))


# ── profile 判定（设计 §4.2 / §12.9：不猜 profile）──


def entry_target_names(code: str, *, entry_name: str = "do_work") -> set[str] | None:
    """取入口首参标注的类型名集合（无法解析返回 None）。"""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return None
    entry = _find_entry(tree, entry_name)
    if entry is None:
        return None
    args = list(getattr(entry.args, "posonlyargs", [])) + list(entry.args.args)
    if not args:
        return None
    return _annotation_names(args[0].annotation)


#: profile 来源标记（provisional evidence 与权威来源必须可区分）。
PROFILE_SOURCE_EXPLICIT = "explicit"
PROFILE_SOURCE_ANNOTATION = "annotation"
PROFILE_SOURCE_UNRESOLVED = "unresolved"


def profile_for_code(code: str, *, explicit: str | None = None) -> str:
    """决定校验用哪个 runtime profile（**禁止静默默认**，决定 §13.1）。

    规则：

    1. 调用方给了明确 profile → 用它（**唯一权威来源**：Runtime 的执行目标事实）；
    2. 注解**只**写 ``UserBrowser`` → browser（**provisional**，非权威）；
    3. 注解**只**写 ``UserWindow`` → desktop（**provisional**，非权威）；
    4. 其余（Union / 无注解 / 无法解析）→ :data:`registry.UNRESOLVED_PROFILE`。

    历史上第 4 类被静默判成 desktop（旧 ``registry.DEFAULT_PROFILE``），
    而 Union 与无注解在实况脚本里很常见（``scripts/`` 16/47、V3 commits 3/8 是 Union，
    另有 2/8 无注解）→ 等于拿错误 profile 去校验。
    调用方拿到 ``unresolved`` 后应改用 ``registry.effective_profile()``
    落到 ``portable_common`` 安全交集，或先去 Runtime 解析出真实执行目标。

    需要区分「权威 vs provisional」时用 :func:`profile_source_for_code`。
    """
    if explicit:
        return registry.get_profile(explicit).runtime_profile
    names = entry_target_names(code) or set()
    if names == {"UserBrowser"}:
        return "browser_user_browser"
    if names == {"UserWindow"}:
        return registry.LEGACY_DEFAULT_PROFILE
    return registry.UNRESOLVED_PROFILE


def profile_source_for_code(code: str, *, explicit: str | None = None) -> str:
    """返回 profile 的**来源**：``explicit`` / ``annotation`` / ``unresolved``。

    只有 ``explicit`` 是权威；``annotation`` 是 provisional evidence，
    不得据此签发绑定 profile 的凭证（设计 §14.4、决定 §13.1）。
    """
    if explicit:
        return PROFILE_SOURCE_EXPLICIT
    if profile_for_code(code) == registry.UNRESOLVED_PROFILE:
        return PROFILE_SOURCE_UNRESOLVED
    return PROFILE_SOURCE_ANNOTATION
