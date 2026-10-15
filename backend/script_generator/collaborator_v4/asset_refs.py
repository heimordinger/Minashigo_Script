"""Compact asset directory and typo-tolerant reference candidates for V4."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


_CONFUSABLES = str.maketrans({"l": "1", "i": "1", "Ⅰ": "1", "一": "1"})


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.translate(_CONFUSABLES)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def compact_asset_index(session, *, limit: int = 60) -> list[dict[str, Any]]:
    """Return names and routing metadata, never paths or image contents."""
    from backend.script_generator.collaborator.resource_registry import active_session_assets

    focused = {str(x) for x in (getattr(session, "focused_asset_ids", None) or [])}
    assets = active_session_assets(session)[-max(1, int(limit)) :]
    out: list[dict[str, Any]] = []
    for offset, item in enumerate(assets):
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not asset_id or not name:
            continue
        path = str(item.get("path") or item.get("source") or "")
        aliases = [str(x).strip() for x in (item.get("aliases") or []) if str(x).strip()][:5]
        out.append(
            {
                "asset_id": asset_id,
                "name": name,
                "kind": str(item.get("kind") or Path(path).suffix.lstrip(".") or "asset"),
                "role": str(item.get("role") or ""),
                "group": str(item.get("group") or ""),
                "aliases": aliases,
                "focused": asset_id in focused,
                "recent": offset >= max(0, len(assets) - 5),
            }
        )
    return out


def _best_similarity(needle: str, text: str) -> float:
    if not needle or not text:
        return 0.0
    if needle in text:
        return 1.0
    lengths = range(max(1, len(needle) - 1), min(len(text), len(needle) + 1) + 1)
    return max(
        (
            SequenceMatcher(None, needle, text[start : start + width]).ratio()
            for width in lengths
            for start in range(0, max(1, len(text) - width + 1))
        ),
        default=0.0,
    )


def resolve_asset_references(user_text: str, asset_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Produce bounded candidates; the LLM still decides each candidate's role."""
    normalized_text = _norm(user_text)
    candidates: list[dict[str, Any]] = []
    for asset in asset_index:
        labels = [asset.get("name"), *(asset.get("aliases") or [])]
        scored = [(_best_similarity(_norm(label), normalized_text), str(label)) for label in labels if _norm(label)]
        if not scored:
            continue
        score, label = max(scored, key=lambda pair: pair[0])
        if score < 0.62:
            continue
        candidates.append(
            {
                "asset_id": asset["asset_id"],
                "asset_name": asset["name"],
                "matched_label": label,
                "confidence": round(score, 3),
                "confidence_level": "high" if score >= 0.84 else "medium",
                "reason": "名称完全匹配" if score == 1.0 else "名称容错近似匹配",
            }
        )
    return sorted(candidates, key=lambda item: (-item["confidence"], item["asset_name"]))[:8]
