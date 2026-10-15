# -*- coding: utf-8 -*-
"""协作素材操作：给 Agent 真正能改素材的手。

- crop_asset：按素材 id / 路径 + 矩形裁剪，产物**自动登记进素材区**
- register_new_probe_images：把临时探针脚本这次新写出的图登记进素材区

历史问题：Agent 只能"说"要裁剪，没有任何工具能产出新素材；
探针 save_image 的产物也只落在输出目录里，用户界面上看不到变化。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT
from backend.script_generator.collaborator.canvas import PROBE_GROUP, PROBE_ROLE

COLLAB_CROP_ROOT = PROJECT_ROOT / "screenshots" / "collab_crops"
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


def _safe_name(raw: str, fallback: str = "crop") -> str:
    stem = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", str(raw or "").strip())
    stem = re.sub(r"_+", "_", stem).strip("_")
    return (stem[:40] or fallback)


def _is_image(path: Path) -> bool:
    try:
        return path.is_file() and path.suffix.lower() in _IMG_EXTS
    except Exception:
        return False


def _normalize_rect(rect: Any, w: int, h: int) -> tuple[int, int, int, int]:
    """接受归一化(<=1)或像素矩形，返回夹紧后的像素框。"""
    if isinstance(rect, dict):
        raw = [
            rect.get("x0", 0),
            rect.get("y0", 0),
            rect.get("x1", 1),
            rect.get("y1", 1),
        ]
    else:
        raw = list(rect or [0, 0, 1, 1])
    if len(raw) < 4:
        raise ValueError("rect 需要 4 个数：x0,y0,x1,y1")
    vals: list[float] = []
    for v in raw[:4]:
        try:
            vals.append(float(v))
        except Exception:
            raise ValueError(f"rect 含非数字：{v!r}")
    normalized = all(0.0 <= v <= 1.0 for v in vals)
    if normalized:
        x0, y0, x1, y1 = (
            int(round(vals[0] * w)),
            int(round(vals[1] * h)),
            int(round(vals[2] * w)),
            int(round(vals[3] * h)),
        )
    else:
        x0, y0, x1, y1 = (int(round(v)) for v in vals)
    x0, x1 = sorted((max(0, min(x0, w)), max(0, min(x1, w))))
    y0, y1 = sorted((max(0, min(y0, h)), max(0, min(y1, h))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise ValueError(f"裁剪框太小：{x1 - x0}x{y1 - y0}")
    return x0, y0, x1, y1


def crop_asset(
    session,
    *,
    asset_id: str = "",
    template_path: str = "",
    rect: Any = None,
    name: str = "",
    focus: bool = True,
) -> dict[str, Any]:
    """裁剪会话素材（或任意图片）并登记为新素材。"""
    from backend.script_generator.collaborator.canvas import (
        add_asset_from_path,
        asset_display_name,
        resolve_template_path,
        set_focused,
    )
    # 中文路径必须走 np.fromfile/cv2.imdecode + cv2.imencode/write_bytes：
    # cv2.imread/imwrite 在 Windows 上按 ANSI 处理路径，中文名会被写成乱码文件名，
    # 表现就是"裁剪成功但素材文件不存在"，然后模型反复重裁。
    from backend.script_generator.vision_tools import _imread, _imwrite

    src, asset, err = resolve_template_path(
        session, asset_id=str(asset_id or ""), template_path=str(template_path or "")
    )
    if err or src is None:
        return {"ok": False, "error": err or "找不到要裁剪的素材"}
    try:
        img = _imread(src)
    except Exception as e:
        return {"ok": False, "error": f"读图失败：{src}（{e}）"}
    h, w = img.shape[:2]
    try:
        x0, y0, x1, y1 = _normalize_rect(rect, w, h)
    except ValueError as e:
        return {"ok": False, "error": str(e), "source_size": [w, h]}
    crop = img[y0:y1, x0:x1]
    if crop is None or crop.size == 0:
        return {"ok": False, "error": "裁剪结果为空", "source_size": [w, h]}

    sid = str(getattr(session, "session_id", "") or "session")
    out_dir = COLLAB_CROP_ROOT / _safe_name(sid, "session")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _safe_name(name or f"{src.stem}_裁剪", fallback=f"{src.stem}_crop")
    out = out_dir / f"{base}.png"
    # 同名即"重裁"：覆盖同一个文件，素材 id 不变，避免素材区里堆出
    # 「对手格子底部.png / 对手格子底部_2.png」这种同名不同内容的分裂状态。
    try:
        _imwrite(out, crop)
    except Exception as e:
        return {"ok": False, "error": f"写入失败：{out}（{e}）"}

    item = add_asset_from_path(
        session,
        out,
        role="template",
        caption=f"裁剪自 {asset_display_name(asset) if asset else src.name}",
        group="裁剪",
    )
    from backend.script_generator.collaborator.resource_registry import register_asset

    register_asset(
        session,
        item,
        status="candidate",
        derived_from=[str(asset.get("id"))] if asset and asset.get("id") else [],
        provenance={
            "source": "crop_asset",
            "source_path": str(src),
            "source_asset_id": str(asset.get("id") or "") if asset else "",
            "rect": [x0, y0, x1, y1],
        },
    )
    if focus and item.get("id"):
        set_focused(session, [str(item["id"])])
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    session.artifacts["canvas_dirty"] = True
    session.artifacts["last_crop"] = {
        "asset_id": item.get("id"),
        "path": str(out),
        "rect": [x0, y0, x1, y1],
    }
    return {
        "ok": True,
        "asset_id": item.get("id"),
        "name": item.get("name") or out.name,
        "path": str(out),
        "source": str(src),
        "source_size": [w, h],
        "rect": [x0, y0, x1, y1],
        "size": [x1 - x0, y1 - y0],
        "note": "裁剪结果已加入素材区（可继续 match_template 验证）",
    }


def probe_out_dir(session) -> Path:
    from backend.script_generator.collaborator.probe_script import PROBE_OUT_ROOT

    sid = str(getattr(session, "session_id", "") or "probe")
    # 与 probe_script._build_namespace 保持一致（session_id 本身已是安全文件名）
    return PROBE_OUT_ROOT / sid


def snapshot_dir_files(directory: Path) -> set[str]:
    """记录目录里现有图片，用于识别本次新产出的文件。"""
    out: set[str] = set()
    try:
        if Path(directory).is_dir():
            for p in Path(directory).rglob("*"):
                if _is_image(p):
                    out.add(str(p.resolve()))
    except Exception:
        pass
    return out


def register_new_probe_images(
    session,
    *,
    before: Optional[set[str]] = None,
    directory: Optional[Path] = None,
    focus: bool = True,
    role: str = PROBE_ROLE,
) -> list[dict[str, Any]]:
    """把探针本次新写出的图片登记进素材区，返回新素材列表。

    默认 ``role=PROBE_ROLE``：探针产出图（标注 / 拼版预览 / 自检图）是**产出物**，
    不是游戏界面本身，因此不算场景素材——否则会被当成"待识别的界面内容"反复
    送识图，形成"拿自己画出来的图再去识图"的自反馈闭环（实况会话里发生过）。
    显式引用（``match_template(asset_id=…)`` / ``crop_asset``）仍然可用。

    传 ``role="annotation"`` 表示模型自己画出来的标注图（``mark_on_image`` 用）。
    """
    from backend.script_generator.collaborator.canvas import (
        add_asset_from_path,
        set_focused,
    )

    root = Path(directory) if directory is not None else probe_out_dir(session)
    seen = before if isinstance(before, set) else snapshot_dir_files(root)
    fresh: list[Path] = []
    try:
        if root.is_dir():
            for p in sorted(root.rglob("*"), key=lambda x: x.stat().st_mtime):
                if _is_image(p) and str(p.resolve()) not in seen:
                    fresh.append(p)
    except Exception:
        return []
    if not fresh:
        return []
    added: list[dict[str, Any]] = []
    for p in fresh:
        item = add_asset_from_path(
            session,
            p,
            role=str(role or PROBE_ROLE),
            caption="探针产出",
            group=PROBE_GROUP,
        )
        added.append(
            {
                "asset_id": item.get("id"),
                "name": item.get("name") or p.name,
                "path": str(p),
                "role": item.get("role"),
            }
        )
    # 标注图不再抢焦点：焦点是给"待识别/待匹配的素材"用的，标注图是给人看的
    if focus and added and str(role) not in ("annotation", PROBE_ROLE):
        ids = [str(a["asset_id"]) for a in added if a.get("asset_id")]
        if ids:
            set_focused(session, ids)
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    session.artifacts["canvas_dirty"] = True
    return added


def register_result_images(
    session,
    result: Any,
    *,
    role: str = PROBE_ROLE,
) -> list[dict[str, Any]]:
    """Register image paths explicitly returned by a tool, regardless of directory.

    Temporary scripts do not always write into ``probe_out_dir``.  The result
    contract is therefore also an authoritative publication channel.  Only
    output-like keys are considered, and paths already present in the asset
    rail are ignored.
    """
    from backend.script_generator.collaborator.canvas import add_asset_from_path
    from backend.script_generator.collaborator.resource_registry import register_asset

    output_keys = {
        "output_path",
        "image_path",
        "saved_path",
        "preview_path",
        "debug_path",
        "annotated_path",
    }
    candidates: list[Path] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key).casefold())
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, key)
        elif isinstance(value, (str, Path)) and key in output_keys:
            path = Path(value)
            if _is_image(path):
                candidates.append(path)

    walk(result)
    existing = {
        str(Path(str(item.get("path") or "")).resolve()).casefold()
        for item in (getattr(session, "assets", None) or [])
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    added: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in candidates:
        resolved = str(path.resolve())
        identity = resolved.casefold()
        if identity in existing or identity in seen:
            continue
        seen.add(identity)
        item = add_asset_from_path(
            session,
            path,
            role=str(role or PROBE_ROLE),
            caption="工具产出",
            group=PROBE_GROUP,
        )
        register_asset(
            session,
            item,
            status="candidate",
            provenance={"source": "tool_result", "output_path": resolved},
        )
        added.append(
            {
                "asset_id": item.get("id"),
                "name": item.get("name") or path.name,
                "path": resolved,
                "role": item.get("role"),
            }
        )
    if added:
        if not isinstance(getattr(session, "artifacts", None), dict):
            session.artifacts = {}
        session.artifacts["canvas_dirty"] = True
    return added
