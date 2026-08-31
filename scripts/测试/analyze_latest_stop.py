from pathlib import Path
import sys
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.matcher.matcher import matcher  # noqa: E402

STOP = ROOT / "screenshots/daily_stop"
IMG = ROOT / "assets/images/DeepOne/DO日常"

fp = sorted(STOP.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[0]
frame = cv2.imdecode(np.fromfile(str(fp), dtype=np.uint8), cv2.IMREAD_COLOR)
fh, fw = frame.shape[:2]
print("latest:", fp.name)
print("size:", fw, "x", fh)

checks = [
    "jjc_ok", "jjc_rankup", "jjc_logo", "jjc_段位", "jjc_出击",
    "ta_logo", "rank", "home",
]
for tpl in checks:
    p = IMG / f"{tpl}.png"
    r0 = matcher.match(frame, template=p, match_type="image", threshold=0.0)
    if r0:
        yf = r0["y"] / fh
        print(f"  {tpl}: {r0['score']:.4f} @({r0['x']:.0f},{r0['y']:.0f}) y_frac={yf:.3f}")
    else:
        print(f"  {tpl}: none")

res = matcher.match(
    frame, template=IMG / "jjc_rankup.png",
    match_type="image_multi", threshold=0.0, min_dist=20,
)
res = sorted(res or [], key=lambda x: -x["score"])
y_lo, y_hi = fh * 0.10, fh * 0.55
in_band = [r for r in res if y_lo <= r["y"] <= y_hi and r["score"] >= 0.8]
print(f"rankup in-band (10-55%) >=0.8: {len(in_band)}")
if in_band:
    b = max(in_band, key=lambda x: x["score"])
    print(f"  best: {b['score']:.4f} @ y={b['y']:.0f}")
