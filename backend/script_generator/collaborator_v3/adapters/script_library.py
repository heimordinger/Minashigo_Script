"""项目成品脚本库：让模型能找到并读一段项目里已有的脚本。

协作/V3 侧原本**没有任何**查找成品脚本的入口——`few_shot` 那套只在经典生成
管线里用（`agent.py` / `nodes.py` / `optimize.py`），模型在会话里问「项目里做过
类似的东西吗」只能得到空。

两个来源合一：

1. **corpus 模板**（`corpus/index.json` 的 ``templates``）：人工标注过的成品脚本，
   带 ``when_to_use`` / ``copy`` / ``do_not_copy`` / ``states`` / ``images`` / ``keywords``。
   设计文档 §21.3 第 5 条要的「查找对应脚本、验证适用范围、建立参考关系而非盲目复制」
   靠的就是这份标注。
2. **脚本树**（``scripts/**/*.py``）：其余成品脚本，从文件本身派生元信息
   （首行说明、行数、目录、修改时间）。

只读、不写。内容按需取，避免一次把几千行灌进上下文。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.path import PROJECT_ROOT

SCRIPTS_ROOT = PROJECT_ROOT / "scripts"

#: 脚本树里不索引的目录（试跑临时产物、备份、协作草稿、测试/分析脚本）
SKIP_DIRS = {"__pycache__", "_trial", "_collab", "_backup", ".history", "测试"}
#: 以这些前缀开头的文件是一次性分析/探针脚本，不是成品
SKIP_PREFIXES = ("_",)
#: `script.search` 的默认条数（脚本列表条目轻，但也不该一次给几十条）
DEFAULT_SEARCH_LIMIT = 10


@dataclass(frozen=True)
class ScriptEntry:
    id: str
    kind: str  # "template" | "script"
    title: str
    path: str  # 相对 PROJECT_ROOT
    summary: str = ""
    tags: tuple[str, ...] = ()
    folder: str = ""
    lines: int = 0
    bytes: int = 0
    updated_at: str = ""
    #: 仅模板有：适用范围与复制建议
    when_to_use: str = ""
    copy_hints: tuple[str, ...] = ()
    do_not_copy: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    explanation: str = ""

    def searchable(self) -> str:
        return " ".join(
            [
                self.id,
                self.title,
                self.summary,
                self.when_to_use,
                self.folder,
                " ".join(self.tags),
                " ".join(self.keywords),
                " ".join(self.states),
                " ".join(self.images),
            ]
        ).casefold()


@dataclass
class _Cache:
    fingerprint: tuple = ()
    entries: list[ScriptEntry] = field(default_factory=list)


_CACHE = _Cache()


def _first_sentence(path: Path, limit: int = 200) -> tuple[str, int, int]:
    """取首个有意义的行当说明，并返回 (说明, 行数, 字节数)。

    优先模块 docstring，其次第一个非 import 的注释/代码行。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", 0, 0
    lines = text.splitlines()
    size = len(text.encode("utf-8", errors="replace"))

    head: list[str] = []
    in_doc = False
    for line in lines[:60]:
        stripped = line.strip()
        if not stripped:
            if in_doc:
                break
            continue
        if stripped.startswith(('"""', "'''")):
            body = stripped.strip("\"'").strip()
            if body:
                head.append(body)
                break
            in_doc = True
            continue
        if in_doc:
            if stripped.endswith(('"""', "'''")):
                body = stripped.rstrip("\"'").strip()
                if body:
                    head.append(body)
                break
            head.append(stripped)
            continue
        if stripped.startswith("#"):
            head.append(stripped.lstrip("# ").strip())
            break
        # 跳过 import / 编码声明 / from __future__
        if stripped.startswith(("import ", "from ", "#!", "# -*-", "# coding")):
            continue
        head.append(stripped)
        break
    summary = " ".join(x for x in head if x).strip()
    # 脚本习惯用 ===== 画下划线，别让它糊进标题
    summary = re.sub(r"[=~\-*_]{3,}", " ", summary)
    summary = re.sub(r"\s+", " ", summary).strip()
    return summary[:limit], len(lines), size


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def should_index(path: Path) -> bool:
    """这个文件算不算"成品脚本"。"""
    if set(path.parts) & SKIP_DIRS:
        return False
    if path.suffix.lower() != ".py":
        return False
    return not any(path.name.startswith(prefix) for prefix in SKIP_PREFIXES)


def _tree_fingerprint() -> tuple[tuple[str, int, int], ...]:
    """脚本树的目录签名：``(相对路径, mtime_ns, size)``。

    只 stat、不读内容，所以很便宜（33 个文件不到 1 ms）。用它判断索引要不要重建，
    比"固定 TTL"准得多——新增/删除/改名/改内容都能立刻反映，不用等 30 秒。
    """
    if not SCRIPTS_ROOT.is_dir():
        return ()
    rows: list[tuple[str, int, int]] = []
    for path in SCRIPTS_ROOT.rglob("*.py"):
        if not should_index(path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((_relative(path), stat.st_mtime_ns, stat.st_size))
    rows.sort()
    return tuple(rows)


def _tree_entries() -> list[ScriptEntry]:
    if not SCRIPTS_ROOT.is_dir():
        return []
    out: list[ScriptEntry] = []
    for path in sorted(SCRIPTS_ROOT.rglob("*.py")):
        if not should_index(path):
            continue
        rel = _relative(path)
        folder = str(path.parent.relative_to(SCRIPTS_ROOT)).replace("\\", "/")
        if folder == ".":
            folder = ""
        summary, lines, size = _first_sentence(path)
        try:
            mtime = time.strftime(
                "%Y-%m-%d", time.localtime(path.stat().st_mtime)
            )
        except OSError:
            mtime = ""
        stem = path.stem
        entry_id = f"{folder}/{stem}" if folder else stem
        # 目录名本身就是最有用的标签（Deep One / yys / 孤儿）
        tags = tuple(x for x in {folder, path.parent.name} if x)
        out.append(
            ScriptEntry(
                id=entry_id,
                kind="script",
                title=summary or stem,
                path=rel,
                summary=summary,
                tags=tags,
                folder=folder,
                lines=lines,
                bytes=size,
                updated_at=mtime,
            )
        )
    return out


def _template_entries() -> list[ScriptEntry]:
    try:
        from backend.script_generator.few_shot import load_index

        index = load_index()
    except Exception:
        return []
    out: list[ScriptEntry] = []
    for item in index.get("templates") or []:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("id") or "").strip()
        if not entry_id:
            continue
        rel = str(item.get("script") or "").strip()
        path = (PROJECT_ROOT / rel) if rel else None
        lines = size = 0
        if path is not None and path.is_file():
            _, lines, size = _first_sentence(path)
        out.append(
            ScriptEntry(
                id=entry_id,
                kind="template",
                title=str(item.get("title") or entry_id),
                path=rel,
                summary=str(item.get("summary") or ""),
                tags=tuple(str(x) for x in (item.get("tags") or [])),
                folder=str(item.get("source_dir") or ""),
                lines=lines,
                bytes=size,
                updated_at="",
                when_to_use=str(item.get("when_to_use") or ""),
                copy_hints=tuple(str(x) for x in (item.get("copy") or [])),
                do_not_copy=tuple(str(x) for x in (item.get("do_not_copy") or [])),
                states=tuple(str(x) for x in (item.get("states") or [])),
                images=tuple(str(x) for x in (item.get("images") or [])),
                keywords=tuple(str(x) for x in (item.get("keywords") or [])),
                explanation=str(item.get("explanation") or ""),
            )
        )
    return out


def load_entries(*, refresh: bool = False) -> list[ScriptEntry]:
    """全部条目（模板在前，脚本树在后）。

    **近实时**：模板每次都从 ``corpus/index.json`` 真读盘；脚本树按目录签名
    （路径 + mtime + 大小）判断是否重建，新增/删除/改内容都立刻反映，不设固定 TTL。
    重建一次全量约 65 ms，相对一次模型往返（3~4 s）可以忽略。

    模板已经覆盖的那份脚本，不再从脚本树里重复列出——同一份 ``DO登录_v2.py``
    出现在两条结果里只会让模型以为有两个脚本。
    """
    fingerprint = _tree_fingerprint()
    if not refresh and _CACHE.entries and fingerprint == _CACHE.fingerprint:
        return list(_CACHE.entries)
    templates = _template_entries()
    annotated_paths = {entry.path for entry in templates if entry.path}
    tree = [entry for entry in _tree_entries() if entry.path not in annotated_paths]
    entries = templates + tree
    _CACHE.entries = entries
    _CACHE.fingerprint = fingerprint
    return list(entries)


def _entry_from_disk(rel: str) -> ScriptEntry | None:
    """索引里还没有、但磁盘上确实存在的脚本：现场造一条。

    否则会出现「刚建的脚本就在 scripts/ 下，模型知道路径却读不到」。
    """
    candidate = (PROJECT_ROOT / rel).resolve()
    try:
        scripts_root = SCRIPTS_ROOT.resolve()
        candidate.relative_to(scripts_root)
    except (ValueError, OSError):
        return None
    if not candidate.is_file() or not should_index(candidate):
        return None
    summary, lines, size = _first_sentence(candidate)
    folder = str(candidate.parent.relative_to(SCRIPTS_ROOT)).replace("\\", "/")
    if folder == ".":
        folder = ""
    rel_posix = _relative(candidate)
    try:
        mtime = time.strftime("%Y-%m-%d", time.localtime(candidate.stat().st_mtime))
    except OSError:
        mtime = ""
    return ScriptEntry(
        id=f"{folder}/{candidate.stem}" if folder else candidate.stem,
        kind="script",
        title=summary or candidate.stem,
        path=rel_posix,
        summary=summary,
        tags=tuple(x for x in {folder, candidate.parent.name} if x),
        folder=folder,
        lines=lines,
        bytes=size,
        updated_at=mtime,
    )


def find_entry(entry_id: str, *, refresh: bool = False) -> ScriptEntry | None:
    target = str(entry_id or "").strip()
    if not target:
        return None
    normalized = target.replace("\\", "/").removesuffix(".py")
    entries = load_entries(refresh=refresh)
    for entry in entries:
        if entry.id == normalized or entry.id == target:
            return entry
    # 全路径、文件名，或"模板已覆盖所以脚本树里没有"的路径都能定位到
    for entry in entries:
        path = entry.path.removesuffix(".py")
        if entry.path == target or path == normalized:
            return entry
        if Path(path).name == normalized or path.endswith("/" + normalized):
            return entry
    # 索引里没有：如果磁盘上确实存在，现场造一条（新建的脚本立刻可用）
    for rel in (target, f"{target}.py"):
        fresh = _entry_from_disk(rel)
        if fresh is not None:
            return fresh
    return None


def search_entries(
    *,
    query: str = "",
    kind: str = "",
    folder: str = "",
    tags: str = "",
    limit: int = 0,
) -> tuple[list[ScriptEntry], int]:
    """按关键词/类型/目录/标签筛。返回 (命中, 总数)。"""
    entries = load_entries()
    term = str(query or "").strip().casefold()
    kind_term = str(kind or "").strip().lower()
    folder_term = str(folder or "").strip().casefold()
    tag_terms = [x.strip().casefold() for x in str(tags or "").split(",") if x.strip()]
    cap = int(limit) if int(limit or 0) > 0 else DEFAULT_SEARCH_LIMIT

    def _keep(entry: ScriptEntry) -> bool:
        if kind_term and entry.kind != kind_term:
            return False
        if folder_term and folder_term not in entry.folder.casefold():
            return False
        if tag_terms:
            own = {t.casefold() for t in entry.tags}
            if not all(any(term in t for t in own) for term in tag_terms):
                return False
        if term and term not in entry.searchable():
            return False
        return True

    matched = [entry for entry in entries if _keep(entry)]
    # 模板（人工标注、明确可参考）排在脚本树前面
    matched.sort(key=lambda e: (0 if e.kind == "template" else 1, -e.lines, e.id))
    return matched[:cap], len(matched)


def read_source(entry: ScriptEntry, *, max_lines: int = 0) -> tuple[str, bool, int]:
    """读脚本正文。返回 (内容, 是否被行数截断, 总行数)。

    每次真读文件：索引可能比磁盘慢一拍，正文不能跟着旧。
    """
    path = (PROJECT_ROOT / entry.path) if entry.path else None
    if path is None or not path.is_file():
        return "", False, 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False, 0
    lines = text.splitlines()
    total = len(lines)
    if max_lines and 0 < max_lines < total:
        return "\n".join(lines[:max_lines]), True, total
    return text, False, total


def read_explanation(entry: ScriptEntry, *, max_chars: int = 6000) -> str:
    if not entry.explanation:
        return ""
    path = PROJECT_ROOT / entry.explanation
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_chars]


def to_summary(entry: ScriptEntry) -> dict[str, Any]:
    """列表用的摘要：给"选哪个"足够的信息，不含正文。"""
    out: dict[str, Any] = {
        "id": entry.id,
        "kind": entry.kind,
        "title": entry.title[:120],
        "path": entry.path,
        "lines": entry.lines,
    }
    if entry.summary:
        out["summary"] = entry.summary[:200]
    if entry.kind == "template":
        out["when_to_use"] = entry.when_to_use[:300]
        out["tags"] = list(entry.tags)
        if entry.states:
            out["states"] = list(entry.states)
    elif entry.tags:
        out["tags"] = list(entry.tags)
    if entry.updated_at:
        out["updated_at"] = entry.updated_at
    return out


def to_detail(entry: ScriptEntry, *, max_lines: int = 0, with_source: bool = True) -> dict[str, Any]:
    """单条详请：模板给完整标注，两者都可附正文。"""
    out = to_summary(entry)
    out["id"] = entry.id
    out["bytes"] = entry.bytes
    if entry.kind == "template":
        out["copy"] = list(entry.copy_hints)
        out["do_not_copy"] = list(entry.do_not_copy)
        if entry.images:
            out["images"] = list(entry.images)
        if entry.keywords:
            out["keywords"] = list(entry.keywords)
        explanation = read_explanation(entry)
        if explanation:
            out["explanation_text"] = explanation
    if with_source:
        source, truncated, total = read_source(entry, max_lines=max_lines)
        out["source"] = source
        out["total_lines"] = total
        if truncated:
            out["source_truncated"] = True
            out["note"] = f"只给了前 {max_lines} 行；全文 {total} 行，需要更多就用更大的 max_lines"
    return out
