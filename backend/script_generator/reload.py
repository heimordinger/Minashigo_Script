"""开发期热重载：生成前刷新 script_generator 包，避免改代码后必须重启整个 GUI。"""

from __future__ import annotations

import importlib
import sys


def reload_script_generator() -> None:
    """按依赖顺序 reload，并清空 LangGraph 编译缓存。"""
    # 先清 graph 编译缓存（若已加载）
    builder = sys.modules.get("backend.script_generator.graph.builder")
    if builder is not None and hasattr(builder, "_compiled"):
        builder._compiled = None

    # 子模块先于父包；按名字长度倒序大致保证深层先 reload
    names = [
        name for name in list(sys.modules)
        if name == "backend.script_generator"
        or name.startswith("backend.script_generator.")
    ]
    names.sort(key=lambda n: n.count("."), reverse=True)

    for name in names:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            importlib.reload(mod)
        except Exception as e:
            print(f"[ScriptGenerator] reload 跳过 {name}: {e}")

    # 再确保入口可导入
    import backend.script_generator.agent  # noqa: F401
    try:
        import backend.script_generator.graph.builder as builder_mod
        builder_mod._compiled = None
    except Exception:
        pass
