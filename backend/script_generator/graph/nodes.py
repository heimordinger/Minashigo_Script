"""Graph nodes: plan → (generate | generate_task×N → merge) → validate ↔ fix."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.script_generator.graph.plan_schema import (
    allowed_browser_methods,
    empty_plan,
    extract_image_role_hints,
    format_plan_for_display,
    format_plan_for_prompt,
    format_required_task_keys_checklist,
    format_task_contract_for_prompt,
    normalize_plan,
    parse_plan_text,
    plan_schema_hint,
    resolve_task_image_paths,
    select_plan_images,
    should_split,
    task_states_var,
    task_timeout_var,
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


def _hoist_plan_explanation(explanation: str) -> str:
    """plan 节点：介绍已在 generate 入口 normalize 过；此处再兜底一次。"""
    try:
        from backend.script_generator.explain_norm import prepare_explanation_for_codegen
        return prepare_explanation_for_codegen(explanation or "").normalized
    except Exception:
        try:
            from backend.script_generator.feedback_opt import hoist_trial_constraints
            return hoist_trial_constraints(explanation or "")
        except Exception:
            return explanation or ""


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


async def _llm_call_with_tools(
    state: ScriptGenState,
    *,
    messages: list[dict],
    system_prompt: str,
    on_partial=None,
) -> tuple[str, int, int]:
    from backend.script_generator.agent import call_llm_with_tools
    from backend.script_generator.api_catalog import allow_login_from_explanation

    return await call_llm_with_tools(
        provider=state["provider"],
        api_key=state["api_key"],
        model=state["model"],
        api_endpoint=state.get("api_endpoint"),
        messages=messages,
        system_prompt=system_prompt,
        on_partial=on_partial,
        on_status=state.get("on_status"),
        max_tokens=state.get("max_tokens"),
        allow_login=allow_login_from_explanation(state.get("explanation") or ""),
    )


def _emit_split_skeleton(state: ScriptGenState, plan: dict) -> None:
    tasks = plan.get("tasks") or []
    for i, t in enumerate(tasks):
        name = t.get("name") or f"任务{i + 1}"
        imgs = ", ".join(t.get("images") or []) or "（沿用共享/全部图）"
        _emit_artifact(
            state,
            "stage",
            f"task_{i}|pending|任务: {name}|图片: {imgs}",
        )
    _emit_artifact(
        state,
        "stage",
        "merge|pending|合并脚本|等待各任务片段生成完成",
    )


async def plan_node(state: ScriptGenState) -> dict[str, Any]:
    """Produce a structured JSON contract (vision subset or filenames + role hints)."""
    if not state.get("enable_plan", True):
        plan = empty_plan()
        _emit_artifact(state, "stage", "plan|done|已跳过规划|直接进入整文件生成")
        return {
            "plan": "",
            "plan_struct": plan,
            "split_mode": False,
            "task_index": 0,
            "task_codes": [],
            "stage": "plan_skipped",
        }

    _emit_status(state, "规划中…")
    _emit_artifact(
        state,
        "stage",
        "plan|running|规划中|统筹共享状态 / 是否拆分 / 图片分发",
    )

    from backend.script_generator.agent import (
        _build_messages,
        _load_config,
        _provider_supports_images,
    )

    cfg = _load_config()
    scripts = cfg.get("available_scripts", [])
    script_hint = "\n".join(
        f"- {s.get('module')}: {s.get('name')} — {s.get('desc')}" for s in scripts
    ) or "(none)"

    all_paths = [Path(p) for p in state.get("image_paths") or []]
    names = [p.name for p in all_paths]
    img_list = "\n".join(f"- {n}" for n in names) or "(no images)"
    explanation = state.get("explanation") or ""
    hints = extract_image_role_hints(explanation, names)
    hint_block = "\n".join(hints) if hints else "(none extracted; do not invent roles)"

    system = (
        "You are a planner for Minashigo automation scripts. "
        "Do NOT write Python. Output ONLY valid JSON.\n\n"
        + plan_schema_hint()
        + "\n\nExtra requirements:\n"
        "- First decide kind. multi_task only when >=2 independent goals.\n"
        "- For multi_task: define shared_states BEFORE tasks; assign shared_images "
        "and per-task images so every listed filename is claimed when relevant.\n"
        "- Do not put available_scripts into reuse unless the game/domain matches.\n"
        "- Prefer explanation + role hints over pixels when they disagree.\n"
        "- AUTHORITY: explanation weight > generic Rules / few-shot; @helper means return that state name.\n"
    )
    user = (
        f"## Script explanation\n{_hoist_plan_explanation(explanation)}\n\n"
        f"## Image filenames\n{img_list}\n\n"
        f"## Image role hints (from explanation / filenames; do not guess beyond this)\n"
        f"{hint_block}\n\n"
        f"## Available scripts to reuse\n{script_hint}\n\n"
        f"## Source dir\n{state.get('source_dir') or '(unset)'}\n"
    )
    send = bool(state.get("send_images")) and _provider_supports_images(state["provider"])
    plan_imgs = select_plan_images(all_paths, explanation, max_n=8) if send else []
    if send and plan_imgs:
        _emit_artifact(
            state,
            "stage",
            "plan|running|规划看图|"
            + ", ".join(p.name for p in plan_imgs[:8]),
        )
        messages = _build_messages(
            user,
            plan_imgs,
            provider=state["provider"],
            send_images=True,
            compress_images=bool(state.get("compress_images", False)),
        )
    else:
        messages = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    text, inp, out = await _llm_call(state, messages=messages, system_prompt=system)
    plan = parse_plan_text(text)
    split = should_split(plan)
    display = format_plan_for_display(plan)
    _emit_artifact(state, "plan", display)
    think = _plan_think_blurb(plan, split)
    _emit_artifact(state, "stage", f"plan|done|规划完成|{think or display}")
    if split:
        _emit_split_skeleton(state, plan)
    else:
        _emit_artifact(
            state,
            "stage",
            "generate|pending|整文件生成|不拆分，一次生成完整脚本",
        )
    return {
        "plan": display,
        "plan_struct": plan,
        "split_mode": split,
        "task_index": 0,
        "task_codes": [],
        "stage": "planned",
        **_add_tokens(state, inp, out),
    }


def _plan_think_blurb(plan: dict, split: bool) -> str:
    if not isinstance(plan, dict):
        return ""
    parts: list[str] = []
    kind = plan.get("kind") or ""
    if kind:
        parts.append(f"类型: {kind}")
    parts.append("拆分: 是" if split else "拆分: 否")
    shared = plan.get("shared_states") or []
    if isinstance(shared, list) and shared:
        names = []
        for s in shared[:8]:
            if isinstance(s, dict):
                names.append(str(s.get("name") or ""))
            else:
                names.append(str(s))
        names = [n for n in names if n]
        if names:
            parts.append("共享状态: " + ", ".join(names))
    tasks = plan.get("tasks") or []
    if isinstance(tasks, list) and tasks:
        tnames = [
            str(t.get("name") or "")
            for t in tasks
            if isinstance(t, dict)
        ]
        tnames = [n for n in tnames if n]
        parts.append(f"任务({len(tasks)}): " + " / ".join(tnames[:6]))
    return "\n".join(parts)


def route_after_plan(state: ScriptGenState) -> str:
    if state.get("split_mode") or should_split(state.get("plan_struct") or {}):
        return "split"
    return "single"


async def generate_node(state: ScriptGenState) -> dict[str, Any]:
    """Generate full Python script from explanation + structured plan + images."""
    _emit_status(state, "生成脚本中…")
    _emit_artifact(state, "stage", "generate|running|生成脚本|按计划与规则编写完整 Python")

    from backend.script_generator.agent import (
        _build_messages,
        _build_system_prompt,
        enforce_img_dir,
        format_explanation_structure_checklist,
        strip_code_fences,
    )

    source_dir = state.get("source_dir") or ""
    explanation = state.get("explanation") or ""
    free = bool(state.get("free_mode"))
    prompt = _build_system_prompt(
        source_dir=source_dir, explanation=explanation, free_mode=free,
    )
    plan_struct = state.get("plan_struct") or empty_plan()
    plan_block = format_plan_for_prompt(plan_struct)
    if plan_block.strip() and not free:
        explanation = f"{explanation}\n\n{plan_block}\n"
    elif plan_block.strip() and free and (plan_struct.get("tasks")):
        explanation = f"{explanation}\n\n## Pseudo plan (from introduction)\n{plan_block}\n"
    struct_checklist = format_explanation_structure_checklist(explanation)
    if struct_checklist.strip() and free:
        explanation = f"{explanation}\n\n{struct_checklist}\n"

    image_paths = [Path(p) for p in state.get("image_paths") or []]
    messages = _build_messages(
        explanation,
        image_paths,
        provider=state["provider"],
        send_images=bool(state.get("send_images", True)),
        compress_images=bool(state.get("compress_images", False)),
        lean=free,
    )
    raw, inp, out = await _llm_call_with_tools(
        state,
        messages=messages,
        system_prompt=prompt,
        on_partial=state.get("on_partial"),
    )
    code = enforce_img_dir(strip_code_fences(raw), source_dir)
    _emit_artifact(
        state,
        "stage",
        f"generate|done|脚本草稿已生成|约 {len(code)} 字符，进入结构校验",
    )
    return {
        "code": code,
        "errors": [],
        "attempt": 0,
        "stage": "generated",
        **_add_tokens(state, inp, out),
    }


async def generate_task_node(state: ScriptGenState) -> dict[str, Any]:
    """Generate one task fragment under the multi-task contract."""
    plan = normalize_plan(state.get("plan_struct") or empty_plan())
    tasks = plan.get("tasks") or []
    idx = int(state.get("task_index") or 0)
    if idx < 0 or idx >= len(tasks):
        return {
            "stage": "task_skip",
            "task_index": idx,
        }

    task = tasks[idx]
    tname = task.get("name") or f"任务{idx + 1}"
    _emit_status(state, f"生成任务 {idx + 1}/{len(tasks)}：{tname}…")
    _emit_artifact(
        state,
        "stage",
        f"task_{idx}|running|生成任务: {tname}|仅本任务状态机片段，不含 do_work",
    )

    from backend.script_generator.agent import (
        _build_messages,
        _build_system_prompt,
        strip_code_fences,
    )

    source_dir = state.get("source_dir") or ""
    explanation = state.get("explanation") or ""
    free = bool(state.get("free_mode"))
    base_prompt = _build_system_prompt(
        source_dir=source_dir, explanation=explanation, free_mode=free,
    )
    contract = format_task_contract_for_prompt(
        plan, task, task_index=idx, task_count=len(tasks),
    )
    system = (
        base_prompt
        + "\n\n## Fragment mode (CRITICAL)\n"
        "You are generating ONE task fragment for a multi-task script.\n"
        "Output ONLY Python code for this task (handlers + TASK_*_STATES + TIMEOUT).\n"
        "Do NOT output module docstring, IMG_DIR, imports, do_work, or other tasks.\n"
        f"Variable names MUST be exactly {task_states_var(task, idx)} and "
        f"{task_timeout_var(task, idx)}.\n"
        "Every key in the task.states list MUST appear verbatim as dict keys "
        "(do not rename 竞技场→arena etc.).\n"
        "Shared navigation handlers: write a short comment listing shared state names; "
        "implement business states for THIS task only.\n"
    )
    user_text = (
        f"{explanation}\n\n{contract}\n\n"
        f"## Full plan (context)\n{format_plan_for_prompt(plan)}\n"
    )
    all_paths = [Path(p) for p in state.get("image_paths") or []]
    image_paths = resolve_task_image_paths(plan, task, all_paths)
    messages = _build_messages(
        user_text,
        image_paths,
        provider=state["provider"],
        send_images=bool(state.get("send_images", True)),
        compress_images=bool(state.get("compress_images", False)),
        lean=free,
    )
    raw, inp, out = await _llm_call_with_tools(
        state,
        messages=messages,
        system_prompt=system,
        on_partial=state.get("on_partial"),
    )
    fragment = strip_code_fences(raw).strip()
    codes = list(state.get("task_codes") or [])
    # pad if needed
    while len(codes) < idx:
        codes.append("")
    if len(codes) == idx:
        codes.append(fragment)
    else:
        codes[idx] = fragment

    preview = fragment[:300] + ("…" if len(fragment) > 300 else "")
    _emit_artifact(
        state,
        "stage",
        f"task_{idx}|done|任务完成: {tname}|约 {len(fragment)} 字符\n{preview}",
    )
    return {
        "task_codes": codes,
        "task_index": idx + 1,
        "stage": f"task_{idx}_done",
        **_add_tokens(state, inp, out),
    }


def route_after_task(state: ScriptGenState) -> str:
    plan = normalize_plan(state.get("plan_struct") or {})
    n = len(plan.get("tasks") or [])
    idx = int(state.get("task_index") or 0)
    if idx < n:
        return "next"
    return "merge"


async def merge_node(state: ScriptGenState) -> dict[str, Any]:
    """Merge task fragments into one complete script."""
    _emit_status(state, "合并任务脚本中…")
    plan = normalize_plan(state.get("plan_struct") or empty_plan())
    codes = list(state.get("task_codes") or [])
    tasks = plan.get("tasks") or []
    _emit_artifact(
        state,
        "stage",
        f"merge|running|合并脚本|整合 {len(codes)} 个任务片段 + 共享导航 + do_work",
    )

    from backend.script_generator.agent import (
        _build_system_prompt,
        enforce_img_dir,
        strip_code_fences,
    )

    source_dir = state.get("source_dir") or ""
    explanation = state.get("explanation") or ""
    free = bool(state.get("free_mode"))
    base_prompt = _build_system_prompt(
        source_dir=source_dir, explanation=explanation, free_mode=free,
    )
    system = (
        base_prompt
        + "\n\n## Merge mode (CRITICAL)\n"
        "Merge the task fragments into ONE complete runnable Python file.\n"
        "Rules:\n"
        "- Single set of imports, IMG_DIR, thresholds, unknown_state / shared nav handlers.\n"
        "- Keep each TASK_*_STATES / TIMEOUT with EXACT variable names and keys from the plan.\n"
        "- Do NOT rename dict keys; do NOT drop business states when deduplicating shared handlers.\n"
        "- Wire do_work with run_task over all tasks.\n"
        "- Each task entry clicks ONLY its own entry.\n"
        "- Output ONLY the full Python source. No markdown fences.\n"
        "\n"
        + format_required_task_keys_checklist(plan)
        + "\n"
    )

    parts: list[str] = [
        format_plan_for_prompt(plan),
        "",
        "## Script explanation (summary context)",
        explanation[:4000],
        "",
    ]
    for i, frag in enumerate(codes):
        tname = tasks[i]["name"] if i < len(tasks) else f"task{i + 1}"
        parts.append(f"## Fragment {i + 1}: {tname}\n```python\n{frag}\n```\n")

    user = "\n".join(parts) + "\nMerge into the final complete script now."
    messages = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    # Merge is text-only (fragments already carry image knowledge)
    raw, inp, out = await _llm_call_with_tools(
        state,
        messages=messages,
        system_prompt=system,
        on_partial=state.get("on_partial"),
    )
    code = enforce_img_dir(strip_code_fences(raw), source_dir)
    _emit_artifact(
        state,
        "stage",
        f"merge|done|合并完成|约 {len(code)} 字符，进入结构校验",
    )
    return {
        "code": code,
        "errors": [],
        "attempt": 0,
        "stage": "merged",
        **_add_tokens(state, inp, out),
    }


def validate_node(state: ScriptGenState) -> dict[str, Any]:
    """Local validation: syntax + structural checks (no LLM)."""
    _emit_status(state, "校验中…")
    attempt = int(state.get("attempt") or 0)
    _emit_artifact(
        state,
        "stage",
        f"validate_{attempt}|running|校验中|语法 / FSM / API 白名单等本地检查",
    )
    from backend.script_generator.agent import (
        apply_codegen_patches,
        format_explanation_structure_checklist,
        validate_for_codegen,
    )

    code = state.get("code") or ""
    plan_struct = state.get("plan_struct") or {}
    source_dir = state.get("source_dir") or ""
    explanation = state.get("explanation") or ""
    free = bool(state.get("free_mode"))
    code, patch_notes = apply_codegen_patches(
        code,
        source_dir=source_dir,
        plan=plan_struct,
        explanation=explanation,
        free_mode=free,
    )
    if patch_notes:
        preview = "; ".join(patch_notes[:6])
        more = f" 等 {len(patch_notes)} 项" if len(patch_notes) > 6 else ""
        _emit_artifact(
            state,
            "stage",
            f"validate_{attempt}|running|本地补全状态键|{preview}{more}",
        )
    errors = validate_for_codegen(
        code,
        plan=plan_struct,
        source_dir=state.get("source_dir") or "",
        image_paths=state.get("image_paths") or [],
        explanation=state.get("explanation") or "",
        free_mode=free,
    )
    validate_label = "校验未通过" if errors else "校验通过"
    if free:
        validate_label += "（自由模式·严格成品检验）"
    if errors:
        body = "\n".join(f"- {e}" for e in errors[:8])
        if len(errors) > 8:
            body += f"\n…共 {len(errors)} 项"
        _emit_artifact(state, "stage", f"validate_{attempt}|error|{validate_label}|{body}")
    else:
        extra = ""
        if patch_notes:
            extra = f"（已本地补键 {len(patch_notes)} 处）"
        _emit_artifact(
            state,
            "stage",
            f"validate_{attempt}|done|{validate_label}|结构检查未发现问题{extra}",
        )
    out: dict[str, Any] = {
        "errors": errors,
        "stage": "validated" if not errors else "validate_failed",
    }
    if patch_notes and code != (state.get("code") or ""):
        out["code"] = code
    return out


async def fix_node(state: ScriptGenState) -> dict[str, Any]:
    """Ask LLM to fix validation errors."""
    attempt = int(state.get("attempt") or 0) + 1
    _emit_status(state, f"修复中（第 {attempt} 次）…")
    errs = state.get("errors") or []
    preview = "\n".join(f"- {e}" for e in errs[:5])
    _emit_artifact(
        state,
        "stage",
        f"fix_{attempt}|running|修复中（第 {attempt} 次）|{preview or '根据校验错误改写代码'}",
    )

    from backend.script_generator.agent import (
        build_img_dir_line,
        enforce_img_dir,
        format_explanation_structure_checklist,
        is_img_identifiers_only,
        strip_code_fences,
    )

    errors = state.get("errors") or []
    err_block = "\n".join(f"- {e}" for e in errors)
    methods = ", ".join(sorted(allowed_browser_methods()))
    plan_struct = state.get("plan_struct") or empty_plan()
    plan_block = format_plan_for_prompt(plan_struct)
    source_dir = state.get("source_dir") or ""
    explanation = state.get("explanation") or ""
    img_dir_hint = build_img_dir_line(source_dir) if source_dir else ""
    free = bool(state.get("free_mode"))
    tasks = (plan_struct or {}).get("tasks") or []
    if free or not tasks:
        checklist = format_explanation_structure_checklist(explanation)
    else:
        checklist = format_required_task_keys_checklist(plan_struct)
    img_id_only = is_img_identifiers_only()
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
        "When errors mention missing keys: ADD those exact string keys to the named dict; "
        "do not invent alternate names; do not put task A's keys into task B's table.\n"
        "Scene wiring: hub states 主界面/出击界面 belong in every task table; "
        "task-specific scenes (房间界面/竞技场/塔) only in the matching TASK_* table. "
        "You may alias a scene key to an existing handler (same function, two keys).\n"
        + (
            "Keep _img('stem') from script explanation; folder filename alignment is local.\n"
            if img_id_only
            else "Only use _img() / register_guard for PNG files that exist in the selected folder. "
            "Do NOT copy err1_1/err2_2 guards from login scripts unless those files exist.\n"
        )
        + "Add `import time` if using time.time / time.sleep.\n"
        "No Chinese punctuation outside string literals and comments.\n"
        + (f"IMG_DIR MUST be exactly: {img_dir_hint}\n" if img_dir_hint else "")
        + (
            "\nFREE MODE: introduction requires full multitask skeleton "
            "(run_task + TASK1_STATES, TASK2_STATES, …). Do NOT leave a STATES-only stub.\n"
            if free
            else ""
        )
    )
    user = (
        f"## Validation errors\n{err_block}\n\n"
        f"{plan_block}\n\n"
        f"{checklist}\n\n"
        f"## Current code\n```python\n{state.get('code') or ''}\n```\n\n"
        "Return the complete fixed file. Satisfy EVERY missing-key error using the "
        "exact TASK_* names and keys listed above."
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": user}]}]
    raw, inp, out = await _llm_call(
        state,
        messages=messages,
        system_prompt=system,
        on_partial=state.get("on_partial"),
    )
    code = enforce_img_dir(strip_code_fences(raw), source_dir)
    _emit_artifact(
        state,
        "stage",
        f"fix_{attempt}|done|第 {attempt} 次修复完成|已回写代码，将再次校验",
    )
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
