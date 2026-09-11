# Script Generator skills

Skills that belong to the **generate → trial → optimize** pipeline.

| Skill | Path | Purpose |
|-------|------|---------|
| `optimize-script-perf` | [optimize-script-perf/SKILL.md](optimize-script-perf/SKILL.md) | Pseudo-record driven FSM script performance loop |

**GUI entry:** Script Generator tab「4. 试运行」→ **脚本优化** (uses `backend/script_generator/optimize.py` + `pseudo_analyze.py`).

Cursor discovery: thin wrappers under `.cursor/skills/<name>/` point agents to read these files (Cursor only auto-loads `.cursor/skills/`).
