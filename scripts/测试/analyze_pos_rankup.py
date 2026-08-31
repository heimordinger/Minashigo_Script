"""Analyze positive rankup frame from user."""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.matcher.matcher import matcher  # noqa: E402

TPL = ROOT / "assets/images/DeepOne/DO日常/jjc_rankup.png"
POS = Path(
    r"C:/Users/30241/.cursor/projects/f-Minashigo-script/assets/"
    r"c__Users_30241_AppData_Roaming_Cursor_User_workspaceStorage_2663938f02b2e6086503f0cb1942200d_images_image-64df18ad-856b-4fa7-ac80-b62e2234415e.jpg"
)
TH = 0.8
Y_MAX_FRAC = 0.55


def imread(path: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def main() -> None:
    frame = imread(POS)
    fh, fw = frame.shape[:2]
    res = matcher.match(
        frame, template=TPL, match_type="image_multi", threshold=0.0, min_dist=20
    )
    res = sorted(res or [], key=lambda r: -r["score"])

    print(f"frame: {fw}x{fh}")
    print(f"candidates: {len(res)}")
    print("\nTop 15:")
    for r in res[:15]:
        yf = r["y"] / fh
        in15 = 0.15 * fh <= r["y"] <= Y_MAX_FRAC * fh
        print(
            f"  {r['score']:.4f} ({r['x']:4.0f},{r['y']:4.0f}) "
            f"y_frac={yf:.3f} {'IN15-55' if in15 else 'OUT'}"
        )

    # title popup band ~10%-22% visually
    for lo, hi, name in [
        (0.06, 0.10, "顶栏 6-10%"),
        (0.10, 0.15, "弹窗标题 10-15%"),
        (0.15, 0.25, "标题下 15-25%"),
        (0.15, 0.55, "当前 Y 带"),
    ]:
        hits = [r for r in res if lo * fh <= r["y"] <= hi * fh]
        best = max(hits, key=lambda x: x["score"], default=None)
        if best:
            print(f"\n[{name}] best={best['score']:.4f} @y={best['y']:.0f} n={len(hits)}")
        else:
            print(f"\n[{name}] 无候选")

    print("\n_jjc_rankup_visible 模拟 (th=0.8):")
    for y_min in (0.06, 0.08, 0.10, 0.12, 0.15):
        y_lo = fh * y_min
        y_hi = fh * Y_MAX_FRAC
        hits = [r for r in res if r["score"] >= TH and y_lo <= r["y"] <= y_hi]
        best = max(hits, key=lambda x: x["score"], default=None)
        print(
            f"  Y_MIN={y_min:.2f}: visible={bool(hits)}"
            + (f" best={best['score']:.4f}@y={best['y']:.0f}" if best else "")
        )

    ok = ROOT / "assets/images/DeepOne/DO日常/jjc_ok.png"
    r = matcher.match(frame, template=ok, match_type="image", threshold=0.0)
    if r:
        print(f"\njjc_ok: {r['score']:.4f} @({r['x']:.0f},{r['y']:.0f}) y_frac={r['y']/fh:.3f}")
    else:
        print("\njjc_ok: 无匹配")


if __name__ == "__main__":
    main()
