"""jjc_ok on rankup popup - full viewport simulation."""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.matcher.matcher import matcher  # noqa: E402

IMG = ROOT / "assets/images/DeepOne/DO日常"
USER = Path(
    r"C:/Users/30241/.cursor/projects/f-Minashigo-script/assets/"
    r"c__Users_30241_AppData_Roaming_Cursor_User_workspaceStorage_2663938f02b2e6086503f0cb1942200d_images_image-c03ccc91-8951-4f8f-a440-3d0becc60c2e.jpg"
)


def imread(p: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def ok_scores(frame: np.ndarray, label: str) -> None:
    fh, fw = frame.shape[:2]
    print(f"\n--- {label} {fw}x{fh} ---")
    r0 = matcher.match(frame, template=IMG / "jjc_ok.png", match_type="image", threshold=0.0)
    if r0:
        print(f"  jjc_ok best={r0['score']:.4f}@({r0['x']:.0f},{r0['y']:.0f}) y_frac={r0['y']/fh:.3f}")
    for th in (0.82, 0.80, 0.78, 0.75, 0.70):
        r = matcher.match(frame, template=IMG / "jjc_ok.png", match_type="image", threshold=th)
        print(f"  th={th}: {'HIT' if r else 'miss'}")


def main() -> None:
    crop = imread(USER)
    ok_scores(crop, "user_crop")

    # simulate full FANZA viewport: pad top ~54px black bar
    fh, fw = crop.shape[:2]
    bar = np.zeros((54, fw, 3), dtype=np.uint8)
    bar[:] = (30, 30, 30)
    padded = np.vstack([bar, crop])
    ok_scores(padded, "crop+54px_top_bar")

    # scale to 1910x915 like runtime
    full = cv2.resize(padded, (1910, 915), interpolation=cv2.INTER_LINEAR)
    ok_scores(full, "scaled_1910x915")

    # rankup title on full
    r = matcher.match(full, template=IMG / "jjc_rankup.png", match_type="image", threshold=0.0)
    if r:
        print(f"  rankup best={r['score']:.4f}@y_frac={r['y']/915:.3f}")


if __name__ == "__main__":
    main()
