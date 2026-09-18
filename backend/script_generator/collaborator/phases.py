"""协作状态机阶段（与 design_collaborative_v2 §5.3 对齐）。"""

from __future__ import annotations

from enum import Enum


class CollabPhase(str, Enum):
    IDLE = "idle"
    UNDERSTAND = "understand"
    CLARIFY = "clarify"
    EXPLORE = "explore"
    PROPOSE_LOGIC = "propose_logic"
    CODEGEN = "codegen"
    READY_TRIAL = "ready_trial"
    TRIAL = "trial"
    DONE = "done"


PHASE_BANNER: dict[CollabPhase, str] = {
    CollabPhase.IDLE: "协作 · 用一句话说目标开始",
    CollabPhase.UNDERSTAND: "协作 · 正在理解目标…",
    CollabPhase.CLARIFY: "协作 · 需要你选一下（或补充一句）",
    CollabPhase.EXPLORE: "协作 · 正在看画面 / 跑探针…",
    CollabPhase.PROPOSE_LOGIC: "协作 · 请核对逻辑与预览，确认后生成",
    CollabPhase.CODEGEN: "协作 · 正在生成脚本…",
    CollabPhase.READY_TRIAL: "协作 · 脚本已就绪，可去试运行",
    CollabPhase.TRIAL: "协作 · 试跑中",
    CollabPhase.DONE: "协作 · 已完成",
}
