"""Quick latency probe for matcher (+ adaptive ROI) without browser capture."""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from backend.matcher.hotspot_roi import adaptive_match, get_hotspot_store, normalize_template_key
from backend.matcher.matcher import Matcher


def find_frame():
    roots = [Path("screenshots"), Path("screenshot"), Path("user_data")]
    cands = []
    for r in roots:
        if r.exists():
            cands.extend(r.rglob("*.png"))
    for p in sorted(cands, key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
        img = cv2.imread(str(p))
        if img is not None and img.shape[0] >= 400 and img.shape[1] >= 800:
            return img, p
    return np.full((915, 1910, 3), 40, np.uint8), None


def main():
    frame, fp = find_frame()
    print(f"frame={None if fp is None else fp} shape={frame.shape}")
    store = get_hotspot_store()
    print(f"hotspot buckets={len(store._buckets)} enabled={store.enabled}")

    m = Matcher()
    base = Path("assets/images/DeepOne/DO日常")
    temps = ["home.png", "jjc_logo.png", "ta_logo.png", "jjc_ok.png", "room_ok.png"]

    # decode cost proxy for screenshot path (PNG encode size unknown; measure imdecode of saved)
    raw = cv2.imencode(".png", frame)[1].tobytes()
    t0 = time.perf_counter()
    _ = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    t1 = time.perf_counter()
    print(f"png_decode_proxy={(t1 - t0) * 1000:.1f}ms bytes={len(raw)}")

    for name in temps:
        tpath = base / name
        if not tpath.is_file():
            print("missing", name)
            continue
        tkey = normalize_template_key(tpath)
        for use_orb, label in ((False, "tmpl"), (True, "tmpl+orb")):
            t0 = time.perf_counter()
            r = m.match(frame, tpath, threshold=0.9, match_type="image", use_orb=use_orb)
            t1 = time.perf_counter()
            t2 = time.perf_counter()
            r2 = adaptive_match(
                m,
                frame,
                tpath,
                threshold=0.9,
                match_type="image",
                use_orb=use_orb,
                template_key=tkey,
                capture_mode="full",
                enabled=True,
            )
            t3 = time.perf_counter()
            print(
                f"{name:12s} {label:8s} "
                f"full={(t1 - t0) * 1000:6.1f}ms ok={bool(r)} score={getattr(r, 'score', None)} | "
                f"adaptive={(t3 - t2) * 1000:6.1f}ms ok={bool(r2)} x={getattr(r2, 'x', None)}"
            )

    # parallel-ish cost of 11 templates (scene probe), sequential wall
    specs = temps + ["出击_logo.png", "rank.png", "room_logo.png", "meiri_logo.png"]
    paths = [base / n for n in specs if (base / n).is_file()]
    t0 = time.perf_counter()
    for p in paths:
        m.match(frame, p, threshold=0.88, match_type="image", use_orb=True)
    t1 = time.perf_counter()
    print(f"scene_probe_seq_n={len(paths)} wall={(t1 - t0) * 1000:.0f}ms (~{((t1 - t0) / max(len(paths), 1)) * 1000:.0f}ms/img)")


if __name__ == "__main__":
    main()
