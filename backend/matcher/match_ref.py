"""可选 match_ref.json 快路径（非强制）。

若素材目录提供 match_ref.json，则用 s = frame_w / ref_width 跳过自动定标。
不限制用户窗口比例；无此文件时走 scale_calibrate。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Union

DEFAULT_REF = (1280, 960)
DEFAULT_SCALE_BAND = (0.97, 1.0, 1.03)


@dataclass(frozen=True)
class MatchRef:
    ref_width: int
    ref_height: int
    scale_band: tuple[float, ...]
    fallback_wide_scales: bool


@dataclass(frozen=True)
class ScalePlan:
    scale: float
    ref: MatchRef
    frame_w: int
    frame_h: int
    warning: str = ""


def _as_band(raw: Any) -> tuple[float, ...]:
    if isinstance(raw, (list, tuple)) and raw:
        vals = tuple(float(x) for x in raw)
        if all(v > 0 for v in vals):
            return vals
    return DEFAULT_SCALE_BAND


def _parse_ref(data: dict[str, Any]) -> MatchRef:
    rw = int(data.get("ref_width") or DEFAULT_REF[0])
    rh = int(data.get("ref_height") or DEFAULT_REF[1])
    if rw <= 0 or rh <= 0:
        rw, rh = DEFAULT_REF
    return MatchRef(
        ref_width=rw,
        ref_height=rh,
        scale_band=_as_band(data.get("scale_band")),
        fallback_wide_scales=bool(data.get("fallback_wide_scales", True)),
    )


@lru_cache(maxsize=64)
def _load_ref_file(path_str: str) -> Optional[MatchRef]:
    path = Path(path_str)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _parse_ref(data)


def find_match_ref(template: Union[str, Path, None]) -> Optional[MatchRef]:
    """从模板路径向上查找 match_ref.json。"""
    if template is None or isinstance(template, (bytes, bytearray)):
        return None
    try:
        p = Path(str(template))
    except Exception:
        return None
    if str(p).startswith("data:image"):
        return None
    cur = p.parent if p.suffix else p
    for _ in range(5):
        candidate = cur / "match_ref.json"
        if candidate.is_file():
            ref = _load_ref_file(str(candidate.resolve()))
            if ref is not None:
                return ref
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def plan_scale(frame_w: int, frame_h: int, ref: MatchRef) -> ScalePlan:
    s = float(frame_w) / float(ref.ref_width) if ref.ref_width else 1.0
    if s <= 0:
        s = 1.0
    return ScalePlan(
        scale=s,
        ref=ref,
        frame_w=frame_w,
        frame_h=frame_h,
        warning="",
    )


def resolve_scale_for_frame(
    template: Union[str, Path, None],
    frame_w: int,
    frame_h: int,
) -> Optional[ScalePlan]:
    ref = find_match_ref(template)
    if ref is None:
        return None
    return plan_scale(frame_w, frame_h, ref)


def scales_from_plan(plan: ScalePlan) -> list[float]:
    return [plan.scale * f for f in plan.ref.scale_band if plan.scale * f > 0]
