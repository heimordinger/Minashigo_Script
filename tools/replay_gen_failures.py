#!/usr/bin/env python3
"""生成失败回放：对归档会话跑「补丁链 + 校验」，量化修复覆盖率。

用法:
    python tools/replay_gen_failures.py            # 最近 16 个会话
    python tools/replay_gen_failures.py --limit 40

输出: 控制台表格 + Markdown 报告（默认 backend/script_generator/regression/gen_failure_replay.md）

注意: 归档里没有 plan_struct（生成上下文），因此回放用 plan=None；
      真实管线带 plan 时命中率只会更高（plan 版表名对齐补丁在 GUI 侧生效）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.script_generator.agent import (  # noqa: E402
    apply_codegen_patches,
    validate_generated_code,
)

CODE_ORDER = [
    "code_generated.py",
    "code_failed.py",
    "code_post_revise.py",
    "code_post_optimize.py",
    "code_at_trial.py",
    "code_pre_revise.py",
]

TAGS = [
    ("table_name", r"状态表名应为|请改名"),
    ("timeout_table", r"_TIMEOUT"),
    ("keys", r"但状态表无此键|缺少键|缺少状态表"),
    ("helper", r"辅助步骤"),
    ("stub", r"空壳 handler"),
    ("image", r"图片文件不存在|幻觉|未引用该素材"),
    ("click_wait", r"会换场景时"),
    ("punct", r"中文标点"),
    ("syntax", r"语法错误|AST 解析失败"),
    ("state", r"缺少「未知」|缺少 STATES"),
    ("other", r"."),
]


def tag_of(msg: str) -> str:
    for name, pat in TAGS:
        if re.search(pat, msg):
            return name
    return "other"


def pick_code(d: Path):
    for name in CODE_ORDER:
        p = d / name
        if p.is_file() and p.stat().st_size > 0:
            return name, p.read_text(encoding="utf-8", errors="replace")
    return "", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=Path, default=ROOT / "backend/script_generator/corpus/sessions")
    ap.add_argument("--limit", type=int, default=16)
    ap.add_argument("--out", type=Path, default=ROOT / "backend/script_generator/regression/gen_failure_replay.md")
    args = ap.parse_args()

    dirs = sorted([d for d in args.sessions.iterdir() if d.is_dir()], key=lambda p: p.name, reverse=True)
    dirs = [d for d in dirs if d.name.endswith(("_generate", "_optimize"))][: args.limit]

    rows = []
    for d in dirs:
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        src = str(meta.get("source_dir") or "")
        expl_p = d / "explanation.txt"
        expl = expl_p.read_text(encoding="utf-8", errors="replace") if expl_p.is_file() else ""
        cname, code = pick_code(d)
        err_p = d / "generate_error.txt"
        gen_err = err_p.read_text(encoding="utf-8", errors="replace").splitlines()[0] if err_p.is_file() else ""
        row = {
            "session": d.name,
            "last": meta.get("last_event", ""),
            "code": cname or "-",
            "compiles": None,
            "before": [],
            "after_norm": None,
            "after_free": None,
            "residual": [],
            "gen_err": gen_err,
        }
        if code.strip():
            try:
                compile(code, "<replay>", "exec")
                row["compiles"] = True
            except SyntaxError:
                row["compiles"] = False
            if row["compiles"]:
                row["before"] = validate_generated_code(code, source_dir=src, explanation=expl)
                out_n, _ = apply_codegen_patches(code, source_dir=src, explanation=expl, free_mode=False)
                out_f, _ = apply_codegen_patches(code, source_dir=src, explanation=expl, free_mode=True)
                a_n = validate_generated_code(out_n, source_dir=src, explanation=expl)
                a_f = validate_generated_code(out_f, source_dir=src, explanation=expl)
                row["after_norm"] = a_n
                row["after_free"] = a_f
                row["residual"] = a_n
        rows.append(row)

    tag_counter: dict[str, int] = {}
    for r in rows:
        for m in r["residual"]:
            tag_counter[tag_of(m)] = tag_counter.get(tag_of(m), 0) + 1

    fixed = [r for r in rows if r["before"] and r["after_norm"] is not None and len(r["after_norm"]) == 0]
    improved = [r for r in rows if r["before"] and r["after_norm"] is not None and 0 < len(r["after_norm"]) < len(r["before"])]
    still = [r for r in rows if r["after_norm"]]

    L = ["# 生成失败回放报告（补丁链覆盖率）", ""]
    L.append(f"- 回放会话：{len(rows)}；其中可编译 {sum(1 for r in rows if r['compiles'])}")
    L.append(f"- 补丁链把错误清空：**{len(fixed)}**；部分减少：{len(improved)}；仍有残差：{len(still)}")
    L.append("- 残差类别：" + (", ".join(f"{k}×{v}" for k, v in sorted(tag_counter.items(), key=lambda kv: -kv[1])) or "无"))
    L.append("")
    L.append("| 会话 | last_event | 代码 | 编译 | 错误(前) | 错误(补丁后/常规) | 错误(补丁后/自由) | 残差类别 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        tags = ",".join(sorted({tag_of(m) for m in r["residual"]})) or "-"
        L.append(
            f"| {r['session']} | {r['last']} | {r['code']} | "
            f"{'OK' if r['compiles'] else ('FAIL' if r['compiles'] is False else '-')} | "
            f"{len(r['before'])} | "
            f"{('-' if r['after_norm'] is None else len(r['after_norm']))} | "
            f"{('-' if r['after_free'] is None else len(r['after_free']))} | {tags} |"
        )
    L.append("")
    L.append("> 口径：归档无 plan_struct，回放按 plan=None 校验；真实管线带 plan，")
    L.append("> 表名/键集类补丁（patch_task_table_contract 等）命中率更高。")
    txt = "\n".join(L) + "\n"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(txt, encoding="utf-8")
    print(txt)
    print(f"[replay] report -> {args.out}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())