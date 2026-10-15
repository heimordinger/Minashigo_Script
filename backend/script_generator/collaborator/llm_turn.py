"""协作回合：调用用户 API 配置的 LLM。

主协议：系统发【状态摘要】+ 空表 → LLM 只填 <<<COLLAB_FORM>>> → 系统解析呈现。
OpenAI/Claude 仍可选用 tools；最终答复仍须落成同一张表（兼容旧 COLLAB_JSON）。
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from backend.script_generator.collaborator.skills.registry import SkillRegistry
from backend.script_generator.collaborator.session import CollabSession
from backend.script_generator.collaborator.collab_form import (
    build_form_system_prompt,
    build_status_sheet,
    classify_done_when,
    collab_form_present,
    contract_gap_note,
    form_to_runtime_meta,
    looks_like_reasoning,
    parse_collab_form,
    plan_contract_gaps,
    plan_should_continue,
    remember_plan,
    stream_form_preview,
    hold_for_user,
    surface_plan_pause,
    collab_script_issues,
)

# 旧标记（兼容）
_JSON_BLOCK = re.compile(
    r"<<<COLLAB_JSON>>>\s*(\{.*?\})\s*<<<END(?:_JSON|_COLLAB(?:_JSON)?)?>>>",
    re.DOTALL | re.IGNORECASE,
)
_JSON_LOOSE = re.compile(
    r"<<<COLLAB_JSON>>>\s*(\{.*)",
    re.DOTALL | re.IGNORECASE,
)

# 送图给视觉模型时的最长边，必须与 describe_collab_frame 里的压缩保持一致
# （那边调 _image_b64(..., max_size=1280)，等比缩放）。
_VISION_MAX_SIDE = 1280


def _path_is_file(p) -> bool:
    try:
        return Path(p).is_file()
    except Exception:
        return False


def build_system_prompt(
    registry: SkillRegistry,
    session: CollabSession,
    *,
    model_name: str = "",
    vision_prefetch: str = "",
    runtime_block: str = "",
    permissions: Optional[dict] = None,
    tools_available: bool = True,
    primary_visual: bool = False,
) -> str:
    """稳定系统提示（填表规则）。状态摘要每轮另发，不塞进 system。"""
    _ = (registry, session, vision_prefetch, runtime_block, permissions)
    return build_form_system_prompt(
        model_name=model_name,
        tools_available=bool(tools_available),
        primary_visual=bool(primary_visual),
    )

def _reply_asks_for_frame(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    keys = (
        "我看不见", "看不到", "看不到画面", "没有画面", "请提供画面",
        "请发一张", "请再截", "再截一张", "用当前窗口", "选已有截图",
        "需要看画面", "需要看到画面", "把画面发", "发个截图", "先截图",
        "暂时看不到", "无法看到", "看不到截图",
    )
    return any(k in t for k in keys)


def sanitize_visible_when_frame_ready(
    visible: str,
    *,
    has_frame: bool,
    probe_brief: str = "",
    analyze_summary: str = "",
) -> str:
    """已有画面时，若模型仍索要截图，改写成基于探针的答复。"""
    text = (visible or "").strip()
    if not has_frame or not _reply_asks_for_frame(text):
        return text
    brief = (probe_brief or analyze_summary or "").strip()
    if brief and _assist_vision_is_blind(brief):
        return (
            "辅助识图没有真正读到画面（多半是识图模型选成了纯文本，"
            "例如 qwen3.5-flash / deepseek）。\n"
            "请到「1. API 配置 → 辅助识图」换成带视觉的模型"
            "（如 qwen-vl-max / qwen-vl-plus / gpt-4o），再问「再分析」。"
        )
    if brief and not _assist_vision_is_blind(brief):
        return (
            f"{brief}\n\n"
            "右侧已有当前画面，我按这张图分析了。"
            "若叠框不对，再换一张；对的话可以直接确认生成。"
        )
    return (
        "右侧预览里已经有画面，不必再发同一张。"
        "若要重认，说「再分析」或换一张图；也可以继续生成。"
    )




def _reply_promises_pending_look(text: str) -> bool:
    """模型口头说要确认/匹配，但本轮可能没真正请求行动。"""
    t = (text or "").strip()
    if not t:
        return False
    # 若本轮核心是要权限，不算「空口看图」——交给权限兜底
    if _infer_need_permission_from_text(t):
        return False
    keys = (
        "确认一下", "先确认", "我先确认", "我先看", "先看一下", "核对一下",
        "先匹配", "匹配看", "我先匹配", "匹配这两", "匹配这两张", "直接跑",
        "两件事一起", "接下来我会", "我先核对", "让我先",
        "稍后再", "稍后确认", "对齐坐标", "确认细节", "再确认一张",
        "看结算", "看图细节", "确认点完",
    )
    return any(k in t for k in keys)


def _reply_wants_match(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    keys = ("匹配", "模板", "对不上", "能不能当", "是否像", "是不是挑战", "是不是结算")
    return any(k in t for k in keys)


def _infer_need_permission_from_text(text: str) -> str:
    """从口头回复推断 need_permission。只在**确实在向用户要**时才认。

    模型经常在正文里解释性提一句「这样做需要授权」，并不是要用户现在开权限。
    早先的版本只看关键词，于是每轮都被改成 ask 弹卡——用户明明在提需求，
    却收到一张授权卡。现在要求同一句里既有权限名词、又有明确的请求口吻，
    且不是假设语气。
    """
    t = (text or "").strip()
    if not t:
        return ""
    write_keys = (
        "写文件权限", "写权限", "写入权限", "给写权限", "打开写文件",
        "需要写文件", "要写文件", "写文件需要", "得你给写", "请给写",
        "授权写", "write_files", "临时脚本", "probe_script", "跑脚本",
    )
    runtime_keys = (
        "控制窗口", "runtime_control", "改绑窗口", "控制权限", "窗口权限",
    )
    request_keys = (
        "请", "麻烦", "需要你", "要你", "给我", "帮我开", "打开", "同意",
        "授权一下", "放行", "点一下", "允许",
    )
    hypo_keys = ("如果", "假如", "到时候", "以后", "将来", "万一", "的话")

    def _hits(sentence: str, keys: tuple[str, ...]) -> bool:
        low = sentence.lower()
        return any(k.lower() in low for k in keys)

    for sentence in re.split(r"[。！？!?\n；;]", t):
        if not sentence.strip():
            continue
        if _hits(sentence, hypo_keys):
            continue  # 假设语气不是请求
        if not _hits(sentence, request_keys):
            continue
        if _hits(sentence, write_keys):
            return "write_files"
        if _hits(sentence, runtime_keys):
            return "runtime_control"
    return ""



def normalize_turn_meta(meta: dict[str, Any], visible: str) -> str:
    """把 LLM 的 turn 收成 stop|act|ask；缺省时按字段/口头补齐。"""
    if not isinstance(meta, dict):
        meta = {}
    raw = str(meta.get("turn") or meta.get("status") or "").strip().lower()
    aliases = {
        "done": "stop",
        "finish": "stop",
        "finished": "stop",
        "complete": "stop",
        "continue": "act",
        "action": "act",
        "need_action": "act",
        "wait": "ask",
        "await": "ask",
        "await_user": "ask",
        "need_user": "ask",
    }
    turn = aliases.get(raw, raw)
    if turn not in ("stop", "act", "ask"):
        turn = ""

    need_perm = str(meta.get("need_permission") or "").strip()
    # 模型已写 stop 时，不从正文猜权限，避免把结束改成 ask。
    if not need_perm and turn != "stop":
        need_perm = _infer_need_permission_from_text(visible)
        if need_perm:
            meta["need_permission"] = need_perm

    chips = meta.get("chips") or []
    has_ask_chip = any(
        str(c) in ("打开写文件权限", "打开控制窗口权限", "打开临时脚本权限")
        for c in (chips if isinstance(chips, list) else [])
    )
    wants_act = bool(meta.get("describe_frame") or meta.get("need_vision") or meta.get("match_assets"))
    promised = _reply_promises_pending_look(visible)

    # 同轮既要做事又要授权：先 act，做完再弹卡。模型已写 stop 时不改口。
    explicit_stop = turn == "stop"
    if need_perm and not explicit_stop and (wants_act or promised or turn == "act"):
        meta["_ask_after_act"] = need_perm
        turn = "act"
        # 口头还要做事：默认辅助识图；本地匹配仅当 JSON 已写 match_assets
        if not wants_act and promised:
            meta["describe_frame"] = True
    elif need_perm or has_ask_chip:
        turn = "ask"
        meta.pop("_ask_after_act", None)
    elif turn == "ask" and not need_perm and not has_ask_chip:
        turn = "stop"
    elif turn == "act" or ((not explicit_stop) and (wants_act or promised)):
        turn = "act"
        if not wants_act and promised:
            meta["describe_frame"] = True
    elif not turn:
        turn = "stop"

    meta["turn"] = turn
    return turn


def _auto_match_session_assets(session, *, limit: int = 2) -> str:
    """无工具通道时：挑最该验证的素材做匹配，结果写 overlays。

    没有焦点机制可依赖了，这里自己排序：未看过的优先、截图/模板优先，
    标注图（模型自己画的）直接排除。只对前 limit 张动手，避免瞎跑。
    """
    from backend.script_generator.collaborator.canvas import (
        is_scene_asset,
        resolve_template_path,
        template_size,
        upsert_match_overlay,
    )
    from backend.script_generator.collaborator.probe_script import match_template_on_frame

    frame = getattr(session, "frame_path", None)
    if not _path_is_file(frame):
        return ""
    assets = [a for a in (getattr(session, "assets", None) or []) if is_scene_asset(a)]
    if not assets:
        return ""
    inspected = set()
    art = getattr(session, "artifacts", None)
    if isinstance(art, dict):
        last = art.get("last_asset_inspect")
        if isinstance(last, dict) and last.get("asset_id"):
            inspected.add(str(last.get("asset_id")))
        for rec in art.get("evidence") or []:
            if isinstance(rec, dict):
                for key in ("template_asset_id", "target_asset_id"):
                    if rec.get(key):
                        inspected.add(str(rec.get(key)))

    def _rank(a: dict) -> tuple:
        role = str(a.get("role") or "")
        return (
            str(a.get("id") or "") in inspected,  # 没看过的排前面
            0 if role == "template" else 1,
        )

    chosen = sorted(assets, key=_rank)[: max(1, limit)]
    lines = []
    for a in chosen:
        aid = str(a.get("id") or "")
        path, asset, err = resolve_template_path(session, asset_id=aid)
        if err or path is None:
            lines.append(f"· [[{aid}]] 无法匹配：{err or '无路径'}")
            continue
        try:
            if Path(path).resolve() == Path(frame).resolve():
                lines.append(f"· [[{aid}]] 已跳过：不能把当前整图与自身匹配")
                continue
        except Exception:
            pass
        data = match_template_on_frame(
            frame_path=Path(frame),
            template_path=path,
            threshold=0.9,
        )
        if not isinstance(data, dict):
            lines.append(f"· [[{aid}]] 匹配失败")
            continue
        if data.get("ok") is False and data.get("error"):
            lines.append(f"· [[{aid}]] 失败：{data.get('error')}")
            continue
        ok = bool(data.get("matched") or data.get("ok"))
        score = data.get("score")
        if data.get("x") is not None:
            tw, th = template_size(path)
            upsert_match_overlay(
                session, asset=asset or a, match=data, template_wh=(tw, th)
            )
            if not isinstance(session.artifacts, dict):
                session.artifacts = {}
            session.artifacts["canvas_dirty"] = True
        if ok:
            lines.append(f"· [[{aid}]] 命中 score={score}")
        else:
            lines.append(f"· [[{aid}]] 未命中 score={score}")
    if not lines:
        return ""
    return "【模板匹配】\n" + "\n".join(lines)


def _user_wants_vision(text: str) -> bool:
    """是否值得尝试画面探针 / 辅助识图。"""
    t = (text or "").strip()
    if not t:
        return False
    keys = (
        "识别", "认出", "看看", "数一数", "有几", "多少", "画面里", "截图里",
        "这张图", "当前画面", "看见", "看到", "描述一下", "里面有什么",
        "再分析", "分析一下",
    )
    return any(k in t for k in keys)


def _vision_assist_ready(vision_assist: Optional[dict]) -> bool:
    if not isinstance(vision_assist, dict):
        return False
    return bool(
        str(vision_assist.get("provider") or "").strip()
        and str(vision_assist.get("api_key") or "").strip()
        and str(vision_assist.get("model") or "").strip()
    )


def _looks_like_text_only_model(model: str) -> bool:
    """粗判：常见纯文本名，误用作识图会「完成」但声称看不见。"""
    m = (model or "").strip().lower()
    if not m:
        return False
    # 明确视觉
    if any(k in m for k in (
        "-vl", "vl-", "vision", "llava", "gpt-4o", "gemini", "4v",
        "deepseek-flash", "deepseek-v4-flash",
    )):
        return False
    # 明确文本 / 工具辅助常用
    text_markers = (
        "deepseek",
        "qwen3.5-flash",
        "qwen3.5-plus",
        "qwen-plus",
        "qwen-max",
        "qwen-turbo",
        "qwen3-coder",
        "coder",
        "reasoner",
        "chat",
    )
    return any(k in m for k in text_markers)


def _assist_vision_is_blind(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    keys = (
        "无法看到", "看不到截图", "看不见截图", "看不见图", "看不到图",
        "没有看到图", "未收到图", "没有图片", "无法识别图片",
        "不确定界面内容", "我无法查看", "不能查看图片", "不支持看图",
    )
    return any(k in t for k in keys)


_VISION_PROTOCOL_VERSION = "visual-collab-v1"


def _vision_task_spec(
    *,
    question: str = "",
    mode: str = "locate",
    target: str = "",
    distinguish_from: Optional[list] = None,
) -> dict[str, Any]:
    """主模型交给识图模型的结构化任务单（兼容旧 question 参数）。"""
    return {
        "protocol": _VISION_PROTOCOL_VERSION,
        "mode": str(mode or "locate")[:24],
        "target": str(target or question or "识别与用户目标相关的区域")[:500],
        "question": str(question or "")[:1000],
        "distinguish_from": [str(x)[:160] for x in (distinguish_from or [])[:8]],
        "coordinate_space": "原始输入图片像素；bbox=[x0,y0,x1,y1]",
    }


def _vision_cache_key(path: Path, *, model: str, task: dict[str, Any]) -> str:
    try:
        stamp = f"{path.resolve()}|{path.stat().st_mtime_ns}|{path.stat().st_size}"
    except Exception:
        stamp = str(path)
    raw = json.dumps(
        {"stamp": stamp, "model": model, "task": task, "v": _VISION_PROTOCOL_VERSION},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_vision_payload(text: str, *, mode: str = "") -> dict[str, Any]:
    """解析并校验视觉协作 JSON；散文仍保留，但不能成为定位证据。"""
    raw = str(text or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        payload = json.loads(candidate)
    except Exception:
        try:
            payload = json.loads(candidate[candidate.index("{") : candidate.rindex("}") + 1])
        except Exception:
            return {"structured": False, "text": raw, "validation_errors": ["识图回复不是 JSON"]}
    if not isinstance(payload, dict):
        return {"structured": False, "text": raw, "validation_errors": ["识图回复不是对象"]}
    errors: list[str] = []
    size = payload.get("image_size")
    if not isinstance(size, list) or len(size) != 2 or not all(isinstance(v, (int, float)) and v > 0 for v in size):
        errors.append("缺少有效 image_size")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        errors.append("缺少 candidates 数组")
        candidates = []
    valid_candidates = []
    width = float(size[0]) if isinstance(size, list) and len(size) == 2 and isinstance(size[0], (int, float)) else 0
    height = float(size[1]) if isinstance(size, list) and len(size) == 2 and isinstance(size[1], (int, float)) else 0
    for i, item in enumerate(candidates):
        if not isinstance(item, dict):
            errors.append(f"candidate[{i}] 不是对象")
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
            errors.append(f"candidate[{i}] 缺少有效 bbox")
            continue
        x0, y0, x1, y1 = bbox
        if x1 <= x0 or y1 <= y0:
            errors.append(f"candidate[{i}] bbox 无效")
            continue
        if width and height and (x0 < 0 or y0 < 0 or x1 > width or y1 > height):
            errors.append(f"candidate[{i}] bbox 超出图片范围")
            continue
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            errors.append(f"candidate[{i}] 缺少有效 confidence")
            continue
        if not str(item.get("observations") or "").strip():
            errors.append(f"candidate[{i}] 缺少 observations")
            continue
        valid_candidates.append(item)
    payload["candidates"] = valid_candidates
    if mode == "verify" and not isinstance(payload.get("verified"), bool):
        errors.append("verify 任务缺少 verified 布尔值")
    payload["structured"] = not errors
    payload["validation_errors"] = errors
    payload["text"] = raw
    return payload


def _vision_fed_size(width: int, height: int) -> tuple[int, int]:
    """模型真正收到的图片尺寸。口径与 describe_collab_frame 的等比缩放一致。

    必须自己算，不能信模型自报的 ``image_size``：VL 网关经常把它四舍五入成
    16:9（1920x1019 报成 1280x720），照自报值换算会把坐标整体推偏。
    """
    if width <= 0 or height <= 0:
        return 0, 0
    longest = max(width, height)
    if longest <= _VISION_MAX_SIDE:
        return int(width), int(height)
    scale = _VISION_MAX_SIDE / float(longest)
    return int(width * scale), int(height * scale)


_MIN_GROUNDING_SIDE = 8


def _scale_bbox(
    raw_bbox: list, from_w: int, from_h: int, to_w: int, to_h: int
) -> list[int]:
    """把 raw_bbox 从 (from_w, from_h) 坐标系换算到 (to_w, to_h) 并夹紧。"""
    if from_w <= 0 or from_h <= 0 or to_w <= 0 or to_h <= 0:
        return []
    sx, sy = to_w / float(from_w), to_h / float(from_h)
    return [
        max(0, min(to_w, int(round(float(raw_bbox[0]) * sx)))),
        max(0, min(to_h, int(round(float(raw_bbox[1]) * sy)))),
        max(0, min(to_w, int(round(float(raw_bbox[2]) * sx)))),
        max(0, min(to_h, int(round(float(raw_bbox[3]) * sy)))),
    ]


def _bbox_usable(bbox: list, min_side: int = _MIN_GROUNDING_SIDE) -> bool:
    """框要有正面面积，否则是退化框（clamp 出来的零宽/零高）。"""
    return (
        len(bbox) == 4
        and (bbox[2] - bbox[0]) >= min_side
        and (bbox[3] - bbox[1]) >= min_side
    )


def _record_vision_groundings(
    session: CollabSession,
    parsed: dict[str, Any],
    *,
    source_path: Path,
    source_asset_id: str = "",
    mode: str = "locate",
    fed_size: tuple[int, ...] = (),
) -> list[dict[str, Any]]:
    """把视觉坐标变成裁剪工具可核验的来源链。"""
    if not parsed.get("structured"):
        return []
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    vision_size = parsed.get("image_size") or []
    try:
        from backend.script_generator.vision_tools import _imread

        source_image = _imread(Path(source_path))
        source_h, source_w = source_image.shape[:2]
    except Exception:
        source_w = source_h = 0
    try:
        reported_w, reported_h = int(vision_size[0]), int(vision_size[1])
    except Exception:
        reported_w = reported_h = 0
    # 以实际送入尺寸为准；自报值只留作诊断（两者不等就是网关报错尺寸了）。
    # Phase B2：送入尺寸由 Broker 统一算好传进来（同一份不变量），这里只在没给时兜底。
    vision_w, vision_h = (
        (int(fed_size[0]), int(fed_size[1]))
        if isinstance(fed_size, (list, tuple)) and len(fed_size) >= 2
        else _vision_fed_size(source_w, source_h)
    )
    if vision_w <= 0 or vision_h <= 0:
        vision_w, vision_h = _vision_fed_size(source_w, source_h)
    if vision_w <= 0 or vision_h <= 0:
        vision_w, vision_h = reported_w, reported_h
    records = session.artifacts.setdefault("vision_groundings", [])
    if not isinstance(records, list):
        records = []
        session.artifacts["vision_groundings"] = records
    added = []
    for candidate in parsed.get("candidates") or []:
        raw_bbox = list(candidate.get("bbox") or [])
        bbox = list(raw_bbox)
        rescale_note = ""
        # VL 网关可能缩放输入后再报坐标。统一换算回磁盘原图坐标系，
        # 否则小裁剪图（如 272x150 → 640x320）会产生越界 grounding。
        if len(bbox) == 4 and source_w > 0 and source_h > 0:
            primary = _scale_bbox(raw_bbox, vision_w, vision_h, source_w, source_h)
            if _bbox_usable(primary):
                bbox = primary
            else:
                # 退化框：常见于"模型自报尺寸比磁盘图大得多"的情况——
                # 例如磁盘图只有 36×45，模型报 808×1014，坐标全在 808×1014 空间里，
                # 按送入尺寸（=磁盘尺寸）换算等于没缩，越界后被 clamp 成零面积。
                # 这时按「自报尺寸 → 磁盘尺寸」再补一次回缩。
                alt = _scale_bbox(raw_bbox, reported_w, reported_h, source_w, source_h)
                if _bbox_usable(alt):
                    bbox = alt
                    rescale_note = "按模型自报尺寸回缩"
                else:
                    bbox = primary
                    rescale_note = "退化框（换算后面积过小）"
        raw = json.dumps(
            {
                "path": str(source_path),
                "candidate": candidate.get("id"),
                "bbox": bbox,
                "mode": mode,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        grounding_id = "vision_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        candidate["grounding_id"] = grounding_id
        record = {
            "id": grounding_id,
            "candidate_id": str(candidate.get("id") or ""),
            "source_path": str(source_path),
            "source_asset_id": source_asset_id,
            "bbox": bbox,
            "raw_bbox": raw_bbox,
            "vision_image_size": [vision_w, vision_h],
            "vision_reported_size": [reported_w, reported_h],
            "source_image_size": [source_w, source_h],
            "label": str(candidate.get("label") or ""),
            "confidence": candidate.get("confidence"),
            "mode": mode,
            # 退化框不能当裁剪依据：宁可让上游报"定位无效、重新定位"，
            # 也不要留一个零面积框在数据里被当成可用坐标。
            "valid": _bbox_usable(bbox),
        }
        if rescale_note:
            record["note"] = rescale_note
            candidate["bbox_invalid"] = not record["valid"]
        records[:] = [r for r in records if not isinstance(r, dict) or r.get("id") != grounding_id]
        records.append(record)
        added.append(record)
    del records[:-60]
    return added


def _cached_assist_vision(session: CollabSession) -> str:
    art = session.artifacts if isinstance(session.artifacts, dict) else {}
    cached = art.get("assist_vision")
    if not isinstance(cached, dict):
        return ""
    path = session.frame_path
    if not path or not _path_is_file(path):
        return ""
    try:
        mtime = Path(path).stat().st_mtime
    except Exception:
        return ""
    if str(cached.get("path") or "") != str(path):
        return ""
    try:
        if abs(float(cached.get("mtime") or 0) - float(mtime)) > 0.01:
            return ""
    except Exception:
        return ""
    text = str(cached.get("text") or "").strip()
    if _assist_vision_is_blind(text):
        return ""
    return text


def _store_assist_vision(session: CollabSession, text: str, *, model: str) -> None:
    path = session.frame_path
    if not path or not _path_is_file(path) or not (text or "").strip():
        return
    try:
        mtime = Path(path).stat().st_mtime
    except Exception:
        mtime = 0.0
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    session.artifacts["assist_vision"] = {
        "path": str(path),
        "mtime": mtime,
        "model": model,
        "text": text.strip(),
    }


async def describe_collab_frame(
    *,
    frame_path: Path,
    vision_assist: dict,
    user_question: str = "",
    task_spec: Optional[dict[str, Any]] = None,
    on_status=None,
) -> tuple[str, int, int]:
    """用指定视觉后端描述协作画面；可由主模型自身或辅助识图承担。"""
    from backend.script_generator.agent import (
        _image_b64,
        _model_supports_images,
        _provider_api,
        call_llm,
    )

    provider = str(vision_assist.get("provider") or "").strip()
    api_key = str(vision_assist.get("api_key") or "").strip()
    model = str(vision_assist.get("model") or "").strip()
    api_endpoint = vision_assist.get("api_endpoint") or None
    if not provider or not api_key or not model:
        raise RuntimeError("视觉后端配置不完整（提供商 / Key / 模型）")
    if not _model_supports_images(provider, model):
        raise RuntimeError(
            f"模型「{provider}/{model}」未标记支持传图，请换成视觉模型或配置辅助识图"
        )
    if _looks_like_text_only_model(model):
        raise RuntimeError(
            f"识图模型「{model}」看起来是纯文本（不是 VL/vision）。"
            f"请改成 qwen-vl-max / qwen-vl-plus / gpt-4o 等能看图的模型"
        )
    path = Path(frame_path)
    if not path.is_file():
        raise RuntimeError(f"画面不存在：{path}")

    if on_status:
        try:
            on_status(f"①′ 视觉查看 · {provider}/{model}…")
        except Exception:
            pass

    b64data, media_type = _image_b64(path, compress=True, max_size=_VISION_MAX_SIDE)
    # _image_b64 压缩后始终 png 编码
    media_type = "image/png"
    api = _provider_api(provider)
    q = (user_question or "").strip()
    task = task_spec or _vision_task_spec(question=q)
    user_text = (
        "下面附带真实图片。你是视觉观察员，逻辑模型会依据你的坐标采取行动。\n"
        "视觉任务单：\n"
        + json.dumps(task, ensure_ascii=False, indent=2)
        + "\n只返回一个 JSON 对象，禁止 Markdown 和额外散文。格式：\n"
        '{"image_size":[宽,高],"summary":"简述",'
        '"candidates":[{"id":"候选id","label":"像素事实名称",'
        '"bbox":[x0,y0,x1,y1],"confidence":0.0,'
        '"observations":"只写看到的事实","interpretation":"含义推断",'
        '"parent_id":null}],"recommended_candidate":null,'
        '"verified":null,"verification_reason":"",'
        '"uncertainties":[],"need_clarification":false,"clarifying_question":""}\n'
        "要求：bbox 必须是输入图片原始像素坐标；观察与推断分开；"
        "主动排除任务单 distinguish_from 中的相似对象；不确定时给多个候选，"
        "不得编造坐标。mode=verify 时必须把 verified 写成 true/false，"
        "并在 verification_reason 说明裁剪是否包含目标。\n"
        f"文件名：{path.name}"
    )
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
            "image_url": {
                "url": f"data:{media_type};base64,{b64data}",
                "detail": "high",
            },
        }
    # 图在前：部分 VL 网关对「先图后文」更稳
    content = [img_part, {"type": "text", "text": user_text}]
    system = (
        "You are the visual observer in a two-model collaboration. Inspect pixels, "
        "separate observations from interpretations, and return only the requested JSON."
    )
    text, inp, out = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=[{"role": "user", "content": content}],
        system_prompt=system,
        max_tokens=1800,
    )
    text = (text or "").strip()
    if _assist_vision_is_blind(text):
        raise RuntimeError(
            f"识图模型「{model}」返回「看不到图」。"
            "请确认当前模型确实支持视觉（如 deepseek-flash / qwen-vl-* / gpt-4o），"
            "不要用 qwen3.5-flash / deepseek 等纯文本模型"
        )
    return text, int(inp or 0), int(out or 0)


def _await_coro(coro):
    """在同步 tool resolver 里跑协程（tools 循环里的 resolve 是同步的）。"""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=180)


async def _assist_describe_frame(
    session: CollabSession,
    vision_assist: Optional[dict],
    *,
    user_question: str = "",
    mode: str = "locate",
    target: str = "",
    distinguish_from: Optional[list] = None,
    force: bool = False,
    on_status=None,
) -> dict[str, Any]:
    """**薄适配器**：单后端取证也走统一 Broker（缓存/预算/失效都不在这里）。

    保留这个函数只为兼容旧调用点；新代码请直接用 :func:`_vision_acquire`。
    """
    if not _path_is_file(session.frame_path):
        return {"ok": False, "error": "当前没有可用画面"}
    return _vision_acquire(
        session,
        path=Path(session.frame_path),
        primary_vision=vision_assist,
        vision_assist=None,
        mode=mode,
        question=user_question,
        target=target,
        distinguish_from=distinguish_from,
        force=force,
        on_status=on_status,
        trigger="assist_describe",
    )


async def _assist_inspect_asset(
    session: CollabSession,
    vision_assist: Optional[dict],
    *,
    asset_id: str,
    question: str,
    mode: str = "verify",
    target: str = "",
    distinguish_from: Optional[list] = None,
    on_status=None,
) -> dict[str, Any]:
    """**薄适配器**：查看任意会话素材（与当前帧共用同一指纹/缓存/预算规则）。"""
    from backend.script_generator.collaborator.canvas import resolve_template_path

    path, asset, err = resolve_template_path(
        session,
        asset_id=asset_id,
        # 只是"看一眼"：用 explicit_view（标注图也能被显式查看，但不作模板匹配）
        use="explicit_view",
    )
    if err or path is None:
        return {"ok": False, "error": err or "找不到要查看的素材"}
    data = _vision_acquire(
        session,
        path=Path(path),
        primary_vision=vision_assist,
        vision_assist=None,
        resource_id=asset_id,
        mode=mode,
        question=question,
        target=target,
        distinguish_from=distinguish_from,
        on_status=on_status,
        trigger="assist_inspect",
    )
    if data.get("ok"):
        data["name"] = (asset or {}).get("name") or Path(path).name
        data["path"] = str(path)
        inspection = {
            "asset_id": asset_id,
            "path": str(path),
            "question": question,
            "text": str(data.get("text") or ""),
            "structured": bool(data.get("structured")),
            "verified": (data.get("vision") or {}).get("result", {}).get("verified")
            if mode == "verify"
            else None,
            "vision": dict(data.get("vision") or {}),
        }
        data["inspection"] = inspection
        session.artifacts["last_asset_inspect"] = inspection
        history = session.artifacts.setdefault("asset_inspection_log", [])
        if isinstance(history, list):
            history.append({"kind": "inspect", **inspection})
            del history[:-30]
        if mode == "verify" and inspection["verified"] is True:
            verified = session.artifacts.setdefault("verified_asset_ids", [])
            if isinstance(verified, list) and asset_id not in verified:
                verified.append(asset_id)
    return data


def _vision_backend_label(config: Optional[dict]) -> str:
    if not isinstance(config, dict):
        return ""
    return "/".join(
        x for x in (str(config.get("provider") or "").strip(), str(config.get("model") or "").strip()) if x
    )


# ── Phase B2：视觉取证全部走**唯一**的 VisualEvidenceBroker ──────────────
#
# 这里只做三件事：① 组装 VisualEvidenceRequest（指纹材料）；② 把真实模型调用
# 包成 describe(backend_label) 回调；③ 把 Broker 的结果翻译回旧返回形状。
# **缓存、预算、失效、主辅回退顺序都不在这里**（那些规则只有一份，在
# ``backend/script_generator/visual_evidence.py``）。

#: content_hash 很贵（要读整张图），按 (路径, mtime, size) 记忆一次。
#: ⚠️ **Phase B2 起不再用 stat 当内容身份**：伪造 mtime / 同大小改写都骗得过 stat，
#: 所以内容摘要一律走 :func:`_verified_content_hash`（Binder 已核验的 hash，或读字节）。
#: 旧的 ``_cached_content_hash``（stat → 摘要的跳读缓存）已删除，避免被误当成内容事实。


def _marks_revision(session: CollabSession) -> str:
    from backend.script_generator.visual_evidence import marks_revision

    marks = []
    for key in ("marks", "viewport_marks"):
        value = (session.artifacts or {}).get(key) if isinstance(session.artifacts, dict) else None
        if value:
            marks.extend(value if isinstance(value, list) else [value])
    overlays = list(getattr(session, "overlays", None) or [])
    if overlays:
        marks.extend(overlays)
    return marks_revision(marks)


def _visual_broker_for(
    session: CollabSession,
    *,
    evidence_store: Any = None,
    turn_id: str = "",
) -> Any:
    """一次主轮一个 Broker（预算按主轮计）；L1 挂在会话上，L2 用 V3 evidence 表。"""
    from backend.script_generator.collaborator.vision_broker import (
        SessionVisionCache,
        SessionVisionTelemetry,
    )
    from backend.script_generator.visual_evidence import (
        NullEvidenceStore,
        VisualEvidenceBroker,
    )

    broker = getattr(session, "_visual_broker", None)
    if evidence_store is None:
        evidence_store = getattr(session, "_evidence_store", None)
    if isinstance(broker, VisualEvidenceBroker) and getattr(broker, "_l2_label", "") == (
        "v3_evidence" if evidence_store is not None else "none"
    ):
        return broker
    l2: Any = NullEvidenceStore()
    if evidence_store is not None:
        try:
            from backend.script_generator.collaborator_v3.adapters.visual_evidence_store import (
                V3EvidenceStore,
            )

            l2 = V3EvidenceStore(evidence_store, turn_id=turn_id)
        except Exception:  # noqa: BLE001 - 拿不到 L2 就退回"只有 L1"，不假装有权威源
            l2 = NullEvidenceStore()
    broker = VisualEvidenceBroker(
        l1=SessionVisionCache(session),
        l2=l2,
        telemetry=SessionVisionTelemetry(session),
    )
    broker._l2_label = str(getattr(l2, "label", "") or "")  # type: ignore[attr-defined]
    session._visual_broker = broker  # type: ignore[attr-defined]
    return broker


def new_visual_broker(session: CollabSession, *, evidence_store: Any = None, turn_id: str = "") -> Any:
    """开新主轮时重置预算（缓存仍然跨主轮复用）。"""
    if evidence_store is not None:
        # 挂到会话上：这一轮里所有视觉调用点都从同一处取 L2（不逐个传参）
        session._evidence_store = evidence_store  # type: ignore[attr-defined]
    try:
        delattr(session, "_visual_broker")
    except AttributeError:
        pass
    return _visual_broker_for(
        session,
        evidence_store=evidence_store if evidence_store is not None else getattr(session, "_evidence_store", None),
        turn_id=turn_id,
    )


def _vision_backend_adapter(config: Optional[dict], *, kind: str) -> Any:
    """把一个 V2 视觉配置包成 ``VisionBackend``（只负责"真的发一次请求"）。"""
    from backend.script_generator.visual_evidence import VisionBackend  # noqa: F401  (协议文档)

    class _Adapter:
        def __init__(self) -> None:
            self._label = _vision_backend_label(config) or f"{kind}:unconfigured"
            self._ready = _vision_assist_ready(config)

        @property
        def label(self) -> str:
            return self._label

        @property
        def available(self) -> bool:
            return bool(self._ready)

    return _Adapter()


def _verified_content_hash(
    session: CollabSession,
    path: Path,
    *,
    resource_id: str = "",
) -> tuple[str, str, str]:
    """取"这张图的内容摘要"：**优先 Phase B Binder 已登记并核验过的 hash**。

    返回 ``(content_hash, source, failure_code)``：

    - 资源已登记且 Binder 核验通过 → 用它的 ``content_hash``（**不重复读盘**）；
    - Binder 报 ``RESOURCE_CONTENT_CHANGED`` → 返回失败码（**不得签发视觉证据**：
      磁盘内容和登记版本不一致，坐标/结论都会漂）；
    - 临时图片（没登记） → 读文件字节算 SHA-256。

    ``(path, mtime, size)`` **不参与**内容身份：伪造 mtime / 同大小改写都骗得过 stat，
    所以临时图片一律真读字节（Phase B2 人工要求）。
    """
    from backend.script_generator.resource_binding import (
        RESOURCE_CONTENT_CHANGED,
        RESOURCE_NOT_FOUND,
        ResourceBinder,
        ResourceBindingError,
        SessionResourceSource,
    )
    from backend.script_generator.visual_evidence import resolve_content_hash

    bound_hash = ""
    binder = ResourceBinder(SessionResourceSource(session), origin="visual_evidence")
    candidate = str(resource_id or "").strip()
    if not candidate:
        # 没点名资源时，按路径找有没有登记过（同一张图仍走"已核验"这条路）
        try:
            from backend.script_generator.collaborator.resource_registry import resource_for_path

            known = resource_for_path(session, path)
            candidate = str((known or {}).get("resource_id") or "")
        except Exception:  # noqa: BLE001
            candidate = ""
    if candidate:
        try:
            bound = binder.bind(candidate, uses=("explicit_view",))
            bound_hash = str(bound.content_hash or "")
        except ResourceBindingError as exc:
            if exc.code == RESOURCE_CONTENT_CHANGED:
                return "", "content_changed", RESOURCE_CONTENT_CHANGED
            if exc.code != RESOURCE_NOT_FOUND:
                # 其它失败（版本失效/类型/权限/文件缺失）不在这里改写语义：
                # 由工具侧的错误路径负责；这里只退回"按字节算"。
                pass
    digest, source = resolve_content_hash(str(path), bound_hash=bound_hash)
    return digest, source, ""


def _vision_request_for(
    session: CollabSession,
    *,
    path: Path,
    resource_id: str = "",
    mode: str = "locate",
    question: str = "",
    target: str = "",
    region: Any = (),
    origin: str = "",
) -> tuple[Any, str, str]:
    """组装取证请求；返回 ``(request, content_hash_source, failure_code)``。"""
    from backend.script_generator.visual_evidence import (
        VISION_PROTOCOL_VERSION,
        VisualEvidenceRequest,
        question_kind_from,
    )

    content_hash, hash_source, failure = _verified_content_hash(
        session, Path(path), resource_id=resource_id
    )
    if failure:
        return None, hash_source, failure
    kind = question_kind_from(
        (session.artifacts or {}).get("vision_question_kind") if isinstance(session.artifacts, dict) else "",
        target,
        question,
        mode=mode,
    )
    return (
        VisualEvidenceRequest(
            question_kind=kind,
            resource_id=str(resource_id or ""),
            content_hash=content_hash,
            target=str(target or question or ""),
            region=region,
            mark_revision=_marks_revision(session),
            vision_protocol=_VISION_PROTOCOL_VERSION or VISION_PROTOCOL_VERSION,
            backend_version="",
            question=str(question or ""),
            mode=str(mode or "locate"),
            path=str(path),
            origin=str(origin or ""),
        ),
        hash_source,
        "",
    )


def _run_vision_backend(
    session: CollabSession,
    config: dict,
    *,
    path: Path,
    mode: str,
    question: str,
    target: str,
    distinguish_from: Optional[list],
    on_status=None,
) -> dict[str, Any]:
    """单后端调用（**不做**缓存/预算判断）：返回 Broker 需要的原始字段。"""
    task = _vision_task_spec(
        question=question, mode=mode, target=target, distinguish_from=distinguish_from
    )
    # 共同视口内容只在模型主动查看时读取。用户标记属于视口的一层，
    # 此时才连同像素交给视觉观察员，而不是每轮主动塞进主模型提示词。
    try:
        from backend.script_generator.collaborator.canvas import overlays_summary_for_prompt

        task["viewport_annotations"] = overlays_summary_for_prompt(
            list(getattr(session, "overlays", None) or [])
        )
    except Exception:
        task["viewport_annotations"] = "（读取失败）"
    if not _vision_assist_ready(config):
        return {"error": f"视觉后端未配置（{_vision_backend_label(config) or 'unknown'}）"}
    try:
        desc, vin, vout = _await_coro(
            describe_collab_frame(
                frame_path=Path(path),
                vision_assist=config or {},
                user_question=question,
                task_spec=task,
                on_status=on_status,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 后端异常记成失败尝试
        return {"error": str(exc)}
    text = str(desc or "")
    if not text.strip():
        return {"error": "辅助识图返回为空"}
    return {
        "text": text,
        "parsed": _parse_vision_payload(text, mode=mode),
        "tokens_in": int(vin or 0),
        "tokens_out": int(vout or 0),
    }


def _image_size(path: Path) -> tuple[int, int]:
    """磁盘图片的 (宽, 高)；读不到返回 (0, 0)。"""
    try:
        from backend.script_generator.vision_tools import _imread

        image = _imread(Path(path))
        return int(image.shape[1]), int(image.shape[0])
    except Exception:  # noqa: BLE001
        return 0, 0


def _frame_asset_id(session: CollabSession) -> str:
    """底图在素材区里的 asset id（有就带上，让证据能按资源失效/复用）。"""
    path = getattr(session, "frame_path", None)
    if not path:
        return ""
    try:
        target = Path(path).resolve()
    except OSError:
        return ""
    for item in list(getattr(session, "assets", None) or []):
        try:
            if Path(str((item or {}).get("path") or "")).resolve() == target:
                return str(item.get("id") or "")
        except OSError:
            continue
    return ""


def _vision_acquire(
    session: CollabSession,
    *,
    path: Path,
    primary_vision: Optional[dict],
    vision_assist: Optional[dict],
    resource_id: str = "",
    mode: str = "locate",
    question: str = "",
    target: str = "",
    distinguish_from: Optional[list] = None,
    force: bool = False,
    on_status=None,
    evidence_store: Any = None,
    trigger: str = "",
    local_cv: Any = None,
) -> dict[str, Any]:
    """唯一取证入口：指纹 → 缓存 → 本地 CV → 主模型视觉 → 辅助识图 → 一次复核。"""
    from backend.script_generator.visual_evidence import VisualEvidenceRequest

    broker = _visual_broker_for(session, evidence_store=evidence_store)
    request, hash_source, failure = _vision_request_for(
        session,
        path=Path(path),
        resource_id=resource_id,
        mode=mode,
        question=question,
        target=target,
        origin=trigger,
    )
    if request is None:
        # 内容与登记版本不一致（Phase B 的 RESOURCE_CONTENT_CHANGED）→ 不签发视觉证据
        return {
            "ok": False,
            "error": "登记资源的内容已变化，不能据此签发视觉证据；请重新登记版本后再取证",
            "code": failure,
            "cached": False,
            "l2_authoritative": broker.l2_authoritative,
            "cache_scope": broker.cache_scope,
            "content_hash_source": hash_source,
            "tokens_in": 0,
            "tokens_out": 0,
            "groundings": [],
            "text": "",
        }
    configs: dict[str, dict] = {}
    for config, kind in ((primary_vision, "primary"), (vision_assist, "assist")):
        label = _vision_backend_label(config)
        if label:
            configs[label] = config or {}
    primary = _vision_backend_adapter(primary_vision, kind="primary")
    assist = _vision_backend_adapter(vision_assist, kind="assist")

    def describe(label: str) -> dict[str, Any]:
        config = configs.get(label)
        if config is None:
            return {"error": f"没有配置这个视觉后端：{label}"}
        return _run_vision_backend(
            session,
            config,
            path=Path(path),
            mode=mode,
            question=question,
            target=target,
            distinguish_from=distinguish_from,
            on_status=on_status,
        )

    try:
        source_size = _image_size(Path(path))
    except Exception:  # noqa: BLE001
        source_size = (0, 0)

    result = broker.acquire(
        request,
        describe=describe,
        primary=primary,
        assist=assist,
        local_cv=local_cv,
        user_requested=bool(force),
        source_size=source_size,
        trigger=trigger or "visual_gap",
    )
    data = result.to_dict()
    data["vision"] = dict((result.evidence.payload if result.evidence else {}) or {})
    data["structured"] = bool(result.evidence and result.evidence.structured)
    data["validation_errors"] = list(data["vision"].get("validation_errors") or [])
    data["cached"] = bool(result.cached)
    data["visual_source"] = (
        "primary" if result.source == "primary" else ("assist" if result.source == "assist" else result.source)
    )
    data["visual_backend"] = str(getattr(result.evidence, "backend", "") or "")
    data["visual_fingerprint"] = request.fingerprint()
    # 内容摘要来源（bound_resource = 用 Phase B Binder 已核验的 hash，没重复读盘）
    data["content_hash_source"] = hash_source
    data["content_hash"] = str(request.content_hash or "")
    # 只有 L1 时必须如实说明：不是跨主轮权威缓存
    data["l2_authoritative"] = broker.l2_authoritative
    data["cache_scope"] = broker.cache_scope
    data["error"] = "" if result.ok else (result.errors[0] if result.errors else result.code)
    if result.errors and result.source != "primary":
        # 旧契约：主模型视觉失败的原因要带出来（现在由 Broker 的 attempts 汇总）
        data["primary_visual_error"] = str(result.errors[0])
    if result.ok and result.evidence is not None:
        payload = dict(result.evidence.payload)
        text = str(payload.get("text") or "")
        data["text"] = text
        data["fed_size"] = list(payload.get("fed_size") or [])
        if resource_id:
            data["asset_id"] = resource_id
        # grounding 登记仍留在 V2（V2 自己的来源链），但**送入尺寸由 Broker 给**
        # （既有不变量：坐标按实际送入尺寸换算，不信视觉模型自报尺寸）。
        data["groundings"] = _record_vision_groundings(
            session,
            {
                "structured": True,
                "candidates": list(payload.get("candidates") or []),
                "image_size": list(payload.get("vision_reported_size") or []),
                "summary": str(payload.get("observations") or ""),
            },
            source_path=Path(path),
            source_asset_id=resource_id,
            mode=mode,
            fed_size=tuple(payload.get("fed_size") or ()),
        )
    else:
        data["text"] = ""
        data["groundings"] = []
    return data


async def _describe_frame_with_fallback(
    session: CollabSession,
    primary_vision: Optional[dict],
    vision_assist: Optional[dict],
    **kwargs,
) -> dict[str, Any]:
    """**薄适配器**（Phase B2）：主辅回退顺序/缓存/预算全部交给统一 Broker。

    顺序固定：查缓存 → 本地 CV（若给了 probe）→ 主逻辑模型视觉 → 不可用或结果
    不合格才用辅助识图 → 最多一次针对性复核。这里不再自己维护缓存或预算。
    """
    if not _path_is_file(session.frame_path):
        return {"ok": False, "error": "当前没有可用画面"}
    force = bool(kwargs.pop("force", False))
    local_cv = kwargs.pop("local_cv", None)
    evidence_store = kwargs.pop("evidence_store", None)
    trigger = str(kwargs.pop("trigger", "") or "frame_vision")
    # 旧调用点用的是 user_question（V2 历史命名），统一映射到 question
    question = str(kwargs.pop("user_question", "") or kwargs.pop("question", "") or "")
    return _vision_acquire(
        session,
        path=Path(session.frame_path),
        primary_vision=primary_vision,
        vision_assist=vision_assist,
        resource_id=_frame_asset_id(session),
        question=question,
        mode=str(kwargs.pop("mode", "locate") or "locate"),
        target=str(kwargs.pop("target", "") or ""),
        distinguish_from=kwargs.pop("distinguish_from", None),
        on_status=kwargs.pop("on_status", None),
        force=force,
        local_cv=local_cv,
        evidence_store=evidence_store,
        trigger=trigger,
        **kwargs,
    )


async def _inspect_asset_with_fallback(
    session: CollabSession,
    primary_vision: Optional[dict],
    vision_assist: Optional[dict],
    **kwargs,
) -> dict[str, Any]:
    """**薄适配器**：查看指定素材（asset_id 必填，不再回退焦点素材）。"""
    from backend.script_generator.collaborator.canvas import resolve_template_path

    asset_id = str(kwargs.pop("asset_id", "") or "")
    question = str(kwargs.pop("question", "") or "")
    mode = str(kwargs.pop("mode", "verify") or "verify")
    target = str(kwargs.pop("target", "") or "")
    distinguish_from = kwargs.pop("distinguish_from", None)
    on_status = kwargs.pop("on_status", None)
    evidence_store = kwargs.pop("evidence_store", None)
    trigger = str(kwargs.pop("trigger", "") or "asset_vision")
    path, asset, err = resolve_template_path(
        session, asset_id=asset_id, use="explicit_view"
    )
    if err or path is None:
        return {"ok": False, "error": err or "找不到要查看的素材"}
    data = _vision_acquire(
        session,
        path=Path(path),
        primary_vision=primary_vision,
        vision_assist=vision_assist,
        resource_id=asset_id,
        mode=mode,
        question=question,
        target=target,
        distinguish_from=distinguish_from,
        on_status=on_status,
        evidence_store=evidence_store,
        trigger=trigger,
    )
    if data.get("ok"):
        data["name"] = (asset or {}).get("name") or Path(path).name
        data["path"] = str(path)
        data["asset_id"] = asset_id
        inspection = {
            "asset_id": asset_id,
            "path": str(path),
            "question": question,
            "text": str(data.get("text") or ""),
            "structured": bool(data.get("structured")),
            "verified": (data.get("vision") or {}).get("result", {}).get("verified")
            if mode == "verify"
            else None,
            "vision": dict(data.get("vision") or {}),
        }
        data["inspection"] = inspection
        session.artifacts["last_asset_inspect"] = inspection
        history = session.artifacts.setdefault("asset_inspection_log", [])
        if isinstance(history, list):
            history.append({"kind": "inspect", **inspection})
            del history[:-30]
        if mode == "verify" and inspection["verified"] is True:
            verified = session.artifacts.setdefault("verified_asset_ids", [])
            if isinstance(verified, list) and asset_id not in verified:
                verified.append(asset_id)
    return data



def _asset_compare_sheet(session: CollabSession, asset_ids: list[str]) -> tuple[Optional[Path], list[dict], str]:
    """把 2~9 张素材排成编号对照板；不登记为用户素材。"""
    from PIL import Image, ImageDraw
    from backend.script_generator.collaborator.asset_ops import COLLAB_CROP_ROOT
    from backend.script_generator.collaborator.canvas import resolve_template_path

    ids = list(dict.fromkeys(str(x or "").strip() for x in asset_ids if str(x or "").strip()))
    if len(ids) < 2:
        return None, [], "多素材比较至少需要 2 个不同的 asset_id"
    if len(ids) > 9:
        return None, [], "一次最多比较 9 张素材；请先用 list_session_assets 缩小范围"
    resolved: list[tuple[Path, dict]] = []
    for aid in ids:
        path, asset, err = resolve_template_path(session, asset_id=aid)
        if err or path is None:
            return None, [], f"素材 {aid} 无效：{err or '找不到文件'}"
        resolved.append((Path(path), dict(asset or {})))

    digest_src = "|".join(
        f"{p}:{p.stat().st_mtime_ns if p.exists() else 0}" for p, _ in resolved
    )
    digest = hashlib.sha1(digest_src.encode("utf-8", errors="ignore")).hexdigest()[:12]
    sid = re.sub(r"[^\w\-]+", "_", str(getattr(session, "session_id", "") or "session"))[:50]
    out_dir = COLLAB_CROP_ROOT / (sid or "session")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"_asset_compare_{digest}.png"

    cols = min(3, len(resolved))
    rows = (len(resolved) + cols - 1) // cols
    cell_w, cell_h, label_h = 480, 320, 28
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "#202124")
    draw = ImageDraw.Draw(sheet)
    manifest: list[dict] = []
    for index, (path, asset) in enumerate(resolved):
        panel = chr(ord("A") + index)
        with Image.open(path) as src:
            image = src.convert("RGB")
            original_size = list(image.size)
            image.thumbnail((cell_w - 16, cell_h - label_h - 16), Image.Resampling.LANCZOS)
            x0 = (index % cols) * cell_w
            y0 = (index // cols) * cell_h
            px = x0 + (cell_w - image.width) // 2
            py = y0 + label_h + (cell_h - label_h - image.height) // 2
            sheet.paste(image, (px, py))
        draw.rectangle((x0, y0, x0 + cell_w - 1, y0 + label_h), fill="#111827")
        draw.text((10 + x0, 7 + y0), panel, fill="white")
        manifest.append(
            {
                "panel": panel,
                "asset_id": str(asset.get("id") or ids[index]),
                "name": str(asset.get("name") or path.name),
                "source_size": original_size,
            }
        )
    if not out_path.exists():
        sheet.save(out_path, format="PNG", optimize=True)
    return out_path, manifest, ""


async def _compare_assets_with_fallback(
    session: CollabSession,
    primary_vision: Optional[dict],
    vision_assist: Optional[dict],
    *,
    asset_ids: list[str],
    question: str,
    target: str = "",
    distinguish_from: Optional[list] = None,
    on_status=None,
) -> dict[str, Any]:
    """一次视觉调用联合比较多张素材，避免单图结果冒充跨图结论。"""
    sheet, manifest, err = _asset_compare_sheet(session, asset_ids)
    if err or sheet is None:
        return {"ok": False, "error": err or "无法建立素材对照板"}
    mapping = "；".join(
        f"{item['panel']}=[[{item['asset_id']}]]「{item['name']}」" for item in manifest
    )
    compare_question = (
        f"这是多素材对照板，面板映射：{mapping}。\n"
        f"请只基于实际可见面板联合比较，不要把只出现在一张图里的元素说成共有。\n{question}"
    )
    configs: list[tuple[str, dict]] = []
    if _vision_assist_ready(primary_vision):
        configs.append(("primary", dict(primary_vision or {})))
    if _vision_assist_ready(vision_assist) and (
        _vision_backend_label(vision_assist) != _vision_backend_label(primary_vision)
    ):
        configs.append(("assist", dict(vision_assist or {})))
    if not configs:
        return {"ok": False, "error": "当前没有可用视觉模型，无法比较素材内容"}
    task = _vision_task_spec(
        question=compare_question,
        mode="compare",
        target=target,
        distinguish_from=distinguish_from,
    )
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    cache = session.artifacts.setdefault("vision_cache", {})
    errors: list[str] = []
    desc = ""
    vin = vout = 0
    parsed: dict[str, Any] = {}
    source = ""
    config: dict = {}
    for candidate_source, candidate_config in configs:
        cache_key = _vision_cache_key(
            sheet,
            model=str(candidate_config.get("model") or ""),
            task=task,
        )
        cached = cache.get(cache_key) if isinstance(cache, dict) else None
        if isinstance(cached, dict) and cached.get("kind") == "asset_compare":
            result = dict(cached.get("data") or {})
            result.update({"ok": True, "cached": True, "tokens_in": 0, "tokens_out": 0})
            record = dict(result.get("record") or {})
            session.artifacts["last_asset_compare"] = record
            return result
        try:
            desc, vin, vout = await describe_collab_frame(
                frame_path=sheet,
                vision_assist=candidate_config,
                user_question=compare_question,
                task_spec=task,
                on_status=on_status,
            )
        except Exception as exc:
            errors.append(f"{candidate_source}: {exc}")
            continue
        parsed = _parse_vision_payload(desc, mode="compare")
        if parsed.get("structured"):
            source, config = candidate_source, candidate_config
            break
        errors.append(f"{candidate_source}: 返回未通过结构化校验")
    if not source:
        return {"ok": False, "error": "；".join(errors) or "视觉比较失败"}
    record = {
        "asset_ids": [x["asset_id"] for x in manifest],
        "manifest": manifest,
        "question": question,
        "text": desc,
        "structured": bool(parsed.get("structured")),
        "visual_source": source,
    }
    session.artifacts["last_asset_compare"] = record
    history = session.artifacts.setdefault("asset_inspection_log", [])
    if isinstance(history, list):
        history.append({"kind": "compare", **record})
        del history[:-30]
    result = {
        "ok": True,
        **record,
        "vision": parsed,
        "comparison_sheet": str(sheet),
        "visual_backend": _vision_backend_label(config),
        "tokens_in": int(vin or 0),
        "tokens_out": int(vout or 0),
        "cached": False,
        "record": record,
    }
    if isinstance(cache, dict):
        cache_key = _vision_cache_key(
            sheet, model=str(config.get("model") or ""), task=task
        )
        cache[cache_key] = {"kind": "asset_compare", "data": dict(result)}
        if len(cache) > 24:
            for old_key in list(cache)[:-24]:
                cache.pop(old_key, None)
    return result


def _frame_vision_status(session: CollabSession) -> str:
    """给主模型的短状态：有无识图缓存；正文仍由模型决定是否调工具取用。"""
    if not _path_is_file(session.frame_path):
        return ""
    cached = _cached_assist_vision(session)
    if cached:
        return (
            f"【识图缓存】已有（约 {len(cached)} 字）。"
            "需要画面细节时调用 describe_current_frame；图已换或要重看用 force=true。"
        )
    return (
        "【识图缓存】无。"
        "需要画面细节时调用 describe_current_frame（或 JSON describe_frame:true）。"
    )


def _slim_probe_for_prompt(probe: dict | None) -> str:
    if not isinstance(probe, dict) or not probe:
        return ""
    parts = [str(probe.get("summary") or "").strip()]
    best = probe.get("best")
    if isinstance(best, dict):
        parts.append(
            f"建议目标：第 {int(best.get('row', 0)) + 1} 行"
            f"第 {int(best.get('col', 0)) + 1} 列，约 {best.get('count')} 枚"
        )
    cells = probe.get("cells") or probe.get("counts")
    if isinstance(cells, list) and cells and len(parts) < 3:
        parts.append(f"共探测到 {len(cells)} 个格子/堆")
    return "\n".join(p for p in parts if p)


def run_frame_analyze(
    registry: SkillRegistry,
    session: CollabSession,
    *,
    skill_id: str | None = None,
) -> dict[str, Any]:
    """对当前画面跑技能 explore，写回 session.probe / logic。"""
    path = session.frame_path
    if not path or not _path_is_file(path):
        return {"ok": False, "error": "当前没有可用画面"}
    sid = (skill_id or session.skill_id or "").strip()
    sk = registry.get(sid) if sid else None
    if sk is None and session.goal_text:
        sk = registry.match_best(session.goal_text)
    if sk is None:
        if not registry.all():
            return {
                "ok": False,
                "error": "当前未注册任何识图玩法 skill，无法对像素读数；请用文字描述，或去「经典」Tab",
            }
        return {
            "ok": False,
            "error": "还没有对上具体玩法，画面已保存；请再说目标后再分析",
        }
    session.skill_id = sk.id
    try:
        result = sk.explore(session, Path(path))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if result.logic is not None:
        session.logic = result.logic
    session.probe = result.probe
    session.artifacts["last_explore"] = {
        "summary": result.summary,
        "debug_path": result.debug_path,
    }
    return {
        "ok": bool(result.ok),
        "skill_id": sk.id,
        "title": sk.title,
        "summary": result.summary,
        "shape_label": result.shape_label,
        "can_ack": result.can_ack,
        "chips": list(result.chips or []),
        "cta_text": result.cta_text,
        "probe_brief": _slim_probe_for_prompt(
            result.probe if isinstance(result.probe, dict) else None
        ),
        "debug_path": result.debug_path,
    }


def openai_collab_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_skills",
                "description": "列出已注册的协作玩法技能",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_skill_doc",
                "description": (
                    "按名称读取 backend/script_generator/skills 中跨 V2/V3/V4 共用的 "
                    "SKILL.md 工作流正文。skill 只用于指导工具编排，不可直接执行。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "list_skills 返回的说明书技能名称",
                        }
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "select_skill",
                "description": (
                    "后台挂上玩法能力（用户无感知）。根据用户目标自行调用，"
                    "不要在回复里让用户选择技能。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "技能 id（当前可能无已注册 skill）",
                        }
                    },
                    "required": ["skill_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_session_status",
                "description": "查看当前协作会话状态摘要",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_current_frame",
                "description": (
                    "对当前已载入的游戏画面跑已注册 skill 的识图探针。"
                    "若能力清单为空则勿调用（会失败）；改为 describe_current_frame 或请用户描述。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "可选；不传则用当前会话已对上的玩法，不对上则不分析",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_current_frame",
                "description": (
                    "按需查看共同视口截图。当前主模型支持视觉时由主模型自己看，"
                    "否则或主视觉失败时才回退独立辅助识图。"
                    "同图若已有缓存会直接返回；用户要求「再分析」或图已换时 force=true。"
                    "闲聊、改绑窗口、点不动诊断通常不必调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "具体视觉问题；避免只写‘看看画面’",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["locate", "verify", "compare", "count"],
                            "description": "视觉任务阶段，默认 locate",
                        },
                        "target": {
                            "type": "string",
                            "description": "要定位/核实的对象及其用途",
                        },
                        "distinguish_from": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "必须主动排除的相似对象或歧义",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "true=忽略缓存重新识图；默认 false",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_asset",
                "description": (
                    "按需查看素材区中的任意截图或裁剪，并回答指定问题。"
                    "优先使用当前主模型视觉，必要时回退独立辅助识图。"
                    "裁剪后应调用它复核内容和位置，不要只凭裁剪成功就继续。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "要查看的素材 id",
                            "x-resource": dict(RESOURCE_REF_PARAM),
                        },
                        "question": {"type": "string", "description": "针对该素材要核实的问题"},
                        "mode": {
                            "type": "string",
                            "enum": ["verify", "locate", "compare", "count"],
                            "description": "通常裁剪复核用 verify",
                        },
                        "target": {"type": "string", "description": "要核实的目标定义"},
                        "distinguish_from": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "需要排除的相似内容",
                        },
                    },
                    "required": ["asset_id", "question"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "compare_assets",
                "description": (
                    "把素材区中的 2~9 张图组成编号对照板，并在一次视觉调用中联合比较。"
                    "用户说‘看素材区/比较这些素材/找共同元素’时先 list_session_assets，"
                    "再把所有相关 asset_id 一次传入；不得只看一张却声称比较了多张。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 9,
                            "description": "要联合查看的不同素材 id，按期望面板顺序排列",
                            "x-resource": dict(RESOURCE_REF_PARAM),
                        },
                        "question": {
                            "type": "string",
                            "description": "跨图问题，例如找全部素材共有的固定结构及差异",
                        },
                        "target": {"type": "string", "description": "要比较的目标定义"},
                        "distinguish_from": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "需要排除的相似区域或错误解释",
                        },
                    },
                    "required": ["asset_ids", "question"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fit_repeated_regions",
                "description": (
                    "对规则或近似规则排列的重复 UI（卡片、勋章、格子、列表项）做本地精确外框拟合。"
                    "识图模型只需先给出若干/全部粗定位 grounding_id；本工具不会直接生成最终模板，"
                    "只返回候选框和带编号预览。预览必须 inspect_asset(mode=verify) 后才能提交。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "grounding_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "describe_current_frame 返回的同类目标 grounding_id",
                        },
                        "expected_count": {"type": "integer", "description": "期望数量；未知可为 0"},
                        "name_prefix": {"type": "string", "description": "最终模板名称前缀"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "commit_repeated_regions",
                "description": (
                    "提交 fit_repeated_regions 的整批裁剪。仅当其预览素材已经由 inspect_asset(mode=verify)"
                    "明确验证通过时才会执行；失败整批回滚。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"fit_id": {"type": "string"}},
                    "required": ["fit_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "crop_asset",
                "description": (
                    "裁剪素材并**自动加入素材区**（不需要写文件权限）。"
                    "常用于「模板里混进了会变的信息」：只留固定不变的那块。"
                    "rect 用归一化坐标 0~1（x0,y0,x1,y1）或像素整数；"
                    "裁完返回新 asset_id，可直接拿去 match_template。"
                ),
                                "x-result": {
                                    "type": "object",
                                    "required": ["asset_id", "path"],
                                    "properties": {
                                        "asset_id": {"type": "string"},
                                        "path": {"type": "string"},
                                    },
                                },
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "要裁剪的素材 id（**必填**：不再回退焦点素材）",
                            "x-resource": resource_ref_param(
                                materialize_as="template_path",
                                roles=("template", "scene"),
                                use="explicit_view",
                                semantic="被裁剪的源素材",
                                base_types=("image",),
                            ),
                        },
                        "template_path": {
                            "type": "string",
                            "description": "没有 asset_id 时可给路径",
                        },
                        "rect": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "x0,y0,x1,y1；0~1 视为归一化，>1 视为像素",
                        },
                        "vision_candidate_id": {
                            "type": "string",
                            "description": (
                                "describe_current_frame/inspect_asset 返回的 grounding_id，"
                                "或共同视口用户标记的 candidate id；裁剪坐标以它为准"
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": "新素材名（省略则自动命名）",
                        },
                    },
                    "required": ["vision_candidate_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "match_template",
                "x-search-terms": [
                    "template matching", "image analysis", "find object in image",
                    "模板匹配", "图片分析", "识图", "查找目标", "图标",
                ],
                "description": (
                    "用模板在明确指定的目标素材（默认共同视口底图）中匹配，返回 score/坐标。"
                    "模板传 asset_id/template_path；目标传 target_asset_id/target_path。"
                    "禁止模板与目标是同一个文件；目标为共同视口时会叠加匹配框。"
                    "禁止一次对多张素材全量匹配。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "会话素材 id（推荐）",
                            "x-resource": resource_ref_param(
                                materialize_as="template_path",
                                role="template",
                                use="explicit_match",
                                semantic="待寻找目标模板",
                            ),
                        },
                        "template_path": {
                            "type": "string",
                            "description": "模板路径或文件名；无 asset_id 时用",
                        },
                        "target_asset_id": {
                            "type": "string",
                            "description": "目标素材 id；省略则匹配当前共同视口底图",
                            "x-resource": resource_ref_param(
                                materialize_as="target_path",
                                roles=("scene", "template", "crop"),
                                use="explicit_match",
                                semantic="被搜索场景",
                            ),
                        },
                        "target_path": {
                            "type": "string",
                            "description": "目标图片路径；无 target_asset_id 时可用",
                        },
                        "threshold": {
                            "type": "number",
                            "description": "匹配阈值，默认 0.9",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "match_template_multi",
                "x-completion": {
                    "result_is_terminal_by_default": True,
                    "business_result_fields": ["count", "matches"],
                    "retry_requires_changed_inputs": True,
                    "cacheable": True,
                    "snapshot_parameters": ["asset_id", "target_asset_id"],
                },
                "x-search-terms": [
                    "count objects in image", "object count", "image analysis",
                    "template matching", "multi target matching",
                    "目标计数", "数量", "统计", "重复目标", "模板匹配",
                    "图片分析", "识图", "图标计数", "勋章",
                ],
                "description": (
                    "用一个模板在一张目标图中查找多个实例，进行跨尺度候选去重，"
                    "返回全部命中的中心、矩形框、分数和尺度。适合卡片、按钮、格子等重复目标；"
                    "不要再用 run_probe_script 手写 cv2.matchTemplate。默认目标是共同视口，"
                    "也可明确指定 target_asset_id/target_path。命中共同视口时会绘制全部框。"
                ),
                                "x-result": {
                                    "type": "object",
                                    "required": ["count", "matches"],
                                    "properties": {
                                        "count": {"type": "integer"},
                                        "matches": {"type": "array"},
                                    },
                                },
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "模板素材 id（推荐）",
                            "x-resource": resource_ref_param(
                                materialize_as="template_path",
                                role="template",
                                use="explicit_match",
                                semantic="待寻找目标模板",
                            ),
                        },
                        "template_path": {"type": "string", "description": "无 asset_id 时使用的模板路径"},
                        "target_asset_id": {
                            "type": "string",
                            "description": "目标素材 id；省略则用共同视口",
                            "x-resource": resource_ref_param(
                                materialize_as="target_path",
                                roles=("scene", "template", "crop"),
                                use="explicit_match",
                                semantic="被搜索场景",
                            ),
                        },
                        "target_path": {"type": "string", "description": "无 target_asset_id 时使用的目标路径"},
                        "threshold": {"type": "number", "description": "匹配阈值，默认0.9"},
                        "max_count": {"type": "integer", "minimum": 1, "maximum": 200, "description": "最多返回数量，默认50"},
                        "min_distance": {"type": "integer", "minimum": 1, "description": "命中中心去重距离（像素），默认模板短边的一半"},
                        "expected_count": {"type": "integer", "minimum": 0, "description": "预期目标数；仅用于结果核对，不会篡改命中"},
                        "search_region": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                            "description": "可选搜索区域[x0,y0,x1,y1]，全为0~1时按归一化坐标，否则按像素",
                        },
                        "scale_hint": {"type": "number", "description": "可选模板尺度中心"},
                        "scales": {"type": "array", "items": {"type": "number"}, "description": "可选明确尺度列表"},
                        "use_color_check": {"type": "boolean", "description": "是否增加颜色一致性检查"},
                        "color_tol": {"type": "number", "description": "颜色检查容差，默认30"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_session_assets",
                "description": (
                    "列出并筛选素材区（id/name/role/group/尺寸/是否看过）。"
                    "用户提到素材区、或你不确定素材区里有什么时，**先调这个**，"
                    "别猜也别问用户「你说的是哪张」。素材较多时先按 query/group/role "
                    "缩小到同类，再交给 compare_assets。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "按名称、说明或 id 模糊筛选"},
                        "group": {"type": "string", "description": "只保留指定素材分组"},
                        "role": {"type": "string", "description": "只保留指定角色，如 screenshot/template"},
                        "only_uninspected": {"type": "boolean", "description": "只列尚未实际识图检查的素材"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 40, "description": "最多返回数量，默认40"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "show_asset_in_viewport",
                "description": (
                    "把素材区指定图片切换到共同视口，供用户和模型共同查看或展示结果。"
                    "这是本地视图操作，不会自动发送用户消息或开启新回合。"
                    "用户要求‘切到/展示/放到共同视口’时使用；若只要求分析素材，直接 inspect_asset/compare_assets。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "要展示的素材 id",
                            "x-resource": dict(RESOURCE_REF_PARAM),
                        }
                    },
                    "required": ["asset_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_probe_script",
                "description": (
                    "执行一段临时 Python 探针脚本（需 probe_script 权限）。"
                    "预置：frame, FRAME_PATH, match(path, threshold=0.9), matcher, cv2, np, "
                    "Path, PROJECT_ROOT, ASSETS, list_assets(), resolve_image(name_or_path), "
                    "read_image(name_or_path)（中文路径安全，读不到返回 None）, "
                    "save_image/write_text（需写文件权限）, "
                    "runtime_call（需控制窗口权限）。把结论赋给 result。"
                    "读图/定位一律用 read_image(名字) 或 resolve_image(名字)："
                    "它会把素材名解析成真实路径，不要自己拼相对路径、更不要用 sys.argv。"
                    "save_image 写出的图会按「探针产出」登记进素材区：它是产出物、"
                    "不再被当场景素材自动识图；要用它当模板就显式传 asset_id。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "完整 Python 源码",
                        },
                        "script_id": {
                            "type": "string",
                            "description": "脚本的稳定标识；修改同一脚本时沿用，用于保留创建时间并刷新更新时间",
                        },
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_permissions",
                "description": "查看协作页当前权限开关状态",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_runtime_context",
                "description": (
                    "获取账号窗口绑定与近期相关日志摘要(包括试运行）。"
                    "用户抱怨点不动、没反应、空点击时先看这个。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account": {
                            "type": "string",
                            "description": "账号名；省略则用当前选中账号",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_click_target",
                "description": (
                    "检查绑定窗口是外壳还是渲染/操作层，列出同进程候选与推荐 hwnd。"
                    "适用于 MuMu/雷电/夜神等，也适用于其它「能截图但点不动」的 Win32 目标。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account": {"type": "string", "description": "账号名，可选"},
                        "hwnd": {
                            "type": "integer",
                            "description": "直接指定 hwnd；省略则用账号绑定窗",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "message_click_probe",
                "description": (
                    "对目标窗发一次后台消息点击并用前后截图像素差验证是否吃点击。"
                    "用于区分外壳/镜像与真正操作层。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account": {"type": "string"},
                        "hwnd": {"type": "integer"},
                        "x": {"type": "integer", "description": "客户区 x，省略则点中心"},
                        "y": {"type": "integer", "description": "客户区 y，省略则点中心"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "retarget_click_window",
                "description": (
                    "把账号的 window_hwnd 改绑到推荐/指定 hwnd，并重建 UserWindow。"
                    "在 inspect 确认外壳点不动、渲染窗更合适后调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "account": {
                            "type": "string",
                            "description": "账号名；省略则当前选中",
                        },
                        "hwnd": {
                            "type": "integer",
                            "description": "目标 hwnd；省略则用 inspect 推荐值",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mark_on_image",
                "description": (
                    "在图上画圈/画框/编号，产出一张标注图并（默认）切到共同视口给用户看。"
                    "它只负责绘制，不负责识别：先查看原图与参考图，并通过模板匹配或"
                    "本地拟合得到逐项验证的位置后再调用；不要把视觉模型粗框直接拿来画。"
                    "已有验证位置时不要再手写 run_probe_script 探针。坐标按**原图像素**（与 "
                    "describe_current_frame 返回的 bbox 同一套）。"
                    "标注图是给人看的，不是场景素材；生成后必须复核，不要拿它当新的识别底图。"
                ),
                                "x-result": {
                                    "type": "object",
                                    "required": ["count", "path"],
                                    "properties": {
                                        "count": {"type": "integer"},
                                        "path": {"type": "string"},
                                    },
                                },
                "parameters": {
                    "type": "object",
                    "properties": {
                        "marks": {
                            "type": "array",
                            "description": (
                                "标记列表。每项：{rect:[x0,y0,x1,y1]} 或 {x,y,radius}"
                                "，都可带 label/color"
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "rect": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "minItems": 4,
                                        "maxItems": 4,
                                        "description": "原图像素矩形 x0,y0,x1,y1",
                                    },
                                    "x": {"type": "number", "description": "圆心 x（原图像素）"},
                                    "y": {"type": "number", "description": "圆心 y（原图像素）"},
                                    "radius": {"type": "integer", "description": "圆半径，默认 18"},
                                    "label": {"type": "string", "description": "编号旁的文字"},
                                    "color": {
                                        "type": "string",
                                        "description": "red/orange/yellow/green/blue/purple/white/black 或 #RRGGBB",
                                    },
                                },
                            },
                        },
                        "asset_id": {
                            "type": "string",
                            "description": "要标记的素材 id；省略则用共同视口底图",
                            "x-resource": resource_ref_param(
                                roles=("template", "scene", "probe_out"),
                                use="explicit_view",
                                semantic="被标注的底图",
                                base_types=("image",),
                            ),
                        },
                        "name": {"type": "string", "description": "输出的标注图名（可省）"},
                        "title": {"type": "string", "description": "画在图上方的一行说明（可省）"},
                        "show": {
                            "type": "boolean",
                            "description": "是否切到共同视口，默认 true",
                        },
                    },
                    "required": ["marks"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "define_tool",
                "description": (
                    "把一段跑通的临时探针脚本登记成**可复用工具**，之后每轮工具表都会带上它。"
                    "遇到「现有工具做不到」的事：先写 run_probe_script 跑通，再用这个沉淀下来，"
                    "既解决当前问题，也免得后面每次重写。脚本里可用 run_probe_script 的预置"
                    "（frame/FRAME_PATH/match/matcher/cv2/np/Path/save_image/write_text/"
                    "mark_image），参数从 ARGS（或 params）字典取。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "工具名：小写字母/数字/下划线，字母开头，3~40 字符",
                        },
                        "description": {
                            "type": "string",
                            "description": "一句话说清这个工具做什么、返回什么",
                        },
                        "parameters": {
                            "type": "object",
                            "description": "该工具的 JSON Schema（properties/required），参数值会以 ARGS 传进脚本",
                        },
                        "code": {
                            "type": "string",
                            "description": "完整可跑的脚本源码；把结论赋给 result",
                        },
                    },
                    "required": ["name", "description", "code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_custom_tools",
                "description": "列出已登记的自造工具（名字、用途、调用次数）",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_custom_tool",
                "description": "删掉一个不再需要的自造工具",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
        },
    ]


def custom_tool_schemas() -> list[dict]:
    """模型自造工具的 schema；执行时同样要 probe_script 权限。"""
    try:
        from backend.script_generator.collaborator.reusable_tools import (
            openai_custom_tool_schemas,
        )

        return openai_custom_tool_schemas()
    except Exception:
        return []


def resource_tool_schemas() -> list[dict]:
    """统一资源库查询；默认只暴露当前有效资源，避免从旧对话猜素材。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_resources",
                "description": (
                    "查询当前任务资源库中的有效素材、工具和技能。需要资源时先查这里；"
                    "不要从旧对话、旧名称或焦点猜 asset_id。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "resource_type": {
                            "type": "string",
                            "enum": ["", "asset", "tool", "skill", "evidence", "overlay", "claim", "script", "plan"],
                        },
                        "active_only": {"type": "boolean", "default": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_resource",
                "description": "按精确 resource_id 查看资源版本、状态、哈希、来源和替代关系。",
                "parameters": {
                    "type": "object",
                    "properties": {"resource_id": {"type": "string"}},
                    "required": ["resource_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "supersede_resource",
                "description": "确认新版资源可用后，用新版替代旧版；旧版随即退出活动工作集。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old_resource_id": {"type": "string"},
                        "new_resource_id": {"type": "string"},
                    },
                    "required": ["old_resource_id", "new_resource_id"],
                },
            },
        },
    ]


#: 资源引用参数的 schema 标记（写在**参数定义里** = schema 自证，不是外部白名单）。
#: 只有带这个标记的参数才会被 Runtime 当作资源引用去绑定（Phase B 收口）。
RESOURCE_REF_PARAM = {"kind": "resource-ref", "version_required": False}


def resource_ref_param(
    *,
    materialize_as: str = "",
    role: str = "",
    roles: tuple[str, ...] | list[str] = (),
    use: str = "",
    semantic: str = "",
    base_types: tuple[str, ...] | list[str] = (),
    semantic_tags: tuple[str, ...] | list[str] = (),
) -> dict:
    """资源参数的 ``x-resource`` 声明（Phase B4 / 设计 §4.5–§4.6）。

    ``materialize_as`` 是**真实实参名**：Runtime 把绑定 handle 的材料化结果写进它，
    旧工具因此不必自己再解析 id/token/路径。``role``/``roles``/``use``/``base_types``
    是**绑定过滤**（用途不匹配时 Runtime 在工具启动前报 ``RESOURCE_USE_MISMATCH``），
    ``semantic``/``semantic_tags`` 是业务语义（资源和参数都声明了才强校验）。
    """
    blob = dict(RESOURCE_REF_PARAM)
    if materialize_as:
        blob["materialize_as"] = str(materialize_as)
    if role:
        blob["role"] = str(role)
    if roles:
        blob["roles"] = [str(x) for x in roles if str(x)]
    if use:
        blob["use"] = str(use)
    if semantic:
        blob["semantic"] = str(semantic)
    if base_types:
        blob["base_types"] = [str(x) for x in base_types if str(x)]
    if semantic_tags:
        blob["semantic_tags"] = [str(x) for x in semantic_tags if str(x)]
    return blob

#: 资源参数的说明后缀（发给模型看的那一句）。
RESOURCE_PARAM_NOTE = (
    "写**资源 id**（如 asset_xxx）；不要写文件名/路径或绝对路径 —— "
    "Runtime 会在调用前绑定并校验版本/状态/类型/权限/内容 hash，"
    "绑不上会带诊断码退回（RESOURCE_NOT_FOUND / RESOURCE_VERSION_EXPIRED / …）"
)


def annotate_resource_params(tool: dict) -> dict:
    """给**已声明** ``x-resource`` 的资源参数补上说明（返回新对象，不改调用方那份）。

    Phase B 收口：``x-resource`` 是权威声明，这里**不做参数名推断**。
    旧工具（本规则前写的 schema）由 Runtime 的兼容层处理并记
    ``RESOURCE_SCHEMA_LEGACY_INFERRED`` 遥测，不在 schema 层伪装成声明。
    """
    from backend.script_generator.resource_binding import (
        RESOURCE_SCHEMA_KEY,
        declared_resource_params,
    )

    fn = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(fn, dict):
        return tool
    parameters = fn.get("parameters")
    if not isinstance(parameters, dict):
        return tool
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return tool
    names = declared_resource_params(parameters)
    if not names:
        return tool
    new_properties = {
        key: dict(value) if isinstance(value, dict) else value for key, value in properties.items()
    }
    for name in names:
        spec = new_properties.get(name)
        if not isinstance(spec, dict):
            continue
        spec.setdefault(RESOURCE_SCHEMA_KEY, dict(RESOURCE_REF_PARAM))
        existing = str(spec.get("description") or "").strip()
        if RESOURCE_PARAM_NOTE not in existing:
            spec["description"] = f"{existing}；{RESOURCE_PARAM_NOTE}" if existing else RESOURCE_PARAM_NOTE
    new_parameters = dict(parameters)
    new_parameters["properties"] = new_properties
    new_fn = dict(fn)
    new_fn["parameters"] = new_parameters
    new_tool = dict(tool)
    new_tool["function"] = new_fn
    return new_tool


def all_collab_tools() -> list[dict]:
    """内置工具 + 模型自造工具。工具通道和契约检查都用这一份。

    Phase B（设计 §4「资源参数 schema」）：``x-resource`` 是资源参数的权威声明；
    这里只给**已声明**的参数补说明，不再按名字推断。
    """
    tools = openai_collab_tools() + resource_tool_schemas() + custom_tool_schemas()
    return [annotate_resource_params(tool) for tool in tools]


def claude_collab_tools() -> list[dict]:
    out = []
    for t in all_collab_tools():
        fn = t["function"]
        out.append(
            {
                "name": fn["name"],
                "description": fn["description"],
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _loads_meta_object(blob: str) -> dict[str, Any]:
    """从可能含尾巴的文本里抠出第一个 JSON object。"""
    s = (blob or "").strip()
    if not s:
        return {}
    # 先直接 loads；失败再按括号平衡截断
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    start = s.find("{")
    if start < 0:
        return {}
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start : i + 1])
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    return {}
    return {}


def parse_collab_json(text: str) -> tuple[str, dict[str, Any]]:
    """解析填表（或旧 COLLAB_JSON）→ (reply, runtime meta)。"""
    reply, form = parse_collab_form(text)
    meta = form_to_runtime_meta(form)
    # 供上层判断「这一轮到底有没有按格式填表」：缺表 + 空正文要退一次格式重修，
    # 不能把表外推理独白当答复（见 looks_like_reasoning）。
    meta["_form_present"] = collab_form_present(text)
    return reply, meta


_STREAM_MARK = "<<<COLLAB_FORM>>>"


def stream_visible_preview(buf: str) -> str:
    """流式展示：隐藏表尾巴和 DeepSeek 写进正文的工具标记，优先露出 reply。"""
    from backend.script_generator.agent import strip_dsml_markup

    return stream_form_preview(strip_dsml_markup(buf or ""))

def session_messages_for_llm(session: CollabSession, *, limit: int = 16) -> list[dict]:
    msgs = list(session.messages or [])[-limit:]
    out: list[dict] = []
    for m in msgs:
        role = str(m.get("role") or "")
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        if role == "user":
            out.append({"role": "user", "content": text})
        elif role == "agent":
            out.append({"role": "assistant", "content": text})
        # system 通知不进模型上下文，避免污染答话
    return out


_TOOL_HUMAN = {
    "list_skills": "查阅内部能力",
    "get_skill_doc": "读取技能说明书",
    "select_skill": "挂上对应玩法",
    "get_session_status": "查看会话状态",
    "analyze_current_frame": "分析当前画面",
    "describe_current_frame": "查看共同视口",
    "inspect_asset": "检查素材",
    "compare_assets": "联合比较素材",
    "fit_repeated_regions": "拟合重复区域",
    "commit_repeated_regions": "提交重复裁剪",
    "match_template": "模板匹配",
    "match_template_multi": "多目标模板匹配",
    "crop_asset": "裁剪素材",
    "list_session_assets": "查看素材区",
    "show_asset_in_viewport": "切换共同视口",
    "run_probe_script": "临时探针脚本",
    "get_permissions": "查看权限",
    "get_runtime_context": "查看窗口与日志",
    "inspect_click_target": "检查点击目标窗",
    "message_click_probe": "消息点击探针",
    "retarget_click_window": "改绑点击窗口",
    "mark_on_image": "在图上标记",
    "define_tool": "登记自造工具",
    "list_custom_tools": "查看自造工具",
    "delete_custom_tool": "删除自造工具",
    "search_resources": "查询资源库",
    "get_resource": "查看资源详情",
    "supersede_resource": "替换旧资源",
}


def build_capability_block(permissions: Optional[dict] = None) -> str:
    """给模型看的「现在能做什么」清单，替代干巴巴的权限布尔值。"""
    from backend.script_generator.collaborator.probe_script import (
        default_permissions,
    )

    perms = {**default_permissions(), **dict(permissions or {})}
    names = [t["function"]["name"] for t in all_collab_tools()]
    able = [_TOOL_HUMAN.get(n, n) for n in names if n != "get_permissions"]
    lines = [
        "【你现在能做什么】",
        "- 现在就能用：" + "、".join(able),
    ]
    try:
        from backend.script_generator.collaborator.reusable_tools import (
            custom_tools_brief,
        )

        brief = custom_tools_brief()
    except Exception:
        brief = ""
    if brief:
        lines.append("- 你自己登记过的工具：" + brief)
    lines.append(
        "- 想做的事**上面没有现成工具**时：别停下反问，用 run_probe_script 写一段脚本"
        "先把它做出来；跑通后用 define_tool 登记成可复用工具，下一轮就不用再写。"
    )
    missing: list[str] = []
    if not perms.get("write_files"):
        missing.append("存文件（write_files，探针产物/临时文件）")
    if not perms.get("runtime_control"):
        missing.append("控制窗口（runtime_control，真点一下验证）")
    if missing:
        lines.append(
            "- 需要授权：" + "；".join(missing)
            + " → 要用就必须 turn=ask，并给「打开写文件权限 / 打开控制窗口权限」芯片"
        )
    else:
        lines.append("- 授权齐了：存文件、控制窗口都可以直接用。")
    lines.append(
        "- 硬规则：不要承诺工具表里没有的动作；做不到就明说或请用户操作，"
        "不要用别的动作假装推进。"
    )
    return "\n".join(lines)


def _user_wants_click_diag(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    keys = (
        "点不动", "点了没", "没反应", "没有反应", "空点", "点空",
        "没有点击", "没点击", "不点击", "没有点到", "还是没有点",
        "识图成功", "匹配成功但", "点击无效", "点不上", "点不了",
        "窗口不对", "绑错窗", "渲染层", "操作层", "hwnd",
        "改绑", "检查窗口", "为什么点",
    )
    return any(k in t for k in keys)


def _run_runtime_tool(name: str, args: dict) -> dict[str, Any]:
    from backend.script_generator.collaborator.runtime_bridge import (
        build_runtime_context,
        get_runtime_provider,
        resolve_account_hwnd,
    )
    from backend.script_generator.runtime_diag import (
        inspect_click_target,
        message_click_probe,
    )

    if name == "get_runtime_context":
        acc = str(args.get("account") or "").strip() or None
        ctx = build_runtime_context(account=acc)
        return {
            "ok": True,
            "available": ctx.available,
            "selected": ctx.selected,
            "accounts": ctx.accounts,
            "logs": ctx.logs,
            "prompt_block": ctx.to_prompt_block(),
            "human_summary": (
                f"账号 {len(ctx.accounts)} 个 · 日志 {len(ctx.logs)} 条"
                if ctx.available
                else "未接入运行时桥"
            ),
        }

    if name == "inspect_click_target":
        hwnd_arg = args.get("hwnd")
        hwnd_i = int(hwnd_arg) if hwnd_arg not in (None, "") else None
        resolved = resolve_account_hwnd(
            str(args.get("account") or "").strip() or None,
            hwnd_i,
        )
        if not resolved.get("ok"):
            return resolved
        data = inspect_click_target(int(resolved["hwnd"]))
        data["account"] = resolved.get("account") or ""
        return data

    if name == "message_click_probe":
        hwnd_arg = args.get("hwnd")
        hwnd_i = int(hwnd_arg) if hwnd_arg not in (None, "") else None
        resolved = resolve_account_hwnd(
            str(args.get("account") or "").strip() or None,
            hwnd_i,
        )
        if not resolved.get("ok"):
            return resolved
        xy = None
        if args.get("x") is not None and args.get("y") is not None:
            xy = (int(args["x"]), int(args["y"]))
        data = message_click_probe(int(resolved["hwnd"]), xy=xy)
        data["account"] = resolved.get("account") or ""
        return data

    if name == "retarget_click_window":
        p = get_runtime_provider()
        if p is None:
            return {"ok": False, "error": "未接入运行时桥，无法改绑账号窗口"}
        acc = str(args.get("account") or "").strip() or p.selected_account_name()
        hwnd_arg = args.get("hwnd")
        if hwnd_arg in (None, ""):
            resolved = resolve_account_hwnd(acc, None)
            if not resolved.get("ok"):
                return resolved
            insp = inspect_click_target(int(resolved["hwnd"]))
            if not insp.get("ok"):
                return insp
            hwnd_arg = insp.get("recommend_hwnd") or resolved.get("hwnd")
        return p.apply_window_hwnd(acc, int(hwnd_arg))

    return {"ok": False, "error": f"unknown runtime tool {name}"}


def _short_tool_result(name: str, raw: str) -> str:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return "已返回"
    if not isinstance(data, dict):
        return "已返回"
    if name == "list_skills":
        skills = data.get("skills") or []
        return f"共 {len(skills)} 项" if isinstance(skills, list) else "已返回"
    if name == "get_skill_doc":
        if data.get("ok"):
            return f"已读取「{data.get('name') or 'skill'}」"
        return str(data.get("error") or "失败")
    if name == "select_skill":
        if data.get("ok"):
            return f"已选定「{data.get('title') or data.get('skill_id')}」"
        return str(data.get("error") or "失败")
    if name == "get_session_status":
        sid = data.get("skill_id") or "无"
        return f"phase={data.get('phase')} · skill={sid}"
    if name == "analyze_current_frame":
        if data.get("ok"):
            brief = str(data.get("probe_brief") or data.get("summary") or "完成")
            return brief[:40] + ("…" if len(brief) > 40 else "")
        return str(data.get("error") or "失败")
    if name == "describe_current_frame":
        if data.get("ok"):
            tag = "缓存" if data.get("cached") else "新识图"
            brief = str(data.get("text") or "完成")
            return f"{tag} · " + brief[:36] + ("…" if len(brief) > 36 else "")
        return str(data.get("error") or "失败")
    if name == "inspect_asset":
        if data.get("ok"):
            brief = str(data.get("text") or "完成")
            return "已检查 · " + brief[:36] + ("…" if len(brief) > 36 else "")
        return str(data.get("error") or "失败")
    if name == "compare_assets":
        if data.get("ok"):
            n = len(data.get("asset_ids") or [])
            brief = str(data.get("text") or "完成")
            return f"已联合比较 {n} 张 · " + brief[:28] + ("…" if len(brief) > 28 else "")
        return str(data.get("error") or "失败")
    if name == "fit_repeated_regions":
        if data.get("ok"):
            return f"找到 {data.get('count')} 个 · 置信度 {data.get('confidence')} · 待验证"
        return str(data.get("error") or "失败")
    if name == "commit_repeated_regions":
        if data.get("ok"):
            return f"已提交 {data.get('count', len(data.get('asset_ids') or []))} 张"
        return str(data.get("error") or "失败")
    if name == "match_template":
        if data.get("ok"):
            sc = data.get("score")
            matched = "命中" if data.get("matched") else "未命中"
            return f"{matched} · score={sc}"
        return str(data.get("error") or "失败")
    if name == "match_template_multi":
        if data.get("ok"):
            suffix = " · 候选可能泛滥" if data.get("ambiguous") else ""
            return f"命中 {data.get('count', 0)} 个{suffix}"
        return str(data.get("error") or "失败")
    if name == "list_session_assets":
        n = len(data.get("assets") or [])
        return f"共 {n} 张"
    if name == "search_resources":
        return f"找到 {data.get('count', 0)} 项有效资源"
    if name == "get_resource":
        row = data.get("resource") or {}
        return f"{row.get('resource_id') or '资源'} · {row.get('status') or '未知状态'}"
    if name == "supersede_resource":
        return "已替换旧资源" if data.get("ok") else str(data.get("error") or "失败")
    if name == "show_asset_in_viewport":
        if data.get("ok"):
            return f"共同视口已切到「{data.get('name') or data.get('asset_id')}」"
        return str(data.get("error") or "失败")
    if name == "run_probe_script":
        if data.get("ok"):
            return "完成" + (f" · {str(data.get('result'))[:28]}" if data.get("result") is not None else "")
        if data.get("need_permission"):
            return f"需权限 {data.get('need_permission')}"
        return str(data.get("error") or "失败")[:48]
    if name == "mark_on_image":
        if data.get("ok"):
            return f"已画 {data.get('count', 0)} 处标记"
        return str(data.get("error") or "失败")[:48]
    if name == "define_tool":
        if data.get("ok"):
            return f"已登记工具 {data.get('name')}"
        return str(data.get("error") or "失败")[:48]
    if name == "get_permissions":
        return str(data.get("permissions") or data)[:48]
    if name in (
        "get_runtime_context",
        "inspect_click_target",
        "message_click_probe",
        "retarget_click_window",
    ):
        if data.get("ok") is False:
            return str(data.get("error") or "失败")
        hs = str(data.get("human_summary") or "").strip()
        return (hs[:48] + ("…" if len(hs) > 48 else "")) if hs else "完成"
    return "已返回"


def _run_custom_tool(
    name: str, args: dict, *, session, perms: dict
) -> Optional[str]:
    """执行模型自造工具。找不到定义返回 None（调用方按 unknown tool 处理）。

    自造工具本质是"预置好的探针脚本"，所以仍受 probe_script 权限约束：
    用户关掉临时脚本权限，这些工具也就不能跑。
    """
    try:
        from backend.script_generator.collaborator.reusable_tools import (
            build_callable_code,
            bump_uses,
            find_tool_def,
        )
    except Exception:
        return None
    tool_def = find_tool_def(name)
    if not tool_def:
        return None
    if not perms.get("probe_script"):
        if not isinstance(getattr(session, "artifacts", None), dict):
            session.artifacts = {}
        session.artifacts["pending_need_permission"] = "probe_script"
        return json.dumps(
            {
                "ok": False,
                "need_permission": "probe_script",
                "error": f"自造工具「{name}」需要「临时脚本」权限；界面将弹出授权。",
            },
            ensure_ascii=False,
        )
    from backend.script_generator.collaborator.probe_script import run_probe_script

    code = build_callable_code(tool_def, args if isinstance(args, dict) else {})
    data = run_probe_script(
        code,
        session=session,
        permissions=perms,
        args=args,
        script_id=f"custom_tool:{str(tool_def.get('name') or name)}",
    )
    if isinstance(data, dict):
        data["custom_tool"] = str(tool_def.get("name") or name)
    try:
        bump_uses(str(tool_def.get("name") or name))
    except Exception:
        pass
    return json.dumps(data, ensure_ascii=False)


def _tool_json_default(value: Any) -> Any:
    """Last-mile adapter for structured objects returned by legacy tools."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            pass
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def make_tool_resolver(
    registry: SkillRegistry,
    session: CollabSession,
    *,
    primary_vision: Optional[dict] = None,
    vision_assist: Optional[dict] = None,
    permissions: Optional[dict] = None,
    on_select: Optional[Callable[[str], None]] = None,
    on_analyze: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    trace: Optional[list[str]] = None,
    token_acc: Optional[list[int]] = None,
    evidence_store: Any = None,
) -> Callable:
    from backend.script_generator.collaborator.probe_script import (
        match_template_on_frame,
        match_template_multi_on_frame,
        normalize_permissions,
        run_probe_script,
    )
    from backend.script_generator.collaborator.canvas import (
        resolve_template_path,
        replace_multi_match_overlays,
        show_asset_in_viewport,
        template_size,
        upsert_match_overlay,
    )

    # Phase B2：本轮的视觉证据预算从这里开始算（缓存跨主轮复用）。
    try:
        new_visual_broker(session, evidence_store=evidence_store)
    except Exception:  # noqa: BLE001 - 建 broker 失败不该拦住工具表
        pass

    perms = normalize_permissions(permissions)

    def _emit(msg: str) -> None:
        if trace is not None:
            trace.append(msg)
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    def resolve(name: str, arguments) -> str:
        args = arguments
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        label = _TOOL_HUMAN.get(name, name)
        detail = ""
        if name == "select_skill":
            detail = f"（{args.get('skill_id') or '?'}）"
        _emit(f"🔧 调用工具 · {label}{detail}")

        def _pack(data: Any) -> str:
            """实测类工具回结果前先过哨兵：标出可疑点，并把数字记进会话。

            模型看数字的能力没问题，缺的是"这个数字不对劲"的提示——
            所以这里只加字段、不改原值。
            """
            try:
                from backend.script_generator.collaborator.tool_sanity import (
                    sanitize_tool_result,
                )

                data = sanitize_tool_result(name, data, session=session)
            except Exception:
                pass
            vision_evidence_ok = name not in {"describe_current_frame", "inspect_asset", "compare_assets"} or bool(
                isinstance(data, dict) and data.get("structured")
            )
            if isinstance(data, dict) and data.get("ok") and vision_evidence_ok and name in {
                "describe_current_frame",
                "inspect_asset",
                "compare_assets",
                "crop_asset",
                "match_template",
                "match_template_multi",
                "run_probe_script",
                "analyze_current_frame",
            }:
                if not isinstance(session.artifacts, dict):
                    session.artifacts = {}
                evidence = session.artifacts.setdefault("evidence", [])
                if isinstance(evidence, list):
                    item = {
                        "tool": name,
                        "template_asset_id": str(
                            data.get("template_asset_id") or args.get("asset_id") or ""
                        ),
                        "template_name": str(data.get("template_name") or ""),
                        "asset_ids": list(args.get("asset_ids") or [])[:9],
                        "target_asset_id": str(args.get("target_asset_id") or ""),
                        "question": str(args.get("question") or "")[:160],
                        "rect": list(args.get("rect") or [])[:4],
                        "matched": data.get("matched"),
                        "score": data.get("score"),
                        "count": data.get("count"),
                        "matches": list(data.get("matches") or [])[:20],
                        "result": repr(data.get("result"))[:240],
                        "path": str(data.get("path") or ""),
                        "text": str(data.get("text") or "")[-240:],
                    }
                    # 重复读取缓存或重复跑同一个探针不算新证据。
                    if not evidence or evidence[-1] != item:
                        evidence.append(item)
                        try:
                            from backend.script_generator.collaborator.resource_registry import (
                                register_record_resource,
                            )

                            sources = [
                                str(x) for x in (
                                    [item.get("template_asset_id"), item.get("target_asset_id")]
                                    + list(item.get("asset_ids") or [])
                                ) if x
                            ]
                            register_record_resource(
                                session,
                                resource_type="evidence",
                                kind=name,
                                name=f"{name} 结果",
                                payload=item,
                                derived_from=sources,
                            )
                        except Exception:
                            pass
                    del evidence[:-40]
            # VisualEvidence and other typed legacy results may be nested in
            # the tool payload.  Normalize them here, before V4 (or any other
            # caller) receives the resolver's JSON string.
            return json.dumps(data, ensure_ascii=False, default=_tool_json_default)

        if name == "list_skills":
            # registry 里是"会出脚本的玩法插件"；SKILL.md 是说明书，两处都列，
            # 免得模型看到空列表就以为本地没有任何技能。
            lines = registry.catalog_lines()
            try:
                from backend.script_generator.collaborator_v3.adapters.skill_capabilities import (
                    resource_catalog_lines,
                )

                doc_lines = resource_catalog_lines()
            except Exception:
                doc_lines = []
            if doc_lines:
                lines = lines + ["（说明书类技能，读文档照做）"] + doc_lines
            raw = json.dumps({"skills": lines}, ensure_ascii=False)
        elif name == "get_skill_doc":
            requested = str(args.get("name") or "").strip()
            try:
                from backend.script_generator.collaborator_v3.adapters.skill_capabilities import (
                    load_skill_docs,
                )

                doc = next(
                    (
                        item
                        for item in load_skill_docs()
                        if requested in {item.name, f"skill_{item.name}"}
                    ),
                    None,
                )
            except Exception:
                doc = None
            raw = json.dumps(
                (
                    {
                        "ok": True,
                        "kind": "skill",
                        "name": doc.name,
                        "version": doc.content_hash,
                        "description": doc.description,
                        "body": doc.body,
                    }
                    if doc is not None
                    else {"ok": False, "error": f"未知说明书技能: {requested}"}
                ),
                ensure_ascii=False,
            )
        elif name == "get_session_status":
            raw = json.dumps(
                {
                    "phase": getattr(session.phase, "value", str(session.phase)),
                    "skill_id": session.skill_id,
                    "goal_text": session.goal_text,
                    "has_frame": _path_is_file(session.frame_path),
                    "has_vision_cache": bool(_cached_assist_vision(session)),
                    "n_assets": len(getattr(session, "assets", None) or []),
                    "n_overlays": len(getattr(session, "overlays", None) or []),
                    "n_steps": len(session.logic.steps),
                    "script_name": session.script_name,
                    "permissions": perms,
                    "probe_brief": _slim_probe_for_prompt(
                        session.probe if isinstance(session.probe, dict) else None
                    ),
                },
                ensure_ascii=False,
            )
        elif name == "get_permissions":
            raw = json.dumps({"ok": True, "permissions": perms}, ensure_ascii=False)
        elif name == "select_skill":
            sid = str(args.get("skill_id") or "").strip()
            sk = registry.get(sid)
            if sk is None:
                raw = json.dumps({"ok": False, "error": f"未知技能: {sid}"}, ensure_ascii=False)
            else:
                session.skill_id = sid
                if on_select:
                    on_select(sid)
                has_frame = _path_is_file(session.frame_path)
                raw = json.dumps(
                    {
                        "ok": True,
                        "skill_id": sid,
                        "title": sk.title,
                        "has_frame": has_frame,
                        "hint": (
                            "已有画面，可继续调用 analyze_current_frame / match_template / run_probe_script"
                            if has_frame
                            else "请引导用户提供游戏画面（右侧用当前窗口/选截图）"
                        ),
                    },
                    ensure_ascii=False,
                )
        elif name == "analyze_current_frame":
            sid = str(args.get("skill_id") or "").strip() or None
            data = run_frame_analyze(registry, session, skill_id=sid)
            if data.get("ok") and on_analyze:
                try:
                    on_analyze(data)
                except Exception:
                    pass
            if data.get("ok") and on_select and data.get("skill_id"):
                try:
                    on_select(str(data["skill_id"]))
                except Exception:
                    pass
            raw = _pack(data)
        elif name == "describe_current_frame":
            force = bool(args.get("force"))
            q = str(args.get("question") or "").strip()
            mode = str(args.get("mode") or "locate").strip()
            target = str(args.get("target") or "").strip()
            distinguish = list(args.get("distinguish_from") or [])
            try:
                data = _await_coro(
                    _describe_frame_with_fallback(
                        session,
                        primary_vision,
                        vision_assist,
                        user_question=q,
                        mode=mode,
                        target=target,
                        distinguish_from=distinguish,
                        force=force,
                        on_status=_emit,
                    )
                )
            except Exception as e:
                data = {"ok": False, "error": str(e)}
            if (
                isinstance(data, dict)
                and data.get("ok")
                and token_acc is not None
                and len(token_acc) >= 2
            ):
                token_acc[0] += int(data.get("tokens_in") or 0)
                token_acc[1] += int(data.get("tokens_out") or 0)
            raw = _pack(data)
        elif name == "inspect_asset":
            aid = str(args.get("asset_id") or "").strip()
            q = str(args.get("question") or "").strip()
            mode = str(args.get("mode") or "verify").strip()
            target = str(args.get("target") or "").strip()
            distinguish = list(args.get("distinguish_from") or [])
            try:
                data = _await_coro(
                    _inspect_asset_with_fallback(
                        session,
                        primary_vision,
                        vision_assist,
                        asset_id=aid,
                        question=q,
                        mode=mode,
                        target=target,
                        distinguish_from=distinguish,
                        on_status=_emit,
                    )
                )
            except Exception as e:
                data = {"ok": False, "error": str(e)}
            if data.get("ok") and aid:
                try:
                    from backend.script_generator.collaborator.resource_registry import (
                        set_resource_status,
                    )

                    if data.get("verified") is True:
                        set_resource_status(session, aid, "verified", reason="inspect_asset 复核通过")
                    elif data.get("verified") is False:
                        set_resource_status(session, aid, "invalid", reason="inspect_asset 复核未通过")
                except Exception:
                    pass
            if data.get("ok") and token_acc is not None and len(token_acc) >= 2:
                token_acc[0] += int(data.get("tokens_in") or 0)
                token_acc[1] += int(data.get("tokens_out") or 0)
            raw = _pack(data)
        elif name == "compare_assets":
            aids = [str(x or "").strip() for x in list(args.get("asset_ids") or [])]
            q = str(args.get("question") or "").strip()
            try:
                data = _await_coro(
                    _compare_assets_with_fallback(
                        session,
                        primary_vision,
                        vision_assist,
                        asset_ids=aids,
                        question=q,
                        target=str(args.get("target") or "").strip(),
                        distinguish_from=list(args.get("distinguish_from") or []),
                        on_status=_emit,
                    )
                )
            except Exception as e:
                data = {"ok": False, "error": str(e)}
            if data.get("ok") and token_acc is not None and len(token_acc) >= 2:
                token_acc[0] += int(data.get("tokens_in") or 0)
                token_acc[1] += int(data.get("tokens_out") or 0)
            raw = _pack(data)
        elif name == "fit_repeated_regions":
            from backend.script_generator.collaborator.repeated_crop import fit_repeated_regions

            data = fit_repeated_regions(
                session,
                grounding_ids=list(args.get("grounding_ids") or []),
                expected_count=int(args.get("expected_count") or 0),
                name_prefix=str(args.get("name_prefix") or "重复区域"),
            )
            raw = _pack(data)
        elif name == "commit_repeated_regions":
            from backend.script_generator.collaborator.repeated_crop import commit_repeated_regions

            data = commit_repeated_regions(session, fit_id=str(args.get("fit_id") or ""))
            raw = _pack(data)
        elif name == "search_resources":
            from backend.script_generator.collaborator.resource_registry import (
                resource_brief,
                search_resources,
            )

            rows = search_resources(
                session,
                query=str(args.get("query") or ""),
                resource_type=str(args.get("resource_type") or ""),
                active_only=bool(args.get("active_only", True)),
                limit=int(args.get("limit") or 40),
            )
            raw = _pack({"ok": True, "resources": [resource_brief(x) for x in rows], "count": len(rows)})
        elif name == "get_resource":
            from backend.script_generator.collaborator.resource_registry import (
                get_resource,
                resource_brief,
            )

            row = get_resource(session, str(args.get("resource_id") or ""))
            raw = _pack(
                {"ok": True, "resource": resource_brief(row)}
                if row
                else {"ok": False, "error": "资源不存在"}
            )
        elif name == "supersede_resource":
            from backend.script_generator.collaborator.resource_registry import (
                supersede_resource,
            )

            ok, err = supersede_resource(
                session,
                str(args.get("old_resource_id") or ""),
                str(args.get("new_resource_id") or ""),
            )
            raw = _pack({"ok": ok, "error": err})
        elif name == "list_session_assets":
            assets = list(getattr(session, "assets", None) or [])
            try:
                from backend.script_generator.collaborator.resource_registry import active_asset_ids

                active_ids = active_asset_ids(session)
                assets = [a for a in assets if str(a.get("id") or "") in active_ids]
            except Exception:
                pass
            inspected_ids = set()
            if isinstance(session.artifacts, dict):
                for item in list(session.artifacts.get("asset_inspection_log") or []):
                    if not isinstance(item, dict):
                        continue
                    inspected_ids.update(str(x) for x in (item.get("asset_ids") or []) if x)
                    if item.get("asset_id"):
                        inspected_ids.add(str(item["asset_id"]))
            query = str(args.get("query") or "").strip().lower()
            group_filter = str(args.get("group") or "").strip().lower()
            role_filter = str(args.get("role") or "").strip().lower()
            filtered = []
            for asset in assets:
                aid = str(asset.get("id") or "")
                haystack = " ".join(
                    str(asset.get(k) or "") for k in ("id", "name", "caption", "group", "role")
                ).lower()
                if query and query not in haystack:
                    continue
                if group_filter and str(asset.get("group") or "").strip().lower() != group_filter:
                    continue
                if role_filter and str(asset.get("role") or "").strip().lower() != role_filter:
                    continue
                if bool(args.get("only_uninspected")) and aid in inspected_ids:
                    continue
                filtered.append(asset)
            try:
                limit = max(1, min(40, int(args.get("limit") or 40)))
            except Exception:
                limit = 40
            selected = filtered[:limit]
            if isinstance(session.artifacts, dict):
                session.artifacts["last_asset_catalog"] = {
                    "asset_ids": [str(a.get("id") or "") for a in selected],
                    "count": len(selected),
                    "total": len(assets),
                    "filters": {"query": query, "group": group_filter, "role": role_filter},
                    "at": time.time(),
                }
            raw = json.dumps(
                {
                    "ok": True,
                    "assets": [
                        {
                            "id": a.get("id"),
                            "name": a.get("name"),
                            "role": a.get("role"),
                            "path": a.get("path"),
                            "caption": (a.get("caption") or "")[:80],
                            "group": a.get("group"),
                            "size": list(template_size(Path(str(a.get("path") or "")))),
                            "inspected": str(a.get("id") or "") in inspected_ids,
                        }
                        for a in selected
                    ],
                    "n": len(selected),
                    "matched": len(filtered),
                    "total": len(assets),
                    "truncated": len(filtered) > len(selected),
                    "note": (
                        "用 id 或 name 点名要操作哪张，不要靠「焦点」省略参数。"
                        "不认识的图先 inspect_asset 看内容。"
                    ),
                },
                ensure_ascii=False,
            )
        elif name == "show_asset_in_viewport":
            data = show_asset_in_viewport(
                session, str(args.get("asset_id") or "").strip()
            )
            raw = _pack(data)
        elif name == "match_template":
            target_asset_id = str(args.get("target_asset_id") or "").strip()
            target_path_arg = str(args.get("target_path") or "").strip()
            if target_asset_id or target_path_arg:
                target_path, target_asset, target_err = resolve_template_path(
                    session,
                    asset_id=target_asset_id,
                    template_path=target_path_arg,
                )
            else:
                target_path = Path(session.frame_path) if _path_is_file(session.frame_path) else None
                target_asset = None
                target_err = "" if target_path is not None else "当前没有可用画面（共同视口底图为空）"
            if target_err or target_path is None:
                data = {"ok": False, "error": target_err or "目标素材无效"}
            else:
                thr = args.get("threshold")
                try:
                    thr_f = float(thr) if thr is not None else 0.9
                except Exception:
                    thr_f = 0.9
                tmpl_path, asset, err = resolve_template_path(
                    session,
                    asset_id=str(args.get("asset_id") or ""),
                    template_path=str(args.get("template_path") or ""),
                )
                if err or tmpl_path is None:
                    data = {"ok": False, "error": err or "模板无效"}
                else:
                    template_id = str((asset or {}).get("id") or "")
                    provenance = session.artifacts.get("crop_provenance") if isinstance(session.artifacts, dict) else {}
                    is_crop = bool(
                        str((asset or {}).get("group") or "") == "裁剪"
                        or (isinstance(provenance, dict) and template_id in provenance)
                    )
                    verified = session.artifacts.get("verified_asset_ids") if isinstance(session.artifacts, dict) else []
                    if is_crop and template_id not in (verified or []):
                        data = {
                            "ok": False,
                            "error": "裁剪素材尚未通过 inspect_asset(mode=verify)，不能用于匹配",
                        }
                    else:
                        try:
                            same_file = Path(tmpl_path).resolve() == Path(target_path).resolve()
                        except Exception:
                            same_file = str(tmpl_path) == str(target_path)
                    if is_crop and template_id not in (verified or []):
                        pass
                    elif same_file:
                        data = {
                            "ok": False,
                            "error": "模板和目标是同一个文件；自匹配没有验证价值",
                        }
                    else:
                        data = match_template_on_frame(
                            frame_path=Path(target_path),
                            template_path=tmpl_path,
                            threshold=thr_f,
                        )
                        data["target_asset_id"] = (target_asset or {}).get("id") or ""
                        data["target_path"] = str(target_path)
                    # 把实际用的模板写回结果，事后能说清匹配的是哪张图。
                    data["template_asset_id"] = template_id
                    data["template_path"] = str(tmpl_path)
                    data["template_name"] = str(
                        (asset or {}).get("name") or Path(tmpl_path).name
                    )
                    current_target = False
                    try:
                        current_target = bool(
                            session.frame_path
                            and Path(target_path).resolve() == Path(session.frame_path).resolve()
                        )
                    except Exception:
                        pass
                    if data.get("ok") and data.get("matched") and current_target:
                        tw, th = template_size(tmpl_path)
                        ov = upsert_match_overlay(
                            session,
                            asset=asset,
                            match=data,
                            template_wh=(tw, th),
                        )
                        data["overlay_id"] = ov.get("id")
                        data["asset_id"] = (asset or {}).get("id") or ""
                        if not isinstance(session.artifacts, dict):
                            session.artifacts = {}
                        session.artifacts["canvas_dirty"] = True
                        # 匹配成功直接推翻上一轮「素材文件不存在」之类的旧笔记
                        session.artifacts.pop("agent_plan_gap", None)
            raw = _pack(data)
        elif name == "match_template_multi":
            target_asset_id = str(args.get("target_asset_id") or "").strip()
            target_path_arg = str(args.get("target_path") or "").strip()
            if target_asset_id or target_path_arg:
                target_path, target_asset, target_err = resolve_template_path(
                    session, asset_id=target_asset_id, template_path=target_path_arg
                )
            else:
                target_path = Path(session.frame_path) if _path_is_file(session.frame_path) else None
                target_asset = None
                target_err = "" if target_path is not None else "当前没有可用画面（共同视口底图为空）"
            tmpl_path, asset, tmpl_err = resolve_template_path(
                session,
                asset_id=str(args.get("asset_id") or ""),
                template_path=str(args.get("template_path") or ""),
            )
            if target_err or target_path is None:
                data = {"ok": False, "error": target_err or "目标素材无效"}
            elif tmpl_err or tmpl_path is None:
                data = {"ok": False, "error": tmpl_err or "模板无效"}
            else:
                try:
                    same_file = Path(tmpl_path).resolve() == Path(target_path).resolve()
                except Exception:
                    same_file = str(tmpl_path) == str(target_path)
                template_id = str((asset or {}).get("id") or "")
                provenance = session.artifacts.get("crop_provenance") if isinstance(session.artifacts, dict) else {}
                verified = session.artifacts.get("verified_asset_ids") if isinstance(session.artifacts, dict) else []
                is_crop = bool(
                    str((asset or {}).get("group") or "") == "裁剪"
                    or (isinstance(provenance, dict) and template_id in provenance)
                )
                if is_crop and template_id not in (verified or []):
                    data = {"ok": False, "error": "裁剪素材尚未通过 inspect_asset(mode=verify)，不能用于匹配"}
                elif same_file:
                    data = {"ok": False, "error": "模板和目标是同一个文件；自匹配没有验证价值"}
                else:
                    tw, th = template_size(tmpl_path)
                    target_w, target_h = template_size(Path(target_path))
                    region = list(args.get("search_region") or [])
                    crop_tl = crop_br = None
                    if len(region) == 4:
                        try:
                            vals = [float(x) for x in region]
                            if all(0.0 <= x <= 1.0 for x in vals):
                                vals = [
                                    vals[0] * target_w, vals[1] * target_h,
                                    vals[2] * target_w, vals[3] * target_h,
                                ]
                            x0, y0, x1, y1 = [int(round(x)) for x in vals]
                            crop_tl, crop_br = (x0, y0), (x1, y1)
                        except Exception:
                            crop_tl = crop_br = None
                    try:
                        threshold = float(args.get("threshold") if args.get("threshold") is not None else 0.9)
                    except Exception:
                        threshold = 0.9
                    try:
                        max_count = int(args.get("max_count") or 50)
                    except Exception:
                        max_count = 50
                    try:
                        min_distance = int(args.get("min_distance") or max(8, min(tw, th) // 2))
                    except Exception:
                        min_distance = max(8, min(tw, th) // 2)
                    try:
                        scale_hint = float(args["scale_hint"]) if args.get("scale_hint") is not None else None
                    except Exception:
                        scale_hint = None
                    try:
                        scales = [float(x) for x in list(args.get("scales") or []) if float(x) > 0] or None
                    except Exception:
                        scales = None
                    expected = max(0, int(args.get("expected_count") or 0))
                    data = match_template_multi_on_frame(
                        frame_path=Path(target_path),
                        template_path=tmpl_path,
                        threshold=threshold,
                        max_count=max_count,
                        min_distance=min_distance,
                        crop_top_left=crop_tl,
                        crop_bottom_right=crop_br,
                        scale_hint=scale_hint,
                        scales=scales,
                        use_color_check=bool(args.get("use_color_check")),
                        color_tol=float(args.get("color_tol") or 30.0),
                        expected_count=expected,
                    )
                    data["asset_id"] = template_id
                    data["target_asset_id"] = str((target_asset or {}).get("id") or "")
                    data["target_path"] = str(target_path)
                    data["expected_count"] = expected
                    data["count_matches_expected"] = (
                        data.get("count") == expected if expected else None
                    )
                    if expected and data.get("count") != expected:
                        data["count_warning"] = (
                            f"期望 {expected} 个，实际命中 {data.get('count', 0)} 个；"
                            "请检查阈值、去重距离、搜索区域和模板信息量。"
                        )
                    current_target = False
                    try:
                        current_target = bool(
                            session.frame_path
                            and Path(target_path).resolve() == Path(session.frame_path).resolve()
                        )
                    except Exception:
                        pass
                    if data.get("ok") and current_target:
                        overlays = replace_multi_match_overlays(
                            session,
                            asset=asset,
                            matches=list(data.get("matches") or []),
                            threshold=threshold,
                        )
                        data["overlay_ids"] = [o.get("id") for o in overlays]
                        if not isinstance(session.artifacts, dict):
                            session.artifacts = {}
                        session.artifacts["canvas_dirty"] = True
                    if not isinstance(session.artifacts, dict):
                        session.artifacts = {}
                    session.artifacts["last_multi_match"] = {
                        k: data.get(k) for k in (
                            "asset_id", "target_asset_id", "target_path", "count",
                            "expected_count", "matches", "ambiguous", "warning",
                            "count_warning", "diagnostics", "reasons",
                        )
                    }
            raw = _pack(data)
        elif name == "crop_asset":
            from backend.script_generator.collaborator.asset_ops import crop_asset

            grounding_id = str(args.get("vision_candidate_id") or "").strip()
            groundings = session.artifacts.get("vision_groundings") if isinstance(session.artifacts, dict) else []
            grounding = next(
                (
                    item
                    for item in (groundings or [])
                    if isinstance(item, dict) and str(item.get("id") or "") == grounding_id
                ),
                None,
            )
            if not grounding:
                data = {
                    "ok": False,
                    "error": "裁剪必须引用有效 vision_candidate_id；请先让识图模型定位目标",
                }
            elif grounding.get("valid") is False:
                data = {
                    "ok": False,
                    "error": (
                        "这条视觉定位是退化框（换算到原图后面积过小，"
                        f"bbox={grounding.get('bbox')}，原图 {grounding.get('source_image_size')}），"
                        "不能当裁剪依据。请重新定位（必要时换一张更清晰/更完整的图），"
                        "不要直接裁这块。"
                    ),
                    "vision_candidate_id": grounding_id,
                    "bbox": grounding.get("bbox"),
                }
            else:
                requested_asset = str(args.get("asset_id") or "").strip()
                source_asset = str(grounding.get("source_asset_id") or "").strip()
                if requested_asset and source_asset and requested_asset != source_asset:
                    data = {"ok": False, "error": "视觉候选不属于指定源素材"}
                else:
                    data = crop_asset(
                        session,
                        asset_id=requested_asset or source_asset,
                        template_path="" if (requested_asset or source_asset) else str(grounding.get("source_path") or ""),
                        rect=grounding.get("bbox"),
                        name=str(args.get("name") or ""),
                    )
                    if data.get("ok"):
                        data["vision_candidate_id"] = grounding_id
                        data["vision_label"] = grounding.get("label") or ""
                        provenance = session.artifacts.setdefault("crop_provenance", {})
                        if isinstance(provenance, dict) and data.get("asset_id"):
                            provenance[str(data["asset_id"])] = {
                                "vision_candidate_id": grounding_id,
                                "source_asset_id": source_asset,
                                "source_path": grounding.get("source_path") or "",
                                "bbox": grounding.get("bbox") or [],
                            }
            raw = _pack(data)
        elif name == "run_probe_script":
            from backend.script_generator.collaborator.asset_ops import (
                probe_out_dir,
                register_new_probe_images,
                snapshot_dir_files,
            )
            from backend.script_generator.collaborator.tool_sanity import (
                repeat_without_result,
            )

            out_dir = probe_out_dir(session)
            before_files = snapshot_dir_files(out_dir)
            # 空转硬闸：同一张图、同一工具已经连续跑了几次都没产出 result，
            # 原样重跑只是烧预算（实况：同一张标注图在 9 秒内跑了 4 次）。
            spin = repeat_without_result(session, "run_probe_script")
            if spin:
                data = {"ok": False, "error": spin}
            else:
                data = run_probe_script(
                    str(args.get("code") or ""),
                    session=session,
                    permissions=perms,
                    script_id=str(args.get("script_id") or ""),
                )
            if isinstance(data, dict) and data.get("need_permission"):
                if not isinstance(session.artifacts, dict):
                    session.artifacts = {}
                session.artifacts["pending_need_permission"] = data["need_permission"]
            # 探针里 save_image 写出的图自动进素材区（否则用户界面上看不到变化），
            # 但按「产出物」登记：不算场景素材，不会被自动送去识图。
            new_assets = register_new_probe_images(
                session, before=before_files, directory=out_dir
            )
            if new_assets and isinstance(data, dict):
                data["new_assets"] = new_assets
                data["note"] = (
                    "本次产出的图片已加入素材区（asset_id 见 new_assets）。"
                    "它们是产出物、不算场景素材，不会被自动送去识图；"
                    "要拿其中一张当模板，显式把它的 asset_id 传给 match_template。"
                )
            if isinstance(data, dict) and data.get("ok"):
                # 跑通了就该沉淀：同一段脚本反复重写既费 token 又容易跑偏
                data.setdefault(
                    "reuse_hint",
                    "这段脚本跑通了。如果这不是一次性的事，可以用 define_tool "
                    "把它登记成可复用工具（name/description/parameters/code），"
                    "下一轮工具表会带上它。",
                )
            raw = _pack(data)
        elif name == "mark_on_image":
            from backend.script_generator.collaborator.canvas import mark_on_image

            try:
                data = mark_on_image(
                    session,
                    marks=args.get("marks"),
                    asset_id=str(args.get("asset_id") or ""),
                    name=str(args.get("name") or ""),
                    title=str(args.get("title") or ""),
                    show=args.get("show") is not False,
                )
            except Exception as e:
                data = {"ok": False, "error": str(e)}
            raw = _pack(data)
        elif name == "define_tool":
            from backend.script_generator.collaborator.reusable_tools import (
                register_tool,
            )

            data = register_tool(
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                parameters=args.get("parameters") if isinstance(args.get("parameters"), dict) else None,
                code=str(args.get("code") or ""),
            )
            raw = _pack(data)
        elif name == "list_custom_tools":
            from backend.script_generator.collaborator.reusable_tools import (
                load_tool_defs,
            )

            defs = [
                {
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "uses": int(t.get("uses") or 0),
                    "bundled": bool(t.get("bundled")),
                }
                for t in load_tool_defs()
            ]
            raw = _pack({"ok": True, "tools": defs, "n": len(defs)})
        elif name == "delete_custom_tool":
            from backend.script_generator.collaborator.reusable_tools import delete_tool

            raw = _pack(delete_tool(str(args.get("name") or "")))
        elif name in (
            "get_runtime_context",
            "inspect_click_target",
            "message_click_probe",
            "retarget_click_window",
        ):
            if name in ("message_click_probe", "retarget_click_window") and not perms.get(
                "runtime_control"
            ):
                data = {
                    "ok": False,
                    "need_permission": "runtime_control",
                    "error": "需要「控制窗口」权限；界面将弹出授权。",
                }
                if not isinstance(session.artifacts, dict):
                    session.artifacts = {}
                session.artifacts["pending_need_permission"] = "runtime_control"
            else:
                try:
                    data = _run_runtime_tool(name, args)
                except Exception as e:
                    data = {"ok": False, "error": str(e)}
            raw = _pack(data)
        else:
            raw = _run_custom_tool(name, args, session=session, perms=perms)
            if raw is None:
                raw = json.dumps(
                    {"ok": False, "error": f"unknown tool {name}"}, ensure_ascii=False
                )
        _emit(f"✓ {label} · {_short_tool_result(name, raw)}")
        return raw

    return resolve


# 自动续轮预算：轮数 / token / 时长，任一超限就停下汇报，而不是无限空转
_FOLLOWUP_MAX_ROUNDS = 8
_FOLLOWUP_TOKEN_BUDGET = 200_000
_FOLLOWUP_SECONDS = 240.0
# 连续无系统产出只用于诊断，不能据此中断模型：分析、定位、比较等有效步骤
# 本来就可能暂时不写素材。防无限循环由轮数 / token / 时长预算负责。
_NO_PROGRESS_LIMIT = 3
# 同一条缺口文案连续出现几次就停：说明"再问一遍"推不动（模型没东西可改）。
# 实况：done_when 被旧产物顶成"已成立"，系统每轮都喊"改 done_when"，
# 模型每轮只回一句「我这就出图」，8 轮烧完零产出。
_SAME_GAP_LIMIT = 2


def _progress_fingerprint(session) -> tuple:
    """系统侧产物指纹：只看真产出，不看模型说了什么。"""
    art = getattr(session, "artifacts", None)
    art = art if isinstance(art, dict) else {}
    probe = art.get("last_probe_script")
    crop = art.get("last_crop")
    return (
        str(getattr(session, "frame_path", None) or ""),
        len(list(getattr(session, "assets", None) or [])),
        len(list(getattr(session, "overlays", None) or [])),
        len(list(getattr(getattr(session, "logic", None), "steps", None) or [])),
        len(str(getattr(session, "generated_code", "") or "")),
        str(crop.get("path") or "") if isinstance(crop, dict) else "",
        bool(art.get("last_window_inspect")),
        str(probe.get("code") or "")[:60] if isinstance(probe, dict) else "",
        str(probe.get("stdout_tail") or "")[-240:] if isinstance(probe, dict) else "",
        repr(probe.get("result"))[:240] if isinstance(probe, dict) else "",
        sum(
            1
            for row in list(art.get("measurements") or [])
            if isinstance(row, dict)
            and any(k in row for k in ("rows", "cols", "hits", "counts", "count", "best"))
        ),
        len(list(art.get("evidence") or [])),
        str((art.get("last_asset_inspect") or {}).get("text") or "")[-240:]
        if isinstance(art.get("last_asset_inspect"), dict)
        else "",
        str(art.get("last_vision_desc") or "")[:60],
        str((art.get("viewport_change") or {}).get("asset_id") or "")
        if isinstance(art.get("viewport_change"), dict)
        else "",
    )


_RESULT_CLAIM_RE = re.compile(
    r"(结果(?:是|为)|答案(?:是|为)|完全一致|确认(?:是|为)|"
    r"勋章数(?:是|为|如下)|(?:已经|已)(?:生成|标注|加入素材区|裁好|匹配完成|验证通过))"
)


def unsupported_result_claim(reply: str, before: tuple, after: tuple) -> str:
    """阻止把口头断言冒充本轮产物/实测。返回缺口说明，空串表示可放行。"""
    text = str(reply or "")
    if not _RESULT_CLAIM_RE.search(text):
        return ""
    assets_changed = after[1] > before[1]
    overlays_changed = after[2] > before[2]
    script_changed = after[4] > before[4]
    measured = after[10] > before[10]
    evidenced = after[11] > before[11]
    if re.search(r"(?:已经|已)(?:生成|加入素材区|裁好)", text) and not (
        assets_changed or script_changed
    ):
        return "本轮没有生成脚本或新增素材，不能声称已经生成/加入素材区。"
    if re.search(r"(?:已经|已)标注", text) and not (overlays_changed or assets_changed):
        return "本轮没有新增标注层或标注素材，不能声称已经标注。"
    if re.search(r"结果(?:是|为)|答案(?:是|为)|完全一致|确认(?:是|为)|勋章数", text) and not (
        measured or evidenced
    ):
        return "本轮没有新的工具实测或视觉证据，不能给出确定结果；应回答未知并继续核验。"
    if not (assets_changed or overlays_changed or script_changed or measured or evidenced):
        return "本轮没有新增产物、工具实测或视觉证据，不能声称动作已完成。"
    return ""


def followup_stop_reason(
    *,
    round_index: int,
    tokens_used: int,
    seconds_used: float,
    no_progress: int,
) -> str:
    """到达预算/无进展时返回停下原因；否则空串。"""
    if round_index >= _FOLLOWUP_MAX_ROUNDS:
        return f"自动续轮已达上限（{_FOLLOWUP_MAX_ROUNDS} 轮）"
    if tokens_used > _FOLLOWUP_TOKEN_BUDGET:
        return f"token 预算用尽（{tokens_used} / {_FOLLOWUP_TOKEN_BUDGET}）"
    if seconds_used > _FOLLOWUP_SECONDS:
        return f"时间预算用尽（{int(seconds_used)}s / {int(_FOLLOWUP_SECONDS)}s）"
    return ""


def stall_message(reason: str, meta: dict) -> str:
    """卡住时给用户看的说明：原因 + 当前步骤 + 建议。"""
    plan = meta.get("plan") if isinstance(meta.get("plan"), list) else []
    step = ""
    if plan:
        first = plan[0]
        step = str(first.get("step") if isinstance(first, dict) else first or "").strip()
    lines = [f"卡住了：{reason}。"]
    if step:
        lines.append(f"停在这一步：「{step}」")
    lines.append(
        "可以选：换个做法（我换一条路再试），或者你来手动处理这一步后告诉我结果。"
    )
    return "\n".join(lines)


async def _plan_followups(
    *,
    visible: str,
    meta: dict,
    session,
    perms: dict,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint,
    max_tokens,
    system: str,
    vision_prefetch: str,
    runtime_block: str,
    form_messages,
    on_partial,
    emit,
    total_in: int,
    total_out: int,
    has_frame_now: bool,
    followup_call=None,
    supplement_box=None,
) -> tuple[str, dict, int, int]:
    """仅当模型 turn=act 时再填表，最多 3 次。stop / ask 不再由本地计划拉起来。"""
    from backend.script_generator.agent import call_llm
    from backend.script_generator.collaborator.collab_form import implied_work
    from backend.script_generator.collaborator.canvas import render_asset_refs_for_user

    started_at = time.time()
    started_tokens = int(total_in) + int(total_out)
    fingerprint = _progress_fingerprint(session)
    no_progress = 0
    stop_reason = ""
    stagnant = False
    # 同一条缺口文案连着出现几次：说明"再问一遍"根本推不动（模型没东西可改，
    # 或者它只是在重复承诺）。实况里这种空转能烧满 8 轮，用户只看到反复"我这就做"。
    same_gap = 0
    last_gap = ""

    for i in range(_FOLLOWUP_MAX_ROUNDS):
        if str(meta.get("turn") or "") == "ask" or meta.get("need_permission") or meta.get("need_frame"):
            break
        cont, gap = plan_should_continue(meta, session)
        if not cont:
            if surface_plan_pause(meta):
                emit("→ 计划停在要你操作的一步")
            break
        gap_key = str(gap or "").strip()
        if gap_key and gap_key == last_gap:
            same_gap += 1
        else:
            same_gap = 0
            last_gap = gap_key
        if same_gap >= _SAME_GAP_LIMIT:
            stop_reason = (
                f"同一个缺口连续 {same_gap + 1} 次没被解决——再问也不会变，先停下。"
                f"缺口：{gap_key[:120]}"
            )
            stagnant = True
            emit("→ 自动续跑停滞（同一缺口反复出现）")
            break
        stop_reason = followup_stop_reason(
            round_index=i,
            tokens_used=(int(total_in) + int(total_out)) - started_tokens,
            seconds_used=time.time() - started_at,
            no_progress=no_progress,
        )
        if stop_reason:
            break
        emit(f"→ 计划未完成，自动继续（{i + 1}/{_FOLLOWUP_MAX_ROUNDS}）")
        # 用户补充：只在确定还要再请求一次时才取走，否则会被这轮收尾吞掉。
        # 追加进 session.messages 后，下面重建的 followup_messages 自然带上它。
        supplement_note = ""
        if supplement_box is not None:
            added = supplement_box.drain()
            for extra in added:
                session.add_message("user", f"（用户补充）{extra}")
            if added:
                supplement_note = "\n【用户补充】" + "；".join(added)
                emit(f"→ 收到你的补充（{len(added)} 条），带着它继续")
        note = gap
        work = implied_work(meta, session)
        if work in ("window_aligned", "click_probe"):
            try:
                insp = _run_runtime_tool("inspect_click_target", {})
            except Exception as e:
                insp = {"ok": False, "error": str(e)}
            if not isinstance(session.artifacts, dict):
                session.artifacts = {}
            session.artifacts["last_window_inspect"] = insp
            note += "\n" + str(insp.get("human_summary") or insp.get("error") or "")
            if insp.get("ok") and insp.get("resolved_differs") and not perms.get("runtime_control"):
                hold_for_user(meta, "runtime_control", "等控制窗口授权")
                emit("→ 点击窗不一致，请授权后再改绑")
                break
            if work == "click_probe" and not perms.get("runtime_control"):
                hold_for_user(meta, "runtime_control", "等控制窗口授权")
                emit("→ 要探点击落点，请授权「控制窗口」")
                break
            if work == "click_probe" and perms.get("runtime_control"):
                if isinstance(session.artifacts, dict) and session.artifacts.pop("skip_click_probe_once", None):
                    note += "\n点击探针这轮已经做过，用上一则结果，不要再点。"
                else:
                    try:
                        rec = int(insp.get("recommend_hwnd") or 0)
                        probed = _run_runtime_tool(
                            "message_click_probe", {"hwnd": rec} if rec else {}
                        )
                        note += "\n点击探针：" + str(
                            probed.get("human_summary") or probed.get("error") or probed
                        )[:200]
                    except Exception as e:
                        note += f"\n点击探针失败：{e}"
            if insp.get("ok") and insp.get("resolved_differs") and perms.get("runtime_control"):
                try:
                    rec = int(insp.get("recommend_hwnd") or 0)
                    ret = _run_runtime_tool(
                        "retarget_click_window", {"hwnd": rec} if rec else {}
                    )
                    note += "\n已改绑：" + str(ret.get("error") or "完成")[:160]
                    if not ret.get("error") and ret.get("ok") is not False:
                        session.artifacts["last_window_inspect"] = {
                            **insp,
                            "resolved_differs": False,
                            "ok": True,
                        }
                except Exception as e:
                    note += f"\n改绑失败：{e}"
        action = str(meta.get("action") or "")
        if action in ("match_assets", "match_and_describe") and has_frame_now:
            brief = _auto_match_session_assets(session, limit=2)
            if brief:
                note += "\n" + brief
        note += "\n任务还没完成。更新 plan、done_when、task_done。需要系统做事就 turn=act。reply 只写结论。"
        if supplement_note:
            note += f"\n用户在思考途中补充了：{supplement_note.strip()}\n请把补充纳入本轮判断，必要时改 plan。"
        if not isinstance(session.artifacts, dict):
            session.artifacts = {}
        session.artifacts["agent_plan_gap"] = note[:800]
        remember_plan(session, meta)
        session.artifacts["agent_plan_gap"] = note[:800]
        followup_messages = form_messages(prefetch=vision_prefetch)
        if followup_call is not None:
            # 自动续轮必须和首轮拥有相同的工具通道；否则模型只能反复口头规划。
            text2, inp2, out2 = await followup_call(followup_messages)
        else:
            text2, inp2, out2 = await call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=followup_messages,
                system_prompt=system,
                on_partial=on_partial,
                max_tokens=max_tokens or 2048,
            )
        total_in += int(inp2 or 0)
        total_out += int(out2 or 0)
        visible, meta = parse_collab_json(text2)
        if has_frame_now:
            meta["need_frame"] = False
        visible = render_asset_refs_for_user(
            visible, list(getattr(session, "assets", None) or [])
        )
        remember_plan(session, meta)
        emit(f"→ 计划后续 turn={meta.get('turn') or 'stop'} task_done={bool(meta.get('task_done'))}")
        # 无进展检测：系统侧产物完全没变，说明这一轮只是嘴上推进
        fresh = _progress_fingerprint(session)
        if fresh == fingerprint:
            no_progress += 1
            emit(f"→ 本轮没有任何新产出（{no_progress}/{_NO_PROGRESS_LIMIT}）")
        else:
            no_progress = 0
            fingerprint = fresh

    if not stop_reason and str(meta.get("turn") or "") == "act":
        # 循环把轮数跑完了也要如实汇报，不能又变成一个空洞的「继续」
        stop_reason = followup_stop_reason(
            round_index=_FOLLOWUP_MAX_ROUNDS,
            tokens_used=(int(total_in) + int(total_out)) - started_tokens,
            seconds_used=time.time() - started_at,
            no_progress=no_progress,
        )
    if stop_reason and str(meta.get("turn") or "") != "ask":
        # 预算只约束系统自动续跑，不能覆盖模型已经给出的有效答复，更不能
        # 强制把会话变成 ask。保留计划供用户下一句话继续，并把原因留在诊断层。
        meta.pop("stall_note", None)
        meta["auto_pause_reason"] = stop_reason
        meta["turn"] = "stop"
        if not isinstance(session.artifacts, dict):
            session.artifacts = {}
        session.artifacts["last_auto_pause"] = {
            "reason": stop_reason,
            "plan": list(meta.get("plan") or [])[:5],
            "visible_tail": str(visible or "")[-240:],
            "stagnant": bool(stagnant),
        }
        if stagnant:
            # 停滞意味着这一轮确实没推进（模型只是反复承诺）。这时把原因说给用户，
            # 否则用户只看到一句"我这就出图"然后什么都没发生——实况就是这样。
            note = (
                "\n\n（这一轮没能推进：系统自动重试了几次都卡在同一个缺口上，先停下来了。"
                f"原因：{stop_reason.split('缺口：')[0].strip()}"
                "你可以让我换个做法，或手动做完这步再告诉我结果。）"
            )
            visible = (str(visible or "").rstrip() + note).strip()
            meta["reply"] = visible
        else:
            emit("→ 自动续跑内部暂停（不打断用户）：" + stop_reason[:40])
    remember_plan(session, meta)
    return visible, meta, total_in, total_out


async def _repair_script_followup(
    *,
    visible: str,
    meta: dict,
    session,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint,
    max_tokens,
    system: str,
    vision_prefetch: str,
    runtime_block: str,
    form_messages,
    on_partial,
    emit,
    total_in: int,
    total_out: int,
) -> tuple[str, dict, int, int]:
    """脚本缺 do_work 或调用了不存在的接口时，把缺口退回模型重修。最多 2 次。"""
    from backend.script_generator.agent import call_llm
    from backend.script_generator.collaborator.canvas import render_asset_refs_for_user

    for i in range(2):
        script = str(meta.get("script") or "").strip()
        if not script:
            return visible, meta, total_in, total_out
        issues = collab_script_issues(script, session)
        if not issues:
            return visible, meta, total_in, total_out
        emit(f"→ 脚本缺项，退回重修（{i + 1}/2）")
        note = (
            "脚本校验没过。请只重修 script，点击步骤仍用 click_image，不要改成 wait_image。\n"
            + "\n".join(f"- {item}" for item in issues)
            + "\n必须有 async def do_work(browser: UserWindow)。"
            "只调用 UserWindow 上存在的方法。"
        )
        if not isinstance(session.artifacts, dict):
            session.artifacts = {}
        session.artifacts["agent_plan_gap"] = note[:800]
        messages = form_messages(prefetch=vision_prefetch)
        if messages:
            last = dict(messages[-1])
            last["content"] = str(last.get("content") or "") + "\n\n【脚本重修】\n" + note
            messages = list(messages[:-1]) + [last]
        text2, inp2, out2 = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=system,
            on_partial=on_partial,
            max_tokens=max_tokens or 2048,
        )
        total_in += int(inp2 or 0)
        total_out += int(out2 or 0)
        visible, meta = parse_collab_json(text2)
        visible = render_asset_refs_for_user(
            visible, list(getattr(session, "assets", None) or [])
        )
        remember_plan(session, meta)
        emit(f"→ 脚本重修 turn={meta.get('turn') or 'stop'}")
    return visible, meta, total_in, total_out


_FORM_REPAIR_NOTE = (
    "你上一条回复没有带填表块，也没有给出可读正文（表外正文被判定为推理独白，已丢弃）。\n"
    "请**只**输出一张填好的表，形如：\n"
    "<<<COLLAB_FORM>>>\n"
    '{"reply": "给用户看的一两句中文结论", "turn": "stop", ... 其余字段照空表填}\n'
    "<<<END>>>\n"
    "reply 只写给用户看的话；think / plan 不要写进 reply，也不要写表外正文。"
)


async def _repair_form_followup(
    *,
    visible: str,
    meta: dict,
    session,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint,
    max_tokens,
    system: str,
    vision_prefetch: str,
    form_messages,
    on_partial,
    emit,
    total_in: int,
    total_out: int,
) -> tuple[str, dict, int, int]:
    """首轮没填表且正文被丢掉时，补一次「只输出表」的格式重修。

    真实会话里出现过两种情况：模型整段写英文 CoT 没填表（表外正文被
    ``looks_like_reasoning`` 丢掉后 visible 为空），以及回复彻底为空
    （用户看到"（模型未返回可读正文…）"）。这两种都该重来一次，而不是直接把
    空答复或独白丢给用户。
    """
    from backend.script_generator.agent import call_llm
    from backend.script_generator.collaborator.canvas import render_asset_refs_for_user

    if meta.get("_form_present"):
        return visible, meta, total_in, total_out
    if str(visible or "").strip():
        # 不带表但有正常正文：可能是收尾闲聊，不强制重修
        return visible, meta, total_in, total_out
    emit("⚠ 回复缺填表块，退回重修（1/1）")
    messages = list(form_messages(prefetch=vision_prefetch) or [])
    if messages:
        last = dict(messages[-1])
        last["content"] = str(last.get("content") or "") + "\n\n【格式重修】\n" + _FORM_REPAIR_NOTE
        messages = list(messages[:-1]) + [last]
    if not messages:
        return visible, meta, total_in, total_out
    text2, inp2, out2 = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=messages,
        system_prompt=system,
        on_partial=on_partial,
        max_tokens=max_tokens or 2048,
    )
    total_in += int(inp2 or 0)
    total_out += int(out2 or 0)
    visible, meta = parse_collab_json(text2)
    visible = render_asset_refs_for_user(
        visible, list(getattr(session, "assets", None) or [])
    )
    remember_plan(session, meta)
    emit(f"→ 格式重修 turn={meta.get('turn') or 'stop'}")
    return visible, meta, total_in, total_out


def _latest_match_xy(session) -> Optional[tuple[int, int]]:
    """最近一次匹配框的中心，用作点击探针落点。"""
    chosen = None
    for ov in getattr(session, "overlays", None) or []:
        if not isinstance(ov, dict) or ov.get("kind") != "match":
            continue
        chosen = ov
    if not isinstance(chosen, dict):
        return None
    try:
        x = float(chosen["x"]) + float(chosen["w"]) / 2.0
        y = float(chosen["y"]) + float(chosen["h"]) / 2.0
    except Exception:
        return None
    if x < 1 or y < 1:
        return None
    return int(x), int(y)


async def _finish_after_permission(
    *,
    key: str,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint,
    max_tokens,
    session,
    perms: dict,
    on_partial,
    emit,
    trace: list,
) -> tuple[str, dict[str, Any], int, int]:
    """授权后划掉暂停步，补上被拦住的动作，再按剩下的计划继续。"""
    from backend.script_generator.agent import call_llm
    from backend.script_generator.collaborator.canvas import render_asset_refs_for_user
    from backend.script_generator.collaborator.collab_form import (
        build_status_sheet,
        permission_resume_steps,
        remember_plan,
        advance_plan_after_user,
        _plan_step_text,
    )
    from backend.script_generator.collaborator.runtime_bridge import build_runtime_context

    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    session.artifacts.pop("pending_need_permission", None)
    emit("① 已授权，只补被拦住的一步")
    steps = permission_resume_steps(session, key)
    notes: list[str] = []
    local_ok = False
    insp = session.artifacts.get("last_window_inspect")
    if not isinstance(insp, dict):
        insp = {}

    if "click_probe" in steps and not insp.get("ok"):
        emit("→ 查点击窗")
        try:
            insp = _run_runtime_tool("inspect_click_target", {})
        except Exception as e:
            insp = {"ok": False, "error": str(e)}
        session.artifacts["last_window_inspect"] = insp
        notes.append(str(insp.get("human_summary") or insp.get("error") or "窗口检查失败"))

    if "retarget" in steps and insp.get("resolved_differs"):
        emit("→ 改绑点击窗")
        try:
            rec = int(insp.get("recommend_hwnd") or 0)
            ret = _run_runtime_tool("retarget_click_window", {"hwnd": rec} if rec else {})
        except Exception as e:
            ret = {"ok": False, "error": str(e)}
        notes.append("改绑：" + str(ret.get("error") or ret.get("human_summary") or "完成")[:160])
        if not ret.get("error") and ret.get("ok") is not False:
            local_ok = True
            session.artifacts["last_window_inspect"] = {
                **insp,
                "resolved_differs": False,
                "ok": True,
            }
            insp = session.artifacts["last_window_inspect"]
    elif "retarget" in steps:
        notes.append("窗口已一致，没有改绑。")

    if "click_probe" in steps:
        emit("→ 点击探针")
        xy = _latest_match_xy(session)
        try:
            rec = int(insp.get("recommend_hwnd") or 0) if insp.get("ok") else 0
            args: dict[str, Any] = {}
            if rec:
                args["hwnd"] = rec
            if xy is not None:
                args["x"], args["y"] = xy
            probed = _run_runtime_tool("message_click_probe", args)
        except Exception as e:
            probed = {"ok": False, "error": str(e)}
        session.artifacts["last_click_probe"] = probed
        notes.append(
            "点击探针："
            + str(probed.get("human_summary") or probed.get("error") or "无结果")[:240]
        )
        if probed.get("ok"):
            local_ok = True

    did_click = "click_probe" in steps and bool(
        isinstance(session.artifacts.get("last_click_probe"), dict)
        and session.artifacts["last_click_probe"].get("ok")
    )
    insp_now = session.artifacts.get("last_window_inspect")
    did_retarget = (
        "retarget" in steps
        and isinstance(insp_now, dict)
        and bool(insp_now.get("ok"))
        and not insp_now.get("resolved_differs")
    )
    remaining = advance_plan_after_user(
        session.artifacts.get("agent_plan") or [],
        did_click=did_click,
        did_retarget=bool(did_retarget),
    )
    session.artifacts["agent_plan"] = remaining
    session.artifacts["agent_task_done"] = False
    if did_click:
        session.artifacts["skip_click_probe_once"] = True

    if not notes:
        notes.append("权限已开。不要重做识图、匹配和窗口检查。")
    if remaining:
        notes.append("剩下的计划还要做：" + "；".join(_plan_step_text(x) for x in remaining))

    runtime_block = build_runtime_context().to_prompt_block()
    runtime_block += (
        "\n\n【授权后续跑】\n"
        "用户刚完成了计划里的暂停步。下面是已经做完的结果。"
        "把暂停步从 plan 删掉。剩下的步骤可以留在 plan 里。"
        "不要再要同一项权限，不要重做识图和匹配。"
        "这一轮结束与否由你定：还要系统做就 turn=act，说完了就 turn=stop。"
        "reply 只写当前结论。\n"
        + "\n".join(notes)
    )
    last_user = ""
    for m in reversed(session.messages or []):
        if str(m.get("role") or "") == "user":
            last_user = str(m.get("text") or "")
            break
    system = build_system_prompt(
        None,
        session,
        model_name=model,
        permissions=perms,
        tools_available=False,
    )
    sheet = build_status_sheet(
        session,
        runtime_block=runtime_block,
        permissions=perms,
        last_user=last_user,
    )
    messages = session_messages_for_llm(session, limit=6) + [
        {"role": "user", "content": sheet}
    ]
    emit("② 根据已做结果填结论…")
    text, inp, out = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=messages,
        system_prompt=system,
        on_partial=on_partial,
        max_tokens=max_tokens or 2048,
    )
    visible, meta = parse_collab_json(text)
    visible = render_asset_refs_for_user(
        visible, list(getattr(session, "assets", None) or [])
    )
    meta["need_permission"] = None
    meta["describe_frame"] = False
    meta["match_assets"] = False
    if not (visible or "").strip():
        visible = "\n".join(notes)
    remember_plan(session, meta)

    def _form_messages(*, prefetch: str) -> list:
        hist = session_messages_for_llm(session, limit=6)
        sheet2 = build_status_sheet(
            session,
            vision_prefetch=prefetch,
            runtime_block=runtime_block,
            permissions=perms,
            last_user=last_user,
        )
        return list(hist) + [{"role": "user", "content": sheet2}]

    visible, meta, inp, out = await _plan_followups(
        visible=visible,
        meta=meta,
        session=session,
        perms=perms,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        max_tokens=max_tokens,
        system=system,
        vision_prefetch="",
        runtime_block=runtime_block,
        form_messages=_form_messages,
        on_partial=on_partial,
        emit=emit,
        total_in=int(inp or 0),
        total_out=int(out or 0),
        has_frame_now=_path_is_file(getattr(session, "frame_path", None)),
    )
    visible, meta, inp, out = await _repair_script_followup(
        visible=visible,
        meta=meta,
        session=session,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        max_tokens=max_tokens,
        system=system,
        vision_prefetch="",
        runtime_block=runtime_block,
        form_messages=_form_messages,
        on_partial=on_partial,
        emit=emit,
        total_in=int(inp or 0),
        total_out=int(out or 0),
    )
    meta["_trace"] = list(trace)
    emit(f"④ 完成 · turn={meta.get('turn') or 'stop'}")
    return visible, meta, int(inp or 0), int(out or 0)


async def run_collab_llm_turn(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    max_tokens: Optional[int],
    registry: SkillRegistry,
    session: CollabSession,
    vision_assist: Optional[dict] = None,
    permissions: Optional[dict] = None,
    on_status=None,
    on_partial=None,
    resume_permission: str = "",
    supplement_box=None,
) -> tuple[str, dict[str, Any], int, int]:
    """跑一轮协作 LLM。返回 (visible_reply, meta, in_tok, out_tok)。
    meta['_trace'] 为本轮人话过程轨迹，供 UI 展示。
    on_partial: 流式增量回调（delta 字符串）。
    vision_assist: 独立辅助识图；主模型不支持视觉或视觉调用失败时才回退使用。
    permissions: 协作页权限开关（probe_script / write_files / runtime_control）。
    supplement_box: 用户思考途中的补充；在自动续轮的检查点注入当轮上下文。
    """
    from backend.script_generator.agent import (
        _claude_tools_loop,
        _model_supports_images,
        _openai_tools_loop,
        _provider_api,
        _provider_supports_tools,
        call_llm,
    )
    from backend.script_generator.collaborator.probe_script import normalize_permissions

    selected: list[str] = []
    analyzed: list[dict] = []
    trace: list[str] = []
    total_in = total_out = 0
    token_acc = [0, 0]
    perms = normalize_permissions(permissions)
    primary_vision = None
    if _model_supports_images(provider, model):
        primary_vision = {
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "api_endpoint": api_endpoint,
        }

    def _emit(msg: str) -> None:
        trace.append(msg)
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    def _on_select(sid: str) -> None:
        selected.append(sid)

    def _on_analyze(data: dict) -> None:
        analyzed.append(dict(data))

    resume_key = str(resume_permission or "").strip()
    if resume_key:
        return await _finish_after_permission(
            key=resume_key,
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            max_tokens=max_tokens,
            session=session,
            perms=perms,
            on_partial=on_partial,
            emit=_emit,
            trace=trace,
        )

    last_user = ""
    for m in reversed(session.messages or []):
        if str(m.get("role") or "") == "user":
            last_user = str(m.get("text") or "")
            break
    # 本轮所有工具调用之前拍下产物指纹，供最终回复的证据门槛比较。
    turn_start_fp = _progress_fingerprint(session)

    # 默认只给短状态，不自动打 VL；由主模型调 describe_current_frame / describe_frame。
    # 例外：用户明确「再分析」等强制词 → 本轮直接识图并注入正文。
    vision_prefetch = ""
    if _path_is_file(session.frame_path):
        vision_prefetch = _frame_vision_status(session)

    force_vision = any(
        k in (last_user or "") for k in ("再分析", "重新识图", "再看一眼", "重新看")
    )
    if force_vision and _path_is_file(session.frame_path):
        _emit("①′ 用户要求重新识图…")
        vdata = await _describe_frame_with_fallback(
            session,
            primary_vision,
            vision_assist,
            user_question=last_user,
            force=True,
            on_status=_emit,
        )
        total_in += int(vdata.get("tokens_in") or 0)
        total_out += int(vdata.get("tokens_out") or 0)
        if vdata.get("ok") and vdata.get("text"):
            vision_prefetch = f"【视觉观察】\n{vdata['text']}"
            tag = "缓存" if vdata.get("cached") else "新识图"
            source = "主模型视觉" if vdata.get("visual_source") == "primary" else "辅助识图"
            _emit(f"✓ {source}（{tag}）· {str(vdata['text'])[:36]}")
        else:
            _emit(f"⚠ 视觉查看失败：{vdata.get('error') or '未知'}")
    elif (
        _path_is_file(session.frame_path)
        and _user_wants_vision(last_user)
        and not _vision_assist_ready(primary_vision)
        and not _vision_assist_ready(vision_assist)
        and not _cached_assist_vision(session)
    ):
        # 仅提示配置缺失；不自动打 API
        _emit(
            "①′ 当前主模型不支持视觉；若需看图请配置辅助识图"
            "（API 配置 → 识图提供商/Key/VL 模型）"
        )

    # 运行时窗口/日志上下文 + 点不动时自动 inspect（纯文本模型无 tools 时尤其需要）
    from backend.script_generator.collaborator.runtime_bridge import (
        build_runtime_context,
    )

    runtime_ctx = build_runtime_context()
    runtime_block = runtime_ctx.to_prompt_block()
    if _user_wants_click_diag(last_user):
        _emit("①′ 检查点击目标窗…")
        try:
            insp = _run_runtime_tool("inspect_click_target", {})
            brief = str(insp.get("human_summary") or insp.get("error") or "")
            if brief:
                runtime_block = (
                    f"{runtime_block}\n\n【本轮窗口诊断】\n{brief}"
                )
                if insp.get("ok") and insp.get("recommend_hwnd"):
                    runtime_block += (
                        f"\n推荐 hwnd={insp.get('recommend_hwnd')}；"
                        "确认后可 retarget_click_window。"
                    )
                _emit(f"✓ 窗口诊断 · {brief[:40]}")
            session.artifacts["last_window_inspect"] = insp
        except Exception as e:
            _emit(f"⚠ 窗口诊断失败：{e}")

    supports = _provider_supports_tools(provider)
    if "reasoner" in (model or "").lower():
        supports = False
    try:
        from backend.script_generator.collaborator.resource_registry import (
            snapshot_workspace,
            sync_capabilities,
        )

        sync_capabilities(
            session,
            tool_schemas=all_collab_tools(),
            skills=registry.all() if registry is not None else [],
        )
        snapshot_workspace(session, reason="llm_turn_start")
    except Exception:
        pass
    system = build_system_prompt(
        registry,
        session,
        model_name=model,
        vision_prefetch=vision_prefetch,
        runtime_block=runtime_block,
        permissions=perms,
        tools_available=supports,
        primary_visual=bool(primary_vision),
    )
    capability = build_capability_block(perms)

    def _form_messages(*, prefetch: str) -> list[dict]:
        """短历史 + 本轮状态表（发表格）。"""
        hist = session_messages_for_llm(session, limit=8)
        sheet = build_status_sheet(
            session,
            vision_prefetch=prefetch,
            runtime_block=runtime_block,
            capability_block=capability,
            permissions=perms,
            last_user=last_user,
        )
        return list(hist) + [{"role": "user", "content": sheet}]

    messages = _form_messages(prefetch=vision_prefetch)
    resolve = make_tool_resolver(
        registry,
        session,
        primary_vision=primary_vision,
        vision_assist=vision_assist,
        permissions=perms,
        on_select=_on_select,
        on_analyze=_on_analyze,
        on_status=on_status,
        trace=trace,
        token_acc=token_acc,
    )

    api = _provider_api(provider)
    text = ""
    inp = out = 0

    async def _followup_with_tools(followup_messages):
        """让自动续轮复用首轮工具集与 resolver。"""
        try:
            if api == "claude":
                return await _claude_tools_loop(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=followup_messages,
                    system_prompt=system,
                    on_partial=on_partial,
                    on_status=on_status,
                    max_tokens=max_tokens or 2048,
                    max_tool_rounds=4,
                    tools=claude_collab_tools(),
                    resolve_tool=resolve,
                    status_busy="② 继续执行计划…",
                    status_after="② 工具结果已收回，继续推理…",
                    status_call_prefix="🔧 工具",
                )
            return await _openai_tools_loop(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=followup_messages,
                system_prompt=system,
                on_partial=on_partial,
                on_status=on_status,
                max_tokens=max_tokens or 2048,
                max_tool_rounds=4,
                tools=all_collab_tools(),
                resolve_tool=resolve,
                status_busy="② 继续执行计划…",
                status_after="② 工具结果已收回，继续推理…",
                status_call_prefix="🔧 工具",
            )
        except Exception as e:
            _emit(f"⚠ 续轮工具通道不可用，改用填表（{e}）")
            return await call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=followup_messages,
                system_prompt=system,
                on_partial=on_partial,
                max_tokens=max_tokens or 2048,
            )

    _emit(f"① 使用模型 · {model or provider}")
    # 主路径：填表。tools 仅作可选增强；失败则回退纯填表。
    if supports and api in ("openai", "claude"):
        _emit("② 理解目标（可调用工具，最终须填表）…")
        try:
            if api == "claude":
                text, inp, out = await _claude_tools_loop(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=messages,
                    system_prompt=system,
                    on_partial=on_partial,
                    on_status=on_status,
                    max_tokens=max_tokens or 2048,
                    max_tool_rounds=4,
                    tools=claude_collab_tools(),
                    resolve_tool=resolve,
                    status_busy="② 模型思考中…",
                    status_after="② 工具结果已收回，继续推理…",
                    status_call_prefix="🔧 工具",
                )
            else:
                text, inp, out = await _openai_tools_loop(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=messages,
                    system_prompt=system,
                    on_partial=on_partial,
                    on_status=on_status,
                    max_tokens=max_tokens or 2048,
                    max_tool_rounds=4,
                    tools=all_collab_tools(),
                    resolve_tool=resolve,
                    status_busy="② 模型思考中…",
                    status_after="② 工具结果已收回，继续推理…",
                    status_call_prefix="🔧 工具",
                )
        except Exception as e:
            _emit(f"⚠ 工具通道不可用，改用填表（{e}）")
            _emit("② 填表推理中…")
            text, inp, out = await call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=messages,
                system_prompt=system,
                on_partial=on_partial,
                max_tokens=max_tokens or 2048,
            )
    else:
        _emit("② 填表推理中…")
        text, inp, out = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=system,
            on_partial=on_partial,
            max_tokens=max_tokens or 2048,
        )

    total_in += int(inp or 0)
    total_out += int(out or 0)
    total_in += int(token_acc[0] or 0)
    total_out += int(token_acc[1] or 0)

    _emit("③ 整理回复…")
    visible, meta = parse_collab_json(text)
    evidence_gap = unsupported_result_claim(
        visible, turn_start_fp, _progress_fingerprint(session)
    )
    if evidence_gap:
        _emit("⚠ 拦截无证据的确定性结论")
        visible = f"目前还不能下确定结论：{evidence_gap}"
        meta["reply"] = visible
        meta["task_done"] = False
        if not isinstance(session.artifacts, dict):
            session.artifacts = {}
        session.artifacts["agent_plan_gap"] = evidence_gap
    # 用户对当前图片的说法：按图片作用域留档，只供独立测量后对照
    try:
        from backend.script_generator.collaborator.tool_sanity import remember_answer

        if remember_answer(session, meta.get("answer")):
            _emit("→ 记下当前图片的用户说法（非标准答案）")
    except Exception:
        pass
    followup_reason = ""
    if selected and not meta.get("select_skill"):
        meta["select_skill"] = selected[-1]
    # 已有画面时不要再逼用户传图
    has_frame_now = _path_is_file(session.frame_path)
    if has_frame_now:
        meta["need_frame"] = False
    elif selected and meta.get("need_frame") is None:
        meta["need_frame"] = True
    if analyzed:
        meta["analyze_result"] = analyzed[-1]
        meta["analyze_frame"] = False
    elif meta.get("analyze_frame") and has_frame_now:
        _emit("→ 按模型指示再跑画面探针…")
        extra = run_frame_analyze(
            registry, session, skill_id=str(meta.get("select_skill") or "") or None
        )
        if extra.get("ok"):
            meta["analyze_result"] = extra
            if extra.get("skill_id"):
                meta["select_skill"] = extra["skill_id"]

    # 工具路径已识图：并入本轮画面信息
    if isinstance(session.artifacts, dict):
        tool_desc = str(session.artifacts.get("last_vision_desc") or "").strip()
        if tool_desc and tool_desc not in (vision_prefetch or ""):
            vision_prefetch = (
                f"{vision_prefetch}\n\n【视觉观察】\n{tool_desc}".strip()
                if (vision_prefetch or "").strip()
                else tool_desc
            )

    # 由 LLM 的 turn 决定本轮是否结束。stop 不再因为口头提到点击窗而重开一轮。
    turn = normalize_turn_meta(meta, visible)
    _emit(f"→ 本轮结局 turn={turn}")

    # 能力契约：plan 里的承诺必须有对应的手/授权，做不到就不许收工。
    try:
        _tool_names = [t["function"]["name"] for t in all_collab_tools()]
        gaps = plan_contract_gaps(meta, tool_names=_tool_names, permissions=perms)
    except Exception:
        gaps = []
    if gaps:
        note = contract_gap_note(gaps)
        meta["contract_gaps"] = gaps
        meta["contract_note"] = note
        if not isinstance(session.artifacts, dict):
            session.artifacts = {}
        session.artifacts["contract_gap"] = note
        _emit("⚠ 能力契约：" + gaps[0][:44])
        perm_gap = next((g for g in gaps if "权限" in g), "")
        if perm_gap:
            meta["need_permission"] = meta.get("need_permission") or (
                "write_files" if "写文件" in perm_gap else "runtime_control"
            )
            meta["turn"] = "ask"
            turn = "ask"
        elif turn == "stop":
            # 工具不存在：不许当作已完成，给模型一轮改计划
            meta["turn"] = "act"
            turn = "act"
    elif isinstance(session.artifacts, dict):
        session.artifacts.pop("contract_gap", None)

    if turn == "ask":
        # 等用户：不再自动匹配/识图，留给授权卡/chips
        meta["match_assets"] = False
        meta["describe_frame"] = False
        meta["need_vision"] = False
    # 仅当模型明确 match_assets:true 时跑本地 OpenCV 匹配
    if meta.get("match_assets") and has_frame_now and turn != "ask":
        match_brief = _auto_match_session_assets(session, limit=2)
        meta["match_assets"] = False
        if match_brief:
            vision_prefetch = (
                f"{vision_prefetch}\n\n{match_brief}".strip()
                if (vision_prefetch or "").strip()
                else match_brief
            )
            _emit("✓ " + match_brief.splitlines()[0][:48])
            followup_reason = "match"

    # 纯文本协议：模型设 describe_frame / need_vision → 识图后再答一轮
    want_vision = bool(meta.get("describe_frame") or meta.get("need_vision"))
    if want_vision and has_frame_now:
        already = ""
        if isinstance(session.artifacts, dict):
            already = str(session.artifacts.get("last_vision_desc") or "").strip()
        if already and "【视觉观察】" in (vision_prefetch or "") and not force_vision:
            meta["describe_frame"] = False
            meta["need_vision"] = False
        else:
            _emit("①″ 主模型请求看图（describe_frame）…")
            vdata = await _describe_frame_with_fallback(
                session,
                primary_vision,
                vision_assist,
                user_question=last_user,
                force=force_vision,
                on_status=_emit,
            )
            total_in += int(vdata.get("tokens_in") or 0)
            total_out += int(vdata.get("tokens_out") or 0)
            if vdata.get("ok") and vdata.get("text"):
                desc = str(vdata["text"])
                vision_prefetch = (
                    f"{vision_prefetch}\n\n【视觉观察】\n{desc}".strip()
                    if (vision_prefetch or "").strip()
                    else f"【视觉观察】\n{desc}"
                )
                tag = "缓存" if vdata.get("cached") else "新识图"
                source = "主模型视觉" if vdata.get("visual_source") == "primary" else "辅助识图"
                _emit(f"✓ {source}（{tag}）· {desc[:36]}")
                messages2 = _form_messages(prefetch=vision_prefetch)
                _emit("②′ 已识图，请模型再填表…")
                text2, inp2, out2 = await call_llm(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=messages2,
                    system_prompt=system,
                    on_partial=on_partial,
                    max_tokens=max_tokens or 2048,
                )
                total_in += int(inp2 or 0)
                total_out += int(out2 or 0)
                visible, meta = parse_collab_json(text2)
                if selected and not meta.get("select_skill"):
                    meta["select_skill"] = selected[-1]
                if has_frame_now:
                    meta["need_frame"] = False
                meta["describe_frame"] = False
                meta["need_vision"] = False
                meta["match_assets"] = False
                meta["_did_vision_followup"] = True
            else:
                _emit(f"⚠ 视觉查看失败：{vdata.get('error') or '未知'}")
                meta["describe_frame"] = False
                meta["need_vision"] = False

    # 仅有自动匹配结果、未走识图二轮时：仍让主模型基于匹配结果再填表
    if followup_reason == "match" and "【模板匹配】" in (vision_prefetch or ""):
        if not meta.get("_did_vision_followup"):
            messages2 = _form_messages(prefetch=vision_prefetch)
            _emit("②′ 已匹配，请模型再填表…")
            text2, inp2, out2 = await call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=messages2,
                system_prompt=system,
                on_partial=on_partial,
                max_tokens=max_tokens or 2048,
            )
            total_in += int(inp2 or 0)
            total_out += int(out2 or 0)
            visible, meta = parse_collab_json(text2)
            if selected and not meta.get("select_skill"):
                meta["select_skill"] = selected[-1]
            if has_frame_now:
                meta["need_frame"] = False
            meta["describe_frame"] = False
            meta["need_vision"] = False
            meta["match_assets"] = False

    # 模型仍索要截图时改写（会话里已有图）
    ar = meta.get("analyze_result") if isinstance(meta.get("analyze_result"), dict) else {}
    visible = sanitize_visible_when_frame_ready(
        visible,
        has_frame=has_frame_now,
        probe_brief=vision_prefetch
        or _slim_probe_for_prompt(
            session.probe if isinstance(session.probe, dict) else None
        ),
        analyze_summary=str(ar.get("summary") or ar.get("probe_brief") or ""),
    )
    # 用户侧：把 [[asset_id]] 换成当前别名（LLM 侧始终认 id，改名不触发重识）
    from backend.script_generator.collaborator.canvas import render_asset_refs_for_user

    visible = render_asset_refs_for_user(
        visible, list(getattr(session, "assets", None) or [])
    )
    # 收尾：本轮 act 已做完，若挂着授权请求 → 强制 ask 弹卡
    pending_ask = str(meta.pop("_ask_after_act", None) or "").strip()
    if pending_ask:
        meta["need_permission"] = pending_ask
        meta["turn"] = "ask"
        meta["match_assets"] = False
        meta["describe_frame"] = False
        meta["need_vision"] = False
        _emit(f"→ 匹配/识图已完成，请授权：{pending_ask}")
    else:
        # 再扫一遍口头要权（模型可能二轮才提）
        turn = normalize_turn_meta(meta, visible)
        meta["turn"] = turn
        if turn == "ask":
            _emit(f"→ 收束为 ask · {meta.get('need_permission') or 'chips'}")
        # 若 normalize 又挂了 _ask_after_act（二轮才提权），直接兑现
        pending_ask = str(meta.pop("_ask_after_act", None) or "").strip()
        if pending_ask:
            meta["need_permission"] = pending_ask
            meta["turn"] = "ask"
            meta["match_assets"] = False
            meta["describe_frame"] = False
            _emit(f"→ 需要用户授权：{pending_ask}")
    if meta.get("select_skill"):
        _emit("→ 已对上玩法能力")
    if meta.get("need_frame"):
        _emit("→ 还需要游戏画面")
    if meta.get("analyze_result"):
        _emit("→ 已带上画面分析结果")
    need_perm = str(meta.get("need_permission") or "").strip()
    if not need_perm and isinstance(session.artifacts, dict):
        pending = session.artifacts.pop("pending_need_permission", None)
        if pending:
            need_perm = str(pending).strip()
            meta["need_permission"] = need_perm
    # 模型明确 turn=ask 时保留授权字段，交给界面弹卡。已授权且不是在问用户，才跳过。
    if need_perm and perms.get(need_perm) and str(meta.get("turn") or "") != "ask":
        _emit(f"→ 已有权限 {need_perm}，跳过授权卡")
        meta["need_permission"] = None
        need_perm = ""
        chips = [
            c
            for c in (meta.get("chips") or [])
            if str(c)
            not in ("打开写文件权限", "打开控制窗口权限", "打开临时脚本权限")
        ]
        meta["chips"] = chips
        # 若模型仍口头要做事，按 act 再走一轮行动标记
        turn = normalize_turn_meta(meta, visible)
        meta["turn"] = turn
    if need_perm:
        meta["turn"] = "ask"
        _emit(f"→ 需要用户授权：{need_perm}")
        # 确保 chips 里有入口（纯文本模型常忘）
        chips = list(meta.get("chips") or [])
        if need_perm == "write_files" and "打开写文件权限" not in chips:
            chips = (["打开写文件权限"] + chips)[:3]
            meta["chips"] = chips
        elif need_perm == "runtime_control" and "打开控制窗口权限" not in chips:
            chips = (["打开控制窗口权限"] + chips)[:3]
            meta["chips"] = chips
    remember_plan(session, meta)
    visible, meta, total_in, total_out = await _plan_followups(
        visible=visible,
        meta=meta,
        session=session,
        perms=perms,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        max_tokens=max_tokens,
        system=system,
        vision_prefetch=vision_prefetch,
        runtime_block=runtime_block,
        form_messages=_form_messages,
        on_partial=on_partial,
        emit=_emit,
        total_in=total_in,
        total_out=total_out,
        has_frame_now=has_frame_now,
        followup_call=_followup_with_tools if supports and api in ("openai", "claude") else None,
        supplement_box=supplement_box,
    )
    visible, meta, total_in, total_out = await _repair_script_followup(
        visible=visible,
        meta=meta,
        session=session,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        max_tokens=max_tokens,
        system=system,
        vision_prefetch=vision_prefetch,
        runtime_block=runtime_block,
        form_messages=_form_messages,
        on_partial=on_partial,
        emit=_emit,
        total_in=total_in,
        total_out=total_out,
    )
    visible, meta, total_in, total_out = await _repair_form_followup(
        visible=visible,
        meta=meta,
        session=session,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        max_tokens=max_tokens,
        system=system,
        vision_prefetch=vision_prefetch,
        form_messages=_form_messages,
        on_partial=on_partial,
        emit=_emit,
        total_in=total_in,
        total_out=total_out,
    )
    # 最终回复还要再过一次：自动续轮/修复轮可能替换了首轮正文。
    final_gap = unsupported_result_claim(
        visible, turn_start_fp, _progress_fingerprint(session)
    )
    if final_gap:
        _emit("⚠ 最终回复缺少可核验依据，已降级为未知")
        visible = f"目前还不能下确定结论：{final_gap}"
        meta["reply"] = visible
        meta["task_done"] = False
    if not visible:
        visible = "（模型未返回可读正文，请再试一次或换模型。）"
    meta["_trace"] = list(trace)
    _emit(f"④ 完成 · turn={meta.get('turn') or 'stop'}")
    return visible, meta, total_in, total_out
