---
name: vision-count-select
description: >-
  Count repeated UI icons (e.g. Onmyoji realm-raid medals) and pick the richest
  target. Use when collaborating on 结界突破 / multi-instance template count /
  select-max flows. Prefer helper + vision_tools; never reinvent NMS in generated code.
---

# vision-count-select

## When

- User wants「打勋章最多」「选章最多的对手」or similar multi-slot count → click.
- Collaborator explore step after a 结界截图 / 窗口帧.

## Do

1. Call `backend.script_generator.vision_tools.multi_count_medals(frame_or_path)`.
2. Show `summary` + `debug_path` overlay to the user; wait for ack if counts look wrong.
3. Emit / reuse canonical script: `scripts/yys/结界_打最多章.py` via `build_jjtp_script(probe)`.
4. Validate assets with `list_jjtp_assets()`; if `挑战`/`金币` missing, tell user to keep `刷999` copies or drop into `结界/`.

## Don't

- Don't paste OpenCV `matchTemplate` / NMS blocks into generated FSM code.
- Don't click when `best` is null or count is 0 (empty board → wait / ask refresh).
- Don't invent 庭院→结界 navigation unless assets exist.

## Related

- Helper: `scripts/yys/结界勋章.py`
- Script: `scripts/yys/结界_打最多章.py`
- Tools: `vision_tools.multi_count_medals` / `match_probe` / `list_jjtp_assets`
