# scripts/converter/parser.py
"""AST 解析器 —— 读取 Python 脚本，输出 ScriptIR。

- Type A（线性）：顺序 await 调用链
- Type B（FSM）：STATES 字典 + handler 函数 + while True 主循环
- Type C（混合循环）：while True + if/elif/break（暂未实现）
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Optional

from .ir import (
    ActionNode, FsmBody, GuardRef, Handler, ScriptIR, SetVariable,
)


# -- 浏览器方法 -> 节点类型映射 --

_METHOD_MAP = {
    "click":            "click",
    "click_image":      "click_image",
    "click_text":       "click_text",
    "click_until_gone": "click_until_gone",
    "match_image":      "match_image",
    "wait_image":       "wait_image",
    "scroll":           "scroll",
    "scroll_to_bottom": "scroll_to_bottom",
    "scroll_to_top":    "scroll_to_top",
    "dmm_login":        "dmm_login",
    "goto":             "url_goto",
    "b_sleep":          "b_sleep",
}

_SLEEP_METHODS = {"b_sleep", "sleep"}


# -- FSM 相关常量 --

# handler 函数中常见的分支条件方法名
_CONDITIONAL_METHODS = {"match_image", "click_image", "wait_image"}

# 状态名变量的常见赋值目标名
_STATE_VAR_NAMES = {"state_name", "state", "current_state", "current"}


# -- 解析器 --

class ScriptParser:
    """解析一个 Python 脚本文件，返回 ScriptIR。"""

    def __init__(self, source_path: str | Path):
        self.source_path = Path(source_path)
        self._source = self.source_path.read_text(encoding="utf-8")
        self._tree = ast.parse(self._source)
        self._config: dict = {}
        self._guards: Optional[GuardRef] = None
        # 缓存函数定义：name -> ast.AsyncFunctionDef
        self._funcs: dict[str, ast.AsyncFunctionDef] = {}
        self._collect_funcs()

    def _collect_funcs(self):
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, ast.AsyncFunctionDef):
                self._funcs[node.name] = node

    def parse(self) -> ScriptIR:
        entry_point = self._find_entry_point()

        if self._is_fsm_pattern():
            return self._parse_fsm(entry_point)
        if self._is_hybrid_loop_pattern():
            return self._parse_hybrid(entry_point)

        return self._parse_linear(entry_point)

    # -- 模式识别 --

    def _is_fsm_pattern(self) -> bool:
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Assign):
                if self._is_states_dict(node):
                    return True
        return False

    def _is_hybrid_loop_pattern(self) -> bool:
        has_while_true = False
        has_break = False
        has_if_chain = False
        for node in ast.walk(self._tree):
            if isinstance(node, ast.While):
                if self._is_always_true(node.test):
                    has_while_true = True
                    for child in ast.walk(node):
                        if isinstance(child, ast.If):
                            has_if_chain = True
                        if isinstance(child, ast.Break):
                            has_break = True
        return has_while_true and (has_break or has_if_chain)

    @staticmethod
    def _is_always_true(test) -> bool:
        return (isinstance(test, ast.Constant) and test.value is True) or \
               (isinstance(test, ast.Name) and test.id == "True")

    @staticmethod
    def _is_states_dict(node: ast.Assign) -> bool:
        if not isinstance(node.value, ast.Dict):
            return False
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return True
        return False

    # -- 入口函数检测 --

    def _find_entry_point(self) -> str:
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "do_work":
                return node.name
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, ast.AsyncFunctionDef):
                return node.name
        raise ValueError(f"未找到 async 函数: {self.source_path}")

    def _get_entry_func(self) -> ast.AsyncFunctionDef:
        name = self._find_entry_point()
        func = self._funcs.get(name)
        if func is None:
            raise ValueError(f"入口函数 {name} 丢失")
        return func

    # -- 线性解析（Type A） --

    def _parse_linear(self, entry_point: str) -> ScriptIR:
        func = self._get_entry_func()
        nodes: list = []

        for stmt in func.body:
            parsed = self._parse_statement(stmt)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                nodes.extend(parsed)
            else:
                nodes.append(parsed)

        return ScriptIR(
            entry_point=entry_point,
            body=nodes,
            guards=self._guards,
            config=self._config,
            source_path=str(self.source_path),
        )

    def _parse_statement(self, stmt: ast.stmt) -> Optional[ActionNode | list[ActionNode]]:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await):
            return self._parse_await_call(stmt.value)
        return None

    def _parse_await_call(self, node: ast.Await) -> Optional[ActionNode | list[ActionNode]]:
        call = node.value
        if not isinstance(call, ast.Call):
            return None
        func = call.func
        method_name = None
        if isinstance(func, ast.Attribute):
            method_name = func.attr
        elif isinstance(func, ast.Name):
            method_name = func.id
        if method_name is None:
            return None
        if method_name == "sleep" and self._is_asyncio_func(func):
            method_name = "b_sleep"
        node_type = _METHOD_MAP.get(method_name)
        if node_type is None:
            return None
        kwargs = self._extract_keywords(call)
        if node_type == "b_sleep":
            args = self._extract_positional(call)
            if "seconds" not in kwargs and len(args) > 0:
                kwargs["seconds"] = args[0]
            if "seconds" not in kwargs:
                kwargs["seconds"] = 1.0
        return ActionNode(type=node_type, properties=kwargs)

    # -- FSM 解析（Type B） --

    def _parse_fsm(self, entry_point: str) -> ScriptIR:
        """完整的 FSM 解析。"""
        # 1. 提取 states 字典
        states_dict = self._find_states_dict()
        state_names = list(states_dict.keys())

        # 2. 提取 STATE_TIMEOUT
        timeouts = self._find_state_timeout()

        # 3. 解析每个 handler
        handlers: list[Handler] = []
        for name in state_names:
            handler_func_name = states_dict[name]
            handler = self._parse_handler(name, handler_func_name, timeouts)
            handlers.append(handler)

        # 4. 确定初始状态
        initial = state_names[0] if state_names else ""

        # 5. 检测 guard
        self._detect_guards()

        # 6. 构建 FSM 主体
        fsm_body = FsmBody(handlers=handlers, initial_state=initial)

        return ScriptIR(
            entry_point=entry_point,
            body=[fsm_body],
            guards=self._guards,
            config=self._config,
            source_path=str(self.source_path),
        )

    def _find_states_dict(self) -> dict[str, str]:
        """从 AST 中找到 STATES = {"name": func_name, ...} 并返回 {状态名: 函数名}。"""
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, ast.Assign):
                if not isinstance(node.value, ast.Dict):
                    continue
                target = node.targets[0] if node.targets else None
                if target is None or not isinstance(target, ast.Name):
                    continue
                # 匹配 STATES / states / state_map
                if target.id.upper() != "STATES" and target.id.lower() not in ("states", "state_map"):
                    continue
                result = {}
                for key, val in zip(node.value.keys, node.value.values):
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        continue
                    func_name = _ast_value(val)
                    if isinstance(func_name, str):
                        result[key.value] = func_name
                if result:
                    return result
        return {}

    def _find_state_timeout(self) -> dict[str, float]:
        """提取 STATE_TIMEOUT 字典。"""
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, ast.Assign):
                if not isinstance(node.value, ast.Dict):
                    continue
                target = node.targets[0] if node.targets else None
                if target is None or not isinstance(target, ast.Name):
                    continue
                if target.id.upper() != "STATE_TIMEOUT":
                    continue
                result = {}
                for key, val in zip(node.value.keys, node.value.values):
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        continue
                    v = _ast_value(val)
                    if isinstance(v, (int, float)):
                        result[key.value] = float(v)
                return result
        return {}

    def _parse_handler(self, state_name: str, func_name: str,
                       timeouts: dict[str, float]) -> Handler:
        """解析一个 handler 函数体。"""
        func = self._funcs.get(func_name)
        if func is None:
            # handler 函数未找到，返回空 body
            return Handler(name=state_name, body=[],
                          timeout=timeouts.get(state_name))

        body_nodes: list = []

        for stmt in func.body:
            parsed = self._parse_handler_statement(stmt)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                body_nodes.extend(parsed)
            else:
                body_nodes.append(parsed)

        return Handler(name=state_name, body=body_nodes,
                      timeout=timeouts.get(state_name))

    def _parse_handler_statement(self, stmt: ast.stmt
                                 ) -> Optional[ActionNode | list[ActionNode]]:
        """解析 handler 函数中的一条语句。

        支持的类型：
        - await browser.xxx() — 常规操作
        - if await browser.match/click/wait() : ... return "state" — 条件分支
        - return "state" — 状态转移
        - return None — 保持
        - return "__exit__" — 终止
        """
        # await 操作
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await):
            return self._parse_await_call(stmt.value)

        # return 语句
        if isinstance(stmt, ast.Return):
            return self._parse_return(stmt)

        # if 条件分支（match_image / click_image 等条件式）
        if isinstance(stmt, ast.If):
            return self._parse_if_branch(stmt)

        # 属性赋值忽略（如 `CFG.xxx = ...`、`xxx._counter = ...`）
        if isinstance(stmt, ast.Assign):
            return None

        return None

    def _parse_return(self, stmt: ast.Return) -> Optional[ActionNode]:
        """解析 return "state" / return None / return "__exit__"。

        返回一个特殊的 ActionNode 来表示状态转移。
        """
        if stmt.value is None:
            return ActionNode(type="_keep_state", properties={})

        val = _ast_value(stmt.value)
        if val is None or val == "None":
            return ActionNode(type="_keep_state", properties={})
        if val == "__exit__":
            return ActionNode(type="_exit", properties={})

        # return "下一状态"
        return ActionNode(type="_goto_state", properties={"target": str(val)})

    def _parse_if_branch(self, stmt: ast.If) -> Optional[list]:
        """解析 if 条件分支。

        常见模式：
        if await browser.click_image(x):  # 条件操作
            await browser.b_sleep(...)
            return "新状态"
        """
        # 只有当条件是 await browser.xxx() 时才处理
        if not (isinstance(stmt.test, ast.Await)
                and isinstance(stmt.test.value, ast.Call)):
            return None

        cond_call = stmt.test.value
        func = cond_call.func
        if not isinstance(func, ast.Attribute):
            return None

        cond_method = func.attr
        if cond_method not in _CONDITIONAL_METHODS:
            return None

        # 条件操作本身是一个 ActionNode
        cond_action = self._parse_await_call(stmt.test)
        if cond_action is None:
            return None

        nodes: list = [cond_action]

        # 提取 if 分支的返回值
        for body_stmt in stmt.body:
            parsed = self._parse_handler_statement(body_stmt)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                nodes.extend(parsed)
            else:
                nodes.append(parsed)

        return nodes

    def _detect_guards(self):
        """检测脚本中的 GUARDS 列表和 register_guard 调用。"""
        # 简单实现：找 GUARDS = [] 和 register_guard 调用
        guard_images = []
        for node in ast.iter_child_nodes(self._tree):
            # register_guard(img_path, ...)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Name) and call.func.id == "register_guard":
                    if call.args:
                        path = _ast_value(call.args[0])
                        if isinstance(path, str):
                            guard_images.append(path)

        if guard_images:
            self._guards = GuardRef(images=guard_images)

    # -- 混合循环解析（Type C，占位） --

    def _parse_hybrid(self, entry_point: str) -> ScriptIR:
        raise NotImplementedError("Type C（混合循环）解析尚未实现")

    # -- 工具方法 --

    @staticmethod
    def _extract_keywords(call: ast.Call) -> dict:
        kwargs = {}
        for kw in call.keywords:
            if kw.arg is None:
                continue
            kwargs[kw.arg] = _ast_value(kw.value)
        return kwargs

    @staticmethod
    def _extract_positional(call: ast.Call) -> list:
        return [_ast_value(a) for a in call.args]

    @staticmethod
    def _is_asyncio_func(func: ast.Attribute) -> bool:
        if isinstance(func.value, ast.Attribute):
            return func.value.attr == "asyncio" or "asyncio" in str(func.value.attr)
        if isinstance(func.value, ast.Name):
            return func.value.id == "asyncio"
        return False


# -- AST 值提取 --

def _ast_value(node: ast.expr):
    """将 AST 常量表达式提取为 Python 值。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        val = _ast_value(node.operand)
        return -val if isinstance(val, (int, float)) else val
    if isinstance(node, ast.BinOp):
        left = _ast_value(node.left)
        right = _ast_value(node.right)
        if isinstance(node.op, ast.Add):
            return left + right if isinstance(left, str) and isinstance(right, str) else None
        if isinstance(node.op, ast.Div):
            return left / right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
        if isinstance(node.op, ast.Mult):
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left * right
    if isinstance(node, ast.List):
        return [_ast_value(el) for el in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_ast_value(el) for el in node.elts)
    if isinstance(node, ast.Dict):
        return {_ast_value(k): _ast_value(v)
                for k, v in zip(node.keys, node.values) if k is not None}
    if isinstance(node, ast.Attribute):
        val = _ast_value(node.value)
        return f"{val}.{node.attr}" if isinstance(val, str) else None
    return None
