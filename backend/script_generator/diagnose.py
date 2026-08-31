"""试跑失败诊断：规则 + LLM 结构化 gap，供修订 prompt 注入。"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TrialDiagnosis:
    symptom: str = ""
    root_cause: str = ""
    must_fix: list[str] = field(default_factory=list)
    do_not: list[str] = field(default_factory=list)
    code_hints: list[str] = field(default_factory=list)
    source: str = "local"  # local | llm | merged
    need_vision: Optional[bool] = None
    vision_reason: str = ""
    frame_caption: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symptom": self.symptom,
            "root_cause": self.root_cause,
            "must_fix": list(self.must_fix),
            "do_not": list(self.do_not),
            "code_hints": list(self.code_hints),
            "source": self.source,
            "need_vision": self.need_vision,
            "vision_reason": self.vision_reason,
            "frame_caption": self.frame_caption,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrialDiagnosis:
        if not isinstance(data, dict):
            return cls()
        nv = data.get("need_vision")
        if nv is not None:
            nv = bool(nv)
        return cls(
            symptom=str(data.get("symptom") or ""),
            root_cause=str(data.get("root_cause") or ""),
            must_fix=[str(x) for x in (data.get("must_fix") or []) if str(x).strip()],
            do_not=[str(x) for x in (data.get("do_not") or []) if str(x).strip()],
            code_hints=[str(x) for x in (data.get("code_hints") or []) if str(x).strip()],
            source=str(data.get("source") or "llm"),
            need_vision=nv,
            vision_reason=str(data.get("vision_reason") or ""),
            frame_caption=str(data.get("frame_caption") or ""),
        )


_ROOM_HANDLER_RE = re.compile(r"room|claim|领", re.I)
_POPUP_FB_RE = re.compile(r"ok|弹窗|奖励窗|没点|未点|卡住", re.I)
_NO_REWARD_LOG_RE = re.compile(r"no reward|没有.*收取|无.*奖励|任务完成", re.I)
_HOME_FB_RE = re.compile(r"返回主界面|先回主界面|go_home", re.I)
_VAGUE_FB_RE = re.compile(r"老问题|又挂了|还是|依旧|同样|房间|领取|日常", re.I)

# 试跑日志事件（匹配 / 点击）
_LOG_CLICK_RE = re.compile(
    r"点击图片[:：].*?([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g))",
    re.I,
)
_LOG_MATCH_FAIL_RE = re.compile(
    r"(?:匹配无结果|❌\s*匹配无结果|score=0\.0).*?"
    r"|图片匹配\[.*?([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g))\].*?\(None,\s*None\)",
    re.I,
)
_LOG_MATCH_LINE_RE = re.compile(
    r"(?:开始匹配|图片匹配\[|转换为Path:).*?([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g))",
    re.I,
)
_LOG_MATCH_OK_RE = re.compile(r"✅\s*匹配成功|匹配成功:", re.I)
_LOG_MATCH_FAIL_MARK_RE = re.compile(
    r"❌\s*匹配无结果|匹配无结果|\(None,\s*None\).*?0\.0|score=0\.0",
    re.I,
)
_LOG_FRAME_TS_RE = re.compile(r"_frame_ts=([0-9]+(?:\.[0-9]+)?)")
_NAV_STEMS = (
    "出击_logo", "出击.png", "home.png", "rank.png",
    "jjc.png", "jjc_logo", "ta.png", "ta_logo",
)
_CLAIM_STEM = "收取奖励"
_OK_STEM = "room_ok"

# 代码：无 ok + 收取还在 → 当成功结束
_FALSE_COMPLETE_CODE_RE = re.compile(
    r"not\s+await\s+browser\.match_image\([^)]*room_ok[\s\S]{0,400}?"
    r"await\s+browser\.match_image\([^)]*收取奖励[\s\S]{0,200}?"
    r"return\s+(?:True|['\"]__exit__['\"])",
    re.I,
)
_FALSE_COMPLETE_LOG_RE = re.compile(
    r"仍见收取|视为无法领取|没有 room_ok|未出现 room_ok|领取奖励完成",
    re.I,
)


def _stem_of(path_or_name: str) -> str:
    name = (path_or_name or "").replace("\\", "/").split("/")[-1]
    return name.lower()


def _is_claim_stem(stem: str) -> bool:
    s = _stem_of(stem)
    return _CLAIM_STEM in s or s.startswith("room_收取")


def _is_ok_stem(stem: str) -> bool:
    s = _stem_of(stem)
    return _OK_STEM in s


def _is_nav_stem(stem: str) -> bool:
    s = _stem_of(stem)
    return any(n.lower() in s for n in _NAV_STEMS)


@dataclass
class LogEvent:
    kind: str  # click | match_ok | match_fail
    stem: str
    line_no: int = 0


def parse_trial_log_events(trial_log: str, *, limit: int = 80) -> list[LogEvent]:
    """从试跑/终端日志抽出 click / match 成败序列（规则，零 token）。"""
    events: list[LogEvent] = []
    lines = (trial_log or "").splitlines()
    pending_stem = ""
    for i, ln in enumerate(lines):
        m_click = _LOG_CLICK_RE.search(ln)
        if m_click:
            events.append(LogEvent("click", _stem_of(m_click.group(1)), i))
            pending_stem = ""
            continue
        m_stem = _LOG_MATCH_LINE_RE.search(ln)
        if m_stem:
            pending_stem = _stem_of(m_stem.group(1))
        if _LOG_MATCH_OK_RE.search(ln) and pending_stem:
            events.append(LogEvent("match_ok", pending_stem, i))
            pending_stem = ""
            continue
        if _LOG_MATCH_FAIL_MARK_RE.search(ln):
            stem = pending_stem
            if not stem:
                m2 = re.search(
                    r"([A-Za-z0-9_\u4e00-\u9fff\-]+\.(?:png|jpe?g))",
                    ln,
                    re.I,
                )
                if m2:
                    stem = _stem_of(m2.group(1))
            if stem:
                events.append(LogEvent("match_fail", stem, i))
                pending_stem = ""
            continue
    if len(events) > limit:
        return events[-limit:]
    return events


def summarize_event_chain(events: list[LogEvent], *, max_n: int = 12) -> str:
    if not events:
        return ""
    # 优先从最近一次「收取」动作起截，避免长日志把关键前缀挤掉
    start = max(0, len(events) - max_n)
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if ev.kind in ("click", "match_ok") and _is_claim_stem(ev.stem):
            start = i
            break
    window = events[start : start + max_n]
    if not window:
        window = events[-max_n:]
    parts = []
    for ev in window:
        tag = {"click": "click", "match_ok": "ok", "match_fail": "fail"}.get(ev.kind, ev.kind)
        parts.append(f"{tag}:{ev.stem}")
    return " → ".join(parts)


def frame_timestamps_advancing(trial_log: str) -> Optional[bool]:
    """若日志含 _frame_ts：有递增则 True（帧在刷新），仅一条则 None。"""
    vals = [float(x) for x in _LOG_FRAME_TS_RE.findall(trial_log or "")]
    if len(vals) < 2:
        return None
    return max(vals) - min(vals) > 1e-3


def detect_claim_ok_miss_then_nav(events: list[LogEvent]) -> bool:
    """点收取成功后 room_ok 连续失败，随后出现出击/home/rank 等导航图。"""
    n = len(events)
    for i, ev in enumerate(events):
        claim_click = ev.kind == "click" and _is_claim_stem(ev.stem)
        claim_ok = ev.kind == "match_ok" and _is_claim_stem(ev.stem)
        if not (claim_click or claim_ok):
            continue
        # 若是 match_ok 收取，后面常紧跟 click 收取；从该点往后看
        ok_fails = 0
        saw_nav = False
        for j in range(i + 1, min(i + 25, n)):
            e2 = events[j]
            if _is_ok_stem(e2.stem) and e2.kind == "match_fail":
                ok_fails += 1
                continue
            if _is_ok_stem(e2.stem) and e2.kind in ("match_ok", "click"):
                break  # 已经点到 ok，不是本模式
            if ok_fails >= 1 and _is_nav_stem(e2.stem):
                saw_nav = True
                break
            if ok_fails >= 1 and e2.kind == "click" and _is_nav_stem(e2.stem):
                saw_nav = True
                break
        if ok_fails >= 1 and saw_nav:
            return True
        # 宽松：收取 click 后至少 2 次 room_ok fail（即使尚未看到 nav）
        if claim_click and ok_fails >= 2:
            return True
    return False


def detect_false_complete_claim_branch(code: str) -> list[str]:
    """代码里「无 room_ok 且收取还在 → return True/__exit__」假完成分支。"""
    hits: list[str] = []
    if _FALSE_COMPLETE_CODE_RE.search(code or ""):
        hits.append(
            "房间领取：存在「无 room_ok 且仍见 room_收取奖励 → 直接结束」分支；"
            "点收取后须 wait/循环处理 room_ok（或明确 room_ap上限），"
            "禁止短窗口内据此假成功结束"
        )
    # 逐步扫描 handler，给出函数名
    for hname, hsrc in _extract_room_handlers(code or ""):
        if _FALSE_COMPLETE_CODE_RE.search(hsrc):
            hits.append(
                f"{hname}: 去掉「无 ok + 收取还在 → return True/__exit__」的过早完成；"
                "改为点 room_收取奖励 后等待 room_ok 并循环点击至消失"
            )
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def apply_sequence_diagnosis(
    d: TrialDiagnosis,
    *,
    code: str,
    trial_log: str,
    feedback: str = "",
) -> TrialDiagnosis:
    """把「日志序列 → 代码分支」推理链落到 must_fix（零 token）。"""
    events = parse_trial_log_events(trial_log)
    chain = summarize_event_chain(events)
    if chain:
        d.code_hints.append(f"日志动作链: {chain}")

    seq_hit = detect_claim_ok_miss_then_nav(events)
    code_hits = detect_false_complete_claim_branch(code)
    log_says_false = bool(_FALSE_COMPLETE_LOG_RE.search(trial_log or ""))
    fb = feedback or ""
    vague = bool(_VAGUE_FB_RE.search(fb)) or not fb.strip()

    advancing = frame_timestamps_advancing(trial_log)
    if advancing is True:
        d.code_hints.append("frame_ts 有递增 → 优先排除「旧帧未刷新」为主因")
        d.do_not.append("不要把本次主因归为 update_frame/旧帧（日志显示帧时间戳在推进）")
    elif advancing is False:
        d.code_hints.append("frame_ts 未见递增 → 可保留帧新鲜度排查")

    if seq_hit or (code_hits and (log_says_false or vague or seq_hit)):
        if not d.symptom:
            d.symptom = (
                "点 room_收取奖励 后 room_ok 未命中，脚本却结束房间并转向出击/home 等导航"
                if seq_hit
                else "房间领取可能在未关闭 room_ok 时被当成任务完成"
            )
        if not d.root_cause:
            d.root_cause = (
                "领取后短窗口看不到 room_ok 时，用「收取按钮还在」或提前 return "
                "假成功结束；随后进入下一日常任务导航"
            )
        msg = (
            "点 room_收取奖励 后必须等待并处理 room_ok（循环点击至消失）；"
            "仅当明确出现 room_ap上限（或等价「无法领取」标识）才可本任务完成；"
            "禁止仅因短时间内 match 不到 room_ok、或「收取按钮还在」就结束房间任务"
        )
        if msg not in d.must_fix:
            d.must_fix.insert(0, msg)
        for h in code_hits:
            if h not in d.must_fix:
                d.must_fix.append(h)
        d.do_not.append(
            "不要只加 b_sleep/update_frame 而不改「假成功结束」分支"
        )

    # 用户反馈已写清假完成 / 未点确认窗
    if re.search(r"假成功|提前结束|没点.*ok|未.*room_ok|确认窗", fb, re.I):
        msg2 = (
            "按反馈：点收取后须处理 room_ok 确认窗；禁止未关窗就结束房间/跳转下一任务"
        )
        if msg2 not in d.must_fix:
            d.must_fix.insert(0, msg2)

    return d


def _extract_room_handlers(code: str) -> list[tuple[str, str]]:
    """返回 (handler名, 函数源码) 列表。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        name = node.name
        if not _ROOM_HANDLER_RE.search(name):
            stems: set[str] = set()
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_img"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    stems.add(call.args[0].value.lower())
            if not any("room" in s or "收取" in s for s in stems):
                continue
        src = ast.unparse(node) if hasattr(ast, "unparse") else ""
        if src:
            out.append((name, src))
    return out


def diagnose_local(
    *,
    code: str,
    trial_log: str = "",
    feedback: str = "",
    explanation: str = "",
) -> TrialDiagnosis:
    """不调 LLM 的规则诊断。"""
    d = TrialDiagnosis(source="local")
    log = trial_log or ""
    fb = feedback or ""
    expl = explanation or ""

    # —— room 领取 / room_ok 弹窗 ——
    try:
        from backend.script_generator.agent import validate_generated_code

        val = validate_generated_code(
            code,
            source_dir="",
            image_paths=[],
            explanation=expl,
        )
        room_errs = [e for e in val if "room_ok" in e or "收取奖励" in e or "room_claim" in e.lower()]
        for e in room_errs:
            if e not in d.must_fix:
                d.must_fix.append(e)
    except Exception:
        room_errs = []

    handlers = _extract_room_handlers(code)
    popup_signal = (
        _POPUP_FB_RE.search(fb)
        or (_NO_REWARD_LOG_RE.search(log) and _POPUP_FB_RE.search(fb))
    )
    for hname, hsrc in handlers:
        if "room_ok" not in hsrc.lower() and ("room" in hsrc.lower() or "收取" in hsrc):
            if "room" in expl.lower() or "room" in fb.lower():
                item = f"{hname}: 房间领取逻辑应引用 room_ok 并处理弹窗循环"
                if item not in d.must_fix:
                    d.must_fix.append(item)
        if re.search(r"not\s+await\s+browser\.match_image\([^)]*收取奖励", hsrc):
            if re.search(r"return\s+['\"]__exit__['\"]", hsrc):
                ok_before = bool(
                    re.search(
                        r"match_image\([^)]*room_ok[\s\S]*?"
                        r"not\s+await\s+browser\.match_image\([^)]*收取奖励",
                        hsrc,
                        re.I,
                    )
                )
                if not ok_before:
                    msg = (
                        f"{hname}: 在「无 room_收取奖励」时直接 __exit__；"
                        "若 room_ok 弹窗已开须先点击并循环直到消失"
                    )
                    if msg not in d.must_fix and not any(hname in x for x in d.must_fix):
                        d.must_fix.append(msg)
                    if popup_signal and not d.symptom:
                        d.symptom = "奖励弹窗已出现但脚本未点 room_ok 或提前结束"
                        d.root_cause = (
                            f"{hname} 把「看不见收取按钮」当成无奖励，"
                            "未处理仅 room_ok 可见的中间态"
                        )

    if _HOME_FB_RE.search(fb) or _HOME_FB_RE.search(expl):
        if "返回主界面" not in code and "go_home" not in code.lower():
            d.must_fix.append("房间/多任务入口：介绍要求先返回主界面，TASK_room 需映射「返回主界面」")

    if _NO_REWARD_LOG_RE.search(log) and _POPUP_FB_RE.search(fb):
        if not d.symptom:
            d.symptom = "日志显示无奖励/提前完成，但用户反馈弹窗未关"
        if not d.root_cause and d.must_fix:
            d.root_cause = d.must_fix[0]

    # —— 日志动作链 × 代码假完成分支（零 token 推理链）——
    apply_sequence_diagnosis(d, code=code or "", trial_log=log, feedback=fb)

    if "room" in expl.lower() or "room" in fb.lower() or d.must_fix:
        d.do_not.extend([
            "不要改动 jjc / tower 任务的入口 handler（除非反馈明确要求）",
            "不要在 返回主界面 成功时 return '__exit__'（那是本步骤结束，应 return '主界面'）",
            "不要把整段脚本结束和本任务 __exit__ 混用",
        ])

    if handlers:
        preview = handlers[0][1]
        if len(preview) > 1200:
            preview = preview[:1200] + "\n# ..."
        hint = f"相关 handler `{handlers[0][0]}`:\n{preview}"
        if hint not in d.code_hints:
            d.code_hints.append(hint)

    # 去重保序
    seen: set[str] = set()
    d.must_fix = [x for x in d.must_fix if x not in seen and not seen.add(x)]  # type: ignore
    seen.clear()
    d.do_not = [x for x in d.do_not if x not in seen and not seen.add(x)]  # type: ignore
    seen.clear()
    d.code_hints = [x for x in d.code_hints if x not in seen and not seen.add(x)]  # type: ignore

    return d


def merge_diagnosis(local: TrialDiagnosis, llm: TrialDiagnosis) -> TrialDiagnosis:
    out = TrialDiagnosis(source="merged")
    out.symptom = llm.symptom or local.symptom
    out.root_cause = llm.root_cause or local.root_cause
    out.need_vision = (
        llm.need_vision if llm.need_vision is not None else local.need_vision
    )
    out.vision_reason = llm.vision_reason or local.vision_reason
    out.frame_caption = llm.frame_caption or local.frame_caption
    seen: set[str] = set()
    for item in local.must_fix + llm.must_fix:
        if item and item not in seen:
            seen.add(item)
            out.must_fix.append(item)
    seen.clear()
    for item in local.do_not + llm.do_not:
        if item and item not in seen:
            seen.add(item)
            out.do_not.append(item)
    seen.clear()
    for item in local.code_hints + llm.code_hints:
        if item and item not in seen:
            seen.add(item)
            out.code_hints.append(item)
    return out


def format_diagnosis_block(d: TrialDiagnosis) -> str:
    if not any(
        [d.symptom, d.root_cause, d.must_fix, d.do_not, d.vision_reason, d.frame_caption]
    ):
        return ""
    lines = ["## Trial diagnosis (MUST implement before other tweaks)"]
    if d.symptom:
        lines.append(f"- Symptom: {d.symptom}")
    if d.root_cause:
        lines.append(f"- Root cause: {d.root_cause}")
    if d.need_vision is not None:
        lines.append(
            f"- Stop-frame vision: {'yes' if d.need_vision else 'no'}"
            + (f" ({d.vision_reason})" if d.vision_reason else "")
        )
    if d.frame_caption:
        lines.append(f"- Stop-frame caption: {d.frame_caption}")
    if d.must_fix:
        lines.append("- Must fix:")
        for i, item in enumerate(d.must_fix, 1):
            lines.append(f"  {i}. {item}")
    if d.do_not:
        lines.append("- Do NOT:")
        for item in d.do_not:
            lines.append(f"  - {item}")
    return "\n".join(lines)


def local_need_stop_frame_vision(
    *,
    feedback: str = "",
    trial_log: str = "",
    has_frame: bool = False,
) -> tuple[bool, str]:
    """规则侧提示：是否值得看停帧（供思考步参考，非最终决定）。"""
    if not has_frame:
        return False, "无停帧截图缓存"
    fb = feedback or ""
    log = trial_log or ""
    if _POPUP_FB_RE.search(fb):
        return True, "反馈涉及弹窗/未点击/卡住，停帧可确认画面控件"
    if _NO_REWARD_LOG_RE.search(log) and _POPUP_FB_RE.search(fb):
        return True, "日志称无奖励但反馈像弹窗态，文字可能矛盾"
    # 日志已呈现「收取→ok失败→导航」时，文字链足够；默认可不识图（省 token）
    try:
        evs = parse_trial_log_events(log)
        if detect_claim_ok_miss_then_nav(evs):
            return False, "日志动作链已定位房间假完成，不必识停帧"
    except Exception:
        pass
    if re.search(r"卡住|卡死|没动|停在|画面|界面|弹窗|奖励", fb):
        return True, "反馈描述画面态，停帧有助于对齐日志"
    if re.search(r"语法|NameError|ImportError|硬校验|校验失败", fb, re.I):
        return False, "像纯代码/校验问题，不必识图"
    return False, "文字反馈与日志似乎足够，默认可不识图"


def _parse_need_vision_json(text: str) -> tuple[Optional[bool], str]:
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return None, ""
    need = data.get("need_vision")
    if need is None:
        need = data.get("need")
    reason = str(data.get("reason") or data.get("vision_reason") or "").strip()
    if isinstance(need, str):
        need = need.strip().lower() in ("1", "true", "yes", "y", "需要", "是")
    elif need is not None:
        need = bool(need)
    return need, reason


async def decide_stop_frame_vision(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    feedback: str = "",
    trial_log: str = "",
    has_frame: bool = False,
    local_hint: tuple[bool, str] = (False, ""),
    max_tokens: int = 512,
    on_status=None,
    on_artifact=None,
) -> tuple[bool, str, int, int]:
    """思考：是否需要识读试跑停帧。返回 (need, reason, inp, out)。"""
    hint_need, hint_reason = local_hint
    if not has_frame:
        reason = "无停帧截图，跳过识图"
        if on_artifact:
            on_artifact(
                "stage",
                f"vision_decide|done|是否识停帧|不需要 — {reason}",
            )
        return False, reason, 0, 0

    if on_status:
        on_status("思考是否需要识读停帧…")
    if on_artifact:
        on_artifact(
            "stage",
            "vision_decide|running|是否识停帧|"
            f"规则提示: {'需要' if hint_need else '可不识'} — {hint_reason or '无'}",
        )

    from backend.script_generator.agent import call_llm, _focus_trial_log

    system = (
        "You decide whether a game automation revise step needs to look at "
        "the trial-stop screenshot.\n"
        "Output ONLY JSON: {\"need_vision\": true|false, \"reason\": \"short Chinese\"}.\n"
        "need_vision=true when: popup/stuck UI, log vs feedback conflict, "
        "or unclear which scene/button is on screen.\n"
        "need_vision=false when: pure syntax/NameError/validation, or text already enough.\n"
        "Do not describe the image (you cannot see it yet)."
    )
    user = (
        f"## Has stop-frame cached\n{'yes' if has_frame else 'no'}\n\n"
        f"## Local heuristic\nneed={hint_need}; reason={hint_reason or '(none)'}\n\n"
        f"## User feedback\n{(feedback or '').strip() or '(none)'}\n\n"
        f"## Trial log (focused)\n{_focus_trial_log(trial_log)}\n\n"
        "Decide need_vision."
    )
    try:
        raw, inp, out = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
            system_prompt=system,
            max_tokens=max_tokens,
        )
        need, reason = _parse_need_vision_json(raw)
        if need is None:
            need, reason = hint_need, (hint_reason or "解析失败，沿用规则提示")
        elif not reason:
            reason = hint_reason or ("需要识停帧" if need else "不必识停帧")
        if on_artifact:
            on_artifact(
                "stage",
                f"vision_decide|done|是否识停帧|"
                f"{'需要' if need else '不需要'} — {reason}",
            )
        return bool(need), reason, inp, out
    except Exception as e:
        need, reason = hint_need, (hint_reason or f"思考失败，沿用规则: {e}")
        if on_artifact:
            on_artifact(
                "stage",
                f"vision_decide|done|是否识停帧|"
                f"{'需要' if need else '不需要'} — {reason}",
            )
        return bool(need), reason, 0, 0


async def caption_stop_frame(
    *,
    frame_path: str,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    feedback: str = "",
    trial_log: str = "",
    explanation: str = "",
    compress_images: bool = True,
    max_tokens: int = 1024,
    on_status=None,
    on_artifact=None,
) -> tuple[str, int, int]:
    """识读试跑停帧，返回短中文描述。"""
    from pathlib import Path

    path = Path(frame_path)
    if not path.is_file():
        return "", 0, 0

    if on_status:
        on_status("识读试跑停帧…")
    if on_artifact:
        on_artifact("stage", f"stop_vision|running|识读停帧|{path.name}")

    from backend.script_generator.agent import (
        _image_b64,
        _provider_api,
        _provider_supports_images,
        call_llm,
        _focus_trial_log,
    )

    if not _provider_supports_images(provider):
        msg = f"识图模型 {provider} 不支持看图，跳过停帧"
        if on_artifact:
            on_artifact("stage", f"stop_vision|done|识读停帧|{msg}")
        return "", 0, 0

    system = (
        "You describe ONE game UI screenshot from a stopped automation trial.\n"
        "Reply in concise Chinese (5-12 lines):\n"
        "- Likely scene (主界面/房间/出击/弹窗等)\n"
        "- Visible buttons / dialogs that matter for scripting "
        "(e.g. room_ok, 收取奖励, rank)\n"
        "- Anything that contradicts a 'no reward / done' log if relevant\n"
        "Do not write Python."
    )
    user_text = (
        "这是试运行「停止」时缓存的游戏画面。\n"
        f"## 用户反馈\n{(feedback or '').strip() or '(无)'}\n\n"
        f"## 试跑日志（摘录）\n{_focus_trial_log(trial_log)}\n\n"
        f"## 介绍摘录\n{(explanation or '')[:1200]}\n"
    )
    try:
        b64data, media_type = _image_b64(path, compress=compress_images)
    except Exception as e:
        if on_artifact:
            on_artifact("stage", f"stop_vision|done|识读停帧|加载失败: {e}")
        return "", 0, 0

    api = _provider_api(provider)
    if api == "claude":
        img_part = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64data},
        }
    elif api == "google":
        img_part = {"inline_data": {"mime_type": media_type, "data": b64data}}
    else:
        img_part = {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64data}"},
        }
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": user_text},
            img_part,
        ],
    }]
    try:
        text, inp, out = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=system,
            max_tokens=max_tokens,
        )
        caption = (text or "").strip()
        if on_artifact:
            preview = caption if len(caption) <= 500 else caption[:500] + "…"
            on_artifact("stage", f"stop_vision|done|识读停帧|{preview}")
        return caption, inp, out
    except Exception as e:
        if on_artifact:
            on_artifact("stage", f"stop_vision|done|识读停帧|失败: {e}")
        return "", 0, 0


def _parse_diagnosis_json(text: str) -> TrialDiagnosis:
    raw = (text or "").strip()
    if not raw:
        return TrialDiagnosis(source="llm")
    # 允许 ```json ... ```
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试截取第一个 { ... }
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return TrialDiagnosis(source="llm")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return TrialDiagnosis(source="llm")
    d = TrialDiagnosis.from_dict(data)
    d.source = "llm"
    return d


def _focus_code_snippets(code: str, limit: int = 4000) -> str:
    handlers = _extract_room_handlers(code)
    if not handlers:
        text = code or ""
        return text[-limit:] if len(text) > limit else text
    chunks = [f"### {name}\n{src}" for name, src in handlers[:4]]
    text = "\n\n".join(chunks)
    if len(text) > limit:
        return text[:limit] + "\n# ..."
    return text


async def diagnose_trial_failure(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    code: str,
    trial_log: str = "",
    feedback: str = "",
    explanation: str = "",
    stop_frame_path: str = "",
    vision_assist: Optional[dict] = None,
    max_tokens: int = 2048,
    on_status=None,
    on_artifact=None,
) -> tuple[TrialDiagnosis, int, int]:
    """规则诊断 +（可选）停帧思考/识图 + LLM 结构化诊断。"""
    from pathlib import Path

    local = diagnose_local(
        code=code,
        trial_log=trial_log,
        feedback=feedback,
        explanation=explanation,
    )

    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    def _artifact(kind: str, payload: str) -> None:
        if on_artifact:
            try:
                on_artifact(kind, payload)
            except Exception:
                pass

    fb = (feedback or "").strip()
    log = (trial_log or "").strip()
    frame_path = (stop_frame_path or "").strip()
    has_frame = bool(frame_path and Path(frame_path).is_file())
    total_in = 0
    total_out = 0

    if not fb and not log and not local.must_fix and not has_frame:
        return local, 0, 0

    # —— 思考：要不要识停帧 ——
    hint = local_need_stop_frame_vision(
        feedback=fb, trial_log=log, has_frame=has_frame,
    )
    need, reason, din, dout = await decide_stop_frame_vision(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        feedback=fb,
        trial_log=log,
        has_frame=has_frame,
        local_hint=hint,
        max_tokens=min(max_tokens, 512),
        on_status=_status,
        on_artifact=_artifact,
    )
    total_in += din
    total_out += dout
    local.need_vision = need
    local.vision_reason = reason

    frame_caption = ""
    if need and has_frame:
        va = vision_assist if isinstance(vision_assist, dict) else {}
        v_provider = str(va.get("provider") or provider)
        v_key = str(va.get("api_key") or api_key)
        v_model = str(va.get("model") or model)
        v_endpoint = va.get("api_endpoint")
        if v_endpoint is None:
            v_endpoint = api_endpoint
        else:
            v_endpoint = str(v_endpoint).strip() or None
        compress = bool(va.get("compress_images", True))
        if not (v_key and v_model):
            note = "需要识停帧但未配置识图 Key/模型，跳过"
            local.vision_reason = (reason + "；" + note).strip("；")
            _artifact("stage", f"stop_vision|done|识读停帧|{note}")
        else:
            frame_caption, vin, vout = await caption_stop_frame(
                frame_path=frame_path,
                provider=v_provider,
                api_key=v_key,
                model=v_model,
                api_endpoint=v_endpoint,
                feedback=fb,
                trial_log=log,
                explanation=explanation,
                compress_images=compress,
                max_tokens=min(max_tokens, 1024),
                on_status=_status,
                on_artifact=_artifact,
            )
            total_in += vin
            total_out += vout
            local.frame_caption = frame_caption
            if frame_caption:
                local.must_fix.append(
                    f"【停帧画面】{frame_caption[:400]}"
                    + ("…" if len(frame_caption) > 400 else "")
                )
    elif need and not has_frame:
        local.vision_reason = (reason + "；但无停帧文件").strip("；")

    _status("试跑诊断…")
    local_preview = format_diagnosis_block(local)
    _artifact(
        "stage",
        "diagnose|running|试跑诊断|"
        + (local_preview or "分析日志与代码…"),
    )

    from backend.script_generator.agent import call_llm, _focus_trial_log

    system = (
        "You diagnose Minashigo game automation script trial failures.\n"
        "Output ONLY one JSON object (no markdown), keys:\n"
        "  symptom (string), root_cause (string),\n"
        "  must_fix (array of concrete fix strings in Chinese),\n"
        "  do_not (array of things to avoid changing),\n"
        "  code_hints (array, optional short notes).\n"
        "Focus on FSM control-flow gaps vs user feedback and trial log.\n"
        "If a stop-frame caption is provided, treat it as ground truth for "
        "what was on screen when the user stopped.\n"
        "Common patterns (prefer local must_fix if already listed):\n"
        "1) room_ok popup visible but handler exits when room_收取奖励 missing — click room_ok loop.\n"
        "2) After click room_收取奖励, room_ok never matched then script navigates away "
        "(出击/home) because of early return when claim button still visible — "
        "FORBIDDEN false-success; must wait/handle room_ok or room_ap上限.\n"
        "Do not suggest inventing browser APIs."
    )
    _chain = summarize_event_chain(parse_trial_log_events(log), max_n=16)
    user = (
        f"## User feedback\n{fb or '(none)'}\n\n"
        f"## Log action chain (rule-extracted)\n{_chain or '(none)'}\n\n"
        f"## Trial log (focused)\n{_focus_trial_log(log)}\n\n"
        f"## Script explanation excerpt\n{(explanation or '')[:3000]}\n\n"
        f"## Relevant code\n```python\n{_focus_code_snippets(code)}\n```\n\n"
    )
    if local.need_vision is not None:
        user += (
            f"## Stop-frame vision decision\n"
            f"need={local.need_vision}; reason={local.vision_reason or '(none)'}\n\n"
        )
    if frame_caption:
        user += f"## Stop-frame caption (from vision)\n{frame_caption}\n\n"
    if local.must_fix:
        user += "## Local pre-check (verify / extend)\n" + "\n".join(
            f"- {x}" for x in local.must_fix
        ) + "\n\n"
    user += "Respond with JSON only."

    try:
        raw, inp, out = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
            system_prompt=system,
            max_tokens=max_tokens,
        )
        total_in += inp
        total_out += out
        llm_d = _parse_diagnosis_json(raw)
        merged = merge_diagnosis(local, llm_d)
        merged.need_vision = local.need_vision
        merged.vision_reason = local.vision_reason
        merged.frame_caption = local.frame_caption or merged.frame_caption
        _artifact("diagnosis", json.dumps(merged.to_dict(), ensure_ascii=False, indent=2))
        _artifact(
            "stage",
            "diagnose|done|诊断完成|"
            + format_diagnosis_block(merged),
        )
        return merged, total_in, total_out
    except Exception as e:
        _artifact(
            "stage",
            f"diagnose|done|诊断完成（仅规则）|{format_diagnosis_block(local)}\n\n(LLM 跳过: {e})",
        )
        _artifact("diagnosis", json.dumps(local.to_dict(), ensure_ascii=False, indent=2))
        return local, total_in, total_out
