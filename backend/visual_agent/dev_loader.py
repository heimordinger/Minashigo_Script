"""Safe source-mode hot loader for the visual execution backend.

The desktop app owns a single visual-agent window.  A new run is only started
after the previous run is terminal, so source modules can be refreshed before
constructing the next runtime without mutating a live runtime instance.
"""
from __future__ import annotations

import importlib
import sys
from typing import Type


_RELOAD_ORDER = (
    "backend.visual_agent.action_effect",
    "backend.visual_agent.loading_watch",
    "backend.visual_agent.prohibitions",
    "backend.visual_agent.transition_memory",
    "backend.visual_agent.task_graph.models",
    "backend.visual_agent.task_graph.logic",
    "backend.visual_agent.task_graph.store",
    "backend.visual_agent.task_graph.workflow",
    "backend.visual_agent.task_graph",
    "backend.visual_agent.runtime",
)


def source_hot_reload_enabled() -> bool:
    """Packaged builds stay stable; source checkouts refresh on every new run."""
    return not bool(getattr(sys, "frozen", False))


def load_runtime_class(*, hot_reload: bool | None = None) -> Type:
    enabled = source_hot_reload_enabled() if hot_reload is None else bool(hot_reload)
    importlib.invalidate_caches()
    if enabled:
        for name in _RELOAD_ORDER:
            module = sys.modules.get(name)
            if module is not None:
                importlib.reload(module)
    module = importlib.import_module("backend.visual_agent.runtime")
    return module.VisualAgentRuntime
