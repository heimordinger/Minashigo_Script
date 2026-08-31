"""辅助识图结果磁盘缓存（按图片内容 hash + 识图模型 + prompt 版本）。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Optional

VISION_PROMPT_VERSION = "1"
CACHE_DIR_NAME = ".vision_cache"
INDEX_NAME = "index.json"
CATALOG_TXT_NAME = "识图目录.txt"

_FILE_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g|webp))",
    re.I,
)
_FILE_LINE_START = re.compile(
    r"^[\s\-*•]*(?P<name>[A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g|webp))\s*[：:]\s*(?P<rest>.+)$",
    re.I,
)
_ID_LINE_RE = re.compile(r"标识图|作为.{0,8}标识|场景标识")
_BTN_LINE_RE = re.compile(r"按钮|点击")
_USEFUL_SECTION_MARKERS = (
    "场景标识",
    "图片说明",
    "特殊规则",
    "目标",
    "辅助步骤",
)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _cache_key(sha256: str, provider: str, model: str) -> str:
    return f"{sha256}|{provider}|{model}|{VISION_PROMPT_VERSION}"


class VisionCache:
    """素材目录旁 `.vision_cache/index.json`。"""

    def __init__(self, cache_root: Path):
        self.root = cache_root
        self.index_path = cache_root / INDEX_NAME
        self._data: dict = {"prompt_version": VISION_PROMPT_VERSION, "entries": {}}
        self._load()

    @classmethod
    def for_source_dir(cls, source_dir: str | Path | None) -> Optional["VisionCache"]:
        root = Path(source_dir or "")
        if not root.is_dir():
            return None
        return cls(root / CACHE_DIR_NAME)

    def _load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            self._data = raw
            self._data.setdefault("entries", {})

    def get(self, path: Path, provider: str, model: str) -> Optional[str]:
        try:
            sha = file_sha256(path)
        except OSError:
            return None
        key = _cache_key(sha, provider, model)
        entry = (self._data.get("entries") or {}).get(key)
        if not entry or not isinstance(entry, dict):
            return None
        if entry.get("sha256") != sha:
            return None
        cap = entry.get("caption")
        return cap if isinstance(cap, str) and cap.strip() else None

    def put(
        self,
        path: Path,
        provider: str,
        model: str,
        caption: str,
    ) -> None:
        try:
            sha = file_sha256(path)
        except OSError:
            return
        key = _cache_key(sha, provider, model)
        entries = self._data.setdefault("entries", {})
        entries[key] = {
            "filename": path.name,
            "sha256": sha,
            "provider": provider,
            "model": model,
            "prompt_version": VISION_PROMPT_VERSION,
            "caption": (caption or "").strip(),
        }

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._data["prompt_version"] = VISION_PROMPT_VERSION
        self.index_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_catalog_sha(self, filename: str) -> Optional[str]:
        m = (self._data.get("catalog_sha256") or {}).get(filename)
        return m if isinstance(m, str) else None

    def set_catalog_sha(self, filename: str, sha256: str) -> None:
        self._data.setdefault("catalog_sha256", {})[filename] = sha256

    def file_sha256_safe(self, path: Path) -> Optional[str]:
        try:
            return file_sha256(path)
        except OSError:
            return None


_SECTION_RE = re.compile(
    r"^#{1,3}\s*(?P<name>[^\s#]+\.(?:png|jpg|jpeg|webp))\s*$",
    re.I | re.M,
)
_BULLET_FILE_RE = re.compile(
    r"^[\-*•]\s*(?P<name>[^\s:]+\.(?:png|jpg|jpeg|webp))\b",
    re.I | re.M,
)


def parse_per_image_captions(text: str, filenames: list[str]) -> dict[str, str]:
    """把一批识图输出拆成「文件名 → 描述」。"""
    out: dict[str, str] = {}
    if not (text or "").strip() or not filenames:
        return out

    name_map = {fn.lower(): fn for fn in filenames}
    body = text.strip()

    # ### rank.png 分段
    sections: list[tuple[str, str]] = []
    matches = list(_SECTION_RE.finditer(body))
    if matches:
        for i, m in enumerate(matches):
            raw_name = m.group("name")
            canon = name_map.get(raw_name.lower(), raw_name)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            chunk = body[start:end].strip()
            if chunk:
                sections.append((canon, chunk))
        for fn, chunk in sections:
            if fn in filenames and fn not in out:
                out[fn] = f"### {fn}\n{chunk}".strip()

    # - rank.png: … 行首 bullet
    for m in _BULLET_FILE_RE.finditer(body):
        raw_name = m.group("name")
        canon = name_map.get(raw_name.lower())
        if not canon or canon in out:
            continue
        start = m.start()
        nxt = _BULLET_FILE_RE.search(body, m.end())
        end = nxt.start() if nxt else len(body)
        out[canon] = body[start:end].strip()

    # 单行内嵌文件名
    for fn in filenames:
        if fn in out:
            continue
        pat = re.compile(
            rf"(^|\n)([\-*•].*{re.escape(fn)}.*(?:\n(?![\-*•#]).*)*)",
            re.I | re.M,
        )
        m = pat.search(body)
        if m:
            out[fn] = m.group(2).strip()

    return out


def format_image_caption(filename: str, caption: str) -> str:
    cap = (caption or "").strip()
    if not cap:
        return f"- {filename}\n  （无描述）"
    if filename.lower() in cap.lower():
        return cap
    return f"### {filename}\n{cap}"


def is_sufficient_explanation_caption(caption: str) -> bool:
    """介绍里对该图的说明是否足够跳过识图 API。"""
    text = (caption or "").strip()
    if len(text) < 12:
        return False
    if _ID_LINE_RE.search(text) or _BTN_LINE_RE.search(text):
        return True
    if any(k in text for k in ("标识", "按钮", "点击", "界面", "偏移")):
        return True
    return len(text) >= 24


def extract_explanation_captions(explanation: str) -> dict[str, str]:
    """从脚本介绍提取 filename.lower() → 说明文本。"""
    out: dict[str, str] = {}
    expl = explanation or ""

    for m in _FILE_LINE_START.finditer(expl):
        name = m.group("name")
        rest = m.group("rest").strip()
        key = name.lower()
        prev = out.get(key, "")
        if len(rest) > len(prev):
            out[key] = rest

    for line in expl.splitlines():
        files = _FILE_IN_TEXT.findall(line)
        if not files:
            continue
        snippet = line.strip()
        if not snippet:
            continue
        useful = (
            _ID_LINE_RE.search(line)
            or _BTN_LINE_RE.search(line)
            or "：" in line
            or ":" in line
        )
        if not useful:
            continue
        for fname in files:
            key = fname.lower()
            if key in out and len(out[key]) >= len(snippet):
                continue
            if is_sufficient_explanation_caption(snippet):
                out[key] = snippet

    return out


def vision_explanation_excerpt(explanation: str, max_chars: int = 2800) -> str:
    """识图 API 用的介绍摘录（场景标识 / 图片说明等）。"""
    expl = (explanation or "").strip()
    if not expl:
        return ""

    blocks: list[str] = []
    current: list[str] = []
    active = False

    for line in expl.splitlines():
        stripped = line.strip()
        if any(m in line for m in _USEFUL_SECTION_MARKERS):
            if current:
                blocks.append("\n".join(current))
            current = [line.rstrip()]
            active = True
            continue
        if active:
            if stripped.startswith("（") and "任务流程" in stripped:
                active = False
                if current:
                    blocks.append("\n".join(current))
                    current = []
                continue
            if stripped.startswith("## "):
                active = False
                if current:
                    blocks.append("\n".join(current))
                    current = []
                continue
            current.append(line.rstrip())

    if current:
        blocks.append("\n".join(current))

    if not blocks:
        png_lines = [ln.rstrip() for ln in expl.splitlines() if _FILE_IN_TEXT.search(ln)]
        text = "\n".join(png_lines[:40])
    else:
        text = "\n\n".join(blocks)

    text = text.strip()
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def load_catalog_txt(source_dir: str | Path | None) -> dict[str, str]:
    """读取素材目录旁 ``识图目录.txt``。"""
    root = Path(source_dir or "")
    path = root / CATALOG_TXT_NAME
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    names = _FILE_IN_TEXT.findall(text)
    if not names:
        return {}
    unique = list(dict.fromkeys(names))
    return parse_per_image_captions(text, unique)


def write_catalog_txt(
    source_dir: str | Path,
    per_file: dict[str, str],
    paths: list[Path],
) -> None:
    """合并写回 ``识图目录.txt``（可手改）。"""
    root = Path(source_dir)
    if not root.is_dir():
        return
    lines = [
        "# 识图目录（辅助识图自动生成，可手改；图片文件变更后会重新识图）",
        f"# prompt_version={VISION_PROMPT_VERSION}",
        "",
    ]
    for p in paths:
        cap = per_file.get(p.name, "")
        lines.append(format_image_caption(p.name, cap))
        lines.append("")
    out_path = root / CATALOG_TXT_NAME
    out_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def clear_vision_cache(
    source_dir: str | Path | None,
    *,
    include_catalog_txt: bool = True,
) -> list[str]:
    """清除素材目录旁的识图缓存（``.vision_cache`` / ``识图目录.txt``）。"""
    root = Path(source_dir or "")
    if not root.is_dir():
        return []
    notes: list[str] = []
    cache_dir = root / CACHE_DIR_NAME
    if cache_dir.is_dir():
        shutil.rmtree(cache_dir)
        notes.append(CACHE_DIR_NAME)
    if include_catalog_txt:
        cat = root / CATALOG_TXT_NAME
        if cat.is_file():
            cat.unlink()
            notes.append(CATALOG_TXT_NAME)
    return notes


def build_vision_user_context(
    explanation: str,
    chunk_names: list[str],
    expl_captions: dict[str, str],
) -> str:
    """拼进识图 user 消息的介绍上下文。"""
    parts: list[str] = []
    excerpt = vision_explanation_excerpt(explanation)
    if excerpt:
        parts.append(
            "## 脚本介绍摘录（描述须与下列一致，勿矛盾）\n" + excerpt
        )
    hints: list[str] = []
    for fn in chunk_names:
        cap = expl_captions.get(fn.lower())
        if cap:
            hints.append(f"- {fn}: {cap}")
    if hints:
        parts.append("## 介绍中已有说明（可沿用措辞）\n" + "\n".join(hints))
    return "\n\n".join(parts)

