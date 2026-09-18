"""Collaborator 会话状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from backend.script_generator.logic_graph import LogicGraph
from backend.script_generator.collaborator.phases import CollabPhase


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_session_id(goal: str = "") -> str:
    import re

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (goal or "chat").strip())
    s = re.sub(r"_+", "_", s).strip("_")[:20] or "chat"
    return f"{ts}_{s}"


@dataclass
class CollabSession:
    phase: CollabPhase = CollabPhase.IDLE
    goal_text: str = ""
    skill_id: str | None = None
    frame_path: Path | None = None
    logic: LogicGraph = field(default_factory=LogicGraph)
    probe: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    # 共同视口：素材区 + 标注层 + 焦点（最多 2）
    assets: list[dict[str, Any]] = field(default_factory=list)
    overlays: list[dict[str, Any]] = field(default_factory=list)
    focused_asset_ids: list[str] = field(default_factory=list)
    clarify_count: int = 0
    max_clarify: int = 3
    generated_code: str = ""
    intro_text: str = ""
    script_name: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 恢复 UI 用（不进 LLM 上下文）
    last_chips: list[str] = field(default_factory=list)
    last_cta_text: str = "看起来对，生成脚本"
    last_cta_enabled: bool = False
    last_trial_ready: bool = False
    last_trial_hint: str = ""
    last_shape_label: str = ""
    last_preview_path: str = ""
    last_preview_caption: str = ""

    def ensure_id(self) -> str:
        if not self.session_id:
            self.session_id = new_session_id(self.goal_text)
        if not self.created_at:
            self.created_at = _now()
        return self.session_id

    def touch(self) -> None:
        self.updated_at = _now()

    def add_message(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        role = (role or "agent").strip() or "agent"
        if self.messages:
            last = self.messages[-1]
            if (
                isinstance(last, dict)
                and str(last.get("role") or "") == role
                and str(last.get("text") or "").strip() == text
            ):
                return
        self.messages.append({"role": role, "text": text})
        self.touch()

    def reset(self) -> None:
        fresh = CollabSession()
        fresh.ensure_id()
        self.__dict__.update(fresh.__dict__)

    def to_dict(self) -> dict[str, Any]:
        from backend.script_generator.collaborator.canvas import (
            normalize_asset,
            normalize_overlay,
        )

        self.ensure_id()
        assets = []
        for a in self.assets or []:
            na = normalize_asset(a if isinstance(a, dict) else None)
            if na:
                assets.append(na)
        overlays = []
        for o in self.overlays or []:
            no = normalize_overlay(o if isinstance(o, dict) else None)
            if no:
                overlays.append(no)
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at or _now(),
            "phase": self.phase.value if isinstance(self.phase, CollabPhase) else str(self.phase),
            "goal_text": self.goal_text,
            "skill_id": self.skill_id,
            "frame_path": str(self.frame_path) if self.frame_path else "",
            "logic": self.logic.to_dict(),
            "probe": _slim_probe(self.probe),
            "artifacts": dict(self.artifacts or {}),
            "assets": assets,
            "overlays": overlays,
            "focused_asset_ids": [str(x) for x in (self.focused_asset_ids or [])][:2],
            "clarify_count": self.clarify_count,
            "max_clarify": self.max_clarify,
            "generated_code": self.generated_code,
            "intro_text": self.intro_text,
            "script_name": self.script_name,
            "messages": list(self.messages),
            "last_chips": list(self.last_chips),
            "last_cta_text": self.last_cta_text,
            "last_cta_enabled": self.last_cta_enabled,
            "last_trial_ready": self.last_trial_ready,
            "last_trial_hint": self.last_trial_hint,
            "last_shape_label": self.last_shape_label,
            "last_preview_path": self.last_preview_path,
            "last_preview_caption": self.last_preview_caption,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CollabSession":
        from backend.script_generator.collaborator.canvas import (
            normalize_asset,
            normalize_overlay,
        )

        phase_raw = str(data.get("phase") or "idle")
        try:
            phase = CollabPhase(phase_raw)
        except ValueError:
            phase = CollabPhase.IDLE
        fp = str(data.get("frame_path") or "").strip()
        assets = []
        for a in data.get("assets") or []:
            na = normalize_asset(a if isinstance(a, dict) else None)
            if na:
                assets.append(na)
        overlays = []
        for o in data.get("overlays") or []:
            no = normalize_overlay(o if isinstance(o, dict) else None)
            if no:
                overlays.append(no)
        sess = cls(
            phase=phase,
            goal_text=str(data.get("goal_text") or ""),
            skill_id=str(data.get("skill_id") or "") or None,
            frame_path=Path(fp) if fp else None,
            logic=LogicGraph.from_dict(data.get("logic") if isinstance(data.get("logic"), dict) else None),
            probe=dict(data.get("probe") or {}) if isinstance(data.get("probe"), dict) else {},
            artifacts=dict(data.get("artifacts") or {}) if isinstance(data.get("artifacts"), dict) else {},
            assets=assets,
            overlays=overlays,
            focused_asset_ids=[str(x) for x in (data.get("focused_asset_ids") or [])][:2],
            clarify_count=int(data.get("clarify_count") or 0),
            max_clarify=int(data.get("max_clarify") or 3),
            generated_code=str(data.get("generated_code") or ""),
            intro_text=str(data.get("intro_text") or ""),
            script_name=str(data.get("script_name") or ""),
            messages=[
                {"role": str(m.get("role") or ""), "text": str(m.get("text") or "")}
                for m in (data.get("messages") or [])
                if isinstance(m, dict)
            ],
            session_id=str(data.get("session_id") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_chips=[str(x) for x in (data.get("last_chips") or [])],
            last_cta_text=str(data.get("last_cta_text") or "看起来对，生成脚本"),
            last_cta_enabled=bool(data.get("last_cta_enabled")),
            last_trial_ready=bool(data.get("last_trial_ready")),
            last_trial_hint=str(data.get("last_trial_hint") or ""),
            last_shape_label=str(data.get("last_shape_label") or ""),
            last_preview_path=str(data.get("last_preview_path") or ""),
            last_preview_caption=str(data.get("last_preview_caption") or ""),
        )
        sess.ensure_id()
        return sess


def _slim_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """去掉过大字段，保留恢复 UI 所需摘要。"""
    if not isinstance(probe, dict):
        return {}
    keep = {
        k: probe[k]
        for k in (
            "ok",
            "tool",
            "rows",
            "cols",
            "counts",
            "hits",
            "scale",
            "threshold",
            "best",
            "debug_path",
            "summary",
        )
        if k in probe
    }
    return keep
