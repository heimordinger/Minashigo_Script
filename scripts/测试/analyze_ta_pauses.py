import json
from pathlib import Path

root = Path("screenshots/pseudo_record")
dirs = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
d = None
for x in dirs:
    if x.is_dir() and "191130" in x.name:
        d = x
        break
if d is None:
    d = dirs[0]
print("DIR", d.name)
print("files", [p.name for p in d.iterdir()])
tl = d / "timeline.jsonl"
events = []
with tl.open(encoding="utf-8") as f:
    for line in f:
        if line.strip():
            events.append(json.loads(line))
print("events", len(events), "total_t", events[-1].get("t") if events else None)
logs = [(float(e["t"]), e.get("msg") or "") for e in events if e.get("kind") == "log"]
print("--- logs ---")
for t, m in logs:
    print(f"{t:7.1f}s  {m[:130]}")

# gaps between logs
print("--- log gaps >=3s ---")
for i in range(1, len(logs)):
    dt = logs[i][0] - logs[i - 1][0]
    if dt >= 3:
        print(f"  +{dt:5.1f}s  {logs[i-1][1][:50]}  =>  {logs[i][1][:50]}")
