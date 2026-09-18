# Script Generator skills

Skills that belong to the **generate → trial → optimize** / **collab** pipeline.

| Skill | Path | Purpose |
|-------|------|---------|
| `collab-script-coach` | [collab-script-coach/SKILL.md](collab-script-coach/SKILL.md) | Collaborator 共创教练（阶段/原则） |
| `optimize-script-perf` | [optimize-script-perf/SKILL.md](optimize-script-perf/SKILL.md) | Pseudo-record driven FSM performance loop |
| `window-click-retarget` | [window-click-retarget/SKILL.md](window-click-retarget/SKILL.md) | 外壳/渲染分层导致「点不动」诊断（tools，不限 MuMu） |
| `vision-count-select` | [vision-count-select/SKILL.md](vision-count-select/SKILL.md) | Multi-instance count → pick max（文档保留；**当前未注册到协作默认 registry**） |

**正式版：** GUI 只渲染 `CollabViewUpdate`；玩法逻辑在 `backend/script_generator/collaborator/` + skills 注册表。窗口诊断为横切 tools，默认可用。

Cursor：`.cursor/skills/<name>/` 薄包装指向本目录。
