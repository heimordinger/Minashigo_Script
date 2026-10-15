"""工具结果哨兵：把"看起来就不对"的实测结果标出来，供模型追问。

起因（真实案例）：某张结界截图里 9 张对手卡只有 1 张有勋章，
``count_opponent_medals`` 因为"行列由命中聚类推断"，返回了 **1×1 网格 + 3 枚**。
数值本身没报错、也没抛异常，模型完全看不出问题——它没有任何东西提示
"这一栏本该是 9"。

哨兵只做规则判断，不依赖视觉模型：把不可能/可疑的组合翻译成一句人话，
塞进工具结果（``_sanity``）和会话的实测记录里，让模型有机会去追问或换做法。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 实测记录最多留多少条（状态摘要里只回灌最近几条）
_MAX_RECORDS = 12
# 回灌给模型时每条截断长度
_NOTE_MAX = 220
# 能与数量型标准答案对照的实测字段。都没有 = 这次实测没量数量，
# 不能判"什么都没认出"（crop_asset / inspect_asset 就是这种）。
_COUNT_KEYS = ("hits", "rows", "cols", "counts", "count", "best")


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def grid_sanity(data: dict[str, Any]) -> list[str]:
    """棋盘类结果的可疑点（纯规则，无视觉依赖）。"""
    reasons: list[str] = []
    if not isinstance(data, dict):
        return reasons
    rows = _to_int(data.get("rows"))
    cols = _to_int(data.get("cols"))
    hits = _to_int(data.get("hits"))
    if hits is None:
        raw_hits = data.get("hits")
        if isinstance(raw_hits, (list, tuple)):
            hits = len(raw_hits)

    if cols is not None and rows is not None:
        if cols <= 1 and (hits or 0) > 0:
            reasons.append(
                f"只认出 {rows} 行 {cols} 列，但确实命中了 {hits} 个目标。"
                "对手板通常是 3×3（个人）或 2×N（阴阳寮）；"
                "只有一处命中时行列聚类推不出网格——"
                "这不代表画面里只有一张卡。别据此判断「没有别的对手」。"
            )
        if rows == 0 or cols == 0:
            if (hits or 0) > 0:
                reasons.append(
                    f"网格是空的，却报出 {hits} 个命中，两者矛盾，先核对参数。"
                )
            else:
                reasons.append(
                    "一个目标都没认出。可能是空板，也可能是模板尺度/阈值不对——"
                    "两种情况的处理完全不同，别默认成空板。"
                )
    return reasons


def scale_sanity(data: dict[str, Any], *, tolerance: float = 0.15, min_gap: float = 0.03) -> list[str]:
    """尺度失配：实测尺度与 layout hint 差得多 → 强烈提示。"""
    reasons: list[str] = []
    if not isinstance(data, dict):
        return reasons
    # 已经命中就说明本次用的尺度是对的，hint 过时是配置问题、不是这一步的障碍。
    # 这时还喊"去跑 scale_sweep"只会把模型带偏（真实会话里它就是这么跑掉的）。
    if data.get("matched"):
        return reasons
    scale = _to_float(data.get("scale"))
    hint = _to_float(data.get("scale_hint"))
    if scale is None or hint is None or hint <= 0:
        return reasons
    delta = abs(scale - hint) / hint
    # 两个条件都要过：相对差够大，且绝对值不是四舍五入的噪声
    if delta > tolerance and abs(scale - hint) >= min_gap:
        peak_hint = _to_float(data.get("peak_at_hint"))
        peak_best = _to_float(data.get("peak_at_scale"))
        best = _to_float(data.get("best_scale"))
        thr = _to_float(data.get("threshold"))
        # 最佳尺度上也没对上：这不是 hint 过时，换任何尺度都一样。
        # 真实会话里模型被"hint 差了 67%"这句拖着去重定标，其实该查的是模板本身。
        if peak_best is not None and thr is not None and thr > 0 and peak_best < thr - 0.05:
            where = f"{best:.3f}" if best is not None else "最佳处"
            reasons.append(
                f"扫描范围内最佳尺度 {where} 的峰值也只有 {peak_best:.3f}"
                f"（阈值 {thr:.3f}）：换任何尺度都对不上。"
                "先别改 hint，去核对模板与目标是不是同一类画面"
                "（分辨率、裁切范围、是不是整窗截图）。"
            )
            return reasons
        detail = ""
        if peak_hint is not None and peak_best is not None:
            detail = f"（hint 峰值 {peak_hint:.3f} vs 最佳峰值 {peak_best:.3f}）"
        reasons.append(
            f"本次实际用的尺度 {scale:.3f} 和配置里的 hint {hint:.3f} 差了 "
            f"{delta * 100:.0f}%{detail}。配置 hint 可能过时（换分辨率/模拟器都会变），"
            "先跑 scale_sweep 确认，再决定改配置还是改调用。"
        )
    return reasons


def match_sanity(data: dict[str, Any]) -> list[str]:
    """模板匹配：没命中但分数贴着阈值 → 多半是阈值/尺度问题。"""
    reasons: list[str] = []
    if not isinstance(data, dict) or data.get("ok") is False:
        return reasons
    if data.get("matched"):
        return reasons
    score = _to_float(data.get("score"))
    thr = _to_float(data.get("threshold"))
    if score is not None and thr is not None and thr > 0 and score >= thr - 0.05:
        reasons.append(
            f"没命中，但最高分 {score:.3f} 已经贴着阈值 {thr:.3f}。"
            "先试 scale_sweep（尺度不对最像这种表现），不要直接下结论「画面里没有」。"
        )
    return reasons


def multi_match_sanity(data: dict[str, Any]) -> list[str]:
    """多目标匹配：候选顶格、分数扎堆、或模板本身缺信息量时提醒。"""
    if not isinstance(data, dict) or data.get("ok") is False:
        return []
    reasons: list[str] = []
    extra = data.get("reasons")
    if isinstance(extra, list):
        reasons.extend(str(x) for x in extra if str(x).strip())
    if data.get("ambiguous") and not reasons:
        reasons.append(
            str(data.get("warning") or "多目标候选过多或分数过于接近，先验证模板信息量。")
        )
    if data.get("count_warning"):
        reasons.append(str(data["count_warning"]))
    # 不再因为"命中多但没给 expected_count"发提醒：本项目的界面本来就是规则网格
    # （结界 3 行卡 × 5 槽 = 45 格），命中 42 个可能是完全正确的空格数。
    return reasons


def probe_sanity(data: dict[str, Any]) -> list[str]:
    """探针脚本：跑成功但没给结论，或者结论与实测记录无关。"""
    reasons: list[str] = []
    if not isinstance(data, dict) or not data.get("ok"):
        return reasons
    result = data.get("result")
    if result is None:
        reasons.append(
            "探针跑完了但 result 是空的。把要看的数字赋给 result，"
            "否则这一轮等于没测（系统只在 result/stdout 里看结论）。"
        )
    return reasons


# 同一张图、同一工具连续几次没有 result 就判空转
_SPIN_LIMIT = 3
_EMPTY_RESULTS = frozenset({"", "none", "null", "{}", "[]"})


def _has_result(row: dict[str, Any]) -> bool:
    raw = row.get("result")
    if raw is None:
        return False
    return str(raw).strip().lower() not in _EMPTY_RESULTS


def repeat_without_result(session: Any, tool: str, *, limit: int = _SPIN_LIMIT) -> str:
    """同一图片、同一工具连续跑了几次都没产出 result → 返回"别原样重跑"的硬闸说明。

    软提示（``probe_sanity``）只在事后说"这轮等于没测"，模型照样重跑；
    实况里同一张标注图在 4 秒内被跑了 4 次。这里在**执行前**拦下。
    """
    art = getattr(session, "artifacts", None)
    records = art.get("measurements") if isinstance(art, dict) else None
    if not isinstance(records, list) or not records:
        return ""
    scope = _current_image_scope(session)
    if not any(scope.values()):
        return ""
    matching = [
        r
        for r in records
        if isinstance(r, dict)
        and _same_scope(r, scope)
        and str(r.get("tool") or "") == str(tool)
    ]
    streak = 0
    for row in reversed(matching):
        if _has_result(row):
            break
        streak += 1
    if streak < max(1, limit):
        return ""
    return (
        f"这张图片上 {tool} 已经连续跑了 {streak} 次都没有产出 result——原样重跑没有意义。"
        "先改一处再跑：要么把要看的数字赋给 result（系统只从 result/stdout 里看结论），"
        "要么换假设、换搜索区域/阈值/模板，或改用别的工具。"
    )


def sanitize_tool_result(
    name: str,
    data: Any,
    *,
    session: Any = None,
) -> dict[str, Any]:
    """给工具结果加 ``_sanity``，并把实测值记进会话。

    只加字段、不改原值：模型看到的数字仍是工具真实返回。
    """
    payload = dict(data) if isinstance(data, dict) else {"ok": False, "raw": data}
    reasons: list[str] = []
    if name in ("analyze_current_frame", "multi_count", "count_opponent_medals"):
        reasons += grid_sanity(payload)
    if name in ("match_template", "analyze_current_frame", "count_opponent_medals"):
        reasons += scale_sanity(payload)
    if name == "match_template":
        reasons += match_sanity(payload)
    if name == "match_template_multi":
        reasons += multi_match_sanity(payload)
    if name == "run_probe_script":
        reasons += probe_sanity(payload)

    if reasons:
        payload["_sanity"] = {            "suspicious": True,
            "reasons": reasons[:3],
            "note": "这些是系统按规则标出的疑点，不是结论；先验证再决定动作。",
        }
    if session is not None:
        remember_measurement(session, name=name, data=payload, reasons=reasons)
    return payload


def remember_measurement(
    session: Any,
    *,
    name: str,
    data: dict[str, Any],
    reasons: Optional[list[str]] = None,
) -> None:
    """把一次实测的关键数字存进会话，供后续轮次和状态摘要使用。"""
    if session is None:
        return
    art = getattr(session, "artifacts", None)
    if not isinstance(art, dict):
        try:
            session.artifacts = {}
            art = session.artifacts
        except Exception:
            return
    keys = (
        "rows",
        "cols",
        "hits",
        "counts",
        "best",
        "matched",
        "score",
        "threshold",
        "scale",
        "scale_hint",
        "peak_at_hint",
        "peak_at_scale",
        "result",
        "count",
        "expected_count",
        "ambiguous",
        "saturated",
    )
    scope = _current_image_scope(session)
    row: dict[str, Any] = {"tool": str(name), "at": _now_iso(), **scope}
    for k in keys:
        if k in data and data[k] is not None:
            v = data[k]
            row[k] = v if isinstance(v, (int, float, bool)) else str(v)[:120]
    if reasons:
        row["suspicious"] = reasons[0][:160]
    meaningful = any(k in row for k in keys) or bool(reasons)
    records = art.get("measurements")
    if not isinstance(records, list):
        records = []
    records.append(row)
    art["measurements"] = records[-_MAX_RECORDS:]
    if not meaningful:
        return
    try:
        from backend.script_generator.collaborator.resource_registry import (
            register_record_resource,
        )

        register_record_resource(
            session,
            resource_type="evidence",
            kind="measurement",
            name=str(name),
            payload=row,
            derived_from=[scope["scope_asset_id"]] if scope.get("scope_asset_id") else [],
        )
    except Exception:
        pass


def measurements_block(session: Any, *, limit: int = 4) -> str:
    """状态摘要里回灌的实测记录（最近几条）。"""
    art = getattr(session, "artifacts", None)
    records = art.get("measurements") if isinstance(art, dict) else None
    if not isinstance(records, list) or not records:
        return ""
    scope = _current_image_scope(session)
    scoped = [r for r in records if isinstance(r, dict) and _same_scope(r, scope)]
    if not scoped:
        return ""
    lines = ["【当前图片的系统实测记录】（仅限这张图片；可能已过时）"]
    for row in scoped[-max(1, limit):]:
        if not isinstance(row, dict):
            continue
        bits = []
        for k in ("rows", "cols", "hits", "counts", "best", "matched", "score", "scale"):
            if k in row:
                bits.append(f"{k}={row[k]}")
        line = f"- {row.get('tool')}: " + " ".join(bits)
        if row.get("suspicious"):
            line += f"  ⚠ {str(row['suspicious'])[:_NOTE_MAX]}"
        lines.append(line)
    return "\n".join(lines)


# ── 用户说法（按图片作用域；不是 ground truth）──────────────────────

_MAX_ANSWERS = 8
_ANSWER_MAX = 300

# 同一"指标"的识别：靠场景词 + 是否给了一串数量。场景词不同 = 不同指标。
_PLACE_RE = re.compile(r"结界\s*\d+|当前界面|素材区|共同视口|画面|窗口")
_NEGATION_RE = re.compile(r"不是|不对|错了|作废|推翻|别管|忽略|不算|别用|不用管|没这回事")
_RULE_RE = re.compile(r"固定|规则|代表|从左往右|依次填|每张.{0,8}(槽|格)|如果|否则|剩下")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_path(value: Any) -> str:
    try:
        return str(Path(value).resolve()).replace("\\", "/").lower()
    except Exception:
        return str(value or "").replace("\\", "/").lower()


def _current_image_scope(session: Any) -> dict[str, str]:
    """把记录钉在当前共同视口的文件上；hash 防止同路径内容被替换。"""
    fp = getattr(session, "frame_path", None)
    if not fp:
        return {"scope_path": "", "scope_asset_id": "", "scope_image_hash": ""}
    path = _norm_path(fp)
    asset_id = ""
    for asset in list(getattr(session, "assets", None) or []):
        if not isinstance(asset, dict):
            continue
        if _norm_path(asset.get("path") or asset.get("file_path")) == path:
            asset_id = str(asset.get("id") or asset.get("asset_id") or "")
            break
    digest = ""
    try:
        p = Path(fp)
        if p.is_file():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:20]
    except Exception:
        pass
    return {"scope_path": path, "scope_asset_id": asset_id, "scope_image_hash": digest}


def _same_scope(row: dict[str, Any], scope: dict[str, str]) -> bool:
    if row.get("scope_image_hash") and scope.get("scope_image_hash"):
        return row["scope_image_hash"] == scope["scope_image_hash"]
    if row.get("scope_asset_id") and scope.get("scope_asset_id"):
        return row["scope_asset_id"] == scope["scope_asset_id"]
    return bool(row.get("scope_path") and row.get("scope_path") == scope.get("scope_path"))


def _answer_subjects(text: str) -> list[str]:
    return sorted({m.group(0).replace(" ", "") for m in _PLACE_RE.finditer(str(text or ""))})


def _answer_numbers(text: str) -> list[int]:
    import re as _re

    out = []
    for m in _re.finditer(r"\d+", str(text or "")):
        try:
            out.append(int(m.group(0)))
        except ValueError:
            continue
    return out


def _same_metric(a: str, b: str) -> bool:
    """两条答案是否在说同一个指标（同场景 + 都给数量）。"""
    sa, sb = set(_answer_subjects(a)), set(_answer_subjects(b))
    if not sa or not sb or not (sa & sb):
        return False
    return bool(_answer_numbers(a)) and bool(_answer_numbers(b))


def remember_answer(session: Any, text: str) -> Optional[str]:
    """兼容旧字段名：保存为当前图片的用户说法，而非权威标准答案。"""
    value = str(text or "").strip()
    if not value or session is None or _RULE_RE.search(value) or not _answer_numbers(value):
        return None
    value = value[:_ANSWER_MAX]
    art = getattr(session, "artifacts", None)
    if not isinstance(art, dict):
        try:
            session.artifacts = {}
            art = session.artifacts
        except Exception:
            return None
    scope = _current_image_scope(session)
    # 没有图片时不能赋予图像结论作用域；宁可不记，也不能污染之后的图片。
    if not any(scope.values()):
        return None
    claims = art.get("user_claims")
    if not isinstance(claims, list):
        claims = []
    claims = [x for x in claims if isinstance(x, dict) and str(x.get("text") or "").strip()]
    if any(str(x.get("text")) == value and _same_scope(x, scope) for x in claims):
        return value
    claims = [
        x for x in claims
        if not (_same_scope(x, scope) and _same_metric(str(x.get("text") or ""), value))
    ]
    claims.append({"text": value, "source": "user_claim", "verified": False,
                   "created_at": _now_iso(), **scope})
    art["user_claims"] = claims[-_MAX_ANSWERS:]
    try:
        from backend.script_generator.collaborator.resource_registry import (
            register_record_resource,
        )

        register_record_resource(
            session,
            resource_type="claim",
            kind="user_claim",
            name="当前图片的用户说法",
            payload=claims[-1],
            derived_from=[scope["scope_asset_id"]] if scope.get("scope_asset_id") else [],
        )
    except Exception:
        pass
    # 旧版全局答案不再参与任何判断，保留归档便于诊断。
    legacy = art.pop("ground_truth", None)
    if isinstance(legacy, list) and legacy:
        art["legacy_ground_truth"] = legacy[-_MAX_ANSWERS:]
    return value


def retire_negated_answers(session: Any, user_text: str) -> list[str]:
    """用户明确否掉某个场景的答案时，把那条作废。返回被移走的条目。"""
    text = str(user_text or "")
    art = getattr(session, "artifacts", None)
    answers = art.get("user_claims") if isinstance(art, dict) else None
    if not text or not _NEGATION_RE.search(text) or not isinstance(answers, list):
        return []
    said = set(_answer_subjects(text))
    if not said:
        return []
    kept, removed = [], []
    scope = _current_image_scope(session)
    for a in answers:
        s = str(a.get("text") or "") if isinstance(a, dict) else str(a)
        if isinstance(a, dict) and _same_scope(a, scope) and set(_answer_subjects(s)) & said:
            removed.append(s)
        else:
            kept.append(s)
    if removed:
        art["user_claims"] = kept
    return removed



def ground_truth_entries(session: Any) -> list[str]:
    """兼容旧 API：只返回当前图片绑定的用户说法。"""
    art = getattr(session, "artifacts", None)
    claims = art.get("user_claims") if isinstance(art, dict) else None
    if not isinstance(claims, list):
        return []
    scope = _current_image_scope(session)
    return [str(x.get("text")) for x in claims if isinstance(x, dict) and _same_scope(x, scope)]


def ground_truth_block(session: Any) -> str:
    """兼容旧 API：返回非权威、当前图片限定的用户说法块。"""
    entries = ground_truth_entries(session)
    if not entries:
        if getattr(session, "frame_path", None):
            return ("【当前图片结论】未知：没有当前图片的已验证结果。"
                    "其他图片的说法不得套用；先独立测量，测不到就如实说未知。")
        return ""
    art = getattr(session, "artifacts", None)
    claims = art.get("user_claims") if isinstance(art, dict) else []
    records = art.get("measurements") if isinstance(art, dict) else []
    scope = _current_image_scope(session)
    current_claims = [x for x in (claims or []) if isinstance(x, dict) and _same_scope(x, scope)]
    newest_claim_at = max((str(x.get("created_at") or "") for x in current_claims), default="")
    independently_measured = any(
        isinstance(row, dict)
        and _same_scope(row, scope)
        and str(row.get("at") or "") >= newest_claim_at
        and any(k in row for k in _COUNT_KEYS)
        for row in (records or [])
    )
    if not independently_measured:
        return (
            "【当前图片有用户说法待核验】先独立测量；在得到当前图片的新实测前，"
            "不要把用户给出的数字当作参数、答案或调参目标。测不到就回答未知。"
        )
    lines = ["【当前图片的用户说法】（仅供独立测量后对照，不是标准答案）"]
    lines += [f"- {e}" for e in entries]
    lines.append(
        "先按画面和工具证据盲测，再比较这条说法；不一致时报告不一致并继续核验，"
        "不得为迎合说法调参，也不得复制到别的图片。没有证据时结论就是未知。"
    )
    return "\n".join(lines)


def _numbers_in(text: str) -> list[int]:
    """文本里出现的整数（用于把"实际是 3 枚"和实测 count 对上）。"""
    import re

    out: list[int] = []
    for m in re.finditer(r"\d+", str(text or "")):
        try:
            raw = m.group(0)
            # 槽位分布串（如 003000000）是逐位数量，不是三百万。
            if len(raw) >= 4 and (raw.startswith("0") or len(set(raw)) <= 4):
                out.extend(int(ch) for ch in raw)
            else:
                out.append(int(raw))
        except ValueError:
            continue
    return out


def ground_truth_conflict(session: Any) -> str:
    """旧版硬闸已废弃：用户说法不能否决实测或完成条件。"""
    return ""

