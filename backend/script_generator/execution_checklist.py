# -*- coding: utf-8 -*-
"""方案 C/D：动作级执行清单 — 抽出、注入、离线覆盖校验。"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional


_IMG_TOKEN_RE = re.compile(
    r"`([^`]+?\.(?:png|jpe?g))`|"
    r"(?<![A-Za-z0-9_])([A-Za-z0-9_\u4e00-\u9fff\-]+(?:/[A-Za-z0-9_\u4e00-\u9fff\-]+)*\.(?:png|jpe?g))",
    re.I,
)
_HELPER_REF_RE = re.compile(r"@([^\s（）()\]]+)")
_STEP_LINE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.\s、．]+(.+)$")


def _norm_stem(name: str) -> str:
    n = (name or "").strip().replace("\\", "/")
    lower = n.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        if lower.endswith(ext):
            return n[: -len(ext)]
    return n


def _imgs_in_text(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in _IMG_TOKEN_RE.finditer(text or ""):
        raw = (m.group(1) or m.group(2) or "").strip()
        k = _norm_stem(raw)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def extract_execution_checklist(
    explanation: str,
    *,
    source_dir: str = "",
) -> list[dict[str, Any]]:
    """从介绍抽出动作级清单项。

    每项: {id, section, text, images[], helpers[], required}
    """
    expl = explanation or ""
    items: list[dict[str, Any]] = []
    n = 0

    def add(section: str, text: str, *, required: bool = True) -> None:
        nonlocal n
        text = (text or "").strip()
        if not text or len(text) < 2:
            return
        imgs = _imgs_in_text(text)
        helpers = _HELPER_REF_RE.findall(text)
        # 无图无 helper 的纯叙述略降权，但仍列入
        n += 1
        items.append({
            "id": f"S{n:02d}",
            "section": section,
            "text": text[:200],
            "images": imgs,
            "helpers": helpers,
            "required": required and bool(imgs or helpers or re.search(
                r"点击|匹配|拖拽|等待|执行|返回|识别", text,
            )),
        })

    # 辅助步骤
    hm = re.search(
        r"辅助步骤[^\n]*\n([\s\S]*?)(?=\n场景标识：|\n图片说明：|\n任务流程：|\Z)",
        expl,
    )
    if hm:
        body = hm.group(1)
        current = ""
        helper_name = ""
        for line in body.splitlines():
            hm2 = re.match(r"[（(][a-z][）)]\s*(.+)$", line.strip())
            if hm2:
                if current:
                    add(f"helper:{helper_name}", current)
                helper_name = hm2.group(1).strip()
                current = helper_name
                continue
            sm = _STEP_LINE_RE.match(line.strip())
            if sm:
                if current and current != helper_name:
                    add(f"helper:{helper_name}", current)
                current = sm.group(2).strip()
                continue
            if line.strip().startswith("-"):
                if current:
                    add(f"helper:{helper_name}", current)
                current = line.strip().lstrip("-").strip()
                continue
            if line.strip() and current:
                current += " " + line.strip()
        if current:
            add(f"helper:{helper_name}", current)

    # 任务流程
    tm = re.search(
        r"任务流程：\n([\s\S]*?)(?=\n特殊规则：|\n## |\Z)",
        expl,
    )
    if tm:
        body = tm.group(1)
        parts = re.split(r"\n(?=[（(]\d+[）)])", body)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            head = re.match(r"[（(](\d+)[）)]\s*([^\n]+)", part)
            if not head:
                continue
            title = head.group(2).strip()
            section = f"task:{title}"
            add(section, title, required=False)
            for line in part.splitlines()[1:]:
                s = line.strip()
                if not s:
                    continue
                sm = _STEP_LINE_RE.match(s)
                if sm:
                    add(section, sm.group(2).strip())
                elif s.startswith("-") or s.startswith("·"):
                    add(section, s.lstrip("-·").strip())
                elif re.search(r"`[^`]+\.png`|点击|匹配|执行@", s):
                    add(section, s)

    return items


def format_checklist_for_prompt(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = [
        "## Execution checklist (assemble ALL required steps)",
        "Each required item must appear in code (`_img` / click / helper flow).",
        "Do not compress away middle steps (e.g. 2_back after attribute lock).",
        "",
        "| id | section | required | images | step |",
        "|----|---------|----------|--------|------|",
    ]
    for it in items[:80]:
        imgs = ", ".join(it.get("images") or []) or "-"
        req = "yes" if it.get("required") else "no"
        text = (it.get("text") or "").replace("|", "/")
        if len(text) > 60:
            text = text[:60] + "…"
        lines.append(
            f"| {it.get('id')} | {it.get('section')} | {req} | `{imgs}` | {text} |"
        )
    if len(items) > 80:
        lines.append(f"| … | | | | +{len(items) - 80} more |")
    return "\n".join(lines) + "\n"


def _collect_img_stems_from_code(code: str) -> set[str]:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return set()
    from backend.script_generator.agent import _collect_img_names, _norm_img_key
    return {_norm_img_key(n) for n in _collect_img_names(tree)}


def checklist_coverage_errors(
    code: str,
    items: list[dict[str, Any]] | None = None,
    *,
    explanation: str = "",
) -> list[str]:
    """离线覆盖：required 项的 images 是否在代码 `_img` 中出现。"""
    if items is None:
        items = extract_execution_checklist(explanation)
    if not items:
        return []
    stems = _collect_img_stems_from_code(code)
    if not stems:
        # 语法坏或无 _img — 交给其它校验
        return []
    bases = {s.rsplit("/", 1)[-1].lower() for s in stems}
    errors: list[str] = []
    missing_ids: list[str] = []
    for it in items:
        if not it.get("required"):
            continue
        imgs = it.get("images") or []
        if not imgs:
            continue
        ok = False
        for img in imgs:
            k = _norm_stem(img)
            base = k.rsplit("/", 1)[-1].lower()
            if k in stems or base in bases:
                ok = True
                break
            # 任一路径后缀匹配
            if any(s.endswith("/" + k) or s.endswith(k) for s in stems):
                ok = True
                break
        if not ok:
            missing_ids.append(
                f"{it.get('id')}({','.join(imgs[:3])})"
            )
    if missing_ids:
        preview = ", ".join(missing_ids[:10])
        more = f" 等 {len(missing_ids)} 项" if len(missing_ids) > 10 else ""
        errors.append(
            f"执行清单未覆盖（代码缺少对应 `_img`）: {preview}{more}"
        )
    # 特殊：介绍有 2_back 则代码必须出现
    expl = explanation or ""
    if re.search(r"2_back\.png|`进入战斗/2_back`|进入战斗/2_back\.png", expl):
        if not any(
            s.endswith("2_back") or s.endswith("/2_back") or s == "2_back"
            for s in stems
        ):
            errors.append(
                "执行清单硬项：介绍要求点击 进入战斗/2_back，代码中未见 `_img('…2_back')`"
            )
    return errors


def checklist_coverage_report(
    code: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """供方案 D：逐条 covered/missing（离线）。"""
    stems = _collect_img_stems_from_code(code)
    bases = {s.rsplit("/", 1)[-1].lower() for s in stems}
    covered: list[str] = []
    missing: list[str] = []
    for it in items:
        if not it.get("required"):
            continue
        imgs = it.get("images") or []
        if not imgs:
            covered.append(f"{it.get('id')}: (no image token)")
            continue
        ok = False
        for img in imgs:
            k = _norm_stem(img)
            base = k.rsplit("/", 1)[-1].lower()
            if k in stems or base in bases:
                ok = True
                break
        label = f"{it.get('id')}: {it.get('text', '')[:40]}"
        if ok:
            covered.append(label)
        else:
            missing.append(label + f" — need {imgs}")
    return {
        "ok": not missing,
        "covered": covered,
        "missing": missing,
        "mode": "offline_checklist",
    }


def dumps_checklist(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False, indent=2)
