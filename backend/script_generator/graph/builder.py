"""Build and run the script-generation LangGraph."""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from backend.script_generator.graph.nodes import (
    fix_node,
    generate_node,
    plan_node,
    route_after_validate,
    validate_node,
)
from backend.script_generator.graph.state import ScriptGenState

_compiled = None


def build_script_gen_graph():
    """Compile plan → generate → validate ↔ fix."""
    global _compiled
    if _compiled is not None:
        return _compiled

    g = StateGraph(ScriptGenState)
    g.add_node("plan", plan_node)
    g.add_node("generate", generate_node)
    g.add_node("validate", validate_node)
    g.add_node("fix", fix_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges(
        "validate",
        route_after_validate,
        {"ok": END, "fix": "fix", "fail": END},
    )
    g.add_edge("fix", "validate")

    _compiled = g.compile()
    return _compiled


async def run_script_gen_graph(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    image_paths: list,
    source_dir: str = "",
    send_images: bool = True,
    compress_images: bool = False,
    enable_plan: bool = True,
    max_fix_retries: int = 2,
    max_tokens: int = 16384,
    on_partial=None,
    on_status=None,
    on_artifact=None,
) -> tuple[str, int, int]:
    graph = build_script_gen_graph()
    initial: ScriptGenState = {
        "explanation": explanation_text,
        "image_paths": [str(p) for p in image_paths],
        "source_dir": source_dir or "",
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "api_endpoint": api_endpoint,
        "send_images": send_images,
        "compress_images": compress_images,
        "enable_plan": enable_plan,
        "max_fix_retries": max_fix_retries,
        "max_tokens": int(max_tokens or 16384),
        "plan": "",
        "plan_struct": {},
        "code": "",
        "errors": [],
        "attempt": 0,
        "stage": "start",
        "input_tokens": 0,
        "output_tokens": 0,
        "on_partial": on_partial,
        "on_status": on_status,
        "on_artifact": on_artifact,
    }
    result: dict[str, Any] = await graph.ainvoke(initial)

    code = (result.get("code") or "").strip()
    errors = result.get("errors") or []
    if not code:
        raise RuntimeError("LangGraph 未生成任何代码")
    if errors:
        detail = "; ".join(errors[:5])
        raise RuntimeError(
            f"生成的脚本校验失败（已重试 {result.get('attempt', 0)} 次）: {detail}"
        )

    return code, int(result.get("input_tokens") or 0), int(result.get("output_tokens") or 0)
