"""AST 调用点提取（Phase 2，只读）。

只做三件事：

1. 找出源码里的**方法调用点**，给出**稳定**的 source span 与 node id；
2. 把接收者来源推到"入口参数 / 简单别名 / 已失效 / 不可确定 / 与入口无关"几态；
3. 保守处理分支与作用域：**不确定就报不确定**（``ambiguous``），绝不替调用方挑一个。

明确不做（Phase 2 范围）：

* 不做生产拒绝（硬闸属 Phase 3）；
* 不做类型/数据流分析、不做跨函数指针分析；
* 不解析属性链接收者（``self.win.x()``）：标 ``attribute``，宁可"未能归属"也不猜。

node id 的稳定性：``cs_<sha1(path|span|method)[:16]>``。同一个文件同一处调用
（哪怕整个项目换了绝对路径）都得到同一个 id，可以直接写进凭据/报告做对账键。
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass

#: ``do_work`` 的入口参数名（与 ``validator.DEFAULT_TARGET_NAMES`` 同一份事实）。
ENTRY_TARGET_NAMES: tuple[str, ...] = ("browser", "win")

#: 接收者来源状态。
RECEIVER_ENTRY = "entry_param"
RECEIVER_ALIAS = "alias"
RECEIVER_REASSIGNED = "reassigned"
RECEIVER_AMBIGUOUS = "ambiguous"
RECEIVER_ATTRIBUTE = "attribute"
RECEIVER_OTHER = "other"
RECEIVER_UNRESOLVED = "unresolved"

#: "曾经是别名、后来被重新赋值"与"分支里赋值"的内部标记。
_POISON_REASSIGNED = ("<reassigned>",)
_POISON_AMBIGUOUS = ("<ambiguous>",)


@dataclass(frozen=True)
class CallSite:
    """一个方法调用点（只读事实，不含任何判定）。"""

    node_id: str
    path: str
    line: int
    col: int
    end_line: int
    end_col: int
    receiver_expr: str
    receiver_root: str
    receiver_kind: str
    method: str
    is_await: bool
    positional: int
    keywords: tuple[str, ...]
    alias_chain: tuple[str, ...] = ()
    #: ``line:col-end_line:end_col``（便于日志/报告直接引用）。
    span: str = ""

    @property
    def receiver_resolved(self) -> bool:
        """接收者是否被证明来自入口执行器（别名链有效）。"""
        return self.receiver_kind in (RECEIVER_ENTRY, RECEIVER_ALIAS)


def _node_id(path: str, span: str, method: str) -> str:
    raw = "{}|{}|{}".format(path or "<source>", span, method)
    return "cs_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _span_of(node: ast.AST) -> tuple[int, int, int, int]:
    return (
        int(getattr(node, "lineno", 0) or 0),
        int(getattr(node, "col_offset", 0) or 0),
        int(getattr(node, "end_lineno", 0) or 0),
        int(getattr(node, "end_col_offset", 0) or 0),
    )


def _root_name(node: ast.AST) -> str:
    """属性链/名字的最左根名（``self.win`` → ``self``）。"""
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else ""


class _AliasTracker(ast.NodeVisitor):
    """按语句顺序跟踪"哪个名字是入口执行器"，并在不确定时把它标成不可信。

    保守规则（宁可报不确定）：

    * ``bb = browser`` ⇒ ``bb`` 是别名（链 ``("bb", "browser")``）；
    * ``bb = 别的值`` ⇒ 标 ``reassigned``（**来源失效**）；
    * 分支（``if/for/while/try``）里赋值 ⇒ 只有各分支都赋成**同一个**链才算别名，
      否则标 ``ambiguous``；
    * 函数参数/``for``/``with``/``except``/海象运算符 绑定到同名 ⇒ 一律按失效处理；
    * 嵌套函数继承外层别名，但自己的参数会遮蔽。
    """

    def __init__(self, *, path: str, entry_names: tuple[str, ...]) -> None:
        self.path = path
        self.entry_names = tuple(entry_names)
        self.sites: list[CallSite] = []
        self._aliases: dict[str, tuple[str, ...]] = {}
        self._await_depth = 0

    # ── 作用域 ──

    def _visit_function(self, node: ast.AST) -> None:
        saved = dict(self._aliases)
        args = getattr(node, "args", None)
        names: list[str] = []
        if args is not None:
            names = [a.arg for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)]
            if args.vararg:
                names.append(args.vararg.arg)
            if args.kwarg:
                names.append(args.kwarg.arg)
        for name in names:
            if name in self.entry_names:
                self._aliases[name] = (name,)
            else:
                self._aliases.pop(name, None)
        for stmt in getattr(node, "body", []) or []:
            self.visit(stmt)
        self._aliases = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        saved = dict(self._aliases)
        for arg in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            if arg.arg in self.entry_names:
                self._aliases[arg.arg] = (arg.arg,)
            else:
                self._aliases.pop(arg.arg, None)
        self.visit(node.body)
        self._aliases = saved

    # ── 绑定/别名 ──

    def _bind(self, name: str, value: ast.AST | None) -> None:
        if not name:
            return
        chain = self._chain_of(value)
        if chain is not None:
            # 链里**接收者在前、来源在后**：``bb = browser`` → ``("bb", "browser")``
            self._aliases[name] = (name,) + tuple(chain)
        elif name in self._aliases:
            self._aliases[name] = _POISON_REASSIGNED
        else:
            self._aliases.pop(name, None)

    def _chain_of(self, value: ast.AST | None) -> tuple[str, ...] | None:
        """``value`` 对应的来源链（不含调用方自己的名字）。"""
        if isinstance(value, ast.Name):
            key = value.id
            if key in self.entry_names:
                return (key,)
            chain = self._aliases.get(key)
            if chain is None:
                return None
            if chain in (_POISON_REASSIGNED, _POISON_AMBIGUOUS):
                return None
            return tuple(chain)
        return None

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, node.value)
            else:
                self._invalidate_target(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None:
            self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, node.value)
        else:
            self._invalidate_target(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:  # noqa: N802
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, node.value)

    def _invalidate_target(self, target: ast.AST) -> None:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name):
                self._bind(sub.id, None)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        self.visit(node.iter)
        self._invalidate_target(node.target)
        self._visit_branch([node.body, node.orelse])

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802
        self.visit_For(node)  # type: ignore[arg-type]

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._invalidate_target(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self.visit_With(node)  # type: ignore[arg-type]

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.name:
            self._bind(node.name, None)
        self._visit_branch([node.body])

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        self.visit(node.test)
        self._visit_branch([node.body, node.orelse])

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        self.visit(node.test)
        self._visit_branch([node.body, node.orelse])

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        branches = [node.body] + [h.body for h in node.handlers] + [node.orelse, node.finalbody]
        self._visit_branch(branches)

    def visit_Match(self, node: ast.AST) -> None:  # noqa: N802 - py3.10 match
        for case in getattr(node, "cases", []) or []:
            self._visit_branch([case.body])

    def _visit_branch(self, branches: list[list[ast.stmt]]) -> None:
        """分支合并：各分支结果一致才算别名，否则标 ``ambiguous``。"""
        snapshot = dict(self._aliases)
        results: list[dict[str, tuple[str, ...]]] = []
        for body in branches:
            self._aliases = dict(snapshot)
            for stmt in body:
                self.visit(stmt)
            results.append(dict(self._aliases))

        merged: dict[str, tuple[str, ...]] = {}
        names: set[str] = set()
        for result in results:
            names |= set(result)
        for name in names:
            values = {result.get(name) for result in results}
            if len(values) == 1 and None not in values:
                merged[name] = next(iter(values))
            else:
                merged[name] = _POISON_AMBIGUOUS
        for name, chain in snapshot.items():
            merged.setdefault(name, chain)
        self._aliases = merged

    # ── 调用点 ──

    def visit_Await(self, node: ast.Await) -> None:  # noqa: N802
        self._await_depth += 1
        self.visit(node.value)
        self._await_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute):
            self.sites.append(self._site(node, func))
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def _site(self, node: ast.Call, func: ast.Attribute) -> CallSite:
        line, col, end_line, end_col = _span_of(node)
        span = "{}:{}-{}:{}".format(line, col, end_line, end_col)
        receiver = func.value
        root = _root_name(receiver)
        kind = RECEIVER_OTHER
        chain: tuple[str, ...] = ()
        if isinstance(receiver, ast.Name):
            if root in self.entry_names:
                kind, chain = RECEIVER_ENTRY, (root,)
            else:
                found = self._aliases.get(root)
                if found is None:
                    kind = RECEIVER_UNRESOLVED
                elif found == _POISON_REASSIGNED:
                    kind = RECEIVER_REASSIGNED
                elif found == _POISON_AMBIGUOUS:
                    kind = RECEIVER_AMBIGUOUS
                else:
                    kind, chain = RECEIVER_ALIAS, found
        elif isinstance(receiver, ast.Attribute):
            kind = RECEIVER_ATTRIBUTE
        return CallSite(
            node_id=_node_id(self.path, span, func.attr),
            path=self.path,
            line=line,
            col=col,
            end_line=end_line,
            end_col=end_col,
            receiver_expr=ast.unparse(receiver) if hasattr(ast, "unparse") else root,
            receiver_root=root,
            receiver_kind=kind,
            method=str(func.attr),
            is_await=self._await_depth > 0,
            positional=len(node.args),
            keywords=tuple(kw.arg for kw in node.keywords if kw.arg),
            alias_chain=tuple(chain),
            span=span,
        )


def extract_calls(
    source: str, *, path: str = "", entry_names: tuple[str, ...] = ENTRY_TARGET_NAMES
) -> tuple[CallSite, ...]:
    """提取源码里的方法调用点（语法错误返回空元组；调用方另行判定语法）。"""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return ()
    tracker = _AliasTracker(path=path, entry_names=tuple(entry_names))
    for stmt in tree.body:
        tracker.visit(stmt)
    return tuple(tracker.sites)


def entry_target_names(source: str, *, default: tuple[str, ...] = ENTRY_TARGET_NAMES) -> tuple[str, ...]:
    """从 ``do_work`` 的首参名推出"入口执行器"的名字（脚本可以叫 ``win`` 或别的）。

    取不到就退回默认（``browser``/``win``）。**只读**，不改契约判定。
    """
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return tuple(default)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "do_work":
            args = list(node.args.posonlyargs) + list(node.args.args)
            if args:
                return (args[0].arg,) + tuple(default)
    return tuple(default)
