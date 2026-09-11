"""试运行 Live HUD：运行状态点 + 实时状态/动作 + 计时 + L1 可行性/可靠性回显。

纯前端组件（不依赖后端事件结构）：ScriptGenerator 试运行页把
log_signal / state_event 的行与终态喂进来即可。视觉由 script_gen.qss
（dark/light 各一份）里 QFrame#TrialHudCard 一组 objectName 控制。
"""

from __future__ import annotations

import re
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy


# ── 日志行解析（尽力而为，识别不出就只当“动作”显示）──────────────

_META_BRACKET = {
    "试运行", "伪录制", "黑屏", "超时", "状态", "场景", "阶段", "步骤",
    "info", "log", "warn", "warning", "error", "debug", "trace",
    "exit", "done",
}
_STATE_HINT_TOKENS = {"scene", "step", "state", "note"}

_BRACKET_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(.*)$")
_LEVEL_TOKEN_RE = re.compile(r"^(?:[A-Z][A-Z0-9_]*|[\w.]*\.[A-Z]+)$")
_NOISE_RE = re.compile(
    r"仍在等待|继续|重试|no match|未命中|尝试点击|识别到.*|等待.*完成|(已|正在).{0,6}(跳转|返回|点击|进入)",
    re.I,
)


def parse_trial_line(line: str) -> dict:
    """从一行日志抽出 {state, action}（可能为空）。

    逐层剥掉 [INFO]/[LogLevel.INFO] 这类级别前缀与噪音括号标记，
    把 [wait_game_load] / [scene] 主界面 等状态标记解出来。
    """
    text = (line or "").strip()
    if not text:
        return {}
    rest = text
    final_state = None
    action = None
    for _ in range(4):
        m = _BRACKET_RE.match(rest)
        if not m:
            break
        token = m.group(1).strip()
        after = m.group(2).strip()
        tlow = token.lower()
        if tlow in _META_BRACKET or _LEVEL_TOKEN_RE.match(token):
            rest = after
            continue
        if tlow in _STATE_HINT_TOKENS:
            final_state = (after or token)[:24]
            action = after[:64] or None
            rest = ""
            break
        # 普通 [状态词] 标记行
        final_state = token[:24]
        action = after[:64] or None
        rest = ""
        break
    if action is None:
        # 去前缀后的纯文本/残余 → 作为动作
        candidate = _BRACKET_RE.sub("", rest).strip() or _BRACKET_RE.sub("", text).strip()
        action = candidate[:64] or text[:64]
    if final_state and _NOISE_RE.search(final_state):
        final_state = None
    return {k: v for k, v in (("state", final_state), ("action", action)) if v}


def _clip(text: str, n: int) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


class TrialHud(QFrame):
    """单行试运行状态条（状态点 / 账号 / 当前状态 / 最近动作 / 计时 / 结论徽章）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TrialHudCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(10)

        self._dot = QLabel("")
        self._dot.setObjectName("TrialHudDot")
        self._dot.setFixedSize(10, 10)
        lay.addWidget(self._dot)

        self._status_text = QLabel("空闲")
        self._status_text.setObjectName("TrialHudStateText")
        lay.addWidget(self._status_text)

        sep1 = QLabel("·")
        sep1.setObjectName("TrialHudMuted")
        lay.addWidget(sep1)

        self._account_lbl = QLabel("—")
        self._account_lbl.setObjectName("TrialHudAccount")
        lay.addWidget(self._account_lbl)

        sep2 = QLabel("·")
        sep2.setObjectName("TrialHudMuted")
        lay.addWidget(sep2)

        self._state_lbl = QLabel("状态 —")
        self._state_lbl.setObjectName("TrialHudStateBadge")
        lay.addWidget(self._state_lbl)

        sep3 = QLabel("·")
        sep3.setObjectName("TrialHudMuted")
        lay.addWidget(sep3)

        self._action_lbl = QLabel("动作 —")
        self._action_lbl.setObjectName("TrialHudAction")
        self._action_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._action_lbl, 1)

        self._count_lbl = QLabel("日志 0")
        self._count_lbl.setObjectName("TrialHudMuted")
        lay.addWidget(self._count_lbl)

        self._timer_lbl = QLabel("00:00")
        self._timer_lbl.setObjectName("TrialHudTimer")
        lay.addWidget(self._timer_lbl)

        self._feas_lbl = QLabel("")
        self._feas_lbl.setObjectName("TrialHudVerdict")
        self._feas_lbl.hide()
        lay.addWidget(self._feas_lbl)

        # 计时 / 呼吸灯
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._pulse = QTimer(self)
        self._pulse.setInterval(900)
        self._pulse.timeout.connect(self._on_pulse)
        self._started_at = None
        self._pulse_on = False
        self._dot_state = "idle"
        self._log_count = 0

    # ── 对外 API ──────────────────────────────────────────────

    def reset(self, account: str = "") -> None:
        self._tick.stop()
        self._pulse.stop()
        self._started_at = None
        self._log_count = 0
        self._set_dot("idle")
        self._status_text.setText("空闲")
        self._account_lbl.setText(account or "—")
        self._state_lbl.setText("状态 —")
        self._action_lbl.setText("动作 —")
        self._count_lbl.setText("日志 0")
        self._timer_lbl.setText("00:00")
        self._feas_lbl.hide()
        self._feas_lbl.setProperty("level", "")
        self._feas_lbl.setProperty("reliability", "")
        self._repolish(self._feas_lbl)

    def start_run(self, account: str) -> None:
        self.reset(account)
        self._started_at = datetime.now()
        self._dot_state = "running"
        self._pulse_on = True
        self._repolish(self._dot)
        self._status_text.setText("运行中")
        self._tick.start()
        self._pulse.start()

    def on_log_line(self, line: str) -> None:
        if not line or not (line or "").strip():
            return
        self._log_count += 1
        self._count_lbl.setText(f"日志 {self._log_count}")
        parsed = parse_trial_line(line)
        state = parsed.get("state")
        action = parsed.get("action")
        if state:
            self._state_lbl.setText("状态 " + _clip(state, 22))
        if action and not _NOISE_RE.search(action):
            self._action_lbl.setText("动作 " + _clip(action, 56))
            self._action_lbl.setToolTip(line.strip()[:300])

    def set_terminal(self, status: str = "") -> None:
        """终态：finished / stopped / error / idle。"""
        self._tick.stop()
        self._pulse.stop()
        st = (status or "").lower()
        if st == "finished":
            dot, label = "ok", "已完成"
        elif st == "stopped":
            dot, label = "warn", "已停止"
        elif st == "error":
            dot, label = "err", "出错"
        elif st == "idle":
            dot, label = "warn", "已结束"
        else:
            dot, label = "idle", status or "已结束"
        self._set_dot(dot)
        self._status_text.setText(label)

    def set_feasibility(self, feas: dict) -> None:
        """渲染 L1 可行性徽章；带 reliability_compare 时追加可靠性徽章。"""
        if not feas:
            self._feas_lbl.hide()
            return
        level = str(feas.get("level") or "")
        self._feas_lbl.setProperty("level", level)
        rel = feas.get("reliability_compare") or {}
        if rel:
            ok = bool(rel.get("ok"))
            self._feas_lbl.setProperty("reliability", "ok" if ok else "risk")
            rel_note = ""
            if not ok:
                degraded = rel.get("degraded") or []
                rel_note = " · 可靠性可能下降：" + "；".join(str(d) for d in degraded[:3])
            text = f"L1 {_feas_label(level)}"
            tip = (feas.get("user_message") or "") + rel_note
            self._feas_lbl.setText(text)
            self._feas_lbl.setToolTip(tip.strip() or "L1 可行性")
            self._feas_lbl.show()
        elif level:
            self._feas_lbl.setProperty("reliability", "")
            self._feas_lbl.setText(f"L1 {_feas_label(level)}")
            self._feas_lbl.setToolTip(feas.get("user_message") or "")
            self._feas_lbl.show()
        self._repolish(self._feas_lbl)

    # ── 内部 ──────────────────────────────────────────────────

    def _set_dot(self, state: str) -> None:
        self._dot_state = state
        self._dot.setProperty("state", state)
        self._dot.setProperty("pulse", "0")
        self._repolish(self._dot)

    def _on_tick(self) -> None:
        if self._started_at is None:
            return
        secs = max(0, int((datetime.now() - self._started_at).total_seconds()))
        self._timer_lbl.setText(f"{secs // 60:02d}:{secs % 60:02d}")

    def _on_pulse(self) -> None:
        if self._dot_state != "running":
            return
        self._pulse_on = not self._pulse_on
        self._dot.setProperty("pulse", "1" if self._pulse_on else "0")
        self._repolish(self._dot)

    @staticmethod
    def _repolish(w) -> None:
        try:
            w.style().unpolish(w)
            w.style().polish(w)
        except Exception:
            pass


def _feas_label(level: str) -> str:
    return {
        "pass": "通过",
        "partial": "部分",
        "fail": "未通过",
    }.get(str(level or ""), str(level or "—"))