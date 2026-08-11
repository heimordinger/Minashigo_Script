# scripts/converter/ir.py
"""中间表示（IR）定义 —— 脚本转换管道的核心契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ==============================================================
# IR 契约一：所有 action 节点带统一"错误"输出字段
# 契约二：Guard/可复用块建模为带参数引用，不内联展开
# ==============================================================


@dataclass
class ActionNode:
    """单个操作节点（对应一个 TaskFlow action 节点）。"""
    type: str                     # "click", "click_image", "scroll", "b_sleep", ...
    properties: dict              # 该操作的参数（threshold、delta_y、down_time...）
    title: str = ""               # 节点标题（自动生成，可选覆盖）
    error_slot: bool = True       # IR 契约一：永远带错误输出插槽


@dataclass
class GuardRef:
    """Guard 弹窗守护引用（IR 契约二：参数化引用不内联）。"""
    images: list[str]            # 弹窗图片路径列表
    pianyi: list[tuple[int, int]] = field(default_factory=list)
    threshold: float = 0.85


@dataclass
class Loop:
    """循环块。"""
    body: list                    # 循环体内的 IR 节点序列
    header_actions: list = field(default_factory=list)  # 循环头隐式操作（如 update_frame）


# ==============================================================
# 类型 B（FSM）相关 IR
# ==============================================================


@dataclass
class Handler:
    """FSM 的一个状态处理器。"""
    name: str                     # 状态名
    body: list                    # handler 函数体解析出的 IR 节点序列
    timeout: Optional[float] = None  # 来自 STATE_TIMEOUT


@dataclass
class SetVariable:
    """设置变量值（用于 FSM 状态变量赋值）。"""
    name: str
    value: Any
    type_hint: str = "string"

@dataclass
class FsmBody:
    """FSM 主体：状态派发 + 所有处理器 + 循环逻辑。

    渲染器会将其展开为：
    - 1 个 flow/variable 节点（当前状态）
    - 1 个 flow/state_dispatch 节点（动态输出插槽）
    - 每个 handler → 一个子图
    - 末尾 goto 回循环头
    """
    handlers: list[Handler]
    initial_state: str = ""
    dispatch_var: str = "__state"



@dataclass
class ScriptIR:
    """脚本的完整中间表示。

    目前支持三种模式，由 body 序列中的顶层元素类型区分：
    - Type A（线性）：body = list[ActionNode]
    - Type B（FSM）：body 包含 Loop + 状态派发 + GuardRef
    - Type C（混合循环）：body 包含 Loop + 条件分支
    """
    entry_point: str                     # 入口函数名（通常是 "do_work"）
    body: list                          # 顶层 IR 节点序列
    guards: Optional[GuardRef] = None  # 脚本级 guard（FSM 模式）
    config: dict = field(default_factory=dict)   # 脚本配置
    source_path: str = ""               # 源脚本路径（调试用）
