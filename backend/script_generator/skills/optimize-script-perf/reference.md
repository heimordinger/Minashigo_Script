# Reference — pseudo-record & analysis

## Summary fields

From `summary.txt` / `summary.json`:

| Field | Meaning |
|-------|---------|
| `total_s` | Wall clock of session |
| `black_s` | Sum of `black_off.dt_ms` spans |
| `effective_s` | `total_s − black_s` — **optimize this** |

## Useful timeline kinds

| kind | Use |
|------|-----|
| `log` | FSM / script messages |
| `match` | `ok`, `score`, `template`, `dt_ms` |
| `black_on` / `black_off` | Load spans (`dt_ms` on off) |
| `capture` / `sleep` | Overhead buckets |
| `keyframe` | Visual proof (`frames/*.jpg`; Windows: imencode path) |

## Analysis snippet

```python
from pathlib import Path
import json
root = Path("screenshots/pseudo_record")
dirs = sorted(
    [p for p in root.iterdir() if p.is_dir() and "孤儿登录" in p.name],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
d = dirs[0]
print((d / "summary.txt").read_text(encoding="utf-8")[:2000])
for line in (d / "timeline.jsonl").open(encoding="utf-8"):
    e = json.loads(line)
    if e.get("kind") == "log":
        print(f"{float(e['t']):7.1f}s  {e.get('msg')}")
```

## Instrumentation hooks (script side)

```python
browser.enable_pseudo_record(script_name="孤儿登录", force=True)

is_black, metrics = analyze_frame_blackness(frame)
rec.note_black_frame(is_black, **metrics)

rec.note_black_frame(False, reason="force_off")
browser.finish_pseudo_record(status=status)
```

## Known waste patterns (Minashigo)

1. **Menu/logo sticky loop** — cooldown after tap; prioritize skip/ok over menu.
2. **Crop realign while mode=full** — disable game-frame capture after fail.
3. **Full template gather during black** — light probe + long poll on black.
4. **Cached URL for DMM** — live `page.url` via `get_url`.
5. **Start dialog re-entry** — after start2 success, ignore start2-only unless start1 remains.

## Peer baselines (indicative)

| Script | Notes |
|--------|--------|
| DO登录_v2 | ~30–40s effective after wait/crop fixes |
| 孤儿登录_v2 | ~85s → ~74s effective after menu cooldown; cut double-start next |
