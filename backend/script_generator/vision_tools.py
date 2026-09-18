"""协作 Agent 视觉探针（结界初试版）。

对外 tool 名（给未来 Collaborator / revise 共用）：
  - multi_count_medals
  - match_probe
  - build_jjtp_script（生成/读取可试跑脚本）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

import cv2
import numpy as np

from core.path import IMG_PATH, PROJECT_ROOT, SCRIPTS_PATH

PathLike = Union[str, Path]

CANONICAL_JJTP_SCRIPT = SCRIPTS_PATH / "yys" / "结界_打最多章.py"


def _imread(path: PathLike) -> np.ndarray:
    p = Path(path)
    data = np.fromfile(str(p), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(p)
    return img


def _imwrite(path: Path, bgr: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError(f"encode failed: {path}")
    path.write_bytes(buf.tobytes())
    return path


def frame_from_source(source: Union[PathLike, np.ndarray]) -> np.ndarray:
    if isinstance(source, np.ndarray):
        if source.ndim == 2:
            return cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
        if source.shape[2] == 4:
            return cv2.cvtColor(source, cv2.COLOR_BGRA2BGR)
        return source
    return _imread(source)


def multi_count_medals(
    source: Union[PathLike, np.ndarray],
    *,
    threshold: float = 0.75,
    scale: Optional[float] = None,
    debug_name: str = "collab_medal_debug.png",
) -> dict[str, Any]:
    """统计结界对手勋章；返回摘要 + debug 图路径。"""
    from scripts.yys.结界勋章 import count_opponent_medals, draw_debug

    frame = frame_from_source(source)
    result = count_opponent_medals(frame, scale=scale, threshold=threshold)
    dbg = draw_debug(frame, result)
    out = PROJECT_ROOT / "screenshots" / debug_name
    _imwrite(out, dbg)

    best = None
    if result.best is not None:
        b = result.best
        best = {
            "row": b.row,
            "col": b.col,
            "count": b.count,
            "click_xy": list(b.click_xy),
        }

    return {
        "ok": True,
        "tool": "multi_count_medals",
        "rows": result.rows,
        "cols": result.cols,
        "counts": [list(r) for r in result.counts],
        "hits": len(result.hits),
        "scale": result.scale,
        "threshold": result.threshold,
        "best": best,
        "debug_path": str(out),
        "summary": _human_summary(result.rows, result.cols, result.counts, best),
    }


def match_probe(
    source: Union[PathLike, np.ndarray],
    template: PathLike,
    *,
    threshold: float = 0.8,
    multi: bool = False,
    debug_name: str = "collab_match_probe.png",
) -> dict[str, Any]:
    """单模板探针：返回最佳命中（或多目标）。"""
    from backend.matcher.matcher import matcher

    frame = frame_from_source(source)
    templ = _imread(template)
    mtype = "image_multi" if multi else "image"
    raw = matcher.match(
        target=frame,
        template=templ,
        match_type=mtype,
        threshold=threshold,
        min_dist=12,
    )

    hits: list[dict[str, Any]] = []
    if multi:
        for r in raw or []:
            if isinstance(r, dict):
                hits.append(
                    {
                        "x": int(r.get("x") or 0),
                        "y": int(r.get("y") or 0),
                        "score": float(r.get("score") or 0),
                    }
                )
            else:
                hits.append(
                    {
                        "x": int(getattr(r, "x", 0) or 0),
                        "y": int(getattr(r, "y", 0) or 0),
                        "score": float(getattr(r, "score", 0) or 0),
                    }
                )
    else:
        ok = bool(raw and getattr(raw, "x", None) is not None)
        if ok:
            hits.append(
                {
                    "x": int(raw.x),
                    "y": int(raw.y),
                    "score": float(getattr(raw, "score", getattr(raw, "max_val", 0)) or 0),
                }
            )

    dbg = frame.copy()
    for h in hits:
        cv2.circle(dbg, (h["x"], h["y"]), 12, (0, 0, 255), 2)
        cv2.putText(
            dbg,
            f"{h['score']:.2f}",
            (h["x"] + 8, h["y"] - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 255),
            1,
        )
    out = PROJECT_ROOT / "screenshots" / debug_name
    _imwrite(out, dbg)

    best = max(hits, key=lambda h: h["score"]) if hits else None
    return {
        "ok": bool(hits),
        "tool": "match_probe",
        "template": str(template),
        "threshold": threshold,
        "multi": multi,
        "hits": hits,
        "best": best,
        "debug_path": str(out),
        "summary": (
            f"命中 {len(hits)} 处"
            + (f"，最佳 ({best['x']},{best['y']}) score={best['score']:.3f}" if best else "")
        ),
    }


def _human_summary(
    rows: int,
    cols: int,
    counts: list[list[int]],
    best: Optional[dict],
) -> str:
    if rows <= 0 or cols <= 0 or not counts:
        return "画面上没有识别到实心勋章（可能是空板，或需要换一张结界截图）。"
    lines = [f"识别到 {rows}×{cols} 网格，命中矩阵："]
    for r in counts:
        lines.append("  " + " ".join(str(c) for c in r))
    if best:
        lines.append(
            f"建议挑战第 {best['row'] + 1} 行第 {best['col'] + 1} 列"
            f"（{best['count']} 枚），点击约 {tuple(best['click_xy'])}。"
        )
    else:
        lines.append("没有可打的高章目标（全 0）。")
    return "\n".join(lines)


def build_jjtp_script(probe: Optional[dict[str, Any]] = None) -> str:
    """返回可试跑的结界脚本源码（以仓库 canonical 文件为准）。"""
    if not CANONICAL_JJTP_SCRIPT.is_file():
        raise FileNotFoundError(CANONICAL_JJTP_SCRIPT)
    body = CANONICAL_JJTP_SCRIPT.read_text(encoding="utf-8")
    summary = ""
    if probe:
        summary = str(probe.get("summary") or "").strip()
    if summary:
        header = (
            f"# ── 协作探针摘要（生成时）──\n"
            + "\n".join(f"# {line}" for line in summary.splitlines())
            + "\n# ────────────────────────\n\n"
        )
        # 插到 docstring 之后
        if body.startswith('"""'):
            end = body.find('"""', 3)
            if end != -1:
                end += 3
                return body[:end] + "\n\n" + header + body[end:].lstrip("\n")
        return header + body
    return body


# 兼容旧名
def build_jjtp_script_stub(probe: dict[str, Any]) -> str:
    return build_jjtp_script(probe)


def list_jjtp_assets() -> dict[str, Any]:
    """检查结界相关素材是否齐全。"""
    need = {
        "勋章": IMG_PATH / "yys" / "结界" / "勋章.png",
        "挑战": IMG_PATH / "yys" / "刷999" / "挑战.png",
        "金币": IMG_PATH / "yys" / "刷999" / "金币.png",
    }
    # 结界目录优先覆盖
    for name in ("挑战", "金币"):
        alt = IMG_PATH / "yys" / "结界" / f"{name}.png"
        if alt.is_file():
            need[name] = alt
    missing = [k for k, p in need.items() if not p.is_file()]
    return {
        "ok": not missing,
        "paths": {k: str(v) for k, v in need.items()},
        "missing": missing,
        "script": str(CANONICAL_JJTP_SCRIPT),
        "summary": (
            "素材齐全" if not missing else f"缺少素材: {', '.join(missing)}"
        ),
    }
