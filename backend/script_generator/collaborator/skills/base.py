"""协作 Skill 协议：玩法能力插件，禁止在 GUI 里硬编码业务流程。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from backend.script_generator.logic_graph import LogicGraph
from backend.script_generator.collaborator.session import CollabSession


@dataclass
class ExploreResult:
    ok: bool
    summary: str
    probe: dict[str, Any] = field(default_factory=dict)
    logic: Optional[LogicGraph] = None
    debug_path: str | None = None
    shape_label: str = ""
    # 有可确认目标时 True；无目标时也可仍允许 ack（文案由 skill 决定）
    can_ack: bool = False
    empty_ok: bool = False  # 允许在「无目标物」时仍生成等待版
    chips: list[str] = field(default_factory=list)
    cta_text: str = "看起来对，生成脚本"


@dataclass
class EmitResult:
    code: str
    intro: str
    script_name: str
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class CollabSkill(Protocol):
    id: str
    title: str

    def match(self, text: str) -> float:
        """0~1，越高越匹配用户目标。"""

    def on_matched(self, session: CollabSession) -> tuple[str, list[str]]:
        """返回 (agent 话, chips)。通常进入 need_frame。"""

    def explore(self, session: CollabSession, frame_path: Path) -> ExploreResult:
        ...

    def emit(self, session: CollabSession) -> EmitResult:
        ...

    def tip_for_step(self, step_id: str) -> str:
        ...
