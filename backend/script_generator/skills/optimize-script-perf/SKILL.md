---
name: optimize-script-perf
description: >-
  Optimize Minashigo FSM automation scripts using pseudo-record metrics
  (total/black/effective), staged matching, crop fallback, and wait tightening.
  Part of Script Generator generate+optimize loop. Use when optimizing login/daily
  scripts, analyzing pseudo_record, comparing black-screen vs judgment latency,
  or extending script generation with a performance revise pass.
---

# Optimize Minashigo script performance

> Canonical home: `backend/script_generator/skills/` (generate → trial → optimize).  
> Cursor entry: `.cursor/skills/optimize-script-perf/` redirects here.

Measure with **effective = total − black**. Do not treat game load black-screen as script slowness.

Golden references (read before rewriting):
- `scripts/Deep One/DO登录_v2.py`
- `scripts/孤儿/孤儿登录_v2.py`

Recorder APIs: `backend/automation/run_recorder.py` (`PseudoRecorder`, `analyze_frame_blackness`).

## Role in Script Generator

Flow: **generate → trial run → pseudo-record analyze → optimize** (GUI: 试运行页「脚本优化」).

Implementation: `pseudo_analyze.py` (`assess_optimization_ceiling`) + `optimize.py`.

When **near ceiling** (mostly AUTO/battle/black load): GUI warns before LLM; may skip AI revision.

## Workflow (follow in order)

```
- [ ] 1. Baseline: read script + 脚本解释 / assets
- [ ] 2. Instrument: pseudo-record + black detect (+ optional dense diag frames)
- [ ] 3. User runs one real-account session (not demo mode)
- [ ] 4. Analyze latest screenshots/pseudo_record/*_<script>/
- [ ] 5. Fix script-side waste only; leave irreducible load alone
- [ ] 6. Re-run; compare effective; stop when near peer baseline
```

### 1. Baseline

- Identify FSM states, templates, poll sleeps, whether every wait tick matches many images.
- Note login URL / DMM path if any (`dmm_login(eager=True)`, live `get_url`).

### 2. Instrument (if missing)

Mirror DO/orphan flags:

- `DEBUG_PSEUDO_RECORD = True` → `enable_pseudo_record(script_name=..., force=True)`
- `DETECT_BLACK_SCREEN = True` → `note_black_frame` on wait polls; `force_off` on leave wait / exit
- `DEBUG_WAIT_FRAMES = False` for normal runs; `True` only when diagnosing miss-match
- `finish_pseudo_record(status=...)` in `finally`
- Crop once via `_prepare_game_matching`; on fail set `use_game_frame_capture = False` and **stop periodic realign**

Black probe is **telemetry only** — never branch business clicks on black alone except to choose light vs full match set / long vs short poll.

### 3. User run

Ask user to run the script on a **real account**. Demo mode is for screenshots only.

Output dir: `screenshots/pseudo_record/{account}_{ts}_{script_name}/`
- `summary.txt` / `summary.json` — `total_s`, `black_s`, `effective_s`
- `timeline.jsonl` — `log`, `match`, `black_on`/`black_off`, `keyframe`

### 4. Analyze

Prefer latest matching `*_{script_name}` by mtime. Dump:

1. All `kind==log` lines with `t`
2. First `match` ok per template
3. `black_on` / `black_off` spans
4. Match ok/fail counts by template
5. Split: enter page → first start UI → after-start → rank/done

**Verdict rules:**

| Observation | Cause | Action |
|-------------|--------|--------|
| Scores stuck at ~0 until UI appears, then ≥0.99 | Game load | Do not “optimize” with more matching |
| Scores mid (0.6–0.84) while UI visible | Miss / threshold / crop | Fix templates or ROI; use diag frames |
| Same state re-entered many times (e.g. menu→tap→menu) | Script thrash | Cooldown / priority / settle |
| Repeated “滚动对齐” + `mode=full` | Crop thrash | One fail → full viewport, no retry loop |
| Huge `match` n during long black | Over-probing | Light probe (start+rank only) + long poll on black |

Report to user as a short table: wall / black / effective + top waste.

### 5. Fix patterns (apply selectively)

1. **Staged probe** — before UI: only scene ids (e.g. start2 + rank). After start: full set.
2. **Black → light probe + long `wait_poll_sleep`** — even if `_ui_seen_start`.
3. **Crop fallback** — fail once, disable `use_game_frame_capture`, cap realign fails.
4. **Priority** — reward buttons (skip/ok/close) before sticky chrome (menu/logo).
5. **Cooldownoldown** — after tapping sticky UI, don’t re-enter that state for N seconds.
6. **Tighten post-UI sleeps** — start disappear poll, click settle, `wait_poll_sleep_active`.
7. **Quiet matches** — `quiet=True` on wait probes; avoid logging every loop state.
8. **DMM** — `dmm_login(eager=True)`; never trust stale cached URL (use live `page.url` via `get_url`).
9. **Start once** — after successful start2, don’t re-enter start_dialog unless start1 still visible.

**Do not:**

- Optimize away black load with faster polling
- Spam keyframes (`diag` must respect interval)
- Commit `screenshots/`, `browser_data/`, real `accounts.json`

### 6. Re-measure

Compare `effective_s` (and after-start span). Peer baseline: DO登录 ~30–40s effective. Stop when remaining time is mostly black or necessary clicks.

## Checklist for new login-like scripts

- [ ] Pseudo-record + black summary logged
- [ ] Crop fail → full viewport, no realign thrash
- [ ] Eager DMM if applicable
- [ ] Staged + black-aware matching
- [ ] Sticky-UI cooldown / correct priority
- [ ] Start-dialog one-shot (no false re-entry)
- [ ] Short polls only when non-black and UI seen

## More detail

- Metric and timeline field notes: [reference.md](reference.md)
