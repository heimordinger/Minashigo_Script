"""v2 脚本 ↔ 介绍语义 → FSM 结构契约（生成/修订 prompt 注入）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_MAP_PATH = Path(__file__).parent / "corpus" / "semantic_maps" / "v2_semantic_map.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_map() -> dict[str, Any]:
    if not _MAP_PATH.is_file():
        return {"universal": {}, "patterns": []}
    return json.loads(_MAP_PATH.read_text(encoding="utf-8"))


def _folder_key(source_dir: str) -> str:
    n = str(source_dir or "").replace("\\", "/").rstrip("/").lower()
    if not n:
        return ""
    return n.split("/")[-1]


def _score_pattern(pattern: dict[str, Any], explanation: str, source_dir: str) -> int:
    score = 0
    fk = _folder_key(source_dir)
    for key in pattern.get("folder_keys") or []:
        if key.lower() == fk or key.lower() in fk:
            score += 50
    text = (explanation or "").lower()
    for kw in pattern.get("keywords") or []:
        if kw.lower() in text:
            score += 3
    for tag in pattern.get("tags") or []:
        if tag.lower() in text:
            score += 2
    return score


def select_patterns(
    explanation: str = "",
    source_dir: str = "",
    *,
    max_items: int = 2,
) -> list[dict[str, Any]]:
    data = _load_map()
    patterns = list(data.get("patterns") or [])
    scored = [( _score_pattern(p, explanation, source_dir), p) for p in patterns]
    scored.sort(key=lambda x: (-x[0], x[1].get("id") or ""))
    out: list[dict[str, Any]] = []
    for sc, p in scored:
        if sc <= 0 and out:
            break
        if sc > 0 or not out:
            out.append(p)
        if len(out) >= max_items:
            break
    if not out and patterns:
        out.append(patterns[0])
    return out[:max_items]


def _format_intro_rows(rows: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        intro = (row.get("intro") or "").strip()
        structure = (row.get("structure") or "").strip()
        if intro and structure:
            lines.append(f"| {intro} | {structure} |")
    return lines


def _format_scene_map(rows: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        img = row.get("image") or ""
        state = row.get("state") or ""
        layer = row.get("layer") or ""
        if img and state:
            extra = f" ({layer})" if layer else ""
            lines.append(f"- {img} → STATES/unknown return `{state}`{extra}")
    return lines


def format_pattern_block(pattern: dict[str, Any]) -> str:
    lines = [
        f"### {pattern.get('title') or pattern.get('id')}",
        f"- archetype: `{pattern.get('archetype') or ''}`",
        f"- ref: `{pattern.get('script') or ''}`",
    ]
    if pattern.get("orchestration"):
        lines.append(f"- orchestration: {pattern['orchestration']}")
    if pattern.get("states"):
        st = pattern["states"]
        if isinstance(st, list):
            lines.append("- states: " + " · ".join(str(s) for s in st))
    if pattern.get("timeouts"):
        lines.append(f"- timeouts: {pattern['timeouts']}")

    scene_lines = _format_scene_map(list(pattern.get("scene_map") or []))
    if scene_lines:
        lines.append("- scene_map:")
        lines.extend(scene_lines)

    for row in pattern.get("scene_to_entry") or []:
        lines.append(
            f"- scene_recovery: `{row.get('scene')}` "
            f"({row.get('when') or ''}) → entry `{row.get('entry') or ''}`"
        )
    for row in pattern.get("scene_to_step") or []:
        lines.append(
            f"- SCENE_TO_STEP: task={row.get('task')} "
            f"`{row.get('scene')}` → `{row.get('step')}`"
        )

    intro_rows = _format_intro_rows(list(pattern.get("intro_to_structure") or []))
    if intro_rows:
        lines.append("")
        lines.append("| 介绍语义 | 代码结构 |")
        lines.append("| --- | --- |")
        lines.extend(intro_rows)
    return "\n".join(lines)


def format_universal_block(universal: dict[str, Any]) -> str:
    lines = [f"### {universal.get('title') or 'Universal'}"]
    for rule in universal.get("branching") or []:
        lines.append(f"- [branch] {rule}")
    for rule in universal.get("rules") or []:
        lines.append(f"- {rule}")
    anti = universal.get("anti_patterns") or []
    if anti:
        lines.append("")
        lines.append("**Anti-patterns (v2 归档常见问题):**")
        for a in anti:
            lines.append(f"- {a}")
    return "\n".join(lines)


def build_structure_contract_block(
    explanation: str = "",
    source_dir: str = "",
    *,
    max_patterns: int = 2,
) -> str:
    """生成「介绍语义 → 结构」契约块，供 normal / free mode 注入。"""
    data = _load_map()
    universal = data.get("universal") or {}
    patterns = select_patterns(explanation, source_dir, max_items=max_patterns)

    parts = [
        "## Structure Contract (v2 semantic map)",
        "Map script explanation phrases to FSM architecture. "
        "When explanation conflicts with template, follow explanation + this contract.",
        "",
        format_universal_block(universal),
    ]
    for p in patterns:
        parts.append("")
        parts.append(format_pattern_block(p))

    parts.append("")
    parts.append(
        "_Contract source: production `*_v2.py` scripts paired with player explanations._"
    )
    return "\n".join(parts)


def read_few_shot_semantic_snippet() -> str:
    """10_v2_semantic_structure_map.py 全文（自由模式短范文）。"""
    path = _PROJECT_ROOT / "backend/script_generator/corpus/few_shot/10_v2_semantic_structure_map.py"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    # 去掉文件头注释行，保留代码块
    lines = text.splitlines()
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    return "\n".join(lines).strip()


def build_free_mode_structure_block(
    explanation: str = "",
    source_dir: str = "",
) -> str:
    """自由模式：契约 + 短 few-shot（不注入完整 Rules/多条范文）。"""
    contract = build_structure_contract_block(explanation, source_dir)
    snippet = read_few_shot_semantic_snippet()
    parts = [contract]
    if snippet:
        parts.append("")
        parts.append("## v2 structure snippet (copy patterns, adapt names)")
        parts.append(f"```python\n{snippet}\n```")
    return "\n".join(parts)
