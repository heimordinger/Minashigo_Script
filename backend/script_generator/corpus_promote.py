"""语料「只进不出」：会话高频校验错 → 升成 prompt 规则（可追加、不删除）。

扫描 corpus/sessions 校验错误聚类；达到阈值的簇写入
``corpus/promoted/rules.json``（append-only：同 id 只更新计数/样例，永不删条目）。

生成侧通过 ``format_promoted_rules_block`` 注入 Structure Contract。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CORPUS = Path(__file__).resolve().parent / "corpus"
_PROMOTED_PATH = _CORPUS / "promoted" / "rules.json"
_SESSIONS = _CORPUS / "sessions"

# 与 tools/analyze_gen_sessions 对齐的轻量归一化（避免强依赖 tools）
_PREFIX_RE = re.compile(r"^[A-Za-z_][\w.\-]*(?:@\d+)?[:：@]\s*")
_LINE_NO_RE = re.compile(r"第\s*\d+\s*行|第\d+行")
_LEAD_RE = re.compile(r"^[\s\-•*>\d\.\)、]+")


def promoted_rules_path() -> Path:
    return _PROMOTED_PATH


def _strip_bullet(line: str) -> str:
    s = _LEAD_RE.sub("", line).strip()
    for _ in range(3):
        m = _PREFIX_RE.match(s)
        if m:
            s = s[m.end() :].strip()
        else:
            break
    return s


def normalize_error(line: str) -> str:
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


def _rule_id(cluster_key: str) -> str:
    h = hashlib.sha1(cluster_key.encode("utf-8")).hexdigest()[:10]
    return f"auto_{h}"


def _collect_session_errors(sessions_dir: Path) -> dict[str, dict[str, Any]]:
    """cluster_key → {sessions:set, samples:list, status, category}."""
    try:
        from backend.script_generator.patch_registry import classify_text
    except Exception:
        def classify_text(t: str):  # type: ignore
            return "llm", "other"

    clusters: dict[str, dict[str, Any]] = {}
    if not sessions_dir.is_dir():
        return clusters
    for sd in sessions_dir.iterdir():
        if not sd.is_dir():
            continue
        errs: list[str] = []
        for f in sorted(sd.glob("validation*.txt")):
            try:
                for ln in f.read_text(encoding="utf-8").splitlines():
                    t = ln.strip().lstrip("-").strip()
                    if t:
                        errs.append(t)
            except Exception:
                continue
        rs = sd / "revise_summary.txt"
        if rs.is_file():
            try:
                for ln in rs.read_text(encoding="utf-8").splitlines():
                    m = re.search(r"校验仍失败[:：]?\s*(.+)", ln)
                    if m:
                        errs.append(m.group(1).strip())
            except Exception:
                pass
        seen: set[str] = set()
        for e in errs:
            key = normalize_error(e) or e.strip()[:80]
            if key in seen:
                continue
            seen.add(key)
            st, cat = classify_text(e)
            slot = clusters.setdefault(
                key,
                {
                    "sessions": set(),
                    "samples": [],
                    "status": st,
                    "category": cat,
                },
            )
            slot["sessions"].add(sd.name)
            if len(slot["samples"]) < 3:
                slot["samples"].append(e.strip()[:160])
            # 保留更「可修」的 status（covered < gap < llm 不覆盖已有 covered）
            order = {"covered": 0, "gap": 1, "llm": 2, "unknown": 3}
            if order.get(st, 9) < order.get(slot["status"], 9):
                slot["status"] = st
                slot["category"] = cat
    return clusters


def load_promoted_rules() -> list[dict[str, Any]]:
    path = _PROMOTED_PATH
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict) and r.get("active", True)]


def _load_all_promoted() -> dict[str, Any]:
    path = _PROMOTED_PATH
    if not path.is_file():
        return {"version": 1, "rules": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("rules"), list):
            return data
    except Exception:
        pass
    return {"version": 1, "rules": []}


def _cluster_to_rule_text(cluster_key: str, sample: str, category: str) -> str:
    """把聚类键写成可注入 prompt 的约束句（只进不出内容）。"""
    base = (sample or cluster_key).strip()
    # 去掉行号噪音
    base = _LINE_NO_RE.sub("", base)
    base = re.sub(r"\s+", " ", base).strip(" ：:。.")
    if not base:
        base = cluster_key
    return (
        f"[auto/{category}] 避免再犯：{base}。"
        "（来自会话高频校验；生成/修订时必须遵守。）"
    )


def promote_from_sessions(
    *,
    sessions_dir: Path | None = None,
    min_sessions: int = 3,
    include_covered: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """扫描会话，把高频错追加进 promoted/rules.json（永不删除已有条目）。"""
    sdir = sessions_dir or _SESSIONS
    clusters = _collect_session_errors(sdir)
    data = _load_all_promoted()
    existing = {
        str(r.get("cluster_key") or r.get("id")): r
        for r in (data.get("rules") or [])
        if isinstance(r, dict)
    }
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates = sorted(
        clusters.items(),
        key=lambda kv: -len(kv[1]["sessions"]),
    )
    for key, info in candidates:
        n = len(info["sessions"])
        if n < min_sessions:
            continue
        st = info.get("status") or "llm"
        if st == "covered" and not include_covered:
            skipped.append(f"{key[:40]} (covered,{n})")
            continue
        rid = _rule_id(key)
        sample = (info["samples"][0] if info["samples"] else key)
        rule_text = _cluster_to_rule_text(key, sample, info.get("category") or "other")
        prev = existing.get(key) or existing.get(rid)
        if prev:
            # 只进不出：可抬高计数与刷新样例，不删、不改 active→False
            prev["session_count"] = max(int(prev.get("session_count") or 0), n)
            prev["last_seen"] = now
            prev["sample"] = sample
            prev["rule"] = prev.get("rule") or rule_text
            prev["active"] = True
            updated.append(rid)
            continue
        entry = {
            "id": rid,
            "cluster_key": key,
            "rule": rule_text,
            "category": info.get("category") or "other",
            "patch_status": st,
            "session_count": n,
            "sample": sample,
            "first_seen": now,
            "last_seen": now,
            "active": True,
            "source": "sessions_auto",
        }
        data.setdefault("rules", []).append(entry)
        existing[key] = entry
        added.append(rid)

    data["updated_at"] = now
    data["version"] = int(data.get("version") or 1)
    if not dry_run and (added or updated):
        _PROMOTED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROMOTED_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped[:20],
        "total_rules": len(data.get("rules") or []),
        "path": str(_PROMOTED_PATH),
        "dry_run": dry_run,
    }


def format_promoted_rules_block(*, max_items: int = 12) -> str:
    """注入生成 prompt 的「会话升格规则」块；空则返回空串。"""
    rules = load_promoted_rules()
    if not rules:
        return ""
    # 按 session_count 降序
    ranked = sorted(
        rules,
        key=lambda r: (-int(r.get("session_count") or 0), str(r.get("id") or "")),
    )[:max_items]
    lines = [
        "## Session-promoted rules (append-only corpus)",
        "High-frequency validation failures from past generate sessions. Obey these:",
        "",
    ]
    for r in ranked:
        text = (r.get("rule") or "").strip()
        if not text:
            continue
        n = r.get("session_count")
        lines.append(f"- ({n} sessions) {text}")
    if len(lines) <= 3:
        return ""
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-sessions", type=int, default=3)
    ap.add_argument("--include-covered", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sessions", type=Path, default=None)
    args = ap.parse_args()
    result = promote_from_sessions(
        sessions_dir=args.sessions,
        min_sessions=args.min_sessions,
        include_covered=args.include_covered,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
