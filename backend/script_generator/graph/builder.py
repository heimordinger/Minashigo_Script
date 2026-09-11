"""Build and run the script-generation LangGraph."""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from backend.script_generator.graph.nodes import (
    fix_node,
    generate_node,
    generate_task_node,
    merge_node,
    plan_node,
    route_after_plan,
    route_after_task,
    route_after_validate,
    select_images_node,
    validate_node,
)
from backend.script_generator.graph.state import ScriptGenState

_compiled = None


def build_script_gen_graph():
    """Compile plan → select_images → (generate | task×N → merge) → validate ↔ fix."""
    global _compiled
    if _compiled is not None:
        return _compiled

    g = StateGraph(ScriptGenState)
    g.add_node("plan", plan_node)
    g.add_node("select_images", select_images_node)
    g.add_node("generate", generate_node)
    g.add_node("generate_task", generate_task_node)
    g.add_node("merge", merge_node)
    g.add_node("validate", validate_node)
    g.add_node("fix", fix_node)

    g.add_edge(START, "plan")
    g.add_edge("plan", "select_images")
    g.add_conditional_edges(
        "select_images",
        route_after_plan,
        {"single": "generate", "split": "generate_task"},
    )
    g.add_edge("generate", "validate")
    g.add_conditional_edges(
        "generate_task",
        route_after_task,
        {"next": "generate_task", "merge": "merge"},
    )
    g.add_edge("merge", "validate")
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
    free_mode: bool = False,
    on_partial=None,
    on_status=None,
    on_artifact=None,
) -> tuple[str, int, int]:
    graph = build_script_gen_graph()
    # 0 = 无上限（与 UI / resolve_max_tokens 约定一致）
    stored_mt = 0 if max_tokens is None else int(max_tokens)
    plan_struct: dict = {}
    if free_mode:
        try:
            from backend.script_generator.agent import build_pseudo_plan_from_explanation
            from backend.script_generator.architecture import apply_architecture_to_plan
            plan_struct = apply_architecture_to_plan(
                build_pseudo_plan_from_explanation(explanation_text or "") or {},
                explanation_text or "",
            )
        except Exception:
            plan_struct = {}
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
        "max_tokens": stored_mt,
        "free_mode": bool(free_mode),
        "plan": "",
        "plan_struct": plan_struct,
        "split_mode": False,
        "task_index": 0,
        "task_codes": [],
        "image_parts": [],
        "selected_part_ids": [],
        "image_selection": {},
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
    # 统一校验上下文：让 GUI 归档/试运行用与管线完全相同的 plan/free/source_dir 校验，
    # 避免「管线判过、UI 判不过」的伪失败。
    if callable(on_artifact):
        try:
            import json as _json

            on_artifact(
                "gen_ctx",
                _json.dumps(
                    {
                        "family": "graph",
                        "plan_struct": result.get("plan_struct") or {},
                        "free_mode": bool(result.get("free_mode")),
                        "source_dir": source_dir or "",
                        "split_mode": bool(result.get("split_mode")),
                        "selected_part_ids": result.get("selected_part_ids") or [],
                        "attempt": int(result.get("attempt") or 0),
                        "max_fix_retries": int(max_fix_retries or 0),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
    if not code:
        raise RuntimeError("LangGraph 未生成任何代码")
    if errors:
        detail = "; ".join(errors[:5])
        err = RuntimeError(
            f"生成的脚本校验失败（已重试 {result.get('attempt', 0)} 次）: {detail}",
        )
        err.partial_code = code  # type: ignore[attr-defined]
        raise err

    return code, int(result.get("input_tokens") or 0), int(result.get("output_tokens") or 0)
