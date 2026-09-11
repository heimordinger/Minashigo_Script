"""Network 独立窗口入口（与 script_gen / script_spec 同级）。

关闭=隐藏缓存，不销毁监听；主程序退出时 force_close。
也可: python -m network_inspector
"""

from __future__ import annotations

# 实现仍在 gui.widgets；此处统一对外入口，避免主窗把它当工具子窗挂 parent
from gui.widgets.NetworkInspector import NetworkInspectorWindow

__all__ = ["NetworkInspectorWindow"]
