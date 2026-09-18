"""协作回合：调用用户 API 配置的 LLM。

主协议：系统发【状态摘要】+ 空表 → LLM 只填 <<<COLLAB_FORM>>> → 系统解析呈现。
OpenAI/Claude 仍可选用 tools；最终答复仍须落成同一张表（兼容旧 COLLAB_JSON）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from backend.script_generator.collaborator.skills.registry import SkillRegistry
from backend.script_generator.collaborator.session import CollabSession
from backend.script_generator.collaborator.collab_form import (
    build_form_system_prompt,
    build_status_sheet,
    classify_done_when,
    form_to_runtime_meta,
    parse_collab_form,
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
) -> str:
    """稳定系统提示（填表规则）。状态摘要每轮另发，不塞进 system。"""
    _ = (registry, session, vision_prefetch, runtime_block, permissions)
    return build_form_system_prompt(
        model_name=model_name,
        tools_available=bool(tools_available),
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
    """从口头回复推断 need_permission；模型常说要写权限却不写 JSON。"""
    t = (text or "").strip()
    if not t:
        return ""
    write_keys = (
        "写文件权限", "写权限", "写入权限", "给写权限", "打开写文件",
        "需要写文件", "要写文件", "写文件需要", "得你给写", "请给写",
        "授权写", "需要你授权", "请你授权", "得你授权", "要你授权",
        "存成正式", "保存为正式", "写入素材", "落盘", "write_files",
    )
    runtime_keys = (
        "控制窗口", "runtime_control", "改绑窗口", "控制权限", "窗口权限",
    )
    if any(k in t for k in write_keys):
        return "write_files"
    # 组合：「写文件/落盘」+「授权/权限」
    if (("写文件" in t) or ("落盘" in t) or ("存模板" in t) or ("正式模板" in t)) and (
        ("授权" in t) or ("权限" in t)
    ):
        return "write_files"
    if any(k in t for k in runtime_keys):
        return "runtime_control"
    if (("窗口" in t) or ("改绑" in t)) and (("授权" in t) or ("权限" in t)):
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
    """无工具通道时：对焦点或前 N 张素材做匹配，结果写 overlays。"""
    from backend.script_generator.collaborator.canvas import (
        resolve_template_path,
        template_size,
        upsert_match_overlay,
    )
    from backend.script_generator.collaborator.probe_script import match_template_on_frame

    frame = getattr(session, "frame_path", None)
    if not _path_is_file(frame):
        return ""
    assets = list(getattr(session, "assets", None) or [])
    if not assets:
        return ""
    focused = list(getattr(session, "focused_asset_ids", None) or [])
    chosen = []
    if focused:
        by_id = {str(a.get("id")): a for a in assets}
        for aid in focused:
            if aid in by_id:
                chosen.append(by_id[aid])
    if not chosen:
        chosen = assets[:limit]
    chosen = chosen[:limit]
    lines = []
    for a in chosen:
        aid = str(a.get("id") or "")
        name = str(a.get("name") or Path(str(a.get("path") or "")).name)
        path, asset, err = resolve_template_path(session, asset_id=aid)
        if err or path is None:
            lines.append(f"· [[{aid}]] 无法匹配：{err or '无路径'}")
            continue
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
    if any(k in m for k in ("-vl", "vl-", "vision", "llava", "gpt-4o", "gemini", "4v")):
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
    on_status=None,
) -> tuple[str, int, int]:
    """用 API 配置的辅助识图模型描述协作画面，供纯文本主模型使用。"""
    from backend.script_generator.agent import (
        _image_b64,
        _provider_api,
        _provider_supports_images,
        call_llm,
    )

    provider = str(vision_assist.get("provider") or "").strip()
    api_key = str(vision_assist.get("api_key") or "").strip()
    model = str(vision_assist.get("model") or "").strip()
    api_endpoint = vision_assist.get("api_endpoint") or None
    if not provider or not api_key or not model:
        raise RuntimeError("辅助识图未配置完整（提供商 / Key / 模型）")
    if not _provider_supports_images(provider):
        raise RuntimeError(
            f"辅助识图提供商「{provider}」未标记支持传图，请换成带视觉能力的提供商"
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
            on_status(f"①′ 辅助识图 · {provider}/{model}…")
        except Exception:
            pass

    b64data, media_type = _image_b64(path, compress=True, max_size=1280)
    # _image_b64 压缩后始终 png 编码
    media_type = "image/png"
    api = _provider_api(provider)
    q = (user_question or "").strip()
    ask = (
        f"用户本轮问题：「{q}」。请优先回答与该问题相关的画面细节。\n"
        if q
        else ""
    )
    user_text = (
        f"{ask}"
        "下面消息里附带了一张游戏截图（image）。请直接根据像素描述，禁止说「看不到图」。\n"
        "用中文简要说明：\n"
        "1) 这是什么界面/场景；\n"
        "2) 主要可点击控件与可见文字；\n"
        "3) 若有重复图标/格子/数量信息，尽量逐个说明位置与数量；\n"
        "4) 看不清的局部才写不确定。\n"
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
        "You are a vision model. The user message includes a real screenshot. "
        "Describe it in short Chinese. Never claim you cannot see the image."
    )
    text, inp, out = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=[{"role": "user", "content": content}],
        system_prompt=system,
        max_tokens=1024,
    )
    text = (text or "").strip()
    if _assist_vision_is_blind(text):
        raise RuntimeError(
            f"识图模型「{model}」返回「看不到图」。"
            "请确认辅助识图选的是视觉模型（qwen-vl-* / gpt-4o 等），"
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
    force: bool = False,
    on_status=None,
) -> dict[str, Any]:
    """缓存优先的辅助识图。返回 ok/text/cached/tokens_*/error。"""
    if not _path_is_file(session.frame_path):
        return {"ok": False, "error": "当前没有可用画面"}
    if not force:
        cached = _cached_assist_vision(session)
        if cached:
            if isinstance(session.artifacts, dict):
                session.artifacts["last_vision_desc"] = cached
            return {
                "ok": True,
                "text": cached,
                "cached": True,
                "tokens_in": 0,
                "tokens_out": 0,
            }
    if not _vision_assist_ready(vision_assist):
        return {
            "ok": False,
            "error": (
                "未配置辅助识图；请到「1. API 配置」填写识图提供商/Key/模型"
                "（如 qwen-vl-max / qwen-vl-plus）"
            ),
        }
    try:
        desc, vin, vout = await describe_collab_frame(
            frame_path=Path(session.frame_path),
            vision_assist=vision_assist or {},
            user_question=user_question,
            on_status=on_status,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not (desc or "").strip():
        return {"ok": False, "error": "辅助识图返回为空"}
    _store_assist_vision(
        session,
        desc,
        model=str((vision_assist or {}).get("model") or ""),
    )
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    session.artifacts["last_vision_desc"] = desc
    return {
        "ok": True,
        "text": desc,
        "cached": False,
        "tokens_in": int(vin or 0),
        "tokens_out": int(vout or 0),
    }


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
                    "用辅助识图模型描述当前游戏截图（主模型看不见像素时用这个）。"
                    "同图若已有缓存会直接返回；用户要求「再分析」或图已换时 force=true。"
                    "闲聊、改绑窗口、点不动诊断通常不必调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "希望识图重点回答的问题；可省略",
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
                "name": "match_template",
                "description": (
                    "对共同视口底图做模板匹配，返回 score/坐标，并把匹配框叠到标注层。"
                    "优先传 asset_id（会话素材）；也可 template_path。"
                    "禁止一次对多张素材全量匹配。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "会话素材 id（推荐）",
                        },
                        "template_path": {
                            "type": "string",
                            "description": "模板路径或文件名；无 asset_id 时用",
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
                "name": "list_session_assets",
                "description": "列出共同视口素材区中的模板/截图（id/name/role/焦点）",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "focus_asset",
                "description": (
                    "设置素材区焦点（最多 2 个）。后续 match_template 可省略 asset_id 用焦点。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "要设为焦点的素材 id",
                        },
                        "asset_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "批量设置焦点（最多 2）",
                        },
                    },
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
                    "Path, PROJECT_ROOT, ASSETS, list_assets(), save_image/write_text（需写文件权限）, "
                    "runtime_call（需控制窗口权限）。把结论赋给 result。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "完整 Python 源码",
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
                    "获取账号窗口绑定与近期相关日志摘要。"
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
    ]


def claude_collab_tools() -> list[dict]:
    out = []
    for t in openai_collab_tools():
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
    "select_skill": "挂上对应玩法",
    "get_session_status": "查看会话状态",
    "analyze_current_frame": "分析当前画面",
    "describe_current_frame": "辅助识图",
    "match_template": "模板匹配",
    "list_session_assets": "查看素材区",
    "focus_asset": "聚焦素材",
    "run_probe_script": "临时探针脚本",
    "get_permissions": "查看权限",
    "get_runtime_context": "查看窗口与日志",
    "inspect_click_target": "检查点击目标窗",
    "message_click_probe": "消息点击探针",
    "retarget_click_window": "改绑点击窗口",
}


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
    if name == "match_template":
        if data.get("ok"):
            sc = data.get("score")
            matched = "命中" if data.get("matched") else "未命中"
            return f"{matched} · score={sc}"
        return str(data.get("error") or "失败")
    if name == "list_session_assets":
        n = len(data.get("assets") or [])
        return f"共 {n} 张"
    if name == "focus_asset":
        if data.get("ok"):
            return "焦点 " + ",".join(data.get("focused") or [])
        return str(data.get("error") or "失败")
    if name == "run_probe_script":
        if data.get("ok"):
            return "完成" + (f" · {str(data.get('result'))[:28]}" if data.get("result") is not None else "")
        if data.get("need_permission"):
            return f"需权限 {data.get('need_permission')}"
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


def make_tool_resolver(
    registry: SkillRegistry,
    session: CollabSession,
    *,
    vision_assist: Optional[dict] = None,
    permissions: Optional[dict] = None,
    on_select: Optional[Callable[[str], None]] = None,
    on_analyze: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    trace: Optional[list[str]] = None,
    token_acc: Optional[list[int]] = None,
) -> Callable:
    from backend.script_generator.collaborator.probe_script import (
        match_template_on_frame,
        normalize_permissions,
        run_probe_script,
    )
    from backend.script_generator.collaborator.canvas import (
        resolve_template_path,
        set_focused,
        template_size,
        upsert_match_overlay,
    )

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
        if name == "list_skills":
            lines = registry.catalog_lines()
            raw = json.dumps({"skills": lines}, ensure_ascii=False)
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
                    "focused_asset_ids": list(
                        getattr(session, "focused_asset_ids", None) or []
                    ),
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
            raw = json.dumps(data, ensure_ascii=False)
        elif name == "describe_current_frame":
            force = bool(args.get("force"))
            q = str(args.get("question") or "").strip()
            try:
                data = _await_coro(
                    _assist_describe_frame(
                        session,
                        vision_assist,
                        user_question=q,
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
            raw = json.dumps(data, ensure_ascii=False)
        elif name == "list_session_assets":
            assets = list(getattr(session, "assets", None) or [])
            focused = list(getattr(session, "focused_asset_ids", None) or [])
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
                            "focused": a.get("id") in focused,
                        }
                        for a in assets
                    ],
                    "focused": focused,
                    "n": len(assets),
                },
                ensure_ascii=False,
            )
        elif name == "focus_asset":
            ids = []
            if args.get("asset_ids"):
                ids = [str(x) for x in (args.get("asset_ids") or [])]
            elif args.get("asset_id"):
                ids = [str(args.get("asset_id"))]
            focused = set_focused(session, ids)
            raw = json.dumps(
                {"ok": True, "focused": focused},
                ensure_ascii=False,
            )
        elif name == "match_template":
            if not _path_is_file(session.frame_path):
                data = {"ok": False, "error": "当前没有可用画面（共同视口底图为空）"}
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
                    data = match_template_on_frame(
                        frame_path=Path(session.frame_path),
                        template_path=tmpl_path,
                        threshold=thr_f,
                    )
                    if data.get("ok") and data.get("matched"):
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
            raw = json.dumps(data, ensure_ascii=False)
        elif name == "run_probe_script":
            data = run_probe_script(
                str(args.get("code") or ""),
                session=session,
                permissions=perms,
            )
            if isinstance(data, dict) and data.get("need_permission"):
                if not isinstance(session.artifacts, dict):
                    session.artifacts = {}
                session.artifacts["pending_need_permission"] = data["need_permission"]
            raw = json.dumps(data, ensure_ascii=False)
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
            raw = json.dumps(data, ensure_ascii=False)
        else:
            raw = json.dumps({"ok": False, "error": f"unknown tool {name}"}, ensure_ascii=False)
        _emit(f"✓ {label} · {_short_tool_result(name, raw)}")
        return raw

    return resolve


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
) -> tuple[str, dict, int, int]:
    """仅当模型 turn=act 时再填表，最多 3 次。stop / ask 不再由本地计划拉起来。"""
    from backend.script_generator.agent import call_llm
    from backend.script_generator.collaborator.collab_form import implied_work
    from backend.script_generator.collaborator.canvas import render_asset_refs_for_user

    for i in range(3):
        if str(meta.get("turn") or "") == "ask" or meta.get("need_permission") or meta.get("need_frame"):
            break
        cont, gap = plan_should_continue(meta, session)
        if not cont:
            if surface_plan_pause(meta):
                emit("→ 计划停在要你操作的一步")
            break
        emit(f"→ 计划未完成，继续填表（{i + 1}/3）")
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
        if not isinstance(session.artifacts, dict):
            session.artifacts = {}
        session.artifacts["agent_plan_gap"] = note[:800]
        remember_plan(session, meta)
        session.artifacts["agent_plan_gap"] = note[:800]
        text2, inp2, out2 = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=form_messages(prefetch=vision_prefetch),
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


def _focused_match_xy(session) -> Optional[tuple[int, int]]:
    """焦点素材上一次匹配框的中心，用作点击探针落点。"""
    focus = [str(x) for x in (getattr(session, "focused_asset_ids", None) or []) if str(x)]
    chosen = None
    for ov in getattr(session, "overlays", None) or []:
        if not isinstance(ov, dict) or ov.get("kind") != "match":
            continue
        if focus and str(ov.get("asset_id") or "") not in focus:
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
        xy = _focused_match_xy(session)
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
) -> tuple[str, dict[str, Any], int, int]:
    """跑一轮协作 LLM。返回 (visible_reply, meta, in_tok, out_tok)。
    meta['_trace'] 为本轮人话过程轨迹，供 UI 展示。
    on_partial: 流式增量回调（delta 字符串）。
    vision_assist: API 配置页的辅助识图；主模型看不了图时用于描述当前画面。
    permissions: 协作页权限开关（probe_script / write_files / runtime_control）。
    """
    from backend.script_generator.agent import (
        _claude_tools_loop,
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

    # 默认只给短状态，不自动打 VL；由主模型调 describe_current_frame / describe_frame。
    # 例外：用户明确「再分析」等强制词 → 本轮直接识图并注入正文。
    vision_prefetch = ""
    if _path_is_file(session.frame_path):
        vision_prefetch = _frame_vision_status(session)
        if session.probe and isinstance(session.probe, dict):
            slim = _slim_probe_for_prompt(session.probe)
            if slim:
                vision_prefetch = f"{vision_prefetch}\n\n【玩法探针】\n{slim}".strip()

    force_vision = any(
        k in (last_user or "") for k in ("再分析", "重新识图", "再看一眼", "重新看")
    )
    if force_vision and _path_is_file(session.frame_path):
        _emit("①′ 用户要求重新识图…")
        vdata = await _assist_describe_frame(
            session,
            vision_assist,
            user_question=last_user,
            force=True,
            on_status=_emit,
        )
        total_in += int(vdata.get("tokens_in") or 0)
        total_out += int(vdata.get("tokens_out") or 0)
        if vdata.get("ok") and vdata.get("text"):
            vision_prefetch = f"【辅助识图】\n{vdata['text']}"
            tag = "缓存" if vdata.get("cached") else "新识图"
            _emit(f"✓ 辅助识图（{tag}）· {str(vdata['text'])[:36]}")
        else:
            _emit(f"⚠ 辅助识图失败：{vdata.get('error') or '未知'}")
    elif (
        _path_is_file(session.frame_path)
        and _user_wants_vision(last_user)
        and not _vision_assist_ready(vision_assist)
        and not _cached_assist_vision(session)
    ):
        # 仅提示配置缺失；不自动打 API
        _emit(
            "①′ 若需看图请配置辅助识图（API 配置 → 识图提供商/Key/VL 模型）；"
            "主模型可调用 describe_current_frame"
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
    system = build_system_prompt(
        registry,
        session,
        model_name=model,
        vision_prefetch=vision_prefetch,
        runtime_block=runtime_block,
        permissions=perms,
        tools_available=supports,
    )

    def _form_messages(*, prefetch: str) -> list[dict]:
        """短历史 + 本轮状态表（发表格）。"""
        hist = session_messages_for_llm(session, limit=8)
        sheet = build_status_sheet(
            session,
            vision_prefetch=prefetch,
            runtime_block=runtime_block,
            permissions=perms,
            last_user=last_user,
        )
        return list(hist) + [{"role": "user", "content": sheet}]

    messages = _form_messages(prefetch=vision_prefetch)
    resolve = make_tool_resolver(
        registry,
        session,
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
                    tools=openai_collab_tools(),
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
                f"{vision_prefetch}\n\n【辅助识图】\n{tool_desc}".strip()
                if (vision_prefetch or "").strip()
                else tool_desc
            )

    # 由 LLM 的 turn 决定本轮是否结束。stop 不再因为口头提到点击窗而重开一轮。
    turn = normalize_turn_meta(meta, visible)
    _emit(f"→ 本轮结局 turn={turn}")

    if turn == "ask":
        # 等用户：不再自动匹配/识图，留给授权卡/chips
        meta["match_assets"] = False
        meta["describe_frame"] = False
        meta["need_vision"] = False
    elif turn == "act" and has_frame_now:
        # 本地匹配只听 LLM 显式 match_assets；未指定时默认辅助识图
        has_action = bool(
            meta.get("describe_frame")
            or meta.get("need_vision")
            or meta.get("match_assets")
        )
        if not has_action:
            meta["describe_frame"] = True
            _emit("→ turn=act，交辅助识图（不自动本地匹配）")

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
        if already and "【辅助识图】" in (vision_prefetch or "") and not force_vision:
            meta["describe_frame"] = False
            meta["need_vision"] = False
        else:
            _emit("①″ 主模型请求看图（describe_frame）…")
            vdata = await _assist_describe_frame(
                session,
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
                    f"{vision_prefetch}\n\n【辅助识图】\n{desc}".strip()
                    if (vision_prefetch or "").strip()
                    else f"【辅助识图】\n{desc}"
                )
                tag = "缓存" if vdata.get("cached") else "新识图"
                _emit(f"✓ 辅助识图（{tag}）· {desc[:36]}")
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
                _emit(f"⚠ 辅助识图失败：{vdata.get('error') or '未知'}")
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
    # 表内 focus_assets → 会话焦点
    focus = meta.get("focus_assets") or []
    if isinstance(focus, list) and focus:
        ids: list[str] = []
        for x in focus:
            s = str(x).strip()
            if not s:
                continue
            m = re.match(r"\[\[([^\]]+)\]\]", s)
            ids.append(m.group(1).strip() if m else s.strip("[]"))
        if ids:
            session.focused_asset_ids = ids[:8]
            _emit(f"→ 焦点更新 · {len(ids)} 张")

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
    if not visible:
        visible = "（模型未返回可读正文，请再试一次或换模型。）"
    meta["_trace"] = list(trace)
    _emit(f"④ 完成 · turn={meta.get('turn') or 'stop'}")
    return visible, meta, total_in, total_out
