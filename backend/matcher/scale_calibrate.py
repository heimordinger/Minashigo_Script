"""模板尺度自动定标与缓存。

不依赖固定分辨率 / 宽高比：按 (template_key, frame_w×frame_h, capture_mode)
缓存最佳尺度 s*；未命中时对数粗搜 + 峰值附近细搜。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Optional, Union

import cv2
import numpy as np

# 连续未达阈值次数达到此值则作废缓存并重定标
MISS_RECALIBRATE_AFTER = 3

NARROW_BAND = (0.97, 1.0, 1.03)
COARSE_MIN = 0.30
COARSE_MAX = 1.80
COARSE_STEPS = 16
FINE_SPAN = 0.10
FINE_STEPS = 11


@dataclass
class ScaleEntry:
    scale: float
    score: float
    misses: int = 0


def cache_key(
    template_key: str,
    frame_w: int,
    frame_h: int,
    capture_mode: str = "full",
) -> str:
    return f"{template_key}|{int(frame_w)}x{int(frame_h)}|{capture_mode or 'full'}"


def narrow_scales(scale: float, band: tuple[float, ...] = NARROW_BAND) -> list[float]:
    s = float(scale)
    if s <= 0:
        return [1.0]
    return [s * f for f in band if s * f > 0]


def _logspace(lo: float, hi: float, n: int) -> list[float]:
    if n <= 1:
        return [float(lo)]
    lo = max(1e-3, float(lo))
    hi = max(lo, float(hi))
    return [float(math.exp(math.log(lo) + i * (math.log(hi) - math.log(lo)) / (n - 1))) for i in range(n)]


def _peak_at_scale(frame_gray: np.ndarray, templ_gray: np.ndarray, scale: float) -> float:
    th, tw = templ_gray.shape[:2]
    nh = max(1, int(round(th * scale)))
    nw = max(1, int(round(tw * scale)))
    fh, fw = frame_gray.shape[:2]
    if nh >= fh or nw >= fw or nh < 4 or nw < 4:
        return -1.0
    resized = cv2.resize(templ_gray, (nw, nh), interpolation=cv2.INTER_AREA)
    res = cv2.matchTemplate(frame_gray, resized, cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(res)[1])


def calibrate_scale(
    frame_bgr: np.ndarray,
    templ_bgr: np.ndarray,
    *,
    s_min: float = COARSE_MIN,
    s_max: float = COARSE_MAX,
    coarse_steps: int = COARSE_STEPS,
    fine_span: float = FINE_SPAN,
    fine_steps: int = FINE_STEPS,
) -> tuple[Optional[float], float]:
    """粗搜 + 细搜，返回 (best_scale, best_score)。失败返回 (None, 0)。"""
    if frame_bgr is None or templ_bgr is None:
        return None, 0.0
    frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    templ_gray = cv2.cvtColor(templ_bgr, cv2.COLOR_BGR2GRAY)

    best_s: Optional[float] = None
    best_score = -1.0
    for s in _logspace(s_min, s_max, coarse_steps):
        sc = _peak_at_scale(frame_gray, templ_gray, s)
        if sc > best_score:
            best_score = sc
            best_s = s

    if best_s is None or best_score < 0:
        return None, 0.0

    lo = max(s_min, best_s * (1.0 - fine_span))
    hi = min(s_max, best_s * (1.0 + fine_span))
    for s in _logspace(lo, hi, fine_steps):
        sc = _peak_at_scale(frame_gray, templ_gray, s)
        if sc > best_score:
            best_score = sc
            best_s = s

    return best_s, float(best_score)


class ScaleCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, ScaleEntry] = {}

    def get(self, key: str) -> Optional[ScaleEntry]:
        with self._lock:
            return self._entries.get(key)

    def set(self, key: str, scale: float, score: float) -> None:
        with self._lock:
            self._entries[key] = ScaleEntry(scale=float(scale), score=float(score), misses=0)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def note_miss(self, key: str) -> bool:
        """记录一次未达阈值；若应重定标则清缓存并返回 True。"""
        with self._lock:
            ent = self._entries.get(key)
            if ent is None:
                return True
            ent.misses += 1
            if ent.misses >= MISS_RECALIBRATE_AFTER:
                self._entries.pop(key, None)
                return True
            return False

    def note_hit(self, key: str) -> None:
        with self._lock:
            ent = self._entries.get(key)
            if ent is not None:
                ent.misses = 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_frame_size(self, frame_w: int, frame_h: int) -> None:
        token = f"|{int(frame_w)}x{int(frame_h)}|"
        with self._lock:
            drop = [k for k in self._entries if token in k]
            for k in drop:
                del self._entries[k]


_CACHE = ScaleCache()


def get_scale_cache() -> ScaleCache:
    return _CACHE


def resolve_scale_scales(
    *,
    matcher,
    frame,
    template: Union[str, object],
    template_key: str,
    frame_w: int,
    frame_h: int,
    capture_mode: str = "full",
    force_recalibrate: bool = False,
) -> tuple[Optional[float], Optional[list[float]], str]:
    """
    解析本帧应使用的尺度。
    返回 (scale_hint, scales, source)；source ∈ match_ref|cache|calibrate|wide。
    """
    from backend.matcher.match_ref import resolve_scale_for_frame, scales_from_plan

    # 可选快路径：素材目录提供了 match_ref（强制重定标时跳过）
    if not force_recalibrate:
        plan = resolve_scale_for_frame(template, frame_w, frame_h)
        if plan is not None:
            return plan.scale, scales_from_plan(plan), "match_ref"

    key = cache_key(template_key, frame_w, frame_h, capture_mode)
    cache = get_scale_cache()

    if not force_recalibrate:
        ent = cache.get(key)
        if ent is not None and ent.scale > 0:
            return ent.scale, narrow_scales(ent.scale), "cache"

    templ, _ = matcher._load_template_cached(template)
    best_s, best_sc = calibrate_scale(frame, templ)
    if best_s is None or best_sc < 0.25:
        return None, None, "wide"

    cache.set(key, best_s, best_sc)
    print(
        f"[scale_calibrate] {template_key} @ {frame_w}x{frame_h} "
        f"s={best_s:.4f} score={best_sc:.4f}"
    )
    return best_s, narrow_scales(best_s), "calibrate"
