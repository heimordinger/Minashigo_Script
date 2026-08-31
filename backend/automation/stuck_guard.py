"""脚本卡住检测：动作循环 + 状态停滞 + 长闲置兜底（替代短 sleep 看门狗）。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional


def _is_wait_state(name: str | None) -> bool:
    """识别/等待类状态：长时间停留是正常的，不按「状态停滞」硬杀。"""
    if not name:
        return False
    n = str(name).strip().lower()
    if n in {"未知", "unknown", "wait_game_load", "loading"}:
        return True
    if n.startswith("wait_") or n.startswith("等待"):
        return True
    # 中文步骤名：含「等待」视为加载/战斗等待（如 竞技场等待战斗、塔等待载入）
    if "等待" in str(name):
        return True
    return False


class StuckGuard:
    """
    - note_state(name): 同一状态停留过久 → 警告 / 失败
      （「未知」等等待态只警告，不因状态名未变而失败）
    - note_action(...): 相同动作结果连续重复 → 警告 / 失败
    - check_idle(): 长时间无任何进展 → 失败
    正常「等待界面出现」的 match 失败不计入动作循环。
    """

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None):
        self._log = log_fn or (lambda _msg: None)

        # 长闲置（秒）：无 match 成功 / click 成功 / 状态切换
        self.idle_limit: float = 300.0
        self._last_progress: float = time.time()

        # 状态停滞（仅对非等待态硬失败）
        self._state: str | None = None
        self._state_since: float | None = None
        self._state_warned: bool = False
        self.state_warn_s: float = 90.0
        self.state_fail_s: float = 180.0
        # 等待态只告警，阈值更长
        self.wait_state_warn_s: float = 120.0

        # 动作循环：连续相同指纹
        self._streak_fp: tuple | None = None
        self._streak_n: int = 0
        self._loop_warned: bool = False
        # match 成功重复（已在目标界面空转）
        self.match_ok_warn: int = 20
        self.match_ok_fail: int = 40
        # click 失败重复（点不到还在点）
        self.click_fail_warn: int = 8
        self.click_fail_fail: int = 16
        # click 成功重复只警告、默认不杀（跳过动画等场景合法）
        self.click_ok_warn: int = 30

    def set_log(self, log_fn: Callable[[str], None]):
        self._log = log_fn

    def reset(self):
        """新任务开始时调用，避免跨任务同名状态（如「未知」）累计计时。"""
        self._last_progress = time.time()
        self._state = None
        self._state_since = None
        self._state_warned = False
        self._streak_fp = None
        self._streak_n = 0
        self._loop_warned = False

    def note_progress(self, *, clear_actions: bool = False):
        self._last_progress = time.time()
        # 有进展则刷新状态计时，避免误杀
        self._state_since = time.time()
        self._state_warned = False
        if clear_actions:
            self._streak_fp = None
            self._streak_n = 0
            self._loop_warned = False

    def check_idle(self):
        idle = time.time() - self._last_progress
        if idle > self.idle_limit:
            raise RuntimeError(
                f"长时间无进展：{idle:.0f}s 内无成功匹配/点击/状态切换"
                f"（上限 {self.idle_limit:.0f}s），脚本可能已卡死"
            )

    def note_state(self, name: str | None):
        """FSM 每轮调用；状态名变化视为进展。"""
        now = time.time()
        if name != self._state:
            self._state = name
            self._state_since = now
            self._state_warned = False
            self.note_progress(clear_actions=True)
            return

        if self._state_since is None:
            self._state_since = now
            return

        elapsed = now - self._state_since
        waiting = _is_wait_state(name)

        if waiting:
            # 「未知」等：靠脚本自身 STATE_TIMEOUT / 闲置检测，不因同名停留硬杀
            if elapsed >= self.wait_state_warn_s and not self._state_warned:
                self._state_warned = True
                self._log(
                    f"[WARN] 等待态「{name}」已停留 {elapsed:.0f}s，"
                    f"若一直无法识别场景请检查模板图/阈值"
                )
            return

        if elapsed >= self.state_fail_s:
            raise RuntimeError(
                f"状态停滞：已在「{name}」连续停留 {elapsed:.0f}s，"
                f"未见状态切换。请检查该状态逻辑或模板图。"
            )
        if elapsed >= self.state_warn_s and not self._state_warned:
            self._state_warned = True
            self._log(
                f"[WARN] 状态「{name}」已停留 {elapsed:.0f}s，"
                f"若界面无变化请检查脚本"
            )

    def note_action(self, action: str, img_path, ok: bool):
        """
        action: "match" | "click"
        - match：仅应在匹配成功时调用
        - click：成功/失败都可调用
        """
        name = Path(str(img_path)).name if img_path else ""
        if name.startswith("data:"):
            name = "(内存图)"
        fp = (action, name, bool(ok))

        if fp == self._streak_fp:
            self._streak_n += 1
        else:
            self._streak_fp = fp
            self._streak_n = 1
            self._loop_warned = False

        if ok:
            self._last_progress = time.time()
            self._state_since = time.time()
            self._state_warned = False

        warn_at, fail_at = self._thresholds(action, ok)
        if fail_at and self._streak_n >= fail_at:
            raise RuntimeError(
                f"动作循环：连续 {self._streak_n} 次 {action}「{name}」"
                f"{'成功' if ok else '失败'}，脚本可能在空转。"
                f"常见原因：目标已达成却未 __exit__，或模板/阈值不对。"
            )
        if warn_at and self._streak_n >= warn_at and not self._loop_warned:
            self._loop_warned = True
            self._log(
                f"[WARN] 动作可能循环：连续 {self._streak_n} 次 "
                f"{action}「{name}」{'成功' if ok else '失败'}"
            )

    def _thresholds(self, action: str, ok: bool) -> tuple[int | None, int | None]:
        if action == "match" and ok:
            return self.match_ok_warn, self.match_ok_fail
        if action == "click" and not ok:
            return self.click_fail_warn, self.click_fail_fail
        if action == "click" and ok:
            return self.click_ok_warn, None
        return None, None
