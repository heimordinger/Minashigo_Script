"""函数级局部修订：只改目标 top-level 函数/状态表，再拼回原文件。"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class CodeUnit:
    kind: str  # func | assign
    name: str
    lineno: int  # 1-based inclusive
    end_lineno: int
    source: str


_STATE_ASSIGN_RE = re.compile(
    r"^(STATES|STATE_TIMEOUT|TASK_\w+_STATES|TASK_\w+_TIMEOUT|GUARDS)$"
)
_FUNC_NAME_RE = re.compile(r"\b([A-Za-z_][\w]*)\b")
_HINT_MAP: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"room_ok|收取奖励|房间|领体力|room_claim|离房|出房间", re.I),
     ["room", "claim", "领"]),
    (re.compile(r"jjc|竞技场|段位|倍率", re.I), ["jjc", "arena", "竞技"]),
    (re.compile(r"塔|tower|ta_", re.I), ["ta_", "tower", "爬塔", "塔"]),
    (re.compile(r"出击|sortie|go_sortie|返回出击", re.I), ["sortie", "出击", "go_sortie"]),
    (re.compile(r"主界面|go_home|返回主界面|home", re.I), ["home", "主界面", "go_home"]),
    (re.compile(r"unknown_state|未知|场景路由", re.I), ["unknown"]),
    (re.compile(r"do_work|run_task", re.I), ["do_work", "run_task"]),
]


def _node_source(code: str, node: ast.AST) -> str:
    lines = code.splitlines(keepends=True)
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if not start or not end:
        return ast.unparse(node) if hasattr(ast, "unparse") else ""
    return "".join(lines[start - 1 : end])


def list_code_units(code: str) -> list[CodeUnit]:
    """列出可替换的顶层 async/def 与 STATES/TASK_* 赋值。"""
    if not (code or "").strip():
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    units: list[CodeUnit] = []
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            src = _node_source(code, node)
            if not src.strip():
                continue
            units.append(
                CodeUnit(
                    kind="func",
                    name=node.name,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno or node.lineno,
                    source=src.rstrip() + "\n",
                )
            )
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and _STATE_ASSIGN_RE.match(t.id):
                src = _node_source(code, node)
                if not src.strip():
                    continue
                units.append(
                    CodeUnit(
                        kind="assign",
                        name=t.id,
                        lineno=node.lineno,
                        end_lineno=node.end_lineno or node.lineno,
                        source=src.rstrip() + "\n",
                    )
                )
    return units


def select_target_names(
    code: str,
    feedback_items: list[str],
    *,
    max_n: int = 6,
) -> list[str]:
    """根据反馈/诊断文案挑选要改的单元名。"""
    units = list_code_units(code)
    if not units:
        return []
    by_name = {u.name: u for u in units}
    names = list(by_name.keys())
    text = "\n".join(feedback_items or [])
    picked: list[str] = []
    seen: set[str] = set()

    def _add(n: str) -> None:
        if n in by_name and n not in seen:
            seen.add(n)
            picked.append(n)

    # 1) 文案里直接出现的函数/表名
    for m in _FUNC_NAME_RE.finditer(text):
        _add(m.group(1))

    # 2) 关键词 → 名字子串
    for pat, keys in _HINT_MAP:
        if not pat.search(text):
            continue
        for n in names:
            nl = n.lower()
            if any(k.lower() in nl for k in keys):
                _add(n)

    # 3) 房间相关时带上 TASK_room_* 表
    if re.search(r"room|房间|收取", text, re.I):
        for n in names:
            if re.search(r"TASK_.*room|room.*STATES|room.*TIMEOUT", n, re.I):
                _add(n)

    if not picked:
        # 兜底：unknown + do_work（避免空目标）
        for n in ("unknown_state", "do_work"):
            _add(n)

    return picked[:max_n]


def format_units_block(units: list[CodeUnit], names: list[str]) -> str:
    by = {u.name: u for u in units}
    parts: list[str] = []
    for n in names:
        u = by.get(n)
        if not u:
            continue
        parts.append(f"### {u.name}\n```python\n{u.source.rstrip()}\n```")
    return "\n\n".join(parts)


def parse_surgical_output(raw: str) -> tuple[str, dict[str, str], Optional[str]]:
    """解析局部修订输出。

    返回 (summary, {name: source}, full_code_or_None)。
    若模型仍回整文件，full_code 非空，调用方走整文件路径。
    """
    text = (raw or "").strip()
    summary = ""
    body = text
    if "<<<SUMMARY>>>" in text:
        rest = text.split("<<<SUMMARY>>>", 1)[1]
        if "<<<FUNCS>>>" in rest:
            summary, body = rest.split("<<<FUNCS>>>", 1)
        elif "<<<CODE>>>" in rest:
            summary, body = rest.split("<<<CODE>>>", 1)
        else:
            summary, body = rest, ""
    elif "<<<FUNCS>>>" in text:
        body = text.split("<<<FUNCS>>>", 1)[1]
    elif "<<<CODE>>>" in text:
        body = text.split("<<<CODE>>>", 1)[1]

    summary = summary.strip()
    body = body.strip()
    if body.endswith("<<<END>>>"):
        body = body[: -len("<<<END>>>")].strip()

    # 去掉 markdown 围栏整包
    fence = re.match(r"^```(?:python)?\s*([\s\S]*?)```\s*$", body)
    if fence:
        body = fence.group(1).strip()

    replacements: dict[str, str] = {}
    # ### name 分段
    chunks = re.split(r"(?m)^###\s+([A-Za-z_][\w]*)\s*$", body)
    if len(chunks) >= 3:
        # chunks[0]=preamble, then name, src, name, src...
        for i in range(1, len(chunks) - 1, 2):
            name = chunks[i].strip()
            src = chunks[i + 1].strip()
            src = re.sub(r"^```(?:python)?\s*", "", src)
            src = re.sub(r"\s*```\s*$", "", src)
            src = src.strip() + "\n"
            if name and src.strip():
                replacements[name] = src

    if replacements:
        return summary, replacements, None

    # 尝试：body 本身是若干 top-level def，按 AST 拆
    try:
        tree = ast.parse(body)
        for node in tree.body:
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                replacements[node.name] = _node_source(body, node).rstrip() + "\n"
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name) and _STATE_ASSIGN_RE.match(t.id):
                    replacements[t.id] = _node_source(body, node).rstrip() + "\n"
        if replacements:
            # 若几乎整文件（函数很多），当作 full
            orig_hint = len(re.findall(r"(?m)^(async\s+)?def\s+", body))
            if orig_hint >= 8 and len(replacements) >= 8:
                return summary, {}, body
            return summary, replacements, None
    except SyntaxError:
        pass

    if body.strip():
        return summary, {}, body
    return summary, {}, None


def splice_units(original: str, replacements: dict[str, str]) -> tuple[str, list[str]]:
    """按行号从后往前替换，避免偏移。返回 (new_code, notes)。"""
    if not replacements:
        return original, ["无替换单元"]
    units = list_code_units(original)
    by = {u.name: u for u in units}
    notes: list[str] = []
    lines = original.splitlines(keepends=True)

    # 新增：原文件没有的单元 → 插在 do_work 前或文末
    missing_new = [n for n in replacements if n not in by]
    ordered = sorted(
        [by[n] for n in replacements if n in by],
        key=lambda u: u.lineno,
        reverse=True,
    )
    for u in ordered:
        src = replacements[u.name]
        if not src.endswith("\n"):
            src += "\n"
        # 校验可解析
        try:
            ast.parse(src)
        except SyntaxError as e:
            notes.append(f"跳过 {u.name}：替换片段语法错误 ({e.msg})")
            continue
        lines[u.lineno - 1 : u.end_lineno] = [src if src.endswith("\n") else src + "\n"]
        notes.append(f"已替换 {u.kind}:{u.name}")

    code = "".join(lines)
    if missing_new:
        insert_blobs: list[str] = []
        for n in missing_new:
            src = replacements[n]
            if not src.endswith("\n"):
                src += "\n"
            try:
                ast.parse(src)
            except SyntaxError as e:
                notes.append(f"跳过新增 {n}：语法错误 ({e.msg})")
                continue
            insert_blobs.append(src)
            notes.append(f"已新增 {n}")
        if insert_blobs:
            blob = "\n".join(insert_blobs) + "\n"
            # 插到 do_work 之前
            try:
                tree = ast.parse(code)
                insert_at = None
                for node in tree.body:
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "do_work":
                        insert_at = node.lineno - 1
                        break
                clines = code.splitlines(keepends=True)
                if insert_at is None:
                    code = code.rstrip() + "\n\n" + blob
                else:
                    clines.insert(insert_at, "\n" + blob)
                    code = "".join(clines)
            except SyntaxError:
                code = code.rstrip() + "\n\n" + blob

    try:
        ast.parse(code)
    except SyntaxError as e:
        return original, [f"拼接后语法错误，已回退：{e.msg}（第 {e.lineno} 行）"]
    return code, notes


SURGICAL_SYSTEM_ADDENDUM = """
## Surgical revise mode (CRITICAL)
Do NOT rewrite the whole file.
Output format (STRICT):
<<<SUMMARY>>>
Chinese checklist (已改/未改) naming the functions you changed.
<<<FUNCS>>>
### function_or_assign_name
<complete replacement source for that top-level unit only>
### another_name
<...>
<<<END>>>
Only include units listed under TARGET UNITS.
You may ADD a new helper only if listed targets require it (use ### new_name).
Keep signatures and browser API whitelist. Never invent _img() filenames.
""".strip()
