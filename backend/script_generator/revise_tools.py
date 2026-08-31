"""修订阶段只读工具：查函数 / 列图 / 诊断日志 / 硬校验。"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ReviseToolContext:
    """一次修订会话内工具可访问的只读上下文。"""

    code: str = ""
    source_dir: str = ""
    trial_log: str = ""
    feedback: str = ""
    explanation: str = ""
    methods: str = ""
    # 工具调用痕迹（给 meta / 摘要用）
    calls: list[str] = field(default_factory=list)


def openai_revise_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_unit",
                "description": (
                    "Get one top-level async/def or STATES/TASK_* assignment source. "
                    "Omit name (or pass empty) to list available unit names."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Unit name, e.g. room_claim or TASK_room_STATES",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_images",
                "description": (
                    "List ALLOWED image filenames in the script source folder. "
                    "Never invent png names not in this list."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "diagnose_log",
                "description": (
                    "Rule-based diagnosis from trial log + feedback: symptom, "
                    "root cause, must_fix, suspected code units. No LLM."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "feedback": {
                            "type": "string",
                            "description": "Optional override feedback text",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "validate_code",
                "description": (
                    "Hard-validate Python: pass full file in `code`, or a single "
                    "unit replacement in `snippet` (optional `name` for labeling). "
                    "Omit both to validate the current full script in context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Full Python source to validate",
                        },
                        "snippet": {
                            "type": "string",
                            "description": "Single top-level unit source to syntax-check",
                        },
                        "name": {
                            "type": "string",
                            "description": "Unit name when validating snippet",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lookup_api",
                "description": (
                    "Look up contract card for one allowed UserBrowser method "
                    "(e.g. wait_image, click_image, match_image)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Method name",
                        }
                    },
                    "required": ["name"],
                },
            },
        },
    ]


def claude_revise_tools() -> list[dict]:
    """Anthropic tools 形态（与 openai_revise_tools 同语义）。"""
    out: list[dict] = []
    for item in openai_revise_tools():
        fn = item.get("function") or {}
        out.append({
            "name": fn.get("name") or "",
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {
                "type": "object",
                "properties": {},
            },
        })
    return out


def revise_tools_hint() -> str:
    return (
        "\n## Revise tools (optional, prefer before editing)\n"
        "- get_unit: inspect one function/assign (empty name → list names)\n"
        "- list_images: ALLOWED png whitelist\n"
        "- diagnose_log: map trial log → must_fix / suspected units\n"
        "- validate_code: hard-check full code or a unit snippet\n"
        "- lookup_api: method contract card\n"
        "After tools, output <<<SUMMARY>>> then <<<FUNCS>>> only for TARGET UNITS.\n"
    )


def revise_text_tools_hint() -> str:
    """无原生 function-calling 时的伪工具协议。"""
    return (
        "\n## Text tools skill (no native function calling)\n"
        "You MAY call tools by emitting ONE or more blocks BEFORE final code:\n"
        "<<<TOOL>>>\n"
        '{"name":"diagnose_log","arguments":{}}\n'
        "<<<END_TOOL>>>\n"
        "or\n"
        "<<<TOOL>>>\n"
        '{"name":"get_unit","arguments":{"name":"room_claim"}}\n'
        "<<<END_TOOL>>>\n"
        "Available names: get_unit, list_images, diagnose_log, validate_code, lookup_api.\n"
        "Do NOT invent tool results. Wait for <<<TOOL_RESULT>>> from the system.\n"
        "When done investigating, output <<<SUMMARY>>> then <<<FUNCS>>> (no more TOOL blocks).\n"
    )


_TOOL_BLOCK_RE = re.compile(
    r"<<<TOOL>>>\s*([\s\S]*?)\s*<<<END_TOOL>>>",
    re.I,
)


def parse_text_tool_calls(raw: str) -> list[tuple[str, Any]]:
    """从模型纯文本里解析伪工具调用。返回 [(name, arguments), ...]。"""
    text = raw or ""
    calls: list[tuple[str, Any]] = []
    for m in _TOOL_BLOCK_RE.finditer(text):
        body = (m.group(1) or "").strip()
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
        name = ""
        arguments: Any = {}
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                name = str(data.get("name") or "").strip()
                arguments = data.get("arguments", {})
                if arguments is None:
                    arguments = {}
        except json.JSONDecodeError:
            # name: xxx\narguments: {...}
            nm = re.search(r"(?im)^name\s*[:=]\s*([A-Za-z_][\w]*)", body)
            if nm:
                name = nm.group(1).strip()
            am = re.search(r"(?is)arguments\s*[:=]\s*(\{[\s\S]*\})", body)
            if am:
                try:
                    arguments = json.loads(am.group(1))
                except json.JSONDecodeError:
                    arguments = {}
            elif not name and body:
                # 单行工具名
                name = body.split()[0].strip()
        if name:
            calls.append((name, arguments))
    return calls


def strip_text_tool_blocks(raw: str) -> str:
    return _TOOL_BLOCK_RE.sub("", raw or "").strip()


def format_text_tool_results(results: list[tuple[str, str]]) -> str:
    parts = ["<<<TOOL_RESULT>>>"]
    for name, content in results:
        parts.append(f"### {name}\n{(content or '').strip()}")
    parts.append("<<<END_TOOL_RESULT>>>")
    parts.append(
        "Continue: more <<<TOOL>>> if needed, else <<<SUMMARY>>> + <<<FUNCS>>>."
    )
    return "\n".join(parts)


def resolve_revise_tool_assist(
    *,
    tool_assist: Optional[dict] = None,
    vision_assist: Optional[dict] = None,
    defaults: Optional[dict] = None,
) -> Optional[dict]:
    """解析修订工具辅助模型（推荐千问文本模型）。

    优先显式 tool_assist；否则在 defaults.revise_tool_assist.enabled 时，
    复用辅助识图的 Key（reuse_vision_key），模型默认 qwen3.5-flash。
    """
    d = defaults if isinstance(defaults, dict) else {}
    cfg = d.get("revise_tool_assist") if isinstance(d.get("revise_tool_assist"), dict) else {}
    if tool_assist and isinstance(tool_assist, dict):
        key = str(tool_assist.get("api_key") or "").strip()
        model = str(tool_assist.get("model") or "").strip()
        provider = str(tool_assist.get("provider") or "").strip()
        if key and model and provider:
            return {
                "provider": provider,
                "api_key": key,
                "model": model,
                "api_endpoint": tool_assist.get("api_endpoint"),
            }

    if cfg.get("enabled") is False:
        return None

    prefer_provider = str(cfg.get("prefer_provider") or "qwen").strip() or "qwen"
    prefer_model = str(
        cfg.get("prefer_model") or "qwen3.5-flash"
    ).strip() or "qwen3.5-flash"
    reuse = cfg.get("reuse_vision_key", True)

    va = vision_assist if isinstance(vision_assist, dict) else {}
    if not reuse or not va:
        return None
    v_key = str(va.get("api_key") or "").strip()
    if not v_key:
        return None
    v_provider = str(va.get("provider") or "").strip()
    # 识图常为 qwen-vl-*；工具辅助改用文本模型，Key 同属百炼即可
    provider = prefer_provider
    endpoint = va.get("api_endpoint")
    if v_provider and v_provider != prefer_provider:
        # Key 可能只属于识图提供商；仅当识图也是 qwen/dashscope 系时复用
        if "qwen" not in v_provider.lower() and prefer_provider == "qwen":
            return None
        provider = v_provider if "qwen" in v_provider.lower() else prefer_provider
        if provider != prefer_provider:
            # 非千问：仍可用识图同模型，但 vl 不适合作码工具 → 跳过
            v_model = str(va.get("model") or "")
            if re.search(r"-vl|vision|llava", v_model, re.I):
                return None
            prefer_model = v_model or prefer_model
            endpoint = va.get("api_endpoint")
    return {
        "provider": provider,
        "api_key": v_key,
        "model": prefer_model,
        "api_endpoint": endpoint,
    }


def _parse_args(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    raw = (arguments or "").strip() if isinstance(arguments, str) else ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def dispatch_revise_tool(
    name: str,
    arguments: Any,
    ctx: ReviseToolContext,
    *,
    allow_login: bool = False,
) -> str:
    """执行修订工具；始终返回短文本结果。"""
    n = (name or "").strip()
    args = _parse_args(arguments)
    ctx.calls.append(n or "?")

    if n == "get_unit":
        return _tool_get_unit(ctx, str(args.get("name") or "").strip())
    if n == "list_images":
        return _tool_list_images(ctx)
    if n == "diagnose_log":
        fb = str(args.get("feedback") or "").strip() or ctx.feedback
        return _tool_diagnose_log(ctx, feedback=fb)
    if n == "validate_code":
        return _tool_validate_code(
            ctx,
            code=str(args.get("code") or ""),
            snippet=str(args.get("snippet") or ""),
            unit_name=str(args.get("name") or "").strip(),
        )
    if n == "lookup_api":
        from backend.script_generator.api_catalog import lookup_api, parse_lookup_name
        return lookup_api(parse_lookup_name(arguments), allow_login=allow_login)

    return (
        f"unknown tool {n!r}; available: "
        "get_unit, list_images, diagnose_log, validate_code, lookup_api"
    )


def _tool_get_unit(ctx: ReviseToolContext, name: str) -> str:
    from backend.script_generator.surgical_revise import list_code_units

    units = list_code_units(ctx.code or "")
    if not units:
        return "No parseable top-level units (syntax error or empty code)."
    if not name:
        lines = [f"- {u.kind}:{u.name} (L{u.lineno}-{u.end_lineno})" for u in units]
        return "Available units:\n" + "\n".join(lines)
    by = {u.name: u for u in units}
    u = by.get(name)
    if not u:
        hints = [x.name for x in units if name.lower() in x.name.lower()][:8]
        tip = f" Did you mean: {', '.join(hints)}?" if hints else ""
        return f"Unit {name!r} not found.{tip}"
    src = u.source
    if len(src) > 12000:
        src = src[:12000] + "\n# ... truncated ...\n"
    return (
        f"### {u.name} ({u.kind} L{u.lineno}-{u.end_lineno})\n"
        f"```python\n{src.rstrip()}\n```"
    )


def _tool_list_images(ctx: ReviseToolContext) -> str:
    from backend.script_generator.agent import list_source_image_names

    names = list_source_image_names(ctx.source_dir or "")
    if not names:
        return "No images found in source_dir (empty or missing folder)."
    return "ALLOWED images (" + str(len(names)) + "):\n" + "\n".join(
        f"- {n}" for n in names
    )


def _tool_diagnose_log(ctx: ReviseToolContext, *, feedback: str) -> str:
    from backend.script_generator.diagnose import diagnose_local, format_diagnosis_block
    from backend.script_generator.surgical_revise import select_target_names

    d = diagnose_local(
        code=ctx.code or "",
        trial_log=ctx.trial_log or "",
        feedback=feedback or "",
        explanation=ctx.explanation or "",
    )
    block = format_diagnosis_block(d)
    targets = select_target_names(
        ctx.code or "",
        [feedback or "", d.symptom, d.root_cause] + list(d.must_fix or []),
        max_n=8,
    )
    extra = ""
    if targets:
        extra = (
            "\n\nSuspected units (for get_unit / <<<FUNCS>>>):\n"
            + ", ".join(targets)
        )
    return (block or "(no strong local diagnosis)") + extra


def _tool_validate_code(
    ctx: ReviseToolContext,
    *,
    code: str,
    snippet: str,
    unit_name: str,
) -> str:
    from backend.script_generator.agent import validate_generated_code

    snip = (snippet or "").strip()
    if snip:
        try:
            ast.parse(snip)
        except SyntaxError as e:
            label = unit_name or "snippet"
            return f"FAIL {label}: syntax error — {e.msg} (line {e.lineno})"
        if unit_name and (ctx.code or "").strip():
            try:
                from backend.script_generator.surgical_revise import splice_units
                merged, notes = splice_units(ctx.code, {unit_name: snip + "\n"})
                if any("回退" in n for n in notes):
                    return "Snippet parses, but splice failed: " + "; ".join(notes[:4])
                errs = validate_generated_code(
                    merged,
                    source_dir=ctx.source_dir or "",
                    image_paths=[],
                    explanation=ctx.explanation or "",
                )
                if not errs:
                    return f"OK: snippet {unit_name!r} splices + hard-validate pass"
                return (
                    f"Snippet {unit_name!r} parses, but after splice hard-validate fails:\n"
                    + "\n".join(f"- {e}" for e in errs[:12])
                )
            except Exception as e:
                return f"Snippet parses OK; splice/validate skipped ({e})"
        return f"OK: snippet{(f' {unit_name}' if unit_name else '')} parses"

    src = (code or "").strip() or (ctx.code or "")
    if not src.strip():
        return "FAIL: no code to validate"
    try:
        ast.parse(src)
    except SyntaxError as e:
        return f"FAIL: syntax error — {e.msg} (line {e.lineno})"
    errs = validate_generated_code(
        src,
        source_dir=ctx.source_dir or "",
        image_paths=[],
        explanation=ctx.explanation or "",
    )
    if not errs:
        return "OK: hard-validate pass"
    return "FAIL hard-validate:\n" + "\n".join(f"- {e}" for e in errs[:16])
