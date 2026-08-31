"""独立脚本生成器工具包。可从主窗口打开，或 python -m script_gen。"""

from __future__ import annotations

__all__ = ["ScriptGenWindow", "open_script_gen"]


def open_script_gen(*, facade=None, parent=None):
    """打开（或复用）脚本生成 Tool 窗口。"""
    from script_gen.window import ScriptGenWindow

    return ScriptGenWindow.open(facade=facade, parent=parent)
