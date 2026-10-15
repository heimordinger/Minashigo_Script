# -*- coding: utf-8 -*-
"""协作确认后的出码桥：LogicGraph / 生成用介绍 + 会话素材 → 经典 v1 codegen。

分工（design_collaborative_v2 §5.2）：
  - 协作侧负责谈拢逻辑、攒素材、写「生成用介绍」；
  - 出码复用经典 generate 管线（plan → architecture → checklist → few-shot
    → validate/fix），不再让模型自由写整份脚本。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT

# 会话素材落到 assets/images/_collab/<session_id>/，作为经典生成的 source_dir
COLLAB_IMG_ROOT = PROJECT_ROOT / "assets" / "images" / "_collab"
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


def _safe_stem(raw: str, fallback: str = "img") -> str:
    stem = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", str(raw or "").strip())
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem[:40] or fallback


def session_image_dir(session) -> Path:
    """本会话的图片目录（经典生成的 source_dir）。"""
    try:
        sid = session.ensure_id()
    except Exception:
        sid = str(getattr(session, "session_id", "") or "")
    return COLLAB_IMG_ROOT / _safe_stem(sid, "session")


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.resolve() == b.resolve():
            return True
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except Exception:
        return False


def materialize_images(session) -> tuple[Path, list[dict[str, Any]]]:
    """把会话素材拷进会话图片目录（可读文件名），返回 (目录, 素材表)。

    素材表每项: {id, name, stem, file}。整帧截图（role=frame）不作为模板拷入。
    """
    out_dir = session_image_dir(session)
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_src: set[str] = set()
    items: list[dict[str, Any]] = []
    for a in list(getattr(session, "assets", None) or []):
        if not isinstance(a, dict):
            continue
        if str(a.get("role") or "template").strip().lower() == "frame":
            continue
        src = Path(str(a.get("path") or ""))
        if not src.is_file() or src.suffix.lower() not in _IMG_EXTS:
            continue
        key = str(src.resolve())
        if key in seen_src:
            continue
        seen_src.add(key)
        stem = _safe_stem(str(a.get("name") or src.stem), fallback=_safe_stem(src.stem))
        target = out_dir / f"{stem}{src.suffix.lower()}"
        n = 2
        while target.exists() and not _same_file(target, src):
            target = out_dir / f"{stem}_{n}{src.suffix.lower()}"
            n += 1
        if not _same_file(target, src):
            try:
                shutil.copy2(src, target)
            except Exception:
                continue
        items.append(
            {
                "id": str(a.get("id") or ""),
                "name": str(a.get("name") or src.stem),
                "stem": target.stem,
                "file": str(target),
            }
        )
    return out_dir, items


def _stem_map(items: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        for key in (it.get("id"), it.get("name")):
            k = str(key or "").strip()
            if k:
                out[k] = str(it.get("stem") or "")
    return out


def build_fallback_intro(session, items: list[dict[str, Any]]) -> str:
    """模型没给「生成用介绍」时的兜底：把 LogicGraph 摊成经典介绍格式。"""
    logic = getattr(session, "logic", None)
    steps = list(getattr(logic, "steps", None) or [])
    title = str(getattr(logic, "title", "") or "").strip() or "协作脚本"
    goal = str(getattr(session, "goal_text", "") or "").strip()
    # 旧会话里确认话术可能已经覆盖过 goal_text；出码管线的「目的」不能是
    # 「确认，按此生成脚本」这种流程话，退回逻辑标题。
    if goal:
        try:
            from backend.script_generator.collaborator.engine import CollaboratorEngine

            if CollaboratorEngine._is_meta_question(goal):
                goal = ""
        except Exception:
            pass
    arch = str(getattr(logic, "architecture", "") or "scene_driven").strip()
    stems = _stem_map(items)

    def _imgs(step) -> list[str]:
        out: list[str] = []
        for raw in list(getattr(getattr(step, "bind", None), "images", None) or []):
            k = str(raw or "").strip().strip("[]")
            s = stems.get(k) or stems.get(str(raw or "").strip())
            if s and s not in out:
                out.append(s)
        return out

    lines = [f"# {title}", "", f"架构：{arch}", "", "目的：", goal or title, ""]

    lines.append("图片说明：")
    if items:
        for it in items:
            lines.append(f"- `{it['stem']}.png`：{it.get('name') or it['stem']}")
    else:
        lines.append("- （会话素材区为空）")
    lines.append("")

    lines.append("任务流程：")
    for i, step in enumerate(steps, 1):
        label = str(getattr(step, "label", "") or "").strip() or f"步骤 {i}"
        lines.append(f"（{i}）{label}")
        when = str(getattr(step, "when", "") or "").strip()
        detail = label if not when else f"{label}（{when}）"
        imgs = _imgs(step)
        if imgs:
            detail += "；素材：" + "、".join(f"`{s}.png`" for s in imgs)
        lines.append(f"1. {detail}")
    if not steps:
        lines.append("（1）按目的直接执行")
        lines.append("1. 按会话里确认过的顺序执行")

    return "\n".join(lines).strip() + "\n"


def script_name_for(session) -> str:
    """脚本名：优先模型给的，其次按目标 / 逻辑标题推。"""
    explicit = str(getattr(session, "script_name", "") or "").strip()
    if explicit:
        return explicit if explicit.lower().endswith(".py") else f"{explicit}.py"
    logic = getattr(session, "logic", None)
    base = (
        str(getattr(session, "goal_text", "") or "").strip()
        or str(getattr(logic, "title", "") or "").strip()
        or "collab_script"
    )
    name = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", base)
    name = re.sub(r"_+", "_", name).strip("_")[:24] or "collab_script"
    return f"{name}.py"


def build_codegen_params(
    *,
    session,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    max_tokens: Optional[int] = None,
    vision_assist: Optional[dict] = None,
    free_mode: bool = False,
    send_images: Optional[bool] = None,
) -> dict[str, Any]:
    """组装经典 generate_script 参数（含 source_dir / 介绍 / 识图取舍）。"""
    from backend.script_generator.collaborator.llm_turn import _looks_like_text_only_model

    img_dir, items = materialize_images(session)
    intro = str(getattr(session, "intro_text", "") or "").strip()
    if not intro:
        intro = build_fallback_intro(session, items)
    if send_images is None:
        # 主模型看不了图 → 不直接传图；配了识图就转文字目录
        send_images = not _looks_like_text_only_model(model)
    va = None
    if not send_images and isinstance(vision_assist, dict) and vision_assist.get("api_key"):
        va = dict(vision_assist)
    return {
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "api_endpoint": api_endpoint,
        "explanation_text": intro,
        "image_paths": [Path(str(it["file"])) for it in items],
        "source_dir": str(img_dir) if items else "",
        "send_images": bool(send_images),
        "compress_images": False,
        "vision_assist": va,
        "max_tokens": max_tokens,
        "free_mode": bool(free_mode),
        # 协作/线性脚本不走状态机：交付闸（collab_script_issues）也是 require_fsm=False。
        # 按 FSM 判会把好脚本误杀（实况 10/10 出码失败：unknown_state / 空壳 handler）。
        "require_fsm": False,
    }


async def generate_from_logic(
    *,
    session,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    max_tokens: Optional[int] = None,
    vision_assist: Optional[dict] = None,
    free_mode: bool = False,
    on_status=None,
    on_partial=None,
    on_artifact=None,
) -> dict[str, Any]:
    """跑一遍经典生成管线。返回 dict（ok / code / intro / error …）。"""
    from backend.script_generator.agent import generate_script

    img_dir, items = materialize_images(session)
    params = build_codegen_params(
        session=session,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        max_tokens=max_tokens,
        vision_assist=vision_assist,
        free_mode=free_mode,
    )
    base = {
        "ok": False,
        "code": "",
        "intro": params["explanation_text"],
        "source_dir": params["source_dir"],
        "images": items,
        "script_name": script_name_for(session),
        "tokens": (0, 0),
        "error": "",
    }
    try:
        code, inp, out = await generate_script(
            **params,
            on_partial=on_partial,
            on_status=on_status,
            on_artifact=on_artifact,
        )
    except Exception as e:  # noqa: BLE001 - 失败要原样报给协作页
        return {**base, "error": f"{type(e).__name__}: {e}"}
    code = (code or "").strip()
    if not code:
        return {**base, "error": "经典生成器没有返回脚本"}
    return {
        **base,
        "ok": True,
        "code": code,
        "tokens": (int(inp or 0), int(out or 0)),
    }
