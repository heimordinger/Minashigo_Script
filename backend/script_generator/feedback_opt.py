"""Classify and compress trial-run feedback before writing it into 脚本解释."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace


@dataclass
class FeedbackCandidate:
    kind: str  # constraint | oneoff | duplicate
    original: str
    rewritten: str


_ONEOFF_RE = re.compile(
    r"超时|timeout|cdp|websocket|ws断开|网络|连不上|崩溃|端口|断开|"
    r"chrome.?error|导航被中断|卡死了|卡住了|闪退",
    re.I,
)
_CONSTRAINT_RE = re.compile(
    r"必须|禁止|不要|不能|wait_image|优先|阈值|threshold|退出|结束|"
    r"未知|标识|转场|nav_threshold|__exit__|任务完成",
    re.I,
)
_IMG_RE = re.compile(r"([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g))|_img\(|\b(rank|logo)\b", re.I)
_CLICK_NO_REACT_RE = re.compile(r"(点(?:击|了)|click).{0,24}(没反应|没转|没跳|卡住|无反应)", re.I)
_UNKNOWN_ON_HIT_RE = re.compile(r"(命中|匹配成功|识别到).{0,16}未知|return\s*['\"]未知", re.I)
_EXIT_RE = re.compile(
    r"(出现.{0,12}就.{0,6}(结束|完成|退出)|rank.{0,8}(结束|完成)|"
    r"脚本结束|任务结束|任务完成|本任务完成|本任务结束)",
    re.I,
)
_STEP_END_RE = re.compile(
    r"本步骤结束|结束该任务|结束本步骤|辅助.?结束|"
    r"(已在主界面|已在出击|匹配.?rank|匹配.?出击_logo).{0,24}(结束|完成)",
    re.I,
)
_BUSINESS_EXIT_HINT_RE = re.compile(
    r"本任务|jjc_end|收取奖励|room_ok|耗尽|无奖励|领完|次数用完",
    re.I,
)
_HOME_NAV_RE = re.compile(r"返回主界面|先回(到)?主界面|go_home", re.I)
_WHOLE_SCRIPT_DONE_RE = re.compile(r"整个脚本|脚本结束|全部任务|do_work.{0,8}结束", re.I)

TRIAL_FEEDBACK_HEADER = "试运行反馈（生成时必须遵守）"
_NAV_TH_RE = re.compile(r"(阈值|threshold|nav_threshold|logo|标识图)", re.I)
_LOG_DETAIL_RE = re.compile(
    r"日志|script_log|看不出|卡在哪|哪一步|不够详细|不知道.*(哪|状态|步骤)",
    re.I,
)
_STALE_FRAME_RE = re.compile(
    r"旧帧|旧画面|没刷新|update_frame|截图缓存|还是之前|点完.*(仍|还|又).*(看见|匹配)",
    re.I,
)


def extract_trial_constraints(explanation: str) -> list[str]:
    """从介绍末尾的「试运行反馈」区块抽出约束句（去掉日期前缀）。"""
    text = explanation or ""
    marker = f"## {TRIAL_FEEDBACK_HEADER}"
    idx = text.find(marker)
    if idx < 0:
        idx = text.find(TRIAL_FEEDBACK_HEADER)
        if idx < 0:
            return []
        rest = text[idx + len(TRIAL_FEEDBACK_HEADER) :]
    else:
        rest = text[idx + len(marker) :]
    nxt = re.search(r"\n## ", rest)
    if nxt:
        rest = rest[: nxt.start()]
    out: list[str] = []
    for ln in rest.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        ln = re.sub(r"^[-*•]\s*", "", ln)
        ln = re.sub(r"^\d{4}-\d{2}-\d{2}：", "", ln)
        if ln:
            out.append(_sanitize_constraint(ln))
    return out


def _sanitize_constraint(text: str) -> str:
    """纠正「看到 rank 就 __exit__」这类会跳过任务的旧约束。"""
    t = (text or "").strip()
    if "__exit__" in t and re.search(r"rank", t, re.I) and not _WHOLE_SCRIPT_DONE_RE.search(t):
        return (
            "必须：仅本任务业务完成时 return '__exit__'；"
            "「本步骤结束」（出现 rank / 导航成功）须 return '主界面'，禁止 __exit__"
        )
    return t


def hoist_trial_constraints(explanation: str) -> str:
    """把既往试运行约束提到全文最前，避免被长流程淹没。"""
    expl = explanation or ""
    bullets = extract_trial_constraints(expl)
    if not bullets:
        return expl
    block = (
        "## HARD CONSTRAINTS from previous trial runs "
        "(MUST implement; if they conflict with earlier flow text, follow THESE)\n"
        + "\n".join(f"- {b}" for b in bullets)
    )
    return block + "\n\n" + expl


def revise_checklist(feedback: str, explanation: str = "") -> list[str]:
    """本次反馈压句 + 既往约束，作为修订/审查清单。"""
    distilled = distill_feedback(feedback, explanation)
    items: list[str] = []
    seen: set[str] = set()
    for it in distilled:
        if it.kind == "duplicate":
            continue
        text = (it.rewritten or it.original).strip()
        if not text:
            continue
        if it.kind == "oneoff":
            text = f"（可能一次性故障，仍请对照日志排查）{text}"
        if text in seen:
            continue
        seen.add(text)
        items.append(text)
    if not items and (feedback or "").strip():
        items = [(feedback or "").strip()]
    for past in extract_trial_constraints(explanation):
        if any(_similar_constraint(past, x) for x in items):
            continue
        items.append(f"（既往约束）{past}")
    return items


def _similar_constraint(a: str, b: str) -> bool:
    def _core(s: str) -> str:
        s = re.sub(r"（[^）]*）", "", s)
        s = re.sub(r"^必须：|^禁止：", "", s.strip())
        return re.sub(r"\s+", "", s)
    ca, cb = _core(a), _core(b)
    if not ca or not cb:
        return False
    return ca in cb or cb in ca


def distill_feedback(feedback: str, explanation: str = "") -> list[FeedbackCandidate]:
    """Split feedback into compressed candidates with a default writeback kind."""
    from backend.script_generator.agent import _split_feedback_items

    items = _split_feedback_items(feedback or "")
    expl = explanation or ""
    out: list[FeedbackCandidate] = []
    seen_rw: set[str] = set()
    for raw in items:
        text = re.sub(r"^[-*•]\s*", "", (raw or "").strip())
        text = re.sub(r"^[\d]+[\.\)、]\s*", "", text)
        if not text:
            continue
        rewritten, _ = _rewrite(text)
        rewritten = _sanitize_constraint(rewritten)
        kind = _classify(text, rewritten, expl)
        key = rewritten.strip()
        if key in seen_rw:
            kind = "duplicate"
        seen_rw.add(key)
        out.append(FeedbackCandidate(kind=kind, original=text, rewritten=rewritten))
    return out


def _classify(original: str, rewritten: str, explanation: str) -> str:
    body = rewritten.split("：", 1)[-1].strip() if "：" in rewritten else rewritten
    if body and (body in explanation or rewritten in explanation or original in explanation):
        return "duplicate"
    if _CONSTRAINT_RE.search(original) or _IMG_RE.search(original) or rewritten.startswith(("必须", "禁止")):
        if _ONEOFF_RE.search(original) and not _IMG_RE.search(original) and not _CONSTRAINT_RE.search(original):
            return "oneoff"
        return "constraint"
    if _ONEOFF_RE.search(original):
        return "oneoff"
    if len(original) <= 24 and not _IMG_RE.search(original):
        return "oneoff"
    return "constraint"


def _rewrite(text: str) -> tuple[str, bool]:
    """Return (rewritten, used_template). used_template=False → 建议走 LLM 压句。"""
    img = _first_img(text)
    if _CLICK_NO_REACT_RE.search(text):
        target = img or "该按钮"
        return f"必须：点击 {target} 后 wait_image 确认转场，失败则 return 未知", True
    if _UNKNOWN_ON_HIT_RE.search(text):
        return "禁止：unknown_state 命中场景 id 后 return 未知；应返回业务状态名", True
    if _HOME_NAV_RE.search(text) and not _WHOLE_SCRIPT_DONE_RE.search(text):
        prefix = ""
        if re.search(r"房间|领(取)?体力|收取奖励", text):
            prefix = "房间任务须先「返回主界面」，成功后再由「主界面」点击房间。"
        return (
            f"必须：{prefix}返回主界面成功（出现 rank）后 return '主界面'（本步骤结束），"
            "禁止在导航态 return '__exit__'（会跳过后续任务步骤）",
            True,
        )
    # 辅助/导航「结束」优先于笼统「任务结束」
    if _STEP_END_RE.search(text) and not _BUSINESS_EXIT_HINT_RE.search(text):
        return (
            "必须：导航/辅助成功为「本步骤结束」，return 业务态名（如 '主界面'/'出击界面'），"
            "禁止 return '__exit__'",
            True,
        )
    if _EXIT_RE.search(text):
        if _WHOLE_SCRIPT_DONE_RE.search(text):
            return "必须：整段脚本完成条件满足后 return '__exit__'", True
        return (
            "必须：仅「本任务完成」时 return '__exit__'；"
            "「本步骤结束」（导航到主界面/出击界面成功）须 return 业务态名，禁止 __exit__",
            True,
        )
    if _NAV_TH_RE.search(text) and re.search(r"logo|rank|标识|阈值", text, re.I):
        return "必须：场景 id（logo / rank / *_logo）的 match_image 使用 CFG.nav_threshold", True
    if _LOG_DETAIL_RE.search(text):
        return (
            "必须：各状态 handler 在入口及 click_image / wait_image 前后 "
            "script_log 当前状态与目标图/结果",
            True,
        )
    if _STALE_FRAME_RE.search(text):
        return (
            "运行时会在 click/b_sleep 后自动失效帧，match_image 前自动补帧；"
            "一般不必再手写 update_frame。需要更高持续识别频率时用 "
            "await browser.request_fps(hz)",
            True,
        )
    compact = re.sub(r"\s+", " ", text).strip()
    compact = re.sub(r"^(感觉|好像|可能|就是|然后)", "", compact)
    if len(compact) > 80:
        compact = compact[:80] + "…"
    if compact.startswith(("必须", "禁止", "不要", "不能")):
        return _sanitize_constraint(compact), True
    return f"必须：{compact}", False


def _needs_llm_rewrite(item: FeedbackCandidate) -> bool:
    if item.kind != "constraint":
        return False
    _, used_template = _rewrite(item.original)
    return not used_template


async def optimize_feedback_rewrites(
    items: list[FeedbackCandidate],
    explanation: str = "",
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: str | None = None,
) -> list[FeedbackCandidate]:
    """用 LLM 把口语反馈压成可写进脚本介绍的约束句；失败则保留原 rewrite。"""
    if not items or not (api_key or "").strip() or not (model or "").strip():
        return items

    indices = [i for i, it in enumerate(items) if _needs_llm_rewrite(it)]
    if not indices:
        return items

    payload = [
        {
            "index": i,
            "kind": items[i].kind,
            "original": items[i].original,
            "draft": items[i].rewritten,
        }
        for i in indices
    ]
    expl_excerpt = (explanation or "").strip()
    if len(expl_excerpt) > 1200:
        expl_excerpt = expl_excerpt[:1200] + "…"

    system = (
        "You rewrite Minashigo automation trial-run feedback into durable script-spec constraints.\n"
        "Output ONLY a JSON array, same length and order as input items.\n"
        'Each element: {"rewritten": "..."}.\n'
        "Rules:\n"
        "- Use 必须 or 禁止 prefix for durable logic/logging/threshold constraints.\n"
        "- Third person, imperative, actionable; mention script_log / wait_image / nav_threshold when relevant.\n"
        "- One sentence, <= 72 Chinese chars when possible.\n"
        "- Do NOT copy user complaint verbatim; do NOT keep first-person (我/看不出).\n"
        "- For environment glitches (network, crash, timeout, CDP), set rewritten to empty string.\n"
        "- If draft is already good, you may return it polished."
    )
    user = (
        "## Existing script explanation (avoid duplicates)\n"
        f"{expl_excerpt or '(empty)'}\n\n"
        "## Items to rewrite\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    from backend.script_generator.agent import call_llm

    try:
        raw, _, _ = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
            system_prompt=system,
            max_tokens=1024,
        )
    except Exception as e:
        print(f"[feedback_opt] LLM rewrite failed: {e}")
        return items

    parsed = _parse_rewrite_response(raw, len(indices))
    if not parsed:
        return items

    out = list(items)
    for j, idx in enumerate(indices):
        new_text = (parsed[j] or "").strip()
        if not new_text:
            continue
        if not new_text.startswith(("必须", "禁止")):
            new_text = f"必须：{new_text.lstrip('：:')}"
        out[idx] = replace(out[idx], rewritten=new_text)
    return out


def optimize_feedback_sync(
    items: list[FeedbackCandidate],
    explanation: str = "",
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: str | None = None,
) -> list[FeedbackCandidate]:
    """GUI 同步入口：在无 event loop 时跑 optimize_feedback_rewrites。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # 不应在已运行的 loop 里阻塞；GUI 调用前请确保无 running loop
        return items
    return asyncio.run(
        optimize_feedback_rewrites(
            items,
            explanation,
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
        )
    )


def _parse_rewrite_response(raw: str, expected: int) -> list[str] | None:
    text = (raw or "").strip()
    if not text:
        return None
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or len(data) != expected:
        return None
    out: list[str] = []
    for row in data:
        if isinstance(row, dict):
            out.append(str(row.get("rewritten") or ""))
        elif isinstance(row, str):
            out.append(row)
        else:
            return None
    return out


def _first_img(text: str) -> str:
    m = re.search(r"([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g))", text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"_img\(\s*['\"]([^'\"]+)['\"]", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(rank|logo|[A-Za-z0-9_]+_logo)\b", text, re.I)
    if m:
        return m.group(1)
    return ""
