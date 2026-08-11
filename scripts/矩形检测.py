"""
矩形 / 卡池格子检测（带滑块 GUI）
================================
模式：
  0 = 卡池格子（水平分隔线 + 周期网格）
  1 = 经典矩形（梯度轮廓，原版逻辑）

运行后弹出 OpenCV 窗口，可用滑块实时微调；ESC 保存到
screenshots/rect_detection.png
"""

from __future__ import annotations

from collections import Counter

import cv2
import numpy as np

from backend.browser.user_browser import UserBrowser
from core.path import PROJECT_ROOT


# ---------------------------------------------------------------------------
# 经典矩形检测（保留原版）
# ---------------------------------------------------------------------------

def _detect_classic(frame, min_area, grad_thresh, min_size, solidity_min, grad_min_ratio):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    mag = cv2.magnitude(grad_x, grad_y)
    mag = np.uint8(np.clip(mag, 0, 255))

    _, binary = cv2.threshold(mag, grad_thresh, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < min_size or bh < min_size:
            continue
        roi = mag[y:y + bh, x:x + bw]
        mean_grad = np.mean(roi)
        rect_area = bw * bh
        solidity = area / rect_area if rect_area > 0 else 0
        if solidity > solidity_min / 100 and mean_grad > grad_thresh * (grad_min_ratio / 100):
            candidates.append((x, y, bw, bh, solidity))

    candidates.sort(key=lambda r: r[2] * r[3], reverse=True)
    results = []
    for r in candidates:
        x1, y1, w1, h1 = r[:4]
        contained = False
        for r2 in candidates:
            if r2 is r:
                continue
            x2, y2, w2, h2 = r2[:4]
            if (x2 <= x1 and y2 <= y1 and x2 + w2 >= x1 + w1 and y2 + h2 >= y1 + h1):
                if (w2 * h2) > (w1 * h1) * 1.5:
                    contained = True
                    break
        if not contained:
            results.append(r)
    return results


def _auto_search_classic(frame):
    best_count = 0
    best_params = None
    best_rects = []
    for grad in range(5, 65, 5):
        for min_a in [50, 100, 200, 400]:
            min_s = max(5, grad // 3)
            sol = max(20, 50 - grad // 2)
            g_ratio = max(10, 30 - grad // 3)
            rects = _detect_classic(
                frame,
                min_area=min_a,
                grad_thresh=grad,
                min_size=min_s,
                solidity_min=sol,
                grad_min_ratio=g_ratio,
            )
            if len(rects) > best_count:
                best_count = len(rects)
                best_params = (min_a, grad, min_s, sol, g_ratio)
                best_rects = rects
    return best_params, best_rects


# ---------------------------------------------------------------------------
# 卡池格子检测
# ---------------------------------------------------------------------------

# 跨次检测记住稳定卡高，避免滚动后漂到半周期
_LAST_GOOD_PITCH: int | None = None
_LAST_GOOD_WIDTH: int | None = None
_LOCK_PATH = PROJECT_ROOT / "screenshots" / "gacha_slot_lock.json"


def _load_pitch_lock() -> None:
    global _LAST_GOOD_PITCH, _LAST_GOOD_WIDTH
    try:
        import json

        if not _LOCK_PATH.exists():
            return
        data = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
        p = data.get("pitch")
        w = data.get("width")
        _LAST_GOOD_PITCH = int(p) if p else None
        _LAST_GOOD_WIDTH = int(w) if w else None
    except Exception:
        pass


def _save_pitch_lock() -> None:
    try:
        import json

        _LOCK_PATH.parent.mkdir(exist_ok=True)
        _LOCK_PATH.write_text(
            json.dumps({"pitch": _LAST_GOOD_PITCH, "width": _LAST_GOOD_WIDTH}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _cluster_peaks(peaks, merge: int = 3) -> list[int]:
    lines: list[int] = []
    if len(peaks) == 0:
        return lines
    start = prev = int(peaks[0])
    for y in peaks[1:]:
        y = int(y)
        if y - prev <= merge:
            prev = y
        else:
            lines.append((start + prev) // 2)
            start = prev = y
    lines.append((start + prev) // 2)
    return lines


def _estimate_pitch(cands: list[int], pitch_min: int, pitch_max: int, signal=None, length: int | None = None) -> int | None:
    """估单卡高度。以相邻分隔线间距为主，自相关为辅；避免误升到 2 倍周期。"""
    # 1) 相邻候选间距（最贴近真实卡高）
    adj_gaps: list[float] = []
    if len(cands) >= 2:
        ordered = sorted(int(y) for y in cands)
        for a, b in zip(ordered, ordered[1:]):
            d = b - a
            if pitch_min <= d <= pitch_max:
                adj_gaps.append(float(d))

    gap_pitch = None
    if adj_gaps:
        rounded = [int(round(d / 2.0) * 2) for d in adj_gaps]
        mode = Counter(rounded).most_common(1)[0][0]
        near = [d for d in adj_gaps if abs(d - mode) <= max(8, mode * 0.14)]
        gap_pitch = int(round(float(np.median(near if near else adj_gaps))))

    # 2) 自相关峰
    corr_peaks: list[tuple[float, int]] = []
    if signal is not None and length is not None and length > pitch_max:
        sig = signal.astype(np.float64)
        sig = sig - sig.mean()
        if float(np.std(sig)) > 1e-6:
            corr = np.correlate(sig, sig, mode="full")
            corr = corr[len(corr) // 2 :]
            lo = max(1, pitch_min)
            hi = min(pitch_max, len(corr) - 2)
            if hi > lo + 5:
                window = corr[lo : hi + 1].astype(np.float64)
                for i in range(2, len(window) - 2):
                    v = float(window[i])
                    if v >= float(window[i - 1]) and v >= float(window[i + 1]) and v > 0:
                        corr_peaks.append((v, lo + i))
                if not corr_peaks:
                    lag = lo + int(np.argmax(window))
                    corr_peaks.append((float(window.max()), lag))

    corr_pitch = None
    if corr_peaks:
        max_s = max(s for s, _ in corr_peaks)
        scored: list[tuple[float, int]] = []
        for strength, lag in corr_peaks:
            if strength < max_s * 0.4:
                continue
            score = strength
            # 若存在约一半的峰且更贴 gap，则当前 lag 可能是 2 倍卡高 → 降权
            half = next((s for s, L in corr_peaks if abs(L * 2 - lag) <= 8), None)
            if half is not None and half >= strength * 0.5:
                score *= 0.5
            # 与相邻间距众数接近则加分
            if gap_pitch and abs(lag - gap_pitch) <= max(10, gap_pitch * 0.12):
                score *= 1.35
            scored.append((score, lag))
        if not scored:
            # 阈值过严时回退到最强相关峰
            scored = [(s, L) for s, L in corr_peaks]
        scored.sort(key=lambda x: x[0], reverse=True)
        corr_pitch = int(scored[0][1])
        # 若相关峰≈2*gap，强制回到 gap（常见误检）
        if gap_pitch and abs(corr_pitch - 2 * gap_pitch) <= max(10, gap_pitch * 0.15):
            corr_pitch = gap_pitch

    if gap_pitch and corr_pitch:
        # 两者冲突时信相邻间距
        if abs(gap_pitch - corr_pitch) <= max(12, gap_pitch * 0.15):
            return int(round((gap_pitch + corr_pitch) / 2))
        return gap_pitch
    return gap_pitch or corr_pitch


def _trim_home_back_y(roi_bgr: np.ndarray) -> tuple[int, int]:
    """在列表 ROI 内再裁掉顶部 HOME、底部 BACK，返回相对 y0/y1。

    只做保守裁切。注意：卡面「残り」角标也是蓝色，不能按全列蓝带往上爬。
    """
    rh, rw = roi_bgr.shape[:2]
    if rh < 40:
        return 0, rh
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, (90, 50, 50), (140, 255, 255))
    row_blue = (blue > 0).mean(axis=1)
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    dark = (gray < 55).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, rw // 15), 1))
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, k)
    dark_row = (dark > 0).sum(axis=1)
    min_w = max(8, int(rw * 0.40))
    dark_lines = _cluster_peaks(np.where(dark_row >= min_w)[0], 3)

    y0 = 0
    top_n = max(10, int(rh * 0.18))
    if float(row_blue[:top_n].max()) >= 0.18:
        peak = int(np.argmax(row_blue[:top_n]))
        y = peak
        while y < top_n and row_blue[y] > 0.08:
            y += 1
        pad = 0
        while y < min(rh - 1, peak + max(16, int(rh * 0.05))) and pad < 10:
            if row_blue[y] > 0.05:
                y += 1
                pad += 1
                continue
            break
        after = [
            d
            for d in dark_lines
            if peak + 4 <= d <= min(int(rh * 0.28), peak + max(55, int(rh * 0.14)))
        ]
        if after:
            y0 = min(after)
        else:
            y0 = min(y, int(rh * 0.18))
    elif dark_lines and dark_lines[0] < rh * 0.08 and len(dark_lines) > 1:
        y0 = dark_lines[0]

    y1 = rh
    # BACK 只在最底部一小段找，避免被卡面蓝角标带上去
    bot_zone = max(y0 + 40, int(rh * 0.86))
    if float(row_blue[bot_zone:].max()) >= 0.20:
        peak = bot_zone + int(np.argmax(row_blue[bot_zone:]))
        y = peak
        while y > bot_zone and row_blue[y] >= 0.12:
            y -= 1
        # 只允许吸附紧贴 BACK 上方的深色线，禁止上跳到卡缝
        near = [d for d in dark_lines if y - 12 <= d <= y + 3]
        if near:
            y1 = min(rh, near[-1] + 1)
        else:
            y1 = max(bot_zone, min(rh, y + 1))
    # 若底部仍被裁太狠（剩余高度不足），回退少裁
    if y1 - y0 < max(100, int(rh * 0.55)):
        y1 = rh

    if y1 - y0 < max(80, int(rh * 0.45)):
        return 0, rh
    return y0, y1


def _estimate_pitch_from_dark(dark_lines: list[int], pitch_min: int, pitch_max: int) -> int | None:
    """用相邻深色边间距估单卡高度（比全组合间距更稳）。"""
    if len(dark_lines) < 2:
        return None
    gaps = []
    ordered = sorted(int(y) for y in dark_lines)
    for a, b in zip(ordered, ordered[1:]):
        d = b - a
        if pitch_min <= d <= pitch_max:
            gaps.append(d)
    if len(gaps) < 2:
        return None
    # 卡顶/卡底会出成对短间距；取较大众数簇
    gaps_sorted = sorted(gaps)
    # 优先落在 [pmin, pmax] 内且靠近中位的值
    med = float(np.median(gaps_sorted))
    near = [g for g in gaps_sorted if abs(g - med) <= max(8, med * 0.18)]
    if not near:
        near = gaps_sorted
    return int(round(float(np.median(near))))


def _slot_blue_ratio(bgr_roi: np.ndarray) -> float:
    """HOME 圆形按钮偏蓝，用来剔除误检。"""
    if bgr_roi.size == 0:
        return 0.0
    hsv = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, (90, 60, 60), (140, 255, 255))
    return float((blue > 0).mean())


def _refine_and_filter_slots(
    frame: np.ndarray,
    slots: list[tuple[int, int, int, int, float]],
    pitch: int,
    roi: tuple[int, int, int, int],
) -> list[tuple[int, int, int, int, float]]:
    """按高度一致性过滤，并去掉顶部 HOME / 底部 BACK 一类误检。"""
    if not slots or pitch <= 0:
        return slots

    left, top, right, bottom = roi
    kept: list[tuple[int, int, int, int, float]] = []
    for x, y, bw, bh, sc in slots:
        if bh >= pitch * 0.70:
            kept.append((x, y, bw, bh, 1.0 if bh >= pitch * 0.85 else sc))
        elif (y <= top + 3 or y + bh >= bottom - 3) and bh >= pitch * 0.40:
            kept.append((x, y, bw, bh, 0.5))
    if not kept:
        return []

    heights = [s[3] for s in kept if s[4] >= 0.9]
    med = float(np.median(heights)) if heights else float(pitch)
    if heights:
        kept = [
            s for s in kept
            if s[4] < 0.9 or abs(s[3] - med) <= max(18, med * 0.22)
        ]

    def _looks_nav(patch: np.ndarray) -> bool:
        if patch.size == 0:
            return False
        br = _slot_blue_ratio(patch)
        if br < 0.14:
            return False
        std = float(np.std(patch.reshape(-1, 3), axis=0).mean())
        # HOME/BACK：偏蓝且色彩更“干净”
        return br > 0.18 or (br > 0.14 and std < 42)

    if len(kept) >= 2:
        first = kept[0]
        patch0 = frame[first[1] : first[1] + first[3], first[0] : first[0] + first[2]]
        if first[1] <= top + max(8, int(pitch * 0.35)) and _looks_nav(patch0):
            kept = kept[1:]
        elif len(kept) >= 2:
            blues = [_slot_blue_ratio(frame[s[1] : s[1] + s[3], s[0] : s[0] + s[2]]) for s in kept]
            rest_blue = float(np.median(blues[1:])) if len(blues) > 1 else 0.0
            if blues[0] > rest_blue + 0.08 and blues[0] > 0.14:
                kept = kept[1:]

    if len(kept) >= 2:
        last = kept[-1]
        patch_l = frame[last[1] : last[1] + last[3], last[0] : last[0] + last[2]]
        if last[1] + last[3] >= bottom - max(8, int(pitch * 0.35)) and _looks_nav(patch_l):
            kept = kept[:-1]

    return kept


def _find_content_bbox(frame):
    """去掉左右黑/白边，返回内容区 (left, top, right, bottom)。"""
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    y0, y1 = int(h * 0.12), int(h * 0.88)
    band = gray[y0:y1]
    # 非近白且非近黑的像素占比
    non_blank = ((band < 245) & (band > 12)).mean(axis=0)
    xs = np.where(non_blank > 0.12)[0]
    if len(xs) < 20:
        return 0, 0, w, h
    left = int(xs[0])
    right = int(xs[-1]) + 1
    # 上下也略裁一点纯边
    non_blank_row = ((gray[:, left:right] < 245) & (gray[:, left:right] > 12)).mean(axis=1)
    ys = np.where(non_blank_row > 0.08)[0]
    top = int(ys[0]) if len(ys) else 0
    bottom = int(ys[-1]) + 1 if len(ys) else h
    # 防御太狠则回退
    if (right - left) < w * 0.5 or (bottom - top) < h * 0.5:
        return 0, 0, w, h
    return left, top, right, bottom


def _auto_find_list_roi(frame):
    """
    自动锁定左侧卡池列表区域。
    全屏时先去掉黑白边，再在内容区左栏找列表；窄图则用整图。
    最后再按颜色裁掉 HOME / BACK。
    返回 (left, top, right, bottom)。
    """
    h, w = frame.shape[:2]
    if w < 520 or (w / max(h, 1)) < 0.75:
        left, top, right, bottom = 0, 0, w, h
    else:
        c_left, c_top, c_right, c_bottom = _find_content_bbox(frame)
        cw = c_right - c_left
        ch = c_bottom - c_top
        if cw < 200 or ch < 200:
            c_left, c_top, c_right, c_bottom = 0, 0, w, h
            cw, ch = w, h

        content = frame[c_top:c_bottom, c_left:c_right]
        gray = cv2.cvtColor(content, cv2.COLOR_BGR2GRAY)
        # 粗裁顶部/底部导航，后续再用颜色精修（勿裁太狠）
        top_i = int(ch * 0.06)
        bottom_i = int(ch * 0.96)
        gy = np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3))
        gx = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
        col_v = gx[top_i:bottom_i, :].mean(axis=0)
        col_v = np.convolve(col_v, np.ones(9) / 9.0, mode="same")

        x_lo, x_hi = int(cw * 0.10), int(cw * 0.35)
        x_hi = max(x_lo + 10, min(x_hi, cw - 1))
        v_peak = x_lo + int(np.argmax(col_v[x_lo:x_hi]))

        pmin = max(40, int(ch * 0.08))
        pmax = max(pmin + 20, int(ch * 0.20))
        best = None
        for right in range(int(cw * 0.12), int(cw * 0.30) + 1, 4):
            strip = gy[top_i:bottom_i, int(cw * 0.005):right]
            if strip.size == 0:
                continue
            row_e = strip.mean(axis=1)
            sig = row_e - row_e.mean()
            if float(np.std(sig)) < 1e-6:
                continue
            corr = np.correlate(sig, sig, mode="full")
            corr = corr[len(corr) // 2 :]
            if len(corr) <= pmax:
                continue
            corr = corr.copy()
            corr[:pmin] = 0
            window = corr[pmin:pmax + 1]
            if window.size == 0 or float(window.max()) <= 0:
                continue
            lag = pmin + int(np.argmax(window))
            aspect = right / max(lag, 1)
            if not (1.7 <= aspect <= 4.2):
                continue
            peak_val = float(window.max())
            score = peak_val * (1.0 + 0.35 * (1.0 - abs(right - v_peak) / max(cw * 0.12, 1.0)))
            ideal = cw * 0.18
            score *= 1.0 + 0.25 * (1.0 - min(abs(right - ideal) / max(ideal, 1), 1.0))
            edge_boost = float(col_v[min(right, cw - 1)]) / (float(col_v.max()) + 1e-6)
            score *= 1.0 + 0.4 * edge_boost
            rec = (score, right, lag)
            if best is None or rec[0] > best[0]:
                best = rec

        if best is None:
            right_local = int(cw * 0.18)
        else:
            right_local = int(best[1]) + max(4, int(cw * 0.004))

        left = c_left
        right = min(c_right, c_left + right_local)
        top = c_top + top_i
        bottom = c_top + bottom_i

    # 颜色精修：去掉 HOME / BACK
    base_top = top
    y0, y1 = _trim_home_back_y(frame[top:bottom, left:right])
    top = base_top + y0
    bottom = base_top + y1
    return left, top, right, bottom


def _detect_gacha_slots(
    frame: np.ndarray,
    *,
    auto_roi: int = 1,
    roi_top_pct: int = 0,
    roi_bottom_pct: int = 0,
    roi_left_pct: int = 0,
    roi_right_pct: int = 0,
    dark_thresh: int = 55,
    bright_thresh: int = 200,
    bg_ratio: int = 50,
    width_pct: int = 50,
    pitch_min: int = 95,
    pitch_max: int = 145,
    snap_tol: int = 18,
    update_lock: bool = True,
):
    h, w = frame.shape[:2]
    if auto_roi:
        left, top, right, bottom = _auto_find_list_roi(frame)
    else:
        top = int(h * roi_top_pct / 100)
        bottom = h - int(h * roi_bottom_pct / 100)
        left = int(w * roi_left_pct / 100)
        right = w - int(w * roi_right_pct / 100)

    if bottom - top < 40 or right - left < 40:
        return [], {"pitch": None, "seps": [], "roi": (left, top, right, bottom)}

    # 全屏误检防护：列表宽度不应接近整屏
    if (right - left) > w * 0.45 and w > 700:
        left, top, right, bottom = _auto_find_list_roi(frame)

    global _LAST_GOOD_PITCH, _LAST_GOOD_WIDTH  # noqa: PLW0603 — used at end too
    # 滚动后宽度漂了，沿用首次成功的列表宽
    if _LAST_GOOD_WIDTH and w > 700:
        cur_w = right - left
        if abs(cur_w - _LAST_GOOD_WIDTH) > _LAST_GOOD_WIDTH * 0.25:
            right = min(w, left + _LAST_GOOD_WIDTH)

    roi = frame[top:bottom, left:right]
    rh, rw = roi.shape[:2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    # 卡面宽高比约 2.2~3.3；再用深色边间距校正 ideal，避免锁到 2 倍卡高
    ideal_pitch = max(48, int(rw / 2.70))
    min_w = max(8, int(rw * width_pct / 100))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, rw // 15), 1))

    dark = (gray < dark_thresh).astype(np.uint8) * 255
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, k)
    dark_row = dark.sum(axis=1).astype(np.float64)
    dark_lines = _cluster_peaks(np.where(dark_row >= min_w)[0], 3)

    # 用固定阈值估一次“权威”卡高，避免 dark_thresh 滑块漂到半周期
    _k0 = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, rw // 15), 1))
    _dark0 = (gray < 55).astype(np.uint8) * 255
    _dark0 = cv2.morphologyEx(_dark0, cv2.MORPH_CLOSE, _k0)
    _drow0 = _dark0.sum(axis=1)
    _mw0 = max(8, int(rw * 0.50))
    _lines0 = _cluster_peaks(np.where(_drow0 >= _mw0)[0], 3)
    canon_pitch = _estimate_pitch_from_dark(
        _lines0, max(40, int(rh * 0.08)), max(55, int(rh * 0.26))
    )
    if canon_pitch:
        ideal_pitch = int(round(ideal_pitch * 0.25 + canon_pitch * 0.75))
        dark_pitch = canon_pitch
    else:
        dark_pitch = None

    if pitch_min < 20 or pitch_max <= pitch_min:
        pitch_min = max(40, int(ideal_pitch * 0.80))
        pitch_max = max(pitch_min + 15, int(ideal_pitch * 1.22))
    else:
        # 允许向下扩到 dark/ideal，否则高 pitch_min 会错过真卡高
        pitch_min = min(pitch_min, max(40, int(ideal_pitch * 0.82)))
        pitch_max = min(max(pitch_max, int(ideal_pitch * 1.25)), max(pitch_min + 12, int(ideal_pitch * 1.35)))
    pitch_max = min(pitch_max, max(pitch_min + 10, int(rh * 0.32)))
    if pitch_max <= pitch_min:
        pitch_min = max(40, int(ideal_pitch * 0.80))
        pitch_max = max(pitch_min + 15, int(ideal_pitch * 1.22))

    bright = (gray > bright_thresh).astype(np.uint8) * 255
    bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN, k)
    bright_min_w = max(8, int(rw * max(width_pct - 15, 25) / 100))
    bright_row = (bright > 0).sum(axis=1).astype(np.float64)
    bright_lines = _cluster_peaks(np.where(bright_row >= bright_min_w)[0], 2)

    bg = cv2.inRange(hsv, (85, 20, 160), (115, 120, 255))
    bg = cv2.bitwise_or(bg, cv2.inRange(hsv, (0, 0, 200), (180, 40, 255)))
    row_bg = (bg > 0).mean(axis=1)
    bg_lines = _cluster_peaks(np.where(row_bg >= bg_ratio / 100.0)[0], 4)

    # 卡与卡之间的背景缝更可靠；内部「残り/立绘」边容易在滚动后变成半周期
    sep_signal = (
        dark_row / max(rw, 1) * 0.35
        + bright_row / max(rw, 1) * 0.35
        + row_bg * 2.8
    )
    cands = sorted(set(dark_lines + bright_lines + bg_lines))
    # 只用中段估周期，避开滚后上下半截卡
    mid0, mid1 = int(rh * 0.18), int(rh * 0.82)
    if mid1 - mid0 > pitch_min * 2:
        mid_signal = sep_signal[mid0:mid1]
        mid_cands = [c - mid0 for c in cands if mid0 <= c < mid1]
        mid_bg = [c - mid0 for c in bg_lines if mid0 <= c < mid1]
        mid_dark = [c - mid0 for c in dark_lines if mid0 <= c < mid1]
        # 深色边相邻间距最贴近卡高
        if len(mid_dark) >= 2:
            pitch_cands = mid_dark
        elif len(mid_bg) >= 2:
            pitch_cands = mid_bg
        else:
            pitch_cands = mid_cands
        pitch = _estimate_pitch(
            pitch_cands, pitch_min, pitch_max, signal=mid_signal, length=mid1 - mid0
        )
    else:
        pitch = _estimate_pitch(cands, pitch_min, pitch_max, signal=sep_signal, length=rh)

    if pitch and _LAST_GOOD_PITCH:
        # 滚动后若漂到约一半/两倍，拉回上次稳定值
        if abs(pitch * 2 - _LAST_GOOD_PITCH) <= max(12, _LAST_GOOD_PITCH * 0.12):
            pitch = _LAST_GOOD_PITCH
        elif abs(pitch - _LAST_GOOD_PITCH / 2) <= max(10, _LAST_GOOD_PITCH * 0.1):
            pitch = _LAST_GOOD_PITCH
        elif abs(pitch - _LAST_GOOD_PITCH) <= max(14, _LAST_GOOD_PITCH * 0.15):
            pitch = _LAST_GOOD_PITCH

    # 相对列表宽仍过大 → 多半是 2 倍并格，折半（允许低于调用方 pitch_min）
    if pitch and pitch > ideal_pitch * 1.28:
        half = int(round(pitch / 2))
        lo = max(40, int(ideal_pitch * 0.70))
        hi = max(pitch_max, int(ideal_pitch * 1.25))
        if lo <= half <= hi:
            pitch = half
    # 相对列表宽过小 → 半卡，尝试 2 倍（仅当 2P 仍合理）
    elif pitch and pitch < ideal_pitch * 0.72:
        dbl = int(round(pitch * 2))
        if max(40, int(ideal_pitch * 0.8)) <= dbl <= min(int(rh * 0.32), int(ideal_pitch * 1.30)):
            pitch = dbl
    # 深色边间距足够稳时，直接采用（最抗半周期/倍周期）
    if dark_pitch and pitch:
        if abs(pitch - dark_pitch) > max(6, dark_pitch * 0.10):
            if abs(pitch - 2 * dark_pitch) <= max(10, dark_pitch * 0.15) or abs(
                2 * pitch - dark_pitch
            ) <= max(10, pitch * 0.15):
                pitch = dark_pitch
            elif abs(pitch - dark_pitch) <= max(18, dark_pitch * 0.28):
                pitch = dark_pitch
            elif pitch < dark_pitch * 0.85 or pitch > dark_pitch * 1.2:
                # 相关峰漂了，仍信深色边
                pitch = dark_pitch
    elif dark_pitch and not pitch:
        pitch = dark_pitch

    debug = {
        "pitch": pitch,
        "seps": [],
        "roi": (left, top, right, bottom),
        "cand_count": len(cands),
        "auto_roi": bool(auto_roi),
    }
    if pitch is None or pitch <= 0:
        return [], debug

    def _edge_bg(y: int, half: int = 2) -> float:
        y0 = max(0, y - half)
        y1 = min(rh, y + half + 1)
        return float(row_bg[y0:y1].mean()) if y1 > y0 else 0.0

    def _slot_quality(y1: int, y2: int) -> float:
        """完整卡：上下是缝(高 bg)，中间是内容(低 bg)。跨缝则扣分。"""
        if y2 <= y1 + 4:
            return -1e6
        hh = y2 - y1
        mid = (y1 + y2) // 2
        band = max(2, hh // 7)
        c0, c1 = max(0, mid - band), min(rh, mid + band + 1)
        center_bg = float(row_bg[c0:c1].mean()) if c1 > c0 else 1.0
        e = max(2, hh // 10)
        top_bg = float(row_bg[max(0, y1) : min(rh, y1 + e)].mean())
        bot_bg = float(row_bg[max(0, y2 - e) : min(rh, y2)].mean())
        score = (top_bg + bot_bg) * 1.5 - center_bg * 2.4
        if center_bg > 0.42:
            score -= 2.5
        score -= abs(hh - pitch) / max(pitch, 1) * 1.0
        return score

    def _is_clear_straddle(y1: int, y2: int) -> bool:
        hh = y2 - y1
        if hh <= 4:
            return True
        mid = (y1 + y2) // 2
        band = max(2, hh // 6)
        c0, c1 = max(0, mid - band), min(rh, mid + band + 1)
        center_bg = float(row_bg[c0:c1].mean()) if c1 > c0 else 1.0
        return center_bg > 0.45

    def _score_phase(phase: int) -> tuple[float, int, int]:
        """均匀网格评分：固定 pitch 铺格。优先分隔线对齐 + 非骑缝格数。"""
        score = 0.0
        n = 0
        bad = 0
        y1 = phase
        if y1 > 0:
            y1 -= pitch
        while y1 < rh - pitch * 0.28:
            y2 = y1 + pitch
            vis_t = max(0, y1)
            vis_b = min(rh - 1, y2)
            hh = vis_b - vis_t
            if hh < pitch * 0.28:
                y1 += pitch
                continue
            # 网格线对齐背景缝
            if 0 <= y1 < rh:
                score += _edge_bg(max(0, y1)) * 3.0
            if 0 <= y2 < rh:
                score += _edge_bg(min(rh - 1, y2)) * 3.0
            interior = vis_t > 2 and vis_b < rh - 3 and hh >= pitch * 0.85
            if interior and _is_clear_straddle(vis_t, vis_b):
                bad += 1
                score -= 12.0
            else:
                score += _slot_quality(vis_t, vis_b) * 1.2 + 6.0
                n += 1
            y1 += pitch
        # 覆盖率：尽量铺满 ROI
        score += n * 4.0 - bad * 8.0
        return score, n, bad

    # 相位搜索：只评均匀网格
    step = max(1, pitch // 24)
    seeds = set(range(0, pitch, step))
    seeds.update(int(y) % pitch for y in bg_lines)
    seeds.update(int(y) % pitch for y in cands[:40])
    best_phase = 0
    best_score = -1e18
    best_n = 0
    best_bad = 99
    for phase in seeds:
        sc, n, bad = _score_phase(int(phase) % pitch)
        better = sc > best_score or (
            abs(sc - best_score) < 1e-6 and (bad < best_bad or (bad == best_bad and n > best_n))
        )
        if better:
            best_score, best_phase, best_n, best_bad = sc, int(phase) % pitch, n, bad

    for delta in range(-max(1, pitch // 3), max(1, pitch // 3) + 1):
        phase = (best_phase + delta) % pitch
        sc, n, bad = _score_phase(phase)
        better = sc > best_score or (
            abs(sc - best_score) < 1e-6 and (bad < best_bad or (bad == best_bad and n > best_n))
        )
        if better:
            best_score, best_phase, best_n, best_bad = sc, phase, n, bad

    # 整列相位吸附到深色/背景缝（只平移，不改单格高度）
    sep_anchors = sorted(set(bg_lines + dark_lines))
    if sep_anchors:
        snap_best, snap_score = best_phase, -1e18
        for delta in range(-max(4, pitch // 8), max(4, pitch // 8) + 1):
            phase = (best_phase + delta) % pitch
            y = phase
            if y > 0:
                y -= pitch
            align = 0.0
            while y < rh + pitch:
                if 0 <= y < rh:
                    # 最近锚点距离
                    d = min(abs(y - a) for a in sep_anchors)
                    if d <= max(5, pitch * 0.08):
                        align += 3.0 - d * 0.15
                    align += _edge_bg(int(y)) * 1.2
                y += pitch
            if align > snap_score:
                snap_score, snap_best = align, phase
        best_phase = snap_best

    # 按最佳相位均匀铺格：中间格一律保留，避免漏卡
    slots = []
    seps = []
    y1 = best_phase
    if y1 > 0:
        y1 -= pitch
    while y1 < rh - pitch * 0.28:
        y2 = y1 + pitch
        seps.append(max(0, min(rh - 1, y1)))
        vis_t = max(0, y1)
        vis_b = min(rh - 1, y2)
        hh = vis_b - vis_t
        y1 += pitch
        if hh < pitch * 0.28:
            continue
        partial = vis_t <= 1 or vis_b >= rh - 2 or hh < pitch * 0.78
        # 过高异常格（并进了 HOME）丢掉
        if hh > pitch * 1.35:
            continue
        interior = vis_t > 2 and vis_b < rh - 3
        # 仅边缘且几乎全是背景缝才丢；中间格与顶部半卡保留
        if (not interior) and hh < pitch * 0.55 and _is_clear_straddle(vis_t, vis_b):
            continue
        slots.append((left, top + vis_t, right - left - 1, hh, 0.5 if partial else 1.0))
    if y1 - pitch + pitch <= rh:
        seps.append(min(rh - 1, y1))

    debug["seps"] = [top + y for y in seps]
    debug["phase"] = best_phase

    slots.sort(key=lambda s: s[1])
    merged = []
    for s in slots:
        if not merged or abs(s[1] - merged[-1][1]) > pitch * 0.45:
            merged.append(s)
        else:
            cur = merged[-1]
            if abs(s[3] - pitch) < abs(cur[3] - pitch):
                merged[-1] = s

    merged = [s for s in merged if s[2] <= w * 0.45 or w < 700]
    merged = _refine_and_filter_slots(frame, merged, pitch, (left, top, right, bottom))

    # 结果稳定则锁定 pitch/宽度，供滚动后复用
    full_heights = [s[3] for s in merged if s[4] >= 0.9]
    if update_lock and len(full_heights) >= 2 and pitch:
        med = float(np.median(full_heights))
        if med >= pitch * 0.85 and float(np.std(full_heights)) <= max(12, med * 0.12):
            _LAST_GOOD_PITCH = int(round(med if abs(med - pitch) < 12 else pitch))
            _LAST_GOOD_WIDTH = int(right - left)
            _save_pitch_lock()

    debug["pitch"] = pitch
    debug["roi"] = (left, top, right, bottom)
    return merged, debug


def _auto_search_gacha(frame: np.ndarray):
    global _LAST_GOOD_PITCH, _LAST_GOOD_WIDTH
    _load_pitch_lock()
    # 寻参过程不写入锁；保留磁盘先验供半周期回退
    prior_pitch, prior_width = _LAST_GOOD_PITCH, _LAST_GOOD_WIDTH
    _LAST_GOOD_PITCH, _LAST_GOOD_WIDTH = None, None

    h = frame.shape[0]
    w = frame.shape[1]
    # 列表宽度差太大则丢弃旧 pitch（不同窗口缩放不能共用）
    probe = _auto_find_list_roi(frame)
    probe_w = max(1, probe[2] - probe[0])
    if prior_width and abs(probe_w - prior_width) > max(24, prior_width * 0.18):
        prior_pitch, prior_width = None, None

    # 覆盖常见窗口高度下的单卡高度（约 55~120）
    base_ranges = [
        (max(55, int(h * 0.09)), max(95, int(h * 0.16))),
        (max(65, int(h * 0.10)), max(110, int(h * 0.18))),
        (max(75, int(h * 0.11)), max(130, int(h * 0.22))),
    ]
    if w < 520:
        base_ranges = [
            (max(55, int(h * 0.10)), max(95, int(h * 0.18))),
            (max(65, int(h * 0.12)), max(110, int(h * 0.20))),
            (max(75, int(h * 0.13)), max(125, int(h * 0.22))),
        ]
    best = None
    best_slots = []
    best_debug = {}
    for dark in (45, 55, 70):
        for bright in (190, 200, 210):
            for bg in (45, 55, 65):
                for width_pct in (45, 55, 65):
                    for pmin, pmax in base_ranges:
                        slots, debug = _detect_gacha_slots(
                            frame,
                            auto_roi=1,
                            dark_thresh=dark,
                            bright_thresh=bright,
                            bg_ratio=bg,
                            width_pct=width_pct,
                            pitch_min=pmin,
                            pitch_max=pmax,
                            update_lock=False,
                        )
                        if not slots:
                            continue
                        pitch = debug.get("pitch")
                        if not pitch or pitch < h * 0.07:
                            continue
                        roi = debug.get("roi")
                        if roi is not None and frame.shape[1] > 700 and (roi[2] - roi[0]) > frame.shape[1] * 0.4:
                            continue
                        # ROI 过短（HOME/BACK 裁过头）直接丢
                        if roi is not None and (roi[3] - roi[1]) < h * 0.45:
                            continue
                        heights = [s[3] for s in slots if s[4] >= 0.9]
                        full_n = len(heights)
                        if full_n < 2:
                            continue
                        med = float(np.median(heights))
                        if med < pitch * 0.82:
                            continue
                        min_med = h / 8.5 if frame.shape[1] < 520 else h / 12
                        if med < min_med:
                            continue
                        # 排斥明显 2 倍周期（一格盖两卡）
                        if roi is not None and med > (roi[3] - roi[1]) / 3.5:
                            continue
                        consist = sum(1 for hh in heights if abs(hh - med) <= max(10, med * 0.12))
                        roi_h = (roi[3] - roi[1]) if roi else h
                        expected = max(1.0, roi_h / max(med, 1.0))
                        count_pen = abs(full_n - expected) * 10
                        if full_n > expected + 1.2:
                            count_pen += (full_n - expected) * 20
                        if full_n < expected - 1.5:
                            count_pen += (expected - full_n) * 14
                        coverage = sum(s[3] for s in slots) / max(roi_h, 1)
                        score = (
                            consist * 28
                            + full_n * 8
                            + min(coverage, 1.02) * 40
                            - abs(med - pitch) * 0.8
                            - float(np.std(heights)) * 3.0
                            - count_pen
                        )
                        # 格数明显超过 ROI/卡高 预期 → 半周期碎格
                        if full_n > expected + 0.8:
                            score -= (full_n - expected) * 18
                        if med > roi_h / 3.2:
                            score -= 35
                        # 顶部格像 HOME 则扣分
                        if slots:
                            top_patch = frame[
                                slots[0][1] : slots[0][1] + slots[0][3],
                                slots[0][0] : slots[0][0] + slots[0][2],
                            ]
                            if _slot_blue_ratio(top_patch) > 0.20:
                                score -= 25
                        if prior_pitch and abs(pitch - prior_pitch) <= max(10, prior_pitch * 0.1):
                            score += 30
                        if prior_pitch and abs(pitch * 2 - prior_pitch) <= 12:
                            score -= 40
                        if prior_pitch and abs(pitch - prior_pitch / 2) <= 10:
                            score -= 25
                        params = (dark, bright, bg, width_pct, pmin, pmax)
                        rec = (score, params)
                        if best is None or rec[0] > best[0]:
                            best = rec
                            best_slots = slots
                            best_debug = debug

    # 滚动后若估成半周期，强制用历史 pitch 重检
    if best is not None and prior_pitch and best_debug.get("pitch"):
        p = int(best_debug["pitch"])
        if abs(p * 2 - prior_pitch) <= max(12, prior_pitch * 0.12) or p < prior_pitch * 0.7:
            _LAST_GOOD_PITCH, _LAST_GOOD_WIDTH = prior_pitch, prior_width
            slots, debug = _detect_gacha_slots(
                frame,
                auto_roi=1,
                dark_thresh=best[1][0],
                bright_thresh=best[1][1],
                bg_ratio=best[1][2],
                width_pct=best[1][3],
                pitch_min=max(20, prior_pitch - 15),
                pitch_max=prior_pitch + 15,
                update_lock=True,
            )
            if len(slots) >= 2:
                _save_pitch_lock()
                return best, slots, debug

    if best is not None and best_debug.get("pitch") and len(best_slots) >= 2:
        _LAST_GOOD_PITCH = int(best_debug["pitch"])
        roi = best_debug.get("roi")
        if roi is not None:
            _LAST_GOOD_WIDTH = int(roi[2] - roi[0])
        _save_pitch_lock()
    else:
        _LAST_GOOD_PITCH, _LAST_GOOD_WIDTH = prior_pitch, prior_width
    return best, best_slots, best_debug


def _draw(frame, rects, status="", seps=None, roi=None):
    draw = frame.copy()
    if roi is not None:
        left, top, right, bottom = roi
        cv2.rectangle(draw, (left, top), (right - 1, bottom - 1), (255, 180, 0), 1)
    if seps:
        for y in seps:
            if 0 <= y < draw.shape[0]:
                cv2.line(draw, (0, int(y)), (min(30, draw.shape[1] - 1), int(y)), (0, 0, 255), 2)
    for i, (x, y, bw, bh, score) in enumerate(rects):
        color = (0, 220, 0) if score >= 0.9 else (0, 180, 255)
        cv2.rectangle(draw, (x, y), (x + bw, y + bh), color, 2)
        cv2.putText(
            draw,
            f"#{i + 1} {bw}x{bh}",
            (x + 4, min(y + 18, draw.shape[0] - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
        )
    cv2.putText(
        draw,
        f"Rect: {len(rects)}  {status}",
        (10, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (0, 220, 0),
        2,
    )
    return draw


async def do_work(browser: UserBrowser):
    await browser.update_frame()
    frame = browser._browser._frame
    if frame is None:
        browser.script_log("[矩形检测] 截图失败")
        return

    h, w = frame.shape[:2]
    browser.script_log(f"[矩形检测] 画面 {w}x{h}")

    MAX_DISP = 1200
    sc = min(MAX_DISP / w, MAX_DISP / h, 1.0)
    if sc < 1:
        disp = cv2.resize(frame, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
    else:
        disp = frame.copy()

    # 画面与滑块分两个窗口，避免滑块把预览挤扁
    VIEW = "RectDetector - preview | ESC save"
    CTRL = "RectDetector - controls"
    cv2.namedWindow(VIEW, cv2.WINDOW_NORMAL)
    cv2.namedWindow(CTRL, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(VIEW, max(320, int(w * sc)), max(240, int(h * sc)))
    cv2.resizeWindow(CTRL, 520, 420)
    # 控制窗放一块占位图，滑块挂在这个窗口上
    ctrl_panel = np.full((80, 520, 3), 40, dtype=np.uint8)
    cv2.putText(
        ctrl_panel,
        "R=refresh frame | ESC=save",
        (12, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (200, 200, 200),
        1,
    )
    cv2.imshow(VIEW, _draw(disp, [], "searching..."))
    cv2.imshow(CTRL, ctrl_panel)
    cv2.waitKey(1)

    _load_pitch_lock()
    if _LAST_GOOD_PITCH:
        browser.script_log(f"[矩形检测] 已加载历史 pitch={_LAST_GOOD_PITCH} width={_LAST_GOOD_WIDTH}")

    browser.script_log("[矩形检测] 自动搜索卡池参数…")
    gacha_best, gacha_slots, gacha_debug = _auto_search_gacha(frame)
    if gacha_best is None:
        gacha_params = (55, 200, 50, 50, 95, 145)
        gacha_slots, gacha_debug = _detect_gacha_slots(
            frame,
            dark_thresh=gacha_params[0],
            bright_thresh=gacha_params[1],
            bg_ratio=gacha_params[2],
            width_pct=gacha_params[3],
            pitch_min=gacha_params[4],
            pitch_max=gacha_params[5],
        )
    else:
        gacha_params = gacha_best[1]

    browser.script_log("[矩形检测] 自动搜索经典矩形参数…")
    classic_params, classic_rects = _auto_search_classic(frame)
    if classic_params is None:
        classic_params = (50, 5, 10, 20, 10)
        classic_rects = _detect_classic(frame, *classic_params)

    dark, bright, bg, width_pct, pmin, pmax = gacha_params
    ma, gt, ms, sol, gr = classic_params
    roi = gacha_debug.get("roi")
    browser.script_log(
        f"[矩形检测] 卡池: pitch={gacha_debug.get('pitch')} slots={len(gacha_slots)} "
        f"roi={roi} | 经典: grad={gt} area={ma} rects={len(classic_rects)}"
    )

    # 0=卡池 1=经典；其余滑块两种模式都会显示，按当前模式取用
    sliders = [
        ("mode", "0_mode(0gacha/1classic)", 0, 1),
        ("auto_roi", "1_auto_roi", 1, 1),
        ("roi_top", "2_roi_top%", 0, 40),
        ("roi_bottom", "3_roi_bot%", 0, 40),
        ("roi_left", "4_roi_left%", 0, 40),
        ("roi_right", "5_roi_right%", 0, 40),
        ("dark", "6_dark_thr", dark, 120),
        ("bright", "7_bright_thr", bright, 255),
        ("bg", "8_bg_ratio%", bg, 90),
        ("width", "9_width%", width_pct, 90),
        ("pmin", "A_pitch_min", pmin, 200),
        ("pmax", "B_pitch_max", pmax, 250),
        ("min_area", "C_min_area", min(ma, 500), 1000),
        ("grad_thresh", "D_grad_thr", gt, 100),
        ("min_size", "E_min_size", min(ms, 50), 100),
        ("solidity", "F_solidity", int(sol), 100),
        ("grad_ratio", "G_grad_rat", int(gr), 100),
    ]
    for _, label, val, maxv in sliders:
        cv2.createTrackbar(label, CTRL, int(val), maxv, lambda _: None)

    def _window_alive(name: str) -> bool:
        """窗口被点关闭后应立刻退出循环，避免对已销毁窗口 imshow 卡死。"""
        try:
            prop = cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE)
            # OpenCV: 窗口不存在时返回 -1
            return prop >= 0 and prop != 0
        except Exception:
            return False

    def get_params():
        if not _window_alive(CTRL):
            raise RuntimeError("controls closed")
        return tuple(cv2.getTrackbarPos(s[1], CTRL) for s in sliders)

    def run_detect(p, use_frame=None):
        src = frame if use_frame is None else use_frame
        mode = p[0]
        if mode == 0:
            pmin_v, pmax_v = p[10], p[11]
            if pmin_v >= pmax_v:
                pmax_v = pmin_v + 10
            slots, debug = _detect_gacha_slots(
                src,
                auto_roi=p[1],
                roi_top_pct=p[2],
                roi_bottom_pct=p[3],
                roi_left_pct=p[4],
                roi_right_pct=p[5],
                dark_thresh=max(1, p[6]),
                bright_thresh=max(1, p[7]),
                bg_ratio=max(1, p[8]),
                width_pct=max(10, p[9]),
                pitch_min=max(20, pmin_v),
                pitch_max=max(pmin_v + 10, pmax_v),
            )
            return slots, debug, "gacha"
        rects = _detect_classic(
            src,
            min_area=max(1, p[12]),
            grad_thresh=max(1, p[13]),
            min_size=max(1, p[14]),
            solidity_min=max(1, p[15]),
            grad_min_ratio=max(1, p[16]),
        )
        return rects, {"pitch": None, "seps": [], "roi": None}, "classic"

    def rebuild_disp(src):
        nonlocal disp, sc
        hh, ww = src.shape[:2]
        sc = min(MAX_DISP / ww, MAX_DISP / hh, 1.0)
        if sc < 1:
            disp = cv2.resize(src, (int(ww * sc), int(hh * sc)), interpolation=cv2.INTER_AREA)
        else:
            disp = src.copy()

    def show(rects, debug, tag=""):
        if not _window_alive(VIEW) or not _window_alive(CTRL):
            raise RuntimeError("window closed")
        seps = debug.get("seps") or []
        roi = debug.get("roi")
        pitch_v = debug.get("pitch")
        scaled = [
            (int(x * sc), int(y * sc), int(bw * sc), int(bh * sc), s)
            for x, y, bw, bh, s in rects
        ]
        scaled_seps = [int(y * sc) for y in seps]
        scaled_roi = None
        if roi is not None:
            l, t, r, b = roi
            scaled_roi = (int(l * sc), int(t * sc), int(r * sc), int(b * sc))
        extra = f"pitch={pitch_v}" if pitch_v is not None else tag
        status = f"{tag} {extra}".strip()
        cv2.imshow(VIEW, _draw(disp, scaled, status, scaled_seps, scaled_roi))
        cv2.imshow(CTRL, ctrl_panel)

    show(gacha_slots, gacha_debug, "AUTO gacha")
    browser.script_log(
        "[矩形检测] GUI：滚动后按 R 刷新；关闭任一窗口或 ESC 退出保存"
    )

    params_ui = get_params()
    cur_rects, cur_debug, cur_mode = gacha_slots, gacha_debug, "gacha"
    try:
        while True:
            if not _window_alive(VIEW) or not _window_alive(CTRL):
                break
            key = cv2.waitKeyEx(30)
            if key == 27:  # ESC
                break
            # 再检一次，防止 waitKey 期间窗口被关掉
            if not _window_alive(VIEW) or not _window_alive(CTRL):
                break
            # R / r：滚动列表后刷新画面并重检（保留 pitch 锁）
            if key in (ord("r"), ord("R")):
                try:
                    await browser.update_frame()
                    new_frame = browser._browser._frame
                    if new_frame is not None:
                        frame = new_frame
                        rebuild_disp(frame)
                        cur_rects, cur_debug, cur_mode = run_detect(get_params(), use_frame=frame)
                        show(cur_rects, cur_debug, f"{cur_mode} refresh")
                        browser.script_log(
                            f"[矩形检测] 已刷新 pitch={cur_debug.get('pitch')} slots={len(cur_rects)}"
                        )
                except Exception as e:
                    browser.script_log(f"[矩形检测] 刷新中断: {e}")
                    break
                continue

            try:
                new_p = get_params()
            except Exception:
                break
            if new_p != params_ui:
                params_ui = new_p
                try:
                    cur_rects, cur_debug, cur_mode = run_detect(params_ui)
                    show(cur_rects, cur_debug, cur_mode)
                except Exception as e:
                    browser.script_log(f"[矩形检测] 显示中断: {e}")
                    break
    finally:
        try:
            cv2.destroyWindow(VIEW)
        except Exception:
            pass
        try:
            cv2.destroyWindow(CTRL)
        except Exception:
            pass
        cv2.destroyAllWindows()
        cv2.waitKey(1)

    try:
        final_p = get_params()
    except Exception:
        final_p = params_ui
    try:
        cur_rects, cur_debug, cur_mode = run_detect(final_p)
    except Exception:
        pass
    final = _draw(
        frame,
        cur_rects,
        f"{cur_mode} pitch={cur_debug.get('pitch')}",
        cur_debug.get("seps"),
        cur_debug.get("roi"),
    )
    out_path = PROJECT_ROOT / "screenshots" / "rect_detection.png"
    out_path.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(out_path), final)
    browser.script_log(f"[矩形检测] 已保存 ({len(cur_rects)} 个, mode={cur_mode}) → {out_path}")
    for i, (x, y, bw, bh, score) in enumerate(cur_rects[:20]):
        browser.script_log(f"  [{i + 1}] ({x},{y}) {bw}x{bh}  s={score:.2f}")
    if len(cur_rects) > 20:
        browser.script_log(f"  ... 共 {len(cur_rects)} 个")
