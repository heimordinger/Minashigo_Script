"""试跑游标：把脚本 ``note_state(name)`` 映回 LogicGraph 的步骤 id。

契约（design_collaborative_v2 §4.1 C / §6）：
- 生成时按步骤 id 写 ``browser.note_state("<step_id>")``；试跑时运行层把每次
  状态名变化打一行标记日志，GUI 解析后喂给本模块，命中步骤即「试跑至此」。
- 只在状态名真变化时打标记（FSM 每轮都会调 note_state，不能每轮刷屏）。
- 匹配不上就什么都不做：游标宁可不走，也不能跳到一个不相干的步骤。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# 运行层（UserWindow / UserBrowser）与 GUI 共用的标记协议。
# 形如： [MINASHIGO_STEP] attack_best
STEP_MARK = "[MINASHIGO_STEP]"

_STEP_MARK_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*\[MINASHIGO_STEP\]\s*(?P<name>.+?)\s*$"
)
_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def step_mark_message(name: str) -> str:
    """运行层写日志时用这个，保证与 GUI 解析同一协议。"""
    return f"{STEP_MARK} {str(name or '').strip()}"


def parse_step_mark(line: str) -> str:
    """从一行日志里取出状态名；不是标记行返回空串。"""
    m = _STEP_MARK_RE.match(str(line or ""))
    return (m.group("name") if m else "").strip()


def normalize_step_token(text: str) -> str:
    """步骤 id / 状态名的宽松归一：大小写、标点、下划线差异都不算差别。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = _CAMEL.sub("_", raw)
    return _NON_WORD.sub("_", raw).strip("_").lower()


def _tokens(text: str) -> set[str]:
    raw = _CAMEL.sub("_", str(text or ""))
    return {t for t in _NON_WORD.split(raw) if t}


def match_step_id(state: str, step_ids: Iterable[str]) -> Optional[str]:
    """把 ``note_state`` 的状态名对到步骤 id；对不上返回 None。

    候选只来自 LogicGraph 自己的步骤 id，所以「对不上」是安全结果。
    评分：完全相同 > 前缀包含 > 词元包含；都需要一侧完整覆盖另一侧，
    避免 ``wait_battle`` 这类短名把无关步骤点亮。
    """
    raw = str(state or "").strip()
    if not raw:
        return None
    norm = normalize_step_token(raw)
    if not norm:
        return None

    best_id: Optional[str] = None
    best_score = 0.0
    for sid in step_ids or []:
        sid_s = str(sid or "").strip()
        if not sid_s:
            continue
        sid_norm = normalize_step_token(sid_s)
        if not sid_norm:
            continue
        score = 0.0
        if norm == sid_norm:
            score = 1.0
        elif min(len(norm), len(sid_norm)) >= 3 and (
            norm.startswith(sid_norm) or sid_norm.startswith(norm)
        ):
            score = 0.8
        else:
            a, b = _tokens(raw), _tokens(sid_s)
            if a and b and (a <= b or b <= a):
                # 词元数越接近越可信
                score = 0.5 + 0.1 * (min(len(a), len(b)) / max(len(a), len(b)))
        if score > best_score:
            best_score, best_id = score, sid_s
    return best_id


@dataclass
class TrialCursor:
    """一次试跑的步骤游标：当前步 + 走过的步。

    - ``active``：``note_state`` 刚报到的步骤，卡片显示「试跑至此」。
    - ``visited``：本次试跑走过（或跳过）的步骤，收尾时显示「已确认」当轨迹。
    - ``failed``：用户点过「这一步不对」的步骤，任何时候都不被游标覆盖。
    """

    step_ids: list[str] = field(default_factory=list)
    index: int = -1
    visited: list[int] = field(default_factory=list)
    failed: set[int] = field(default_factory=set)
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.step_ids = [str(s or "") for s in (self.step_ids or [])]

    def bind_steps(self, step_ids: Iterable[str]) -> None:
        """换一份逻辑图（重新生成 / 恢复会话）时重绑，游标清零。"""
        new_ids = [str(s or "") for s in (step_ids or [])]
        if new_ids == self.step_ids:
            return
        self.step_ids = new_ids
        self.reset()

    def reset(self, *, keep_log: bool = False) -> None:
        self.index = -1
        self.visited = []
        if not keep_log:
            self.log = []

    @property
    def active_id(self) -> str:
        if 0 <= self.index < len(self.step_ids):
            return self.step_ids[self.index]
        return ""

    def note(self, state: str) -> Optional[str]:
        """报一个状态名。返回命中的步骤 id；认不出或重复返回 None。"""
        name = str(state or "").strip()
        if not name:
            # 显式清空：本轮结束 / 脚本退出
            self.index = -1
            return None
        sid = match_step_id(name, self.step_ids)
        if sid is None:
            return None
        idx = self.step_ids.index(sid)
        if idx == self.index:
            return None
        # 只要「到过」，就记进 visited —— 它同时是当前步和轨迹的唯一来源，
        # 收尾时不用再猜「最后那步算不算跑到」。
        if idx not in self.visited:
            self.visited.append(idx)
        self.index = idx
        self.log.append(name)
        del self.log[:-24]
        return sid

    def statuses(self) -> dict[str, str]:
        """本次试跑后每个步骤应有的状态（全量，未走到的回落 draft）。

        给全量而不是只给变化项：游标推进要能把「上一跑亮过、这一跑没走到」的
        步骤熄掉；只做局部更新会留下脏状态。
        当前步 = 走到过的最后一步（``visited`` 末位），``index`` 只管「有没有在跑」。
        """
        last = self.visited[-1] if (self.visited and self.index >= 0) else -1
        out: dict[str, str] = {}
        for i, sid in enumerate(self.step_ids):
            if not sid:
                continue
            if i in self.failed:
                out[sid] = "failed"
            elif i == last:
                out[sid] = "running"
            elif i in self.visited:
                out[sid] = "acked"
            else:
                out[sid] = "draft"
        return out

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.step_ids),
            "visited": len(self.visited),
            "active": 1 if self.active_id else 0,
        }
