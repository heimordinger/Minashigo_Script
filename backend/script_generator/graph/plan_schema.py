"""Structured generation plan / multi-task contract: parse / normalize / format."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PLAN_KINDS = ("single_fsm", "multi_task", "utility")
IMAGE_ROLES = ("id", "button", "other")

_ALLOWED_BROWSER_METHODS = frozenset({
    "match_image",
    "match_image_multi",
    "click_image",
    "click",
    "wait_image",
    "b_sleep",
    "update_frame",
    "request_fps",
    "release_fps",
    "script_log",
    "note_state",
    "note_progress",
})

_NAV_HINTS = ("未知", "返回", "主界面", "出击界面", "出击")


def allowed_browser_methods() -> frozenset[str]:
    return _ALLOWED_BROWSER_METHODS


def empty_plan() -> dict[str, Any]:
    return {
        "kind": "single_fsm",
        "states": [],
        "shared_states": [],
        "shared_images": [],
        "image_roles": [],
        "reuse": [],
        "scene_map": [],
        "tasks": [],
        "notes": "",
    }


def _normalize_state_item(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None
        return {"name": name, "purpose": "", "timeout": 30.0}
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    try:
        timeout = float(item.get("timeout", 30))
    except (TypeError, ValueError):
        timeout = 30.0
    return {
        "name": name,
        "purpose": str(item.get("purpose") or "").strip(),
        "timeout": timeout,
    }


def _normalize_name_list(items: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("image") or "").strip()
        else:
            name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _slug_task_id(name: str, index: int) -> str:
    """ASCII-only id for TASK_* constants (Chinese names → taskN)."""
    raw = re.sub(r"[^0-9A-Za-z]+", "_", (name or "").strip())
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw:
        raw = f"task{index + 1}"
    if raw[0].isdigit():
        raw = "T" + raw
    return raw[:40]


def sanitize_task_id(raw: str, index: int = 0) -> str:
    return _slug_task_id(raw, index)


def task_states_var(task: dict[str, Any], index: int = 0) -> str:
    tid = sanitize_task_id(str(task.get("id") or ""), index)
    return f"TASK_{tid}_STATES"


def task_timeout_var(task: dict[str, Any], index: int = 0) -> str:
    tid = sanitize_task_id(str(task.get("id") or ""), index)
    return f"TASK_{tid}_TIMEOUT"


def format_required_task_keys_checklist(plan: Any) -> str:
    """给 fix / merge 用的硬性键清单。"""
    plan = normalize_plan(plan)
    lines = [
        "### Required TASK_* tables (names and keys MUST match exactly)",
    ]
    for i, t in enumerate(plan.get("tasks") or []):
        st_var = task_states_var(t, i)
        to_var = task_timeout_var(t, i)
        keys = t.get("states") or []
        key_list = ", ".join(repr(k) for k in keys) if keys else "(none)"
        lines.append(
            f"- task id={t.get('id')!r} name={t.get('name')!r}\n"
            f"  - {st_var} MUST contain keys: [{key_list}]\n"
            f"  - {to_var} MUST contain the same keys"
        )
    if len(lines) == 1:
        lines.append("- (no tasks)")
    return "\n".join(lines)


def _infer_shared_states(base: dict[str, Any]) -> None:
    """Fill shared_states when planner omitted them."""
    if base.get("shared_states"):
        return
    shared: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(st: dict[str, Any]) -> None:
        n = st["name"]
        if n in seen:
            return
        seen.add(n)
        shared.append(st)

    for s in base.get("states") or []:
        if any(h in s["name"] for h in _NAV_HINTS):
            _add(s)

    # States that appear in 2+ tasks are shared
    counts: dict[str, int] = {}
    for t in base.get("tasks") or []:
        for sn in t.get("states") or []:
            counts[sn] = counts.get(sn, 0) + 1
    name_to_state = {s["name"]: s for s in (base.get("states") or [])}
    for sn, c in counts.items():
        if c >= 2:
            _add(name_to_state.get(sn) or {"name": sn, "purpose": "", "timeout": 30.0})

    if not shared:
        # Always keep 未知 if present anywhere
        for s in base.get("states") or []:
            if s["name"] == "未知":
                _add(s)
                break
        else:
            for t in base.get("tasks") or []:
                if "未知" in (t.get("states") or []):
                    _add({"name": "未知", "purpose": "场景恢复", "timeout": 180.0})
                    break

    base["shared_states"] = shared


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

    states: list[dict[str, Any]] = []
    for item in data.get("states") or []:
        st = _normalize_state_item(item)
        if st:
            states.append(st)
    base["states"] = states

    shared_states: list[dict[str, Any]] = []
    for item in data.get("shared_states") or []:
        st = _normalize_state_item(item)
        if st:
            shared_states.append(st)
    base["shared_states"] = shared_states
    base["shared_images"] = _normalize_name_list(data.get("shared_images"))

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
    for i, item in enumerate(data.get("tasks") or []):
        if isinstance(item, str):
            name = item.strip()
            if not name:
                continue
            tasks.append({
                "id": _slug_task_id(name, i),
                "name": name,
                "entry": "",
                "exit": "",
                "states": [],
                "images": [],
                "purpose": "",
            })
            continue
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        st_list = _normalize_name_list(item.get("states"))
        tid_raw = str(item.get("id") or "").strip() or _slug_task_id(name, i)
        tid = sanitize_task_id(tid_raw, i)
        tasks.append({
            "id": tid,
            "name": name,
            "entry": str(item.get("entry") or "").strip(),
            "exit": str(item.get("exit") or "").strip(),
            "states": st_list,
            "images": _normalize_name_list(item.get("images")),
            "purpose": str(item.get("purpose") or "").strip(),
        })
    base["tasks"] = tasks

    # Demote multi_task with <2 tasks → single_fsm (no split)
    if base["kind"] == "multi_task" and len(tasks) < 2:
        base["kind"] = "single_fsm"

    if base["kind"] == "multi_task":
        _infer_shared_states(base)

    base["notes"] = str(data.get("notes") or "").strip()
    return base


def should_split(plan: Any) -> bool:
    """True when plan asks for per-task generation + merge."""
    p = normalize_plan(plan)
    return p["kind"] == "multi_task" and len(p.get("tasks") or []) >= 2


def resolve_task_image_paths(
    plan: Any,
    task: dict[str, Any],
    all_paths: list,
) -> list[Path]:
    """Images for one task: shared_images ∪ task.images; fallback to all if empty."""
    paths = [Path(p) for p in (all_paths or []) if p]
    by_name = {p.name: p for p in paths}
    plan_n = normalize_plan(plan)
    names = list(plan_n.get("shared_images") or []) + list(task.get("images") or [])
    seen: set[str] = set()
    out: list[Path] = []
    for n in names:
        n = str(n).strip()
        if not n or n in seen:
            continue
        seen.add(n)
        if n in by_name:
            out.append(by_name[n])
    return out if out else paths


def parse_plan_text(raw: str) -> dict[str, Any]:
    """Parse LLM plan output (JSON or fenced JSON) into a normalized plan."""
    text = (raw or "").strip()
    if not text:
        return empty_plan()

    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        return normalize_plan(json.loads(text))
    except json.JSONDecodeError:
        pass

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
    if plan["shared_states"]:
        lines.append("- shared_states (define once; all tasks reuse these handlers):")
        for s in plan["shared_states"]:
            lines.append(
                f"  - {s['name']}: {s.get('purpose') or '(no purpose)'} "
                f"(timeout≈{s.get('timeout', 30)})"
            )
    if plan["shared_images"]:
        lines.append("- shared_images (navigation / common ids):")
        for n in plan["shared_images"]:
            lines.append(f"  - {n}")
    if plan["states"] and not plan["shared_states"]:
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
            "- tasks (each TASK_*_STATES must include shared keys + its own business states):"
        )
        for i, t in enumerate(plan["tasks"]):
            st = ", ".join(t.get("states") or []) or "(fill from explanation)"
            imgs = ", ".join(t.get("images") or []) or "(none)"
            extra = []
            if t.get("id"):
                extra.append(f"id={t['id']}")
            if t.get("entry"):
                extra.append(f"entry={t['entry']}")
            if t.get("exit"):
                extra.append(f"exit={t['exit']}")
            meta = f" ({', '.join(extra)})" if extra else ""
            st_var = task_states_var(t, i)
            lines.append(
                f"  - {t['name']}{meta}: {st_var} keys=[{st}]; images=[{imgs}]"
            )
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


def format_task_contract_for_prompt(
    plan: dict[str, Any],
    task: dict[str, Any],
    *,
    task_index: int,
    task_count: int,
) -> str:
    """Prompt slice for generating one task fragment."""
    plan = normalize_plan(plan)
    lines = [
        "### Multi-task contract — generate ONE task fragment only",
        f"- This is task {task_index + 1}/{task_count}: {task.get('name')}",
        f"- task id: {sanitize_task_id(str(task.get('id') or ''), task_index)}",
        f"- MUST define exactly: {task_states_var(task, task_index)} and "
        f"{task_timeout_var(task, task_index)}",
        "- Do NOT write do_work / full file imports / other tasks.",
        "- DO write: state handler async defs for THIS task's business states,",
        f"  plus {task_states_var(task, task_index)} / {task_timeout_var(task, task_index)} "
        "that INCLUDE every key listed below (verbatim Chinese/English strings).",
        "- Shared navigation handlers: prefer commenting "
        "`# shared: 未知 / 返回主界面 — merge will keep one copy`.",
        "- Each task's entry state must click ONLY this task's entry (no other task entries).",
    ]
    if plan.get("shared_states"):
        lines.append("- shared_states (reference only):")
        for s in plan["shared_states"]:
            lines.append(
                f"  - {s['name']}: {s.get('purpose') or ''} (timeout≈{s.get('timeout', 30)})"
            )
    if plan.get("shared_images"):
        lines.append("- shared_images: " + ", ".join(plan["shared_images"]))
    st = ", ".join(task.get("states") or []) or "(from explanation)"
    imgs = ", ".join(task.get("images") or []) or "(assigned images below)"
    lines.append(f"- this task states: [{st}]")
    lines.append(f"- this task images: [{imgs}]")
    if task.get("entry"):
        lines.append(f"- entry: {task['entry']}")
    if task.get("exit"):
        lines.append(f"- exit / done: {task['exit']}")
    if task.get("purpose"):
        lines.append(f"- purpose: {task['purpose']}")
    if plan.get("scene_map"):
        lines.append("- scene_map (for unknown_state routing; shared):")
        for m in plan["scene_map"]:
            lines.append(f"  - {m['image']} -> {m['state']}")
    return "\n".join(lines)


def format_plan_for_display(plan: dict[str, Any]) -> str:
    """Human-readable plan for the GUI panel."""
    plan = normalize_plan(plan)
    lines = [f"类型: {plan['kind']}"]
    if should_split(plan):
        lines.append(f"拆分生成: 是（{len(plan['tasks'])} 个任务 → 再合并）")
    else:
        lines.append("拆分生成: 否（整文件一次生成）")
    if plan["shared_states"]:
        lines.append("共享状态:")
        for s in plan["shared_states"]:
            purpose = s.get("purpose") or ""
            lines.append(f"  - {s['name']}  ({s.get('timeout', 30)}s)  {purpose}")
    if plan["shared_images"]:
        lines.append("共享图片: " + ", ".join(plan["shared_images"]))
    if plan["states"] and not plan["shared_states"]:
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
            imgs = ", ".join(t.get("images") or [])
            lines.append(f"  - [{t.get('id')}] {t['name']}")
            if st:
                lines.append(f"      状态: {st}")
            if imgs:
                lines.append(f"      图片: {imgs}")
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
    if len(lines) <= 2 and not plan["states"] and not plan["scene_map"] and not plan["tasks"]:
        lines.append("(空计划)")
    return "\n".join(lines)


def plan_schema_hint() -> str:
    """JSON schema reminder for the planner LLM."""
    return (
        "Reply with ONLY a JSON object (no markdown, no prose) matching:\n"
        "{\n"
        '  "kind": "single_fsm" | "multi_task" | "utility",\n'
        '  "shared_states": [{"name": "未知", "purpose": "场景恢复", "timeout": 180}, '
        '{"name": "返回主界面", "purpose": "...", "timeout": 90}],\n'
        '  "shared_images": ["rank.png", "home.png", "出击_logo.png"],\n'
        '  "states": [{"name": "...", "purpose": "...", "timeout": 30}],\n'
        '  "scene_map": [{"image": "rank.png", "state": "主界面"}, '
        '{"image": "出击_logo.png", "state": "出击界面"}],\n'
        '  "tasks": [{\n'
        '    "id": "room",\n'
        '    "name": "房间领体力",\n'
        '    "entry": "主界面点房间",\n'
        '    "exit": "领完回主界面",\n'
        '    "states": ["未知", "返回主界面", "主界面", "房间领体力"],\n'
        '    "images": ["room_ap上限.png", "ta_jiangli.png"],\n'
        '    "purpose": "领取房间奖励"\n'
        "  }],\n"
        '  "image_roles": [{"name": "foo.png", "role": "id"|"button"|"other"}],\n'
        '  "reuse": [{"module": "scripts....", "name": "func(...)" }],\n'
        '  "notes": "optional short Chinese note"\n'
        "}\n"
        "Decision rules:\n"
        "- If explanation has 2+ independent goals (e.g. 房间奖励 + 竞技场 + 塔), "
        'use kind=multi_task and fill tasks[] (>=2).\n'
        "- If only one FSM / one goal, use kind=single_fsm and leave tasks=[] "
        "(or a single task — runtime will NOT split).\n"
        "- ALWAYS define shared_states first for multi_task: 未知 + navigation returns. "
        "Task states reference those names; do not invent conflicting names.\n"
        "- Assign every relevant image: navigation → shared_images; "
        "task-specific UI → that task.images. An image may appear in multiple tasks.\n"
        "- Build scene_map from explanation phrases like '可作为…标识图'.\n"
        "- Always include a state named 未知 for scene recovery.\n"
        "- Only list reuse when the module domain matches the explanation.\n"
        "- If screenshots are attached, use them only to confirm image_roles "
        "(id vs button); explanation text still wins over pixels.\n"
    )


_FILE_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g))",
    re.I,
)
_ID_LINE_RE = re.compile(r"标识图|作为.{0,8}标识|场景标识")
_BTN_LINE_RE = re.compile(r"按钮|点击")
_ID_NAME_RE = re.compile(r"(?i)^(logo|rank|.+_logo|.+_id)$")


def extract_image_role_hints(explanation: str, names: list[str] | None = None) -> list[str]:
    """Pull id/button hints from explanation lines that mention filenames."""
    expl = explanation or ""
    hints: list[str] = []
    seen: set[str] = set()
    for line in expl.splitlines():
        files = _FILE_IN_TEXT.findall(line)
        if not files:
            continue
        role = "other"
        if _ID_LINE_RE.search(line):
            role = "id"
        elif _BTN_LINE_RE.search(line):
            role = "button"
        snippet = line.strip()
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"
        for fname in files:
            key = fname.lower()
            if key in seen:
                continue
            seen.add(key)
            hints.append(f"- {fname} → {role}: {snippet}")
    for n in names or []:
        stem = Path(n).stem
        key = Path(n).name.lower()
        if key in seen or f"{stem.lower()}.png" in seen:
            continue
        if _ID_NAME_RE.match(stem):
            seen.add(key)
            hints.append(f"- {Path(n).name} → id (filename heuristic: logo/rank)")
    return hints[:24]


def select_plan_images(
    image_paths: list,
    explanation: str,
    max_n: int = 8,
) -> list[Path]:
    """Subset of screenshots for the plan step (named / logo-like first)."""
    paths = [Path(p) for p in (image_paths or []) if p]
    if not paths:
        return []
    expl = (explanation or "").lower()
    marked: list[Path] = []
    id_like: list[Path] = []
    mentioned: list[Path] = []
    for p in paths:
        name = p.name.lower()
        stem = p.stem.lower()
        in_text = name in expl
        if not in_text and len(stem) >= 4:
            in_text = stem in expl
        is_id = bool(_ID_NAME_RE.match(stem))
        is_marked = False
        if in_text:
            for m in re.finditer(re.escape(name), expl):
                window = expl[max(0, m.start() - 16) : m.end() + 28]
                if "标识" in window or "场景" in window:
                    is_marked = True
                    break
            if not is_marked:
                for m in re.finditer(re.escape(stem), expl):
                    window = expl[max(0, m.start() - 16) : m.end() + 28]
                    if "标识" in window:
                        is_marked = True
                        break
        if is_marked:
            marked.append(p)
        elif is_id:
            id_like.append(p)
        elif in_text:
            mentioned.append(p)
    ordered = marked + id_like + mentioned
    out: list[Path] = []
    seen: set[str] = set()
    for p in ordered:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= max(1, int(max_n)):
            break
    return out
