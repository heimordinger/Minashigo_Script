"""Structured generation plan: parse / normalize / format."""

from __future__ import annotations

import json
import re
from typing import Any


PLAN_KINDS = ("single_fsm", "multi_task", "utility")
IMAGE_ROLES = ("id", "button", "other")

_ALLOWED_BROWSER_METHODS = frozenset({
    "match_image",
    "match_image_multi",
    "click_image",
    "wait_image",
    "b_sleep",
    "update_frame",
    "script_log",
    "note_state",
    "note_progress",
})


def allowed_browser_methods() -> frozenset[str]:
    return _ALLOWED_BROWSER_METHODS


def empty_plan() -> dict[str, Any]:
    return {
        "kind": "single_fsm",
        "states": [],
        "image_roles": [],
        "reuse": [],
        "scene_map": [],
        "tasks": [],
        "notes": "",
    }


def normalize_plan(data: Any) -> dict[str, Any]:
    """Coerce arbitrary JSON-ish data into a stable plan dict."""
    base = empty_plan()
    if not isinstance(data, dict):
        if isinstance(data, str) and data.strip():
            base["notes"] = data.strip()
        return base

    kind = str(data.get("kind") or "single_fsm").strip()
    if kind not in PLAN_KINDS:
        kind = "single_fsm"
    base["kind"] = kind

    states = []
    for item in data.get("states") or []:
        if isinstance(item, str):
            states.append({"name": item, "purpose": "", "timeout": 30})
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            timeout = float(item.get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30.0
        states.append({
            "name": name,
            "purpose": str(item.get("purpose") or "").strip(),
            "timeout": timeout,
        })
    base["states"] = states

    roles = []
    for item in data.get("image_roles") or []:
        if isinstance(item, str):
            roles.append({"name": item, "role": "other"})
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        role = str(item.get("role") or "other").strip().lower()
        if role not in IMAGE_ROLES:
            role = "other"
        roles.append({"name": name, "role": role})
    base["image_roles"] = roles

    reuse = []
    for item in data.get("reuse") or []:
        if isinstance(item, str):
            reuse.append({"module": item, "name": ""})
            continue
        if not isinstance(item, dict):
            continue
        module = str(item.get("module") or "").strip()
        if not module:
            continue
        reuse.append({
            "module": module,
            "name": str(item.get("name") or "").strip(),
        })
    base["reuse"] = reuse

    scene_map = []
    for item in data.get("scene_map") or []:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        image = str(item.get("image") or item.get("img") or "").strip()
        state = str(item.get("state") or "").strip()
        if not image or not state:
            continue
        scene_map.append({"image": image, "state": state})
    base["scene_map"] = scene_map

    tasks = []
    for item in data.get("tasks") or []:
        if isinstance(item, str):
            tasks.append({"name": item, "states": []})
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        st_list = []
        for s in item.get("states") or []:
            sn = str(s).strip() if not isinstance(s, dict) else str(s.get("name") or "").strip()
            if sn:
                st_list.append(sn)
        tasks.append({"name": name, "states": st_list})
    base["tasks"] = tasks

    base["notes"] = str(data.get("notes") or "").strip()
    return base


def parse_plan_text(raw: str) -> dict[str, Any]:
    """Parse LLM plan output (JSON or fenced JSON) into a normalized plan."""
    text = (raw or "").strip()
    if not text:
        return empty_plan()

    # strip ```json ... ```
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        return normalize_plan(json.loads(text))
    except json.JSONDecodeError:
        pass

    # try first {...} blob
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return normalize_plan(json.loads(text[start : end + 1]))
        except json.JSONDecodeError:
            pass

    plan = empty_plan()
    plan["notes"] = text
    return plan


def format_plan_for_prompt(plan: dict[str, Any]) -> str:
    """Compact structured block injected into the generate user message."""
    plan = normalize_plan(plan)
    lines = [
        "### Structured plan (MUST follow)",
        f"- kind: {plan['kind']}  "
        "(single_fsm → one STATES+do_work; multi_task → run_task + TASK*_STATES; "
        "utility → callable helpers + thin do_work if needed)",
    ]
    if plan["states"]:
        lines.append("- states:")
        for s in plan["states"]:
            lines.append(
                f"  - {s['name']}: {s.get('purpose') or '(no purpose)'} "
                f"(timeout≈{s.get('timeout', 30)})"
            )
    if plan["scene_map"]:
        lines.append(
            "- scene_map (unknown_state MUST return these state names on match; "
            "NEVER return 未知 after a successful id match):"
        )
        for m in plan["scene_map"]:
            lines.append(f"  - {m['image']} -> {m['state']}")
    if plan["tasks"]:
        lines.append(
            "- tasks (each TASK_*_STATES must include at least these state keys):"
        )
        for t in plan["tasks"]:
            st = ", ".join(t.get("states") or []) or "(fill from explanation)"
            lines.append(f"  - {t['name']}: [{st}]")
    if plan["image_roles"]:
        lines.append("- image_roles (id→nav_threshold, button→threshold/icon_threshold):")
        for r in plan["image_roles"]:
            lines.append(f"  - {r['name']}: {r['role']}")
    if plan["reuse"]:
        lines.append("- reuse (import these, do NOT rewrite; only if domain matches):")
        for u in plan["reuse"]:
            lines.append(f"  - {u['module']}" + (f" :: {u['name']}" if u.get("name") else ""))
    if plan.get("notes"):
        lines.append(f"- notes: {plan['notes']}")
    return "\n".join(lines)


def format_plan_for_display(plan: dict[str, Any]) -> str:
    """Human-readable plan for the GUI panel."""
    plan = normalize_plan(plan)
    lines = [f"类型: {plan['kind']}"]
    if plan["states"]:
        lines.append("状态:")
        for s in plan["states"]:
            purpose = s.get("purpose") or ""
            lines.append(f"  - {s['name']}  ({s.get('timeout', 30)}s)  {purpose}")
    if plan["scene_map"]:
        lines.append("场景映射:")
        for m in plan["scene_map"]:
            lines.append(f"  - {m['image']} -> {m['state']}")
    if plan["tasks"]:
        lines.append("任务:")
        for t in plan["tasks"]:
            st = ", ".join(t.get("states") or [])
            lines.append(f"  - {t['name']}: {st}")
    if plan["image_roles"]:
        lines.append("图片角色:")
        for r in plan["image_roles"]:
            lines.append(f"  - {r['name']} -> {r['role']}")
    if plan["reuse"]:
        lines.append("复用脚本:")
        for u in plan["reuse"]:
            lines.append(f"  - {u['module']}" + (f" / {u['name']}" if u.get("name") else ""))
    if plan.get("notes"):
        lines.append(f"备注: {plan['notes']}")
    if len(lines) == 1 and not plan["states"] and not plan["scene_map"] and not plan["tasks"]:
        lines.append("(空计划)")
    return "\n".join(lines)


def plan_schema_hint() -> str:
    """JSON schema reminder for the planner LLM."""
    return (
        "Reply with ONLY a JSON object (no markdown, no prose) matching:\n"
        "{\n"
        '  "kind": "single_fsm" | "multi_task" | "utility",\n'
        '  "states": [{"name": "未知", "purpose": "...", "timeout": 180}, ...],\n'
        '  "scene_map": [{"image": "rank.png", "state": "主界面"}, '
        '{"image": "出击_logo.png", "state": "出击界面"}],\n'
        '  "tasks": [{"name": "房间领体力", "states": ["未知", "返回主界面", "主界面", "房间领体力"]}],\n'
        '  "image_roles": [{"name": "foo.png", "role": "id"|"button"|"other"}],\n'
        '  "reuse": [{"module": "scripts....", "name": "func(...)" }],\n'
        '  "notes": "optional short Chinese note"\n'
        "}\n"
        "Extract scene_map from explanation phrases like '可作为…标识图'. "
        "For multi_task, fill tasks[].states with the full navigation chain per task. "
        "Always include a state named 未知 for scene recovery. "
        "Only list reuse when the module domain matches the explanation (e.g. no 孤儿 helpers for DeepOne). "
        "Classify each listed image. Prefer reuse over rewriting only when appropriate."
    )
