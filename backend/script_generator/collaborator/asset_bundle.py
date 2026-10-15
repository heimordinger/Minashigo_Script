# -*- coding: utf-8 -*-
"""脚本落盘时把用到的素材收进 ``assets/images/<脚本名>/``，并把脚本里的素材路径改成
``IMG_DIR / "文件"``。

落盘形态与手写脚本一致（参见 ``scripts/测试/quick_script.py``）::

    from core.path import IMG_PATH

    IMG_DIR = IMG_PATH / "<脚本名>"

    await browser.click_image(img_path=IMG_DIR / "step_001.png")

这样脚本只认自己那一个文件夹，不再依赖截图目录 / 会话素材目录，把脚本挪走也不会丢图。

三种引用都能认出来并一起搬走：
  1. 字符串字面量里的绝对 / 相对图片路径；
  2. ``IMG_DIR / "x.png"``、``os.path.join(IMG_DIR, "x.png")``；
  3. ``[[asset_id]]`` / ``[[素材名]]`` 占位符（按素材区解析）。
"""

from __future__ import annotations

import ast
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.path import IMG_PATH

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

_IMG_DIR_REF_RE = re.compile(
    r"""IMG_DIR\s*/\s*(?P<q>['"])(?P<name>[^'"]+)(?P=q)"""
    r"""|os\.path\.join\(\s*IMG_DIR\s*,\s*(?P<q2>['"])(?P<name2>[^'"]+)(?P=q2)\s*\)"""
)
_IMG_DIR_IMPORT_LINE = "from core.path import IMG_PATH"


@dataclass
class BundleResult:
    """一次素材打包的结果。"""

    out_dir: Path
    copied: list[dict[str, str]] = field(default_factory=list)
    rewrites: int = 0
    unresolved: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: True 表示只算改写、不真的拷文件（校验闸门用：不得为了校验写用户文件）。
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.copied or self.rewrites)

    def summary_lines(self) -> list[str]:
        out: list[str] = []
        if self.copied:
            out.append(f"素材已打包 {len(self.copied)} 个 → {self.out_dir}")
        if self.rewrites:
            out.append(f"脚本里的素材路径已改成 IMG_DIR / 文件名（{self.rewrites} 处）")
        if self.unresolved:
            out.append("没能解析的素材引用：" + "、".join(self.unresolved[:6]))
        if self.errors:
            out.append("打包出错：" + "；".join(self.errors[:3]))
        return out


@dataclass
class _Plan:
    src: Path
    name: str
    ok: bool = False


def _line_starts(lines: list[str]) -> list[int]:
    starts: list[int] = []
    acc = 0
    for ln in lines:
        starts.append(acc)
        acc += len(ln) + 1
    return starts


def _byte_col_to_char(line: str, byte_col: int) -> int:
    """ast 的 col_offset 是 UTF-8 字节偏移；CJK 路径必须换算成字符下标。"""
    if byte_col <= 0:
        return 0
    raw = line.encode("utf-8")
    if len(raw) == len(line):
        return byte_col
    return len(raw[:byte_col].decode("utf-8", errors="replace"))


def _in_ranges(lineno: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= lineno <= b for a, b in ranges)


def _quoted(value: Any) -> str:
    s = str(value)
    if "'" in s:
        return '"' + s.replace('"', '\\"') + '"'
    return "'" + s + "'"


def _looks_like_path(s: str) -> bool:
    if not s or len(s) > 4096 or "\n" in s or "\r" in s:
        return False
    return "/" in s or "\\" in s or ":" in s


def _dir_from_node(node: Optional[ast.AST]) -> Optional[Path]:
    """``IMG_DIR = X`` 里 X 若是目录字面量则取出该目录，否则 None。"""
    raw: Optional[str] = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        raw = node.value
    elif isinstance(node, ast.Call) and len(node.args) == 1:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            raw = arg.value
    if not raw:
        return None
    p = Path(raw)
    try:
        return p if p.is_dir() else None
    except OSError:
        return None


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + new + text[end:]
    return text


def _set_img_dir(code: str, stem: str) -> str:
    """把 IMG_DIR 赋值指向 IMG_PATH / <stem>，并保证 IMG_PATH 已 import。"""
    from backend.script_generator.collaborator.collab_form import (
        _has_symbol_import,
        _insert_after_imports,
    )

    if not _has_symbol_import(code, "core.path", "IMG_PATH"):
        code = _insert_after_imports(code, [_IMG_DIR_IMPORT_LINE])

    # 只认第 0 列的顶层赋值：函数内的局部 IMG_DIR 不动，免得改变原意
    assign = re.compile(r"^IMG_DIR[ \t]*=")
    target = f"IMG_DIR = IMG_PATH / {_quoted(stem)}"
    out: list[str] = []
    placed = False
    for line in code.split("\n"):
        if assign.match(line):
            if not placed:
                out.append(target)
                placed = True
            continue
        out.append(line)
    code = "\n".join(out)
    if placed:
        return code
    return _insert_after_imports(code, [target])


def bundle_script_assets(
    code: str,
    *,
    session: Any = None,
    script_stem: str = "",
    img_root: Optional[Path] = None,
    dry_run: bool = False,
) -> tuple[str, BundleResult]:
    """把脚本用到的素材搬进 ``<img_root>/<脚本名>/`` 并把路径改成 ``IMG_DIR / 文件名``。

    返回 ``(新代码, BundleResult)``。任何一步失败都只记录在 results.errors，
    不改动已经能写盘的东西（保存流程不应因为打包失败而中断）。

    ``dry_run=True``：只做同一套改写判定、**不碰文件系统**（设计 §29：不得为了校验
    写用户文件）。它产出的文本与真实打包（全部拷贝成功时）逐字节相同。
    """
    from backend.script_generator.collaborator.codegen_bridge import (
        _safe_stem,
        _same_file,
    )

    text = code or ""
    stem = _safe_stem(script_stem or "script")
    out_dir = Path(img_root) / stem if img_root else IMG_PATH / stem
    res = BundleResult(out_dir=out_dir, dry_run=bool(dry_run))
    if not text.strip():
        return text, res
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        res.errors.append(f"语法错误，未打包素材：{e}")
        return text, res

    lines = text.split("\n")
    starts = _line_starts(lines)

    # IMG_DIR 赋值本身不是素材，不能被当成路径字面量改写掉
    img_assigns: list[ast.Assign] = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "IMG_DIR" for t in n.targets)
    ]
    img_assign_ranges = [(n.lineno, n.end_lineno or n.lineno) for n in img_assigns]
    cur_img_dir = (
        _dir_from_node(max(img_assigns, key=lambda n: n.lineno).value)
        if img_assigns
        else None
    )

    # f-string 里的片段不是完整表达式，替换会写出 f"IMG_DIR / 'x'" 这种坏代码
    fstring_parts = {
        id(c)
        for n in ast.walk(tree)
        if isinstance(n, ast.JoinedStr)
        for c in n.values
    }

    plans: list[_Plan] = []
    by_key: dict[str, _Plan] = {}
    taken: dict[str, str] = {}
    # 同大小的候选才比内容，避免为几十张图两两读盘
    by_size: dict[int, list[_Plan]] = {}

    def _plan_for(src: Path) -> Optional[_Plan]:
        try:
            key = str(src.resolve()).lower()
            size = src.stat().st_size
        except OSError:
            return None
        if key in by_key:
            return by_key[key]
        for cand in by_size.get(size, ()):
            if _same_file(cand.src, src):
                by_key[key] = cand
                return cand
        name = src.name
        n = 2
        while taken.get(name.lower(), key) != key:
            name = f"{src.stem}_{n}{src.suffix}"
            n += 1
        plan = _Plan(src=src, name=name)
        taken[name.lower()] = key
        plans.append(plan)
        by_key[key] = plan
        by_size.setdefault(size, []).append(plan)
        return plan

    # 1) 字面量里的图片路径
    literal_spans: list[tuple[int, int, _Plan]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in fstring_parts or _in_ranges(node.lineno, img_assign_ranges):
            continue
        raw = node.value.strip()
        if not _looks_like_path(raw):
            continue
        src = Path(raw)
        try:
            if not src.is_file() or src.suffix.lower() not in _IMG_EXTS:
                continue
        except OSError:
            continue
        plan = _plan_for(src)
        if plan is None:
            continue
        start = starts[node.lineno - 1] + _byte_col_to_char(
            lines[node.lineno - 1], node.col_offset
        )
        end = starts[(node.end_lineno or node.lineno) - 1] + _byte_col_to_char(
            lines[(node.end_lineno or node.lineno) - 1], node.end_col_offset
        )
        literal_spans.append((start, end, plan))

    # 2) IMG_DIR / "x.png"（文件在旧 IMG_DIR 里，名字沿用）
    img_dir_refs: list[tuple[int, int, str, _Plan]] = []
    if cur_img_dir is not None:
        for m in _IMG_DIR_REF_RE.finditer(text):
            name = m.group("name") or m.group("name2") or ""
            if not name:
                continue
            src = cur_img_dir / name
            try:
                if not src.is_file() or src.suffix.lower() not in _IMG_EXTS:
                    continue
            except OSError:
                continue
            plan = _plan_for(src)
            if plan is not None:
                img_dir_refs.append((m.start(), m.end(), name, plan))

    # 3) [[asset_id]] / [[素材名]] 占位符
    from backend.script_generator.collaborator.canvas import find_asset
    from backend.script_generator.collaborator.collab_form import _ASSET_REF_RE

    assets = [
        a
        for a in (getattr(session, "assets", None) or [])
        if isinstance(a, dict)
    ]
    placeholder_spans: list[tuple[int, int, _Plan]] = []
    for m in _ASSET_REF_RE.finditer(text):
        ref = (m.group(2) or "").strip()
        hit = find_asset(assets, asset_id=ref) or find_asset(assets, name=ref)
        src = Path(str((hit or {}).get("path") or ""))
        try:
            found = bool(ref) and src.is_file() and src.suffix.lower() in _IMG_EXTS
        except OSError:
            found = False
        if not found:
            if ref and ref not in res.unresolved:
                res.unresolved.append(ref)
            continue
        plan = _plan_for(src)
        if plan is not None:
            placeholder_spans.append((m.start(), m.end(), plan))

    if not plans:
        return text, res

    # 拷贝（同内容复用，不同内容不覆盖）；dry_run 只判定可改写性，不碰磁盘。
    if not dry_run:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001 - 打包失败不该挡住保存
            res.errors.append(f"建目录失败：{e}")
            return text, res
    for plan in plans:
        if dry_run:
            plan.ok = True
            continue
        dst = out_dir / plan.name
        try:
            if not _same_file(dst, plan.src):
                shutil.copy2(plan.src, dst)
            plan.ok = True
            res.copied.append(
                {"name": plan.name, "src": str(plan.src), "dst": str(dst)}
            )
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{plan.name}: {e}")

    # 只改拷贝成功那些引用
    edits: list[tuple[int, int, str]] = []
    for start, end, plan in literal_spans + placeholder_spans:
        if plan.ok:
            edits.append((start, end, f"IMG_DIR / {_quoted(plan.name)}"))
    for start, end, name, plan in img_dir_refs:
        if plan.ok and plan.name != name:
            edits.append((start, end, f"IMG_DIR / {_quoted(plan.name)}"))
    new_text = _apply_edits(text, edits) if edits else text
    res.rewrites = len(edits)

    if any(p.ok for p in plans):
        new_text = _set_img_dir(new_text, stem)
    return new_text, res
