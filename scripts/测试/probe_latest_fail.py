from pathlib import Path
import cv2
from backend.matcher.matcher import Matcher

frame = cv2.imread(r"screenshots/daily_stop/_latest_fail.png")
print("frame", None if frame is None else frame.shape)
m = Matcher()
base = Path("assets/images/DeepOne/DO日常")
names = [
    "home", "rank", "出击_logo", "出击", "jjc_logo", "room_logo", "ta_logo",
    "meiri", "meiri_logo", "meiri_skip", "meiri_skip_title", "meiri_skip_skip",
    "meiri_ok", "meiri_skip_attked",
]
for n in names:
    p = base / f"{n}.png"
    if not p.is_file():
        print(n, "MISSING")
        continue
    r = m.match(frame, p, threshold=0.8, match_type="image", use_orb=False)
    sc = float(getattr(r, "score", 0) or 0)
    print(f"{n:20s} ok={bool(r):5} score={sc:.3f} xy=({r.x},{r.y})")
