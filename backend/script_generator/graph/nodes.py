"""Graph nodes: plan → generate → validate ↔ fix."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.script_generator.graph.plan_schema import (
    allowed_browser_methods,
    empty_plan,
    format_plan_for_display,
    format_plan_for_prompt,
    parse_plan_text,
    plan_schema_hint,
)
from backend.script_generator.graph.state import ScriptGenState


def _emit_status(state: ScriptGenState, msg: str) -> None:
    cb = state.get("on_status")
    if callable(cb):
        try:
            cb(msg)
        except Exception:
            pass


def _emit_artifact(state: ScriptGenState, kind: str, payload: str) -> None:
    cb = state.get("on_artifact")
    if callable(cb):
        try:
            cb(kind, payload)
        except Exception:
            pass


def _add_tokens(state: ScriptGenState, inp: int, out: int) -> dict[str, int]:
    return {
        "input_tokens": int(state.get("input_tokens") or 0) + int(inp or 0),
        "output_tokens": int(state.get("output_tokens") or 0) + int(out or 0),
    }


async def _llm_call(
    state: ScriptGenState,
    *,
    messages: list[dict],
    system_prompt: str,
    on_partial=None,
) -> tuple[str, int, int]:
    from backend.script_generator.agent import call_llm

    return await call_llm(
        provider=state["provider"],
        api_key=state["api_key"],
        model=state["model"],
        api_endpoint=state.get("api_endpoint"),
        messages=messages,
        system_prompt=system_prompt,
        on_partial=on_partial,
        max_tokens=state.get("max_tokens"),
    )


async def plan_node(state: ScriptGenState) -> dict[str, Any]:
    """Produce a structured JSON plan (text only, no images)."""
    if not state.get("enable_plan", True):
        plan = empty_plan()
        return {
            "plan": "",
            "plan_struct": plan,
            "stage": "plan_skipped",
        }

    _emit_status(state, "规划中…")

    from backend.script_generator.agent import _load_config

    cfg = _load_config()
    scripts = cfg.get("available_scripts", [])
    script_hint = "\n".join(
        f"- {s.get('module')}: {s.get('name')} — {s.get('desc')}" for s in scripts
    ) or "(none)"

    names = [Path(p).name for p in state.get("image_paths") or []]
    img_list = "\n".join(f"- {n}" for n in names) or "(no images)"

    system = (
        "You are a planner for Minashigo automation scripts. "
        "Do NOT write Python. Output ONLY valid JSON.\n\n"
        + plan_schema_hint()
        + "\n\nExtra requirements:\n"
        "- Build scene_map from every scene-identifier image mentioned in the explanation.\n"
        "- If kind is multi_task, tasks[] is REQUIRED; each task.states must include 未知 "
        "and navigation states (返回主界面 / 返回出击界面) when the explanation says so.\n"
        "- Do not put available_scripts into reuse unless the game/domain matches.\n"
    )
    user = (
        f"## Script explanation\n{state.get('explanation', '')}\n\n"
        f"## Image filenames\n{img_list}\n\n"
        f"## Available scripts to reuse\n{script_hint}\n\n"
        f"## Source dir\n{state.get('source_dir') or '(unset)'}\n"
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    text, inp, out = await _llm_call(state, messages=messages, system_prompt=system)
    plan = parse_plan_text(text)
    display = format_plan_for_display(plan)
    _emit_artifact(state, "plan", display)
    return {
        "plan": display,
        "plan_struct": plan,
        "stage": "planned",
        **_add_tokens(state, inp, out),
    }


async def generate_node(state: ScriptGenState) -> dict[str, Any]:
    """Generate full Python script from explanation + structured plan + images."""
    _emit_status(state, "生成脚本中…")

    from backend.script_generator.agent import (
        _build_messages,
        _build_system_prompt,
        enforce_img_dir,
        strip_code_fences,
    )

    source_dir = state.get("source_dir") or ""
    prompt = _build_system_prompt(source_dir=source_dir)
    explanation = state.get("explanation") or ""
    plan_struct = state.get("plan_struct") or empty_plan()
    plan_block = format_plan_for_prompt(plan_struct)
    if plan_block.strip():
        explanation = f"{explanation}\n\n{plan_block}\n"

    image_paths = [Path(p) for p in state.get("image_paths") or []]
    messages = _build_messages(
        explanation,
        image_paths,
        provider=state["provider"],
        send_images=bool(state.get("send_images", True)),
        compress_images=bool(state.get("compress_images", False)),
    )
    raw, inp, out = await _llm_call(
        state,
        messages=messages,
        system_prompt=prompt,
        on_partial=state.get("on_partial"),
    )
    code = enforce_img_dir(strip_code_fences(raw), source_dir)
    return {
        "code": code,
        "errors": [],
        "attempt": 0,
        "stage": "generated",
        **_add_tokens(state, inp, out),
    }


def validate_node(state: ScriptGenState) -> dict[str, Any]:
    """Local validation: syntax + structural checks (no LLM)."""
    _emit_status(state, "校验中…")
    from backend.script_generator.agent import validate_generated_code

    code = state.get("code") or ""
    plan_struct = state.get("plan_struct") or {}
    errors = validate_generated_code(
        code,
        plan=plan_struct,
        source_dir=state.get("source_dir") or "",
        image_paths=state.get("image_paths") or [],
    )
    return {
        "errors": errors,
        "stage": "validated" if not errors else "validate_failed",
    }


async def fix_node(state: ScriptGenState) -> dict[str, Any]:
    """Ask LLM to fix validation errors."""
    attempt = int(state.get("attempt") or 0) + 1
    _emit_status(state, f"修复中（第 {attempt} 次）…")

    from backend.script_generator.agent import (
        build_img_dir_line,
        enforce_img_dir,
        strip_code_fences,
    )

    errors = state.get("errors") or []
    err_block = "\n".join(f"- {e}" for e in errors)
    methods = ", ".join(sorted(allowed_browser_methods()))
    plan_struct = state.get("plan_struct") or empty_plan()
    plan_block = format_plan_for_prompt(plan_struct)
    source_dir = state.get("source_dir") or ""
    img_dir_hint = build_img_dir_line(source_dir) if source_dir else ""

    system = (
        "You fix Minashigo automation Python scripts.\n"
        "Output ONLY the full corrected Python source. No markdown fences, no explanation.\n"
        f"Allowed browser methods ONLY: {methods}. Do NOT invent others "
        "(no click, get_window_size, get_element, etc.).\n"
        "Keep FSM shape: do_work(browser: UserBrowser) with type annotation, "
        "未知 recovery state, STATES or TASK*_STATES + run_task, "
        "unknown_state must return business state names on id match (never 未知 after match), "
        "every called helper must be imported, "
        "STATE_TIMEOUT / TASK*_TIMEOUT.\n"
        "No Chinese punctuation outside string literals and comments.\n"
        + (f"IMG_DIR MUST be exactly: {img_dir_hint}\n" if img_dir_hint else "")
    )
    user = (
        f"## Validation errors\n{err_block}\n\n"
        f"{plan_block}\n\n"
        f"## Current code\n```python\n{state.get('code') or ''}\n```\n\n"
        "Return the complete fixed file."
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    raw, inp, out = await _llm_call(
        state,
        messages=messages,
        system_prompt=system,
        on_partial=state.get("on_partial"),
    )
    code = enforce_img_dir(strip_code_fences(raw), source_dir)
    return {
        "code": code,
        "attempt": attempt,
        "errors": [],
        "stage": "fixed",
        **_add_tokens(state, inp, out),
    }


def route_after_validate(state: ScriptGenState) -> str:
    errors = state.get("errors") or []
    if not errors:
        return "ok"
    attempt = int(state.get("attempt") or 0)
    max_retries = int(state.get("max_fix_retries") or 2)
    if attempt < max_retries:
        return "fix"
    return "fail"
