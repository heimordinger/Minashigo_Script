"""Analyze latest pseudo_record for pause points vs previous run."""
from __future__ import annotations

import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path


def latest_dir() -> Path:
    root = Path("screenshots/pseudo_record")
    dirs = [d for d in root.iterdir() if d.is_dir() and (d / "summary.json").is_file()]
    return max(dirs, key=lambda d: d.stat().st_mtime)


def main() -> None:
    d = latest_dir()
    print("DIR", d.name)
    summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    print("total_s", summary.get("total_s"))
    print("by_kind", {k: v for k, v in (summary.get("by_kind") or {}).items()})
    print("totals", summary.get("totals"))

    events = []
    with (d / "timeline.jsonl").open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))

    logs = [(float(e["t"]), e.get("msg") or "") for e in events if e.get("kind") == "log"]

    # key phase markers
    print("--- phase logs ---")
    for t, msg in logs:
        if any(
            x in msg
            for x in (
                "步骤",
                "第",
                "完成",
                "离开编队",
                "战斗结束",
                "AUTO",
                "领取",
                "扫荡",
                "全部完成",
                "游戏帧",
                "伪录制",
                "观察",
                "等待",
                "点击",
                "进入",
                "过场",
                "ta_cishu",
                "jjc_end",
                "gift",
            )
        ):
            if len(msg) > 100:
                msg = msg[:100]
            print(f"{t:7.1f}s  {msg}")

    # long gaps between ANY events (true stalls)
    ts = [float(e["t"]) for e in events]
    gaps = []
    for i in range(1, len(ts)):
        dt = ts[i] - ts[i - 1]
        if dt >= 2.0:
            gaps.append((dt, ts[i - 1], ts[i], events[i - 1], events[i]))
    gaps.sort(reverse=True)
    print("--- top wall gaps >=2s between consecutive events ---")
    for dt, t0, t1, a, b in gaps[:25]:
        def brief(e):
            k = e.get("kind")
            if k == "log":
                return f"log:{(e.get('msg') or '')[:40]}"
            if k == "sleep":
                return f"sleep:{e.get('planned_s')}->{e.get('dt_ms')}ms"
            if k == "capture":
                return f"cap:{e.get('dt_ms')}ms mode={e.get('mode')}"
            if k == "match":
                tpl = (e.get("template") or "").split("/")[-1]
                return f"match:{tpl} ok={e.get('ok')} {e.get('dt_ms')}ms"
            return k

        print(f"  +{dt:5.2f}s  [{t0:.1f}->{t1:.1f}]  {brief(a)}  =>  {brief(b)}")

    # capture/match rates in AUTO-like window from logs
    auto_start = auto_end = None
    jjc_fight_s = jjc_fight_e = None
    for t, msg in logs:
        if "交由游戏 AUTO" in msg or "塔 AUTO 运行中，被动" in msg:
            auto_start = t
        if "AUTO 已结束" in msg:
            auto_end = t
        if "进入战斗/加载" in msg or "离开编队界面" in msg:
            jjc_fight_s = t
        if "战斗结束" in msg and jjc_fight_e is None and jjc_fight_s:
            jjc_fight_e = t

    def window_stats(name, a, b):
        if a is None or b is None or b <= a:
            print(f"{name}: n/a")
            return
        caps = [e for e in events if e.get("kind") == "capture" and a <= float(e["t"]) <= b]
        mats = [e for e in events if e.get("kind") == "match" and a <= float(e["t"]) <= b]
        fails = sum(1 for e in mats if not e.get("ok"))
        cms = [float(e.get("dt_ms") or 0) for e in caps if e.get("dt_ms")]
        mms = [float(e.get("dt_ms") or 0) for e in mats if e.get("dt_ms")]
        wall = b - a
        print(
            f"{name}: wall={wall:.1f}s caps={len(caps)} ({len(caps)/wall:.2f}/s) "
            f"cap_cpu={sum(cms)/1000:.1f}s matches={len(mats)} fail={fails} "
            f"match_cpu={sum(mms)/1000:.1f}s"
        )
        if cms:
            print(
                f"  capture ms mean={st.mean(cms):.0f} p50={st.median(cms):.0f} "
                f"p90={sorted(cms)[int(len(cms)*0.9)]:.0f}"
            )

    print("--- window stats ---")
    window_stats("jjc_fight", jjc_fight_s, jjc_fight_e)
    window_stats("tower_auto", auto_start, auto_end)

    # sleep planned vs actual
    sleeps = [e for e in events if e.get("kind") == "sleep"]
    bad = []
    for e in sleeps:
        planned = float(e.get("planned_s") or 0)
        actual = float(e.get("dt_ms") or 0) / 1000.0
        if planned > 0 and actual > planned + 1.0:
            bad.append((actual - planned, planned, actual, float(e["t"])))
    bad.sort(reverse=True)
    print(f"--- sleeps overrun >1s: {len(bad)}/{len(sleeps)} ---")
    for over, p, a, t in bad[:12]:
        print(f"  t={t:.1f} planned={p:.2f} actual={a:.2f} over=+{over:.2f}")

    # match fail templates
    fail = Counter()
    for e in events:
        if e.get("kind") == "match" and not e.get("ok"):
            tpl = (e.get("template") or "empty").split("/")[-1] or "empty"
            fail[tpl] += 1
    print("--- top fail templates ---")
    for tpl, n in fail.most_common(12):
        print(f"  {n:4d}  {tpl}")

    # capture modes
    modes = Counter(e.get("mode") for e in events if e.get("kind") == "capture")
    print("capture modes", dict(modes))


if __name__ == "__main__":
    main()
