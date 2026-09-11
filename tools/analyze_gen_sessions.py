#!/usr/bin/env python3
"""跨 corpus/sessions 的生成质量统计工具（脚本生成 P0 第 1 项）。

对 backend/script_generator/corpus/sessions/ 下全部 *_generate 会话做聚合：
  1) 会话覆盖概览（生成/试运行/修订 到达率、模型、试运行终态、token）
  2) 校验错误聚类（Top 高频错误，按 session 去重计数）
  3) 每条错误按 backend.script_generator.patch_registry 打 covered/gap/llm 标
  4) 校验失败残留（多轮补修仍失败的会话）里未覆盖的错误类别 —— 即“新增确定性 patch 候选”
  5) 试运行日志 / 诊断中的失败信号关键词
输出：Markdown 报告（默认 docs/gen_quality_report.md），控制台打印摘要。

纯 stdlib；UTF-8。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ── 规范化 / 聚类 ──────────────────────────────────────────────

_PREFIX_RE = re.compile(r"^[A-Za-z_][\w.\-]*(?:@\d+)?[:：@]\s*")
_LINE_NO_RE = re.compile(r"第\s*\d+\s*行|第\d+行")
_LEAD_RE = re.compile(r"^[\s\-•*>\d\.\)、]+")


def _strip_bullet(line: str) -> str:
    s = _LEAD_RE.sub("", line).strip()
    for _ in range(3):  # 剥掉 handler/行号前缀
        m = _PREFIX_RE.match(s)
        if m:
            s = s[m.end():].strip()
        else:
            break
    return s


def normalize_error(line: str) -> str:
    """去掉函数名/行号/数字等易变部分，得到聚类键。

    细节：截断到第一个中文分号前的分句（长句带具体对策时保留差异），
    并剥掉首尾残留的全角括号/标点，避免同一条错误被拆成多个簇。
    """
    s = _strip_bullet(line)
    s = _LINE_NO_RE.sub("第N行", s)
    s = re.sub(r"\d+", "N", s)
    if len(s) > 80:
        cut = re.search(r"[；;。]", s)
        if cut:
            s = s[: cut.start()]
    s = re.sub(r"\s+", " ", s).strip(" 。；;:：,，)）】」』〉】\u3000\t")
    s = s.strip(" （(【[『〈<")
    return s[:140]


def classify_text(text: str):
    try:
        from backend.script_generator import patch_registry as pr
        return pr.classify_text(text)
    except Exception:
        return ("unknown", "other")


def match_registry_ids(text: str):
    try:
        from backend.script_generator import patch_registry as pr
        return [e["id"] for e in pr.match_registry(text)]
    except Exception:
        return []


def collect_validation_errors(sdir: Path) -> list[str]:
    """validation_*.txt + revise_summary 里『校验仍失败: x』的完整错误行。"""
    errs: list[str] = []
    for f in sorted(sdir.glob("validation*.txt")):
        try:
            for ln in f.read_text(encoding="utf-8").splitlines():
                t = ln.strip()
                if t.startswith("-"):
                    errs.append(t[1:].strip())
                elif t:
                    errs.append(t)
        except Exception:
            continue
    rs = sdir / "revise_summary.txt"
    if rs.is_file():
        try:
            for ln in rs.read_text(encoding="utf-8").splitlines():
                m = re.search(r"校验仍失败[:：]?\s*(.+)", ln)
                if m:
                    errs.append(m.group(1).strip())
        except Exception:
            pass
    return errs


def residual_failed(sdir: Path) -> bool:
    """修订后仍失败：revise_summary 含『校验仍失败』或存在非空 validation_post_revise.txt。"""
    rs = sdir / "revise_summary.txt"
    if rs.is_file():
        try:
            if any("校验仍失败" in ln for ln in rs.read_text(encoding="utf-8").splitlines()):
                return True
        except Exception:
            pass
    vp = sdir / "validation_post_revise.txt"
    if vp.is_file():
        try:
            if vp.read_text(encoding="utf-8").strip():
                return True
        except Exception:
            pass
    return False


def fix_rounds(sdir: Path) -> int:
    rs = sdir / "revise_summary.txt"
    if not rs.is_file():
        return 0
    try:
        txt = rs.read_text(encoding="utf-8")
    except Exception:
        return 0
    n = max([0] + [int(m) for m in re.findall(r"【补修·第(\d+)轮】", txt)])
    return n


def has_diagnosis_with_vision(sdir: Path) -> bool:
    d = sdir / "diagnosis.json"
    if not d.is_file():
        return False
    try:
        data = json.loads(d.read_text(encoding="utf-8"))
    except Exception:
        return False
    must = data.get("must_fix") or []
    return any("停帧画面" in str(x) for x in must)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=Path, default=None,
                    help="sessions 目录（默认 <root>/backend/script_generator/corpus/sessions）")
    ap.add_argument("--out", type=Path, default=None,
                    help="Markdown 报告输出（默认 <root>/backend/script_generator/regression/gen_quality_report.md）")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--no-write", action="store_true", help="只打印不写报告")
    ap.add_argument(
        "--promote",
        action="store_true",
        help="扫描后把高频校验错追加进 corpus/promoted/rules.json（只进不出）",
    )
    ap.add_argument("--promote-min", type=int, default=3, help="升格所需最少会话数")
    ap.add_argument("--promote-dry-run", action="store_true")
    args = ap.parse_args()

    sessions_dir = args.sessions or (_PROJECT_ROOT / "backend/script_generator/corpus/sessions")
    out_path = args.out or (_PROJECT_ROOT / "backend" / "script_generator" / "regression" / "gen_quality_report.md")
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))

    sdirs = sorted(p for p in sessions_dir.iterdir() if p.is_dir() and p.name.endswith("_generate"))
    print(f"[analyze] sessions={len(sdirs)} dir={sessions_dir}")

    # ── 逐会话特征 ──
    rows = []  # (session, flags dict)
    for sd in sdirs:
        meta = {}
        try:
            meta = json.loads((sd / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        flags = {
            "has_code": (sd / "code_generated.py").is_file(),
            "has_trial_log": (sd / "trial_log.txt").is_file(),
            "has_trial_log_pre": (sd / "trial_log_pre_revise.txt").is_file(),
            "has_revised": (sd / "code_post_revise.py").is_file(),
            "has_diagnosis": (sd / "diagnosis.json").is_file(),
            "has_feedback": (sd / "feedback.txt").is_file(),
            "diag_vision": has_diagnosis_with_vision(sd),
            "residual_fail": residual_failed(sd),
        }
        flags["fix_rounds"] = fix_rounds(sd)
        rows.append((sd, meta, flags))

    n = len(rows)
    gen = sum(1 for _, _, f in rows if f["has_code"])
    trial = sum(1 for _, _, f in rows if f["has_trial_log"])
    rev = sum(1 for _, _, f in rows if f["has_revised"])
    resid = sum(1 for _, _, f in rows if f["residual_fail"])
    diag = sum(1 for _, _, f in rows if f["has_diagnosis"])
    rounds_max = max([0] + [f["fix_rounds"] for _, _, f in rows])

    # 试运行终态
    tstat = Counter((m.get("trial_status") or "n/a") for _, m, _ in rows if m.get("trial_status"))
    model_c = Counter((m.get("model") or "n/a") for _, m, _ in rows if m.get("model"))
    tok_in = sum(int(m.get("tokens_in") or 0) for _, m, _ in rows)
    tok_out = sum(int(m.get("tokens_out") or 0) for _, m, _ in rows)

    # ── 校验错误聚类 ──
    cluster_sessions: dict[str, set[str]] = defaultdict(set)
    cluster_raw: dict[str, list[str]] = defaultdict(list)
    per_sess_err = 0
    for sd, _m, _f in rows:
        errs = collect_validation_errors(sd)
        if errs:
            per_sess_err += 1
        for e in errs:
            key = normalize_error(e) or e.strip()[:80]
            cluster_sessions[key].add(sd.name)
            if len(cluster_raw[key]) < 3:
                cluster_raw[key].append(e.strip()[:160])

    # 状态分布统计
    status_sess: Counter = Counter()          # status -> #sessions (每 session 每 status 至多计 1)
    status_sess_done: Counter = Counter()     # 仅 residual_fail 会话里
    cat_sess: Counter = Counter()
    for sd, _m, _f in rows:
        errs = collect_validation_errors(sd)
        seen_s, seen_c = set(), set()
        for e in errs:
            st, cat = classify_text(e)
            if st not in seen_s:
                status_sess[st] += 1
                seen_s.add(st)
            if cat not in seen_c:
                cat_sess[cat] += 1
                seen_c.add(cat)
        if not _f["residual_fail"]:
            continue
        seen2 = set()
        for e in errs:
            st, _ = classify_text(e)
            if st not in seen2:
                status_sess_done[st] += 1
                seen2.add(st)

    # 试运行日志关键词（每会话去重）
    log_kw = [
        ("超时/timeout", r"超时|timeout|se_time"),
        ("异常/Traceback", r"Traceback|NameError|AttributeError|TypeError|SyntaxError|异常|ERROR"),
        ("未覆盖/卡住", r"未覆盖|卡住|停滞|未命中|no match"),
        ("失败/退出", r"失败|failed|__exit__|已完成"),
    ]
    kw_sess = {k: set() for k, _ in log_kw}
    for sd, _m, _f in rows:
        if not _f["has_trial_log"] and not _f["has_trial_log_pre"]:
            continue
        txt = ""
        for fn in ("trial_log.txt", "trial_log_pre_revise.txt", "revise_summary.txt"):
            p = sd / fn
            if p.is_file():
                try:
                    txt += p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        for k, pat in log_kw:
            if re.search(pat, txt):
                kw_sess[k].add(sd.name)

    # ── 报告文本 ──
    L: list[str] = []
    A = L.append
    A("# Script Generator 生成质量分析报告（corpus/sessions）")
    A("")
    A(f"- 会话数：{n}（目录：`{sessions_dir.relative_to(_PROJECT_ROOT) if sessions_dir.is_relative_to(_PROJECT_ROOT) else sessions_dir}`）")
    A(f"- 覆盖：生成 {gen}/{n} · 试运行日志 {trial}/{n} · 修订 {rev}/{n} · 诊断 {diag}/{n}")
    A(f"- 修订后仍残留校验失败：**{resid}** 会话；单会话最大补修轮数：{rounds_max}")
    A(f"- 累计 token：in {tok_in:,} / out {tok_out:,}")
    A("")
    A("## 1. 试运行终态 / 模型")
    A("")
    A("| 终态 | 会话数 |")
    A("|---|---|")
    for k, c in tstat.most_common():
        A(f"| {k} | {c} |")
    A("")
    A("| 模型 | 会话数 |")
    A("|---|---|")
    for k, c in model_c.most_common(10):
        A(f"| {k} | {c} |")
    A("")
    A("## 2. 校验错误 Top（按命中会话数，每会话去重）")
    A("")
    A("| # | 会话数 | 类别(status) | 归一化错误 | 样例原文 |")
    A("|---|---|---|---|---|")
    ranked = sorted(cluster_sessions.items(), key=lambda kv: -len(kv[1]))
    for i, (key, sset) in enumerate(ranked[: args.top], 1):
        st, cat = classify_text(cluster_raw[key][0] if cluster_raw[key] else key)
        sample = (cluster_raw[key][0] if cluster_raw[key] else key).replace("|", "\\|")
        A(f"| {i} | {len(sset)} | {st}/{cat} | {key} | {sample[:110]} |")
    A("")
    A("## 3. 错误类别 → 修复策略（covered/gap/llm 会话数）")
    A("")
    A("| 类别 | 会话数 |")
    A("|---|---|")
    for k, c in cat_sess.most_common():
        A(f"| {k} | {c} |")
    A("")
    A("| 策略 | 会话数 | 其中修订后仍失败 |")
    A("|---|---|---|")
    for st in ("covered", "gap", "llm", "unknown"):
        A(f"| {st} | {status_sess.get(st, 0)} | {status_sess_done.get(st, 0)} |")
    A("")
    A("## 4. 试运行日志失败信号（每会话去重）")
    A("")
    A("| 信号 | 会话数 |")
    A("|---|---|")
    for k, sset in kw_sess.items():
        A(f"| {k} | {len(sset)} |")
    A("")
    A("## 5. 结论（自动摘要）")
    A("")
    resid_names = [sd.name for sd, _m, f in rows if f["residual_fail"]]
    if resid_names:
        A(f"- 修订后仍失败的 {len(resid_names)} 个会话：`{'`, `'.join(resid_names)}`。")
    if status_sess.get("gap", 0) or status_sess_done.get("gap", 0):
        A(f"- 有 {status_sess.get('gap', 0)} 个会话的错误落在 **gap** 类别（建议新增确定性 patch）；"
          f"其中修订后仍失败 {status_sess_done.get('gap', 0)} 个 —— 优先实现这些 patch 可减少 LLM 轮次。")
    if status_sess_done.get("covered", 0):
        A(f"- 有 {status_sess_done.get('covered', 0)} 个『修订后仍失败』会话的错误本属 **covered**"
          "（patch 未生效，通常是 revise 路径未传 explanation/plan，或归档生成于旧版"
          "『自由模式完全跳过本地 patch』的代码）。已修复 revise 路径补传 explanation/free_mode，"
          "建议以修复后新会话复测本指标。")
    if status_sess_done.get("llm", 0):
        A(f"- 有 {status_sess_done.get('llm', 0)} 个『修订后仍失败』会话的错误属 **llm** 语义类别（死循环上限/__exit__ 混淆等），建议增强提示约束与合规审查。")
    A("")
    A("> 口径：validation_*.txt 与 revise_summary 中『校验仍失败』仅在错误残留时写入，")
    A("> 因此‘覆盖数’为 55 个会话中经历过失败的子集，不代表全部生成会话的质量。")
    A("")

    txt = "\n".join(L)

    if not args.no_write:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(txt, encoding="utf-8")
        print(f"[analyze] report -> {out_path}")
    print(f"[analyze] gen={gen} trial={trial} revised={rev} residual_fail={resid} diag={diag} max_fix_rounds={rounds_max}")
    print(f"[analyze] status sessions: covered={status_sess.get('covered',0)} gap={status_sess.get('gap',0)} llm={status_sess.get('llm',0)} unknown={status_sess.get('unknown',0)}")
    print(f"[analyze] top clusters:")
    for key, sset in ranked[:12]:
        st, cat = classify_text(cluster_raw[key][0] if cluster_raw[key] else key)
        print(f"  x{len(sset):<3} [{st}/{cat}] {key}")

    if args.promote or args.promote_dry_run:
        try:
            from backend.script_generator.corpus_promote import promote_from_sessions

            promo = promote_from_sessions(
                sessions_dir=sessions_dir,
                min_sessions=args.promote_min,
                dry_run=not args.promote,
            )
            print(
                f"[promote] added={len(promo.get('added') or [])} "
                f"updated={len(promo.get('updated') or [])} "
                f"total={promo.get('total_rules')} "
                f"path={promo.get('path')} dry_run={promo.get('dry_run')}"
            )
            if args.promote and not args.no_write:
                A("")
                A("## 6. 语料升格（只进不出）")
                A("")
                A(
                    f"- 新增 `{len(promo.get('added') or [])}` 条，"
                    f"更新计数 `{len(promo.get('updated') or [])}` 条；"
                    f"库内共 `{promo.get('total_rules')}` 条 → `{promo.get('path')}`"
                )
                # 重写报告尾部
                txt = "\n".join(L)
                out_path.write_text(txt, encoding="utf-8")
        except Exception as e:
            print(f"[promote] failed: {e}")

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())