"""jjc_rankup Y 带 / 阈值扩展扫描（单次归档分析）。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.matcher.matcher import matcher  # noqa: E402

TPL = ROOT / "assets/images/DeepOne/DO日常/jjc_rankup.png"
STOP = ROOT / "screenshots/daily_stop"
TH = 0.8


def imread(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(path)
    return frame


def scan_frame(frame_path: Path) -> None:
    frame = imread(frame_path)
    fh, fw = frame.shape[:2]
    res = matcher.match(
        frame,
        template=TPL,
        match_type="image_multi",
        threshold=0.0,
        min_dist=20,
    )
    res = sorted(res or [], key=lambda r: -r["score"])

    print(f"\n{'='*60}")
    print(f"帧: {frame_path.name}  ({fw}x{fh})")
    print(f"候选总数: {len(res)}")

    zones = [
        ("顶栏区 y<10%", lambda y: y < fh * 0.10),
        ("游戏上沿 10%-15%", lambda y: fh * 0.10 <= y < fh * 0.15),
        ("中带 15%-55%", lambda y: fh * 0.15 <= y <= fh * 0.55),
        ("中下 35%-50%", lambda y: fh * 0.35 <= y <= fh * 0.50),
    ]
    for label, pred in zones:
        hits = [r for r in res if pred(r["y"]) and r["score"] >= TH]
        best = max(hits, key=lambda x: x["score"], default=None)
        if best:
            print(
                f"  [{label}] best={best['score']:.4f} "
                f"@({best['x']:.0f},{best['y']:.0f}) "
                f"y_frac={best['y']/fh:.3f}  n>={TH}={len(hits)}"
            )
        else:
            print(f"  [{label}] 无 >={TH} 命中")

    print("\nY_MIN 扫描 (Y_MAX=0.55, th=0.8):")
    for y_min_frac in (0.06, 0.08, 0.10, 0.12, 0.15, 0.18):
        y_lo, y_hi = fh * y_min_frac, fh * 0.55
        in_band = [r for r in res if y_lo <= r["y"] <= y_hi and r["score"] >= TH]
        top = [r for r in res if r["y"] < y_lo and r["score"] >= TH]
        best_in = max(in_band, key=lambda x: x["score"], default=None)
        print(
            f"  Y_MIN={y_min_frac:.2f} ({y_lo:.0f}px): "
            f"带内={len(in_band)}"
            + (f" best={best_in['score']:.4f}@y={best_in['y']:.0f}" if best_in else "")
            + f" | 顶栏误报={len(top)}"
        )

    print("\n阈值扫描 (当前 Y 带 15%-55%):")
    y_lo, y_hi = fh * 0.15, fh * 0.55
    for t in (0.99, 0.97, 0.95, 0.92, 0.90, 0.85, 0.82, 0.80, 0.78):
        n = sum(1 for r in res if y_lo <= r["y"] <= y_hi and r["score"] >= t)
        mark = " <- 当前" if abs(t - TH) < 0.001 else ""
        print(f"  t={t:.2f}: 带内命中={n}{mark}")


def check_related_templates(frame_path: Path) -> None:
    """同帧上结算相关模板 best score。"""
    img_dir = ROOT / "assets/images/DeepOne/DO日常"
    frame = imread(frame_path)
    print(f"\n--- 同帧其它模板 ({frame_path.name}) ---")
    for tpl in ("jjc_结算.png", "jjc_ok.png", "jjc_touch.png"):
        r = matcher.match(
            frame,
            template=img_dir / tpl,
            match_type="image",
            threshold=0.0,
        )
        if r:
            print(f"  {tpl}: best={r['score']:.4f} @({r['x']:.0f},{r['y']:.0f})")
        else:
            print(f"  {tpl}: 无匹配")


def main() -> None:
    names = [
        "光物-圣诞水剑_20260825_232635_error.png",
        "光物-圣诞水剑_20260825_231341_error.png",
    ]
    for name in names:
        p = STOP / name
        if p.is_file():
            scan_frame(p)
            check_related_templates(p)
        else:
            print(f"缺失: {p}")


if __name__ == "__main__":
    main()
