"""jjc_rankup 边界值测试：对 daily_stop 归档帧扫分。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.matcher.matcher import matcher  # noqa: E402

IMG = ROOT / "assets/images/DeepOne/DO日常"
STOP = ROOT / "screenshots/daily_stop"
TPL = IMG / "jjc_rankup.png"

# 与 DO日常.py 一致
Y_MIN_FRAC = 0.10
Y_MAX_FRAC = 0.55
THRESHOLD = 0.8
MARGIN = 0.03
FLOOR = 0.8


def imread(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(f"decode failed: {path}")
    return frame


def analyze_frame(frame_path: Path) -> None:
    frame = imread(frame_path)
    fh, fw = frame.shape[:2]
    print(f"\n{'='*60}")
    print(f"帧: {frame_path.name}")
    print(f"尺寸: {fw}x{fh}")

    res = matcher.match(
        frame,
        template=TPL,
        match_type="image_multi",
        threshold=0.0,
        min_dist=20,
    )
    if not res:
        print("无候选（t=0 也空）")
        return

    res = sorted(res, key=lambda r: -r["score"])
    print(f"候选总数: {len(res)}")
    print("\nTop 15 候选 (score, x, y, y_frac):")
    for r in res[:15]:
        yf = r["y"] / fh
        zone = "IN" if Y_MIN_FRAC * fh <= r["y"] <= Y_MAX_FRAC * fh else "OUT"
        print(
            f"  {r['score']:.4f}  ({r['x']:4.0f},{r['y']:4.0f})  "
            f"y_frac={yf:.3f}  [{zone}]"
        )

    y_lo, y_hi = Y_MIN_FRAC * fh, Y_MAX_FRAC * fh
    in_band = [r for r in res if y_lo <= r["y"] <= y_hi]
    out_band = [r for r in res if r not in in_band]

    tp_best = max(in_band, key=lambda x: x["score"]) if in_band else None
    fp_best = max(out_band, key=lambda x: x["score"]) if out_band else None

    tp_max = tp_best["score"] if tp_best else 0.0
    fp_max = fp_best["score"] if fp_best else 0.0

    print(f"\n当前 Y 带 [{Y_MIN_FRAC:.2f}, {Y_MAX_FRAC:.2f}]  (px {y_lo:.0f}~{y_hi:.0f})")
    if tp_best:
        print(f"  带内最佳(视为 TP): {tp_best['score']:.4f} @ y={tp_best['y']:.0f}")
    else:
        print("  带内最佳(视为 TP): 无 —— 真 rankup 未进带或未匹配")
    if fp_best:
        print(f"  带外最佳(视为 FP): {fp_best['score']:.4f} @ y={fp_best['y']:.0f}")
    else:
        print("  带外最佳(视为 FP): 无")

    tp_min = min(r["score"] for r in in_band) if in_band else None
    print(f"\n边界分析:")
    print(f"  TP_min(带内最低) = {tp_min:.4f}" if tp_min else "  TP_min = N/A")
    print(f"  FP_max(带外最高) = {fp_max:.4f}")
    if tp_min is not None and fp_max < tp_min:
        gap = tp_min - fp_max
        suggested = max(FLOOR, fp_max + MARGIN)
        suggested = min(suggested, tp_min)
        print(f"  安全区间宽度 = {gap:.4f}")
        print(f"  建议 threshold = {suggested:.3f}  (FP_max+{MARGIN}, 地板{FLOOR})")
    else:
        print("  [!] 带内/带外分数重叠或无带内命中 -> 不能只靠阈值，需调 Y 带或重裁模板")

    print(f"\n阈值扫描 (带内命中数 / 带外命中数):")
    for t in [0.99, 0.97, 0.95, 0.92, 0.90, 0.85, 0.80, 0.78]:
        hi = sum(1 for r in in_band if r["score"] >= t)
        ho = sum(1 for r in out_band if r["score"] >= t)
        mark = " ← 当前" if abs(t - THRESHOLD) < 0.001 else ""
        print(f"  t={t:.2f}: in={hi}  out={ho}{mark}")

    # 模拟 _jjc_rankup_visible
    visible = any(
        r["score"] >= THRESHOLD and y_lo <= r["y"] <= y_hi for r in res
    )
    print(f"\n_jjc_rankup_visible(th={THRESHOLD}, Y带) = {visible}")


def main() -> None:
    if not TPL.is_file():
        print(f"模板不存在: {TPL}")
        return

    patterns = [
        "*圣诞水剑*error*.png",
        "*圣诞水剑*.png",
        "*error*.png",
    ]
    seen: set[str] = set()
    frames: list[Path] = []
    for pat in patterns:
        for p in sorted(STOP.glob(pat), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.name not in seen:
                seen.add(p.name)
                frames.append(p)

    if not frames:
        print(f"未找到归档帧: {STOP}")
        return

    print(f"模板: {TPL.name}")
    for fp in frames[:4]:
        analyze_frame(fp)


if __name__ == "__main__":
    main()
