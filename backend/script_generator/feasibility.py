"""试运行 L1 可行性评估：优先 timeline 事件，日志仅作回退。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional


_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)|^\s*File \".+\", line \d+", re.M)
_TIMEOUT_RE = re.compile(r"超时|timeout|Timeout", re.I)
_EXIT_RE = re.compile(r"__exit__|全部完成|任务完成|登录完成|脚本结束|do_work.*结束", re.I)
_MATCH_OK_RE = re.compile(r"匹配成功|✅\s*匹配")
_CLICK_RE = re.compile(r"点击图片|click_image|点击\s")
_STUCK_RE = re.compile(r"卡住|空转|未见进展|同一状态|反复")
_ERR_POPUP_RE = re.compile(r"代理.*失败|err2|网络错误", re.I)
_PSEUDO_RE = re.compile(r"伪录制|pseudo_record|enable_pseudo", re.I)


def match_stats_from_timeline(
    timeline_path: Path | str | None = None,
    *,
    record_dir: Path | str | None = None,
) -> Optional[dict[str, Any]]:
    """从 timeline.jsonl 统计 match ok/fail 与 click（不依赖日志措辞）。"""
    path: Path | None = None
    if timeline_path:
        path = Path(timeline_path)
    elif record_dir:
        path = Path(record_dir) / "timeline.jsonl"
    if path is None or not path.is_file():
        return None

    match_ok = match_fail = clicks = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            kind = str(e.get("kind") or "")
            if kind == "match":
                if e.get("ok"):
                    match_ok += 1
                else:
                    match_fail += 1
            elif kind in ("click", "click_image"):
                clicks += 1
    except Exception:
        return None

    return {
        "match_ok": match_ok,
        "match_fail": match_fail,
        "clicks": clicks,
        "source": "timeline",
        "path": str(path),
    }


def assess_trial_feasibility(
    trial_log: str,
    *,
    status: str = "",
    min_match_ok: int = 1,
    min_clicks: int = 0,
    timeline_path: Path | str | None = None,
    record_dir: Path | str | None = None,
) -> dict[str, Any]:
    """评估试跑是否达到最低可行（L1）。

    通过条件（默认）：
    - 无 Traceback
    - 终态不是纯 error（stopped 需有进度证据）
    - 至少有匹配成功或明确完成信号

    匹配/点击优先读 timeline 事件；无 timeline 时回退日志中文措辞。
    """
    log = trial_log or ""
    st = (status or "").strip().lower()
    reasons: list[str] = []
    blockers: list[str] = []

    has_tb = bool(_TRACEBACK_RE.search(log))
    if has_tb:
        blockers.append("存在 Traceback / 未捕获异常")

    tl = match_stats_from_timeline(timeline_path, record_dir=record_dir)
    if tl is not None:
        match_ok_n = int(tl["match_ok"])
        click_n = int(tl["clicks"])
        match_source = "timeline"
        reasons.append(
            f"进度来自 timeline：match_ok={match_ok_n} "
            f"match_fail={tl['match_fail']} clicks={click_n}"
        )
    else:
        match_ok_n = len(_MATCH_OK_RE.findall(log))
        click_n = len(_CLICK_RE.findall(log))
        match_source = "log_text"
        if log.strip():
            reasons.append("无 timeline，匹配计数回退日志措辞（可能随日志风格漂移）")

    has_exit = bool(_EXIT_RE.search(log))
    has_timeout = bool(_TIMEOUT_RE.search(log))
    has_stuck = bool(_STUCK_RE.search(log))
    has_net = bool(_ERR_POPUP_RE.search(log))
    has_pseudo = bool(_PSEUDO_RE.search(log)) or tl is not None

    if st in ("error",):
        blockers.append(f"试跑终态为 {st}")
    if match_ok_n < min_match_ok and not has_exit:
        blockers.append(
            f"关键进度不足：匹配成功 {match_ok_n} 次，且未见完成信号"
        )
    if min_clicks and click_n < min_clicks and not has_exit:
        blockers.append(f"点击过少（{click_n} < {min_clicks}）")

    if has_timeout:
        reasons.append("日志含超时信号")
    if has_stuck:
        reasons.append("日志含卡住/空转描述")
    if has_net:
        reasons.append("疑似网络/代理错误弹窗（与脚本逻辑无关）")
    if has_pseudo:
        reasons.append("已开启伪录制" if tl is None else "已读到伪录制 timeline")
    if has_exit:
        reasons.append("出现完成/退出信号")
    if st in ("finished", "idle"):
        reasons.append(f"终态 {st}")
    elif st == "stopped":
        reasons.append("终态 stopped（需人工确认是否中途停止）")

    passed = not blockers
    # stopped 且有足够进度 → 仍可算「部分可行」
    partial = False
    if not passed and not has_tb and (match_ok_n >= min_match_ok or has_exit):
        if st in ("stopped", "", "idle"):
            partial = True

    level = "pass" if passed else ("partial" if partial else "fail")
    user_message = _format_feasibility_message(
        level=level,
        blockers=blockers,
        reasons=reasons,
        match_ok_n=match_ok_n,
        click_n=click_n,
        status=st,
        match_source=match_source,
    )
    return {
        "level": level,
        "passed": passed,
        "partial": partial,
        "blockers": blockers,
        "reasons": reasons,
        "signals": {
            "traceback": has_tb,
            "match_ok": match_ok_n,
            "match_fail": int(tl["match_fail"]) if tl else None,
            "clicks": click_n,
            "match_source": match_source,
            "exit_signal": has_exit,
            "timeout": has_timeout,
            "stuck": has_stuck,
            "network_popup": has_net,
            "pseudo_record": has_pseudo,
            "status": st,
        },
        "user_message": user_message,
        # 优化门禁：未过 L1 不应用「接近边界跳过」
        "optimize_ready": passed or partial,
    }


def _format_feasibility_message(
    *,
    level: str,
    blockers: list[str],
    reasons: list[str],
    match_ok_n: int,
    click_n: int,
    status: str,
    match_source: str = "log_text",
) -> str:
    lines: list[str] = []
    if level == "pass":
        lines.append("【L1 可行性：通过】脚本有真实进度且未崩溃。")
    elif level == "partial":
        lines.append("【L1 可行性：部分】有进度证据，但未干净结束；可谨慎优化。")
    else:
        lines.append("【L1 可行性：未通过】尚不具备可靠优化/验收基础。")

    src = "timeline" if match_source == "timeline" else "日志"
    lines.append(
        f"终态={status or '?'}  匹配成功={match_ok_n}  点击≈{click_n}  （{src}）"
    )
    if blockers:
        lines.append("阻断：")
        for b in blockers[:6]:
            lines.append(f"· {b}")
    if reasons:
        lines.append("观察：")
        for r in reasons[:6]:
            lines.append(f"· {r}")
    return "\n".join(lines)


def compare_reliability(
    before_log: str,
    after_log: str,
    *,
    before_timeline: Path | str | None = None,
    after_timeline: Path | str | None = None,
    before_record_dir: Path | str | None = None,
    after_record_dir: Path | str | None = None,
) -> dict[str, Any]:
    """对比优化前后试跑的可靠性信号（优先 timeline）。"""
    a = assess_trial_feasibility(
        before_log,
        timeline_path=before_timeline,
        record_dir=before_record_dir,
    )
    b = assess_trial_feasibility(
        after_log,
        timeline_path=after_timeline,
        record_dir=after_record_dir,
    )
    sa, sb = a["signals"], b["signals"]
    degraded: list[str] = []
    if sb["traceback"] and not sa["traceback"]:
        degraded.append("新出现 Traceback")
    if sb["match_ok"] + 2 < sa["match_ok"] and sa["match_ok"] >= 3:
        degraded.append(
            f"匹配成功次数下降 {sa['match_ok']}→{sb['match_ok']}"
        )
    if b["level"] == "fail" and a["level"] != "fail":
        degraded.append("可行性从非失败降为失败")
    return {
        "before": a,
        "after": b,
        "degraded": degraded,
        "ok": not degraded,
    }
