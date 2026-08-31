"""开发期热重载：生成前 / 打开脚本生成窗时刷新模块，避免每次改代码都重启 GUI。"""

from __future__ import annotations

import importlib
import sys


_BACKEND_EXTRAS = (
    "backend.script_generator.few_shot",
    "backend.script_generator.api_catalog",
    "backend.script_generator.feedback_opt",
    "backend.script_generator.explain_norm",
    "backend.script_generator.v2_semantic_map",
    "backend.script_generator.chat_session",
    "backend.script_generator.surgical_revise",
    "backend.script_generator.revise_tools",
    "backend.script_generator.diagnose",
    "backend.script_generator.session_archive",
    "backend.script_generator.vision_cache",
    "backend.script_generator.agent",
    "backend.script_generator.graph.plan_schema",
    "backend.script_generator.graph.state",
    "backend.script_generator.graph.nodes",
    "backend.script_generator.graph.builder",
    "backend.script_generator.graph",
)

_GUI_EXTRAS = (
    "gui.widgets.GenTrajectory",
    "gui.widgets.ScriptGenerator",
)


def reload_script_generator(*, include_gui: bool = False) -> None:
    """按依赖顺序 reload，并清空 LangGraph 编译缓存。

    include_gui=True 时同时重载轨迹 / 脚本生成面板模块（打开窗口时用）。
    """
    builder = sys.modules.get("backend.script_generator.graph.builder")
    if builder is not None and hasattr(builder, "_compiled"):
        builder._compiled = None

    names = [
        name for name in list(sys.modules)
        if name == "backend.script_generator"
        or name.startswith("backend.script_generator.")
    ]
    extras = list(_BACKEND_EXTRAS)
    if include_gui:
        extras.extend(_GUI_EXTRAS)
        # 也刷新已加载的 gui 相关
        for name in list(sys.modules):
            if name in _GUI_EXTRAS or name.startswith("gui.widgets.ScriptGenerator"):
                if name not in names:
                    names.append(name)

    for extra in extras:
        if extra not in names:
            try:
                __import__(extra)
                names.append(extra)
            except Exception:
                pass
    names.sort(key=lambda n: n.count("."), reverse=True)

    reloaded = []
    for name in names:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            importlib.reload(mod)
            reloaded.append(name)
        except Exception as e:
            print(f"[ScriptGenerator] reload 跳过 {name}: {e}")

    import backend.script_generator.agent  # noqa: F401
    try:
        import backend.script_generator.graph.builder as builder_mod
        builder_mod._compiled = None
    except Exception:
        pass

    scope = "backend+gui" if include_gui else "backend"
    print(f"[ScriptGenerator] 热重载完成（{scope}，{len(reloaded)} 个模块）")
