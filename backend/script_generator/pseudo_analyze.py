"""伪录制（pseudo_record）本地分析，供脚本性能优化使用。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from backend.automation.run_recorder import (
    PSEUDO_RECORD_ROOT,
    build_summary,
    format_summary_text,
)


def _norm(s: str) -> str:
    return str(s or "").strip().lower()


def _script_stem(name: str) -> str:
    n = Path(name).name
    return n[:-3] if n.lower().endswith(".py") else n


def list_pseudo_record_dirs(
    *,
    root: Path | None = None,
    script_hint: str = "",
    account_hint: str = "",
) -> list[Path]:
    """列出含 timeline.jsonl 的伪录制目录，按 mtime 降序。"""
    base = Path(root or PSEUDO_RECORD_ROOT)
    if not base.is_dir():
        return []
    hint = _norm(_script_stem(script_hint))
    acct = _norm(account_hint)
    out: list[Path] = []
    for d in base.iterdir():
        if not d.is_dir() or not (d / "timeline.jsonl").is_file():
            continue
        name = _norm(d.name)
        if hint and hint not in name:
            continue
        if acct and acct not in name:
            continue
        out.append(d)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def find_latest_pseudo_record(
    *,
    script_hint: str = "",
    account_hint: str = "",
    root: Path | None = None,
) -> Path | None:
    dirs = list_pseudo_record_dirs(
        root=root,
        script_hint=script_hint,
        account_hint=account_hint,
    )
    return dirs[0] if dirs else None


def _load_events(record_dir: Path) -> list[dict]:
    path = record_dir / "timeline.jsonl"
    events: list[dict] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def _top_wall_gaps(events: list[dict], *, min_dt: float = 2.0, limit: int = 12) -> list[dict]:
    gaps: list[dict] = []
    for i in range(1, len(events)):
        try:
            dt = float(events[i]["t"]) - float(events[i - 1]["t"])
        except Exception:
            continue
        if dt < min_dt:
            continue
        a, b = events[i - 1], events[i]
        gaps.append(
            {
                "dt_s": round(dt, 2),
                "t0": a.get("t"),
                "t1": b.get("t"),
                "from": _event_brief(a),
                "to": _event_brief(b),
            }
        )
    gaps.sort(key=lambda x: -x["dt_s"])
    return gaps[:limit]


def _event_brief(e: dict) -> str:
    kind = str(e.get("kind") or "")
    if kind == "log":
        msg = str(e.get("msg") or "")[:60]
        return f"log:{msg}"
    if kind == "sleep":
        return f"sleep planned={e.get('planned_s')} dt={e.get('dt_ms')}ms"
    if kind == "capture":
        return f"capture {e.get('dt_ms')}ms mode={e.get('mode')}"
    if kind == "match":
        tpl = str(e.get("template") or "").replace("\\", "/").split("/")[-1]
        return f"match:{tpl} ok={e.get('ok')} {e.get('dt_ms')}ms"
    if kind in ("black_on", "black_off"):
        return f"{kind} dt={e.get('dt_ms')}ms"
    return kind


def _match_template_stats(events: list[dict], *, limit: int = 15) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for e in events:
        if e.get("kind") != "match":
            continue
        tpl = str(e.get("template") or "").replace("\\", "/").split("/")[-1] or "?"
        bucket = stats.setdefault(
            tpl,
            {"ok": 0, "fail": 0, "dt_ms": 0.0, "scores": []},
        )
        if e.get("ok"):
            bucket["ok"] += 1
        else:
            bucket["fail"] += 1
        bucket["dt_ms"] += float(e.get("dt_ms") or 0)
        sc = e.get("score")
        if sc is not None:
            try:
                bucket["scores"].append(float(sc))
            except Exception:
                pass
    ranked = sorted(
        stats.items(),
        key=lambda kv: kv[1]["fail"] * 1000 + kv[1]["dt_ms"],
        reverse=True,
    )
    out: dict[str, dict] = {}
    for tpl, b in ranked[:limit]:
        scores = b["scores"]
        out[tpl] = {
            "ok": b["ok"],
            "fail": b["fail"],
            "match_cpu_s": round(b["dt_ms"] / 1000.0, 2),
            "score_min": round(min(scores), 3) if scores else None,
            "score_max": round(max(scores), 3) if scores else None,
        }
    return out


def _phase_logs(events: list[dict], *, limit: int = 40) -> list[dict]:
    keys = (
        "步骤", "第", "完成", "等待", "点击", "进入", "过场", "AUTO",
        "伪录制", "对齐", "匹配", "失败", "回退", "jjc", "塔", "room",
    )
    out: list[dict] = []
    for e in events:
        if e.get("kind") != "log":
            continue
        msg = str(e.get("msg") or "")
        if not any(k in msg for k in keys):
            continue
        out.append({"t": e.get("t"), "msg": msg[:120]})
        if len(out) >= limit:
            break
    return out


def analyze_pseudo_record_dir(record_dir: Path) -> dict[str, Any]:
    """分析单个伪录制目录。始终从 timeline 重算 summary。"""
    record_dir = Path(record_dir)
    timeline = record_dir / "timeline.jsonl"
    summary = build_summary(timeline)
    try:
        (record_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (record_dir / "summary.txt").write_text(
            format_summary_text(summary) + "\n", encoding="utf-8"
        )
    except Exception:
        pass

    events = _load_events(record_dir)
    capture_modes = Counter(
        e.get("mode") for e in events if e.get("kind") == "capture"
    )
    return {
        "dir": str(record_dir),
        "dir_name": record_dir.name,
        "summary": summary,
        "summary_text": format_summary_text(summary),
        "phase_logs": _phase_logs(events),
        "phases": _infer_phases(events),
        "top_gaps": _top_wall_gaps(events),
        "match_templates": _match_template_stats(events),
        "capture_modes": dict(capture_modes),
        "event_count": len(events),
        "metrics_reliable": bool(summary.get("metrics_reliable", True)),
        "ceiling_eligible": bool(summary.get("ceiling_eligible", False)),
        "weak_ceiling_eligible": bool(summary.get("weak_ceiling_eligible", False)),
        "quality_flags": list(summary.get("quality_flags") or []),
    }


_PHASE_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("enter_page", re.compile(r"导航|goto|打开|进入游戏|游戏页|entry", re.I)),
    ("first_ui", re.compile(r"start|开始|点击开始|logo|加载完成|wait_game", re.I)),
    ("after_start", re.compile(r"登录奖励|skip|关闭|前进|进入主界面|主界面", re.I)),
    ("battle_or_auto", re.compile(r"AUTO|战斗|出撃|挑戦|结算", re.I)),
    ("done", re.compile(r"完成|__exit__|全部完成|登录完成|rank", re.I)),
]


def _infer_phases(events: list[dict]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in events:
        if e.get("kind") != "log":
            continue
        msg = str(e.get("msg") or "")
        for name, pat in _PHASE_MARKERS:
            if name in seen:
                continue
            if pat.search(msg):
                seen.add(name)
                hits.append({"phase": name, "t": e.get("t"), "msg": msg[:100]})
                break
    return hits


_IRREDUCIBLE_GAP_RE = re.compile(
    r"加载|过场|AUTO|战斗|结算|banner|等待.*完成|运行中|挑戦|出撃制限",
    re.I,
)
_SCRIPT_WASTE_GAP_RE = re.compile(
    r"对齐|滚动|匹配|切换难度|关闭.*横幅|notice|伪匹配|重试|"
    r"未见|超时|回退|切下一|空转|误匹配|banner\+\(",
    re.I,
)
_OVERHEAD_LOW_RATIO = 0.22
_IRREDUCIBLE_GAP_RATIO = 0.62


def compare_recent_effective(
    *,
    script_hint: str = "",
    account_hint: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    """对比最近两次可比伪录制的 effective。"""
    dirs = list_pseudo_record_dirs(
        root=root, script_hint=script_hint, account_hint=account_hint,
    )
    if len(dirs) < 2 and account_hint:
        dirs = list_pseudo_record_dirs(root=root, script_hint=script_hint)
    if len(dirs) < 2:
        return {"has_prior": False, "reason": "不足两次录制"}

    cur_dir, prev_dir = dirs[0], dirs[1]
    cur = analyze_pseudo_record_dir(cur_dir)
    prev = analyze_pseudo_record_dir(prev_dir)
    cur_s = cur.get("summary") or {}
    prev_s = prev.get("summary") or {}

    if not cur_s.get("metrics_reliable") or not prev_s.get("metrics_reliable"):
        return {
            "has_prior": True,
            "comparable": False,
            "reason": "录制质量不可靠，拒绝用 effective 判断收敛",
            "current_dir": cur_dir.name,
            "prior_dir": prev_dir.name,
            "converged": False,
        }

    cur_eff = float(cur_s.get("effective_s") or 0)
    prev_eff = float(prev_s.get("effective_s") or 0)
    cur_total = float(cur_s.get("total_s") or 0)
    prev_total = float(prev_s.get("total_s") or 0)
    if prev_eff <= 0 or cur_eff <= 0:
        return {
            "has_prior": True,
            "comparable": False,
            "reason": "effective≤0",
            "converged": False,
            "current_dir": cur_dir.name,
            "prior_dir": prev_dir.name,
        }

    if prev_total > 0 and cur_total > 0:
        ratio = cur_total / prev_total
        if ratio < 0.6 or ratio > 1.67 or abs(cur_total - prev_total) > 180:
            return {
                "has_prior": True,
                "comparable": False,
                "reason": f"墙钟不可比 cur={cur_total:.0f}s prev={prev_total:.0f}s",
                "current_dir": cur_dir.name,
                "prior_dir": prev_dir.name,
                "current_effective_s": round(cur_eff, 2),
                "prior_effective_s": round(prev_eff, 2),
                "converged": False,
            }

    cur_norm = cur_eff / cur_total if cur_total > 0 else 0.0
    prev_norm = prev_eff / prev_total if prev_total > 0 else 0.0
    delta = cur_eff - prev_eff
    pct = delta / prev_eff * 100.0
    norm_delta_pct = (
        (cur_norm - prev_norm) / prev_norm * 100.0 if prev_norm > 0 else 0.0
    )

    return {
        "has_prior": True,
        "comparable": True,
        "current_dir": cur_dir.name,
        "prior_dir": prev_dir.name,
        "current_effective_s": round(cur_eff, 2),
        "prior_effective_s": round(prev_eff, 2),
        "current_total_s": round(cur_total, 2),
        "prior_total_s": round(prev_total, 2),
        "delta_s": round(delta, 2),
        "delta_pct": round(pct, 1),
        "norm_effective_ratio": {
            "current": round(cur_norm, 3),
            "prior": round(prev_norm, 3),
            "delta_pct": round(norm_delta_pct, 1),
        },
        "converged": abs(pct) < 8.0 and abs(norm_delta_pct) < 8.0,
    }


def assess_optimization_ceiling(
    analysis: dict[str, Any] | None,
    *,
    prior_compare: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """评估脚本侧优化是否已接近边界。脏数据时禁止 recommend_skip。"""
    if not analysis:
        return {
            "status": "insufficient_data",
            "near_ceiling": False,
            "confidence": 0.0,
            "reasons": ["无伪录制数据，无法评估优化边界"],
            "user_message": (
                "未找到伪录制，无法判断优化边界。"
                "建议试跑（会自动开伪录制）后再优化。"
            ),
            "recommend_skip": False,
        }

    summary = analysis.get("summary") or {}
    totals = summary.get("totals") or {}
    flags = list(analysis.get("quality_flags") or summary.get("quality_flags") or [])
    reliable = bool(analysis.get("metrics_reliable", summary.get("metrics_reliable", True)))
    eligible = bool(analysis.get("ceiling_eligible", summary.get("ceiling_eligible", False)))
    weak_eligible = bool(
        analysis.get("weak_ceiling_eligible", summary.get("weak_ceiling_eligible", False))
    )

    if not reliable:
        msg = (
            "伪录制指标不可靠（"
            + ", ".join(flags[:4])
            + "），不能据此判断优化边界或跳过 LLM。"
        )
        return {
            "status": "unreliable_metrics",
            "near_ceiling": False,
            "confidence": 0.0,
            "reasons": flags or ["metrics_unreliable"],
            "user_message": msg,
            "recommend_skip": False,
            "metrics": {
                "total_s": summary.get("total_s"),
                "effective_s": summary.get("effective_s"),
                "black_s": summary.get("black_s"),
                "quality_flags": flags,
            },
        }

    total_s = float(summary.get("total_s") or 0)
    effective_s = float(summary.get("effective_s") or totals.get("effective_s") or 0)
    black_s = float(summary.get("black_s") or totals.get("black_s") or 0)
    # 脚本线程 overhead：只用非黑段 match（capture 为并行 observer，不进占比）
    match_s = float(totals.get("match_out_black_s") or totals.get("match_s") or 0)
    parallel_s = float(
        totals.get("parallel_overhead_s")
        or totals.get("capture_out_black_s")
        or totals.get("capture_s")
        or 0
    )
    match_fail = int(
        totals.get("match_fail_out_black")
        if totals.get("match_fail_out_black") is not None
        else totals.get("match_fail")
        or 0
    )
    match_ok = int(
        totals.get("match_ok_out_black")
        if totals.get("match_ok_out_black") is not None
        else totals.get("match_ok")
        or 0
    )
    match_total = match_ok + match_fail
    # 无黑埋点时 out_black≈全量；弱评估统一用 totals 全量 match
    if "no_black_instrumentation" in flags:
        match_fail = int(totals.get("match_fail") or match_fail)
        match_ok = int(totals.get("match_ok") or match_ok)
        match_s = float(totals.get("match_s") or match_s)
        match_total = match_ok + match_fail

    reasons: list[str] = []
    score_near = 0

    if flags:
        reasons.append("质量标记: " + ", ".join(flags))

    if total_s > 30 and black_s / total_s >= 0.25:
        reasons.append(
            f"墙钟 {total_s:.0f}s 中约 {black_s / total_s * 100:.0f}% 为游戏黑屏"
            "（已不计入 effective，不宜靠加快轮询压缩）"
        )

    if effective_s > 15:
        overhead_ratio = match_s / effective_s
        if overhead_ratio < _OVERHEAD_LOW_RATIO:
            score_near += 2
            reasons.append(
                f"非黑段识图仅占 effective 的 {overhead_ratio * 100:.0f}%，"
                "脚本探测开销已较低"
            )
        elif overhead_ratio > 0.40:
            score_near -= 2
            pct = overhead_ratio * 100
            reasons.append(
                f"非黑段识图累计 {match_s:.0f}s"
                f"（约为 effective 的 {pct:.0f}%），仍有压缩空间"
            )
        if parallel_s > 0:
            reasons.append(
                f"并行截图 observer≈{parallel_s:.0f}s（不计入脚本线程 overhead）"
            )

    # 弱评估：match 失败率权重更高（门槛略降）
    match_gate = 20 if (weak_eligible or "no_black_instrumentation" in flags) else 30
    if match_total >= match_gate:
        fail_rate = match_fail / match_total
        if fail_rate >= 0.22:
            score_near -= 2 if eligible else 3
            reasons.append(
                f"模板失败率 {fail_rate * 100:.0f}%"
                f"（{match_fail}/{match_total}），可能误匹配空转"
            )
        elif fail_rate < 0.10:
            score_near += 1 if eligible else 2
            reasons.append(
                f"模板失败率 {fail_rate * 100:.0f}%"
                f"（{match_fail}/{match_total}），匹配较稳"
            )
        else:
            reasons.append(
                f"模板失败率 {fail_rate * 100:.0f}%（{match_fail}/{match_total}）"
            )

    gaps = analysis.get("top_gaps") or []
    irreducible_s = 0.0
    script_candidate_s = 0.0
    for g in gaps:
        dt = float(g.get("dt_s") or 0)
        text = f"{g.get('from')} {g.get('to')}"
        if _SCRIPT_WASTE_GAP_RE.search(text):
            script_candidate_s += dt
        elif _IRREDUCIBLE_GAP_RE.search(text):
            irreducible_s += dt
        else:
            irreducible_s += dt * 0.55

    gap_total = irreducible_s + script_candidate_s
    if gap_total >= 20:
        irr_ratio = irreducible_s / gap_total if gap_total else 0
        if irr_ratio >= _IRREDUCIBLE_GAP_RATIO:
            score_near += 2
            reasons.append(
                f"主要空档（≥2s）中约 {irr_ratio * 100:.0f}% 为 AUTO/战斗/过场等业务等待"
            )
        if script_candidate_s >= 12:
            score_near -= 2
            reasons.append(
                f"约 {script_candidate_s:.0f}s 空档疑似脚本空转，仍可优化"
            )

    phases = analysis.get("phases") or []
    if phases:
        reasons.append(
            "阶段线索: " + " → ".join(p.get("phase", "?") for p in phases[:5])
        )

    prior = prior_compare or {}
    if prior.get("has_prior") and prior.get("comparable") and prior.get("converged"):
        score_near += 2
        reasons.append(
            f"相较上次可比试跑 effective {prior.get('prior_effective_s')}s → "
            f"{prior.get('current_effective_s')}s（{prior.get('delta_pct'):+.1f}%），"
            "优化收益已收敛"
        )
    elif prior.get("has_prior") and prior.get("comparable") is False:
        reasons.append(f"上次对比不可用：{prior.get('reason')}")

    if score_near >= 3:
        status = "near_ceiling"
        near = True
        confidence = min(0.85, 0.50 + score_near * 0.07)
    elif score_near <= -2:
        status = "clear_room"
        near = False
        confidence = 0.65
    else:
        status = "moderate_room"
        near = False
        confidence = 0.48

    assessment_mode = "full"
    # 无黑屏埋点：弱评估（可给 near 结论供提示，但永不 skip LLM）
    if not eligible:
        if weak_eligible:
            assessment_mode = "weak_no_black"
            reasons.append(
                "弱评估：无黑屏埋点，主要依据 match 失败率与识图占比"
                "（不可据此跳过 LLM）"
            )
            if status == "near_ceiling":
                status = "near_ceiling_weak"
                confidence = min(confidence, 0.55)
            else:
                confidence = min(confidence, 0.58)
        else:
            near = False
            if status == "near_ceiling":
                status = "moderate_room"
            confidence = min(confidence, 0.4)
            reasons.append(
                "缺少黑屏埋点且 match 样本不足，不作「接近边界」结论"
            )

    user_message = format_ceiling_user_message(
        status=status,
        near_ceiling=near,
        confidence=confidence,
        effective_s=effective_s,
        black_s=black_s,
        reasons=reasons,
        prior=prior if prior.get("comparable") else None,
    )

    return {
        "status": status,
        "near_ceiling": near,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "user_message": user_message,
        "assessment_mode": assessment_mode,
        "recommend_skip": bool(near and eligible and confidence >= 0.72),
        "metrics": {
            "total_s": round(total_s, 2),
            "effective_s": round(effective_s, 2),
            "black_s": round(black_s, 2),
            "overhead_ratio": round(match_s / effective_s, 3)
            if effective_s > 0
            else None,
            "parallel_overhead_s": round(parallel_s, 2),
            "match_fail_rate": round(match_fail / match_total, 3)
            if match_total > 0
            else None,
            "irreducible_gap_s": round(irreducible_s, 1),
            "script_waste_gap_s": round(script_candidate_s, 1),
            "quality_flags": flags,
            "ceiling_eligible": eligible,
            "weak_ceiling_eligible": weak_eligible,
            "assessment_mode": assessment_mode,
        },
    }



def format_analysis_for_prompt(analysis: dict[str, Any], *, max_chars: int = 12000) -> str:
    """把分析报告格式化为 LLM 可读文本。"""
    if not analysis:
        return "(无伪录制数据；请结合试跑日志与代码做保守优化。)"

    lines = [
        f"## 伪录制目录\n{analysis.get('dir_name')}\n",
        "## summary.txt",
        str(analysis.get("summary_text") or "").strip(),
        "",
    ]

    s = analysis.get("summary") or {}
    totals = s.get("totals") or {}
    flags = analysis.get("quality_flags") or s.get("quality_flags") or []
    if flags:
        lines.append("## 质量标记")
        lines.append(
            ", ".join(flags)
            + f"\nreliable={analysis.get('metrics_reliable', s.get('metrics_reliable'))}"
        )
        lines.append("")
    if totals:
        lines.append("## 耗时桶 (totals)")
        for k, v in totals.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    phases = analysis.get("phases") or []
    if phases:
        lines.append("## 阶段切分 (启发式)")
        for row in phases:
            lines.append(f"- {row.get('phase')} @ {row.get('t')}s  {row.get('msg')}")
        lines.append("")

    gaps = analysis.get("top_gaps") or []
    if gaps:
        lines.append("## 墙钟空档 (≥2s，脚本侧可优化候选)")
        for g in gaps:
            lines.append(
                f"- +{g['dt_s']}s [{g.get('t0')}→{g.get('t1')}] "
                f"{g.get('from')} => {g.get('to')}"
            )
        lines.append("")

    mt = analysis.get("match_templates") or {}
    if mt:
        lines.append("## 模板匹配 (fail 优先)")
        for tpl, st in mt.items():
            lines.append(
                f"- {tpl}: ok={st['ok']} fail={st['fail']} "
                f"cpu={st['match_cpu_s']}s "
                f"score={st.get('score_min')}..{st.get('score_max')}"
            )
        lines.append("")

    logs = analysis.get("phase_logs") or []
    if logs:
        lines.append("## 阶段日志 (节选)")
        for row in logs:
            lines.append(f"- {row.get('t')}s  {row.get('msg')}")
        lines.append("")

    modes = analysis.get("capture_modes") or {}
    if modes:
        lines.append(f"## capture modes\n{modes}\n")

    text = "\n".join(lines).strip() + "\n"
    if len(text) > max_chars:
        return text[: max_chars - 20] + "\n…(truncated)\n"
    return text


def format_ceiling_user_message(
    *,
    status: str,
    near_ceiling: bool,
    confidence: float,
    effective_s: float,
    black_s: float,
    reasons: list[str],
    prior: dict[str, Any] | None = None,
) -> str:
    """给用户看的边界评估说明。"""
    lines: list[str] = []
    if status == "insufficient_data":
        return (
            "未找到伪录制，无法判断优化边界。"
            "建议试跑并开启 enable_pseudo_record 后再优化。"
        )

    if near_ceiling:
        prefix = "【接近优化边界·弱】" if status == "near_ceiling_weak" else "【接近优化边界】"
        lines.append(f"{prefix}（置信度 {confidence * 100:.0f}%）")
        lines.append(
            f"当前 effective≈{effective_s:.0f}s；剩余耗时多为业务必要等待"
            "（AUTO/战斗/过场），继续优化收益有限。"
        )
        if status == "near_ceiling_weak":
            lines.append("（无黑屏埋点，仅依 match 失败率等弱信号；不会自动跳过 AI 优化。）")
    elif status == "clear_room":
        lines.append("【仍有明显优化空间】")
        lines.append(
            f"当前 effective≈{effective_s:.0f}s；检测到较多脚本侧空转或识图开销，"
            "值得继续优化。"
        )
    else:
        lines.append("【部分可优化】")
        lines.append(
            f"当前 effective≈{effective_s:.0f}s；可能还有少量脚本侧收益，"
            "但业务等待占比已不低。"
        )

    if black_s >= 10:
        lines.append(f"另：墙钟黑屏约 {black_s:.0f}s（不计入 effective，无法靠脚本消除）。")

    if reasons:
        lines.append("")
        lines.append("依据：")
        for r in reasons[:6]:
            lines.append(f"· {r}")

    if prior and prior.get("has_prior"):
        lines.append("")
        lines.append(
            f"对比上次：{prior.get('prior_effective_s')}s → "
            f"{prior.get('current_effective_s')}s（{prior.get('delta_pct'):+.1f}%）"
        )

    if near_ceiling:
        lines.append("")
        lines.append(
            "建议：可停止反复优化；若仍要提速，优先改流程（减轮次/并行任务）"
            "而非继续压轮询间隔。"
        )

    return "\n".join(lines)


def format_ceiling_for_prompt(ceiling: dict[str, Any]) -> str:
    """注入 LLM prompt 的边界评估块。"""
    if not ceiling or ceiling.get("status") == "insufficient_data":
        return "(无边界评估)"
    lines = [
        f"status={ceiling.get('status')} near_ceiling={ceiling.get('near_ceiling')} "
        f"confidence={ceiling.get('confidence')}",
        ceiling.get("user_message") or "",
    ]
    metrics = ceiling.get("metrics") or {}
    if metrics:
        lines.append(f"metrics: {metrics}")
    if ceiling.get("near_ceiling"):
        lines.append(
            "If near_ceiling: prefer NO or MINIMAL code changes; "
            "state clearly in SUMMARY that further gains are mostly game/business wait."
        )
    return "\n".join(lines).strip()
