"""基于历史命中位置的自适应 ROI 搜索。

流程：热区 ROI 先搜 → 未命中再全图 → 成功命中写入历史（仅成功）。
按「模板 + 帧模式 + 分辨率」分桶；ROI 连续失效会自动清空该桶，避免学歪。
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from core.path import IMG_PATH, USER_DATA_PATH

# ── 可调参数 ───────────────────────────────────────────────
HOTSPOT_ENABLED_DEFAULT = True
MIN_SAMPLES = 3
MAX_SAMPLES = 48
# ROI 相对帧的最小边距；再与样本标准差放大取 max
MARGIN_FRAC = 0.10
MIN_PAD_PX = 64
STD_K = 3.0
# ROI 面积占整帧过大则无收益，直接全图
MAX_ROI_AREA_FRAC = 0.72
# ROI 未中但全图中：累计过多则清空该桶
ROI_MISS_CLEAR_AFTER = 6
ROI_MISS_RATIO = 0.45
SAVE_DEBOUNCE_S = 2.0

HOTSPOT_STORE_PATH = USER_DATA_PATH / "match_hotspots.json"

RoiBox = Tuple[int, int, int, int]  # x1,y1,x2,y2 inclusive-exclusive style for matcher


def normalize_template_key(img_path: str | Path) -> str:
    """稳定模板键：优先相对 assets/images。"""
    raw = str(img_path or "").strip()
    if not raw or raw.startswith("data:image"):
        return raw[:120] if raw else ""
    p = Path(raw)
    try:
        rel = p.resolve().relative_to(Path(IMG_PATH).resolve())
        return str(rel).replace("\\", "/").lower()
    except Exception:
        return p.name.replace("\\", "/").lower()


def frame_bucket_key(
    frame_w: int,
    frame_h: int,
    *,
    capture_mode: str | None = None,
) -> str:
    mode = (capture_mode or "full").strip().lower() or "full"
    if mode in ("viewport_crop", "canvas_buffer", "game"):
        mode = "game"
    else:
        mode = "full"
    return f"{mode}:{int(frame_w)}x{int(frame_h)}"


def make_store_key(template_key: str, frame_key: str) -> str:
    return f"{template_key}||{frame_key}"


@dataclass
class _Sample:
    x: float
    y: float
    score: float
    ts: float


@dataclass
class _Bucket:
    samples: list[_Sample] = field(default_factory=list)
    roi_hits: int = 0
    roi_misses: int = 0  # ROI 空、全图却命中
    full_hits: int = 0

    def to_dict(self) -> dict:
        return {
            "samples": [
                {"x": s.x, "y": s.y, "score": s.score, "ts": s.ts}
                for s in self.samples[-MAX_SAMPLES:]
            ],
            "roi_hits": self.roi_hits,
            "roi_misses": self.roi_misses,
            "full_hits": self.full_hits,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_Bucket":
        b = cls(
            roi_hits=int(d.get("roi_hits") or 0),
            roi_misses=int(d.get("roi_misses") or 0),
            full_hits=int(d.get("full_hits") or 0),
        )
        for s in d.get("samples") or []:
            try:
                b.samples.append(
                    _Sample(
                        x=float(s["x"]),
                        y=float(s["y"]),
                        score=float(s.get("score") or 0.0),
                        ts=float(s.get("ts") or 0.0),
                    )
                )
            except Exception:
                continue
        if len(b.samples) > MAX_SAMPLES:
            b.samples = b.samples[-MAX_SAMPLES:]
        return b


class HotspotStore:
    """进程内单例友好的热点库（线程安全）。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or HOTSPOT_STORE_PATH)
        self._lock = threading.RLock()
        self._buckets: dict[str, _Bucket] = {}
        self._dirty = False
        self._last_save = 0.0
        self.enabled = HOTSPOT_ENABLED_DEFAULT
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        buckets = data.get("buckets") if isinstance(data, dict) else None
        if not isinstance(buckets, dict):
            return
        with self._lock:
            for k, v in buckets.items():
                if isinstance(v, dict):
                    self._buckets[str(k)] = _Bucket.from_dict(v)

    def flush(self, *, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            now = time.time()
            if not force and (now - self._last_save) < SAVE_DEBOUNCE_S:
                return
            payload = {
                "version": 1,
                "updated_at": now,
                "buckets": {k: b.to_dict() for k, b in self._buckets.items()},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.path)
            self._dirty = False
            self._last_save = now

    def _bucket(self, key: str) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket()
            self._buckets[key] = b
        return b

    def record_hit(
        self,
        *,
        template_key: str,
        frame_key: str,
        x: float,
        y: float,
        score: float,
        via_roi: bool,
    ) -> None:
        if not self.enabled or not template_key:
            return
        if x is None or y is None:
            return
        key = make_store_key(template_key, frame_key)
        with self._lock:
            b = self._bucket(key)
            b.samples.append(
                _Sample(x=float(x), y=float(y), score=float(score), ts=time.time())
            )
            if len(b.samples) > MAX_SAMPLES:
                b.samples = b.samples[-MAX_SAMPLES:]
            if via_roi:
                b.roi_hits += 1
            else:
                b.full_hits += 1
            self._dirty = True
        self.flush()

    def note_roi_miss_then_full(
        self,
        *,
        template_key: str,
        frame_key: str,
    ) -> None:
        """ROI 未命中但全图命中：可能布局变了。"""
        if not self.enabled or not template_key:
            return
        key = make_store_key(template_key, frame_key)
        with self._lock:
            b = self._bucket(key)
            b.roi_misses += 1
            total_roi = b.roi_hits + b.roi_misses
            if (
                b.roi_misses >= ROI_MISS_CLEAR_AFTER
                and total_roi > 0
                and (b.roi_misses / total_roi) >= ROI_MISS_RATIO
            ):
                b.samples.clear()
                b.roi_hits = 0
                b.roi_misses = 0
            self._dirty = True
        self.flush()

    def clear_bucket(self, *, template_key: str, frame_key: str) -> None:
        key = make_store_key(template_key, frame_key)
        with self._lock:
            self._buckets.pop(key, None)
            self._dirty = True
        self.flush(force=True)

    def clear_all(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._dirty = True
        self.flush(force=True)

    def propose_rois(
        self,
        *,
        template_key: str,
        frame_key: str,
        frame_w: int,
        frame_h: int,
    ) -> list[RoiBox]:
        """返回 0~2 个 ROI（主簇、次簇）；坐标为帧像素。"""
        if not self.enabled or not template_key or frame_w < 8 or frame_h < 8:
            return []
        key = make_store_key(template_key, frame_key)
        with self._lock:
            b = self._buckets.get(key)
            samples = list(b.samples) if b else []

        if len(samples) < MIN_SAMPLES:
            return []

        pts = [(s.x, s.y) for s in samples]
        clusters = _cluster_points(pts, frame_w=frame_w, frame_h=frame_h)
        rois: list[RoiBox] = []
        for cluster in clusters:
            box = _box_from_points(cluster, frame_w=frame_w, frame_h=frame_h)
            if box is None:
                continue
            x1, y1, x2, y2 = box
            area = max(1, (x2 - x1) * (y2 - y1))
            if area >= MAX_ROI_AREA_FRAC * frame_w * frame_h:
                continue
            # 过小 ROI 容易漏（模板本身可能较大）
            if (x2 - x1) < MIN_PAD_PX or (y2 - y1) < MIN_PAD_PX:
                continue
            rois.append(box)
        return rois[:2]


def _stdev(vals: Sequence[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def _box_from_points(
    pts: Sequence[Tuple[float, float]],
    *,
    frame_w: int,
    frame_h: int,
) -> Optional[RoiBox]:
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sx = max(_stdev(xs), frame_w * 0.035)
    sy = max(_stdev(ys), frame_h * 0.035)
    pad_x = max(STD_K * sx, frame_w * MARGIN_FRAC, float(MIN_PAD_PX))
    pad_y = max(STD_K * sy, frame_h * MARGIN_FRAC, float(MIN_PAD_PX))
    # 覆盖样本极值
    pad_x = max(pad_x, (max(xs) - min(xs)) * 0.5 + MIN_PAD_PX * 0.5)
    pad_y = max(pad_y, (max(ys) - min(ys)) * 0.5 + MIN_PAD_PX * 0.5)
    x1 = int(max(0, math.floor(mx - pad_x)))
    y1 = int(max(0, math.floor(my - pad_y)))
    x2 = int(min(frame_w, math.ceil(mx + pad_x)))
    y2 = int(min(frame_h, math.ceil(my + pad_y)))
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return x1, y1, x2, y2


def _cluster_points(
    pts: Sequence[Tuple[float, float]],
    *,
    frame_w: int,
    frame_h: int,
) -> list[list[Tuple[float, float]]]:
    """简单 2 簇：若点集在某一轴上明显双峰则拆分，否则单簇。"""
    if len(pts) < MIN_SAMPLES * 2:
        return [list(pts)]

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    # 选离散更大的轴做 1D 二分
    if _stdev(xs) >= _stdev(ys):
        axis = 0
        span = max(frame_w, 1)
    else:
        axis = 1
        span = max(frame_h, 1)

    vals = sorted(p[axis] for p in pts)
    # 找最大空隙
    best_gap = 0.0
    split_at = None
    for i in range(len(vals) - 1):
        gap = vals[i + 1] - vals[i]
        if gap > best_gap:
            best_gap = gap
            split_at = (vals[i] + vals[i + 1]) * 0.5

    # 空隙不够大则不拆
    if split_at is None or best_gap < span * 0.18:
        return [list(pts)]

    a = [p for p in pts if p[axis] <= split_at]
    b = [p for p in pts if p[axis] > split_at]
    out = []
    for c in (a, b):
        if len(c) >= MIN_SAMPLES:
            out.append(c)
    if not out:
        return [list(pts)]
    # 样本多的簇优先
    out.sort(key=len, reverse=True)
    return out


_STORE: HotspotStore | None = None
_STORE_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def get_hotspot_store() -> HotspotStore:
    global _STORE, _ATEXIT_REGISTERED
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = HotspotStore()
            if not _ATEXIT_REGISTERED:
                import atexit
                atexit.register(lambda: get_hotspot_store().flush(force=True))
                _ATEXIT_REGISTERED = True
        return _STORE


def resolve_capture_mode(holder: Any) -> str:
    """从实际帧裁剪状态推断 game|full（不以意图开关冒充已裁剪）。"""
    for obj in (holder, getattr(holder, "_browser", None)):
        if obj is None:
            continue
        mode = getattr(obj, "_frame_capture_mode", None)
        if mode in ("viewport_crop", "canvas_buffer"):
            return "game"
    return "full"


def _template_size(template, matcher=None) -> Tuple[int, int] | None:
    """返回 (w, h)；失败则 None。优先走 matcher 模板缓存。"""
    try:
        import numpy as np

        if isinstance(template, np.ndarray):
            h, w = template.shape[:2]
            return int(w), int(h)
        if matcher is not None and hasattr(matcher, "_load_template_cached"):
            img, _gray = matcher._load_template_cached(template)
            h, w = img.shape[:2]
            return int(w), int(h)
        import cv2

        p = Path(str(template))
        if not p.is_file():
            return None
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        return int(w), int(h)
    except Exception:
        return None


def _fit_roi_to_template(
    box: RoiBox,
    *,
    frame_w: int,
    frame_h: int,
    templ_wh: Tuple[int, int] | None,
) -> RoiBox:
    """保证 ROI 至少能放下模板 + 边距，避免「ROI 比模板小」假失败。"""
    x1, y1, x2, y2 = box
    if not templ_wh:
        return box
    tw, th = templ_wh
    need_w = tw + MIN_PAD_PX
    need_h = th + MIN_PAD_PX
    cx = (x1 + x2) * 0.5
    cy = (y1 + y2) * 0.5
    half_w = max((x2 - x1) * 0.5, need_w * 0.5)
    half_h = max((y2 - y1) * 0.5, need_h * 0.5)
    nx1 = int(max(0, math.floor(cx - half_w)))
    ny1 = int(max(0, math.floor(cy - half_h)))
    nx2 = int(min(frame_w, math.ceil(cx + half_w)))
    ny2 = int(min(frame_h, math.ceil(cy + half_h)))
    if nx2 - nx1 < 8 or ny2 - ny1 < 8:
        return box
    # 仍过大则放弃该 ROI（由调用方跳过）
    area = (nx2 - nx1) * (ny2 - ny1)
    if area >= MAX_ROI_AREA_FRAC * frame_w * frame_h:
        return box  # 保留原 box；若原也过大 propose 已滤
    return nx1, ny1, nx2, ny2


def adaptive_match(
    matcher,
    frame,
    template,
    *,
    threshold: float,
    match_type: str,
    use_color_check: bool = False,
    match_select: str = "best",
    use_orb: bool = True,
    pixel_tol: float = 8.0,
    template_key: str = "",
    capture_mode: str = "full",
    enabled: bool = True,
    multi: bool = False,
):
    """
    自适应 ROI 匹配。
    - multi=False → MatchResult
    - multi=True  → list[dict]|list[MatchResult]（与 matcher 原返回一致）
    """
    from core.match.match_result import MatchResult

    if frame is None:
        return [] if multi else MatchResult(None, None, 0.0, False)

    fh, fw = frame.shape[:2]
    tkey = template_key or normalize_template_key(template)
    fkey = frame_bucket_key(fw, fh, capture_mode=capture_mode)
    store = get_hotspot_store()
    templ_wh = _template_size(template, matcher)

    def _call(crop_tl=None, crop_br=None):
        return matcher.match(
            target=frame,
            template=template,
            threshold=threshold,
            match_type=match_type,
            use_color_check=use_color_check,
            match_select=match_select,
            use_orb=use_orb,
            pixel_tol=pixel_tol,
            crop_top_left=crop_tl,
            crop_bottom_right=crop_br,
        )

    use_roi = bool(enabled and store.enabled and tkey)
    raw_rois = store.propose_rois(
        template_key=tkey, frame_key=fkey, frame_w=fw, frame_h=fh
    ) if use_roi else []
    rois: list[RoiBox] = []
    for box in raw_rois:
        fitted = _fit_roi_to_template(
            box, frame_w=fw, frame_h=fh, templ_wh=templ_wh
        )
        x1, y1, x2, y2 = fitted
        area = max(1, (x2 - x1) * (y2 - y1))
        if area >= MAX_ROI_AREA_FRAC * fw * fh:
            continue
        if templ_wh and ((x2 - x1) < templ_wh[0] or (y2 - y1) < templ_wh[1]):
            continue
        rois.append(fitted)

    if multi:
        for box in rois:
            x1, y1, x2, y2 = box
            results = _call((x1, y1), (x2, y2))
            if results:
                for r in results:
                    # r may be MatchResult or dict
                    rx = r["x"] if isinstance(r, dict) else r.x
                    ry = r["y"] if isinstance(r, dict) else r.y
                    sc = r["score"] if isinstance(r, dict) else r.max_val
                    if rx is not None and ry is not None:
                        store.record_hit(
                            template_key=tkey,
                            frame_key=fkey,
                            x=float(rx),
                            y=float(ry),
                            score=float(sc or 0.0),
                            via_roi=True,
                        )
                return results
        results = _call(None, None)
        if results and rois:
            store.note_roi_miss_then_full(template_key=tkey, frame_key=fkey)
        if results:
            for r in results:
                rx = r["x"] if isinstance(r, dict) else r.x
                ry = r["y"] if isinstance(r, dict) else r.y
                sc = r["score"] if isinstance(r, dict) else r.max_val
                if rx is not None and ry is not None:
                    store.record_hit(
                        template_key=tkey,
                        frame_key=fkey,
                        x=float(rx),
                        y=float(ry),
                        score=float(sc or 0.0),
                        via_roi=False,
                    )
        return results or []

    # single
    for box in rois:
        x1, y1, x2, y2 = box
        result = _call((x1, y1), (x2, y2))
        ok = bool(result and result.x is not None and getattr(result, "match_success", True))
        if ok and result.score is not None and result.score < threshold:
            ok = False
        if ok:
            store.record_hit(
                template_key=tkey,
                frame_key=fkey,
                x=float(result.x),
                y=float(result.y),
                score=float(result.max_val or 0.0),
                via_roi=True,
            )
            return result

    result = _call(None, None)
    ok = bool(result and result.x is not None and getattr(result, "match_success", True))
    if ok and result.score is not None and result.score < threshold:
        ok = False
    if ok:
        if rois:
            store.note_roi_miss_then_full(template_key=tkey, frame_key=fkey)
        store.record_hit(
            template_key=tkey,
            frame_key=fkey,
            x=float(result.x),
            y=float(result.y),
            score=float(result.max_val or 0.0),
            via_roi=False,
        )
    return result
