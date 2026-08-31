"""Summarize hotspot frame modes and sample jitter."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path


def std(vs: list[float]) -> float:
    if len(vs) < 2:
        return 0.0
    m = sum(vs) / len(vs)
    return math.sqrt(sum((v - m) ** 2 for v in vs) / (len(vs) - 1))


def main() -> None:
    p = Path("user_data/match_hotspots.json")
    d = json.loads(p.read_text(encoding="utf-8"))
    buckets = d.get("buckets") or {}

    by_frame: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "buckets": 0, "roi": 0, "full": 0, "miss": 0}
    )
    stats = []

    for k, b in buckets.items():
        tpl, _, fk = k.partition("||")
        samples = b.get("samples") or []
        n = len(samples)
        by_frame[fk]["n"] += n
        by_frame[fk]["buckets"] += 1
        by_frame[fk]["roi"] += int(b.get("roi_hits") or 0)
        by_frame[fk]["full"] += int(b.get("full_hits") or 0)
        by_frame[fk]["miss"] += int(b.get("roi_misses") or 0)
        if n >= 3:
            xs = [float(s["x"]) for s in samples]
            ys = [float(s["y"]) for s in samples]
            sx, sy = std(xs), std(ys)
            stats.append(
                (
                    max(sx, sy),
                    sx,
                    sy,
                    n,
                    tpl.split("/")[-1],
                    fk,
                    sum(xs) / n,
                    sum(ys) / n,
                )
            )

    print("updated_at", d.get("updated_at"))
    print("bucket_count", len(buckets))
    print("--- by frame_key ---")
    for fk, v in sorted(by_frame.items(), key=lambda x: -x[1]["n"]):
        print(
            f"{fk}: buckets={v['buckets']} samples={v['n']} "
            f"roi={v['roi']} full={v['full']} miss={v['miss']}"
        )

    stats.sort(reverse=True)
    print("--- top jitter ---")
    for mx, sx, sy, n, short, fk, mx_, my_ in stats[:15]:
        print(
            f"std=({sx:.1f},{sy:.1f}) n={n} mean=({mx_:.0f},{my_:.0f}) "
            f"{short} [{fk}]"
        )

    if stats:
        mxs = sorted(s[0] for s in stats)
        print("--- jitter summary ---")
        print(
            f"buckets_ge3={len(stats)} max_std={mxs[-1]:.1f} "
            f"median_std={mxs[len(mxs)//2]:.1f} "
            f"p90={mxs[int(len(mxs)*0.9)]:.1f}"
        )
        print(f"high_jitter_std>=40px: {sum(1 for s in mxs if s >= 40)}")
        print(f"high_jitter_std>=80px: {sum(1 for s in mxs if s >= 80)}")


if __name__ == "__main__":
    main()
