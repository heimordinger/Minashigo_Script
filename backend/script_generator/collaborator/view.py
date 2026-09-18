"""GUI 渲染用的一帧视图更新（引擎 → 协作页）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from backend.script_generator.collaborator.phases import CollabPhase, PHASE_BANNER


@dataclass
class CollabViewUpdate:
    phase: CollabPhase = CollabPhase.IDLE
    banner: str = ""
    agent_message: str = ""
    chips: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    shape_label: str = ""
    cta_text: str = "看起来对，生成脚本"
    cta_enabled: bool = False
    problem_enabled: bool = True
    preview_path: str | None = None
    preview_caption: str = ""
    assets: list[dict[str, Any]] = field(default_factory=list)
    overlays: list[dict[str, Any]] = field(default_factory=list)
    focused_asset_ids: list[str] = field(default_factory=list)
    code: str | None = None
    intro: str | None = None
    script_name: str | None = None
    trial_ready: bool = False
    trial_hint: str = ""
    # 确认逻辑后、还没有成品：GUI 再请模型写 script
    request_script: bool = False
    # True 时表示本次应把 agent_message 追加到对话
    append_agent: bool = True
    # agent = LLM/技能答话；system = 操作通知（截图/抓窗/状态）
    bubble_kind: str = "agent"

    @classmethod
    def from_phase(
        cls,
        phase: CollabPhase,
        *,
        agent_message: str = "",
        chips: list[str] | None = None,
        **kwargs: Any,
    ) -> "CollabViewUpdate":
        return cls(
            phase=phase,
            banner=PHASE_BANNER.get(phase, str(phase.value)),
            agent_message=agent_message,
            chips=list(chips or []),
            **kwargs,
        )
