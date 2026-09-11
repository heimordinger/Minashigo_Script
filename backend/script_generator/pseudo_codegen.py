"""试运行注入 / 确认保存剥离伪录制开关。"""

from __future__ import annotations

import ast
import re
from typing import Optional

_DEBUG_ASSIGN_RE = re.compile(
    r"(?m)^(?P<indent>\s*)DEBUG_PSEUDO_RECORD\s*=\s*(True|False)\s*$"
)


def _safe_script_name(name: str) -> str:
    s = (name or "script").strip() or "script"
    s = re.sub(r"\.py$", "", s, flags=re.I)
    s = re.sub(r'[<>:"/\\|?*]+', "_", s)
    return s[:64] or "script"


def _ast_insert_line(code: str) -> Optional[int]:
    """返回「import 区/docstring 之后、首个实质语句之前」的插入行号（1-based）。

    用 AST 而不是正则：正则会被多行 import、字符串里出现的 def、
    `@decorator` 等形态带偏，插到非法语句边界 → SyntaxError（历史事故）。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for idx, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if (
            idx == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue  # 模块 docstring 不算实质语句
        return getattr(node, "lineno", None)
    return None


def _insert_at_statement_boundary(code: str, text: str) -> str:
    """把 text 插到安全的顶层语句边界；AST 不可用时回退到「首个 def/class 前」。"""
    lineno = _ast_insert_line(code)
    if lineno:
        lines = code.splitlines(keepends=True)
        at = max(0, min(len(lines), lineno - 1))
        return "".join(lines[:at]) + text + "".join(lines[at:])
    m = re.search(r"(?m)^(async\s+def|def|class)\s+", code)
    if m:
        return code[: m.start()] + text + code[m.start():]
    return code.rstrip() + "\n\n" + text


def _ensure_import_os(code: str) -> str:
    if re.search(r"(?m)^\s*import\s+os\b", code):
        return code
    if re.search(r"(?m)^\s*from\s+os\s+import\b", code):
        return code
    return _insert_at_statement_boundary(code, "import os\n")


def _code_compiles(code: str) -> bool:
    try:
        compile(code or "", "<pseudo_inject>", "exec")
        return True
    except SyntaxError:
        return False


def _force_debug_flag(code: str, *, enabled: bool) -> str:
    val = "True" if enabled else "False"
    if _DEBUG_ASSIGN_RE.search(code):
        return _DEBUG_ASSIGN_RE.sub(
            lambda m: f"{m.group('indent')}DEBUG_PSEUDO_RECORD = {val}",
            code,
            count=1,
        )
    if not enabled:
        return code
    marker = (
        "# 试运行自动开启伪录制（确认保存时会去掉）\n"
        f"DEBUG_PSEUDO_RECORD = {val}\n"
    )
    return _insert_at_statement_boundary(code, marker + "\n")


def _do_work_has_pseudo(code: str) -> bool:
    return "enable_pseudo_record" in code and "finish_pseudo_record" in code


def _inject_into_do_work(code: str, *, script_name: str) -> str:
    """若缺少 enable/finish，给 async def do_work 包一层伪录制。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    target: Optional[ast.AsyncFunctionDef] = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "do_work":
            target = node
            break
    if target is None or not target.body:
        return code

    for n in ast.walk(target):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in (
                "enable_pseudo_record",
                "finish_pseudo_record",
            ):
                return code

    lines = code.splitlines(keepends=True)
    start = target.body[0].lineno - 1
    end = len(lines)
    for node in tree.body:
        if getattr(node, "lineno", 0) > target.lineno:
            end = node.lineno - 1
            break

    body_lines = lines[start:end]
    if not body_lines:
        return code

    indent_m = re.match(r"^(\s*)", body_lines[0])
    ind = indent_m.group(1) if indent_m else "    "
    sn = _safe_script_name(script_name).replace("\\", "\\\\").replace("'", "\\'")

    nested: list[str] = []
    for ln in body_lines:
        raw = ln if ln.endswith("\n") else ln + "\n"
        if not raw.strip():
            nested.append("\n")
            continue
        if raw.startswith(ind):
            nested.append(ind + "    " + raw[len(ind) :])
        else:
            nested.append(ind + "    " + raw.lstrip())

    wrapped = (
        f"{ind}status = \"ok\"\n"
        f"{ind}if DEBUG_PSEUDO_RECORD or os.getenv(\"MINASHIGO_PSEUDO_RECORD\"):\n"
        f"{ind}    browser.enable_pseudo_record(script_name='{sn}', force=True)\n"
        f"{ind}try:\n"
        + "".join(nested)
        + f"{ind}except Exception:\n"
        f"{ind}    status = \"error\"\n"
        f"{ind}    raise\n"
        f"{ind}finally:\n"
        f"{ind}    browser.finish_pseudo_record(status=status)\n"
    )
    return "".join(lines[:start]) + wrapped + "".join(lines[end:])


def inject_pseudo_record_for_trial(code: str, *, script_name: str = "script") -> str:
    """试运行写入前：打开 DEBUG 开关；缺 enable/finish 时注入。

    注入后**自检编译**；逐级降级（完整注入 → 只加 import os + DEBUG 开关 → 原文），
    保证返回结果永远是合法 Python——历史上出现过注入把语句边界插坏、
    试运行 import 直接 SyntaxError 的事故。
    """
    text = (code or "").strip()
    if not text:
        return code
    candidates: list[str] = []
    full = _ensure_import_os(text)
    full = _force_debug_flag(full, enabled=True)
    if not _do_work_has_pseudo(full):
        full = _inject_into_do_work(full, script_name=script_name)
    candidates.append(full)
    minimal = _force_debug_flag(_ensure_import_os(text), enabled=True)
    candidates.append(minimal)
    candidates.append(text)
    for cand in candidates:
        if _code_compiles(cand):
            return cand
    return text


def _repair_empty_suites(text: str) -> str:
    """删行后若 try/except/finally/else 体为空，补 pass。"""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        m = re.match(r"^(\s*)(try|except\b.*|finally|else):\s*$", lines[i].rstrip("\r\n"))
        if m:
            ind = m.group(1)
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            need_pass = j >= len(lines)
            if not need_pass:
                next_ind = re.match(r"^(\s*)", lines[j]).group(1)
                if len(next_ind) <= len(ind):
                    need_pass = True
            if need_pass:
                out.append(f"{ind}    pass\n")
        i += 1
    return "".join(out)


def strip_pseudo_record_for_save(code: str) -> str:
    """确认保存前：关掉并去掉伪录制相关语句。"""
    text = code or ""
    if not text.strip():
        return code

    text = _force_debug_flag(text, enabled=False)
    text = re.sub(r"(?m)^\s*#\s*试运行自动开启伪录制.*\n", "", text)

    try:
        tree = ast.parse(text)
    except SyntaxError:
        out_lines = []
        for ln in text.splitlines(keepends=True):
            if "enable_pseudo_record" in ln or "finish_pseudo_record" in ln:
                continue
            out_lines.append(ln)
        return _repair_empty_suites("".join(out_lines))

    lines = text.splitlines(keepends=True)
    drop: set[int] = set()

    def _span_lines(node: ast.AST) -> range:
        end = getattr(node, "end_lineno", None) or node.lineno
        return range(node.lineno - 1, end)

    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            f = node.value.func
            if isinstance(f, ast.Attribute) and f.attr in (
                "enable_pseudo_record",
                "finish_pseudo_record",
            ):
                drop.update(_span_lines(node))
        if isinstance(node, ast.If):
            test_src = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if "DEBUG_PSEUDO_RECORD" in test_src or "MINASHIGO_PSEUDO_RECORD" in test_src:
                body_is_pseudo = True
                for stmt in node.body:
                    src = ast.unparse(stmt) if hasattr(ast, "unparse") else ""
                    if "enable_pseudo_record" not in src and "finish_pseudo_record" not in src:
                        if not isinstance(stmt, ast.Pass):
                            body_is_pseudo = False
                            break
                if body_is_pseudo and not node.orelse:
                    drop.update(_span_lines(node))

    if drop:
        kept = [ln for i, ln in enumerate(lines) if i not in drop]
        cleaned: list[str] = []
        blank = 0
        for ln in kept:
            if ln.strip():
                blank = 0
                cleaned.append(ln)
            else:
                blank += 1
                if blank <= 2:
                    cleaned.append(ln)
        text = "".join(cleaned)

    text = _repair_empty_suites(text)

    if "enable_pseudo_record" not in text and "finish_pseudo_record" not in text:
        text = _DEBUG_ASSIGN_RE.sub("", text)
        text = re.sub(r"(?m)^\s*#\s*.*伪录制.*\n", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text
