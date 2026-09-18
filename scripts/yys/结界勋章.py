"""结界突破：统计对手勋章数，选出最高。

适配个人 3×3、阴阳寮 2×4 等任意规则网格（由命中自动聚类，不写死行列）。

算法：
1. ``勋章.png`` 定标（layout hint 优先，弱峰值不盲目重定标）
2. 全图一次 matchTemplate + NMS
3. 按 y / x 大间隔自动切行、切列
4. 每格 count≤5，返回矩阵与最高格点击点
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import cv2
import numpy as np

from core.path import IMG_PATH

PathLike = Union[str, Path]

ASSET_DIR = IMG_PATH / "yys" / "结界"
DEFAULT_MEDAL = ASSET_DIR / "勋章.png"
DEFAULT_LAYOUT = ASSET_DIR / "layout.json"

_DEFAULT_THR = 0.75
_MAX_MEDALS = 5
# 重定标峰值必须显著高于阈值，避免空板把装饰图标当勋章
_RECALIBRATE_MIN_PEAK = 0.88


@dataclass(frozen=True)
class OpponentMedals:
    """单个对手卡槽。"""

    row: int
    col: int
    count: int
    click_xy: tuple[int, int]
    medal_xy: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class MedalBoardResult:
    """整板统计结果（行列由命中自动推断）。"""

    counts: tuple[tuple[int, ...], ...]
    opponents: tuple[OpponentMedals, ...]
    best: Optional[OpponentMedals]
    scale: float
    threshold: float
    hits: tuple[tuple[int, int, float], ...]
    rows: int
    cols: int


def _imread(path: PathLike) -> np.ndarray:
    p = Path(path)
    data = np.fromfile(str(p), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(p)
    return img


def _to_gray(bgr: np.ndarray) -> np.ndarray:
    if bgr.ndim == 2:
        return bgr
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def _peak_at_scale(frame_bgr: np.ndarray, medal_bgr: np.ndarray, scale: float) -> float:
    fg = _to_gray(frame_bgr)
    tg = _to_gray(medal_bgr)
    th = max(4, int(round(tg.shape[0] * scale)))
    tw = max(4, int(round(tg.shape[1] * scale)))
    if th >= fg.shape[0] or tw >= fg.shape[1]:
        return 0.0
    templ = cv2.resize(tg, (tw, th), interpolation=cv2.INTER_AREA)
    return float(cv2.minMaxLoc(cv2.matchTemplate(fg, templ, cv2.TM_CCOEFF_NORMED))[1])


def calibrate_medal_scale(
    frame_bgr: np.ndarray,
    medal_bgr: np.ndarray,
    *,
    s_min: float = 0.35,
    s_max: float = 1.80,
    steps: int = 28,
) -> tuple[float, float]:
    """粗搜最佳尺度，返回 (scale, peak_score)。"""
    best_s, best_sc = 1.0, -1.0
    for s in np.geomspace(s_min, s_max, steps):
        peak = _peak_at_scale(frame_bgr, medal_bgr, float(s))
        if peak > best_sc:
            best_sc, best_s = peak, float(s)
    return best_s, best_sc


def resolve_medal_scale(
    frame_bgr: np.ndarray,
    medal_bgr: np.ndarray,
    *,
    scale: Optional[float] = None,
    threshold: float = _DEFAULT_THR,
    hint: Optional[float] = None,
) -> float:
    """选择尺度：显式参数 > layout hint（稳）> 高置信重定标。"""
    if scale is not None and float(scale) > 0:
        return float(scale)

    if hint is None and DEFAULT_LAYOUT.is_file():
        try:
            hint = json.loads(DEFAULT_LAYOUT.read_text(encoding="utf-8")).get("medal_scale_hint")
        except Exception:
            hint = None

    cal_s, cal_peak = calibrate_medal_scale(frame_bgr, medal_bgr)
    if hint is not None and float(hint) > 0:
        hint = float(hint)
        hint_peak = _peak_at_scale(frame_bgr, medal_bgr, hint)
        # hint 已能打到阈值，或重定标不够强 → 坚持 hint（空板不漂）
        if hint_peak >= threshold:
            return hint
        if cal_peak >= max(_RECALIBRATE_MIN_PEAK, threshold + 0.08) and cal_peak > hint_peak + 0.05:
            return cal_s
        return hint

    if cal_peak >= threshold:
        return cal_s
    return cal_s


def match_medals(
    frame_bgr: np.ndarray,
    medal_bgr: np.ndarray,
    *,
    scale: float,
    threshold: float = _DEFAULT_THR,
    min_dist: Optional[float] = None,
) -> list[tuple[int, int, float]]:
    """全图多目标匹配实心勋章，NMS 后返回 (cx, cy, score)。"""
    fg = _to_gray(frame_bgr)
    tg = _to_gray(medal_bgr)
    th = max(4, int(round(tg.shape[0] * scale)))
    tw = max(4, int(round(tg.shape[1] * scale)))
    if th >= fg.shape[0] or tw >= fg.shape[1]:
        return []
    templ = cv2.resize(tg, (tw, th), interpolation=cv2.INTER_AREA)
    res = cv2.matchTemplate(fg, templ, cv2.TM_CCOEFF_NORMED)

    if min_dist is None:
        min_dist = 0.78 * max(th, tw)

    ys, xs = np.where(res >= threshold)
    if len(xs) == 0:
        return []
    scores = res[ys, xs]
    order = np.argsort(-scores)
    kept: list[tuple[int, int, float]] = []
    md2 = float(min_dist) ** 2
    for i in order:
        x = int(xs[i] + tw // 2)
        y = int(ys[i] + th // 2)
        sc = float(scores[i])
        if any((x - kx) ** 2 + (y - ky) ** 2 < md2 for kx, ky, _ in kept):
            continue
        kept.append((x, y, sc))
    return kept


def _cluster_1d(values: Sequence[float], gap: float) -> list[list[int]]:
    """对值按间距分簇，返回每簇在原序列中的下标。"""
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i])
    groups: list[list[int]] = [[order[0]]]
    for idx in order[1:]:
        prev = groups[-1][-1]
        if values[idx] - values[prev] > gap:
            groups.append([idx])
        else:
            groups[-1].append(idx)
    return groups


def _estimate_pitch(hits: Sequence[tuple[int, int, float]], scale: float = 0.6) -> float:
    if len(hits) < 2:
        return max(16.0, 32.0 * float(scale))
    ys = [h[1] for h in hits]
    y_gap = 40.0
    if len(ys) > 3:
        diffs = np.diff(sorted(ys))
        pos = diffs[diffs > 5]
        if len(pos):
            y_gap = max(28.0, float(np.median(pos)) * 0.5)
    y_groups = _cluster_1d(ys, gap=y_gap)
    pitches: list[float] = []
    for g in y_groups:
        xs = sorted(hits[i][0] for i in g)
        for a, b in zip(xs, xs[1:]):
            d = b - a
            if 12 <= d <= 90:
                pitches.append(float(d))
    if not pitches:
        xs = sorted(h[0] for h in hits)
        pitches = [float(b - a) for a, b in zip(xs, xs[1:]) if 12 <= b - a <= 90]
    return float(np.median(pitches)) if pitches else max(16.0, 32.0 * float(scale))


def _filter_sidebar_hits(
    hits: Sequence[tuple[int, int, float]],
    *,
    frame_w: int,
    pitch: float,
) -> list[tuple[int, int, float]]:
    """去掉左侧栏等孤立勋章（远离主簇）。"""
    if len(hits) <= 1:
        # 单点且偏左：当作侧栏噪声
        if hits and hits[0][0] < frame_w * 0.18:
            return []
        return list(hits)
    xs = [h[0] for h in hits]
    med_x = float(np.median(xs))
    out = []
    for h in hits:
        if h[0] < med_x - 3.5 * pitch and h[0] < frame_w * 0.14:
            continue
        if h[0] > med_x + 4.5 * pitch and h[0] > frame_w * 0.92:
            continue
        out.append(h)
    return out


def group_hits_to_grid(
    hits: Sequence[tuple[int, int, float]],
    *,
    rows: Optional[int] = None,
    cols: Optional[int] = None,
    pitch: Optional[float] = None,
) -> list[list[list[tuple[int, int, float]]]]:
    """把勋章命中分到自动/指定的网格。

    rows/cols 为 None 时由聚类自动决定（个人 3×3、寮 2×4 等）。
    返回矩形网格；短行右侧以空列表补齐。
    """
    if not hits:
        r = rows or 0
        c = cols or 0
        return [[[] for _ in range(c)] for _ in range(r)]

    pitch = float(pitch if pitch is not None else _estimate_pitch(hits))
    ys = [h[1] for h in hits]
    y_groups = _cluster_1d(ys, gap=max(pitch * 2.0, 40.0))

    # 去掉过小的噪声行（侧栏单点被并进异常 y）
    y_groups = [g for g in y_groups if len(g) >= 1]
    if rows is not None:
        if len(y_groups) > rows:
            y_groups = sorted(y_groups, key=lambda g: len(g), reverse=True)[:rows]
            y_groups.sort(key=lambda g: float(np.mean([hits[i][1] for i in g])))
        while len(y_groups) < rows:
            y_groups.append([])

    card_gap = pitch * 2.2
    row_cards: list[list[list[tuple[int, int, float]]]] = []
    for g in y_groups:
        if not g:
            row_cards.append([])
            continue
        xs = [hits[i][0] for i in g]
        x_groups = _cluster_1d(xs, gap=card_gap)
        # 丢弃明显侧栏单枚簇
        filtered = []
        for xg in x_groups:
            cx = float(np.mean([xs[j] for j in xg]))
            if len(xg) <= 1 and cx < min(xs) + pitch * 1.5 and len(x_groups) > 1:
                continue
            filtered.append(xg)
        x_groups = filtered or x_groups

        if cols is not None and len(x_groups) > cols:
            x_groups = sorted(x_groups, key=lambda gg: len(gg), reverse=True)[:cols]
            x_groups.sort(key=lambda gg: float(np.mean([xs[j] for j in gg])))

        cards = []
        for xg in x_groups:
            pts = [hits[g[j]] for j in xg]
            pts.sort(key=lambda t: t[0])
            cards.append(pts[:_MAX_MEDALS])
        row_cards.append(cards)

    n_rows = len(row_cards)
    if cols is not None:
        n_cols = cols
    else:
        n_cols = max((len(r) for r in row_cards), default=0)

    grid: list[list[list[tuple[int, int, float]]]] = []
    for cards in row_cards:
        row = list(cards)
        while len(row) < n_cols:
            row.append([])
        grid.append(row[:n_cols])
    if rows is not None:
        while len(grid) < rows:
            grid.append([[] for _ in range(n_cols)])
        grid = grid[:rows]
    return grid


def _click_from_medals(
    medals: Sequence[tuple[int, int, float]],
    *,
    pitch: float,
    frame_w: int,
    frame_h: int,
) -> tuple[int, int]:
    """卡面点击点：勋章重心略向左上。"""
    if not medals:
        return frame_w // 2, frame_h // 2
    mx = float(np.mean([m[0] for m in medals]))
    my = float(np.mean([m[1] for m in medals]))
    cx = int(round(mx - 1.55 * pitch))
    cy = int(round(my - 1.05 * pitch))
    return int(np.clip(cx, 0, frame_w - 1)), int(np.clip(cy, 0, frame_h - 1))


def count_opponent_medals(
    frame_bgr: np.ndarray,
    medal_template: Optional[PathLike] = None,
    *,
    scale: Optional[float] = None,
    threshold: float = _DEFAULT_THR,
    rows: Optional[int] = None,
    cols: Optional[int] = None,
) -> MedalBoardResult:
    """统计当前帧各对手勋章数。

    Parameters
    ----------
    frame_bgr : 当前窗口截图 (BGR)
    medal_template : 实心勋章小图
    scale : 模板尺度；None 则自动
    threshold : 匹配阈值（建议 0.72~0.80）
    rows, cols : 指定网格；默认 None=由命中自动推断（3×3 / 2×4 等）
    """
    medal_path = Path(medal_template) if medal_template else DEFAULT_MEDAL
    medal_bgr = _imread(medal_path)

    scale_v = resolve_medal_scale(
        frame_bgr, medal_bgr, scale=scale, threshold=threshold
    )
    hits = match_medals(frame_bgr, medal_bgr, scale=scale_v, threshold=threshold)
    pitch = _estimate_pitch(hits, scale=scale_v) if hits else max(8.0, 20.0 * scale_v)
    fh, fw = frame_bgr.shape[:2]
    hits = _filter_sidebar_hits(hits, frame_w=fw, pitch=pitch)

    cells = group_hits_to_grid(hits, rows=rows, cols=cols, pitch=pitch)
    n_rows = len(cells)
    n_cols = len(cells[0]) if cells else 0

    opponents: list[OpponentMedals] = []
    counts: list[list[int]] = []
    for ri in range(n_rows):
        row_counts: list[int] = []
        for ci in range(n_cols):
            pts = cells[ri][ci]
            cnt = min(_MAX_MEDALS, len(pts))
            row_counts.append(cnt)
            if pts:
                click = _click_from_medals(pts, pitch=pitch, frame_w=fw, frame_h=fh)
            else:
                # 空格无点击意义；占位避免除零
                click = (
                    int((ci + 0.5) * fw / max(1, n_cols)),
                    int((ri + 0.5) * fh / max(1, n_rows)),
                )
            opponents.append(
                OpponentMedals(
                    row=ri,
                    col=ci,
                    count=cnt,
                    click_xy=click,
                    medal_xy=tuple((p[0], p[1]) for p in pts[:cnt]),
                )
            )
        counts.append(row_counts)

    best: Optional[OpponentMedals] = None
    if opponents:
        best = max(opponents, key=lambda o: (o.count, -o.row, -o.col))
        if best.count <= 0:
            best = None

    return MedalBoardResult(
        counts=tuple(tuple(r) for r in counts),
        opponents=tuple(opponents),
        best=best,
        scale=float(scale_v),
        threshold=float(threshold),
        hits=tuple(hits),
        rows=n_rows,
        cols=n_cols,
    )


def load_layout(path: Optional[PathLike] = None) -> dict:
    p = Path(path) if path else DEFAULT_LAYOUT
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def draw_debug(frame_bgr: np.ndarray, result: MedalBoardResult) -> np.ndarray:
    """可视化命中与计数（BGR）。"""
    out = frame_bgr.copy()
    for x, y, sc in result.hits:
        cv2.circle(out, (int(x), int(y)), 9, (0, 0, 255), 2)
    for op in result.opponents:
        if op.count <= 0 and not op.medal_xy:
            continue
        cx, cy = op.click_xy
        color = (0, 255, 0) if result.best is not None and op.row == result.best.row and op.col == result.best.col else (255, 128, 0)
        cv2.putText(
            out,
            str(op.count),
            (cx - 8, cy + 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            color,
            2,
        )
        cv2.drawMarker(out, (cx, cy), color, markerType=cv2.MARKER_CROSS, markerSize=14, thickness=2)
    label = f"{result.rows}x{result.cols} hits={len(result.hits)}"
    cv2.putText(out, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
    return out


async def count_from_window(browser, **kwargs) -> MedalBoardResult:
    """从 UserWindow / UserBrowser 取当前帧并统计。"""
    update = getattr(browser, "update_frame", None)
    if callable(update):
        await update()
    frame = getattr(browser, "_frame", None)
    if frame is None:
        inner = getattr(browser, "_browser", None) or getattr(browser, "_observer", None)
        frame = getattr(inner, "_frame", None) if inner is not None else None
    if frame is None:
        ensure = getattr(getattr(browser, "_observer", None), "ensure_frame", None)
        if callable(ensure):
            frame = await ensure(force=True)
    if frame is None:
        raise RuntimeError("无可用帧：请先截图/绑定窗口")
    return count_opponent_medals(frame, **kwargs)


def pick_richest(result: MedalBoardResult) -> Optional[OpponentMedals]:
    """返回勋章最多的对手（并列取偏左上）。"""
    return result.best
