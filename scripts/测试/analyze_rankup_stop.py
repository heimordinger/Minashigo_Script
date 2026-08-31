"""Analyze archive + user rankup frame for jjc_ok / jjc_rankup."""
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
USER = Path(
    r"C:/Users/30241/.cursor/projects/f-Minashigo-script/assets/"
    r"c__Users_30241_AppData_Roaming_Cursor_User_workspaceStorage_2663938f02b2e6086503f0cb1942200d_images_image-c03ccc91-8951-4f8f-a440-3d0becc60c2e.jpg"
)


def imread(p: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def analyze(label: str, frame: np.ndarray) -> None:
    fh, fw = frame.shape[:2]
    print(f"\n=== {label} ({fw}x{fh}) ===")

    for tpl, th in [("jjc_rankup.png", 0.8), ("jjc_ok.png", 0.82)]:
        r = matcher.match(frame, template=IMG / tpl, match_type="image", threshold=0.0)
        r_th = matcher.match(frame, template=IMG / tpl, match_type="image", threshold=th)
        if r:
            print(
                f"  {tpl}: best={r['score']:.4f}@({r['x']:.0f},{r['y']:.0f}) "
                f"y_frac={r['y']/fh:.3f}"
            )
            print(f"    pass th={th}: {bool(r_th)}")
        else:
            print(f"  {tpl}: 无匹配")

    res = matcher.match(
        frame, template=IMG / "jjc_rankup.png",
        match_type="image_multi", threshold=0.0, min_dist=20,
    )
    res = sorted(res or [], key=lambda r: -r["score"])
    y_lo, y_hi = fh * 0.10, fh * 0.55
    in_band = [r for r in res if y_lo <= r["y"] <= y_hi and r["score"] >= 0.8]
    top = [r for r in res if r["y"] < fh * 0.10 and r["score"] >= 0.8]
    print(f"  rankup Y10-55% >=0.8: {len(in_band)} hits")
    if in_band:
        b = max(in_band, key=lambda x: x["score"])
        print(f"    best in-band: {b['score']:.4f}@y={b['y']:.0f}")
    print(f"  rankup top<10% >=0.8: {len(top)} (FP)")


def main() -> None:
    archives = sorted(STOP.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in archives[:3]:
        analyze(p.name, imread(p))
    if USER.is_file():
        analyze("user_image", imread(USER))


if __name__ == "__main__":
    main()
