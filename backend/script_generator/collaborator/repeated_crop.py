# -*- coding: utf-8 -*-
"""重复 UI 区域拟合：视觉模型认对象，本地 CV 精确下刀。"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from backend.script_generator.collaborator.asset_ops import COLLAB_CROP_ROOT, _safe_name
from backend.script_generator.vision_tools import _imread, _imwrite


def _iou(a: list[int], b: list[int]) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    aa = max(1, a[2] - a[0]) * max(1, a[3] - a[1])
    bb = max(1, b[2] - b[0]) * max(1, b[3] - b[1])
    return inter / float(aa + bb - inter)


def _rect_candidates(image: np.ndarray) -> list[dict[str, Any]]:
    """多阈值找矩形候选；保留轮廓弱但尺寸重复的 UI 外框。"""
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    found: list[dict[str, Any]] = []
    for low, high in ((24, 72), (40, 120), (64, 180)):
        edge = cv2.Canny(gray, low, high)
        edge = cv2.morphologyEx(
            edge,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)),
        )
        contours, _ = cv2.findContours(edge, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, rw, rh = cv2.boundingRect(contour)
            if not (0.10 * w <= rw <= 0.75 * w and 0.045 * h <= rh <= 0.45 * h):
                continue
            if rw * rh < 0.008 * w * h:
                continue
            rect = [int(x), int(y), int(x + rw), int(y + rh)]
            perimeter = max(1.0, 2.0 * (rw + rh))
            support = min(1.0, float(cv2.arcLength(contour, True)) / perimeter)
            found.append({"rect": rect, "w": rw, "h": rh, "support": support})

    # 同一外框会被多个阈值重复发现；相近嵌套框优先保留面积较大的外框。
    found.sort(key=lambda c: ((c["w"] * c["h"]), c["support"]), reverse=True)
    kept: list[dict[str, Any]] = []
    for cand in found:
        r = cand["rect"]
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        duplicate = False
        for old in kept:
            q = old["rect"]
            qx, qy = (q[0] + q[2]) / 2, (q[1] + q[3]) / 2
            size_close = abs(cand["w"] - old["w"]) <= 0.18 * old["w"] and abs(
                cand["h"] - old["h"]
            ) <= 0.22 * old["h"]
            center_close = abs(cx - qx) <= 0.08 * max(cand["w"], old["w"]) and abs(
                cy - qy
            ) <= 0.10 * max(cand["h"], old["h"])
            if _iou(r, q) >= 0.72 or (size_close and center_close):
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)
    return kept[:180]


def _best_repeat_group(candidates: list[dict[str, Any]], expected_count: int) -> list[dict[str, Any]]:
    if not candidates:
        return []
    best: tuple[float, list[dict[str, Any]]] = (-1e9, [])
    for seed in candidates:
        sw, sh = seed["w"], seed["h"]
        group = [
            c
            for c in candidates
            if abs(c["w"] - sw) <= max(5, 0.12 * sw)
            and abs(c["h"] - sh) <= max(5, 0.15 * sh)
        ]
        group.sort(key=lambda c: (c["rect"][1], c["rect"][0]))
        # 同一位置只留一个；候选必须彼此基本不重叠。
        selected: list[dict[str, Any]] = []
        for cand in sorted(group, key=lambda c: c["w"] * c["h"], reverse=True):
            if all(_iou(cand["rect"], old["rect"]) < 0.25 for old in selected):
                selected.append(cand)
        if expected_count > 0 and len(selected) > expected_count:
            selected = sorted(selected, key=lambda c: c["support"], reverse=True)[:expected_count]
        if len(selected) < 2:
            continue
        ws = np.array([c["w"] for c in selected], dtype=float)
        hs = np.array([c["h"] for c in selected], dtype=float)
        regularity = 1.0 - min(1.0, float(ws.std() / max(1.0, ws.mean()) + hs.std() / max(1.0, hs.mean())))
        count_score = 1.0 if expected_count <= 0 else 1.0 - min(
            1.0, abs(len(selected) - expected_count) / max(1, expected_count)
        )
        area = float(np.median(ws * hs))
        score = 5.0 * count_score + 2.0 * regularity + math.log1p(area) / 10.0
        if score > best[0]:
            best = (score, selected)
    return sorted(best[1], key=lambda c: (c["rect"][1], c["rect"][0]))


def fit_repeated_regions(
    session,
    *,
    grounding_ids: list[str] | None = None,
    expected_count: int = 0,
    name_prefix: str = "重复区域",
) -> dict[str, Any]:
    """拟合重复矩形并生成预览；不创建最终模板。"""
    artifacts = session.artifacts if isinstance(getattr(session, "artifacts", None), dict) else {}
    session.artifacts = artifacts
    groundings = artifacts.get("vision_groundings") or []
    wanted = {str(x) for x in (grounding_ids or []) if str(x)}
    picked = [g for g in groundings if isinstance(g, dict) and (not wanted or str(g.get("id")) in wanted)]
    sources = {str(g.get("source_path") or "") for g in picked if g.get("source_path")}
    source = Path(next(iter(sources))) if len(sources) == 1 else Path(str(getattr(session, "frame_path", "") or ""))
    if not source.is_file():
        return {"ok": False, "error": "找不到共同视口底图或视觉定位来源"}
    try:
        image = _imread(source)
    except Exception as e:
        return {"ok": False, "error": f"读图失败：{e}"}
    h, w = image.shape[:2]
    expected = max(0, min(80, int(expected_count or len(picked) or 0)))
    candidates = _rect_candidates(image)
    chosen = _best_repeat_group(candidates, expected)
    if expected and len(chosen) != expected:
        return {
            "ok": False,
            "error": f"只稳定找到 {len(chosen)} 个重复外框，期望 {expected} 个；未提交裁剪",
            "candidate_count": len(candidates),
        }
    if len(chosen) < 2:
        return {"ok": False, "error": "没有找到足够稳定的重复矩形；未提交裁剪"}

    rects = [c["rect"] for c in chosen]
    ws = np.array([r[2] - r[0] for r in rects], dtype=float)
    hs = np.array([r[3] - r[1] for r in rects], dtype=float)
    size_cv = float(ws.std() / max(1.0, ws.mean()) + hs.std() / max(1.0, hs.mean()))
    confidence = max(0.0, min(1.0, 1.0 - 2.5 * size_cv))
    if confidence < 0.72:
        return {"ok": False, "error": "候选尺寸一致性不足；未提交裁剪", "confidence": confidence}

    raw = f"{source}|{source.stat().st_mtime_ns}|{rects}"
    fit_id = "repeat_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    preview = image.copy()
    for i, (x0, y0, x1, y1) in enumerate(rects, 1):
        cv2.rectangle(preview, (x0, y0), (x1 - 1, y1 - 1), (40, 210, 255), 2)
        cv2.putText(preview, str(i), (x0 + 6, y0 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 30, 220), 2)
    out_dir = COLLAB_CROP_ROOT / _safe_name(str(getattr(session, "session_id", "session")), "session")
    out_dir.mkdir(parents=True, exist_ok=True)
    preview_path = out_dir / "repeated_fit_preview.png"
    _imwrite(preview_path, preview)
    from backend.script_generator.collaborator.canvas import add_asset_from_path

    preview_asset = add_asset_from_path(
        session, preview_path, role="screenshot", caption="重复区域拟合预览（尚未提交）", group="拟合预览"
    )
    fits = artifacts.setdefault("repeated_region_fits", {})
    fits[fit_id] = {
        "source_path": str(source),
        "source_size": [w, h],
        "rects": rects,
        "expected_count": expected,
        "confidence": confidence,
        "preview_asset_id": preview_asset.get("id"),
        "name_prefix": _safe_name(name_prefix, "重复区域"),
        "committed": False,
    }
    artifacts["canvas_dirty"] = True
    return {
        "ok": True,
        "fit_id": fit_id,
        "source_size": [w, h],
        "rects": rects,
        "count": len(rects),
        "median_size": [int(round(float(np.median(ws)))), int(round(float(np.median(hs))))],
        "confidence": round(confidence, 4),
        "preview_asset_id": preview_asset.get("id"),
        "note": "仅生成预览；请 inspect_asset 验证预览后再 commit_repeated_regions",
    }


def commit_repeated_regions(session, *, fit_id: str) -> dict[str, Any]:
    """仅在拟合预览被视觉验证通过后，原子式登记整批裁剪。"""
    artifacts = session.artifacts if isinstance(getattr(session, "artifacts", None), dict) else {}
    fit = (artifacts.get("repeated_region_fits") or {}).get(str(fit_id or ""))
    if not isinstance(fit, dict):
        return {"ok": False, "error": "未知或已失效的 fit_id"}
    if fit.get("committed"):
        return {"ok": True, "already_committed": True, "asset_ids": fit.get("asset_ids") or []}
    preview_id = str(fit.get("preview_asset_id") or "")
    inspected = artifacts.get("last_asset_inspect") or {}
    if str(inspected.get("asset_id") or "") != preview_id or inspected.get("verified") is not True:
        return {"ok": False, "error": "拟合预览尚未通过 inspect_asset(mode=verify)，不能批量提交"}
    source = Path(str(fit.get("source_path") or ""))
    if not source.is_file():
        return {"ok": False, "error": "拟合来源图已不存在"}
    from backend.script_generator.collaborator.asset_ops import crop_asset
    from backend.script_generator.collaborator.canvas import remove_asset, set_focused

    added: list[dict[str, Any]] = []
    prefix = str(fit.get("name_prefix") or "重复区域")
    for i, rect in enumerate(fit.get("rects") or [], 1):
        item = crop_asset(
            session, template_path=str(source), rect=rect, name=f"{prefix}{i}", focus=False
        )
        if not item.get("ok"):
            for prior in added:
                remove_asset(session, str(prior.get("asset_id") or ""))
                try:
                    Path(str(prior.get("path") or "")).unlink(missing_ok=True)
                except Exception:
                    pass
            return {"ok": False, "error": f"第 {i} 张写入失败，已回滚：{item.get('error')}"}
        added.append(item)
    ids = [str(x.get("asset_id")) for x in added if x.get("asset_id")]
    if ids:
        set_focused(session, ids[:2])
    fit["committed"] = True
    fit["asset_ids"] = ids
    return {"ok": True, "count": len(ids), "asset_ids": ids, "paths": [x.get("path") for x in added]}
