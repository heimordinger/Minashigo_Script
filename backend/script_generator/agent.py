"""
Script Generator Agent
======================
调用 LLM API，根据用户提供的脚本解释和图片，自动生成自动化脚本。
支持单次调用或 LangGraph 多步编排（plan → generate → validate → fix）。
"""

from __future__ import annotations

import ast
import base64
import json
import re
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# 配置 — 从 config.json 加载，支持热更新
# ═══════════════════════════════════════════════════════════════

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _load_config() -> dict:
    """加载配置 JSON"""
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def is_codegen_free_mode(override: Optional[bool] = None) -> bool:
    """自由模式：生成少约束；成品校验加严（见 validate_generated_code）。"""
    if override is not None:
        return bool(override)
    try:
        return bool(_load_config().get("defaults", {}).get("codegen_free_mode", False))
    except Exception:
        return False


def is_img_identifiers_only(override: Optional[bool] = None) -> bool:
    """生成阶段 _img() 仅用介绍里的标识 stem；不校验素材文件是否存在（路径本地对齐）。"""
    if override is not None:
        return bool(override)
    try:
        return bool(
            _load_config().get("defaults", {}).get("codegen_img_identifiers_only", True)
        )
    except Exception:
        return True


_FREE_MODE_BANNER = """## FREE MODE (ACTIVE)
Generation relaxed:
- Ignore numbered Rules, generic few-shot (except STRUCTURE PARADIGM), plan contract, HARD allowed-images in user message.
- Follow script explanation + Available API + mandatory structure paradigm skeleton.
- Local codegen patches OFF during generation.
- Post-generation validation is STRICT (FSM / branches / API); image file existence skipped when identifiers-only.
"""


def build_img_dir_line(source_dir: str = "") -> str:
    """根据所选图片目录生成 IMG_DIR 赋值行。"""
    if not (source_dir or "").strip():
        return "IMG_DIR = IMG_PATH / 'game' / 'script'"
    try:
        from core.path import IMG_PATH
        rel = Path(source_dir).resolve().relative_to(Path(IMG_PATH).resolve())
        parts = [f"'{p}'" for p in rel.parts]
        return "IMG_DIR = IMG_PATH / " + " / ".join(parts)
    except (ValueError, Exception):
        return f'IMG_DIR = Path(r"{source_dir}")'


_IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

# source_dir resolve key → (keys without ext, basename_lower → [keys])
_FOLDER_IMG_INDEX: dict[str, tuple[set[str], dict[str, list[str]]]] = {}


def _norm_img_key(name: str) -> str:
    """相对路径 key：统一 /，去掉扩展名（保留子目录）。"""
    n = (name or "").strip().replace("\\", "/")
    if not n:
        return ""
    lower = n.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        if lower.endswith(ext):
            return n[: -len(ext)]
    return n


def list_source_image_names(source_dir: str = "", *, limit: int = 400) -> list[str]:
    """列出素材目录相对路径（含子目录，/ 分隔），排序。"""
    root = Path(source_dir or "")
    if not root.is_dir():
        return []
    names: list[str] = []
    try:
        found: list[Path] = []
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in _IMG_EXTS:
                continue
            parts = p.relative_to(root).parts
            if any(part.startswith(".") for part in parts):
                continue
            found.append(p)
        for p in sorted(found, key=lambda x: str(x.relative_to(root)).replace("\\", "/").lower()):
            names.append("/".join(p.relative_to(root).parts))
            if len(names) >= limit:
                break
    except Exception:
        return []
    return names


def list_source_image_paths(source_dir: str = "", *, limit: int = 400) -> list[Path]:
    """列出素材目录下图片的绝对 Path（含子目录，顺序同 list_source_image_names）。"""
    root = Path(source_dir or "")
    if not root.is_dir():
        return []
    return [root / n for n in list_source_image_names(str(root), limit=limit)]


def image_rel_label(path: Path | str, source_dir: str = "") -> str:
    """相对 source_dir 的展示名（/ 分隔）；无法相对化时退回文件名。"""
    p = Path(path)
    root = Path(source_dir or "")
    if root.is_dir():
        try:
            return "/".join(p.resolve().relative_to(root.resolve()).parts)
        except Exception:
            try:
                return "/".join(p.relative_to(root).parts)
            except Exception:
                pass
    return p.name.replace("\\", "/")


def _folder_img_index(source_dir: str | Path) -> tuple[set[str], dict[str, list[str]]]:
    """keys（无扩展名）与 basename→keys 索引；带缓存。"""
    root = Path(source_dir or "")
    try:
        cache_key = str(root.resolve()) if root.exists() else str(root)
    except Exception:
        cache_key = str(root)
    hit = _FOLDER_IMG_INDEX.get(cache_key)
    if hit is not None:
        return hit
    keys = {_norm_img_key(n) for n in list_source_image_names(str(root))}
    by_base: dict[str, list[str]] = {}
    for k in keys:
        base = k.rsplit("/", 1)[-1].lower()
        by_base.setdefault(base, []).append(k)
    _FOLDER_IMG_INDEX[cache_key] = (keys, by_base)
    return keys, by_base


def clear_folder_img_index(source_dir: str | Path | None = None) -> None:
    if source_dir is None:
        _FOLDER_IMG_INDEX.clear()
        return
    root = Path(source_dir)
    try:
        _FOLDER_IMG_INDEX.pop(str(root.resolve()), None)
    except Exception:
        pass
    _FOLDER_IMG_INDEX.pop(str(root), None)


def _key_in_folder(
    key: str,
    folder_keys: set[str],
    by_base: dict[str, list[str]],
) -> bool:
    k = _norm_img_key(key)
    if not k:
        return False
    if k in folder_keys:
        return True
    base = k.rsplit("/", 1)[-1].lower()
    return len(by_base.get(base, [])) == 1


def format_source_image_tree(
    names: list[str],
    *,
    max_entries: int = 220,
) -> str:
    """把相对路径列表收成 ASCII 目录树（相对 IMG_DIR）。

    例::
        .
        ├── 1_logo.png
        ├── 进入战斗/
        │   ├── 2_team.png
        │   └── 属性/
        │       └── 1_dark_1.png
        └── 战斗结算/
            └── 1_bc.png
    """
    root: dict[str, dict | None] = {}
    for raw in names:
        parts = [p for p in str(raw or "").replace("\\", "/").split("/") if p]
        if not parts:
            continue
        cur: dict[str, dict | None] = root
        for i, part in enumerate(parts):
            is_file = i == len(parts) - 1
            if is_file:
                # list_source_image_names 已带 .png；勿叠成 .png.png
                key = (
                    part
                    if Path(part).suffix.lower() in _IMG_EXTS
                    else f"{part}.png"
                )
            else:
                key = part
            if is_file:
                cur[key] = None
            else:
                nxt = cur.get(key)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[key] = nxt
                cur = nxt

    lines: list[str] = ["."]

    def walk(node: dict[str, dict | None], prefix: str = "") -> None:
        items = sorted(
            node.items(),
            key=lambda kv: (0 if isinstance(kv[1], dict) else 1, kv[0].lower()),
        )
        for i, (name, child) in enumerate(items):
            last = i == len(items) - 1
            branch = "└── " if last else "├── "
            if child is None:
                lines.append(f"{prefix}{branch}{name}")
                continue
            lines.append(f"{prefix}{branch}{name}/")
            walk(child, prefix + ("    " if last else "│   "))

    walk(root)
    if len(lines) <= max_entries:
        return "\n".join(lines)
    shown = "\n".join(lines[:max_entries])
    return f"{shown}\n… (+{len(lines) - max_entries} more entries)"


def filter_source_images_by_explanation(
    names: list[str],
    explanation: str,
    source_dir: str = "",
) -> tuple[list[str], list[str]]:
    """磁盘素材按脚本介绍过滤：点名保留，未点名移除。

    返回 (kept_rel_paths, dropped_rel_paths)。
    介绍为空或抽不出任何图名时不过滤（全量返回），避免误删。
    「同理 / 六种属性」时：若某子目录下已有点名图，则保留同目录其余文件
    （如只写了 1_dark_1，仍保留 进入战斗/属性/ 下其它属性图）。
    """
    imgs = [n for n in (names or []) if str(n).strip()]
    expl = (explanation or "").strip()
    if not imgs or not expl:
        return imgs, []

    expl_keys = _stems_mentioned_in_explanation(expl, source_dir)
    if not expl_keys:
        return imgs, []

    disk_keys = {_norm_img_key(n): n for n in imgs}
    expl_bases = {k.rsplit("/", 1)[-1].lower() for k in expl_keys if k}

    keep_keys: set[str] = set()
    for key, _raw in disk_keys.items():
        base = key.rsplit("/", 1)[-1].lower()
        if key in expl_keys:
            keep_keys.add(key)
            continue
        # 介绍写了相对路径或仅 basename，与磁盘 basename 对上
        if base in expl_bases:
            keep_keys.add(key)
            continue
        for ek in expl_keys:
            if key == ek or key.endswith("/" + ek) or ek.endswith("/" + key):
                keep_keys.add(key)
                break

    # 属性「同理」：点名过属性目录下任一文件 → 保留该目录全部
    if re.search(r"同理|六种属性|其他五种属性|五种属性", expl):
        attr_parents: set[str] = set()
        for k in keep_keys:
            if "/属性/" in k.replace("\\", "/") or k.startswith("属性/"):
                parent = k.rsplit("/", 1)[0]
                if parent:
                    attr_parents.add(parent)
        if attr_parents:
            for key in disk_keys:
                parent = key.rsplit("/", 1)[0] if "/" in key else ""
                if parent in attr_parents:
                    keep_keys.add(key)

    kept = [disk_keys[k] for k in sorted(disk_keys) if k in keep_keys]
    # 保持原排序（与 list_source_image_names 一致）
    kept = [n for n in imgs if _norm_img_key(n) in keep_keys]
    dropped = [n for n in imgs if _norm_img_key(n) not in keep_keys]
    return kept, dropped


_PART_BTN_RE = re.compile(r"按钮|点击|点按")
_PART_ID_RE = re.compile(r"标识图|作为.{0,12}标识|场景标识|识别图")
_PART_SCENE_RE = re.compile(
    r"界面[「\[]([^」\]]+)[」\]]|可作为[「\[]([^」\]]+)[」\]]"
)


def _part_meta_from_explanation(rel_file: str, explanation: str) -> tuple[str, str]:
    """从介绍行推断零件 role / 短标签。返回 (role, label)。"""
    key = _norm_img_key(rel_file)
    base = key.rsplit("/", 1)[-1] if key else ""
    file_variants = {
        rel_file.replace("\\", "/"),
        f"{key}.png" if key else "",
        f"{base}.png" if base else "",
        key,
        base,
    }
    file_variants = {v for v in file_variants if v}
    expl = explanation or ""
    best: tuple[int, str, str] | None = None  # score, role, label

    for line in expl.splitlines():
        low = line.replace("\\", "/")
        if not any(v in low for v in file_variants):
            continue
        role = "other"
        score = 1
        if re.search(r"：\s*按钮|按钮；", line):
            role = "button"
            score += 4
        elif _PART_ID_RE.search(line):
            role = "id"
            score += 4
        elif _PART_BTN_RE.search(line):
            role = "button"
            score += 1
        if "可作为" in line or "标识图" in line:
            score += 3
            if role == "other":
                role = "id"
        if "界面「" in line or "界面[" in line:
            score += 2
        if "||" in line:
            score -= 3  # 并行匹配列表，标签噪声大
        m = _PART_SCENE_RE.search(line)
        scene = ""
        if m:
            scene = (m.group(1) or m.group(2) or "").strip()
        desc = ""
        for sep in ("：", ":"):
            if sep in line:
                desc = line.split(sep, 1)[-1].strip().strip("`")
                break
        if not desc:
            desc = line.strip()
        desc = re.sub(r"\s+", " ", desc)
        if len(desc) > 36:
            desc = desc[:36] + "…"
        if scene and desc:
            label = f"{scene} · {desc}"
        elif scene:
            label = scene
        elif desc:
            label = desc
        else:
            continue
        cand = (score, role, label)
        if best is None or cand[0] > best[0]:
            best = cand

    if best:
        return best[1], best[2]
    parent = key.rsplit("/", 1)[0] if "/" in key else ""
    label = f"{parent}/{base}" if parent else base
    return "other", label


def build_image_parts(
    source_dir: str = "",
    *,
    names: list[str] | None = None,
    explanation: str = "",
    identifiers_only: Optional[bool] = None,
) -> tuple[list[dict], list[str], list[str], dict]:
    """构建封闭 IMAGE PARTS 表。

    返回 (parts, dropped, pending, meta)。
    part: {id, path, file, role, label}；path 为 `_img` 实参（无扩展名）。
    meta: {all_count, ambiguous, banned_chrome}
    """
    ids_only = is_img_identifiers_only(identifiers_only)
    all_imgs = list(names) if names is not None else list_source_image_names(source_dir)
    imgs, dropped = filter_source_images_by_explanation(
        all_imgs, explanation or "", source_dir,
    )
    folder_keys = {_norm_img_key(n) for n in all_imgs}
    by_base: dict[str, list[str]] = {}
    for k in folder_keys:
        by_base.setdefault(k.rsplit("/", 1)[-1].lower(), []).append(k)
    expl_keys = _stems_mentioned_in_explanation(explanation or "", source_dir)
    pending = (
        sorted(s for s in expl_keys if not _key_in_folder(s, folder_keys, by_base))
        if ids_only
        else []
    )
    banned_chrome = sorted(
        s for s in _PARADIGM_CHROME_STEMS
        if not _key_in_folder(s, folder_keys, by_base)
    )
    allow_keys = {_norm_img_key(n) for n in imgs}
    allow_by_base: dict[str, list[str]] = {}
    for k in allow_keys:
        allow_by_base.setdefault(k.rsplit("/", 1)[-1].lower(), []).append(k)
    ambiguous = sorted(
        f"{base}.png → " + " | ".join(f"{k}.png" for k in paths)
        for base, paths in allow_by_base.items()
        if len(paths) > 1
    )
    parts: list[dict] = []
    for i, raw in enumerate(imgs):
        path = _norm_img_key(raw)
        file_name = raw.replace("\\", "/")
        if Path(file_name).suffix.lower() not in _IMG_EXTS:
            file_name = f"{path}.png"
        role, label = _part_meta_from_explanation(file_name, explanation or "")
        parts.append({
            "id": f"P{i + 1:02d}",
            "path": path,
            "file": file_name,
            "role": role,
            "label": label,
        })
    meta = {
        "all_count": len(all_imgs),
        "ambiguous": ambiguous,
        "banned_chrome": banned_chrome,
    }
    return parts, dropped, pending, meta


def format_image_parts_block(
    parts: list[dict],
    *,
    dropped: list[str] | None = None,
    pending: list[str] | None = None,
    selected_ids: list[str] | None = None,
    all_count: int = 0,
    ambiguous: list[str] | None = None,
    banned_chrome: list[str] | None = None,
    kept_files: list[str] | None = None,
) -> str:
    """把零件表格式化为 prompt 块（assemble-only）。"""
    if not parts and not pending:
        return (
            "## IMAGE PARTS (closed set — assemble only)\n"
            "(no image folder / empty after intro filter — do NOT invent `_img` names; "
            "use only relative paths that appear in the script explanation if any.)\n"
        )

    lines = [
        "## IMAGE PARTS (closed set — assemble only)",
        "You are assembling a script from IMAGE PARTS + explanation steps, "
        "NOT inventing assets.",
        "Each `_img(...)` / `register_guard(...)` argument MUST be the `path` of a "
        "row below (relative to IMG_DIR, no `.png`).",
        "Copy path verbatim — never invent flat basenames, MENU chrome, or names "
        "from other scripts.",
        "Do NOT infer button purpose from filename stems (出击/决定/ok/…). "
        "Use the role/label column and explanation steps only.",
        "If a step has no matching part: `script_log` / skip — do NOT fabricate.",
        "",
        "| id | role | label | `_img(path)` |",
        "|----|------|-------|--------------|",
    ]
    for p in parts:
        pid = p.get("id") or ""
        role = p.get("role") or "other"
        label = (p.get("label") or "").replace("|", "/")
        path = p.get("path") or ""
        lines.append(f"| {pid} | {role} | {label} | `_img('{path}')` |")

    if dropped:
        sample = ", ".join(_norm_img_key(n) for n in dropped[:12])
        more = f" … +{len(dropped) - 12}" if len(dropped) > 12 else ""
        kept_n = len(parts)
        lines.extend([
            "",
            "### Intro filter",
            f"Kept {kept_n} / {all_count or kept_n} on-disk files named in the introduction; "
            f"omitted {len(dropped)} not mentioned (NOT in PARTS): {sample}{more}",
        ])

    files = kept_files or [p.get("file") or f"{p.get('path')}.png" for p in parts]
    if files:
        tree = format_source_image_tree([str(f) for f in files if f])
        lines.extend([
            "",
            "### Folder tree (relative to IMG_DIR — intro-filtered)",
            "```",
            tree,
            "```",
        ])

    valid_ids = {str(p.get("id") or "") for p in parts}
    sel = [s for s in (selected_ids or []) if s in valid_ids]
    if sel:
        by_id = {str(p.get("id")): p for p in parts}
        lines.extend([
            "",
            "### SELECTED PARTS (HARD — only these may appear in this script's `_img`)",
            "Stage-1 selection; assemble code using ONLY these part paths:",
        ])
        for sid in sel:
            p = by_id[sid]
            lines.append(
                f"- [{sid}] {p.get('role')}: `_img('{p.get('path')}')` — {p.get('label')}"
            )
        # 未选中的零件仍列在总表，但明确禁止使用
        unused = [p for p in parts if str(p.get("id")) not in set(sel)]
        if unused:
            lines.append(
                f"( {len(unused)} other catalog parts exist above but are NOT selected "
                "— do not use them unless a SELECTED path is insufficient and you "
                "script_log the gap. Prefer SELECTED only.)"
            )

    if ambiguous:
        amb_lines = "\n".join(f"- {a}" for a in ambiguous[:20])
        more = f"\n- … +{len(ambiguous) - 20} more" if len(ambiguous) > 20 else ""
        lines.extend([
            "",
            "### Ambiguous basenames (MUST use full relative path)",
            amb_lines + more,
        ])
    if pending:
        pend_lines = "\n".join(f"- {s}.png" for s in pending[:40])
        more = f"\n- … +{len(pending) - 40} more" if len(pending) > 40 else ""
        lines.extend([
            "",
            "### Named in introduction but missing from folder (pending local align)",
            "Use sparingly; prefer skip / script_log if unsure. Do NOT invent a substitute name.",
            pend_lines + more,
        ])
    lines.extend([
        "",
        "### FORBIDDEN",
        "- Any path not in IMAGE PARTS (or pending).",
        "- On-disk files omitted by intro filter (system MENU / unused chrome, etc.).",
        "- Copying nav-chrome from other scripts when not listed as a part.",
        "- Inventing a flat basename when the real file lives under a subdirectory.",
    ])
    if banned_chrome:
        lines.append(
            "- Explicitly banned here (not in folder): "
            + ", ".join(f"{s}.png" for s in banned_chrome)
        )
    lines.append(
        "- Missing control → ALLOWED/SELECTED path or `script_log` — NEVER fabricate."
    )
    return "\n".join(lines) + "\n"


def allowed_images_block(
    source_dir: str = "",
    *,
    names: list[str] | None = None,
    identifiers_only: Optional[bool] = None,
    explanation: str = "",
    selected_part_ids: list[str] | None = None,
) -> str:
    """生成前素材硬白名单：磁盘 ∩ 介绍 → IMAGE PARTS 封闭零件表。

    selected_part_ids: 选图阶段选定的 Pxx；写入后 `_img` 应只用这些零件。
    """
    parts, dropped, pending, meta = build_image_parts(
        source_dir,
        names=names,
        explanation=explanation or "",
        identifiers_only=identifiers_only,
    )
    kept_files = [p.get("file") or f"{p.get('path')}.png" for p in parts]
    return format_image_parts_block(
        parts,
        dropped=dropped,
        pending=pending,
        selected_ids=selected_part_ids,
        all_count=int(meta.get("all_count") or 0),
        ambiguous=list(meta.get("ambiguous") or []),
        banned_chrome=list(meta.get("banned_chrome") or []),
        kept_files=kept_files,
    )


def parse_image_part_selection(
    text: str,
    parts: list[dict],
) -> dict:
    """解析选图 JSON；非法 id 丢弃。返回 {selections, use_ids, raw_ok}。"""
    valid = {str(p.get("id") or "") for p in parts if p.get("id")}
    empty = {"selections": [], "use_ids": [], "raw_ok": False}
    raw = (text or "").strip()
    if not raw or not valid:
        return empty
    # 剥 markdown 围栏
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.I)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except Exception:
        # 尝试截取第一个 { ... }
        brace = re.search(r"\{[\s\S]*\}", raw)
        if not brace:
            return empty
        try:
            data = json.loads(brace.group(0))
        except Exception:
            return empty
    if not isinstance(data, dict):
        return empty
    selections: list[dict] = []
    use_ids: list[str] = []
    seen: set[str] = set()

    def _consume_use(step: str, ids: list) -> None:
        cleaned: list[str] = []
        for x in ids or []:
            sid = str(x or "").strip().upper()
            if not sid:
                continue
            if not sid.startswith("P"):
                # 允许漏写 P：纯数字 → Pxx
                if sid.isdigit():
                    sid = f"P{int(sid):02d}"
                else:
                    continue
            # P1 → P01
            m2 = re.fullmatch(r"P(\d{1,3})", sid, re.I)
            if m2:
                sid = f"P{int(m2.group(1)):02d}"
            if sid not in valid:
                continue
            cleaned.append(sid)
            if sid not in seen:
                seen.add(sid)
                use_ids.append(sid)
        if cleaned or step:
            selections.append({"step": step or "", "use": cleaned})

    items = data.get("selections") or data.get("steps") or data.get("image_plan")
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                step = str(it.get("step") or it.get("name") or it.get("scene") or "").strip()
                use = it.get("use") or it.get("parts") or it.get("ids") or []
                if isinstance(use, str):
                    use = [u.strip() for u in re.split(r"[,，\s]+", use) if u.strip()]
                _consume_use(step, list(use) if isinstance(use, list) else [])
            elif isinstance(it, str):
                _consume_use("", [it])
    # 扁平 use
    flat = data.get("use") or data.get("part_ids") or data.get("selected")
    if isinstance(flat, list) and flat:
        _consume_use("(all)", flat)
    elif isinstance(flat, str) and flat.strip():
        _consume_use("(all)", [u.strip() for u in re.split(r"[,，\s]+", flat) if u.strip()])

    return {
        "selections": selections,
        "use_ids": use_ids,
        "raw_ok": bool(use_ids),
    }


def format_image_selection_for_prompt(selection: dict, parts: list[dict]) -> str:
    """选图结果注入写码 prompt。"""
    by_id = {str(p.get("id")): p for p in parts}
    use_ids = list(selection.get("use_ids") or [])
    sels = list(selection.get("selections") or [])
    if not use_ids:
        return ""
    lines = [
        "## Image part selection (stage-1 — assemble from these)",
        "Only the parts listed here may appear in `_img` / `register_guard`.",
    ]
    if sels:
        for s in sels:
            step = s.get("step") or ""
            ids = s.get("use") or []
            if not ids:
                continue
            detail = ", ".join(
                f"{i}→`_img('{by_id[i]['path']}')`" for i in ids if i in by_id
            )
            lines.append(f"- {step or '(step)'}: {detail}")
    else:
        for i in use_ids:
            p = by_id.get(i)
            if not p:
                continue
            lines.append(f"- [{i}] `_img('{p.get('path')}')` — {p.get('label')}")
    return "\n".join(lines) + "\n"


def image_select_schema_hint() -> str:
    return (
        "Reply with ONLY a JSON object (no markdown, no prose):\n"
        "{\n"
        '  "selections": [\n'
        '    {"step": "选关界面", "use": ["P01", "P02"]},\n'
        '    {"step": "关卡信息窗口", "use": ["P05", "P06"]}\n'
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- `use` ids MUST be from the IMAGE PARTS table (P01, P02, …). "
        "Never invent paths or new ids.\n"
        "- Cover every explanation step that needs images; same part may appear "
        "in multiple steps.\n"
        "- Prefer id-role parts for scene detection; button-role for clicks.\n"
        "- Do NOT select parts you will not need; do NOT output Python.\n"
    )


def enforce_img_dir(code: str, source_dir: str = "") -> str:
    """强制把生成代码中的 IMG_DIR 改成所选目录（不依赖模型自觉）。"""
    if not (source_dir or "").strip() or not (code or "").strip():
        return code
    line = build_img_dir_line(source_dir)
    if re.search(r"^IMG_DIR\s*=", code, flags=re.MULTILINE):
        return re.sub(r"^IMG_DIR\s*=\s*.+$", line, code, count=1, flags=re.MULTILINE)
    # 没有 IMG_DIR 时插到 IMG_PATH import 之后
    m = re.search(r"^(from\s+core\.path\s+import\s+IMG_PATH\s*)$", code, flags=re.MULTILINE)
    if m:
        pos = m.end()
        return code[:pos] + "\n\n" + line + code[pos:]
    return line + "\n\n" + code


def _build_system_prompt(
    source_dir: str = "",
    *,
    explanation: str = "",
    tags: list[str] | None = None,
    free_mode: Optional[bool] = None,
    selected_part_ids: list[str] | None = None,
) -> str:
    """从 config.json 动态构建 system prompt，并按 explanation 注入 few-shot。"""
    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    free = is_codegen_free_mode(free_mode)
    th = defaults.get("threshold", 0.9)
    nav = defaults.get("nav_threshold", 0.8)
    icon_th = defaults.get("icon_threshold", 0.85)

    scripts = cfg.get("available_scripts", [])
    script_lines = "\n".join(
        f'- **{s["module"]}**: `{s["name"]}` — {s["desc"]}'
        for s in scripts
    )
    scripts_block = f"以下脚本已存在，生成新脚本时应 import 使用：\n{script_lines}\n\n用法示例（注意用 scripts. 前缀）：\n"
    scripts_block += "```python\n"
    for s in scripts[:3]:
        name = s["name"].split("(")[0]
        scripts_block += f"from {s['module']} import {name}\n"
    scripts_block += "```"

    if free:
        rules_block = "(FREE MODE) Numbered Rules skipped — follow explanation + API + structure paradigm."
        from backend.script_generator.few_shot import build_paradigm_block
        paradigm = build_paradigm_block(
            explanation=explanation,
            tags=tags,
        )
        struct_checklist = format_explanation_structure_checklist(
            explanation, source_dir=source_dir,
        )
        few_shot_block = ""
        if paradigm.strip():
            few_shot_block = "## Few-shot Examples (structure paradigm only)\n\n" + paradigm
        if struct_checklist.strip():
            few_shot_block = (
                (few_shot_block + "\n\n" if few_shot_block else "")
                + struct_checklist
            )
    else:
        rules = cfg.get("rules", [])
        rules_block = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))
        from backend.script_generator.few_shot import build_few_shot_block
        from backend.script_generator.v2_semantic_map import build_structure_contract_block
        contract = build_structure_contract_block(
            explanation=explanation,
            source_dir=source_dir,
        )
        few_shot_block = build_few_shot_block(
            explanation=explanation,
            tags=tags,
            source_dir=source_dir,
        )
        parts = []
        if contract.strip():
            parts.append(contract)
        if few_shot_block.strip():
            parts.append("## Few-shot Examples (follow these patterns)\n\n" + few_shot_block)
        few_shot_block = "\n\n".join(parts)
        if not few_shot_block.strip():
            few_shot_block = ""

    img_dir_line = build_img_dir_line(source_dir)
    if source_dir:
        src_line = (
            f"图片文件夹路径: {source_dir}\n"
            f"MUST set exactly this line (do NOT use game/script placeholder):\n{img_dir_line}"
        )
    else:
        src_line = "（未指定图片文件夹 — 请要求用户先选择）"

    from backend.script_generator.api_catalog import api_return_types_banner
    contrast = api_return_types_banner().strip()

    template = cfg.get("system_prompt_template", "")
    prompt = template.replace("$THRESHOLD", str(th))
    prompt = prompt.replace("$NAV_THRESHOLD", str(nav))
    prompt = prompt.replace("$ICON_THRESHOLD", str(icon_th))
    prompt = prompt.replace("$AVAILABLE_SCRIPTS", scripts_block)
    prompt = prompt.replace("$SOURCE_DIR", src_line)
    prompt = prompt.replace("$IMG_DIR_LINE", img_dir_line)
    prompt = prompt.replace("$RULES", rules_block)
    prompt = prompt.replace("$FEW_SHOT", few_shot_block)
    # 契约对照条：模板已含则跳过，避免重复
    if "Return-type contrast" not in prompt and contrast:
        prompt = prompt.replace(
            "## Available API (only these methods, do NOT invent others)",
            "## Available API (only these methods, do NOT invent others)\n\n" + contrast,
            1,
        )
    if free:
        prompt = _FREE_MODE_BANNER + "\n" + prompt
        prompt = re.sub(
            r"## Architecture: scene-first.*?(?=\n## )",
            "## Architecture (FREE MODE)\n"
            "Follow explanation task flow; use FSM / run_task as you see fit. "
            "Post-validate will enforce wiring.\n\n",
            prompt,
            count=1,
            flags=re.S,
        )
        prompt = re.sub(
            r"## Multi-Task Orchestration.*?(?=\n## )",
            "",
            prompt,
            count=1,
            flags=re.S,
        )
        prompt = re.sub(
            r"## Authority / Weights \(CRITICAL\).*?(?=\n## |\Z)",
            "## Authority (FREE MODE)\n"
            "Script explanation + Available API only.\n\n",
            prompt,
            count=1,
            flags=re.S,
        )
        # 生成阶段不钉死 MUST IMG_DIR 措辞（校验仍会查）
        prompt = re.sub(
            r"MUST set exactly this line \(do NOT use game/script placeholder\):",
            "Set IMG_DIR to the selected folder (validated after generation):",
            prompt,
            count=1,
        )

    # 生成前硬白名单：IMAGE PARTS 封闭零件表（可选 stage-1 选中子集）
    try:
        img_allow = allowed_images_block(
            source_dir or "",
            explanation=explanation or "",
            selected_part_ids=selected_part_ids,
        )
        if img_allow.strip():
            prompt = prompt.rstrip() + "\n\n" + img_allow
    except Exception:
        pass

    # 方案 E：架构锁定块
    try:
        from backend.script_generator.architecture import architecture_prompt_block
        arch_block = architecture_prompt_block(explanation or "")
        if arch_block.strip():
            prompt = prompt.rstrip() + "\n\n" + arch_block
    except Exception:
        pass

    # 方案 C：动作级执行清单
    try:
        defaults_ck = _load_config().get("defaults") or {}
        if defaults_ck.get("execution_checklist", True):
            from backend.script_generator.execution_checklist import (
                extract_execution_checklist,
                format_checklist_for_prompt,
            )
            items = extract_execution_checklist(
                explanation or "", source_dir=source_dir or "",
            )
            ck = format_checklist_for_prompt(items)
            if ck.strip():
                prompt = prompt.rstrip() + "\n\n" + ck
    except Exception:
        pass

    # 方案 F：生成侧转场点击确认提示
    prompt = (
        prompt.rstrip()
        + "\n\n## Click confirm (optional runtime)\n"
        "For scene-changing / popup-dismiss clicks, prefer "
        "`await browser.click_image(path, expect='appear', appear_path=_img('next'))` "
        "or `expect='gone'`. Same-screen buttons keep default `expect='none'` "
        "(no extra wait).\n"
    )

    return prompt


def _image_b64(image_path: Path, compress: bool = False, max_size: int = 800) -> tuple[str, str]:
    """读取图片为 base64，返回 (base64_data, media_type)。支持中文等非 ASCII 路径。"""
    import cv2
    import numpy as np

    path = Path(image_path)
    raw = path.read_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图片: {path}")
    if compress:
        h, w = img.shape[:2]
        if max(h, w) > max_size:
            scale = max_size / max(h, w)
            new_w, new_h = int(w * scale), int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".png", img)
    data = base64.b64encode(bytes(buf)).decode("utf-8")
    ext = path.suffix.lower().lstrip(".")
    media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
    return data, media_type


def _provider_info(provider: str) -> dict:
    """读取 config.json 中该提供商条目；热更新时每次重新加载。"""
    try:
        info = (_load_config().get("providers") or {}).get(provider)
    except Exception:
        info = None
    return info if isinstance(info, dict) else {}


def _provider_api(provider: str) -> str:
    """调用协议：claude / google / openai（OpenAI 兼容 chat.completions）。"""
    info = _provider_info(provider)
    api = str(info.get("api") or "").strip().lower()
    if api in ("anthropic", "claude"):
        return "claude"
    if api in ("google", "gemini"):
        return "google"
    if api in ("openai", "openai_compat", "openai-compatible"):
        return "openai"
    if provider == "claude":
        return "claude"
    if provider == "google":
        return "google"
    return "openai"


def _provider_supports_images(provider: str) -> bool:
    """是否允许把参考图以多模态格式发给该提供商（具体模型仍可能拒绝）。"""
    info = _provider_info(provider)
    if "supports_images" in info:
        return bool(info["supports_images"])
    return _provider_api(provider) in ("claude", "google", "openai")


async def describe_images_catalog(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    image_paths: list[Path],
    source_dir: str = "",
    explanation_text: str = "",
    refresh_vision: bool = False,
    compress_images: bool = True,
    on_status=None,
    on_artifact=None,
    max_tokens: Optional[int] = 4096,
    chunk_size: int = 8,
) -> tuple[str, int, int]:
    """用识图模型把参考图转成文字目录，供纯文本主模型使用。

    优先级：磁盘缓存 → 脚本介绍已写清 → 识图目录.txt（hash 未变）→ API。
    ``refresh_vision=True`` 时跳过前三项，全部走 API（仍会写回缓存与识图目录）。
    同目录图片按 sha256 + 识图模型缓存到 ``<source_dir>/.vision_cache/``。
    返回 (目录文本, 输入tokens, 输出tokens)。
    """
    from backend.script_generator.vision_cache import (
        VisionCache,
        build_vision_user_context,
        extract_explanation_captions,
        format_image_caption,
        is_sufficient_explanation_caption,
        load_catalog_txt,
        parse_per_image_captions,
        write_catalog_txt,
    )

    paths = [Path(p) for p in (image_paths or []) if p]
    if not paths:
        return "", 0, 0
    if not _provider_supports_images(provider):
        raise RuntimeError(
            f"辅助识图提供商「{provider}」未标记支持传图，请换成带视觉能力的提供商/模型"
        )

    def _label(p: Path) -> str:
        return image_rel_label(p, source_dir)

    cache = VisionCache.for_source_dir(source_dir)
    expl_captions = extract_explanation_captions(explanation_text)
    catalog_txt: dict[str, str] = {}
    if not refresh_vision and source_dir:
        catalog_txt = load_catalog_txt(source_dir)

    if refresh_vision:
        if on_status:
            on_status("重新识图：忽略缓存与识图目录，全部调用 API…")
        if on_artifact:
            on_artifact(
                "stage",
                "vision_refresh|running|重新识图|"
                "已忽略 .vision_cache / 识图目录.txt（介绍仍作上下文）",
            )

    per_file: dict[str, str] = {}
    need_api: list[Path] = []
    cache_hits = intro_hits = txt_hits = 0

    for p in paths:
        label = _label(p)
        key = label.lower()
        base_key = p.name.lower()

        if refresh_vision:
            need_api.append(p)
            continue

        hit = cache.get(p, provider, model) if cache else None
        if hit:
            per_file[label] = hit
            cache_hits += 1
            continue

        intro = expl_captions.get(key) or expl_captions.get(base_key, "")
        if intro and is_sufficient_explanation_caption(intro):
            cap = format_image_caption(label, intro)
            per_file[label] = cap
            intro_hits += 1
            if cache:
                cache.put(p, provider, model, cap)
            continue

        txt_cap = (
            catalog_txt.get(label)
            or catalog_txt.get(key)
            or catalog_txt.get(p.name)
            or catalog_txt.get(base_key)
        )
        if txt_cap and cache:
            sha = cache.file_sha256_safe(p)
            cat_sha = cache.get_catalog_sha(label) or cache.get_catalog_sha(p.name)
            if sha and cat_sha == sha:
                per_file[label] = txt_cap
                txt_hits += 1
                cache.put(p, provider, model, txt_cap)
                continue

        need_api.append(p)

    n = len(paths)
    api_n = len(need_api)
    if on_status:
        parts = []
        if cache_hits:
            parts.append(f"缓存 {cache_hits}")
        if intro_hits:
            parts.append(f"介绍 {intro_hits}")
        if txt_hits:
            parts.append(f"目录 {txt_hits}")
        if parts and api_n:
            on_status(
                f"辅助识图：{' + '.join(parts)}/{n}，待 API {api_n} 张"
            )
        elif not api_n:
            on_status(f"辅助识图：全部本地命中（{n}/{n}）")
        else:
            on_status(f"辅助识图中…（待 API {api_n}/{n} 张）")

    local_hits = cache_hits + intro_hits + txt_hits
    if local_hits and on_artifact:
        on_artifact(
            "stage",
            f"vision_cache|done|识图本地|"
            f"缓存 {cache_hits}，介绍 {intro_hits}，目录 {txt_hits}"
            + (f"；待 API {api_n} 张" if api_n else "，跳过 API"),
        )

    system = (
        "You help game-automation script writers understand UI screenshots.\n"
        "Script explanation context may be provided — align descriptions with it.\n"
        "For EACH image output one markdown section:\n"
        "### relative/path.png\n"
        "1-3 short Chinese lines: UI role (button / popup / scene marker / icon), "
        "visible text, distinctive look for template matching.\n"
        "Use the exact relative path (may include subfolders) as the heading. No code."
    )
    total_in = total_out = 0

    if need_api:
        for i in range(0, api_n, max(1, chunk_size)):
            chunk = need_api[i : i + chunk_size]
            chunk_end = min(i + len(chunk), api_n)
            chunk_names = [_label(p) for p in chunk]
            if on_status and api_n:
                on_status(f"辅助识图中…（API {chunk_end}/{api_n}，总 {n} 张）")
            if on_artifact:
                on_artifact(
                    "stage",
                    f"vision_{i}|running|辅助识图|"
                    f"API 识图 {i + 1}-{chunk_end}/{api_n}"
                    + (f"（本地已命中 {local_hits}）" if local_hits else ""),
                )
            ctx = build_vision_user_context(
                explanation_text, chunk_names, expl_captions,
            )
            user_text = (
                "请按相对路径说明下列游戏 UI 截图，供后续编写自动化脚本使用：\n"
                + "\n".join(f"- {name}" for name in chunk_names)
            )
            if ctx:
                user_text = ctx + "\n\n" + user_text
            load_fail: list[str] = []
            img_parts: list = []
            for p, name in zip(chunk, chunk_names):
                try:
                    b64data, media_type = _image_b64(
                        p, compress=compress_images,
                    )
                    api = _provider_api(provider)
                    if api == "claude":
                        img_parts.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64data},
                        })
                    elif api == "google":
                        img_parts.append({
                            "inline_data": {"mime_type": media_type, "data": b64data},
                        })
                    else:
                        img_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64data}"},
                        })
                    img_parts.append({"type": "text", "text": f"  → {name}"})
                except Exception as e:
                    load_fail.append(f"{name}: {e}")
                    img_parts.append({"type": "text", "text": f"[图片加载失败: {name} - {e}]"})
            if load_fail and on_artifact:
                on_artifact(
                    "stage",
                    f"vision_{i}|error|部分图片未加载|"
                    + "\n".join(f"- {x}" for x in load_fail[:6]),
                )
            content: list = [{"type": "text", "text": user_text}] + img_parts
            messages = [{"role": "user", "content": content}]
            text, inp, out = await call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=messages,
                system_prompt=system,
                max_tokens=max_tokens if max_tokens is not None else 4096,
            )
            total_in += inp or 0
            total_out += out or 0
            parsed = parse_per_image_captions(text or "", chunk_names)
            for p, name in zip(chunk, chunk_names):
                cap = parsed.get(name) or parsed.get(p.name) or (text or "").strip()
                per_file[name] = cap
                if cache and cap.strip():
                    cache.put(p, provider, model, cap)
            if on_artifact:
                preview = (text or "").strip()
                if len(preview) > 400:
                    preview = preview[:400] + "…"
                on_artifact(
                    "stage",
                    f"vision_{i}|done|识图完成（{chunk_end}/{api_n}）|{preview}",
                )

    if cache:
        for p in paths:
            sha = cache.file_sha256_safe(p)
            if sha:
                label = _label(p)
                cache.set_catalog_sha(label, sha)
                if label != p.name:
                    cache.set_catalog_sha(p.name, sha)
        cache.save()

    if source_dir:
        try:
            write_catalog_txt(source_dir, per_file, paths)
        except OSError:
            pass

    catalog = "\n\n".join(
        format_image_caption(_label(p), per_file.get(_label(p), ""))
        for p in paths
    )
    if on_artifact and n:
        on_artifact(
            "stage",
            f"vision_done|done|识图汇总|"
            f"共 {n} 张："
            + ("重新识图，" if refresh_vision else "")
            + f"缓存 {cache_hits}，介绍 {intro_hits}，"
            f"目录 {txt_hits}，API {api_n}",
        )
    return catalog, total_in, total_out


def _hoist_explanation(explanation_text: str, *, lean: bool = False) -> str:
    """normalize 介绍用语 + 提升试运行 HARD 约束。lean=True 时不注入 IR/用语块。"""
    try:
        from backend.script_generator.explain_norm import prepare_explanation_for_codegen
        return prepare_explanation_for_codegen(
            explanation_text or "", lean=lean,
        ).normalized
    except Exception:
        try:
            from backend.script_generator.feedback_opt import hoist_trial_constraints
            return hoist_trial_constraints(explanation_text or "")
        except Exception:
            return explanation_text or ""


def _prepare_explanation(
    explanation_text: str,
    *,
    on_artifact=None,
    on_status=None,
    lean: bool = False,
) -> str:
    """生成/修订入口：编译介绍，并写入轨迹 artifact。"""
    try:
        from backend.script_generator.explain_norm import prepare_explanation_for_codegen
        result = prepare_explanation_for_codegen(explanation_text or "", lean=lean)
    except Exception:
        return explanation_text or ""
    if on_status and (result.notes or result.warnings):
        try:
            msg = "介绍已规范化"
            if result.warnings:
                msg += f"（{len(result.warnings)} 条提示）"
            on_status(msg)
        except Exception:
            pass
    if on_artifact:
        try:
            import json
            meta = result.to_dict()
            on_artifact("explain_norm_meta", json.dumps(meta, ensure_ascii=False, indent=2))
            on_artifact("explain_normalized", result.normalized)
        except Exception:
            pass
    return result.normalized


def _build_messages(
    explanation_text: str,
    image_paths: list[Path],
    provider: str = "claude",
    send_images: bool = True,
    compress_images: bool = False,
    lean: bool = False,
    source_dir: str = "",
) -> list[dict]:
    """构建消息列表，根据 provider 选择图片格式。"""
    content = [{"type": "text", "text": _hoist_explanation(explanation_text, lean=lean)}]
    can_send = bool(send_images and image_paths and _provider_supports_images(provider))

    def _lab(p: Path | str) -> str:
        return image_rel_label(p, source_dir)

    if send_images and image_paths and not _provider_supports_images(provider):
        # 仍把文件名列表塞进文本，避免模型完全不知道有哪些图
        names = "\n".join(f"- {_lab(p)}" for p in image_paths)
        content.append({
            "type": "text",
            "text": (
                f"\n\n（当前提供商不支持看图，已跳过图片二进制。"
                f"请仅根据文件名编写 _img() 引用）\n参考图片文件名：\n{names}"
            ),
        })
    elif can_send:
        content.append({
            "type": "text",
            "text": f"\n\n参考图片共 {len(image_paths)} 张，相对路径对应脚本中的 _img() 名：",
        })
        for img_path in image_paths:
            lab = _lab(img_path)
            try:
                b64data, media_type = _image_b64(img_path, compress=compress_images)
                api = _provider_api(provider)
                if api == "claude":
                    encoded = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64data}}
                elif api == "google":
                    encoded = {"inline_data": {"mime_type": media_type, "data": b64data}}
                else:  # OpenAI 兼容 image_url
                    encoded = {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64data}"}}
                content.append(encoded)
                content.append({"type": "text", "text": f"  → {lab}"})
            except Exception as e:
                content.append({"type": "text", "text": f"[图片加载失败: {lab} - {e}]"})
    return [{"role": "user", "content": content}]


def _flatten_openai_messages(messages: list[dict]) -> list[dict]:
    """把仅含 text 的 content 列表压成字符串，兼容 DeepSeek 等实现。"""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            has_non_text = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
                elif isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    has_non_text = True
                elif isinstance(part, str):
                    texts.append(part)
                else:
                    has_non_text = True
            if has_non_text:
                out.append(msg)
            else:
                out.append({**msg, "content": "\n".join(texts)})
        else:
            out.append(msg)
    return out


def _deepseek_extra_body(_model: str = "") -> dict:
    """
    DeepSeek V4 默认开启 thinking：思考 token 会占满 max_tokens，
    导致 finish_reason=length 且 content 为空。脚本生成关闭 thinking。
    """
    return {"thinking": {"type": "disabled"}}


def strip_code_fences(raw: str) -> str:
    """去掉 markdown 代码块标记。"""
    raw = (raw or "").strip()
    if raw.startswith("```python"):
        raw = raw[len("```python"):].strip()
    if raw.startswith("```"):
        raw = raw[len("```"):].strip()
    if raw.endswith("```"):
        raw = raw[:-len("```")].strip()
    return raw


_CN_PUNCT_RE = re.compile(r"[，。！？；：、“”‘’（）【】《》、]")

# Script Generator 官方 API（与 config system_prompt 一致）
ALLOWED_BROWSER_METHODS = frozenset({
    "match_image",
    "match_image_multi",
    "click_image",
    "click",
    "wait_image",
    "b_sleep",
    "update_frame",
    "request_fps",
    "release_fps",
    "script_log",
    "note_state",
    "note_progress",
    # 伪录制 instrumentation（试跑注入 / 生产脚本均可）
    "enable_pseudo_record",
    "finish_pseudo_record",
})
# login/web only; enabled when explanation looks like a login task
LOGIN_BROWSER_METHODS = frozenset({"goto", "dmm_login"})

_BUILTINS = (
    set(__builtins__.keys()) if isinstance(__builtins__, dict) else set(dir(__builtins__))
)
_BUILTINS |= {
    "True", "False", "None", "Ellipsis", "NotImplemented",
    "asyncio", "Optional", "Path", "Union", "List", "Dict", "Tuple", "Any",
    "print", "range", "len", "min", "max", "sum", "enumerate", "zip", "list", "dict",
    "str", "int", "float", "bool", "type", "isinstance", "hasattr", "getattr",
    "Exception", "TimeoutError", "RuntimeError", "NameError", "ImportError",
    "ValueError", "TypeError", "StopIteration", "BaseException",
}


def _collect_imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "*":
                    continue
                names.add(a.asname or a.name)
    return names


def _bind_target(names: set[str], t: ast.AST) -> None:
    if isinstance(t, ast.Name):
        names.add(t.id)
    elif isinstance(t, (ast.Tuple, ast.List)):
        for elt in t.elts:
            _bind_target(names, elt)
    elif isinstance(t, ast.Starred):
        _bind_target(names, t.value)


def _collect_arg_names(args: ast.arguments) -> set[str]:
    names: set[str] = set()
    for a in args.posonlyargs + args.args + args.kwonlyargs:
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _collect_function_locals(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names |= _collect_arg_names(fn.args)
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
            names.add(node.name)
            names |= _collect_arg_names(node.args)
        elif isinstance(node, ast.Lambda):
            names |= _collect_arg_names(node.args)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _bind_target(names, t)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _bind_target(names, node.target)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for gen in node.generators:
                _bind_target(names, gen.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _bind_target(names, node.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _find_undefined_calls(tree: ast.AST) -> list[str]:
    """模块级可见名字 + 函数局部；报告未定义的 Name 调用。"""
    top = _collect_assigned_names(tree) | _collect_imported_names(tree)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(n.name)

    issues: list[str] = []
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = top | _collect_function_locals(n)
        for node in ast.walk(n):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id not in local and func.id not in _BUILTINS:
                issues.append(f"{func.id}@{getattr(node, 'lineno', 0)}")
    # 去重保序
    seen = set()
    out = []
    for i in issues:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _module_visible_names(tree: ast.AST) -> set[str]:
    names = _collect_assigned_names(tree) | _collect_imported_names(tree)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
    return names


def _find_undefined_name_loads(tree: ast.AST) -> list[str]:
    """报告函数体内未定义的 Name 加载（如 STATE_TIMEOUT.get → NameError）。"""
    top = _module_visible_names(tree)
    issues: list[str] = []
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = top | _collect_function_locals(n)
        for node in ast.walk(n):
            if not isinstance(node, ast.Name):
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            name = node.id
            if name.startswith("__") or name in local or name in _BUILTINS:
                continue
            issues.append(f"{name}@{getattr(node, 'lineno', 0)}")
    seen: set[str] = set()
    out: list[str] = []
    for i in issues:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _code_loads_name(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name:
            return True
    return False


def patch_state_timeout_alias(code: str) -> tuple[str, list[str]]:
    """多任务只有 TASK_*_TIMEOUT、却写了 STATE_TIMEOUT 时，本地合并别名。"""
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    assigned = _collect_assigned_names(tree)
    if "STATE_TIMEOUT" in assigned:
        return code, []
    task_tos = sorted(
        n for n in assigned
        if n.endswith("_TIMEOUT") and n != "STATE_TIMEOUT"
    )
    if not task_tos:
        return code, []
    if not _code_loads_name(tree, "STATE_TIMEOUT"):
        return code, []

    last_line = 0
    for n in tree.body:
        targets: list[str] = []
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    targets.append(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            targets.append(n.target.id)
        if any(t in task_tos for t in targets):
            last_line = max(last_line, getattr(n, "end_lineno", None) or n.lineno)
    if last_line <= 0:
        return code, []

    lines = code.splitlines(keepends=True)
    if last_line > len(lines):
        return code, []
    block = (
        "\n# Shared handlers may use STATE_TIMEOUT; merge per-task timeouts.\n"
        "STATE_TIMEOUT = {}\n"
        + "".join(f"STATE_TIMEOUT.update({n})\n" for n in task_tos)
    )
    # keepends: insert after last_line
    lines.insert(last_line, block if block.endswith("\n") else block + "\n")
    return "".join(lines), [f"补 STATE_TIMEOUT 别名 ← {', '.join(task_tos)}"]


# unknown 场景名 → 同 task 内优先 alias 到的业务态键
_SCENE_BUSINESS_ALIASES: dict[str, tuple[str, ...]] = {
    "房间界面": ("房间领体力", "房间入口"),
    "竞技场界面": ("竞技场",),
    "竞技场": ("竞技场",),
    "塔界面": ("塔",),
    "塔": ("塔",),
}


def _pick_main_scene_handler(
    mapping: dict[str, str],
    fns: dict[str, ast.AST],
) -> Optional[str]:
    """主界面业务态：非「返回*」且 handler 会 return '主界面'。"""
    for k, h in mapping.items():
        if k.startswith("返回"):
            continue
        fn = fns.get(h)
        if fn is not None and _function_returns_literal(fn, "主界面"):
            return k
    return None


def patch_task_scene_aliases(code: str) -> tuple[str, list[str]]:
    """为 TASK_*_STATES 补 unknown_state 场景键 alias（同 handler，不新增函数）。"""
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    scene_keys = _unknown_state_scene_keys(tree)
    if not scene_keys:
        return code, []

    assigns: dict[str, ast.Assign] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if (
                isinstance(t, ast.Name)
                and t.id.startswith("TASK_")
                and t.id.endswith("_STATES")
                and isinstance(node.value, ast.Dict)
            ):
                assigns[t.id] = node

    if not assigns:
        return code, []

    fns = _module_functions(tree)
    notes: list[str] = []

    for var, assign in assigns.items():
        dnode = assign.value
        assert isinstance(dnode, ast.Dict)
        mapping = _dict_str_name_values(dnode)
        keys = set(mapping.keys())

        def add_alias(scene: str, src_key: str) -> None:
            if scene not in scene_keys or scene in keys or src_key not in mapping:
                return
            if not _task_var_matches_scene(var, scene):
                return
            handler = mapping[src_key]
            dnode.keys.append(ast.Constant(value=scene))
            dnode.values.append(ast.Name(id=handler, ctx=ast.Load()))
            notes.append(f"{var} alias {scene!r} → {src_key!r} ({handler})")
            keys.add(scene)

        for scene, candidates in _SCENE_BUSINESS_ALIASES.items():
            for src in candidates:
                if src in mapping:
                    add_alias(scene, src)
                    break

        if "主界面" in scene_keys and "主界面" not in keys:
            src = _pick_main_scene_handler(mapping, fns)
            if not src:
                src = _pick_main_business_handler(var, mapping)
            if src:
                add_alias("主界面", src)

        if "出击界面" in scene_keys and "出击界面" not in keys:
            if "返回出击界面" in mapping:
                add_alias("出击界面", "返回出击界面")

    if not notes:
        return code, []

    try:
        return ast.unparse(tree), notes
    except Exception:
        return code, []


def _pick_main_business_handler(var_name: str, mapping: dict[str, str]) -> Optional[str]:
    """主界面业务态：按 task 偏好选第一个非导航态。"""
    tid = _task_id_from_var(var_name)
    prefs: tuple[str, ...] = ()
    if "room" in tid:
        prefs = ("房间领体力", "房间入口", "room")
    elif "jjc" in tid:
        prefs = ("竞技场", "jjc")
    elif "ta" in tid or "tower" in tid:
        prefs = ("塔", "ta")
    for pref in prefs:
        for k in mapping:
            if pref in k.lower() and not k.startswith("返回"):
                return k
    for k in mapping:
        if k.startswith("返回") or k in ("未知", "主界面", "出击界面"):
            continue
        return k
    return None


def _image_exists_in_dir(img_root: Path, name: str) -> bool:
    """支持子目录相对路径；无斜杠且 basename 唯一时也可命中子目录文件。"""
    if not (name or "").strip():
        return False
    keys, by_base = _folder_img_index(img_root)
    return _key_in_folder(name, keys, by_base)


# 日常范式常抄来的导航 chrome；目录没有时一律当幻觉剥离（即使介绍偶发提到）
_PARADIGM_CHROME_STEMS = frozenset({
    "home", "home_btn", "back_home", "1_home", "back",
})

# 介绍里 png 路径：允许 子目录/文件名（含中文）
_EXPL_IMG_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_\u4e00-\u9fff/])"
    r"((?:[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\-]*/)*)"
    r"([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\-]*)"
    r"\.(?:png|jpg|jpeg|webp|bmp)",
    re.I,
)


def _stems_mentioned_in_explanation(
    explanation: str,
    source_dir: str = "",
) -> set[str]:
    """介绍里点名的图片 key（相对路径、无扩展名）。

    有 source_dir 时优先用 IDE 同款 token 扫描（最长真实路径），避免
    「点击暗进入战斗/属性/1_dark_1.png」把「暗」粘进路径。
    """
    text = explanation or ""
    out: set[str] = set()
    src = (source_dir or "").strip()
    if src:
        try:
            from backend.script_generator.spec_model import (
                find_image_tokens,
                refresh_dir_image_map,
            )

            known = refresh_dir_image_map(src)
            for _s, _e, token in find_image_tokens(text, src, known=known):
                k = _norm_img_key(token)
                if k:
                    out.add(k)
            if out:
                return out
        except Exception:
            pass
    for m in _EXPL_IMG_PATH_RE.finditer(text):
        rel = (m.group(1) + m.group(2)).replace("\\", "/")
        k = _norm_img_key(rel)
        if k:
            out.add(k)
    return out


def _collect_missing_image_names(tree: ast.AST, img_root: Path) -> set[str]:
    """返回缺失图片的 key 集合（相对路径、不含扩展名）。"""
    missing: set[str] = set()
    if not img_root.is_dir():
        return missing
    for name in _collect_img_names(tree):
        if not _image_exists_in_dir(img_root, name):
            missing.add(_norm_img_key(name))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "register_guard"):
            continue
        if not node.args:
            continue
        stem = _image_ref_stem(node.args[0])
        if stem and not _image_exists_in_dir(img_root, stem):
            missing.add(_norm_img_key(stem))
    return missing


def _hallucinated_image_stems(
    tree: ast.AST,
    img_root: Path,
    explanation: str = "",
) -> set[str]:
    """目录无此文件，且介绍未点名（或属范式 chrome）→ 幻觉图。"""
    missing = _collect_missing_image_names(tree, img_root)
    if not missing:
        return set()
    expl = _stems_mentioned_in_explanation(explanation, str(img_root))
    expl_bases = {k.rsplit("/", 1)[-1].lower() for k in expl}
    drop: set[str] = set()
    for s in missing:
        base = s.rsplit("/", 1)[-1].lower()
        if s in _PARADIGM_CHROME_STEMS or base in _PARADIGM_CHROME_STEMS:
            drop.add(s)
            continue
        if s in expl or base in expl_bases:
            continue
        drop.add(s)
    return drop


def _image_ref_stem(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "_img" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                return _norm_img_key(arg0.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return _norm_img_key(right.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _norm_img_key(node.value)
    return None


def patch_strip_missing_image_refs(
    code: str,
    source_dir: str = "",
    explanation: str = "",
) -> tuple[str, list[str]]:
    """剥离幻觉图：目录不存在且介绍未点名（含 home/1_home 等范式 chrome）。

    另剥离「盘内有但介绍未点名」的白名单外图（如 1_menu），避免试跑前硬拦。

    用源码级替换，避免整文件 ast.unparse 破坏格式。
    match/click/wait_image(_img('缺失')) → False；register_guard 行删除；
    dict / 元组内残留 `_img('缺失')` **删除引用**（禁止写成 None，否则 click_image 会 Path 崩溃）。
    """
    img_root = Path(source_dir or "")
    if not (code or "").strip() or not img_root.is_dir():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    drop = set(_hallucinated_image_stems(tree, img_root, explanation))
    try:
        drop |= _intro_unmentioned_image_stems(tree, source_dir, explanation)
    except Exception:
        pass
    if not drop:
        return code, []

    notes: list[str] = []
    new_code = code

    # 1) register_guard 整行
    lines = new_code.splitlines(keepends=True)
    kept: list[str] = []
    removed_guard = 0
    for line in lines:
        if "register_guard" in line and any(m in line for m in drop):
            removed_guard += 1
            continue
        kept.append(line)
    if removed_guard:
        notes.append(f"移除 register_guard 幻觉图 ×{removed_guard}")
        new_code = "".join(kept)

    # 2) browser.match/click/wait_image(_img('stem'), ...) → False
    for stem in sorted(drop, key=len, reverse=True):
        esc = re.escape(stem)
        call_pat = re.compile(
            rf"(?:await\s+)?browser\.(?:match_image|click_image|wait_image)"
            rf"\(\s*_img\(\s*['\"]{esc}(?:\.png)?['\"]\s*\)[^)]*\)",
            re.M,
        )
        newer, nsub = call_pat.subn("False", new_code)
        if nsub:
            new_code = newer
            notes.append(f"白名单外图 {stem}：中和 match/click/wait ×{nsub}")

        # 3) dict 条目（支持单行多 pair）："场景": _img('stem'),
        dict_pat = re.compile(
            rf"['\"][^'\"]+['\"]\s*:\s*_img\(\s*['\"]{esc}(?:\.png)?['\"]\s*\)\s*,?\s*"
        )
        newer, nsub = dict_pat.subn("", new_code)
        if nsub:
            new_code = newer
            notes.append(f"白名单外图 {stem}：移除 dict 条目 ×{nsub}")

        # 4) 残留：删除 _img 引用（含相邻逗号）；禁止写成 None
        n_del = 0
        for pat in (
            rf",\s*_img\(\s*['\"]{esc}(?:\.png)?['\"]\s*\)",
            rf"_img\(\s*['\"]{esc}(?:\.png)?['\"]\s*\)\s*,",
            rf"_img\(\s*['\"]{esc}(?:\.png)?['\"]\s*\)",
        ):
            newer, nsub = re.compile(pat).subn("", new_code)
            if nsub:
                new_code = newer
                n_del += nsub
        if n_del:
            notes.append(f"白名单外图 {stem}：删除 _img 引用 ×{n_del}")

    # 清理 dict/tuple 里删除后可能留下的多余逗号空档（尽力，失败则原样）
    new_code = re.sub(r",\s*,", ",", new_code)
    new_code = re.sub(r"\{\s*,", "{", new_code)
    new_code = re.sub(r",\s*\}", "}", new_code)
    new_code = re.sub(r"\(\s*,", "(", new_code)
    new_code = re.sub(r",\s*\)", ")", new_code)

    try:
        ast.parse(new_code)
    except SyntaxError:
        # 回退：至少保证不破坏原稿
        return code, []

    if not notes:
        return code, []
    preview = ", ".join(sorted(drop)[:6])
    more = f" 等{len(drop)}个" if len(drop) > 6 else ""
    notes.insert(0, f"本地剥离白名单外图: {preview}{more}")
    return new_code, notes


def _none_browser_image_arg_errors(tree: ast.AST) -> list[str]:
    """click/match/wait_image 首参为字面量 None → 运行时 Path 崩溃。"""
    methods = frozenset({
        "click_image", "match_image", "wait_image", "match_image_multi",
    })
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("browser", "win")
            and func.attr in methods
        ):
            continue
        if not node.args:
            continue
        arg0 = node.args[0]
        if isinstance(arg0, ast.Constant) and arg0.value is None:
            bad.append(f"{func.attr}@{getattr(node, 'lineno', '?')}")
    if not bad:
        return []
    preview = ", ".join(bad[:8])
    more = f" 等 {len(bad)} 处" if len(bad) > 8 else ""
    return [
        f"图片 API 首参为 None（会 Path 崩溃）: {preview}{more}；"
        "请删除该调用或改用有效 `_img(...)`"
    ]


def _hallucinated_image_errors(
    tree: ast.AST,
    source_dir: str,
    explanation: str = "",
) -> list[str]:
    """生成环硬错误：残留幻觉图（patch 后仍应为空）。"""
    src = (source_dir or "").strip()
    if not src:
        return []
    img_root = Path(src)
    if not img_root.is_dir():
        return []
    drop = _hallucinated_image_stems(tree, img_root, explanation)
    if not drop:
        return []
    preview = ", ".join(f"{s}.png" for s in sorted(drop)[:8])
    more = f" 等 {len(drop)} 个" if len(drop) > 8 else ""
    return [
        f"幻觉图（目录无且介绍未点名）: {preview}{more}——"
        "禁止引用不存在的素材；应由本地 patch 剥离"
    ]


def _intro_unmentioned_image_stems(
    tree: ast.AST,
    source_dir: str,
    explanation: str = "",
) -> set[str]:
    """盘内存在但介绍未点名的 `_img` stem（与错误文案同源）。"""
    expl = (explanation or "").strip()
    src = (source_dir or "").strip()
    if not expl or not src:
        return set()
    if not _stems_mentioned_in_explanation(expl, src):
        return set()
    disk = list_source_image_names(src)
    kept, dropped = filter_source_images_by_explanation(disk, expl, src)
    if not dropped:
        return set()
    allow = {_norm_img_key(n) for n in kept}
    allow.update(_stems_mentioned_in_explanation(expl, src))
    allow_by_base: dict[str, list[str]] = {}
    for k in allow:
        allow_by_base.setdefault(k.rsplit("/", 1)[-1].lower(), []).append(k)
    drop_keys = {_norm_img_key(n) for n in dropped}
    drop_bases = {k.rsplit("/", 1)[-1].lower() for k in drop_keys}

    bad: set[str] = set()
    for name in _collect_img_names(tree):
        k = _norm_img_key(name)
        if not k:
            continue
        if k in allow:
            continue
        base = k.rsplit("/", 1)[-1].lower()
        cands = allow_by_base.get(base, [])
        if len(cands) == 1:
            continue
        if k in drop_keys or base in drop_bases:
            bad.add(k)
    return bad


def _intro_unmentioned_image_errors(
    tree: ast.AST,
    source_dir: str,
    explanation: str = "",
) -> list[str]:
    """介绍点名过滤后：禁止使用介绍未提但盘内存在的图（如 1_menu）。"""
    bad = _intro_unmentioned_image_stems(tree, source_dir, explanation)
    if not bad:
        return []
    preview = ", ".join(f"{s}.png" for s in sorted(bad)[:8])
    more = f" 等 {len(bad)} 个" if len(bad) > 8 else ""
    return [
        f"介绍未点名素材（已从白名单移除）: {preview}{more}——"
        "请改用介绍中的图，或把该图补进脚本介绍后再生成"
    ]


def patch_missing_stdlib_imports(code: str) -> tuple[str, list[str]]:
    """补全常用 stdlib import（time 等）。"""
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    imported = _collect_imported_names(tree)
    assigned = _collect_assigned_names(tree)
    need: list[str] = []
    for mod in ("time", "random"):
        if mod in imported or mod in assigned:
            continue
        if _code_loads_name(tree, mod):
            need.append(mod)
    if not need:
        return code, []

    block = "".join(f"import {m}\n" for m in need)
    marker = "from backend.browser"
    if marker in code:
        code = code.replace(marker, block + marker, 1)
    elif "import asyncio" in code:
        code = code.replace("import asyncio", "import asyncio\n" + block.rstrip(), 1)
    else:
        code = block + code
    return code, [f"补 import: {', '.join(need)}"]


def patch_go_home_return_main(code: str) -> tuple[str, list[str]]:
    """「返回主界面」handler 误 return __exit__ 时改为 return '主界面'。"""
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    fns = _module_functions(tree)
    notes: list[str] = []
    for n in tree.body:
        d = None
        name = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id == "STATES" or t.id.endswith("_STATES"):
                        d = n.value
                        name = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and (
                n.target.id == "STATES" or n.target.id.endswith("_STATES")
            ):
                d = n.value
                name = n.target.id
        if d is None:
            continue
        mapping = _dict_str_name_values(d)
        if "返回主界面" not in mapping or "主界面" not in mapping:
            continue
        hname = mapping["返回主界面"]
        fn = fns.get(hname)
        if fn is None:
            continue
        changed = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            val = node.value
            if isinstance(val, ast.Constant) and val.value == "__exit__":
                node.value = ast.Constant(value="主界面")
                changed = True
        if changed:
            notes.append(f"{name}: `{hname}` return '__exit__' → '主界面'")

    if not notes:
        return code, []
    try:
        return ast.unparse(tree), notes
    except Exception:
        return code, []


def _task_table_nav_handlers(tree: ast.AST) -> set[str]:
    """收集映射到「返回主界面」「返回出击界面」的 handler 函数名。"""
    names: set[str] = set()
    for n in tree.body:
        d = None
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id == "STATES" or t.id.endswith("_STATES"):
                        d = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and (
                n.target.id == "STATES" or n.target.id.endswith("_STATES")
            ):
                d = n.value
        if d is None:
            continue
        mapping = _dict_str_name_values(d)
        for key in ("返回主界面", "返回出击界面"):
            h = mapping.get(key)
            if h:
                names.add(h)
    return names


def patch_nav_helper_return_unknown(code: str) -> tuple[str, list[str]]:
    """仅修「返回主界面」类：return None 会卡死；改为 '未知'。出击末尾由 patch_go_sortie_fallback_home 管。"""
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    fns = _module_functions(tree)
    targets = set()
    for n in tree.body:
        d = None
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id == "STATES" or t.id.endswith("_STATES"):
                        d = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and (
                n.target.id == "STATES" or n.target.id.endswith("_STATES")
            ):
                d = n.value
        if d is None:
            continue
        mapping = _dict_str_name_values(d)
        h = mapping.get("返回主界面")
        if h:
            targets.add(h)
    if not targets:
        targets = {n for n in fns if re.search(r"go_home|navigate_home|返回主", n, re.I)}
    notes: list[str] = []
    for hname in targets:
        fn = fns.get(hname)
        if fn is None:
            continue
        # 出击辅助交给 patch_go_sortie_fallback_home，避免这里误改
        if re.search(r"sortie|出击", hname, re.I):
            continue
        changed = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return):
                continue
            val = node.value
            if val is None or (isinstance(val, ast.Constant) and val.value is None):
                node.value = ast.Constant(value="未知")
                changed = True
        if changed:
            notes.append(f"`{hname}`: return None → '未知'（辅助失败可重路由）")
    if not notes:
        return code, []
    try:
        return ast.unparse(tree), notes
    except Exception:
        return code, []


def patch_go_sortie_fallback_home(code: str) -> tuple[str, list[str]]:
    """返回出击：点不到出击.png 时末尾 return '未知' → '返回主界面'（房间无出击键）。"""
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    fns = _module_functions(tree)
    targets = set()
    for n in tree.body:
        d = None
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id.endswith("_STATES") or t.id == "STATES":
                        d = n.value
        if d is None:
            continue
        mapping = _dict_str_name_values(d)
        h = mapping.get("返回出击界面")
        if h:
            targets.add(h)
    if not targets:
        targets = {n for n in fns if re.search(r"go_sortie|返回出击", n, re.I)}
    notes: list[str] = []
    for hname in targets:
        fn = fns.get(hname)
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # 只改函数体末尾的 return '未知'
        if not fn.body:
            continue
        last = fn.body[-1]
        if isinstance(last, ast.Return) and isinstance(last.value, ast.Constant):
            if last.value.value == "未知":
                last.value = ast.Constant(value="返回主界面")
                notes.append(f"`{hname}`: 末尾 '未知' → '返回主界面'（无出击键先回家）")
    if not notes:
        return code, []
    try:
        return ast.unparse(tree), notes
    except Exception:
        return code, []


def patch_jjc_home_to_sortie(code: str) -> tuple[str, list[str]]:
    """jjc/塔表里「主界面」误绑竞技/塔业务时，改绑返回出击辅助。"""
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    notes: list[str] = []
    for n in tree.body:
        d = None
        name = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id.endswith("_STATES"):
                        d = n.value
                        name = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and n.target.id.endswith("_STATES"):
                d = n.value
                name = n.target.id
        if d is None:
            continue
        mapping = _dict_str_name_values(d)
        if "返回出击界面" not in mapping or "主界面" not in mapping:
            continue
        sortie_h = mapping["返回出击界面"]
        home_h = mapping["主界面"]
        if home_h == sortie_h:
            continue
        # 主界面不应直接进竞技场/塔业务
        if not re.search(r"jjc|arena|竞技|ta_|tower|塔", home_h, re.I):
            continue
        # 改 dict 字面量
        for i, key in enumerate(d.keys):
            if isinstance(key, ast.Constant) and key.value == "主界面":
                d.values[i] = ast.Name(id=sortie_h, ctx=ast.Load())
                notes.append(
                    f"{name}: 「主界面」{home_h} → {sortie_h}（回家后继续返回出击）"
                )
                break
    if not notes:
        return code, []
    try:
        return ast.unparse(tree), notes
    except Exception:
        return code, []


_TASK_ENTRY_HELPER_SRC = '''
def _task_entry_state(states: dict) -> str:
    """jjc/塔等：优先「返回出击界面」。房间任务可从未知进（允许跳过先回主界面）。"""
    if not isinstance(states, dict):
        return "未知"
    if "返回出击界面" in states:
        return "返回出击界面"
    return "未知"
'''.lstrip()


def _strip_foreign_state_remap(code: str) -> tuple[str, bool]:
    """删除 run_task 里 handler 之后、处理 __exit__ 之前的「nxt not in states」兜底块。"""
    if "nxt not in states" not in (code or ""):
        return code, False

    def _repl(m: re.Match) -> str:
        mid = m.group(2)
        if "nxt not in states" not in mid:
            return m.group(0)
        if "返回主界面" not in mid and "返回出击界面" not in mid:
            return m.group(0)
        return m.group(1) + m.group(3)

    new_code, n = re.subn(
        r"(nxt\s*=\s*await\s+handler\(browser\)\s*\n)"
        r"([\s\S]*?)"
        r"([ \t]*if\s+nxt\s*==\s*['\"]__exit__['\"])",
        _repl,
        code,
        count=1,
    )
    if n and new_code != code:
        return new_code, True
    return code, False


def patch_run_task_entry_helper(code: str) -> tuple[str, list[str]]:
    """run_task 初始态：出击类先「返回出击界面」；并剥离会改写 __exit__ 的异任务兜底。"""
    if not (code or "").strip():
        return code, []
    if "def run_task" not in code and "async def run_task" not in code:
        return code, []
    notes: list[str] = []
    new_code = code
    if "def _task_entry_state(" not in new_code:
        m = re.search(r"(?m)^(async\s+)?def\s+run_task\s*\(", new_code)
        if m:
            new_code = new_code[: m.start()] + _TASK_ENTRY_HELPER_SRC + "\n" + new_code[m.start() :]
            notes.append("注入 _task_entry_state（出击类任务先辅助导航）")
        else:
            return code, []
    elif "房间任务可从未知进" not in new_code and "def _task_entry_state(" in new_code:
        upgraded, n_up = re.subn(
            r"def _task_entry_state\(states: dict\) -> str:.*?(?=\n(?:async )?def |\Z)",
            _TASK_ENTRY_HELPER_SRC.rstrip() + "\n",
            new_code,
            count=1,
            flags=re.S,
        )
        if n_up and "房间任务可从未知进" in upgraded:
            new_code = upgraded
            notes.append("更新 _task_entry_state：房间可跳过强制回主界面")

    pattern = re.compile(
        r"(async\s+def\s+run_task\s*\([\s\S]*?\n)"
        r"([ \t]*)state_name\s*=\s*['\"]未知['\"]",
        re.M,
    )

    def _sub(m: re.Match) -> str:
        indent = m.group(2)
        return m.group(1) + f"{indent}state_name = _task_entry_state(states)"

    new_code2, n = pattern.subn(_sub, new_code, count=1)
    if n:
        notes.append("run_task 初始态改为 _task_entry_state(states)")
        new_code = new_code2

    stripped, did = _strip_foreign_state_remap(new_code)
    if did:
        new_code = stripped
        notes.append("已移除异任务态名兜底（避免改写 __exit__）")

    if notes:
        return new_code, notes
    return code, []


def patch_invalid_unicode_arrows(code: str) -> tuple[str, list[str]]:
    """替换代码区 Unicode 箭头（→）避免 SyntaxError。"""
    if "→" not in code:
        return code, []
    notes: list[str] = []
    lines = code.splitlines()
    new_lines: list[str] = []
    for i, ln in enumerate(lines):
        if "→" in ln:
            new_ln = ln.replace("→", "->")
            if new_ln != ln:
                notes.append(f"行{i + 1}: 替换 Unicode 箭头")
            ln = new_ln
        new_lines.append(ln)
    out = "\n".join(new_lines)
    if code.endswith("\n"):
        out += "\n"
    return out, notes


_CN_PUNCT_REPL = str.maketrans({
    "，": ",",
    "。": ".",
    "！": "!",
    "？": "?",
    "；": ";",
    "：": ":",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "《": "<",
    "》": ">",
    "、": ",",
})


def patch_chinese_punct_in_code(code: str) -> tuple[str, list[str]]:
    """把代码区（非字符串/注释）的中文标点换成 ASCII 等价物。"""
    if not (code or "").strip() or not _CN_PUNCT_RE.search(code):
        return code, []
    notes: list[str] = []
    out_lines: list[str] = []
    for i, line in enumerate(code.splitlines(keepends=True)):
        raw = line.rstrip("\r\n")
        ending = line[len(raw):]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
            continue
        if not _CN_PUNCT_RE.search(raw):
            out_lines.append(line)
            continue
        # 剥字符串与行尾注释后检测；替换时用简易扫描保护引号内容
        without_str = re.sub(
            r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')',
            "",
            raw,
        )
        without_str = re.sub(r"#.*$", "", without_str)
        if not _CN_PUNCT_RE.search(without_str):
            out_lines.append(line)
            continue
        new_raw = _replace_cn_punct_outside_strings(raw)
        if new_raw != raw:
            notes.append(f"行{i + 1}: 代码区中文标点→ASCII")
            out_lines.append(new_raw + ending)
        else:
            out_lines.append(line)
    if not notes:
        return code, []
    return "".join(out_lines), notes


def _replace_cn_punct_outside_strings(line: str) -> str:
    """逐字符替换，跳过字符串与 # 注释。"""
    out: list[str] = []
    i = 0
    n = len(line)
    quote: str | None = None
    while i < n:
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "#":
            out.append(line[i:])
            break
        if ch in ("'", '"'):
            # 三引号
            if line[i : i + 3] in ('"""', "'''"):
                q3 = line[i : i + 3]
                out.append(q3)
                i += 3
                end = line.find(q3, i)
                if end < 0:
                    out.append(line[i:])
                    break
                out.append(line[i:end])
                out.append(q3)
                i = end + 3
                continue
            quote = ch
            out.append(ch)
            i += 1
            continue
        out.append(ch.translate(_CN_PUNCT_REPL))
        i += 1
    return "".join(out)


_HUB_CROSS_PATCH_STATES = ("出击界面", "主界面", "未知")


def _pick_own_entry_stem(own_stems: set[str], prefixes: tuple[str, ...]) -> str | None:
    if not own_stems:
        return None
    scored: list[tuple[int, int, str]] = []
    for s in own_stems:
        low = s.lower()
        score = 50
        if prefixes and any(s.startswith(p) or p in s for p in prefixes):
            score -= 20
        if any(k in low for k in ("enter", "logo", "入口", "btn", "go_", "进入")):
            score -= 15
        if any(k in low for k in ("ok", "close", "skip", "err", "结算")):
            score += 10
        scored.append((score, len(s), s))
    scored.sort()
    return scored[0][2]


def patch_cross_task_hub_images(
    code: str,
    plan: Optional[dict] = None,
    explanation: str = "",
) -> tuple[str, list[str]]:
    """枢纽 handler 中把其它任务专属图改回本任务入口图（确定性）。

    仅改「单任务独占」的 handler；多任务共用同一函数时跳过（避免串改）。
    """
    _ = explanation  # 保留签名；无 plan 时用前缀启发式
    if not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    fns = _module_functions(tree)
    own_by, foreign_by = _plan_task_image_sets(plan)
    # handler → 出现在哪些 task var
    handler_owners: dict[str, list[str]] = {}
    for var, mapping in _iter_task_state_maps(tree):
        for hub in _HUB_CROSS_PATCH_STATES:
            h = mapping.get(hub)
            if h:
                handler_owners.setdefault(h, []).append(var)

    notes: list[str] = []
    for var, mapping in _iter_task_state_maps(tree):
        task = _task_id_from_var(var)
        prefixes = _expected_prefixes(task)
        plan_key = ""
        own: set[str] | None = None
        if own_by:
            for cand in (task, task.replace("task_", ""), re.sub(r"\s+", "", task.lower())):
                if cand in own_by:
                    plan_key = cand
                    own = set(own_by[cand])
                    break
            if own is None:
                for k, v in own_by.items():
                    if k in task or task in k:
                        plan_key = k
                        own = set(v)
                        break
        foreign_pool = foreign_by.get(plan_key) if plan_key else None

        # 无 plan：从本任务其它 handler 收集本前缀图作 own
        if own is None and prefixes:
            own = set()
            for hname in mapping.values():
                fn = fns.get(hname)
                if not fn:
                    continue
                for s in _function_img_stems(fn):
                    if any(s.startswith(p) for p in prefixes):
                        own.add(s)

        for hub in _HUB_CROSS_PATCH_STATES:
            hname = mapping.get(hub)
            if not hname or hname not in fns:
                continue
            owners = handler_owners.get(hname) or [var]
            if len(set(owners)) > 1:
                notes.append(
                    f"跳过共用 handler `{hname}`（{', '.join(sorted(set(owners)))}）；需拆函数"
                )
                continue
            fn = fns[hname]
            stems = _function_img_stems(fn)
            if not stems:
                continue
            foreign = _foreign_task_stems(
                task,
                stems,
                plan=plan,
                own_stems=own,
                foreign_pool=foreign_pool if own is not None else None,
            )
            if not foreign:
                continue
            # 替换目标：本任务入口图
            replace_with = _pick_own_entry_stem(own or set(), prefixes)
            if not replace_with and prefixes:
                # 弱回退：前缀 + logo（不造文件校验；仅修正越界引用）
                replace_with = f"{prefixes[0]}_logo"
            if not replace_with:
                continue
            changed = 0
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name) or node.func.id != "_img":
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                if not isinstance(node.args[0].value, str):
                    continue
                stem = _img_stem(node.args[0].value)
                if stem in foreign:
                    node.args[0] = ast.Constant(value=replace_with)
                    changed += 1
            if changed:
                notes.append(
                    f"{var}/{hub}: `{hname}` 越界图 {','.join(foreign[:3])} → "
                    f"_img('{replace_with}') ×{changed}"
                )

    real = [n for n in notes if "越界图" in n]
    if not real:
        return code, notes
    try:
        return ast.unparse(tree), notes
    except Exception:
        return code, notes


def patch_scene_id_nav_threshold(code: str) -> tuple[str, list[str]]:
    """场景 id（rank/*_logo）的 match_image 自动改用 CFG.nav_threshold。"""
    if not (code or "").strip():
        return code, []
    notes: list[str] = []
    scene_markers = (
        "rank", "_logo", "出击_logo", "room_logo", "jjc_logo", "ta_logo",
    )
    thresh_re = re.compile(
        r"threshold\s*=\s*CFG\.(?:threshold|icon_threshold)",
    )

    def fix_block(block: str, label: str) -> str:
        out_lines: list[str] = []
        for i, line in enumerate(block.splitlines()):
            if "match_image" not in line or "nav_threshold" in line:
                out_lines.append(line)
                continue
            in_unknown = label == "unknown_state"
            has_marker = any(m in line for m in scene_markers)
            if not has_marker and not in_unknown and "match_image(p" not in line:
                out_lines.append(line)
                continue
            new_line = thresh_re.sub("threshold=CFG.nav_threshold", line)
            if new_line != line:
                notes.append(f"{label} 行: match 改用 nav_threshold")
            out_lines.append(new_line)
        return "\n".join(out_lines)

    new_code = re.sub(
        r"(async def unknown_state\b.*?)(?=\nasync def |\ndef [a-zA-Z_\u4e00-\u9fff]+\(|\Z)",
        lambda m: fix_block(m.group(1), "unknown_state"),
        code,
        flags=re.S,
    )
    lines = new_code.splitlines()
    new_lines: list[str] = []
    for line in lines:
        if "match_image" in line and any(m in line for m in scene_markers):
            if "nav_threshold" not in line:
                new_line = thresh_re.sub("threshold=CFG.nav_threshold", line)
                if new_line != line:
                    notes.append("场景 id match 改用 nav_threshold")
                line = new_line
        new_lines.append(line)
    out = "\n".join(new_lines)
    if code.endswith("\n"):
        out += "\n"
    return out, notes


def patch_resolve_state_scene_first(code: str) -> tuple[str, list[str]]:
    """_resolve_state 须先 SCENE_TO_STEP，避免场景键误绑 __exit__ handler。"""
    wrong = (
        "    if nxt in states:\n"
        "        return nxt\n"
        "    step = scene_map.get(nxt)\n"
        "    if step and step in states:\n"
        "        return step"
    )
    right = (
        "    step = scene_map.get(nxt)\n"
        "    if step and step in states:\n"
        "        return step\n"
        "    if nxt in states:\n"
        "        return nxt"
    )
    if wrong in code:
        return code.replace(wrong, right), ["_resolve_state: SCENE_TO_STEP 优先于 states 键"]
    return code, []


def patch_bootstrap_no_unknown_start(code: str) -> tuple[str, list[str]]:
    """bootstrap 识屏失败时返回 None，禁止 resolve('未知') 锁死在识屏态。"""
    notes: list[str] = []
    old = (
        "    scene = await unknown_state(browser)\n"
        "    return _resolve_state(scene or '未知', states, scene_map)"
    )
    new = (
        "    scene = await unknown_state(browser)\n"
        "    if scene is None:\n"
        "        return None\n"
        "    return _resolve_state(scene, states, scene_map)"
    )
    if old in code:
        code = code.replace(old, new)
        notes.append("bootstrap: 无场景时返回 None，不锁 未知")
    old2 = (
        "    scene = await unknown_state(browser)\n"
        "    return _resolve_state(scene or \"未知\", states, scene_map)"
    )
    new2 = (
        "    scene = await unknown_state(browser)\n"
        "    if scene is None:\n"
        "        return None\n"
        "    return _resolve_state(scene, states, scene_map)"
    )
    if old2 in code:
        code = code.replace(old2, new2)
        notes.append("bootstrap: 无场景时返回 None，不锁 未知")
    return code, notes


def patch_run_task_escape_unknown_trap(code: str) -> tuple[str, list[str]]:
    """run_task: 禁止长期停在 未知+unknown_state 只识屏；bootstrap 跳过 未知 起跑。"""
    if "async def run_task" not in code and "def run_task" not in code:
        return code, []
    notes: list[str] = []
    new_code = code

    boot_old = "state_name = boot or _task_entry_state(states, tname)"
    boot_new = (
        "state_name = boot if boot and boot != '未知' else _task_entry_state(states, tname)"
    )
    if boot_old in new_code:
        new_code = new_code.replace(boot_old, boot_new)
        notes.append("run_task: bootstrap 跳过 未知，改从导航入口起跑")

    boot_old2 = "state_name = boot or _task_entry_state(states, task_name)"
    boot_new2 = (
        "state_name = boot if boot and boot != '未知' else _task_entry_state(states, task_name)"
    )
    if boot_old2 in new_code:
        new_code = new_code.replace(boot_old2, boot_new2)
        notes.append("run_task: bootstrap 跳过 未知，改从导航入口起跑")

    resolve_block = (
        "            resolved = _resolve_state(nxt, states, scene_map) if nxt else None\n"
        "            if resolved:\n"
        "                state_name = resolved\n"
        "                se_time = now"
    )
    resolve_new = (
        "            resolved = _resolve_state(nxt, states, scene_map) if nxt else None\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "            elif resolved == '未知' or (state_name == '未知' and not resolved):\n"
        "                state_name = _task_entry_state(states, tname)\n"
        "                se_time = now\n"
        "            elif not resolved and nxt:\n"
        "                state_name = _task_entry_state(states, tname)\n"
        "                se_time = now"
    )
    if resolve_block in new_code:
        new_code = new_code.replace(resolve_block, resolve_new)
        notes.append("run_task: 未知/未映射场景时改从导航入口执行")

    resolve_block2 = resolve_block.replace("tname", "task_name")
    resolve_new2 = resolve_new.replace("tname", "task_name")
    if resolve_block2 in new_code:
        new_code = new_code.replace(resolve_block2, resolve_new2)
        notes.append("run_task: 未知/未映射场景时改从导航入口执行")

    partial = (
        "            resolved = _resolve_state(nxt, states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "            elif not resolved and nxt:\n"
        "                state_name = _task_entry_state(states, tname)\n"
        "                se_time = now"
    )
    partial_new = (
        "            resolved = _resolve_state(nxt, states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "            elif not resolved and nxt:\n"
        "                state_name = _task_entry_state(states, tname)\n"
        "                se_time = now\n"
        "            elif resolved == '未知' or (state_name == '未知' and not resolved):\n"
        "                state_name = _task_entry_state(states, tname)\n"
        "                se_time = now"
    )
    if partial in new_code:
        new_code = new_code.replace(partial, partial_new)
        notes.append("run_task: handler 返回 未知 时逃逸到导航入口")

    partial2 = partial.replace("tname", "task_name")
    partial_new2 = partial_new.replace("tname", "task_name")
    if partial2 in new_code:
        new_code = new_code.replace(partial2, partial_new2)
        notes.append("run_task: handler 返回 未知 时逃逸到导航入口")

    # 旧范式：handler 后仅 if resolved 更新，无逃逸
    loose = (
        "        resolved = _resolve_state(nxt, states, scene_map)\n"
        "        if resolved:\n"
        "            state_name = resolved\n"
        "            se_time = now\n"
        "        elif nxt is None:\n"
        "            pass"
    )
    loose_new = (
        "        resolved = _resolve_state(nxt, states, scene_map)\n"
        "        if resolved and resolved != '未知':\n"
        "            state_name = resolved\n"
        "            se_time = now\n"
        "        elif resolved == '未知' or (state_name == '未知' and not resolved):\n"
        "            state_name = _task_entry_state(states, task_name)\n"
        "            se_time = now\n"
        "        elif not resolved and nxt:\n"
        "            state_name = _task_entry_state(states, task_name)\n"
        "            se_time = now\n"
        "        elif nxt is None:\n"
        "            pass"
    )
    if loose in new_code:
        new_code = new_code.replace(loose, loose_new)
        notes.append("run_task(范式): 未知陷阱逃逸到导航入口")

    boot_old3 = "state_name = boot if boot else _task_entry_state(states, tname)"
    boot_new3 = (
        "state_name = boot if boot and boot != '未知' else _task_entry_state(states, tname)"
    )
    if boot_old3 in new_code:
        new_code = new_code.replace(boot_old3, boot_new3)
        notes.append("run_task: bootstrap 跳过 未知 起跑")

    boot_old4 = "state_name = boot if boot else _task_entry_state(states, task_name)"
    boot_new4 = (
        "state_name = boot if boot and boot != '未知' else _task_entry_state(states, task_name)"
    )
    if boot_old4 in new_code:
        new_code = new_code.replace(boot_old4, boot_new4)
        notes.append("run_task: bootstrap 跳过 未知 起跑")

    scene_unknown = "resolved = _resolve_state(scene or \"未知\", states, scene_map)"
    scene_fix = "resolved = _resolve_state(scene, states, scene_map) if scene else None"
    if scene_unknown in new_code:
        new_code = new_code.replace(scene_unknown, scene_fix)
        notes.append("run_task: 超时重识屏不用 scene or 未知")

    scene_unknown2 = "resolved = _resolve_state(scene or '未知', states, scene_map)"
    scene_fix2 = "resolved = _resolve_state(scene, states, scene_map) if scene else None"
    if scene_unknown2 in new_code:
        new_code = new_code.replace(scene_unknown2, scene_fix2)
        notes.append("run_task: 超时重识屏不用 scene or 未知")

    # 兜底：精确块未命中时，用正则改「if resolved:」为未知逃逸
    # 注意：resolved = _resolve_state(...) if nxt else None 同行后缀，\) 后还有内容
    try:
        tree = ast.parse(new_code)
        still = _run_task_unknown_trap_errors(tree)
    except SyntaxError:
        still = ["parse_fail"]
    if still and still != ["parse_fail"]:
        tname = "task_name"
        if re.search(r"async def run_task\([^)]*\btname\b", new_code):
            tname = "tname"
        # group1=赋值行(含同行后缀)  group2=if 缩进  group3=body 缩进
        # se_time 行可选（有的范式用 continue）
        pat = re.compile(
            r"(resolved\s*=\s*_resolve_state\([^\n]*\)[^\n]*\n)"
            r"([ \t]*)if resolved:\n"
            r"([ \t]*)state_name\s*=\s*resolved\n"
            r"(?:([ \t]*)se_time\s*=\s*now\n)?",
            re.M,
        )

        def _repl(m: re.Match) -> str:
            ind = m.group(2)
            ind2 = m.group(3)
            se_line = f"{ind2}se_time = now\n"
            return (
                f"{m.group(1)}"
                f"{ind}if resolved and resolved != '未知':\n"
                f"{ind2}state_name = resolved\n"
                f"{se_line}"
                f"{ind}elif resolved == '未知' or (state_name == '未知' and not resolved):\n"
                f"{ind2}state_name = _task_entry_state(states, {tname})\n"
                f"{se_line}"
                f"{ind}elif not resolved:\n"
                f"{ind2}state_name = _task_entry_state(states, {tname})\n"
                f"{se_line.rstrip()}"
            )

        newer, nsub = pat.subn(_repl, new_code, count=5)
        if nsub:
            new_code = newer
            notes.append(f"run_task: 正则兜底补未知逃逸 ×{nsub}")

        # bootstrap: boot or entry → 跳过未知
        boot_pat = re.compile(
            r"state_name\s*=\s*boot\s+or\s+_task_entry_state\(states,\s*(tname|task_name)\)"
        )

        def _boot_repl(m: re.Match) -> str:
            return (
                f"state_name = boot if boot and boot != '未知' "
                f"else _task_entry_state(states, {m.group(1)})"
            )

        newer2, nsub2 = boot_pat.subn(_boot_repl, new_code)
        if nsub2:
            new_code = newer2
            notes.append(f"run_task: 正则兜底 bootstrap 跳过未知 ×{nsub2}")

        boot_pat2 = re.compile(
            r"state_name\s*=\s*boot\s+if\s+boot\s+else\s+_task_entry_state\(states,\s*(tname|task_name)\)"
        )

        def _boot_repl2(m: re.Match) -> str:
            return (
                f"state_name = boot if boot and boot != '未知' "
                f"else _task_entry_state(states, {m.group(1)})"
            )

        newer3, nsub3 = boot_pat2.subn(_boot_repl2, new_code)
        if nsub3:
            new_code = newer3
            notes.append(f"run_task: 正则兜底 bootstrap 跳过未知 ×{nsub3}")

    if notes:
        return new_code, notes
    return code, []


_RUN_TASK_SCENE_NONE_HOLD_PLAIN = (
    "            if scene is None:\n"
    "                browser.script_log(\"[scene] 无标识图，视为过场，保持状态\")\n"
    "                se_time = now\n"
    "                await browser.b_sleep(0.8, 1.2)\n"
    "                continue\n"
)

_RUN_TASK_SCENE_NONE_HOLD_CHROME = (
    "            if scene is None:\n"
    "                await browser.update_frame()\n"
    "                if await browser.match_image(_img('{img}'), threshold=CFG.threshold):\n"
    "                    browser.script_log(\"[scene] 无标识但有导航 chrome，非过场\")\n"
    "                    se_time = now\n"
    "                    await browser.b_sleep(0.8, 1.2)\n"
    "                    continue\n"
    "                browser.script_log(\"[scene] 无标识且无导航按钮，视为过场，保持状态\")\n"
    "                se_time = now\n"
    "                await browser.b_sleep(0.8, 1.2)\n"
    "                continue\n"
)


def _find_chrome_image_name(source_dir: str = "") -> str:
    """素材目录里真实存在时才返回可用的导航 chrome 图 stem（否则空串）。

    历史事故：范式样例教「用 home 判过场」，目录没有 home 时模型臆造
    home/home_btn/back_home → 白耗匹配 + 过场判断被带偏。
    """
    if not source_dir:
        return ""
    try:
        names = list_source_image_names(source_dir)
    except Exception:
        return ""
    low_map: dict[str, str] = {}
    for n in names:
        s = str(n)
        stem = s[:-4] if s.lower().endswith(".png") else s
        low_map[stem.lower()] = stem
    for cand in ("home", "home_btn", "back_home", "1_home", "home_1"):
        if cand in low_map:
            return low_map[cand]
    return ""


def _build_none_hold_block(source_dir: str = "") -> str:
    """按素材决定注入哪种「无标识」保持块；无 chrome 素材 → 不注入 home 引用。"""
    img = _find_chrome_image_name(source_dir)
    if img:
        return _RUN_TASK_SCENE_NONE_HOLD_CHROME.replace("{img}", img)
    return _RUN_TASK_SCENE_NONE_HOLD_PLAIN

_RUN_TASK_TIMEOUT_HOLD_OLD = [
    (
        "            scene = await unknown_state(browser)\n"
        "            resolved = _resolve_state(scene, states, scene_map) if scene else None\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            state_name = _task_entry_state(states, tname)\n"
        "            se_time = now\n"
        "            continue",
        "            scene = await unknown_state(browser)\n"
        + "{HOLD}"
        + "            resolved = _resolve_state(scene, states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            if state_name == '未知' or resolved == '未知':\n"
        "                state_name = _task_entry_state(states, tname)\n"
        "                se_time = now\n"
        "                continue\n"
        "            browser.script_log(\"[scene] 未映射场景，保持状态\")\n"
        "            se_time = now\n"
        "            await browser.b_sleep(0.8, 1.2)\n"
        "            continue",
    ),
    (
        "            scene = await unknown_state(browser)\n"
        "            resolved = _resolve_state(scene, states, scene_map) if scene else None\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            state_name = _task_entry_state(states, task_name)\n"
        "            se_time = now\n"
        "            continue",
        "            scene = await unknown_state(browser)\n"
        + "{HOLD}"
        + "            resolved = _resolve_state(scene, states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            if state_name == '未知' or resolved == '未知':\n"
        "                state_name = _task_entry_state(states, task_name)\n"
        "                se_time = now\n"
        "                continue\n"
        "            browser.script_log(\"[scene] 未映射场景，保持状态\")\n"
        "            se_time = now\n"
        "            await browser.b_sleep(0.8, 1.2)\n"
        "            continue",
    ),
    (
        "            scene = await unknown_state(browser)\n"
        "            resolved = _resolve_state(scene or \"未知\", states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            state_name = _task_entry_state(states, tname)\n"
        "            se_time = now\n"
        "            continue",
        "            scene = await unknown_state(browser)\n"
        + "{HOLD}"
        + "            resolved = _resolve_state(scene, states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            if state_name == '未知' or resolved == '未知':\n"
        "                state_name = _task_entry_state(states, tname)\n"
        "                se_time = now\n"
        "                continue\n"
        "            browser.script_log(\"[scene] 未映射场景，保持状态\")\n"
        "            se_time = now\n"
        "            await browser.b_sleep(0.8, 1.2)\n"
        "            continue",
    ),
    (
        "            scene = await unknown_state(browser)\n"
        "            resolved = _resolve_state(scene or \"未知\", states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            state_name = _task_entry_state(states, task_name)\n"
        "            se_time = now\n"
        "            continue",
        "            scene = await unknown_state(browser)\n"
        + "{HOLD}"
        + "            resolved = _resolve_state(scene, states, scene_map)\n"
        "            if resolved and resolved != '未知':\n"
        "                state_name = resolved\n"
        "                se_time = now\n"
        "                continue\n"
        "            if state_name == '未知' or resolved == '未知':\n"
        "                state_name = _task_entry_state(states, task_name)\n"
        "                se_time = now\n"
        "                continue\n"
        "            browser.script_log(\"[scene] 未映射场景，保持状态\")\n"
        "            se_time = now\n"
        "            await browser.b_sleep(0.8, 1.2)\n"
        "            continue",
    ),
]


def patch_run_task_transition_hold_on_no_scene(
    code: str, source_dir: str = ""
) -> tuple[str, list[str]]:
    """步超时重识屏：无场景标识时视为过场保持态（chrome 可选，取决于素材是否存在）。"""
    if "async def run_task" not in code:
        return code, []
    notes: list[str] = []
    hold = _build_none_hold_block(source_dir)
    new_code = code
    if "无标识且无导航按钮" in new_code or "无标识但有导航" in new_code:
        return code, []
    if "无标识图，视为过场" in new_code:
        new_code, n = patch_run_task_transition_nav_chrome(new_code, source_dir=source_dir)
        notes.extend(n)
        return new_code, notes
    for old, new in _RUN_TASK_TIMEOUT_HOLD_OLD:
        if old in new_code:
            new_code = new_code.replace(old, new.replace("{HOLD}", hold))
            notes.append("run_task: 无标识图时过场保持态+暂停步超时")
    if notes:
        return new_code, notes
    return code, []


def patch_run_task_transition_nav_chrome(
    code: str, source_dir: str = ""
) -> tuple[str, list[str]]:
    """已有过场保持块时，**仅当素材里确有导航 chrome 图**才补非过场判定。"""
    if "async def run_task" not in code:
        return code, []
    if "无标识但有导航" in code:
        return code, []
    img = _find_chrome_image_name(source_dir)
    if not img:
        # 目录没有 home/返回图：保持纯过场判定，绝不注入臆造图名
        return code, []
    old = _RUN_TASK_SCENE_NONE_HOLD_PLAIN
    if old not in code:
        return code, []
    new = _RUN_TASK_SCENE_NONE_HOLD_CHROME.replace("{img}", img)
    return code.replace(old, new), [f"run_task: 无标识+{img} 可见 → 非过场"]


def _replace_async_function_body(
    code: str, fn_name: str, body: str
) -> tuple[str, bool]:
    """替换 async def fn_name 的函数体（body 已含每行 4 空格缩进）。"""
    pat = re.compile(
        rf"(?ms)^(async def {re.escape(fn_name)}\([^)]*\)(?:\s*->[^:]+)?:\n)"
        rf"(.*?)(?=^(?:async )?def |\Z)"
    )
    m = pat.search(code)
    if not m:
        return code, False
    if not body.endswith("\n"):
        body = body + "\n"
    return code[:m.start(2)] + body + code[m.end(2):], True


def _room_claim_retry_limit_from_explanation(explanation: str) -> Optional[int]:
    expl = explanation or ""
    m = re.search(
        r"累计\s*(\d+)\s*次|(\d+)\s*次.*?(?:本任务|任务)(?:结束|完成)",
        expl,
    )
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _room_ok_loop_exit_required(explanation: str) -> bool:
    expl = explanation or ""
    return bool(
        re.search(r"没有\s*room_ok.*(?:任务结束|本任务完成)", expl, re.I)
        or re.search(r"没有room_ok", expl, re.I)
    )


def _patch_room_ok_loop_returns(code: str) -> tuple[str, int]:
    """while room_ok 循环结束后若 return 业务步，改为 __exit__。"""
    lines = code.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    fixes = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r"while\s+.*room_ok", line):
            base_indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(lines):
                ln = lines[j]
                stripped = ln.strip()
                if not stripped or stripped.startswith("#"):
                    j += 1
                    continue
                cur_indent = len(ln) - len(ln.lstrip())
                if cur_indent <= base_indent:
                    break
                j += 1
            if j < len(lines):
                m = re.match(
                    r"^(\s*)return\s+['\"](?!__exit__)([^'\"]+)['\"]",
                    lines[j],
                )
                if m:
                    ind = m.group(1)
                    out.extend(lines[i:j])
                    out.append(f"{ind}browser.script_log('  无 ok，本任务完成')\n")
                    out.append(f"{ind}return '__exit__'\n")
                    fixes += 1
                    i = j + 1
                    continue
        out.append(line)
        i += 1
    if fixes:
        return "".join(out), fixes
    return code, 0


def patch_room_ok_loop_exit_from_intro(
    code: str, explanation: str
) -> tuple[str, list[str]]:
    if not _room_ok_loop_exit_required(explanation):
        return code, []
    new_code, n = _patch_room_ok_loop_returns(code)
    if n:
        return new_code, [f"room: room_ok 循环结束后 return __exit__（×{n}）"]
    return code, []


def _jjc_refresh_offset_from_explanation(explanation: str) -> Optional[tuple[int, int]]:
    expl = explanation or ""
    if not re.search(r"jjc_刷新", expl, re.I):
        return None
    candidates: list[tuple[int, int]] = []
    for line in expl.splitlines():
        if re.search(r"jjc_刷新|刷新倍率", line, re.I):
            m = re.search(r"偏移\s*\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", line)
            if m:
                candidates.append((int(m.group(1)), int(m.group(2))))
    if candidates:
        return max(candidates, key=lambda t: abs(t[0]))
    m = re.search(r"偏移\s*\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)", expl)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def patch_jjc_refresh_offset_from_intro(
    code: str, explanation: str
) -> tuple[str, list[str]]:
    offset = _jjc_refresh_offset_from_explanation(explanation)
    if offset is None:
        return code, []
    ox, oy = offset
    lines = code.splitlines(keepends=True)
    out: list[str] = []
    fixes = 0
    for line in lines:
        if "jjc_刷新" in line and re.search(r"pianyi=\(-20,\s*0\)", line):
            line = re.sub(r"pianyi=\(-20,\s*0\)", f"pianyi=({ox}, {oy})", line)
            fixes += 1
        out.append(line)
    if fixes:
        return "".join(out), [f"jjc_刷新: pianyi (-20,0) → ({ox},{oy}) ×{fixes}"]
    return code, []


def patch_jjc_duanwei_max_x_from_intro(
    code: str, explanation: str
) -> tuple[str, list[str]]:
    """介绍写 jjc_段位 x 最大时，修正 match_image_multi 的 max 轴为 x。"""
    expl = explanation or ""
    if not re.search(r"jjc_段位", expl, re.I):
        return code, []
    if not re.search(r"x\s*最大|x最大|最靠右", expl, re.I):
        return code, []
    notes: list[str] = []
    patterns = [
        (
            r"max\s*\(\s*matches\s*,\s*key\s*=\s*lambda\s+m:\s*m\[\s*['\"]y['\"]\s*\]",
            "max(matches, key=lambda m: m['x'])",
        ),
        (
            r"max\s*\(\s*matches\s*,\s*key\s*=\s*lambda\s+m:\s*m\.y\s*\)",
            "max(matches, key=lambda m: m['x'])",
        ),
    ]
    for wrong, right in patterns:
        if re.search(wrong, code):
            code = re.sub(wrong, right, code)
            notes.append("jjc_段位: max(matches) 改为按 x 取最大")
    return code, notes


def _build_room_claim_handler_body(
    limit: int,
    ret_state: str,
    back_state: str,
) -> str:
    """生成房间领取 handler 体（点收取后无 ok 累计达限 __exit__）。"""
    lines = [
        f"    browser.script_log(\"[room][claim]\")",
        "    await browser.update_frame()",
        f"    if not await browser.match_image(_img('room_logo'), threshold=CFG.nav_threshold):",
        f"        return \"{back_state}\"",
        "    if await browser.match_image(_img('room_ap上限'), threshold=CFG.nav_threshold):",
        "        browser.script_log(\"  AP 上限，本任务完成\")",
        "        return \"__exit__\"",
        "    if await browser.match_image(_img('room_ok'), threshold=CFG.threshold):",
        "        browser.script_log(\"  仅有 room_ok 弹窗，先关闭\")",
        "        for _ in range(5):",
        "            if not await browser.click_image(_img('room_ok'), threshold=CFG.threshold):",
        "                break",
        "            await browser.b_sleep(0.5, 0.8)",
        "            await browser.update_frame()",
        "            if not await browser.match_image(_img('room_ok'), threshold=CFG.threshold):",
        "                break",
        "        await browser.update_frame()",
        "        if not await browser.match_image(_img('room_收取奖励'), threshold=CFG.threshold):",
        "            browser.script_log(\"  关 ok 后无收取，本任务完成\")",
        "            return \"__exit__\"",
        "    if not await browser.match_image(_img('room_收取奖励'), threshold=CFG.threshold):",
        "        browser.script_log(\"  无收取按钮且无 ok，本任务完成\")",
        "        return \"__exit__\"",
        f"    max_no_ok = {limit}",
        "    for attempt in range(1, max_no_ok + 1):",
        "        if not await browser.click_image(_img('room_收取奖励'), threshold=CFG.threshold):",
        "            return None",
        "        browser.script_log(f\"  已点收取，等待 ok ({attempt}/{max_no_ok})\")",
        "        await browser.b_sleep(1.5, 1.5)",
        "        await browser.update_frame()",
        "        if await browser.match_image(_img('room_ok'), threshold=CFG.threshold):",
        "            while await browser.match_image(_img('room_ok'), threshold=CFG.threshold):",
        "                await browser.click_image(_img('room_ok'), threshold=CFG.threshold)",
        "                await browser.b_sleep(0.5, 0.8)",
        "                await browser.update_frame()",
        "            browser.script_log(\"  无 ok，本任务完成\")",
        "            return \"__exit__\"",
        "        if attempt >= max_no_ok:",
        "            browser.script_log(\"  无 ok 累计达上限，本任务完成\")",
        "            return \"__exit__\"",
        "    return None",
    ]
    return "\n".join(lines) + "\n"


def patch_room_claim_retry_from_intro(
    code: str, explanation: str
) -> tuple[str, list[str]]:
    """介绍含 room 收取 + 累计次数结束时，注入标准领取/确认窗/计数逻辑。"""
    expl = explanation or ""
    limit = _room_claim_retry_limit_from_explanation(expl)
    if limit is None:
        return code, []
    if not re.search(r"room_ok|收取奖励|room_收取", expl, re.I):
        return code, []
    notes: list[str] = []
    if "room_confirm" in code:
        code = code.replace("room_confirm", "room_ok")
        notes.append("room: room_confirm → room_ok")
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, notes
    back_state = "返回主界面"
    if "返回主界面" in code:
        back_state = "返回主界面"
    fns_to_patch: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        stems = _function_img_stems(node)
        if not any("收取奖励" in s or s == "room_收取奖励" for s in stems):
            continue
        has_ok = any("room_ok" in s for s in stems)
        try:
            src = ast.unparse(node)
        except Exception:
            src = ""
        good_retry = bool(
            re.search(
                rf"range\s*\(\s*1,\s*max_no_ok\s*\+\s*1|"
                rf"range\s*\(\s*{limit}|attempt\s*>=\s*max_no_ok|"
                rf"(?:>=|==)\s*{limit}",
                src,
            )
            and "room_ok" in src
            and "__exit__" in src
            and re.search(
                r"无 ok.*本任务完成|无 ok，本任务完成",
                src,
            )
            and not re.search(
                r"while\s+await\s+browser\.match_image\([^)]*room_ok"
                r"[\s\S]{0,1200}?return\s+[\"'](?!__exit__)",
                src,
            )
        )
        if good_retry:
            continue
        ret_state = node.name.replace("step_", "")
        if ret_state == node.name:
            ret_state = "房间领体力"
        fns_to_patch.append((node.name, ret_state))
    for fn_name, ret_state in fns_to_patch:
        body = _build_room_claim_handler_body(limit, ret_state, back_state)
        code, ok = _replace_async_function_body(code, fn_name, body)
        if ok:
            notes.append(f"room: 重写 {fn_name} 领取+ok累计{limit}次逻辑")
    return code, notes


def patch_multitask_scene_to_step_hubs(code: str) -> tuple[str, list[str]]:
    """为各 TASK*_SCENE_TO_STEP 补 hub/跨任务场景 → 可路由步名，避免识屏后无法执行。"""
    if "SCENE_TO_STEP" not in code:
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    scene_keys = _unknown_state_scene_keys(tree)
    if not scene_keys:
        return code, []
    state_maps = {
        var: set(mapping.keys())
        for var, mapping in _iter_all_state_maps(tree)
    }
    notes: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        t = node.targets[0]
        if not isinstance(t, ast.Name) or "SCENE_TO_STEP" not in t.id:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        d = node.value
        existing = {
            k.value
            for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        tid = t.id.lower()
        states_keys: set[str] = set()
        for svar, keys in state_maps.items():
            if tid.replace("_scene_to_step", "") in svar.lower():
                states_keys |= keys
            elif tid.startswith("task1") and "task1" in svar.lower():
                states_keys |= keys
            elif tid.startswith("task2") and "task2" in svar.lower():
                states_keys |= keys
            elif tid.startswith("task3") and "task3" in svar.lower():
                states_keys |= keys
        if not states_keys:
            continue
        is_room = "task1" in tid or "room" in tid
        nav_home = "返回主界面" if "返回主界面" in states_keys else None
        nav_sortie = "返回出击界面" if "返回出击界面" in states_keys else None
        additions: list[tuple[str, str]] = []
        if "主界面" in scene_keys and "主界面" not in existing:
            step = nav_home or next(
                (x for x in ("房间领体力", "主界面到房间") if x in states_keys),
                None,
            )
            if step:
                additions.append(("主界面", step))
        if "出击界面" in scene_keys and "出击界面" not in existing:
            step = nav_sortie or next(
                (
                    x
                    for x in (
                        "进入竞技场",
                        "竞技场入口",
                        "点击竞技场",
                        "进入塔",
                        "塔入口",
                    )
                    if x in states_keys
                ),
                None,
            )
            if step:
                additions.append(("出击界面", step))
        for scene in sorted(scene_keys):
            if scene in existing or scene in ("主界面", "出击界面"):
                continue
            if scene == "房间界面" and is_room:
                step = next(
                    (
                        x
                        for x in (
                            "房间领体力",
                            "检查收取奖励",
                            "房间收取奖励",
                            "房间领体力",
                        )
                        if x in states_keys
                    ),
                    None,
                )
            elif scene in ("竞技场界面", "竞技场"):
                step = next(
                    (
                        x
                        for x in (
                            "进入竞技场",
                            "竞技场刷新倍率",
                            "检查次数",
                            "竞技场检查次数",
                        )
                        if x in states_keys
                    ),
                    None,
                )
            elif scene in ("塔界面", "塔"):
                step = next(
                    (
                        x
                        for x in ("进入塔", "塔选难度", "检查塔次数")
                        if x in states_keys
                    ),
                    None,
                )
            else:
                step = nav_home if is_room else nav_sortie
            if step and step in states_keys:
                additions.append((scene, step))
        for scene, step in additions:
            d.keys.append(ast.Constant(value=scene))
            d.values.append(ast.Constant(value=step))
            notes.append(f"{t.id} 补 {scene!r} -> {step!r}")
    if not notes:
        return code, []
    try:
        return ast.unparse(tree), notes
    except Exception:
        return code, []


def patch_drop_unused_copy_tables(code: str) -> tuple[str, list[str]]:
    """删除「复制自 STATES / STATE_TIMEOUT / *_STATES 但从未被引用」的假多任务壳表。

    对应生成缺陷：TASK1/TASK2_STATES = STATES.copy() 等复制表 + 双份 SCENE_MAP，
    do_work 实际只跑一张表 —— 冗余表会误导修复轮与审查，直接清掉。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    assigned_top: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = (
                [stmt.target]
                if isinstance(stmt, ast.AnnAssign)
                else stmt.targets
            )
            for t in targets or []:
                if isinstance(t, ast.Name):
                    assigned_top.add(t.id)
    loaded: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            loaded.add(n.id)
    keep: list[ast.stmt] = []
    dropped: list[str] = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            keep.append(stmt)
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name):
            keep.append(stmt)
            continue
        name = target.id
        if not (
            name.endswith("_STATES")
            or name.endswith("_TIMEOUT")
            or name.endswith("_SCENE_MAP")
        ):
            keep.append(stmt)
            continue
        v = stmt.value
        src = ""
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "copy":
            if isinstance(v.func.value, ast.Name):
                src = v.func.value.id
        elif isinstance(v, ast.Name):
            src = v.id
        if src in assigned_top and src != name and name not in loaded:
            dropped.append(name)
            continue
        keep.append(stmt)
    if not dropped:
        return code, []
    try:
        tree.body = keep
        new_code = ast.unparse(tree)
    except Exception:
        return code, []
    return new_code, [f"删除未使用的复制表: {', '.join(sorted(dropped))}"]


def patch_run_task_boot_park_entry(code: str) -> tuple[str, list[str]]:
    """run_task 启动：bootstrap 失败不得停在「未知」，直落 _task_entry_state 入口。

    把 `state_name = boot if boot and boot != '未知' else '未知'` 的停车写法
    改为 else 分支走 _task_entry_state(states, task_name)。
    """
    if "def run_task" not in code:
        return code, []
    if "_task_entry_state" not in code or "states" not in code:
        return code, []
    tname = "task_name" if "task_name" in code else ("tname" if "tname" in code else "")
    if not tname:
        return code, []
    pat = re.compile(
        r"(?m)^(?P<ind>[ \t]*)state_name\s*=\s*boot\s+if\s+boot\s+and\s+boot\s*"
        r"!=\s*(['\"])(未知)\2\s+else\s+(['\"])(未知)\4\s*$"
    )
    m = pat.search(code)
    if not m:
        return code, []
    expr = (
        f"state_name = boot if boot and boot != '未知' "
        f"else _task_entry_state(states, {tname})"
    )
    repl = f"{m.group('ind')}{expr}"
    new_code = code[: m.start()] + repl + code[m.end() :]
    return new_code, ["run_task: bootstrap 无场景直落 _task_entry_state，不停「未知」"]



def patch_task_table_contract(code: str, plan: Optional[dict]) -> tuple[str, list[str]]:
    """把 TASK1_STATES 这类自造表名对齐成 plan 要求的 TASK_<taskid>_STATES，并补缺的超时表。

    对应硬失败：校验器按 plan 要求 TASK_taskN_STATES/超时表，模型却写 TASK1_STATES，
    没有确定性补丁时只能靠 LLM 反复改名，5 轮耗尽即生成失败。
    """
    if not plan:
        return code, []
    try:
        from backend.script_generator.graph.plan_schema import (
            normalize_plan,
            task_states_var,
            task_timeout_var,
        )
    except Exception:
        return code, []
    tasks = (normalize_plan(plan).get("tasks") or [])
    if not tasks:
        return code, []
    names = set(re.findall(r"(?m)^(TASK[A-Za-z0-9_]*_(?:STATES|TIMEOUT))\s*=", code))
    rename: dict[str, str] = {}
    for i, task in enumerate(tasks):
        want_s, want_t = task_states_var(task, i), task_timeout_var(task, i)
        for alt, want in ((f"TASK{i + 1}_STATES", want_s), (f"TASK{i + 1}_TIMEOUT", want_t)):
            if alt in names and want not in names:
                rename[alt] = want
    notes: list[str] = []
    new_code = code
    for alt, want in rename.items():
        new_code = re.sub(rf"\b{re.escape(alt)}\b", want, new_code)
        notes.append(f"表名对齐 {alt} → {want}")
    for i, task in enumerate(tasks):
        want_s, want_t = task_states_var(task, i), task_timeout_var(task, i)
        if re.search(rf"(?m)^{re.escape(want_t)}\s*=", new_code):
            continue
        if not re.search(rf"(?m)^{re.escape(want_s)}\s*=", new_code):
            continue
        try:
            tree = ast.parse(new_code)
        except SyntaxError:
            break
        d = _find_module_dict_assign(tree, want_s)
        keys = sorted(_dict_literal_keys(d)) if d else []
        if not keys:
            continue
        body = ", ".join(
            f'"{k}": ' + ("180" if ("等待" in k or "转场" in k) else "60") for k in keys
        )
        new_code = new_code.rstrip() + f"\n\n{want_t} = {{{body}}}\n"
        notes.append(f"补超时表 {want_t}（{len(keys)} 键）")
    if not notes:
        return code, []
    return new_code, notes


def patch_register_explanation_helpers(code: str, explanation: str) -> tuple[str, list[str]]:
    """介绍里点名的 @辅助步骤若既无状态键也无同名 handler，补同名占位 handler。

    对应硬失败：「介绍辅助步骤 X 须在 STATES/TASK*_STATES 注册为状态键或实现同名 handler」
    无确定性补丁时，模型 5 轮都补不齐。占位 handler 带 codegen 标记，便于人工补齐与校验放行。
    """
    helpers = _explanation_helper_names(explanation or "")
    if not helpers:
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    existing = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    table_keys: set[str] = set()
    for _, mapping in _iter_all_state_maps(tree):
        table_keys.update(str(k) for k in mapping.keys())
    todo = [h for h in helpers if h not in existing and h not in table_keys]
    if not todo:
        return code, []
    blocks = []
    for h in todo:
        safe = re.sub(r"\W", "_", h) or "helper"
        blocks.append(
            f"\n\nasync def {safe}(browser) -> StateName:\n"
            f'    """codegen:helper-stub — 介绍辅助步骤「{h}」占位；补齐动作后请删标记。"""\n'
            f"    await browser.update_frame()\n"
            f"    return None\n"
        )
    return code.rstrip() + "".join(blocks), [f"补介绍辅助步骤 handler: {', '.join(todo)}"]


def patch_resolve_empty_stub_handlers(code: str) -> tuple[str, list[str]]:
    """把状态表里绑定的空壳业务步（step_*）降级为场景桩（stub_*）。

    对应失败：「`step_确定属性` 是空壳 handler」——短时间无法推断业务动作时，
    降级为桩（范式允许 stub_* 只 return 下一业务步）比留空壳更安全。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    fns = _module_functions(tree)
    bound: set[str] = set()
    for _, mapping in _iter_all_state_maps(tree):
        bound.update(str(v) for v in mapping.values())
    rename: dict[str, str] = {}
    for name, fn in fns.items():
        if name.startswith("stub_") or name in ("unknown_state", "run_task", "do_work"):
            continue
        if not (name.startswith("step_") or name.startswith("task")):
            continue
        if name not in bound or not _function_is_empty_stub(fn):
            continue
        new = "stub_" + (name[5:] if name.startswith("step_") else name)
        if new in fns or new in bound:
            continue
        rename[name] = new
    if not rename:
        return code, []
    new_code = code
    for old, new in rename.items():
        new_code = re.sub(rf"\b{re.escape(old)}\b", new, new_code)
    return new_code, [
        "空壳业务步降级为场景桩: " + ", ".join(f"{o}→{n}" for o, n in rename.items())
    ]



_TASK_STATES_NAME_RE = re.compile(r"^TASK_?(?:task)?(\d+)_STATES$")
_FOREIGN_HANDLER_RE = re.compile(r"^task(\d+)_(.+)$")


def _shallow_loads(node: ast.AST) -> set[str]:
    """只收集语句自身的 Name 读取，不下钻嵌套函数/类体（那些是惰性求值）。"""
    out: set[str] = set()

    def rec(n: ast.AST) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.add(n.id)
        for ch in ast.iter_child_nodes(n):
            rec(ch)

    if isinstance(node, ast.Assign):
        rec(node.value)
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        rec(node.value)
    else:
        rec(node)
    return out


def _module_def_index(tree: ast.AST) -> dict[str, int]:
    idx: dict[str, int] = {}
    for i, stmt in enumerate(tree.body):
        names: list[str] = []
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.append(stmt.target.id)
        for n in names:
            idx.setdefault(n, i)
    return idx


def _forward_ref_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """返回 [(语句索引, 被前向引用的名字)]——模块导入时会 NameError。"""
    def_idx = _module_def_index(tree)
    out: list[tuple[int, str]] = []
    for i, stmt in enumerate(tree.body):
        for name in _shallow_loads(stmt):
            j = def_idx.get(name)
            if j is not None and j > i:
                out.append((i, name))
    return out


def _module_level_forward_ref_errors(tree: ast.AST) -> list[str]:
    """模块级语句引用了其后才定义的顶层名字 → import 期 NameError。"""
    errors: list[str] = []
    for i, name in _forward_ref_violations(tree):
        stmt = tree.body[i]
        lineno = getattr(stmt, "lineno", 0)
        errors.append(
            f"第 {lineno} 行模块级引用了其后才定义的 `{name}`："
            "模块导入时会 NameError；请把该赋值移到定义之后"
        )
    seen = set()
    uniq = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq[:6]


def _task_table_foreign_handler_errors(tree: ast.AST) -> list[str]:
    """TASK_taskN_STATES 里绑定 taskM_* (M≠N) 的 handler → 跨任务串表。"""
    errors: list[str] = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(stmt.value, ast.Dict):
            continue
        m = _TASK_STATES_NAME_RE.match(target.id)
        if not m:
            continue
        own = m.group(1)
        for k, v in zip(stmt.value.keys, stmt.value.values):
            if not isinstance(v, ast.Name):
                continue
            fm = _FOREIGN_HANDLER_RE.match(v.id)
            if not fm or fm.group(1) == own:
                continue
            key = k.value if isinstance(k, ast.Constant) else "?"
            errors.append(
                f"{target.id}「{key}」绑定了另一个任务的 `{v.id}`（跨任务串表）："
                f"应绑 task{own}_* 的同名 handler，或删掉该键由本任务补齐"
            )
    seen = set()
    out = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:8]


def patch_rebind_foreign_task_handlers(code: str) -> tuple[str, list[str]]:
    """跨任务串表修复：taskN 表里的 taskM_* 改绑本任务同名 handler，没有则删键。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    top_fns = {
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    notes: list[str] = []
    changed = False
    drop_keys: dict[str, set[str]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(stmt.value, ast.Dict):
            continue
        m = _TASK_STATES_NAME_RE.match(target.id)
        if not m:
            continue
        own = m.group(1)
        keys, values = [], []
        removed: set[str] = set()
        for k, v in zip(stmt.value.keys, stmt.value.values):
            rebind = None
            if isinstance(v, ast.Name):
                fm = _FOREIGN_HANDLER_RE.match(v.id)
                if fm and fm.group(1) != own:
                    cand = f"task{own}_{fm.group(2)}"
                    if cand in top_fns:
                        rebind = cand
                    else:
                        key = k.value if isinstance(k, ast.Constant) else None
                        if key is not None:
                            removed.add(key)
                        notes.append(f"{target.id} 删除跨任务键「{key}」({v.id})")
                        changed = True
                        continue
            keys.append(k)
            values.append(ast.Name(id=rebind, ctx=ast.Load()) if rebind else v)
            if rebind:
                notes.append(f"{target.id}「{k.value}」{v.id} → {rebind}")
                changed = True
        stmt.value.keys = keys
        stmt.value.values = values
        if removed:
            drop_keys[target.id.replace("_STATES", "_TIMEOUT")] = removed
    if drop_keys:
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name) or not isinstance(stmt.value, ast.Dict):
                continue
            bad = drop_keys.get(target.id)
            if not bad:
                continue
            keys, values = [], []
            for k, v in zip(stmt.value.keys, stmt.value.values):
                if isinstance(k, ast.Constant) and k.value in bad:
                    continue
                keys.append(k)
                values.append(v)
            stmt.value.keys = keys
            stmt.value.values = values
            changed = True
    if not changed:
        return code, []
    try:
        return ast.unparse(tree), notes[:12]
    except Exception:
        return code, []


def patch_define_before_use(code: str) -> tuple[str, list[str]]:
    """把「引用了其后才定义的名字」的模块级语句移到定义之后（消除 import 期 NameError）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    notes: list[str] = []
    for _ in range(12):
        viol = _forward_ref_violations(tree)
        if not viol:
            break
        i, name = viol[0]
        def_idx = _module_def_index(tree)
        target_idx = def_idx.get(name, i)
        stmt = tree.body[i]
        del tree.body[i]
        insert_at = min(len(tree.body), target_idx if target_idx > i else i)
        tree.body.insert(insert_at, stmt)
        notes.append(
            f"前向引用修复: 把第 {getattr(stmt, 'lineno', 0)} 行语句移到 `{name}` 定义之后"
        )
    if not notes:
        return code, []
    try:
        return ast.unparse(tree), notes[:6]
    except Exception:
        return code, []


def apply_codegen_patches(
    code: str,
    *,
    source_dir: str = "",
    plan: Optional[dict] = None,
    explanation: str = "",
    free_mode: Optional[bool] = None,
) -> tuple[str, list[str]]:
    """生成/修订共用本地补全。原则：少改、改准；不改写 __exit__；流程以介绍为准。"""
    free = is_codegen_free_mode(free_mode)
    if free:
        notes: list[str] = []  # 仅在确有改动时标记，避免“没改也报已补全”
        pseudo = build_pseudo_plan_from_explanation(explanation)
        # 有 LLM plan.tasks 时优先；scene_driven 伪计划 tasks=[] 时仍用其 architecture
        if plan and (plan.get("tasks") or []):
            plan_use = plan
        else:
            plan_use = pseudo if pseudo else (plan or {})
        try:
            from backend.script_generator.architecture import apply_architecture_to_plan
            plan_use = apply_architecture_to_plan(dict(plan_use or {}), explanation or "")
        except Exception:
            pass
        scene_locked = (
            (plan_use or {}).get("architecture") == "scene_driven"
            or is_scene_driven_explanation(explanation or "")
        )
        code, n = patch_invalid_unicode_arrows(code)
        notes.extend(n)
        code, n = patch_chinese_punct_in_code(code)
        notes.extend(n)
        code, n = patch_scene_id_nav_threshold(code)
        notes.extend(n)
        code, n = patch_resolve_state_scene_first(code)
        notes.extend(n)
        code, n = patch_bootstrap_no_unknown_start(code)
        notes.extend(n)
        code, n = patch_run_task_escape_unknown_trap(code)
        notes.extend(n)
        code, n = patch_run_task_transition_hold_on_no_scene(code, source_dir)
        notes.extend(n)
        code, n = patch_room_claim_retry_from_intro(code, explanation)
        notes.extend(n)
        code, n = patch_room_ok_loop_exit_from_intro(code, explanation)
        notes.extend(n)
        code, n = patch_jjc_refresh_offset_from_intro(code, explanation)
        notes.extend(n)
        code, n = patch_jjc_duanwei_max_x_from_intro(code, explanation)
        notes.extend(n)
        code, n = patch_multitask_scene_to_step_hubs(code)
        notes.extend(n)
        if not scene_locked:
            code, n = patch_ensure_multitask_skeleton(code, explanation)
            notes.extend(n)
        code, n = patch_missing_task_state_keys(code, plan_use)
        notes.extend(n)
        code, n = patch_rebind_foreign_task_handlers(code)
        notes.extend(n)
        code, n = patch_define_before_use(code)
        notes.extend(n)
        code, n = patch_task_table_contract(code, plan_use)
        notes.extend(n)
        code, n = patch_register_explanation_helpers(code, explanation)
        notes.extend(n)
        code, n = patch_resolve_empty_stub_handlers(code)
        notes.extend(n)
        code, n = patch_missing_handler_return_keys(code)
        notes.extend(n)
        code, n = patch_cross_task_hub_images(code, plan_use, explanation)
        notes.extend(n)
        code, n = patch_run_task_boot_park_entry(code)
        notes.extend(n)
        code, n = patch_drop_unused_copy_tables(code)
        notes.extend(n)
        code, n = patch_settlement_click_priority(code)
        notes.extend(n)
        code, n = patch_exit_require_complete_from_intro(
            code, explanation=explanation or "", source_dir=source_dir or ""
        )
        notes.extend(n)
        code, n = patch_guard_identifier_pairs(code, explanation=explanation or "")
        notes.extend(n)
        # 幻觉图 + 介绍未点名盘内图：自由模式也剥离
        if source_dir:
            code, n = patch_strip_missing_image_refs(
                code, source_dir, explanation=explanation or ""
            )
            notes.extend(n)
        code, n = patch_missing_stdlib_imports(code)
        notes.extend(n)
        code, n = patch_run_task_entry_helper(code)
        notes.extend(n)
        if not scene_locked:
            code, n = patch_do_work_multitask_loop(code, explanation)
            notes.extend(n)
        code, n = patch_rebind_foreign_task_handlers(code)
        notes.extend(n)
        code, n = patch_define_before_use(code)
        notes.extend(n)
        code, n = patch_do_work_dual_target(code)
        notes.extend(n)
        if notes:
            notes.insert(0, "自由模式：结构 patch")
        return code, notes
    notes: list[str] = []
    # 硬错误类：缺键 / 缺图 / 缺 import
    code, n = patch_missing_task_state_keys(code, plan)
    notes.extend(n)
    code, n = patch_rebind_foreign_task_handlers(code)
    notes.extend(n)
    code, n = patch_define_before_use(code)
    notes.extend(n)
    code, n = patch_task_table_contract(code, plan)
    notes.extend(n)
    code, n = patch_register_explanation_helpers(code, explanation)
    notes.extend(n)
    code, n = patch_resolve_empty_stub_handlers(code)
    notes.extend(n)
    code, n = patch_missing_handler_return_keys(code)
    notes.extend(n)
    if source_dir:
        code, n = patch_strip_missing_image_refs(
            code, source_dir, explanation=explanation or ""
        )
        notes.extend(n)
    code, n = patch_missing_stdlib_imports(code)
    notes.extend(n)
    code, n = patch_invalid_unicode_arrows(code)
    notes.extend(n)
    code, n = patch_chinese_punct_in_code(code)
    notes.extend(n)
    code, n = patch_scene_id_nav_threshold(code)
    notes.extend(n)
    code, n = patch_task_scene_aliases(code)
    notes.extend(n)
    code, n = patch_state_timeout_alias(code)
    notes.extend(n)
    # 与介绍对齐的窄修复（不注入额外控制流）
    code, n = patch_go_home_return_main(code)
    notes.extend(n)
    code, n = patch_nav_helper_return_unknown(code)
    notes.extend(n)
    code, n = patch_go_sortie_fallback_home(code)
    notes.extend(n)
    code, n = patch_jjc_home_to_sortie(code)
    notes.extend(n)
    code, n = patch_bootstrap_no_unknown_start(code)
    notes.extend(n)
    code, n = patch_run_task_escape_unknown_trap(code)
    notes.extend(n)
    code, n = patch_run_task_transition_hold_on_no_scene(code, source_dir)
    notes.extend(n)
    code, n = patch_run_task_entry_helper(code)
    notes.extend(n)
    code, n = patch_cross_task_hub_images(code, plan, explanation)
    notes.extend(n)
    code, n = patch_run_task_boot_park_entry(code)
    notes.extend(n)
    code, n = patch_drop_unused_copy_tables(code)
    notes.extend(n)
    code, n = patch_settlement_click_priority(code)
    notes.extend(n)
    code, n = patch_exit_require_complete_from_intro(
        code, explanation=explanation or "", source_dir=source_dir or ""
    )
    notes.extend(n)
    code, n = patch_guard_identifier_pairs(code, explanation=explanation or "")
    notes.extend(n)
    # 最终清理：跨任务键 / 前向引用（任何更早 patch 塞回的都在这消掉）
    code, n = patch_rebind_foreign_task_handlers(code)
    notes.extend(n)
    code, n = patch_define_before_use(code)
    notes.extend(n)
    code, n = patch_do_work_dual_target(code)
    notes.extend(n)
    return code, notes


def validate_codegen_patched(
    code: str,
    *,
    plan: Optional[dict] = None,
    source_dir: str = "",
    image_paths: Optional[list] = None,
    explanation: str = "",
    free_mode: Optional[bool] = None,
) -> tuple[str, list[str], list[str]]:
    """先 patch 再 validate。返回 (patched_code, patch_notes, errors)。"""
    free = is_codegen_free_mode(free_mode)
    patched, patch_notes = apply_codegen_patches(
        code,
        source_dir=source_dir,
        plan=plan,
        explanation=explanation,
        free_mode=free,
    )
    errors = validate_generated_code(
        patched,
        plan=plan,
        source_dir=source_dir,
        image_paths=image_paths,
        explanation=explanation,
        free_mode=free,
    )
    return patched, patch_notes, errors


def _is_shared_hub_nav_handler(handler: str, fns: dict[str, ast.AST]) -> bool:
    """共用「返回出击界面」类纯导航 handler 允许（不含它任务入口图）。"""
    fn = fns.get(handler)
    if fn is None:
        return False
    lname = handler.lower()
    if any(x in lname for x in ("sortie", "出击", "chuji", "nav", "hub", "home")):
        pass
    stems = _function_img_stems(fn)
    if not stems:
        return any(x in lname for x in ("sortie", "出击", "home", "nav"))
    return not any(s.startswith(("jjc", "room", "ta")) for s in stems)


def _is_unknown_state_fn(fn: ast.AST) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    lname = fn.name.lower()
    return "unknown" in lname or "未知" in fn.name


def _unknown_state_fns(tree: ast.AST):
    for fn in tree.body:
        if _is_unknown_state_fn(fn):
            yield fn


def _unknown_state_returns_unknown_on_match(tree: ast.AST) -> list[str]:
    """unknown_state 禁止 return '未知'（命中应返回业务名，未命中应 return None）。"""
    errors: list[str] = []
    for fn in _unknown_state_fns(tree):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return):
                continue
            val = node.value
            if isinstance(val, ast.Constant) and val.value in ("未知", "\u672a\u77e5"):
                errors.append(
                    f"{fn.name}@{getattr(node, 'lineno', 0)}: "
                    f"unknown_state 禁止 return '未知'；命中返回业务状态名，未命中 b_sleep 后 return None"
                )
    return errors


_FILE_KEY_RE = re.compile(r"(?i)\.(png|jpg|jpeg|webp)$")
_STEM_AS_KEY_RE = re.compile(r"(?i)^(logo|rank|.+_logo|.+_id|\d+_[a-z0-9_]+)$")
_SCENE_ID_NAME_RE = re.compile(r"(?i)^(logo|rank|.+_logo|.+_id)$")
_DISMISS_IMG_RE = re.compile(
    r"(?i)(?:^|_|-)(close|skip|err|ok|cancel)(?:$|_|-)|关闭|取消"
)
_SKIP_CLICK_WAIT_FN = {
    "do_work", "run_task", "check_guards", "login", "unknown_state",
}
_GOAL_EXIT_RE = re.compile(
    r"(脚本结束|任务结束|任务完成|完成登录|登录完成|"
    r"出现.{0,16}就.{0,8}(结束|完成|退出)|"
    r"(rank|等级).{0,12}(结束|完成|退出))",
    re.I,
)


def _first_img_arg(call: ast.Call) -> str | None:
    if not call.args:
        return None
    arg0 = call.args[0]
    if isinstance(arg0, ast.Call) and isinstance(arg0.func, ast.Name) and arg0.func.id == "_img":
        if arg0.args and isinstance(arg0.args[0], ast.Constant) and isinstance(arg0.args[0].value, str):
            return arg0.args[0].value
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        return arg0.value
    return None


def _call_uses_nav_threshold(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg != "threshold":
            continue
        v = kw.value
        return isinstance(v, ast.Attribute) and v.attr == "nav_threshold"
    return False


def _img_stem(name: str) -> str:
    return _norm_img_key(name)


def _unknown_state_filename_keys(tree: ast.AST) -> list[str]:
    """unknown_state 里 dict 的 key 必须是业务状态名，不能是文件名。"""
    errors: list[str] = []
    states_keys: set[str] = set()
    assigned = _collect_assigned_names(tree)
    for name in assigned:
        if name == "STATES" or name.endswith("_STATES"):
            d = _find_module_dict_assign(tree, name)
            if d:
                states_keys |= _dict_literal_keys(d)
    states_keys.discard("未知")
    states_keys.discard("\u672a\u77e5")

    for fn in _unknown_state_fns(tree):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                    continue
                key = k.value
                img = None
                if (
                    isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id == "_img"
                    and v.args
                    and isinstance(v.args[0], ast.Constant)
                    and isinstance(v.args[0].value, str)
                ):
                    img = v.args[0].value
                if key in states_keys:
                    continue
                looks_file = bool(_FILE_KEY_RE.search(key) or "/" in key or "\\" in key)
                stem = _img_stem(img) if img else ""
                looks_stem = bool(img and key == stem and _STEM_AS_KEY_RE.match(key))
                if looks_file or looks_stem:
                    errors.append(
                        f"{fn.name}@{getattr(node, 'lineno', 0)}: "
                        f"unknown_state 字典 key 必须是业务状态名，不能是文件名 {key!r}"
                    )
    return errors


def _unknown_state_miss_must_sleep(tree: ast.AST) -> list[str]:
    """未命中应 b_sleep 后 return None。"""
    errors: list[str] = []
    for fn in _unknown_state_fns(tree):
        methods = {m for m, _ in _iter_browser_calls(fn)}
        has_match = bool(methods & {"match_image", "match_image_multi"})
        has_sleep = "b_sleep" in methods
        has_none = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return):
                continue
            if node.value is None:
                has_none = True
            elif isinstance(node.value, ast.Constant) and node.value.value is None:
                has_none = True
        if has_match and has_none and not has_sleep:
            errors.append(
                f"{fn.name}: 全部未命中时应 await browser.b_sleep(...) 再 return None"
            )
    return errors


def _module_wait_state_keys(tree: ast.AST) -> set[str]:
    """模块 STATES/TASK*_STATES 里名为「等待/转场」的状态键集合。"""
    keys: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not (target.id == "STATES" or target.id.endswith("_STATES")):
            continue
        if not isinstance(node.value, (ast.Dict, ast.Call)):
            continue
        dict_node = node.value if isinstance(node.value, ast.Dict) else None
        if dict_node is None:
            continue
        for k in dict_node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                if "等待" in k.value or "转场" in k.value:
                    keys.add(k.value)
    return keys


def _click_then_wait_errors(tree: ast.AST) -> list[str]:
    """同一 state 函数里，场景向 click_image 需要 wait_image / match_image 确认转场。"""
    errors: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in _SKIP_CLICK_WAIT_FN:
            continue
        lname = fn.name.lower()
        if "unknown" in lname or "未知" in fn.name or "guard" in lname:
            continue
        clicks: list[str] = []
        has_confirm = False
        for method, node in _iter_browser_calls(fn):
            if method in ("wait_image", "match_image", "match_image_multi"):
                has_confirm = True
            elif method == "click_image":
                img = _first_img_arg(node) or ""
                stem = _img_stem(img)
                if stem and not _DISMISS_IMG_RE.search(stem):
                    clicks.append(stem)
        if clicks and not has_confirm:
            # 允许「返回统一转场/等待态」的写法：该状态内部持续识场景即等价于确认
            rets = _function_return_string_literals(fn) if hasattr(_function_return_string_literals, "__call__") else set()
            wait_keys = _module_wait_state_keys(tree)
            wait_back = any(
                ("等待" in r or "转场" in r) and r in wait_keys for r in rets
            )
            if not wait_back:
                preview = ", ".join(clicks[:4])
                errors.append(
                    f"{fn.name}: click_image({preview}) 会换场景时，同函数内必须 "
                    f"wait_image / match_image 确认下一张，或返回 STATES 中专门的「等待/转场」态"
                )
    return errors


def _apply_frame_dirty_from_method(method: Optional[str], dirty: bool) -> tuple[bool, bool]:
    """根据 browser 方法更新 dirty；返回 (new_dirty, stale_match)。

    stale_match=True 表示在 dirty 状态下又做了 match_image / match_image_multi。
    """
    if not method:
        return dirty, False
    if method == "click_image":
        return True, False
    if method == "b_sleep":
        # sleep 不刷新帧；若此前已 click，仍 dirty
        return dirty, False
    if method in ("update_frame", "wait_image"):
        return False, False
    if method in ("match_image", "match_image_multi"):
        if dirty:
            return dirty, True
        return dirty, False
    return dirty, False


def _scan_expr_for_stale_frame(
    expr: ast.AST,
    dirty: bool,
    *,
    fn_name: str,
    errors: list[str],
    seen: set[tuple[str, int]],
) -> bool:
    """扫描表达式中的 browser 调用（含 if/while 条件），返回新 dirty。"""
    # 按子树出现顺序粗略扫描 Call
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        method = None
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "browser":
                method = func.attr
        if method is None:
            continue
        dirty, stale = _apply_frame_dirty_from_method(method, dirty)
        if stale:
            lineno = getattr(node, "lineno", 0)
            key = (fn_name, lineno)
            if key not in seen:
                seen.add(key)
                errors.append(
                    f"{fn_name}@{lineno}: click/b_sleep 后 match_image 仍用旧帧；"
                    "须先 await browser.update_frame()（或改用 wait_image，其内部会刷新）"
                )
    return dirty


def _scan_stmts_for_stale_frame(
    stmts: list[ast.stmt],
    dirty: bool,
    *,
    fn_name: str,
    errors: list[str],
    seen: set[tuple[str, int]],
) -> bool:
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            dirty = _scan_expr_for_stale_frame(
                stmt.test, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            d_then = _scan_stmts_for_stale_frame(
                stmt.body, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            d_else = _scan_stmts_for_stale_frame(
                stmt.orelse, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            dirty = d_then or d_else
            continue
        if isinstance(stmt, ast.While):
            dirty = _scan_expr_for_stale_frame(
                stmt.test, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            d_body = _scan_stmts_for_stale_frame(
                stmt.body, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            # 循环可能再次进入 test：若 body 结束仍 dirty，则 test 里的 match 会用旧帧
            if d_body:
                _scan_expr_for_stale_frame(
                    stmt.test, True, fn_name=fn_name, errors=errors, seen=seen,
                )
            dirty = d_body
            continue
        if isinstance(stmt, ast.For):
            dirty = _scan_expr_for_stale_frame(
                stmt.iter, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            d_body = _scan_stmts_for_stale_frame(
                stmt.body, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            dirty = d_body
            continue
        if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return)):
            target = stmt.value if hasattr(stmt, "value") and stmt.value is not None else stmt
            if isinstance(stmt, ast.Expr):
                target = stmt.value
            dirty = _scan_expr_for_stale_frame(
                target, dirty, fn_name=fn_name, errors=errors, seen=seen,
            )
            continue
        # with / try 等：递归 body
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(stmt, attr, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                dirty = _scan_stmts_for_stale_frame(
                    block, dirty, fn_name=fn_name, errors=errors, seen=seen,
                )
        handlers = getattr(stmt, "handlers", None)
        if handlers:
            for h in handlers:
                dirty = _scan_stmts_for_stale_frame(
                    h.body, dirty, fn_name=fn_name, errors=errors, seen=seen,
                )
    return dirty


def _stale_frame_after_action_errors(tree: ast.AST) -> list[str]:
    """旧：校验 click/b_sleep 后是否 update_frame。

    运行时已改为：b_sleep/click 自动 invalidate，match_image 前 ensure_frame；
    TaskController 还会按需开启观察 Hz。生成侧不再硬拦，避免误报。
    """
    return []


def _scene_id_threshold_errors(tree: ast.AST) -> list[str]:
    """logo / rank / *_logo 作场景 id 时 match_image 必须用 nav_threshold。"""
    errors: list[str] = []
    seen_lines: set[int] = set()
    for method, node in _iter_browser_calls(tree):
        if method not in ("match_image", "match_image_multi"):
            continue
        img = _first_img_arg(node)
        if not img:
            continue
        stem = _img_stem(img)
        if not _SCENE_ID_NAME_RE.match(stem):
            continue
        if not _call_uses_nav_threshold(node):
            lineno = getattr(node, "lineno", 0)
            seen_lines.add(lineno)
            errors.append(
                f"match_image({img!s})@{lineno}: "
                f"场景 id 应使用 threshold=CFG.nav_threshold"
            )
    for fn in _unknown_state_fns(tree):
        for method, node in _iter_browser_calls(fn):
            if method not in ("match_image", "match_image_multi"):
                continue
            if _call_uses_nav_threshold(node):
                continue
            lineno = getattr(node, "lineno", 0)
            if lineno in seen_lines:
                continue
            seen_lines.add(lineno)
            errors.append(
                f"{fn.name}@{lineno}: 场景路由 match_image 应使用 threshold=CFG.nav_threshold"
            )
    return errors


def _match_multi_dict_errors(tree: ast.AST) -> list[str]:
    """match_image_multi 返回 list[dict]，禁止对 multi 结果用 .y / .x 属性。"""
    multi_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        # matches = await browser.match_image_multi(...)
        if isinstance(val, ast.Await):
            val = val.value
        if not isinstance(val, ast.Call):
            continue
        func = val.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "match_image_multi"
            and isinstance(func.value, ast.Name)
            and func.value.id == "browser"
        ):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    multi_names.add(t.id)
    if not multi_names and not any(
        attr == "match_image_multi" for attr, _ in _iter_browser_calls(tree)
    ):
        return []

    bad: list[str] = []
    # max(matches, key=lambda m: m.y)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "max"):
            continue
        if not node.args:
            continue
        arg0 = node.args[0]
        from_multi = isinstance(arg0, ast.Name) and arg0.id in multi_names
        if not from_multi and not multi_names:
            # 无赋值名时：仍检查 max(..., key=lambda m: m.y) 且文件含 multi
            from_multi = any(
                attr == "match_image_multi" for attr, _ in _iter_browser_calls(tree)
            )
        if not from_multi:
            continue
        for kw in node.keywords:
            if kw.arg != "key" or not isinstance(kw.value, ast.Lambda):
                continue
            for sub in ast.walk(kw.value):
                if (
                    isinstance(sub, ast.Attribute)
                    and sub.attr in ("x", "y", "score", "max_val")
                    and isinstance(sub.value, ast.Name)
                ):
                    bad.append(
                        f"第 {getattr(sub, 'lineno', '?')} 行: match_image_multi 返回 dict，"
                        f"请用 {sub.value.id}['{sub.attr}']，不要 {sub.value.id}.{sub.attr}"
                    )
    # best = max(...); best.y
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in ("x", "y", "score", "max_val"):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        # 仅当同名曾被赋值为 max(multi_name, ...)
        name = node.value.id
        for assign in tree.body if False else ast.walk(tree):
            pass
        for assign in ast.walk(tree):
            if not isinstance(assign, ast.Assign):
                continue
            if not (len(assign.targets) == 1 and isinstance(assign.targets[0], ast.Name)):
                continue
            if assign.targets[0].id != name:
                continue
            call = assign.value
            if isinstance(call, ast.Await):
                call = call.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "max"):
                continue
            if call.args and isinstance(call.args[0], ast.Name) and call.args[0].id in multi_names:
                bad.append(
                    f"第 {getattr(node, 'lineno', '?')} 行: match_image_multi 结果是 dict，"
                    f"请用 {name}['{node.attr}']，不要 {name}.{node.attr}"
                )
    seen: set[str] = set()
    out: list[str] = []
    for m in bad:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out[:6]


def _goal_exit_errors(tree: ast.AST, explanation: str) -> list[str]:
    """介绍写了完成/结束条件时，代码应含 __exit__ 或任务完成路径。"""
    if not explanation or not _GOAL_EXIT_RE.search(explanation):
        return []
    text = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    if "__exit__" in text or "任务完成" in text:
        return []
    return ["介绍写了完成/结束条件，但代码中没有 return '__exit__' 或「任务完成」状态"]


def _validate_task_state_keys(tree: ast.AST, plan: Optional[dict]) -> list[str]:
    """按 task.id → TASK_{id}_STATES 硬对齐校验必填键。"""
    if not plan:
        return []
    try:
        from backend.script_generator.graph.plan_schema import (
            normalize_plan,
            task_states_var,
            task_timeout_var,
        )
    except Exception:
        return []
    plan_n = normalize_plan(plan)
    tasks = plan_n.get("tasks") or []
    if not tasks:
        return []

    errors: list[str] = []
    assigned = _collect_assigned_names(tree)
    dicts: dict[str, set[str]] = {}
    for name in assigned:
        if name == "STATES" or name.endswith("_STATES") or name.endswith("_TIMEOUT"):
            d = _find_module_dict_assign(tree, name)
            if d:
                dicts[name] = _dict_literal_keys(d)

    for i, task in enumerate(tasks):
        tname = str(task.get("name") or "")
        tid = str(task.get("id") or "")
        required = [str(s) for s in (task.get("states") or []) if str(s).strip()]
        if not required:
            continue
        st_var = task_states_var(task, i)
        to_var = task_timeout_var(task, i)

        if st_var not in dicts:
            # 宽松：同名大小写或 TASK_*_STATES 含 id
            alt = None
            tid_u = tid.upper()
            for dname in dicts:
                if dname.endswith("_STATES") and tid_u and tid_u in dname.upper():
                    alt = dname
                    break
            if alt is None:
                errors.append(
                    f"任务「{tname}」(id={tid}) 缺少状态表 {st_var}；"
                    f"请使用确切变量名 {st_var}"
                )
                continue
            errors.append(
                f"任务「{tname}」状态表名应为 {st_var}，当前为 {alt}（请改名）"
            )
            st_var = alt

        keys = dicts.get(st_var) or set()
        missing = [r for r in required if r not in keys]
        if missing:
            errors.append(
                f"任务「{tname}」的状态表 {st_var} 缺少键: {', '.join(missing)}；"
                f"必填: {', '.join(required)}"
            )

        if to_var in dicts:
            tkeys = dicts[to_var]
            missing_to = [r for r in required if r not in tkeys]
            if missing_to:
                errors.append(
                    f"任务「{tname}」的 {to_var} 缺少键: {', '.join(missing_to)}"
                )
        elif any(n.endswith("_TIMEOUT") for n in dicts):
            # 有其它 TIMEOUT 但没有本任务的 —— 提示
            errors.append(
                f"任务「{tname}」缺少超时表 {to_var}（或与 {st_var} 键集一致的 TIMEOUT）"
            )
    return errors


def _stub_handler_name(task_id: str, key: str) -> str:
    ascii_key = re.sub(r"[^0-9A-Za-z]+", "_", key).strip("_")
    if not ascii_key or ascii_key == "state":
        ascii_key = f"k{abs(hash(key)) % 100000}"
    tid = re.sub(r"[^0-9A-Za-z_]+", "_", task_id).strip("_") or "task"
    # stub_ 前缀：空壳检测会跳过，避免补键后仍被 trial 校验拦住
    name = f"stub_{tid}_{ascii_key}"
    if name[0].isdigit():
        name = "s_" + name
    return name[:60]


def _find_handler_for_state_key(
    key: str,
    *,
    existing_funcs: set[str],
    global_handlers: dict[str, str],
) -> Optional[str]:
    """为缺失态名找已有 handler（跨表复用 / step_* / stub_*）。"""
    hit = global_handlers.get(key)
    if hit and hit in existing_funcs:
        return hit
    compact = key.replace("_", "")
    for cand in (
        f"step_{key}",
        f"stub_{key}",
        f"step_{compact}",
        f"stub_{compact}",
    ):
        if cand in existing_funcs:
            return cand
    for fn in existing_funcs:
        if not (fn.startswith("step_") or fn.startswith("stub_")):
            continue
        if compact and compact in fn.replace("_", ""):
            return fn
    return None


def patch_missing_handler_return_keys(code: str) -> tuple[str, list[str]]:
    """补全 handler return 的态名：写入对应 TASK*_STATES / TIMEOUT（复用已有 step/stub）。

    解决「生成完成但试运行脚本检查：return '出击_决定' 但状态表无此键」。
    可能连锁缺键，最多迭代数轮。
    """
    if not (code or "").strip():
        return code, []
    all_notes: list[str] = []
    cur = code
    for _ in range(8):
        nxt, notes = _patch_missing_handler_return_keys_once(cur)
        if not notes:
            break
        all_notes.extend(notes)
        cur = nxt
    return cur, all_notes


def _patch_missing_handler_return_keys_once(code: str) -> tuple[str, list[str]]:
    """单轮：扫描 handler return，把缺失态补进状态表。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    fns = {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    existing_funcs = set(fns)
    assigns: dict[str, ast.Assign] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and isinstance(node.value, ast.Dict):
                assigns[t.id] = node

    global_handlers: dict[str, str] = {}
    for _var, mapping in _iter_all_state_maps(tree):
        for k, h in mapping.items():
            global_handlers.setdefault(k, h)

    scene_maps = _collect_scene_to_step_maps(tree)
    notes: list[str] = []
    stubs_to_add: list[str] = []

    for var, mapping in list(_iter_all_state_maps(tree)):
        st_assign = assigns.get(var)
        if st_assign is None or not isinstance(st_assign.value, ast.Dict):
            continue
        dnode = st_assign.value
        task_keys = set(mapping.keys())
        missing: set[str] = set()
        for hname in mapping.values():
            fn = fns.get(hname)
            if fn is None:
                continue
            for ret in _function_return_string_literals(fn):
                if ret in _SKIP_WIRING_RETURNS:
                    continue
                if ret in task_keys:
                    continue
                if _scene_covered_for_table(ret, task_keys, scene_maps):
                    continue
                missing.add(ret)
        if not missing:
            continue

        tid = var
        if tid.startswith("TASK_") and tid.endswith("_STATES"):
            tid = tid[len("TASK_") : -len("_STATES")]
        to_var = var.replace("_STATES", "_TIMEOUT") if var.endswith("_STATES") else ""
        to_assign = assigns.get(to_var) if to_var else None

        for key in sorted(missing):
            if key in task_keys:
                continue
            handler = _find_handler_for_state_key(
                key,
                existing_funcs=existing_funcs,
                global_handlers=global_handlers,
            )
            # 跨任务串表守卫：本表不得复用其它任务的 taskM_* handler
            _own = re.match(r"TASK_?(?:task)?(\d+)_STATES$", var)
            _fh = re.match(r"^task(\d+)_(.+)$", handler or "")
            if _fh and _own and _fh.group(1) != _own.group(1):
                _cand = f"task{_own.group(1)}_{_fh.group(2)}"
                handler = _cand if _cand in existing_funcs else ""
            if not handler:
                handler = _stub_handler_name(tid, key)
                if handler not in existing_funcs:
                    stubs_to_add.append(
                        f"\nasync def {handler}(browser):\n"
                        f"    browser.script_log(\"route stub: {key}\")\n"
                        f"    return \"未知\"\n"
                    )
                    existing_funcs.add(handler)
            dnode.keys.append(ast.Constant(value=key))
            dnode.values.append(ast.Name(id=handler, ctx=ast.Load()))
            task_keys.add(key)
            mapping[key] = handler
            global_handlers.setdefault(key, handler)
            notes.append(f"{var} 补 return 目标 {key!r} -> {handler}")

            if to_assign is not None and isinstance(to_assign.value, ast.Dict):
                td = to_assign.value
                tkeys = _dict_literal_keys(td)
                if key not in tkeys:
                    waitish = any(
                        x in key for x in ("等待", "战斗", "loading", "Loading", "结算")
                    )
                    td.keys.append(ast.Constant(value=key))
                    td.values.append(ast.Constant(value=180 if waitish else 60))
                    notes.append(
                        f"{to_var} 补键 {key!r}={180 if waitish else 60}"
                    )

    if not notes:
        return code, []

    try:
        new_code = ast.unparse(tree)
    except Exception:
        return code, []

    if stubs_to_add:
        marker = "\nasync def do_work"
        stub_block = "".join(stubs_to_add)
        if marker in new_code:
            new_code = new_code.replace(marker, stub_block + marker, 1)
        else:
            new_code = new_code.rstrip() + stub_block

    try:
        ast.parse(new_code)
    except SyntaxError:
        return code, []
    return new_code, notes


def patch_missing_task_state_keys(
    code: str,
    plan: Optional[dict],
) -> tuple[str, list[str]]:
    """本地补全 TASK_{id}_STATES / TIMEOUT 中缺失的计划键（stub handler）。

    返回 (新代码, 补丁说明列表)。无法安全改写时原样返回。
    """
    if not plan or not (code or "").strip():
        return code, []
    try:
        from backend.script_generator.graph.plan_schema import (
            normalize_plan,
            task_states_var,
            task_timeout_var,
        )
    except Exception:
        return code, []

    plan_n = normalize_plan(plan)
    tasks = plan_n.get("tasks") or []
    if not tasks:
        return code, []

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    notes: list[str] = []
    # Collect top-level assign targets → node
    assigns: dict[str, ast.Assign] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and isinstance(node.value, ast.Dict):
                assigns[t.id] = node

    stubs_to_add: list[str] = []
    existing_funcs = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    for i, task in enumerate(tasks):
        required = [str(s) for s in (task.get("states") or []) if str(s).strip()]
        if not required:
            continue
        tid = str(task.get("id") or f"task{i + 1}")
        st_var = task_states_var(task, i)
        to_var = task_timeout_var(task, i)
        st_assign = assigns.get(st_var)
        if st_assign is None:
            continue  # 整表缺失留给 LLM fix；这里只补键
        dnode = st_assign.value
        assert isinstance(dnode, ast.Dict)
        keys = _dict_literal_keys(dnode)
        for key in required:
            if key in keys:
                continue
            handler = _stub_handler_name(tid, key)
            # append to dict AST
            dnode.keys.append(ast.Constant(value=key))
            dnode.values.append(ast.Name(id=handler, ctx=ast.Load()))
            notes.append(f"{st_var} 补键 {key!r} -> {handler}")
            if handler not in existing_funcs:
                stubs_to_add.append(
                    f"\nasync def {handler}(browser):\n"
                    f"    browser.script_log(\"TODO stub state: {key}\")\n"
                    f"    return \"未知\"\n"
                )
                existing_funcs.add(handler)

        to_assign = assigns.get(to_var)
        if to_assign is not None and isinstance(to_assign.value, ast.Dict):
            td = to_assign.value
            tkeys = _dict_literal_keys(td)
            for key in required:
                if key in tkeys:
                    continue
                td.keys.append(ast.Constant(value=key))
                td.values.append(ast.Constant(value=60))
                notes.append(f"{to_var} 补键 {key!r}=60")

    if not notes:
        return code, []

    try:
        new_code = ast.unparse(tree)
    except Exception:
        return code, []

    if stubs_to_add:
        # insert stubs before do_work if present
        marker = "\nasync def do_work"
        stub_block = "".join(stubs_to_add)
        if marker in new_code:
            new_code = new_code.replace(marker, stub_block + marker, 1)
        else:
            new_code = new_code.rstrip() + stub_block

    return new_code, notes


def _validate_reuse_imports(tree: ast.AST, plan: Optional[dict], code: str) -> list[str]:
    errors: list[str] = []
    imported = _collect_imported_names(tree)
    for u in (plan or {}).get("reuse") or []:
        name_field = str(u.get("name") or "")
        m = re.match(r"([A-Za-z_][\w]*)", name_field.strip())
        sym = m.group(1) if m else ""
        if not sym:
            continue
        if sym in imported:
            continue
        errors.append(
            f"计划 reuse 要求使用 {sym}，但代码中未 from/import 该符号"
        )
    return errors


def _collect_assigned_names(tree: ast.AST) -> set[str]:
    assigned: set[str] = set()
    for n in getattr(tree, "body", []):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            assigned.add(n.target.id)
    return assigned


def _dict_str_name_values(node: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(node, ast.Dict):
        return out
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Name):
            out[k.value] = v.id
    return out


def _function_returns_literal(fn: ast.AST, value: str) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        val = node.value
        if isinstance(val, ast.Constant) and val.value == value:
            return True
    return False


def _function_return_string_literals(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        val = node.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            out.add(val.value)
    return out


def _dict_looks_like_scene_id_map(d: ast.Dict) -> bool:
    """场景标识表：key=场景名，value=_img(...) / stem 字符串 / stem 元组。"""
    if not d.keys:
        return False
    for k, v in zip(d.keys, d.values):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value.strip()):
            return False
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            continue
        if isinstance(v, (ast.Tuple, ast.List)):
            if v.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in v.elts
            ):
                continue
            return False
        if isinstance(v, ast.Call):
            fn = v.func
            if isinstance(fn, ast.Name) and fn.id in ("_img", "img"):
                continue
            if isinstance(fn, ast.Attribute) and fn.attr in ("_img", "img"):
                continue
            return False
        # handler Name（状态表）不算场景标识表
        return False
    return True


def _module_scene_id_maps(tree: ast.AST) -> dict[str, set[str]]:
    """模块级 SCENE_IDS = {'选关界面': (...), ...} 一类场景标识表。"""
    out: dict[str, set[str]] = {}
    for n in tree.body:
        name = None
        d = None
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    name = t.id
                    d = n.value
                    break
        elif (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and isinstance(n.value, ast.Dict)
        ):
            name = n.target.id
            d = n.value
        if not name or d is None or not _dict_looks_like_scene_id_map(d):
            continue
        keys = {
            k.value
            for k in d.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if keys:
            out[name] = keys
    return out


def _scene_keys_from_pair_list(node: ast.AST) -> set[str]:
    """scene_signs = [('选关界面', _img(...)), ...] / 内联 list/tuple 的场景名。"""
    keys: set[str] = set()
    if not isinstance(node, (ast.List, ast.Tuple)):
        return keys
    for elt in node.elts:
        if not isinstance(elt, (ast.Tuple, ast.List)) or len(elt.elts) < 2:
            continue
        name_n, path_n = elt.elts[0], elt.elts[1]
        if not (isinstance(name_n, ast.Constant) and isinstance(name_n.value, str)):
            continue
        if isinstance(path_n, ast.Call):
            fn = path_n.func
            ok = (isinstance(fn, ast.Name) and fn.id in ("_img", "img")) or (
                isinstance(fn, ast.Attribute) and fn.attr in ("_img", "img")
            )
            if not ok:
                continue
        elif isinstance(path_n, ast.Constant) and isinstance(path_n.value, str):
            pass
        else:
            continue
        keys.add(name_n.value)
    return keys


def _unknown_state_scene_keys(tree: ast.AST) -> set[str]:
    """unknown_state 并发识别的场景业务名。

    兼容：
    - 函数内联 cs = {"主界面": _img(...), ...}
    - 模块级 SCENE_IDS = {"选关界面": ("1_logo", ...), ...}
    - 列表写法 scene_signs = [('选关界面', _img(...)), ...]
    """
    keys: set[str] = set()
    mod_maps = _module_scene_id_maps(tree)
    # 模块级 list/tuple 场景表
    mod_lists: dict[str, set[str]] = {}
    for n in tree.body:
        name = None
        val = None
        if isinstance(n, ast.Assign) and isinstance(n.value, (ast.List, ast.Tuple)):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    name = t.id
                    val = n.value
                    break
        elif (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and isinstance(n.value, (ast.List, ast.Tuple))
        ):
            name = n.target.id
            val = n.value
        if name and val is not None:
            sk = _scene_keys_from_pair_list(val)
            if sk:
                mod_lists[name] = sk

    for fn in _unknown_state_fns(tree):
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                if not _dict_looks_like_scene_id_map(node):
                    for k in node.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
                else:
                    for k in node.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
            elif isinstance(node, (ast.List, ast.Tuple)):
                keys |= _scene_keys_from_pair_list(node)
            elif isinstance(node, ast.Assign) and isinstance(
                node.value, (ast.List, ast.Tuple)
            ):
                keys |= _scene_keys_from_pair_list(node.value)
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        for name, scene_keys in mod_maps.items():
            if name in used:
                keys |= scene_keys
        for name, scene_keys in mod_lists.items():
            if name in used:
                keys |= scene_keys
    return keys


_SKIP_WIRING_RETURNS = frozenset({"__exit__", "__task_failed__", "__done__"})

# unknown_state 识别的场景 → 仅这些 task id 必须显式接线（hub 态除外）
_SCENE_TASK_IDS: dict[str, tuple[str, ...]] = {
    "房间界面": ("room",),
    "竞技场界面": ("jjc", "arena",),
    "竞技场": ("jjc", "arena",),
    "塔界面": ("tower", "ta",),
    "塔": ("tower", "ta",),
}
_HUB_SCENES = frozenset({"主界面", "出击界面"})


def _hub_entry_self_loop_errors(tree: ast.AST) -> list[str]:
    """入口/枢纽态 handler 禁止 return 自身态名（会空转死循环）。

    典型坏例：TASK_jjc「出击界面」= arena_entry；match 出击_logo 后 return '出击界面'。
    纯导航（返回出击界面 / go_sortie）允许 return '出击界面'。
    """
    fns = _module_functions(tree)
    errors: list[str] = []
    for n in tree.body:
        d = None
        table = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id == "STATES" or t.id.endswith("_STATES"):
                        d = n.value
                        table = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and (
                n.target.id == "STATES" or n.target.id.endswith("_STATES")
            ):
                d = n.value
                table = n.target.id
        if d is None:
            continue
        mapping = _dict_str_name_values(d)
        nav_handlers = {
            mapping.get("返回出击界面"),
            mapping.get("返回主界面"),
        }
        for state_key, hname in mapping.items():
            if state_key not in _HUB_SCENES:
                continue
            if hname in nav_handlers:
                continue
            if re.search(r"go_sortie|go_home|navigate_home|返回", hname or "", re.I):
                continue
            fn = fns.get(hname)
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if state_key not in _function_return_string_literals(fn):
                continue
            errors.append(
                f"{table}: `{hname}` 绑定「{state_key}」却 return '{state_key}'"
                f"（会空转）。已在枢纽应 click 本任务入口并 return 下一业务态"
                f"（如 竞技场界面/塔/房间领体力）；重试用 return None，禁止 return 自身。"
            )
    seen: set[str] = set()
    out: list[str] = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:8]


def _run_task_entry_state_errors(tree: ast.AST) -> list[str]:
    """多任务 run_task：入口须 arena/tower→返回出击界面，禁止一律返回主界面空等超时。"""
    errors: list[str] = []
    has_run_task = any(
        isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "run_task"
        for n in tree.body
    )
    if not has_run_task:
        return errors
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_task_entry_state":
            try:
                src = ast.unparse(n) if hasattr(ast, "unparse") else ""
            except Exception:
                src = ""
            if not src:
                continue
            has_sortie = "返回出击界面" in src
            has_home = "返回主界面" in src
            has_task_guard = bool(
                re.search(r"task_name|arena|tower|room", src)
            )
            if has_home and has_sortie and not has_task_guard:
                i_home = src.find("返回主界面")
                i_sortie = src.find("返回出击界面")
                if i_home >= 0 and i_sortie >= 0 and i_home < i_sortie:
                    errors.append(
                        "_task_entry_state: 多任务时须先判断 arena/tower "
                        "返回「返回出击界面」，禁止一律优先「返回主界面」（会导致空等超时）"
                    )
            if has_sortie and "return \"未知\"" in src.replace("'", '"'):
                if "返回主界面" not in src and "task_name" not in src:
                    errors.append(
                        "_task_entry_state: 仅有「返回出击界面」时房间任务也需 "
                        "return「返回主界面」或按 task_name 分支"
                    )
    for n in tree.body:
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "run_task":
            try:
                body_src = ast.unparse(n) if hasattr(ast, "unparse") else ""
            except Exception:
                body_src = ""
            if re.search(
                r"state_name\s*=\s*['\"]返回主界面['\"]",
                body_src,
            ) and "_task_entry_state" not in body_src:
                errors.append(
                    "run_task: 禁止硬编码初始 state_name='返回主界面'；"
                    "应使用 _task_entry_state(states, task_name) 或 bootstrap 识场景"
                )
    return errors


def _run_task_bootstrap_errors(tree: ast.AST) -> list[str]:
    """run_task 启动应先识场景再进业务步，避免超时后才操作。"""
    errors: list[str] = []
    for n in tree.body:
        if not isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if n.name != "run_task":
            continue
        try:
            src = ast.unparse(n) if hasattr(ast, "unparse") else ""
        except Exception:
            src = ""
        if not src:
            continue
        if "_task_entry_state" in src and not re.search(
            r"bootstrap|unknown_state\s*\(", src
        ):
            errors.append(
                "run_task: 启动时应先 unknown_state/bootstrap 识当前场景再定 state_name，"
                "避免已在出击/塔界面仍先跑「返回主界面」空转到超时"
            )
        break
    return errors


def _run_task_transition_hold_errors(tree: ast.AST) -> list[str]:
    """run_task 步超时：unknown_state 无命中须保持态+重置 se_time，禁止直接 entry 逃逸。"""
    errors: list[str] = []
    for n in tree.body:
        if not isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if n.name != "run_task":
            continue
        try:
            src = ast.unparse(n) if hasattr(ast, "unparse") else ""
        except Exception:
            src = ""
        if not src or "unknown_state" not in src:
            break
        if "se_time" not in src or "timeouts" not in src:
            break
        if "无标识图，视为过场" in src or "无标识且无导航按钮" in src or "scene is None" in src:
            # 已有过场保持即合格。home chrome 仅在代码仍引用时检查；
            # 无 home 素材被本地剥离后不应再强制（推本等目录无 home）。
            has_chrome_ref = bool(
                re.search(r"_img\(\s*['\"](?:home|home_btn|back_home|1_home)", src)
                or "_has_nav_chrome" in src
            )
            if has_chrome_ref:
                # 引用了 chrome 图则须在无标识分支里真正用到（避免死引用）
                if not re.search(
                    r"(home|导航|_has_nav_chrome).{0,80}(过场|非过场|continue)",
                    src,
                    re.S,
                ):
                    errors.append(
                        "run_task: 已引用 home 等导航 chrome 时，"
                        "unknown_state 无命中分支须先检测 chrome 再判定过场"
                    )
            break
        if "_task_entry_state" in src and re.search(
            r"unknown_state[\s\S]{0,800}?_task_entry_state", src
        ):
            errors.append(
                "run_task: 步超时重识屏时 unknown_state 无命中须视为过场"
                "（保持 state_name、se_time=now），禁止直接 _task_entry_state 导航"
            )
        break
    return errors


def _run_task_unknown_trap_errors(tree: ast.AST) -> list[str]:
    """run_task 禁止 bootstrap/resolve 后长期停在 未知 只识屏不导航。"""
    errors: list[str] = []
    for n in tree.body:
        if not isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if n.name != "run_task":
            continue
        try:
            src = ast.unparse(n) if hasattr(ast, "unparse") else ""
        except Exception:
            src = ""
        if not src:
            break
        has_entry_call = bool(re.search(r"_task_entry_state\s*\(\s*states", src))
        has_unknown_guard = bool(
            re.search(r"(?:boot|resolved|state_name)\s*!=\s*['\"]未知['\"]", src)
            or re.search(r"(?:boot|resolved|state_name)\s*==\s*['\"]未知['\"]", src)
        )
        if "_bootstrap_state" in src or re.search(r"\bboot\b", src):
            if not re.search(r"boot\s*!=\s*['\"]未知['\"]", src):
                errors.append(
                    "run_task: bootstrap 为 未知 时须改从 _task_entry_state 起跑，"
                    "禁止只识屏不做事"
                )
        if not (has_entry_call and has_unknown_guard):
            errors.append(
                "run_task: 未知/未映射场景须逃逸到 _task_entry_state 执行导航，"
                "禁止停在 未知+unknown_state"
            )
        break
    return errors


def _iter_all_state_maps(tree: ast.AST):
    """STATES 与 TASK*_STATES 状态表。"""
    for n in tree.body:
        d = None
        var = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id == "STATES" or (
                        t.id.startswith("TASK_") and t.id.endswith("_STATES")
                    ):
                        d = n.value
                        var = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and (
                n.target.id == "STATES"
                or (
                    n.target.id.startswith("TASK_")
                    and n.target.id.endswith("_STATES")
                )
            ):
                d = n.value
                var = n.target.id
        if d is not None:
            yield var, _dict_str_name_values(d)


def _dict_str_to_str_literal(node: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(node, ast.Dict):
        return out
    for k, v in zip(node.keys, node.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out[k.value] = v.value
    return out


def _collect_scene_to_step_maps(tree: ast.AST) -> list[dict[str, str]]:
    maps: list[dict[str, str]] = []
    for n in tree.body:
        d = None
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if "SCENE_TO_STEP" in t.id:
                        d = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and "SCENE_TO_STEP" in n.target.id:
                d = n.value
        if d is not None:
            parsed = _dict_str_to_str_literal(d)
            if parsed:
                maps.append(parsed)
    return maps


_EXPL_TASK_HEAD_RE = re.compile(r"[（(](\d+)[）)]\s*([^\n]+)")


def _count_explanation_tasks(explanation: str) -> int:
    text = explanation or ""
    tm = re.search(
        r"任务流程：\n([\s\S]*?)(?=\n特殊规则：|\n## |\Z)",
        text,
    )
    if not tm:
        return 0
    body = tm.group(1)
    parts = re.split(r"\n(?=[（(]\d+[）)])", body)
    count = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if _EXPL_TASK_HEAD_RE.match(part):
            count += 1
    return count


_SCENE_DRIVEN_RE = re.compile(
    r"场景识别为驱动|以场景识别为驱动|"
    r"识别场景[，,].{0,24}根据场景|"
    r"根据场景完成对应|"
    r"看场景来|"
    r"每个场景都有对应的处理",
)


def is_scene_driven_explanation(explanation: str) -> bool:
    """介绍声明「看场景驱动」而非「多独立目标顺序跑完」。

    孤儿推本类：任务流程 (1)选关 (2)关卡信息… 是场景处理器，
    不是 DO日常那种 房间→竞技→塔 的独立任务链。
    """
    return bool(_SCENE_DRIVEN_RE.search(explanation or ""))


def _explanation_helper_names(explanation: str) -> list[str]:
    text = explanation or ""
    hm = re.search(
        r"辅助步骤[^\n]*\n([\s\S]*?)(?=\n场景标识：|\n图片说明：|\n任务流程：|\Z)",
        text,
    )
    if not hm:
        return []
    helper_re = re.compile(r"[（(][a-z][）)]\s*([^\n]+)")
    return [m.group(1).strip() for m in helper_re.finditer(hm.group(1))]


def _explanation_task_titles(explanation: str) -> list[str]:
    """任务流程 (1)… 标题列表。"""
    text = explanation or ""
    tm = re.search(
        r"任务流程：\n([\s\S]*?)(?=\n特殊规则：|\n## |\Z)",
        text,
    )
    if not tm:
        return []
    titles: list[str] = []
    for m in _EXPL_TASK_HEAD_RE.finditer(tm.group(1)):
        titles.append(m.group(2).strip())
    return titles


def _explanation_flow_constraint_lines(
    explanation: str,
    source_dir: str = "",
) -> list[str]:
    """从介绍提取通用流程约束（非某游戏/某图专用）。"""
    expl = explanation or ""
    lines: list[str] = []
    folder_stems: set[str] = set()
    if (source_dir or "").strip():
        try:
            folder_stems, _by = _folder_img_index(source_dir)
        except Exception:
            folder_stems = set()
    if re.search(r"场景标识", expl):
        lines.append(
            "- unknown_state: scene id → scene name; on miss return None (never 未知)"
        )
        lines.append(
            "- run_task step timeout: unknown_state returns None → treat as transition; "
            "keep state_name, reset se_time (pause step timer); no _task_entry_state escape"
        )
        chrome = [
            s for s in ("home", "home_btn", "back_home", "1_home", "rank")
            if s in folder_stems or any(k.rsplit("/", 1)[-1] == s for k in folder_stems)
        ]
        if chrome:
            sample = ", ".join(f"{c}.png" for c in chrome[:3])
            lines.append(
                f"- Transition guard: if None but nav chrome visible (e.g. {sample}), "
                "NOT transition — same hold state + reset se_time, let handler continue"
            )
        else:
            lines.append(
                "- Transition guard: if unknown_state returns None, treat as transition "
                "(keep state_name, se_time=now). "
                "FORBIDDEN: invent home.png / home_btn / back_home / 1_home "
                "when not in ALLOWED _img list"
            )
        lines.append(
            "- _resolve_state: SCENE_TO_STEP before states keys; "
            "scene keys must not bind __exit__-only handlers"
        )
        lines.append(
            "- Each TASK*_STATES or SCENE_TO_STEP must route every scene id from intro"
        )
    if re.search(r"过场|loading|加载|战斗结束|长时间", expl, re.I):
        lines.append(
            "- After click that triggers loading: dedicated 等待* step + "
            "long timeout loop until next-phase markers (not short sleep + hub id)"
        )
    if re.search(r"弹窗|确认窗|popup", expl, re.I):
        lines.append(
            "- Popups: loop click until gone; if intro says match-before-click, obey it"
        )
    if re.search(r"没有\s*room_ok.*(?:任务结束|本任务完成)|没有room_ok", expl, re.I):
        lines.append(
            "- room_ok: loop click until gone; then return '__exit__' "
            "(do not return earlier claim/check step)"
        )
    if re.search(r"jjc_刷新", expl, re.I) and re.search(r"偏移\s*\(-200", expl):
        lines.append(
            "- jjc_刷新 click_image must use pianyi=(-200, 0) per introduction"
        )
    if re.search(r"jjc_段位", expl, re.I) and re.search(r"x\s*最大|x最大", expl, re.I):
        lines.append(
            "- jjc_段位: match_image_multi then max(matches, key=lambda m: m['x']); "
            "then click(best['x'], best['y']); wait jjc_出击"
        )
    if re.search(
        r"累计\s*\d+\s*次.*?(?:本任务|任务)(?:结束|完成)|"
        r"\d+\s*次.*?(?:失败|之后).*?(?:本任务|结束)",
        expl,
    ):
        lines.append(
            "- Retry limits in intro: count misses; at limit return '__exit__', "
            "else return earlier step from intro"
        )
    if re.search(r"回到第\s*\d+\s*步|返回第\s*\d+\s*步|返回上一步|回到.*?步", expl):
        lines.append(
            "- Unmet branch condition: return earlier STATES key from intro "
            "(not __exit__ unless limit reached)"
        )
    return lines


def format_explanation_structure_checklist(
    explanation: str,
    source_dir: str = "",
) -> str:
    """从介绍提取硬结构清单（无 plan 时供 generate/fix prompt 使用）。"""
    expl = explanation or ""
    lines = ["## REQUIRED CODE STRUCTURE (from introduction — mandatory)"]
    n = _count_explanation_tasks(expl)
    titles = _explanation_task_titles(expl)
    scene_driven = is_scene_driven_explanation(expl)
    try:
        from backend.script_generator.architecture import infer_architecture
        _arch, _ = infer_architecture(expl)
        if _arch == "scene_driven":
            scene_driven = True
    except Exception:
        pass
    if n >= 2:
        if scene_driven:
            lines.append(
                f"- Architecture LOCKED: scene_driven（{n} 个场景块："
                f"{', '.join(titles[:6])}）"
            )
            lines.append(
                "- MUST: one STATES + STATE_TIMEOUT + while 循环；"
                "unknown_state 识屏后分发到场景 handler"
            )
            lines.append(
                "- FORBIDDEN: sequential TASK_task*_STATES / for run_task "
                "队列把场景块当独立业务任务"
            )
            lines.append(
                "- 场景名作 STATES 键即可；勿发明「选关界面2」类编号变体"
            )
        else:
            task_vars = ", ".join(f"TASK{i}_STATES" for i in range(1, n + 1))
            timeout_vars = ", ".join(f"TASK{i}_TIMEOUT" for i in range(1, n + 1))
            lines.append(
                f"- 任务流程含 {n} 项（{', '.join(titles[:6])}）—"
                "结构自选：single_fsm 或 multi_task，择更贴合介绍者"
            )
            lines.append(
                "- If multi_task: define run_task + "
                f"{task_vars} + {timeout_vars}"
            )
            lines.append(
                "- If single_fsm: one STATES + STATE_TIMEOUT；"
                "场景名可作状态键，do_work 循环识屏分发"
            )
        lines.append("- STATES handlers: async def only (禁止 lambda)；场景桩 stub_* 可只 return 下一业务步")
        lines.append(
            "- On timeout / 未知: call unknown_state then resolve；"
            "FORBIDDEN stuck only calling unknown_state with no progress"
        )
    helpers = _explanation_helper_names(expl)
    if helpers:
        lines.append("- Helper steps → state keys (or same-name async handler):")
        for h in helpers:
            lines.append(f"  - 「{h}」")
    for item in _explanation_flow_constraint_lines(expl, source_dir=source_dir):
        lines.append(item)
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def build_pseudo_plan_from_explanation(explanation: str) -> dict:
    """无 LLM plan 时从介绍推导伪计划（供 patch / fix checklist）。

    scene_driven：必须 single_fsm + tasks=[]，禁止把场景块拆成顺序 TASK_*。
    """
    expl = explanation or ""
    try:
        from backend.script_generator.architecture import (
            ARCH_SCENE,
            apply_architecture_to_plan,
            infer_architecture,
        )
        arch, reason = infer_architecture(expl)
    except Exception:
        arch, reason = (
            ("scene_driven", "scene-driven heuristic")
            if is_scene_driven_explanation(expl)
            else ("", "")
        )
        apply_architecture_to_plan = None  # type: ignore

    if arch == "scene_driven" or is_scene_driven_explanation(expl):
        plan = {
            "kind": "single_fsm",
            "tasks": [],
            "architecture": "scene_driven",
            "architecture_reason": reason or "scene_driven",
            "notes": "pseudo: scene_driven — one loop + unknown_state; no TASK_* queue",
        }
        if apply_architecture_to_plan is not None:
            return apply_architecture_to_plan(plan, expl)
        return plan

    n = _count_explanation_tasks(expl)
    if n < 2:
        plan: dict = {}
        if apply_architecture_to_plan is not None:
            return apply_architecture_to_plan(plan, expl)
        return plan
    titles = _explanation_task_titles(expl)
    helpers = _explanation_helper_names(expl)
    hub = ["主界面", "出击界面"]
    tasks: list[dict] = []
    for i in range(n):
        title = titles[i] if i < len(titles) else f"任务{i + 1}"
        states = ["未知"] + helpers + hub + [title]
        seen: set[str] = set()
        ordered: list[str] = []
        for s in states:
            if s not in seen:
                seen.add(s)
                ordered.append(s)
        tasks.append({
            "id": f"task{i + 1}",
            "name": title,
            "states": ordered,
        })
    plan = {
        "kind": "multi_task",
        "tasks": tasks,
        "notes": "pseudo from explanation",
    }
    if apply_architecture_to_plan is not None:
        return apply_architecture_to_plan(plan, expl)
    return plan


_RUN_TASK_MINIMAL_SRC = '''
def _task_entry_state(states: dict, task_name: str = "") -> str:
    name = (task_name or "").lower()
    if any(x in name for x in ("竞技", "jjc", "arena", "塔", "ta", "tower")) and "返回出击界面" in states:
        return "返回出击界面"
    if "返回主界面" in states:
        return "返回主界面"
    if "返回出击界面" in states:
        return "返回出击界面"
    return "未知"

async def run_task(browser, task_name: str, states: dict, timeouts: dict) -> str:
    state_name = _task_entry_state(states, task_name)
    se_time = asyncio.get_event_loop().time()
    start = se_time
    while True:
        await browser.update_frame()
        if await check_guards(browser):
            continue
        now = asyncio.get_event_loop().time()
        if now - start > getattr(CFG, "total_timeout", 1800.0):
            return "__task_failed__"
        if now - se_time > timeouts.get(state_name, 180):
            scene = await unknown_state(browser)
            if scene and scene in states:
                state_name = scene
            else:
                state_name = "未知"
            se_time = now
            continue
        handler = states.get(state_name)
        if handler is None:
            return "__task_failed__"
        nxt = await handler(browser)
        if nxt == "__exit__":
            return "__done__"
        if nxt and nxt in states:
            state_name = nxt
            se_time = now
        await browser.b_sleep(0.05, 0.15)
'''


def patch_ensure_multitask_skeleton(
    code: str,
    explanation: str,
) -> tuple[str, list[str]]:
    """介绍含多任务但代码无 TASK*_STATES 时，注入最小多任务骨架。"""
    if is_scene_driven_explanation(explanation):
        return code, []
    try:
        from backend.script_generator.architecture import ARCH_SCENE, infer_architecture
        if infer_architecture(explanation or "")[0] == ARCH_SCENE:
            return code, []
    except Exception:
        pass
    n = _count_explanation_tasks(explanation)
    if n < 2 or not (code or "").strip():
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    assigned = _collect_assigned_names(tree)
    task_count = sum(
        1 for a in assigned if a.startswith("TASK") and a.endswith("_STATES")
    )
    if task_count >= 2:
        return code, []
    top_fns = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    notes: list[str] = []
    additions: list[str] = []
    if "_stub_mt_step" not in assigned:
        additions.append(
            "async def _stub_mt_step(browser):\n"
            "    browser.script_log('[stub] multitask step TODO')\n"
            "    await browser.b_sleep(0.4, 0.8)\n"
            "    return None"
        )
    helpers = _explanation_helper_names(explanation)
    for h in helpers:
        if h not in top_fns:
            additions.append(
                f"async def {h}(browser):\n"
                f"    browser.script_log('[helper] {h}')\n"
                f"    await browser.b_sleep(0.4, 0.8)\n"
                f"    return None"
            )
            notes.append(f"补 helper {h}")
    if "unknown_state" not in top_fns:
        additions.append(
            "async def unknown_state(browser):\n"
            "    await browser.b_sleep(1.5, 2.0)\n"
            "    return None"
        )
        notes.append("补 unknown_state 桩")
    titles = _explanation_task_titles(explanation)
    pseudo = build_pseudo_plan_from_explanation(explanation)
    tasks_plan = pseudo.get("tasks") or []
    try:
        from backend.script_generator.graph.plan_schema import (
            task_states_var,
            task_timeout_var,
        )
    except Exception:
        task_states_var = None  # type: ignore
        task_timeout_var = None  # type: ignore
    for i in range(1, n + 1):
        title = titles[i - 1] if i - 1 < len(titles) else f"任务{i}"
        task = tasks_plan[i - 1] if i - 1 < len(tasks_plan) else {"id": f"task{i}"}
        if task_states_var is not None:
            st_var = task_states_var(task, i - 1)
            to_var = task_timeout_var(task, i - 1)
        else:
            st_var = f"TASK{i}_STATES"
            to_var = f"TASK{i}_TIMEOUT"
        keys: list[str] = ["未知", "主界面", "出击界面"] + list(helpers) + [title]
        seen: set[str] = set()
        ordered: list[str] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
        kv: list[str] = []
        to_kv: list[str] = []
        for k in ordered:
            if k == "未知":
                h = "unknown_state"
            elif k in helpers:
                h = k
            else:
                h = "_stub_mt_step"
            kv.append(f'"{k}": {h}')
            to_kv.append(f'"{k}": 120')
        additions.append(f"{st_var} = {{{', '.join(kv)}}}")
        additions.append(f"{to_var} = {{{', '.join(to_kv)}}}")
        notes.append(f"补 {st_var}")
    if "run_task" not in top_fns and "_task_entry_state" not in top_fns:
        additions.append(_RUN_TASK_MINIMAL_SRC.strip())
        notes.append("补 run_task")
    if not additions:
        return code, notes
    block = "\n# --- multitask skeleton patch ---\n" + "\n".join(additions) + "\n"
    m = re.search(r"(?m)^(async\s+)?def\s+do_work\s*\(", code)
    if m:
        new_code = code[:m.start()] + block + code[m.start():]
    else:
        new_code = code.rstrip() + "\n" + block
    return new_code, notes


def patch_do_work_dual_target(code: str) -> tuple[str, list[str]]:
    """生成脚本 do_work 默认兼容浏览器与窗口（UserBrowser | UserWindow）。"""
    if not (code or "").strip():
        return code, []
    notes: list[str] = []
    new_code = code

    # 已是联合类型则跳过改标注
    already_union = bool(
        re.search(
            r"async\s+def\s+do_work\s*\(\s*\w+\s*:\s*"
            r"(?:UserBrowser\s*\|\s*UserWindow|UserWindow\s*\|\s*UserBrowser)\s*\)",
            new_code,
        )
    )
    if not already_union:
        newer, n = re.subn(
            r"(async\s+def\s+do_work\s*\(\s*\w+\s*:\s*)UserBrowser(\s*\))",
            r"\1UserBrowser | UserWindow\2",
            new_code,
            count=1,
        )
        if n:
            new_code = newer
            notes.append("do_work 标注改为 UserBrowser | UserWindow")
        else:
            newer, n = re.subn(
                r"(async\s+def\s+do_work\s*\(\s*\w+\s*:\s*)UserWindow(\s*\))",
                r"\1UserBrowser | UserWindow\2",
                new_code,
                count=1,
            )
            if n:
                new_code = newer
                notes.append("do_work 标注改为 UserBrowser | UserWindow")

    if "UserWindow" in new_code:
        has_uw_import = bool(
            re.search(
                r"from\s+backend\.automation\.user_window\s+import\s+[^\n]*UserWindow",
                new_code,
            )
        )
        if not has_uw_import:
            if "from backend.browser.user_browser import UserBrowser" in new_code:
                new_code = new_code.replace(
                    "from backend.browser.user_browser import UserBrowser",
                    "from backend.browser.user_browser import UserBrowser\n"
                    "from backend.automation.user_window import UserWindow",
                    1,
                )
                notes.append("补 UserWindow import")
            else:
                marker = "from core.path import IMG_PATH"
                if marker in new_code:
                    new_code = new_code.replace(
                        marker,
                        "from backend.browser.user_browser import UserBrowser\n"
                        "from backend.automation.user_window import UserWindow\n"
                        + marker,
                        1,
                    )
                    notes.append("补 UserBrowser/UserWindow import")

    if not notes:
        return code, []
    try:
        ast.parse(new_code)
    except SyntaxError:
        return code, []
    return new_code, notes


def patch_do_work_multitask_loop(
    code: str,
    explanation: str,
) -> tuple[str, list[str]]:
    """do_work 未调 run_task 时，改为多任务 dispatch 循环。"""
    if is_scene_driven_explanation(explanation):
        return code, []
    try:
        from backend.script_generator.architecture import ARCH_SCENE, infer_architecture
        if infer_architecture(explanation or "")[0] == ARCH_SCENE:
            return code, []
    except Exception:
        pass
    n = _count_explanation_tasks(explanation)
    if n < 2 or "run_task" not in code:
        return code, []
    m = re.search(
        r"(?ms)^(async\s+)?def\s+do_work\s*\([^)]*\):\s*\n(.*?)(?=^(async\s+)?def |\Z)",
        code,
    )
    if not m:
        return code, []
    body = m.group(2)
    if "run_task" in body:
        return code, []
    titles = _explanation_task_titles(explanation)
    pseudo = build_pseudo_plan_from_explanation(explanation)
    tasks_plan = pseudo.get("tasks") or []
    try:
        from backend.script_generator.graph.plan_schema import (
            task_states_var,
            task_timeout_var,
        )
    except Exception:
        return code, []
    task_lines = []
    for i in range(1, n + 1):
        title = titles[i - 1] if i - 1 < len(titles) else f"task{i}"
        task = tasks_plan[i - 1] if i - 1 < len(tasks_plan) else {"id": f"task{i}"}
        st_var = task_states_var(task, i - 1)
        to_var = task_timeout_var(task, i - 1)
        task_lines.append(f'        ("{title}", {st_var}, {to_var}),')
    new_body = (
        "    if getattr(CFG, 'use_polling_cache', False):\n"
        "        browser.use_polling_temp_cache = True\n"
        "    tasks = [\n"
        + "\n".join(task_lines)
        + "\n    ]\n"
        "    for name, st, to in tasks:\n"
        "        for attempt in range(3):\n"
        "            result = await run_task(browser, name, st, to)\n"
        "            if result in ('__task_failed__', '__failed__'):\n"
        "                browser.script_log(f'[{name}] failed ({attempt + 1}/3)')\n"
        "                await browser.b_sleep(1.0, 2.0)\n"
        "                continue\n"
        "            browser.script_log(f'[{name}] done')\n"
        "            break\n"
    )
    new_code = code[:m.start(2)] + new_body + code[m.end(2):]
    return new_code, ["do_work 改为 run_task 多任务循环"]


def _scene_covered_for_table(
    scene: str,
    task_keys: set[str],
    scene_maps: list[dict[str, str]],
) -> bool:
    if scene in task_keys:
        return True
    for sm in scene_maps:
        step = sm.get(scene)
        if step and step in task_keys:
            return True
    # 「选关界面2」等编号变体：若基名已在表内则视为已覆盖
    m = re.match(r"^(.+?)(\d+)$", scene or "")
    if m:
        base = m.group(1)
        if base in task_keys:
            return True
        for sm in scene_maps:
            step = sm.get(base)
            if step and step in task_keys:
                return True
    return False


def _function_has_browser_action(fn: ast.AST) -> bool:
    action_methods = frozenset({
        "click_image",
        "wait_image",
        "match_image",
        "match_image_multi",
        "click_game",
    })
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "browser" and func.attr in action_methods:
                return True
    return False


_STUB_HANDLER_SKIP_RE = re.compile(
    r"unknown_state|check_guards|go_home|go_sortie|navigate|guard|stub_",
    re.I,
)


def _function_is_empty_stub(fn: ast.AST) -> bool:
    """业务 handler 仅有 log / return None，无识图点击等待。"""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if _STUB_HANDLER_SKIP_RE.search(fn.name):
        return False
    if _function_has_browser_action(fn):
        return False
    # 本地补键生成的桩（旧名 *_state / 新 stub_*）不拦试运行
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "TODO stub state" in node.value or "route stub:" in node.value:
                return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        val = node.value
        if isinstance(val, ast.Constant):
            if val.value in _SKIP_WIRING_RETURNS:
                return False
            if isinstance(val.value, str):
                # 仅 return 下一态名、无任何 browser 动作 → 空桩
                return True
        else:
            return False
    return True


def _explanation_multi_task_structure_errors(
    tree: ast.AST,
    explanation: str,
    *,
    check_business_imgs: bool = True,
) -> list[str]:
    """介绍含多任务时，代码须有 run_task + 多 TASK 表 + 关键业务图引用。"""
    errors: list[str] = []
    n_tasks = _count_explanation_tasks(explanation)
    if n_tasks < 2:
        return errors
    assigned = _collect_assigned_names(tree)
    top_names = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    if "run_task" not in top_names:
        errors.append(
            f"介绍任务流程含 {n_tasks} 个子任务，须定义 async def run_task(...)"
        )
    task_vars = sorted(
        n for n in assigned if n.startswith("TASK") and n.endswith("_STATES")
    )
    if len(task_vars) < 2:
        errors.append(
            f"介绍含 {n_tasks} 个子任务，须至少 2 个 TASK*_STATES"
            f"（当前: {len(task_vars)}）"
        )
    dw = _find_do_work(tree)
    if dw is not None:
        try:
            dw_src = ast.unparse(dw) if hasattr(ast, "unparse") else ""
        except Exception:
            dw_src = ""
        if dw_src and "run_task" not in dw_src:
            errors.append("do_work 须循环调用 run_task 执行各子任务")
    code_text = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    if not check_business_imgs:
        return errors
    markers = [
        ("room", "room"),
        ("jjc", "jjc"),
        ("ta", "ta"),
    ]
    tm = re.search(
        r"任务流程：\n([\s\S]*?)(?=\n特殊规则：|\n## |\Z)",
        explanation or "",
    )
    task_body = tm.group(1) if tm else ""
    for label, stem in markers:
        if stem in task_body.lower() and f"{stem}." not in code_text.lower():
            if f"'{stem}" not in code_text and f'"{stem}' not in code_text:
                errors.append(
                    f"介绍含「{label}」任务，代码中未见 _img('{stem}...') 等业务图引用"
                )
    return errors


def _explanation_helper_presence_errors(
    tree: ast.AST,
    explanation: str,
) -> list[str]:
    """介绍 @辅助步骤 须在状态表或顶层函数中可找到。"""
    helpers = _explanation_helper_names(explanation)
    if not helpers:
        return []
    all_keys: set[str] = set()
    for _, mapping in _iter_all_state_maps(tree):
        all_keys.update(mapping.keys())
    top_fns = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    errors: list[str] = []
    for h in helpers:
        if h in all_keys or h in top_fns:
            continue
        errors.append(
            f"介绍辅助步骤「{h}」须在 STATES/TASK*_STATES 注册为状态键"
            f"或实现同名 handler"
        )
    return errors


def _explanation_scene_layer_errors(tree: ast.AST, explanation: str) -> list[str]:
    errors: list[str] = []
    if not re.search(r"场景标识", explanation or ""):
        return errors
    scene_keys = _unknown_state_scene_keys(tree)
    if not scene_keys:
        errors.append(
            "介绍含「场景标识」，但 unknown_state 未定义场景识别 dict"
            "（rank/出击_logo/room_logo 等 → 场景名）"
        )
    return errors


def _stub_handler_errors(tree: ast.AST) -> list[str]:
    """状态表绑定的 handler 不得是空壳（只 log + return None）。"""
    fns = _module_functions(tree)
    errors: list[str] = []
    unknown_handlers = set()
    for _, mapping in _iter_all_state_maps(tree):
        uh = mapping.get("未知") or mapping.get("\u672a\u77e5")
        if uh:
            unknown_handlers.add(uh)
    for var, mapping in _iter_all_state_maps(tree):
        for state_key, hname in mapping.items():
            if state_key in ("未知", "\u672a\u77e5"):
                continue
            if hname in unknown_handlers:
                continue
            fn = fns.get(hname)
            if fn is None:
                continue
            # 场景桩（stub_*）按范式允许只 return 下一业务步；
            # codegen:helper-stub 是介绍辅助步骤占位，同样放行（避免与自动补桩互相打脸）
            if hname.startswith("stub_"):
                continue
            if "codegen:helper-stub" in (ast.get_docstring(fn) or ""):
                continue
            if _function_is_empty_stub(fn):
                errors.append(
                    f"{var}「{state_key}」→ `{hname}` 是空壳 handler"
                    f"（须 click/wait/match 或 return 下一业务态）"
                )
    return errors


def _state_dict_lambda_errors(tree: ast.AST) -> list[str]:
    """TASK*_STATES 禁止 lambda handler（run_task 须 await async def）。"""
    errors: list[str] = []
    for n in tree.body:
        d = None
        table = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id == "STATES" or t.id.endswith("_STATES"):
                        d = n.value
                        table = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and (
                n.target.id == "STATES" or n.target.id.endswith("_STATES")
            ):
                d = n.value
                table = n.target.id
        if d is None:
            continue
        for k, v in zip(d.keys, d.values):
            if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                continue
            if isinstance(v, ast.Lambda):
                errors.append(
                    f"{table}「{k.value}」使用 lambda 作 handler，"
                    "须 async def 场景桩（如 stub_主界面_to_room）"
                )
    return errors


def _do_work_blind_scene_routing_errors(tree: ast.AST) -> list[str]:
    """识到 scene 后硬编码 state_name，未走 scene/SCENE_TO_STEP/_resolve_state。"""
    errors: list[str] = []
    for n in tree.body:
        if not isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if n.name not in ("do_work", "run_task"):
            continue
        try:
            src = ast.unparse(n) if hasattr(ast, "unparse") else ""
        except Exception:
            src = ""
        if not src or "unknown_state" not in src:
            continue
        if "_resolve_state" in src or "SCENE_TO_STEP" in src:
            continue
        if re.search(r"state_name\s*=\s*scene\b", src):
            continue
        if re.search(
            r"(?:elif|if)\s+[^\n]*scene[^\n]*:\s*\n\s*state_name\s*="
            r"\s*['\"][^'\"]+['\"]",
            src,
        ):
            errors.append(
                f"{n.name}: 识别 scene 后硬编码 state_name，"
                "未用 scene 变量 / SCENE_TO_STEP / _resolve_state 路由"
            )
    return errors


def _task_var_matches_scene(var_name: str, scene: str) -> bool:
    """该场景是否属于此任务（hub 态对所有 task 都要求）。"""
    if var_name == "STATES":
        return True
    if scene in _HUB_SCENES:
        return True
    aff = _SCENE_TASK_IDS.get(scene)
    if not aff:
        return False
    tid = _task_id_from_var(var_name)
    for a in aff:
        if tid == a:
            return True
        if re.search(rf"(^|_){re.escape(a)}(_|$)", tid):
            return True
    return False


def _task_scene_state_wiring_errors(tree: ast.AST) -> list[str]:
    """handler 返回值须在表内；unknown 场景须在表内或 SCENE_TO_STEP 可解析。"""
    scene_keys = _unknown_state_scene_keys(tree)
    if not scene_keys:
        return []
    scene_maps = _collect_scene_to_step_maps(tree)
    fns = _module_functions(tree)
    errors: list[str] = []
    for var, mapping in _iter_all_state_maps(tree):
        task_keys = set(mapping.keys())
        for sk in sorted(scene_keys):
            if not _task_var_matches_scene(var, sk):
                continue
            if _scene_covered_for_table(sk, task_keys, scene_maps):
                continue
            hint = ""
            if sk == "房间界面" and "房间领体力" in task_keys:
                hint = "（可 alias 到「房间领体力」）"
            elif sk == "主界面" and "返回主界面" in task_keys and "主界面" not in task_keys:
                hint = "（go_home 成功 return '主界面' 时需另有「主界面」业务态）"
            elif sk == "出击界面" and "返回出击界面" in task_keys:
                hint = "（可 alias 到「返回出击界面」handler 或单独业务态）"
            errors.append(
                f"{var} 缺少场景态「{sk}」的 handler{hint}；"
                "unknown_state 识别到该场景时无法路由（须 STATES 键或 SCENE_TO_STEP）"
            )
        for hname in mapping.values():
            fn = fns.get(hname)
            if fn is None:
                continue
            for ret in _function_return_string_literals(fn):
                if ret in _SKIP_WIRING_RETURNS:
                    continue
                if ret not in task_keys:
                    if _scene_covered_for_table(ret, task_keys, scene_maps):
                        continue
                    errors.append(
                        f"{var}: handler `{hname}` return '{ret}' 但状态表无此键"
                        "（且 SCENE_TO_STEP 未映射）"
                    )
    return errors


def _home_nav_exit_errors(tree: ast.AST) -> list[str]:
    """TASK 同时有「返回主界面」和「主界面」时，导航成功不得 __exit__。"""
    fns = {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    errors: list[str] = []
    for n in tree.body:
        d = None
        name = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id == "STATES" or t.id.endswith("_STATES"):
                        d = n.value
                        name = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and (
                n.target.id == "STATES" or n.target.id.endswith("_STATES")
            ):
                d = n.value
                name = n.target.id
        if d is None:
            continue
        mapping = _dict_str_name_values(d)
        if "返回主界面" not in mapping or "主界面" not in mapping:
            continue
        hname = mapping["返回主界面"]
        fn = fns.get(hname)
        if fn is not None and _function_returns_literal(fn, "__exit__"):
            errors.append(
                f"{name}: 「返回主界面」handler `{hname}` 在看到 rank 时不得 "
                "return '__exit__'（本步骤结束 ≠ 本任务完成，会跳过后续任务）；"
                "应 return '主界面'"
            )
    return errors


_SHARED_HUB_STATES = ("出击界面",)  # 导航态可共用；入口点击不可共用
_TASK_IMG_PREFIX: dict[str, tuple[str, ...]] = {
    "room": ("room",),
    "jjc": ("jjc",),
    "tower": ("ta",),
    "ta": ("ta",),
}


def _module_functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _iter_task_state_maps(tree: ast.AST):
    for n in tree.body:
        d = None
        var = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id.startswith("TASK_") and t.id.endswith("_STATES"):
                        d = n.value
                        var = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and n.target.id.startswith("TASK_") and n.target.id.endswith("_STATES"):
                d = n.value
                var = n.target.id
        if d is not None:
            yield var, _dict_str_name_values(d)


def _function_img_stems(fn: ast.AST) -> set[str]:
    stems: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_img":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if isinstance(node.args[0].value, str):
            stems.add(_img_stem(node.args[0].value))
    return stems


def _task_id_from_var(var_name: str) -> str:
    return var_name.replace("TASK_", "").replace("_STATES", "").lower()


def _expected_prefixes(task_id: str) -> tuple[str, ...]:
    for key, prefixes in _TASK_IMG_PREFIX.items():
        if key in task_id:
            return prefixes
    return ()


def _shared_task_hub_handler_errors(tree: ast.AST) -> list[str]:
    """多任务禁止共用会点它任务入口图的 hub handler；纯导航共用允许。"""
    fns = _module_functions(tree)
    usage: dict[tuple[str, str], list[str]] = {}
    for var, mapping in _iter_task_state_maps(tree):
        task = _task_id_from_var(var)
        for hub in _SHARED_HUB_STATES:
            handler = mapping.get(hub)
            if not handler:
                continue
            usage.setdefault((hub, handler), []).append(task)
    errors: list[str] = []
    for (hub, handler), tasks in usage.items():
        uniq = sorted(set(tasks))
        if len(uniq) <= 1:
            continue
        if _is_shared_hub_nav_handler(handler, fns):
            continue
        errors.append(
            f"多任务共用 {hub} 的处理函数 `{handler}`（{', '.join(uniq)}）；"
            "每个任务须单独写入口 handler，禁止塔/竞技场/房间串按钮"
        )
    return errors


def _task_entry_img_prefix_errors(
    tree: ast.AST,
    plan: Optional[dict] = None,
) -> list[str]:
    """任务 STATES 内 handler 不得点其它任务专属图。

    优先用 plan.tasks[].images / shared_images；无 plan 时回退前缀启发式。
    """
    fns = _module_functions(tree)
    own_by_task, foreign_by_task = _plan_task_image_sets(plan)
    errors: list[str] = []
    for var, mapping in _iter_task_state_maps(tree):
        task = _task_id_from_var(var)
        prefixes = _expected_prefixes(task)
        plan_key = ""
        own: set[str] | None = None
        if own_by_task:
            for cand in (task, task.replace("task_", ""), re.sub(r"\s+", "", task.lower())):
                if cand in own_by_task:
                    plan_key = cand
                    own = own_by_task[cand]
                    break
            if own is None:
                for k, v in own_by_task.items():
                    if k in task or task in k:
                        plan_key = k
                        own = v
                        break
        foreign_pool = foreign_by_task.get(plan_key) if plan_key else None
        check_states = list(mapping.keys())
        for hub in check_states:
            handler = mapping.get(hub)
            if not handler or handler not in fns:
                continue
            stems = _function_img_stems(fns[handler])
            if not stems:
                continue
            foreign = _foreign_task_stems(
                task,
                stems,
                plan=plan,
                own_stems=own,
                foreign_pool=foreign_pool if own is not None else None,
            )
            if not foreign:
                # 无 plan 时：仍要求本任务前缀命中（若有前缀表）
                if own is None and prefixes:
                    if any(any(s.startswith(p) for s in stems) for p in prefixes):
                        continue
                    wrong = _foreign_task_stems(task, stems)
                    if wrong:
                        errors.append(
                            f"{var}: {hub} 的 `{handler}` 点击了其它任务的图 "
                            f"({', '.join(wrong[:4])})；{task} 任务应点 "
                            f"{'/'.join(prefixes)}* 系列按钮"
                        )
                continue
            if hub in ("出击界面", "主界面", "未知") or own is None:
                errors.append(
                    f"{var}: {hub} 的 `{handler}` 混入其它任务图 "
                    f"({', '.join(foreign[:4])})；本任务应用本任务图集"
                    + (f"（plan: {', '.join(sorted(own)[:6])}）" if own else "")
                )
    seen: set[str] = set()
    out: list[str] = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:8]


def _img_stem_from_name(name: str) -> str:
    return _norm_img_key(name)


def _plan_task_image_sets(
    plan: Optional[dict],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """返回 (task_id → 本任务+共享 stems, task_id → 其它任务专属 stems)。"""
    if not plan or not isinstance(plan, dict):
        return {}, {}
    tasks = plan.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return {}, {}
    shared = {
        _img_stem_from_name(x)
        for x in (plan.get("shared_images") or [])
        if str(x).strip()
    }
    per: dict[str, set[str]] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("id") or "").strip().lower()
        if not name:
            continue
        # 规范化：去掉空白
        key = re.sub(r"\s+", "", name)
        imgs = {
            _img_stem_from_name(x)
            for x in (t.get("images") or [])
            if str(x).strip()
        }
        per[key] = imgs | shared
    if len(per) < 2:
        # 单任务无「其它任务」可比
        return per, {k: set() for k in per}

    foreign: dict[str, set[str]] = {}
    for key, own in per.items():
        others: set[str] = set()
        for k2, imgs in per.items():
            if k2 == key:
                continue
            # 其它任务专属 = 在对方图集且不在共享、不在本任务
            others |= (imgs - shared - own)
        foreign[key] = others
    return per, foreign


def _foreign_task_stems(
    task_id: str,
    stems: set[str] | list[str],
    *,
    plan: Optional[dict] = None,
    own_stems: Optional[set[str]] = None,
    foreign_pool: Optional[set[str]] = None,
) -> list[str]:
    """返回明显属于其它任务的图 stem。

    有 plan 图集时：命中其它任务专属图即越界。
    否则回退 jjc/room/ta 前缀启发式。
    """
    _ = plan  # 保留签名兼容
    nav_ok = ("home", "back", "close", "ok", "guard", "err", "rank", "出击")
    stem_list = list(stems)

    if foreign_pool is not None and own_stems is not None:
        out = []
        for s in stem_list:
            if any(s.startswith(p) or p in s for p in nav_ok):
                continue
            if s in own_stems or any(
                s.startswith(o) or o.startswith(s) for o in own_stems if len(o) >= 3
            ):
                continue
            if s in foreign_pool or any(
                s.startswith(f) or f.startswith(s) for f in foreign_pool if len(f) >= 3
            ):
                out.append(s)
        return sorted(set(out))

    tid = (task_id or "").lower()
    foreign_rules = [
        ("jjc", ("room", "ta_", "tower", "meiri", "gift")),
        ("tower", ("jjc", "room", "meiri", "gift")),
        ("ta", ("jjc", "room", "meiri", "gift")),
        ("room", ("jjc", "ta_", "tower", "meiri")),
        ("meiri", ("jjc", "ta_", "room", "tower")),
    ]
    bad_prefixes: tuple[str, ...] = ()
    for key, prefs in foreign_rules:
        if key in tid:
            bad_prefixes = prefs
            break
    if not bad_prefixes:
        return []
    out = []
    for s in stem_list:
        if any(s.startswith(p) or p.rstrip("_") in s for p in bad_prefixes):
            if s.startswith(nav_ok):
                continue
            out.append(s)
    return sorted(set(out))


def _function_attribute_counter_errors(tree: ast.AST) -> list[str]:
    """禁止用 handler 函数属性当计数器（async 下不可靠）。"""
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        if node.attr in ("claim_count", "attempt_count", "count", "retry_count"):
            errors.append(
                f"第 {getattr(node, 'lineno', 0)} 行: "
                f"不要用 `{node.value.id}.{node.attr}` 存状态；"
                "请用模块级变量或 handler 外的 dict 计数"
            )
    seen: set[str] = set()
    out: list[str] = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:4]


def _match_image_stem_from_call(call: ast.Call) -> Optional[str]:
    if not isinstance(call.func, ast.Attribute):
        return None
    if call.func.attr not in ("match_image", "click_image", "wait_image"):
        return None
    if not call.args:
        return None
    arg0 = call.args[0]
    if (
        isinstance(arg0, ast.Call)
        and isinstance(arg0.func, ast.Name)
        and arg0.func.id == "_img"
        and arg0.args
        and isinstance(arg0.args[0], ast.Constant)
        and isinstance(arg0.args[0].value, str)
    ):
        return _img_stem(arg0.args[0].value)
    return None


def _is_not_match_stem(test: ast.AST, stem_part: str) -> bool:
    node = test
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    else:
        return False
    if isinstance(node, ast.Await):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    stem = _match_image_stem_from_call(node)
    return bool(stem and stem_part in stem)


def _is_match_stem(test: ast.AST, stem_part: str) -> bool:
    node = test
    if isinstance(node, ast.Await):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    stem = _match_image_stem_from_call(node)
    return bool(stem and stem_part in stem)


def _block_returns_exit(block: list[ast.stmt]) -> bool:
    for stmt in block:
        if isinstance(stmt, ast.Return):
            val = stmt.value
            if isinstance(val, ast.Constant) and val.value == "__exit__":
                return True
        if isinstance(stmt, ast.If) and _block_returns_exit(stmt.body):
            return True
    return False


def _function_only_returns_exit(fn: ast.AST) -> bool:
    """handler 无 browser 动作且所有 return 路径均为 __exit__。"""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if _function_has_browser_action(fn):
        return False
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    if not returns:
        return False
    saw_exit = False
    for r in returns:
        if r.value is None:
            continue
        if isinstance(r.value, ast.Constant) and r.value.value == "__exit__":
            saw_exit = True
            continue
        return False
    return saw_exit


def _resolve_state_scene_first_errors(tree: ast.AST) -> list[str]:
    fn = _module_functions(tree).get("_resolve_state")
    if not fn:
        return []
    try:
        src = ast.unparse(fn)
    except Exception:
        return []
    pos_states = src.find("if nxt in states")
    pos_scene = src.find("scene_map.get")
    if pos_states >= 0 and pos_scene >= 0 and pos_states < pos_scene:
        return [
            "_resolve_state 须先 SCENE_TO_STEP（scene_map.get），再 if nxt in states"
        ]
    return []


def _scene_key_premature_exit_errors(tree: ast.AST) -> list[str]:
    """业务场景名（非 hub）在 STATES 中不得绑定仅 __exit__ 的 handler。"""
    scene_keys = _unknown_state_scene_keys(tree)
    if not scene_keys:
        return []
    fns = _module_functions(tree)
    errors: list[str] = []
    for var, mapping in _iter_all_state_maps(tree):
        for sk in scene_keys:
            if sk in _HUB_SCENES:
                continue
            if not _task_var_matches_scene(var, sk):
                continue
            hname = mapping.get(sk)
            if not hname:
                continue
            fn = fns.get(hname)
            if fn is not None and _function_only_returns_exit(fn):
                errors.append(
                    f"{var}「{sk}」→ `{hname}` 仅 return __exit__"
                    "（应 return 业务步或 SCENE_TO_STEP 映射）"
                )
    return errors


def _loading_wait_step_errors(tree: ast.AST, explanation: str = "") -> list[str]:
    """介绍含过场/loading 时，须有独立等待步 + 长超时。"""
    expl = explanation or ""
    if not re.search(r"过场|loading|加载|战斗结束|长时间", expl, re.I):
        return []
    try:
        code_text = ast.unparse(tree)
    except Exception:
        code_text = ""
    errors: list[str] = []
    wait_key_re = re.compile(r"等待")
    if not wait_key_re.search(code_text):
        errors.append(
            "介绍含过场/loading：须有独立「等待*」状态或 handler，"
            "禁止仅靠短 sleep 后匹配 hub 场景 id"
        )
    min_wait = 180.0
    for n in tree.body:
        d = None
        var = ""
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and isinstance(n.value, ast.Dict):
                    if t.id.endswith("_TIMEOUT"):
                        d = n.value
                        var = t.id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if isinstance(n.value, ast.Dict) and n.target.id.endswith("_TIMEOUT"):
                d = n.value
                var = n.target.id
        if d is None:
            continue
        for k_node, v_node in zip(d.keys, d.values):
            if not isinstance(k_node, ast.Constant) or not isinstance(k_node.value, str):
                continue
            if not wait_key_re.search(k_node.value):
                continue
            if isinstance(v_node, ast.Constant) and isinstance(v_node.value, (int, float)):
                if float(v_node.value) < min_wait:
                    errors.append(
                        f"{var}「{k_node.value}」超时 {v_node.value}s 过短，"
                        f"loading/战斗等待建议 >={min_wait:.0f}s"
                    )
    fns = _module_functions(tree)
    for fn in fns.values():
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not wait_key_re.search(fn.name):
            continue
        try:
            src = ast.unparse(fn)
        except Exception:
            src = ""
        if "deadline" not in src and "STEP_TIMEOUT" not in src:
            if not re.search(r"\+\s*(?:180|240|300|360|420|480)", src):
                errors.append(
                    f"{fn.name}: 等待步须显式长超时循环（deadline 或 STEP_TIMEOUT）"
                )
    return errors


def _explanation_retry_goto_exit_errors(tree: ast.AST, explanation: str = "") -> list[str]:
    """介绍写回退步 + 累计次数结束时，代码须在相关 handler 内实现计数。"""
    expl = explanation or ""
    if not re.search(
        r"回到第\s*\d+\s*步|返回第\s*\d+\s*步|返回上一步|回到.*?步", expl
    ):
        return []
    m = re.search(
        r"累计\s*(\d+)\s*次|(\d+)\s*次.*?(?:本任务|任务)(?:结束|完成)",
        expl,
    )
    if not m:
        return []
    limit = int(m.group(1) or m.group(2))
    try:
        code_text = ast.unparse(tree)
    except Exception:
        code_text = ""
    errors: list[str] = []
    limit_re = (
        rf"(?:>=|==)\s*{limit}|range\s*\(\s*{limit}|"
        rf"range\s*\(\s*1,\s*max_no_ok|attempt\s*>=\s*max_no_ok|"
        rf"miss_count\s*>=\s*{limit}"
    )
    if not re.search(limit_re, code_text):
        errors.append(
            f"介绍要求累计 {limit} 次后本任务结束，"
            f"代码须有 >=/=={limit} 或 range({limit}) 计数"
        )
    # 须在「点击奖励/收取」类 handler 内：无确认窗时计数，达限 __exit__
    fns = _module_functions(tree)
    claim_handlers: list[str] = []
    for name, fn in fns.items():
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        stems = _function_img_stems(fn)
        if not any("收取" in s or "claim" in s.lower() or "_reward" in s.lower() for s in stems):
            continue
        if not _function_has_browser_action(fn):
            continue
        try:
            src = ast.unparse(fn)
        except Exception:
            src = ""
        if re.search(limit_re, src) and "__exit__" in src:
            continue
        claim_handlers.append(name)
    if claim_handlers and re.search(r"ok|确认|弹窗", expl, re.I):
        errors.append(
            f"介绍要求点收取后无确认窗累计 {limit} 次结束；"
            f"handler（如 {claim_handlers[0]}）须在点击后检测确认图，"
            f"无则计数，达 {limit} 次 return '__exit__'，未达则 return 重试步"
        )
    return errors


def _function_has_ok_branch_before(fn: ast.AST, before_lineno: int) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if getattr(node, "lineno", 0) >= before_lineno:
            continue
        if _is_match_stem(node.test, "room_ok"):
            return True
    return False


def _room_ok_loop_exit_errors(tree: ast.AST, explanation: str = "") -> list[str]:
    """介绍写「没有 room_ok 后任务结束」时，ok 循环后须 __exit__ 而非回业务步。"""
    if not _room_ok_loop_exit_required(explanation):
        return []
    errors: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        try:
            src = ast.unparse(node)
        except Exception:
            src = ""
        if "room_ok" not in src:
            continue
        if not re.search(r"while\s+await\s+browser\.match_image\([^)]*room_ok", src):
            continue
        if re.search(
            r"while\s+await\s+browser\.match_image\([^)]*room_ok"
            r"[\s\S]{0,1500}?return\s+[\"'](?!__exit__)",
            src,
        ):
            errors.append(
                f"{node.name}: room_ok 循环结束后须 return '__exit__'（介绍：没有 room_ok 后任务结束），"
                "禁止 return 检查奖励/点击收取等步"
            )
    return errors[:4]


def _jjc_refresh_offset_errors(tree: ast.AST, explanation: str = "") -> list[str]:
    offset = _jjc_refresh_offset_from_explanation(explanation or "")
    if offset is None:
        return []
    ox, oy = offset
    if ox == -20:
        return []
    try:
        code_text = ast.unparse(tree)
    except Exception:
        code_text = ""
    if "jjc_刷新" not in code_text:
        return []
    errors: list[str] = []
    if re.search(r"jjc_刷新[^)]*pianyi=\(-20,\s*0\)", code_text):
        errors.append(
            f"介绍要求 jjc_刷新 点击偏移 ({ox},{oy})，代码仍使用 pianyi=(-20, 0)"
        )
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        stems = _function_img_stems(node)
        if not any("jjc_刷新" in s for s in stems):
            continue
        try:
            src = ast.unparse(node)
        except Exception:
            src = ""
        if re.search(r"pianyi=\(-20,\s*0\)", src) and "jjc_刷新" in src:
            errors.append(
                f"{node.name}: jjc_刷新 click_image 须 pianyi=({ox}, {oy})"
            )
    return errors[:3]


def _jjc_duanwei_max_x_errors(tree: ast.AST, explanation: str = "") -> list[str]:
    expl = explanation or ""
    if not re.search(r"jjc_段位", expl, re.I):
        return []
    if not re.search(r"x\s*最大|x最大|最靠右", expl, re.I):
        return []
    errors: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        try:
            src = ast.unparse(node)
        except Exception:
            src = ""
        if "jjc_段位" not in src or "match_image_multi" not in src:
            continue
        if re.search(r"max\s*\([^)]*key\s*=\s*lambda\s+m:\s*m\[['\"]y['\"]", src):
            errors.append(
                f"{node.name}: jjc_段位须 match_image_multi 后 max(matches, key=lambda m: m['x'])，"
                "介绍要求 x 最大（勿用 m['y']）"
            )
        elif "max(matches" in src and "m['x']" not in src and 'm["x"]' not in src:
            errors.append(
                f"{node.name}: jjc_段位 multi 命中后须按 x 取 max 再 click"
            )
    return errors[:3]


def _room_claim_popup_errors(tree: ast.AST, explanation: str = "") -> list[str]:
    """弹窗/确认窗：无主按钮时若仍有 ok 弹窗须先处理，禁止直接 __exit__。"""
    expl = (explanation or "").lower()
    need = bool(
        re.search(r"room|房间|收取奖励|room_ok", expl, re.I)
        or re.search(r"room_ok|room_收取|收取奖励", ast.unparse(tree) if hasattr(ast, "unparse") else "", re.I)
    )
    if not need:
        return []
    errors: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        stems = _function_img_stems(node)
        has_claim = any("收取奖励" in s or s == "room_收取奖励" for s in stems)
        if not has_claim:
            continue
        has_ok = any("room_ok" in s for s in stems)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.If):
                continue
            if not _is_not_match_stem(sub.test, "收取奖励"):
                continue
            if not _block_returns_exit(sub.body):
                continue
            lineno = getattr(sub, "lineno", 0)
            if has_ok and _function_has_ok_branch_before(node, lineno):
                continue
            errors.append(
                f"{node.name}@{lineno}: 无主奖励按钮时直接 __exit__；"
                "若确认弹窗已开须先 match/click 并循环，再判断无奖励"
            )
        if has_claim and not has_ok and re.search(r"room_ok|收取奖励|弹窗|确认", expl, re.I):
            errors.append(
                f"{node.name}: handler 引用了收取/奖励图但未引用确认弹窗图；"
                "介绍要求点击收取后处理确认弹窗"
            )
    seen: set[str] = set()
    out: list[str] = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:4]


def _duplicate_unused_task_tables_errors(tree: ast.AST) -> list[str]:
    """假多任务壳：TASK*_STATES / _TIMEOUT / _SCENE_MAP 复制自 STATES（或互相复制），
    却在 do_work / run_task 中从未被引用 → 冗余壳，提示收敛为单任务表。"""
    try:
        load_ctx = ast.Load
    except Exception:  # pragma: no cover
        load_ctx = None
    copies: dict[str, str] = {}
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        if not (
            name.endswith("_STATES")
            or name.endswith("_TIMEOUT")
            or name.endswith("_SCENE_MAP")
        ):
            continue
        v = node.value
        src = ""
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "copy":
            if isinstance(v.func.value, ast.Name):
                src = v.func.value.id
        elif isinstance(v, ast.Name):
            src = v.id
        if src:
            copies[name] = src
    errors: list[str] = []
    for name, src in sorted(copies.items()):
        if name in used:
            continue
        # 被其它表 copy 引用也算用过（链式复制仍冗余，不重复报）
        if any(name == s for s in copies.values()):
            continue
        errors.append(
            f"{name} 复制自 {src} 但脚本从未使用（假多任务壳）："
            "单任务/单流程脚本不要复制多任务状态表，直接在 do_work 里跑一张表即可"
        )
    return errors[:6]


def _explanation_condition_image_errors(tree: ast.AST, explanation: str) -> list[str]:
    """介绍把某个素材明确用于「结束/通关判定或切栏条件」时，代码必须引用它。

    覆盖：无 new + 全 complete + 切栏 N 次才 __exit__ 这类条件——漏引条件图
    （如 1_complete）会让挑战失败丢 new 时被误判为通关。
    """
    expl = (explanation or "").strip()
    if not expl:
        return []
    code_text = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    if not code_text:
        return []
    stem_re = re.compile(
        r"(?<![A-Za-z0-9_\u4e00-\u9fff])"
        r"([A-Za-z0-9_][A-Za-z0-9_\-]*(?:[\u4e00-\u9fff]+)?)\.png"
    )
    # 勿用裸「都」「切换」（「切换成已选」会误伤属性按钮）；通关/结束/切栏/无 new 才算条件句
    kw_re = re.compile(
        r"通关|完成|结束|都通关|都已|表示该|判定|条件|上限|"
        r"切栏|切换任务栏|切换栏|三次切换|"
        r"无.?new|没有.?new|若没有|如果没有",
        re.I,
    )
    missing: list[str] = []
    seen_stem: set[str] = set()
    # 弹出层语义（关闭/广告）已用 close/ok/err 或 guard 处理时不强求引用标识图本身
    close_handled = bool(
        re.search(r"_img\(['\"][^'\"]*(close|ok|err)[^'\"]*['\"]\)", code_text, re.I)
        or "register_guard" in code_text
    )
    for line in expl.splitlines():
        for sent in re.split(r"[。；;]", line):
            m = stem_re.search(sent)
            if not m:
                continue
            stem = m.group(1)
            if stem.lower() in ("home", "back", "new") or stem in seen_stem:
                continue
            if not kw_re.search(sent):
                continue
            seen_stem.add(stem)
            if re.search(r"_img\(['\"]" + re.escape(stem) + r"['\"]\)", code_text):
                continue
            # 间接引用也算：素材名以字符串出现在表/循环里（_img(p) 之类），
            # 以前只认字面量 → 明明引用了还被判「未引用」，卡住修订轮。
            # 字符串里「包含」该素材名即算引用（生成常用 '子目录/属性/1_dark_1' 这种带路径写法）
            if re.search(r"['\"][^'\"]*" + re.escape(stem) + r"(\.png)?['\"]", code_text):
                continue
            if close_handled and re.search(r"关闭|广告|弹窗|无视|直接关", sent):
                # 语义 = 关闭弹出层，代码已有 close/ok/err 处理即视为覆盖
                continue
            missing.append(stem)
    if not missing:
        return []
    return [
        f"介绍把 {s} 用于结束/通关判定或切栏条件，但代码未引用该素材——"
        "结束条件会漏判或误判（如挑战失败丢 new 时提前 __exit__）"
        for s in sorted(missing)[:6]
    ]


_SETTLE_CLICK_ORDER = ("4_next", "4_next_1", "4_cihe", "4_rank")


def _settle_img_stem_pat(stem: str) -> re.Pattern:
    """匹配 _img('stem')，避免 4_next 误吃 4_next_1。"""
    return re.compile(
        rf"_img\(\s*['\"]{re.escape(stem)}(?![\w-])(?:\.png)?['\"]"
    )


def _settle_stem_in_ast(node: ast.AST) -> Optional[int]:
    """返回结算点击优先级（越小越优先）；无关则 None。"""
    text = ""
    try:
        text = ast.unparse(node) if hasattr(ast, "unparse") else ""
    except Exception:
        return None
    best: Optional[int] = None
    for i, stem in enumerate(_SETTLE_CLICK_ORDER):
        if _settle_img_stem_pat(stem).search(text):
            best = i if best is None else min(best, i)
    return best


def _settlement_priority_errors(tree: ast.AST, code: str = "") -> list[str]:
    """结算：4_next / 4_next_1 优先于 4_cihe，再才是 4_rank（介绍互斥优先级）。"""
    errors: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seg = ""
        if code and hasattr(ast, "get_source_segment"):
            try:
                seg = ast.get_source_segment(code, fn) or ""
            except Exception:
                seg = ""
        if not seg:
            try:
                seg = ast.unparse(fn) if hasattr(ast, "unparse") else ""
            except Exception:
                continue
        if "4_rank" not in seg:
            continue
        if not any(s in seg for s in ("4_next", "4_next_1", "4_cihe")):
            continue
        hits: list[tuple[int, int, str]] = []
        for i, stem in enumerate(_SETTLE_CLICK_ORDER):
            m = _settle_img_stem_pat(stem).search(seg)
            if m:
                hits.append((m.start(), i, stem))
        if len(hits) < 2:
            continue
        hits.sort(key=lambda x: x[0])
        order = [h[1] for h in hits]
        if order != sorted(order):
            got = " → ".join(h[2] for h in hits)
            want = " → ".join(
                s for s in _SETTLE_CLICK_ORDER if any(h[2] == s for h in hits)
            )
            errors.append(
                f"{fn.name}: 结算点击优先级应为 {want}，当前源码顺序为 {got}"
            )
    return errors[:4]


def _guard_identifier_pair_errors(tree: ast.AST, explanation: str) -> list[str]:
    """介绍里「标识图 + 按钮」成对出现时，代码不能只点按钮不匹配标识。"""
    expl = explanation or ""
    if not expl.strip():
        return []
    try:
        text = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    except Exception:
        return []
    if not text:
        return []
    expl_stems = _stems_mentioned_in_explanation(expl)
    code_stems = {_norm_img_key(n) for n in _collect_img_names(tree)}
    # register_guard('err1_1') 也算引用了按钮
    for m in re.finditer(
        r"register_guard\(\s*['\"]([^'\"]+)['\"]",
        text,
    ):
        code_stems.add(_norm_img_key(m.group(1)))

    pairs = (
        ("err1", "err1_1"),
        ("err2", "err2_2"),
        ("1_shop", "1_close"),
    )
    errors: list[str] = []
    for ident, btn in pairs:
        if ident not in expl_stems or btn not in expl_stems:
            continue
        if btn in code_stems and ident not in code_stems:
            errors.append(
                f"介绍以 {ident}.png 为标识、{btn}.png 为按钮，"
                f"代码引用了 {btn} 但未匹配 {ident}——可能误点或漏关"
            )
    return errors[:4]


def patch_settlement_click_priority(code: str) -> tuple[str, list[str]]:
    """结算 handler 内连续 if：按 4_next → 4_next_1 → 4_cihe → 4_rank 重排。"""
    if not (code or "").strip():
        return code, []
    if "4_rank" not in code or "4_next" not in code:
        return code, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []
    notes: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        new_body: list[ast.stmt] = []
        i = 0
        body = list(fn.body)
        fn_changed = False
        while i < len(body):
            stmt = body[i]
            prio = _settle_stem_in_ast(stmt) if isinstance(stmt, ast.If) else None
            if prio is None:
                new_body.append(stmt)
                i += 1
                continue
            run: list[ast.If] = []
            while i < len(body) and isinstance(body[i], ast.If):
                p = _settle_stem_in_ast(body[i])
                if p is None:
                    break
                run.append(body[i])  # type: ignore[arg-type]
                i += 1
            ordered = sorted(
                run,
                key=lambda s: (
                    p if (p := _settle_stem_in_ast(s)) is not None else 99
                ),
            )
            if [id(x) for x in ordered] != [id(x) for x in run]:
                fn_changed = True
            new_body.extend(ordered)
        if fn_changed:
            fn.body = new_body
            notes.append(f"{fn.name}: 结算点击顺序改为 next → next_1 → cihe → rank")
    if not notes:
        return code, []
    # 逐函数替换源码片段，避免整文件 unparse
    new_code = code
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(fn.name in n for n in notes):
            continue
        old_seg = ast.get_source_segment(code, fn) if hasattr(ast, "get_source_segment") else None
        if not old_seg:
            continue
        try:
            new_seg = ast.unparse(fn)
        except Exception:
            continue
        if old_seg in new_code:
            new_code = new_code.replace(old_seg, new_seg, 1)
    try:
        ast.parse(new_code)
    except SyntaxError:
        return code, []
    return new_code, notes


def patch_exit_require_complete_from_intro(
    code: str,
    explanation: str = "",
    source_dir: str = "",
) -> tuple[str, list[str]]:
    """介绍用 1_complete 作通关/结束条件时，无 new 就 __exit__ 的路径补 complete 确认。"""
    expl = explanation or ""
    if "1_complete" not in expl and "complete.png" not in expl:
        return code, []
    if re.search(r"_img\(\s*['\"]1_complete(?:\.png)?['\"]", code):
        return code, []
    img_root = Path(source_dir or "")
    if source_dir and img_root.is_dir() and not _image_exists_in_dir(img_root, "1_complete"):
        return code, []
    if 'return "__exit__"' not in code and "return '__exit__'" not in code:
        return code, []
    # 仅改「选关/扫描」相关函数里的裸 __exit__
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, []

    inject = (
        "        if not await browser.match_image(_img('1_complete'), threshold=CFG.threshold):\n"
        "            browser.script_log('[选关] 无 new 但未见 complete，不结束')\n"
        "            return None\n"
    )
    notes: list[str] = []
    new_code = code
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not re.search(r"选关|扫描|scan|tab", fn.name, re.I):
            continue
        seg = ast.get_source_segment(code, fn) if hasattr(ast, "get_source_segment") else None
        if not seg or "1_complete" in seg:
            continue
        if 'return "__exit__"' not in seg and "return '__exit__'" not in seg:
            continue
        # 在每个 return "__exit__" 前插入 complete 检查（同缩进）
        def _inject_before_exit(m: re.Match) -> str:
            ind = m.group(1)
            # inject 使用 8 空格模板，改成与 return 同级
            block = (
                f"{ind}if not await browser.match_image(_img('1_complete'), threshold=CFG.threshold):\n"
                f"{ind}    browser.script_log('[选关] 无 new 但未见 complete，不结束')\n"
                f"{ind}    return None\n"
                f"{m.group(0)}"
            )
            return block

        newer, nsub = re.subn(
            r"^([ \t]*)return ['\"]__exit__['\"]",
            _inject_before_exit,
            seg,
            count=2,
            flags=re.M,
        )
        if nsub and newer != seg and seg in new_code:
            new_code = new_code.replace(seg, newer, 1)
            notes.append(f"{fn.name}: __exit__ 前补 1_complete 确认 ×{nsub}")
    if not notes:
        # 退路：_scan_tab / 共用扫描函数
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not re.search(r"scan|切栏|选关", fn.name, re.I):
                continue
            seg = ast.get_source_segment(code, fn) if hasattr(ast, "get_source_segment") else None
            if not seg or "1_complete" in seg:
                continue
            if 'return "__exit__"' not in seg and "return '__exit__'" not in seg:
                continue

            def _inject2(m: re.Match) -> str:
                ind = m.group(1)
                return (
                    f"{ind}if not await browser.match_image(_img('1_complete'), threshold=CFG.threshold):\n"
                    f"{ind}    browser.script_log('[选关] 无 new 但未见 complete，不结束')\n"
                    f"{ind}    return None\n"
                    f"{m.group(0)}"
                )

            newer, nsub = re.subn(
                r"^([ \t]*)return ['\"]__exit__['\"]",
                _inject2,
                seg,
                count=2,
                flags=re.M,
            )
            if nsub and seg in new_code:
                new_code = new_code.replace(seg, newer, 1)
                notes.append(f"{fn.name}: __exit__ 前补 1_complete 确认 ×{nsub}")
                break
    if not notes:
        return code, []
    try:
        ast.parse(new_code)
    except SyntaxError:
        return code, []
    return new_code, notes


def patch_guard_identifier_pairs(
    code: str,
    explanation: str = "",
) -> tuple[str, list[str]]:
    """register_guard 只挂了按钮时，在 check_guards 前补标识 match（源码级窄修）。

    若已有自定义 check_guards 且含 click 无 match 标识，改为：先 match 标识再 click 按钮。
    对 register_guard('err1_1') 这类列表式守卫，改为注册标识图并在描述中保留按钮语义较难；
    此处仅当 check_guards 内直接 click_image(err1_1) 时插入 err1 match。
    """
    expl_stems = _stems_mentioned_in_explanation(explanation or "")
    pairs = (("err1", "err1_1"), ("err2", "err2_2"), ("1_shop", "1_close"))
    notes: list[str] = []
    new_code = code
    for ident, btn in pairs:
        if ident not in expl_stems or btn not in expl_stems:
            continue
        if re.search(rf"_img\(\s*['\"]{re.escape(ident)}(?:\.png)?['\"]", new_code):
            continue
        if not re.search(rf"['\"]{re.escape(btn)}(?:\.png)?['\"]", new_code):
            continue
        # register_guard('btn') → 在 do_work 里于该行前增加注释性：改为先 register 无用
        # 更稳：把 register_guard('err1_1') 换成两行逻辑写进 check_guards 太重。
        # 采用：register_guard 参数改为标识图（点击仍要点按钮）——改 API 语义危险。
        # 改为在 check_guards 循环前插入显式块（若函数体是 for GUARDS 循环）。
        block = (
            f"    if await browser.match_image(_img('{ident}'), threshold=CFG.icon_threshold):\n"
            f"        browser.script_log('[guard] {ident} -> {btn}')\n"
            f"        if await browser.click_image(_img('{btn}'), threshold=CFG.threshold):\n"
            f"            await browser.b_sleep(0.3, 0.8)\n"
            f"            return True\n"
        )
        # 插入到 check_guards 函数开头（async def check_guards 后首个实质行前）
        m = re.search(
            r"(async def check_guards\([^)]*\)[^\n]*\n(?:[ \t]*\"\"\"[\s\S]*?\"\"\"\n)?)",
            new_code,
        )
        if not m:
            m = re.search(r"(def check_guards\([^)]*\)[^\n]*\n)", new_code)
        if not m:
            continue
        # 避免重复插入
        if f"_img('{ident}')" in new_code[m.end() : m.end() + 800]:
            continue
        new_code = new_code[: m.end()] + block + new_code[m.end() :]
        notes.append(f"check_guards: 补 {ident} 标识后再点 {btn}")
    if not notes:
        return code, []
    try:
        ast.parse(new_code)
    except SyntaxError:
        return code, []
    return new_code, notes


def _explanation_feedback_errors(tree: ast.AST, explanation: str) -> list[str]:
    """对照介绍末尾「试运行反馈」检查是否写进代码。"""
    try:
        from backend.script_generator.feedback_opt import extract_trial_constraints
    except Exception:
        return []
    bullets = extract_trial_constraints(explanation or "")
    if not bullets:
        return []
    text = ast.unparse(tree) if hasattr(ast, "unparse") else (explanation or "")
    code_lower = text.lower()
    errors: list[str] = []
    joined = "\n".join(bullets)
    if re.search(r"room_?ok|room_ok", joined, re.I) and "room_ok" not in code_lower:
        errors.append("试运行反馈要求处理 room_ok.png，但代码中未引用 room_ok")
    if re.search(r"room_?收取奖励|收取奖励", joined, re.I) and "room_收取奖励" not in code_lower and "收取奖励" not in code_lower:
        errors.append("试运行反馈要求处理 room_收取奖励，但代码中未引用")
    if re.search(r"三次|3次", joined) and not re.search(r"[>=]\s*3|==\s*3|三次", code_lower):
        errors.append("试运行反馈要求「三次」领取逻辑，但代码中未见 >=3 或 ==3 判断")
    if re.search(r"返回主界面|先回主界面", joined, re.I):
        room_maps = [m for v, m in _iter_task_state_maps(tree) if "room" in _task_id_from_var(v)]
        if room_maps and "返回主界面" not in room_maps[0]:
            errors.append("试运行反馈要求房间任务先返回主界面，但 TASK_room_STATES 缺少「返回主界面」")
    return errors


def _room_requires_go_home_errors(tree: ast.AST, explanation: str) -> list[str]:
    """介绍要求房间先回主界面时，TASK_room 必须有「返回主界面」。"""
    expl = explanation or ""
    if not re.search(r"房间|room", expl, re.I):
        return []
    if not re.search(r"返回主界面|@返回主界面|先.*主界面", expl):
        return []
    errors: list[str] = []
    found_room = False
    for var, mapping in _iter_task_state_maps(tree):
        if "room" not in _task_id_from_var(var):
            continue
        found_room = True
        if "返回主界面" not in mapping:
            errors.append(
                f"{var} 缺少「返回主界面」；介绍要求房间任务先执行返回主界面辅助步骤"
            )
    if not found_room and re.search(r"TASK_.*room|_room_", ast.unparse(tree) if hasattr(ast, "unparse") else "", re.I):
        pass
    return errors


def _business_marker_exit_errors(tree: ast.AST, explanation: str) -> list[str]:
    """引用 jjc_end / ta_cishu 等耗尽标识时，同任务应有 __exit__。"""
    expl = explanation or ""
    text = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    if "__exit__" in text:
        return []
    errors: list[str] = []
    if re.search(r"jjc_end", text, re.I) and re.search(r"jjc_end|竞技场", expl):
        errors.append("代码引用了 jjc_end，但未见 return '__exit__'（竞技场本任务完成应退出 run_task）")
    if re.search(r"ta_cishu", text, re.I) and re.search(r"ta_cishu|耗尽|塔", expl):
        errors.append("代码引用了 ta_cishu，但未见 return '__exit__'（次数耗尽应本任务完成）")
    return errors


def _dict_literal_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    if not isinstance(node, ast.Dict):
        return keys
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
    return keys


def _find_module_dict_assign(tree: ast.AST, name: str) -> Optional[ast.Dict]:
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name and isinstance(n.value, ast.Dict):
                    return n.value
        elif isinstance(n, ast.AnnAssign):
            if isinstance(n.target, ast.Name) and n.target.id == name and isinstance(n.value, ast.Dict):
                return n.value
    return None


def _iter_browser_calls(tree: ast.AST):
    """Yield (method_name, Call node) for browser.xxx(...)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "browser":
                yield func.attr, node


def _iter_browser_method_calls(tree: ast.AST):
    for attr, node in _iter_browser_calls(tree):
        yield attr, getattr(node, "lineno", 0)


def _collect_img_names(tree: ast.AST) -> list[str]:
    """收集 _img('xxx') / _img(\"xxx\") 中的图片名。"""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_img" and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                names.append(arg0.value)
    return names


def _find_do_work(tree: ast.AST) -> Optional[ast.AsyncFunctionDef]:
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "do_work":
            return n
    return None


def _image_file_existence_errors(tree: ast.AST, source_dir: str) -> list[str]:
    """检查 _img / register_guard 引用的文件是否在 source_dir 存在。"""
    src = (source_dir or "").strip()
    if not src:
        return ["未指定图片文件夹（source_dir），无法检查素材"]
    img_root = Path(src)
    if not img_root.is_dir():
        return [f"图片目录不存在: {src}"]
    missing: list[str] = []
    for name in _collect_img_names(tree):
        if not _image_exists_in_dir(img_root, name):
            key = _norm_img_key(name)
            missing.append(f"{key}.png" if key else name)
    for stem in _collect_missing_image_names(tree, img_root):
        fname = f"{stem}.png"
        if fname not in missing:
            missing.append(fname)
    if not missing:
        return []
    seen: set[str] = set()
    uniq: list[str] = []
    for m in missing:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    preview = ", ".join(uniq[:12])
    more = f" 等 {len(uniq)} 个" if len(uniq) > 12 else ""
    return [f"图片文件不存在于所选目录：{preview}{more}"]


def validate_image_assets(code: str, source_dir: str = "") -> list[str]:
    """仅检查素材文件是否存在（validate_script_local 的子集）。"""
    if not (code or "").strip():
        return ["代码为空"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"语法错误: {e.msg}（第 {e.lineno} 行）"]
    return _image_file_existence_errors(tree, source_dir)


def validate_for_codegen(
    code: str,
    plan: Optional[dict] = None,
    source_dir: str = "",
    image_paths: Optional[list] = None,
    explanation: str = "",
    free_mode: Optional[bool] = None,
) -> list[str]:
    """生成 / fix 循环校验：identifiers_only 时跳过素材；自由模式放宽语义项。"""
    free = is_codegen_free_mode(free_mode)
    pseudo = build_pseudo_plan_from_explanation(explanation or "")
    if plan and (plan.get("tasks") or []):
        plan_use = plan
    else:
        plan_use = pseudo if pseudo else (plan or {})
    try:
        from backend.script_generator.architecture import apply_architecture_to_plan
        plan_use = apply_architecture_to_plan(dict(plan_use or {}), explanation or "")
    except Exception:
        pass
    return validate_generated_code(
        code,
        plan=plan_use,
        source_dir=source_dir,
        image_paths=image_paths,
        explanation=explanation,
        free_mode=free_mode,
        check_image_files=False if is_img_identifiers_only() else None,
        relax_semantic=free,
    )


def validate_script_local(
    code: str,
    *,
    plan: Optional[dict] = None,
    source_dir: str = "",
    explanation: str = "",
    free_mode: Optional[bool] = None,
) -> list[str]:
    """本地脚本检查（写入试运行 / 保存前）：结构语义 + 素材，自动执行。"""
    return validate_generated_code(
        code,
        plan=plan,
        source_dir=source_dir,
        image_paths=[],
        explanation=explanation,
        free_mode=free_mode,
        check_image_files=True,
    )



def _cn_punct_outside_strings_error(code: str) -> str:
    """返回第一处「字符串/注释之外的中文标点」错误文案（无则空串）。

    用 tokenize 而不是逐行正则：多行 docstring 的续行以前会被误判成代码区标点，
    导致修订轮反复被告知「删第 N 行标点」，实际无从修改 → 重修订死循环。
    """
    import io
    import tokenize as _tok

    try:
        toks = list(_tok.generate_tokens(io.StringIO(code or "").readline))
    except Exception:
        return ""
    skip = {
        _tok.STRING,
        _tok.COMMENT,
        _tok.NL,
        _tok.NEWLINE,
        _tok.INDENT,
        _tok.DEDENT,
        _tok.ENDMARKER,
    }
    for t in toks:
        if t.type in skip:
            continue
        if _CN_PUNCT_RE.search(t.string):
            return f"第 {t.start[0]} 行代码区含中文标点（非字符串/注释）"
    return ""


def validate_generated_code(
    code: str,
    plan: Optional[dict] = None,
    source_dir: str = "",
    image_paths: Optional[list] = None,
    explanation: str = "",
    free_mode: Optional[bool] = None,
    check_image_files: Optional[bool] = None,
    relax_semantic: bool = False,
) -> list[str]:
    """本地校验生成代码，返回错误列表（空表示通过）。"""
    errors: list[str] = []
    if not (code or "").strip():
        return ["生成结果为空"]

    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        errors.append(f"语法错误: {e.msg}（第 {e.lineno} 行）")
        return errors

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        errors.append(f"AST 解析失败: {e.msg}（第 {e.lineno} 行）")
        return errors

    top_names = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assigned = _collect_assigned_names(tree)
    kind = (plan or {}).get("kind") if plan else None
    free = is_codegen_free_mode(free_mode)
    plan_arch = ((plan or {}).get("architecture") or "") if plan else ""
    arch = str(plan_arch or "").strip()
    if not arch:
        try:
            from backend.script_generator.architecture import infer_architecture
            arch, _ = infer_architecture(explanation or "")
        except Exception:
            arch = ""

    if "do_work" not in top_names:
        errors.append("缺少 async def do_work(...)")
    else:
        dw = _find_do_work(tree)
        if dw is not None:
            if not dw.args.args:
                errors.append("do_work 缺少参数，需要 (browser: UserBrowser) 或 (win: UserWindow)")
            else:
                ann = dw.args.args[0].annotation
                if ann is None:
                    errors.append(
                        "do_work 第一个参数缺少类型标注，请写 "
                        "(browser: UserBrowser) 或 (win: UserWindow)"
                    )
                else:
                    ann_src = ast.unparse(ann) if hasattr(ast, "unparse") else ""
                    if not any(t in ann_src for t in ("UserBrowser", "UserWindow", "Browser")):
                        errors.append(
                            f"do_work 参数类型必须为 UserBrowser / UserWindow，当前为: {ann_src or '?'}"
                        )

    # IMG_DIR 路径校验（自由模式仍保留，否则试跑找不到图）
    if "IMG_DIR" not in assigned:
        errors.append("缺少 IMG_DIR 赋值")
    src = (source_dir or "").strip()
    if src:
        expected = build_img_dir_line(src)
        # 禁止占位目录
        if re.search(r"IMG_DIR\s*=\s*IMG_PATH\s*/\s*['\"]game['\"]\s*/\s*['\"]script['\"]", code):
            errors.append(
                f"IMG_DIR 仍是占位路径 game/script，应为所选目录：{expected}"
            )
        elif expected.replace(" ", "") not in code.replace(" ", ""):
            # 宽松：去掉空格后比较是否包含关键相对路径片段
            try:
                from core.path import IMG_PATH
                rel = Path(src).resolve().relative_to(Path(IMG_PATH).resolve())
                for part in rel.parts:
                    if f"'{part}'" not in code and f'"{part}"' not in code:
                        errors.append(
                            f"IMG_DIR 未包含所选目录片段 '{part}'，期望类似：{expected}"
                        )
                        break
            except Exception:
                if str(Path(src)) not in code and src not in code:
                    errors.append(f"IMG_DIR 未指向所选图片目录：{src}")

        # 成品校验：默认 identifiers_only 时跳过；check_image_files=True 强制检查
        want_img_check = (
            check_image_files
            if check_image_files is not None
            else not is_img_identifiers_only()
        )
        if want_img_check:
            for msg in _image_file_existence_errors(tree, src):
                errors.append(msg)
        # 幻觉图：identifiers_only / 自由模式也硬拦（介绍未点名且目录无）
        for msg in _hallucinated_image_errors(tree, src, explanation or ""):
            errors.append(msg)
        # 盘内但介绍未点名（如 MENU chrome）——与提交白名单一致
        for msg in _intro_unmentioned_image_errors(tree, src, explanation or ""):
            errors.append(msg)
    elif image_paths:
        errors.append("未指定图片文件夹（source_dir），无法校验 IMG_DIR / 图片路径")

    # click/match/wait 首参字面量 None（剥离残留 / 模型手写）
    for msg in _none_browser_image_arg_errors(tree):
        errors.append(msg)

    # 自由模式：生成宽松，但以下结构与语义校验全部执行

    has_states = "STATES" in assigned
    has_task_states = any(name.endswith("_STATES") for name in assigned)
    has_run_task = "run_task" in top_names
    has_timeout = "STATE_TIMEOUT" in assigned or any(
        name.endswith("_TIMEOUT") for name in assigned
    )

    if kind == "multi_task":
        if not has_run_task:
            errors.append("multi_task 计划要求定义 run_task(...)")
        if not has_task_states and not has_states:
            errors.append("multi_task 计划要求 TASK*_STATES（或 STATES）")
    elif kind == "utility":
        # utility 允许只有 helpers；仍建议有 do_work 入口（上面已查）
        pass
    else:
        # single_fsm / unknown
        if not has_states and not (has_task_states and has_run_task):
            errors.append("缺少 STATES 字典（或 TASK*_STATES + run_task）")

    expl_tasks = _count_explanation_tasks(explanation or "")
    # scene_driven：编号块是场景 handler，不要求 run_task 多任务队列
    if expl_tasks >= 2 and not has_run_task and arch != "scene_driven":
        errors.append(
            f"介绍含 {expl_tasks} 个子任务，须定义 run_task(...)"
        )

    if not has_timeout and (has_states or has_task_states):
        errors.append("缺少 STATE_TIMEOUT（或 TASK*_TIMEOUT）")

    # 未知 状态键
    unknown_keys = {"未知", "\u672a\u77e5"}
    found_unknown = False
    for dict_name in list(assigned):
        if dict_name == "STATES" or dict_name.endswith("_STATES"):
            d = _find_module_dict_assign(tree, dict_name)
            if d and (_dict_literal_keys(d) & unknown_keys):
                found_unknown = True
                break
    if (has_states or has_task_states) and not found_unknown:
        errors.append("STATES / TASK*_STATES 中缺少「未知」恢复状态")

    # browser.xxx 白名单
    allowed_methods = set(ALLOWED_BROWSER_METHODS)
    try:
        from backend.script_generator.api_catalog import allow_login_from_explanation
        if allow_login_from_explanation(explanation or ""):
            allowed_methods |= LOGIN_BROWSER_METHODS
    except Exception:
        pass
    illegal: list[str] = []
    for method, lineno in _iter_browser_method_calls(tree):
        if method not in allowed_methods:
            illegal.append(f"{method}@{lineno}")
    if illegal:
        # 去重保留顺序
        seen = set()
        uniq = []
        for item in illegal:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        allowed = ", ".join(sorted(allowed_methods))
        errors.append(
            f"非法 browser 方法: {', '.join(uniq[:8])}；仅允许: {allowed}"
        )

    # 未定义函数调用（如 handle_battle_result 未 import）
    undef = _find_undefined_calls(tree)
    if undef:
        errors.append(
            "存在未定义/未导入的调用: "
            + ", ".join(undef[:8])
            + "；请补全 import 或删除调用"
        )

    # 未定义名字（STATE_TIMEOUT.get 这类不会被「调用」检查抓到）
    undef_names = _find_undefined_name_loads(tree)
    if undef_names and relax_semantic:
        undef_names = [u for u in undef_names if not u.startswith("CFG@")]
    if undef_names:
        # 若仅 STATE_TIMEOUT 且已有 TASK_*_TIMEOUT，给更明确提示
        only_st = all(u.startswith("STATE_TIMEOUT@") for u in undef_names)
        task_tos = sorted(
            n for n in assigned
            if n.endswith("_TIMEOUT") and n != "STATE_TIMEOUT"
        )
        if only_st and task_tos:
            errors.append(
                "使用了未定义的 STATE_TIMEOUT；多任务请用 "
                + ", ".join(task_tos)
                + "，或定义 STATE_TIMEOUT = {} 后 update 各 TASK_*_TIMEOUT"
            )
        else:
            errors.append(
                "存在未定义的名字: "
                + ", ".join(undef_names[:8])
                + "；运行时会 NameError"
            )

    # unknown_state 误路由
    for msg in _unknown_state_returns_unknown_on_match(tree):
        errors.append(msg)
    for msg in _unknown_state_filename_keys(tree):
        errors.append(msg)
    for msg in _unknown_state_miss_must_sleep(tree):
        errors.append(msg)
    # 点后确认：自由模式也硬拦（返回「等待/转场」态可豁免）
    for msg in _click_then_wait_errors(tree):
        errors.append(msg)
    if not relax_semantic:
        for msg in _stale_frame_after_action_errors(tree):
            errors.append(msg)
        for msg in _scene_id_threshold_errors(tree):
            errors.append(msg)
    for msg in _goal_exit_errors(tree, explanation or ""):
        errors.append(msg)
    for msg in _match_multi_dict_errors(tree):
        errors.append(msg)
    for msg in _hub_entry_self_loop_errors(tree):
        errors.append(msg)
    for msg in _resolve_state_scene_first_errors(tree):
        errors.append(msg)
    for msg in _scene_key_premature_exit_errors(tree):
        errors.append(msg)
    for msg in _loading_wait_step_errors(tree, explanation or ""):
        errors.append(msg)
    for msg in _state_dict_lambda_errors(tree):
        errors.append(msg)
    for msg in _run_task_unknown_trap_errors(tree):
        errors.append(msg)
    for msg in _run_task_transition_hold_errors(tree):
        errors.append(msg)
    for msg in _home_nav_exit_errors(tree):
        errors.append(msg)
    if not relax_semantic:
        for msg in _shared_task_hub_handler_errors(tree):
            errors.append(msg)
        for msg in _task_entry_img_prefix_errors(tree, plan=plan):
            errors.append(msg)
    for msg in _function_attribute_counter_errors(tree):
        errors.append(msg)
    if not relax_semantic:
        for msg in _explanation_feedback_errors(tree, explanation or ""):
            errors.append(msg)
        for msg in _room_requires_go_home_errors(tree, explanation or ""):
            errors.append(msg)
        for msg in _business_marker_exit_errors(tree, explanation or ""):
            errors.append(msg)
    for msg in _room_claim_popup_errors(tree, explanation or ""):
        errors.append(msg)
    for msg in _room_ok_loop_exit_errors(tree, explanation or ""):
        errors.append(msg)
    for msg in _jjc_refresh_offset_errors(tree, explanation or ""):
        errors.append(msg)
    for msg in _jjc_duanwei_max_x_errors(tree, explanation or ""):
        errors.append(msg)
    for msg in _explanation_retry_goto_exit_errors(tree, explanation or ""):
        errors.append(msg)
    if not relax_semantic:
        for msg in _task_scene_state_wiring_errors(tree):
            errors.append(msg)
    for msg in _explanation_multi_task_structure_errors(
        tree,
        explanation or "",
        check_business_imgs=not relax_semantic,
    ):
        errors.append(msg)
    for msg in _explanation_helper_presence_errors(tree, explanation or ""):
        errors.append(msg)
    if not relax_semantic:
        for msg in _explanation_scene_layer_errors(tree, explanation or ""):
            errors.append(msg)
    # 空壳 handler 属硬结构问题：任何模式（含自由模式）都必须报，避免
    # 「生成期放行、UI 严格校验拦住」的两套口径。
    for msg in _stub_handler_errors(tree):
        errors.append(msg)
    if not relax_semantic:
        for msg in _do_work_blind_scene_routing_errors(tree):
            errors.append(msg)
        for msg in _run_task_entry_state_errors(tree):
            errors.append(msg)
        for msg in _run_task_bootstrap_errors(tree):
            errors.append(msg)

    # multi_task 状态表键
    for msg in _validate_task_state_keys(tree, plan):
        errors.append(msg)

    # reuse import
    for msg in _validate_reuse_imports(tree, plan, code):
        errors.append(msg)

    # 跨任务 handler 串表 / 模块级前向引用（import 期 NameError 类致命问题）
    for msg in _task_table_foreign_handler_errors(tree):
        errors.append(msg)
    for msg in _module_level_forward_ref_errors(tree):
        errors.append(msg)

    # 假多任务壳 / 结束条件素材 / 结算优先级 / 守卫标识对
    for msg in _duplicate_unused_task_tables_errors(tree):
        errors.append(msg)
    for msg in _explanation_condition_image_errors(tree, explanation or ""):
        errors.append(msg)
    for msg in _settlement_priority_errors(tree, code):
        errors.append(msg)
    for msg in _guard_identifier_pair_errors(tree, explanation or ""):
        errors.append(msg)

    _punct_err = _cn_punct_outside_strings_error(code)
    if _punct_err:
        errors.append(_punct_err)

    # 方案 E：架构硬闸
    try:
        defaults_arch = _load_config().get("defaults") or {}
        if defaults_arch.get("architecture_enforce", True):
            from backend.script_generator.architecture import validate_architecture_code
            for msg in validate_architecture_code(
                code, explanation or "", arch=arch or None,
            ):
                errors.append(msg)
    except Exception:
        pass

    # 方案 C/D：执行清单覆盖（离线硬闸 + 缺项对照明细）
    try:
        defaults_v = _load_config().get("defaults") or {}
        if defaults_v.get("execution_checklist", True):
            from backend.script_generator.execution_checklist import (
                checklist_coverage_errors,
                checklist_coverage_report,
                extract_execution_checklist,
            )
            items = extract_execution_checklist(explanation or "")
            for msg in checklist_coverage_errors(
                code, items, explanation=explanation or "",
            ):
                errors.append(msg)
            if defaults_v.get("checklist_llm_coverage", True) and items:
                report = checklist_coverage_report(code, items)
                missing = report.get("missing") or []
                if missing:
                    preview = "; ".join(missing[:6])
                    more = f" 等{len(missing)}项" if len(missing) > 6 else ""
                    errors.append(
                        f"清单对照缺失项（须补齐，禁止仅宣称已完成）: {preview}{more}"
                    )
    except Exception:
        pass

    return errors


def resolve_max_tokens(max_tokens: Optional[int] = None) -> Optional[int]:
    """解析 max_tokens。

    - 正整数 → 该上限
    - 0 → 无上限（返回 None，调用方不传 / 用模型侧最大）
    - None → 读 config.json defaults；配置为 0 同样表示无上限
    """
    if max_tokens is not None:
        try:
            n = int(max_tokens)
        except (TypeError, ValueError):
            n = -1
        if n == 0:
            return None
        if n > 0:
            return n
    try:
        n = int(_load_config().get("defaults", {}).get("max_tokens", 16384))
        if n == 0:
            return None
        if n > 0:
            return n
    except Exception:
        pass
    return 16384


# Claude 等强制要求 max_tokens 字段时的「无上限」占位
_UNLIMITED_MAX_TOKENS = 128000


async def call_llm(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int, int]:
    """统一 LLM 调用入口，返回 (text, input_tokens, output_tokens)。"""
    mt = resolve_max_tokens(max_tokens)
    info = _provider_info(provider)
    if not info and provider not in ("claude", "openai", "google", "deepseek", "groq"):
        raise ValueError(f"不支持的 provider: {provider}")
    endpoint = (api_endpoint or "").strip() or (info.get("default_endpoint") or "").strip() or None
    extra_headers = info.get("extra_headers") if isinstance(info.get("extra_headers"), dict) else None
    api = _provider_api(provider)
    if api == "claude":
        return await _call_claude(
            api_key, model, endpoint, messages, system_prompt,
            on_partial=on_partial, max_tokens=mt,
        )
    if api == "google":
        text = await _call_gemini(api_key, model, endpoint, messages, system_prompt, on_partial=on_partial)
        return text, 0, 0
    return await _call_openai(
        api_key, model, endpoint, messages, system_prompt,
        on_partial=on_partial, provider=provider, max_tokens=mt,
        extra_headers=extra_headers,
    )


def _provider_supports_tools(provider: str) -> bool:
    info = _provider_info(provider)
    if "supports_tools" in info:
        return bool(info["supports_tools"])
    if provider == "deepseek":
        return False
    api = _provider_api(provider)
    if api == "google":
        return False
    return api in ("openai", "claude")


def _tool_result_text(name: str, arguments, *, allow_login: bool) -> str:
    from backend.script_generator.api_catalog import lookup_api, parse_lookup_name

    if (name or "") != "lookup_api":
        return f"unknown tool {name!r}; only lookup_api is available"
    return lookup_api(parse_lookup_name(arguments), allow_login=allow_login)


async def call_llm_with_tools(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    on_status=None,
    max_tokens: Optional[int] = None,
    allow_login: bool = False,
    max_tool_rounds: int = 4,
) -> tuple[str, int, int]:
    """Generate-time LLM call with optional lookup_api tool loop."""
    from backend.script_generator.api_catalog import (
        claude_tools,
        generate_tool_hint,
        openai_tools,
    )

    supports = _provider_supports_tools(provider)
    prompt = (system_prompt or "") + generate_tool_hint(
        allow_login=allow_login, supports_tools=supports,
    )
    if not supports:
        return await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=prompt,
            on_partial=on_partial,
            max_tokens=max_tokens,
        )

    def _resolve(name, arguments) -> str:
        return _tool_result_text(name, arguments, allow_login=allow_login)

    api = _provider_api(provider)
    try:
        if api == "claude":
            return await _claude_tools_loop(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=messages,
                system_prompt=prompt,
                on_partial=on_partial,
                on_status=on_status,
                max_tokens=max_tokens,
                max_tool_rounds=max_tool_rounds,
                tools=claude_tools(),
                resolve_tool=_resolve,
                status_busy="生成中（可查阅 API 卡片）…",
                status_after="已查 API，继续生成…",
                status_call_prefix="查阅 API",
            )
        return await _openai_tools_loop(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=prompt,
            on_partial=on_partial,
            on_status=on_status,
            max_tokens=max_tokens,
            max_tool_rounds=max_tool_rounds,
            tools=openai_tools(),
            resolve_tool=_resolve,
            status_busy="生成中（可查阅 API 卡片）…",
            status_after="已查 API，继续生成…",
            status_call_prefix="查阅 API",
        )
    except Exception as e:
        msg = str(e).lower()
        if "tool" not in msg and "function" not in msg:
            raise
        if on_status:
            try:
                on_status(f"该提供商不支持工具调用，改为纯文本生成…（{e}）")
            except Exception:
                pass
        return await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=prompt + "\n(tool calling unavailable; use the contracts in this prompt)\n",
            on_partial=on_partial,
            max_tokens=max_tokens,
        )


async def call_llm_with_revise_tools(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    on_status=None,
    max_tokens: Optional[int] = None,
    allow_login: bool = False,
    max_tool_rounds: int = 5,
    tool_ctx=None,
    tool_assist: Optional[dict] = None,
    use_text_tools: bool = True,
) -> tuple[str, int, int]:
    """修订工具：主模型原生 tools → 辅助模型（如千问）代查 → 主模型文本伪工具。"""
    from backend.script_generator.revise_tools import (
        ReviseToolContext,
        claude_revise_tools,
        dispatch_revise_tool,
        openai_revise_tools,
        revise_text_tools_hint,
        revise_tools_hint,
    )

    ctx = tool_ctx if isinstance(tool_ctx, ReviseToolContext) else ReviseToolContext()
    total_in = total_out = 0
    work_messages = list(messages)
    prompt = (system_prompt or "") + revise_tools_hint()
    assist_notes = ""

    # 主模型无原生 tools 时：先让辅助模型（千问等）代跑工具，把笔记注入主模型
    if (
        not _provider_supports_tools(provider)
        and isinstance(tool_assist, dict)
        and str(tool_assist.get("api_key") or "").strip()
        and str(tool_assist.get("model") or "").strip()
    ):
        notes, nin, nout = await _revise_tool_assist_gather(
            tool_assist=tool_assist,
            messages=work_messages,
            system_prompt=system_prompt or "",
            tool_ctx=ctx,
            on_status=on_status,
            max_tokens=min(max_tokens or 4096, 4096) if max_tokens else 4096,
            max_tool_rounds=min(max_tool_rounds, 4),
            allow_login=allow_login,
        )
        total_in += nin
        total_out += nout
        assist_notes = notes or ""
        if assist_notes:
            inj = (
                "## Tool-assist notes (from aux model; trust these facts)\n"
                f"{assist_notes}\n\n"
                "Now produce <<<SUMMARY>>> then <<<FUNCS>>> for TARGET UNITS. "
                "Do not invent images / APIs contradicting the notes.\n"
            )
            work_messages = _inject_text_into_last_user(work_messages, inj)
            if on_status:
                try:
                    on_status(
                        f"辅助工具完成（{tool_assist.get('provider')}/"
                        f"{tool_assist.get('model')}），主模型写补丁…"
                    )
                except Exception:
                    pass

    supports = _provider_supports_tools(provider)
    if supports:
        def _resolve(name, arguments) -> str:
            return dispatch_revise_tool(
                name, arguments, ctx, allow_login=allow_login,
            )

        api = _provider_api(provider)
        try:
            if api == "claude":
                text, tin, tout = await _claude_tools_loop(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=work_messages,
                    system_prompt=prompt,
                    on_partial=on_partial,
                    on_status=on_status,
                    max_tokens=max_tokens,
                    max_tool_rounds=max_tool_rounds,
                    tools=claude_revise_tools(),
                    resolve_tool=_resolve,
                    status_busy="局部修订（可先查函数/日志）…",
                    status_after="工具已返回，继续修订…",
                    status_call_prefix="修订工具",
                )
            else:
                text, tin, tout = await _openai_tools_loop(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=work_messages,
                    system_prompt=prompt,
                    on_partial=on_partial,
                    on_status=on_status,
                    max_tokens=max_tokens,
                    max_tool_rounds=max_tool_rounds,
                    tools=openai_revise_tools(),
                    resolve_tool=_resolve,
                    status_busy="局部修订（可先查函数/日志）…",
                    status_after="工具已返回，继续修订…",
                    status_call_prefix="修订工具",
                )
            return text, total_in + tin, total_out + tout
        except Exception as e:
            msg = str(e).lower()
            if "tool" not in msg and "function" not in msg:
                raise
            if on_status:
                try:
                    on_status(f"原生工具失败，改文本伪工具…（{e}）")
                except Exception:
                    pass
            use_text_tools = True

    # 已有辅助笔记时：主模型直接写补丁，省一轮伪工具
    if assist_notes and not supports:
        text, tin, tout = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=work_messages,
            system_prompt=prompt,
            on_partial=on_partial,
            max_tokens=max_tokens,
        )
        return text, total_in + tin, total_out + tout

    if use_text_tools:
        text, tin, tout = await _text_revise_tools_loop(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=work_messages,
            system_prompt=prompt + revise_text_tools_hint(),
            on_partial=on_partial,
            on_status=on_status,
            max_tokens=max_tokens,
            max_tool_rounds=max_tool_rounds,
            tool_ctx=ctx,
            allow_login=allow_login,
        )
        return text, total_in + tin, total_out + tout

    text, tin, tout = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=work_messages,
        system_prompt=prompt,
        on_partial=on_partial,
        max_tokens=max_tokens,
    )
    return text, total_in + tin, total_out + tout


def _inject_text_into_last_user(messages: list[dict], extra: str) -> list[dict]:
    if not messages:
        return [{
            "role": "user",
            "content": [{"type": "text", "text": extra}],
        }]
    out = [dict(m) for m in messages]
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, list):
        parts = list(content)
        parts.append({"type": "text", "text": extra})
        last["content"] = parts
    elif isinstance(content, str):
        last["content"] = content + "\n\n" + extra
    else:
        last["content"] = [{"type": "text", "text": extra}]
    out[-1] = last
    return out


async def _revise_tool_assist_gather(
    *,
    tool_assist: dict,
    messages: list[dict],
    system_prompt: str,
    tool_ctx,
    on_status=None,
    max_tokens: Optional[int] = 4096,
    max_tool_rounds: int = 4,
    allow_login: bool = False,
) -> tuple[str, int, int]:
    """辅助模型只负责查工具，产出简短 NOTES，不写最终代码。"""
    from backend.script_generator.revise_tools import (
        revise_text_tools_hint,
    )

    a_provider = str(tool_assist.get("provider") or "").strip()
    a_key = str(tool_assist.get("api_key") or "").strip()
    a_model = str(tool_assist.get("model") or "").strip()
    a_endpoint = tool_assist.get("api_endpoint")
    if a_endpoint is not None:
        a_endpoint = str(a_endpoint).strip() or None
    if not (a_provider and a_key and a_model):
        return "", 0, 0

    if on_status:
        try:
            on_status(f"辅助模型查工具（{a_provider}/{a_model}）…")
        except Exception:
            pass

    gather_sys = (
        "You are a revise investigator for Minashigo automation scripts.\n"
        "Use tools to inspect code / logs / images. Do NOT output full script patches.\n"
        "Tools: get_unit, list_images, diagnose_log, validate_code, lookup_api.\n"
        "When done, output ONLY:\n"
        "<<<NOTES>>>\n"
        "Chinese bullet notes: root cause, which units to edit, image names allowed, "
        "pitfalls. Be concrete.\n"
        "<<<END_NOTES>>>\n"
    )
    # 压缩主 user：去掉超长代码块以外的约束仍保留
    gather_user = (
        "Investigate the revise request below. Call tools as needed, then <<<NOTES>>>.\n\n"
    )
    # 从原 messages 抽最后一条 user 文本
    last = messages[-1] if messages else {}
    content = last.get("content")
    if isinstance(content, list):
        texts = [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        gather_user += "\n".join(texts)[:14000]
    elif isinstance(content, str):
        gather_user += content[:14000]
    else:
        gather_user += (system_prompt or "")[:4000]

    gather_msgs = [{
        "role": "user",
        "content": [{"type": "text", "text": gather_user}],
    }]

    if _provider_supports_tools(a_provider):
        from backend.script_generator.revise_tools import (
            claude_revise_tools,
            dispatch_revise_tool,
            openai_revise_tools,
        )

        def _resolve(name, arguments) -> str:
            return dispatch_revise_tool(
                name, arguments, tool_ctx, allow_login=allow_login,
            )

        api = _provider_api(a_provider)
        try:
            if api == "claude":
                raw, tin, tout = await _claude_tools_loop(
                    provider=a_provider,
                    api_key=a_key,
                    model=a_model,
                    api_endpoint=a_endpoint,
                    messages=gather_msgs,
                    system_prompt=gather_sys,
                    on_partial=None,
                    on_status=on_status,
                    max_tokens=max_tokens,
                    max_tool_rounds=max_tool_rounds,
                    tools=claude_revise_tools(),
                    resolve_tool=_resolve,
                    status_busy="辅助查工具…",
                    status_after="辅助继续查…",
                    status_call_prefix="辅助工具",
                )
            else:
                raw, tin, tout = await _openai_tools_loop(
                    provider=a_provider,
                    api_key=a_key,
                    model=a_model,
                    api_endpoint=a_endpoint,
                    messages=gather_msgs,
                    system_prompt=gather_sys,
                    on_partial=None,
                    on_status=on_status,
                    max_tokens=max_tokens,
                    max_tool_rounds=max_tool_rounds,
                    tools=openai_revise_tools(),
                    resolve_tool=_resolve,
                    status_busy="辅助查工具…",
                    status_after="辅助继续查…",
                    status_call_prefix="辅助工具",
                )
        except Exception as e:
            if on_status:
                try:
                    on_status(f"辅助原生工具失败，改文本协议…（{e}）")
                except Exception:
                    pass
            raw, tin, tout = await _text_revise_tools_loop(
                provider=a_provider,
                api_key=a_key,
                model=a_model,
                api_endpoint=a_endpoint,
                messages=gather_msgs,
                system_prompt=gather_sys + revise_text_tools_hint(),
                on_partial=None,
                on_status=on_status,
                max_tokens=max_tokens,
                max_tool_rounds=max_tool_rounds,
                tool_ctx=tool_ctx,
                allow_login=allow_login,
                final_markers=("<<<NOTES>>>", "<<<END_NOTES>>>"),
            )
    else:
        raw, tin, tout = await _text_revise_tools_loop(
            provider=a_provider,
            api_key=a_key,
            model=a_model,
            api_endpoint=a_endpoint,
            messages=gather_msgs,
            system_prompt=gather_sys + revise_text_tools_hint(),
            on_partial=None,
            on_status=on_status,
            max_tokens=max_tokens,
            max_tool_rounds=max_tool_rounds,
            tool_ctx=tool_ctx,
            allow_login=allow_login,
            final_markers=("<<<NOTES>>>", "<<<END_NOTES>>>"),
        )

    notes = ""
    if "<<<NOTES>>>" in (raw or ""):
        notes = (raw or "").split("<<<NOTES>>>", 1)[1]
        if "<<<END_NOTES>>>" in notes:
            notes = notes.split("<<<END_NOTES>>>", 1)[0]
        notes = notes.strip()
    elif raw:
        # 辅助模型可能直接写了 FUNCS；截一段当笔记
        notes = (raw or "").strip()[:4000]
    return notes, tin, tout


async def _text_revise_tools_loop(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str,
    on_partial=None,
    on_status=None,
    max_tokens: Optional[int] = None,
    max_tool_rounds: int = 5,
    tool_ctx=None,
    allow_login: bool = False,
    final_markers: tuple[str, ...] = ("<<<FUNCS>>>", "<<<CODE>>>", "<<<NOTES>>>"),
) -> tuple[str, int, int]:
    """纯文本伪工具循环（DeepSeek / 无 tools 提供商）。"""
    from backend.script_generator.revise_tools import (
        ReviseToolContext,
        dispatch_revise_tool,
        format_text_tool_results,
        parse_text_tool_calls,
        strip_text_tool_blocks,
    )

    ctx = tool_ctx if isinstance(tool_ctx, ReviseToolContext) else ReviseToolContext()
    work = list(messages)
    total_in = total_out = 0
    last_text = ""
    rounds = max(1, int(max_tool_rounds))

    for i in range(rounds):
        if on_status:
            try:
                on_status(
                    "文本工具修订…" if i == 0 else f"文本工具第 {i + 1} 轮…"
                )
            except Exception:
                pass
        raw, rin, rout = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=work,
            system_prompt=system_prompt,
            on_partial=on_partial if i == rounds - 1 else None,
            max_tokens=max_tokens,
        )
        total_in += rin
        total_out += rout
        last_text = raw or ""
        calls = parse_text_tool_calls(last_text)
        # 已有最终标记且无工具 → 结束
        if any(m in last_text for m in final_markers) and not calls:
            if on_partial and i < rounds - 1:
                on_partial(last_text)
            return last_text, total_in, total_out
        if not calls:
            if on_partial:
                on_partial(last_text)
            return last_text, total_in, total_out

        results: list[tuple[str, str]] = []
        for name, arguments in calls[:6]:
            result = dispatch_revise_tool(
                name, arguments, ctx, allow_login=allow_login,
            )
            results.append((name, result))
            if on_status:
                try:
                    on_status(f"文本工具：{name}")
                except Exception:
                    pass

        # 对话续写：assistant 原文 + user 工具结果
        work.append({
            "role": "assistant",
            "content": last_text,
        })
        work.append({
            "role": "user",
            "content": [{
                "type": "text",
                "text": format_text_tool_results(results),
            }],
        })

    if on_partial and last_text:
        on_partial(last_text)
    # 去掉残余 TOOL 块，尽量留下可解析正文
    cleaned = strip_text_tool_blocks(last_text)
    return cleaned or last_text, total_in, total_out


async def _openai_tools_loop(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str,
    on_partial,
    on_status,
    max_tokens: Optional[int],
    max_tool_rounds: int,
    tools: list[dict],
    resolve_tool,
    status_busy: str = "生成中…",
    status_after: str = "工具已返回，继续…",
    status_call_prefix: str = "工具",
    allow_login: bool = False,
) -> tuple[str, int, int]:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("需要安装 openai 包: pip install openai")

    from httpx import Timeout as HttpxTimeout

    info = _provider_info(provider)
    extra_headers = info.get("extra_headers") if isinstance(info.get("extra_headers"), dict) else None
    endpoint = (api_endpoint or "").strip() or (info.get("default_endpoint") or "").strip() or None
    mt = resolve_max_tokens(max_tokens)
    client_kwargs: dict = {"api_key": api_key, "timeout": HttpxTimeout(API_TIMEOUT)}
    if endpoint:
        client_kwargs["base_url"] = endpoint
    if extra_headers:
        client_kwargs["default_headers"] = extra_headers
    client = AsyncOpenAI(**client_kwargs)

    work = _flatten_openai_messages(list(messages))
    total_in = total_out = 0
    rounds = max(1, int(max_tool_rounds) + 1)
    text = ""
    for i in range(rounds):
        last = i == rounds - 1
        create_kwargs: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}] + work,
        }
        if mt is not None:
            create_kwargs["max_tokens"] = mt
        if provider == "deepseek":
            create_kwargs["extra_body"] = _deepseek_extra_body(model)
        if not last:
            create_kwargs["tools"] = tools
        if on_status and not last:
            try:
                on_status(status_busy if i == 0 else status_after)
            except Exception:
                pass
        response = await client.chat.completions.create(**create_kwargs)
        usage = response.usage
        if usage:
            total_in += usage.prompt_tokens or 0
            total_out += usage.completion_tokens or 0
        choice = response.choices[0]
        message = choice.message
        text = message.content or ""
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if not tool_calls:
            if on_partial and text:
                on_partial(text)
            return text, total_in, total_out
        assistant_msg: dict = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ],
        }
        work.append(assistant_msg)
        for tc in tool_calls:
            result = resolve_tool(tc.function.name, tc.function.arguments)
            work.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
            if on_status:
                try:
                    on_status(f"{status_call_prefix}：{tc.function.name}")
                except Exception:
                    pass
    if on_partial and text:
        on_partial(text)
    if not text:
        raise RuntimeError("模型在工具调用后未返回内容，请重试")
    return text, total_in, total_out


def _claude_block_to_dict(block) -> dict:
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "") or ""}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", None) or {},
        }
    if isinstance(block, dict):
        return block
    return {"type": "text", "text": str(block)}


async def _claude_tools_loop(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str,
    on_partial,
    on_status,
    max_tokens: Optional[int],
    max_tool_rounds: int,
    tools: list[dict],
    resolve_tool,
    status_busy: str = "生成中…",
    status_after: str = "工具已返回，继续…",
    status_call_prefix: str = "工具",
    allow_login: bool = False,
) -> tuple[str, int, int]:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("需要安装 anthropic 包: pip install anthropic")

    info = _provider_info(provider)
    endpoint = (api_endpoint or "").strip() or (info.get("default_endpoint") or "").strip() or None
    mt = resolve_max_tokens(max_tokens)
    client_kwargs: dict = {"api_key": api_key, "timeout": API_TIMEOUT}
    if endpoint:
        client_kwargs["base_url"] = endpoint
    client = anthropic.AsyncAnthropic(**client_kwargs)
    claude_mt = mt if mt is not None else _UNLIMITED_MAX_TOKENS

    work = list(messages)
    total_in = total_out = 0
    rounds = max(1, int(max_tool_rounds) + 1)
    text = ""
    for i in range(rounds):
        last = i == rounds - 1
        kwargs: dict = {
            "model": model,
            "max_tokens": claude_mt,
            "system": system_prompt,
            "messages": work,
        }
        if not last:
            kwargs["tools"] = tools
        if on_status and not last:
            try:
                on_status(status_busy if i == 0 else status_after)
            except Exception:
                pass
        response = await client.messages.create(**kwargs)
        usage = response.usage
        if usage:
            total_in += getattr(usage, "input_tokens", 0) or 0
            total_out += getattr(usage, "output_tokens", 0) or 0
        blocks = list(response.content or [])
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        texts = [getattr(b, "text", "") or "" for b in blocks if getattr(b, "type", None) == "text"]
        text = "".join(texts)
        if not tool_uses:
            if on_partial and text:
                on_partial(text)
            if not text:
                raise RuntimeError("Claude 返回了空内容，请重试")
            return text, total_in, total_out
        work.append({"role": "assistant", "content": [_claude_block_to_dict(b) for b in blocks]})
        results = []
        for tu in tool_uses:
            result = resolve_tool(
                getattr(tu, "name", ""), getattr(tu, "input", None),
            )
            results.append({
                "type": "tool_result",
                "tool_use_id": getattr(tu, "id", ""),
                "content": result,
            })
            if on_status:
                try:
                    on_status(f"{status_call_prefix}：{getattr(tu, 'name', '')}")
                except Exception:
                    pass
        work.append({"role": "user", "content": results})
    if on_partial and text:
        on_partial(text)
    if not text:
        raise RuntimeError("Claude 在工具调用后未返回内容，请重试")
    return text, total_in, total_out


async def test_connection(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """
    发送一条极短消息验证 API 连通性。
    返回 {ok, latency_ms, reply, error, input_tokens, output_tokens}。
    """
    import time

    if not (api_key or "").strip():
        return {
            "ok": False,
            "latency_ms": 0,
            "reply": "",
            "error": "API Key 为空",
            "input_tokens": 0,
            "output_tokens": 0,
        }
    if not (model or "").strip():
        return {
            "ok": False,
            "latency_ms": 0,
            "reply": "",
            "error": "模型名为空",
            "input_tokens": 0,
            "output_tokens": 0,
        }

    messages = [{
        "role": "user",
        "content": [{"type": "text", "text": "Reply with exactly: OK"}],
    }]
    system_prompt = "You are a connectivity probe. Reply with exactly OK."

    t0 = time.perf_counter()
    try:
        result = await call_llm(
            provider=provider,
            api_key=api_key.strip(),
            model=model.strip(),
            api_endpoint=api_endpoint,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens if max_tokens is not None else 256,
        )
        if not isinstance(result, (tuple, list)) or len(result) < 3:
            raise RuntimeError(
                f"LLM 调用返回异常（期望 text/tokens，实际 {type(result).__name__}={result!r}）"
            )
        text, inp, out = result[0], result[1], result[2]
        latency_ms = int((time.perf_counter() - t0) * 1000)
        reply = (text or "").strip()
        if not reply:
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "reply": "",
                "error": "API 返回空内容",
                "input_tokens": inp,
                "output_tokens": out,
            }
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "reply": reply[:200],
            "error": "",
            "input_tokens": inp,
            "output_tokens": out,
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "reply": "",
            "error": str(e),
            "input_tokens": 0,
            "output_tokens": 0,
        }


async def _generate_script_legacy(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    image_paths: list[Path],
    source_dir: str = "",
    send_images: bool = True,
    compress_images: bool = False,
    on_partial=None,
    max_tokens: Optional[int] = None,
    free_mode: Optional[bool] = None,
) -> tuple[str, int, int]:
    free = is_codegen_free_mode(free_mode)
    prompt = _build_system_prompt(
        source_dir=source_dir,
        explanation=explanation_text,
        free_mode=free,
    )
    messages = _build_messages(
        explanation_text,
        image_paths,
        provider=provider,
        send_images=send_images,
        compress_images=compress_images,
        lean=free,
        source_dir=source_dir,
    )
    from backend.script_generator.api_catalog import allow_login_from_explanation

    raw, inp_tok, out_tok = await call_llm_with_tools(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=messages,
        system_prompt=prompt,
        on_partial=on_partial,
        max_tokens=max_tokens,
        allow_login=allow_login_from_explanation(explanation_text or ""),
    )
    code = enforce_img_dir(strip_code_fences(raw), source_dir)
    errors = validate_generated_code(
        code, source_dir=source_dir, image_paths=image_paths,
        explanation=explanation_text or "",
        free_mode=free,
    )
    if errors:
        raise RuntimeError(f"生成的脚本校验失败: {errors[0]}，请点「生成脚本」重试")
    return code, inp_tok, out_tok


async def generate_script(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    image_paths: list[Path],
    source_dir: str = "",
    send_images: bool = True,
    compress_images: bool = False,
    vision_assist: Optional[dict] = None,
    on_partial=None,
    on_status=None,
    on_artifact=None,
    max_tokens: Optional[int] = None,
    free_mode: Optional[bool] = None,
) -> tuple[str, int, int]:
    """
    调用 LLM 生成脚本。返回 (代码, 输入tokens, 输出tokens)。

    vision_assist: 可选辅助识图配置
      {provider, api_key, model, api_endpoint, compress_images?}
      启用后先识图成文字目录，再交给主模型（主模型不再直接收图）。
    free_mode: True 时生成少约束（无 Rules/few-shot/plan/IR）；成品校验加严 + 自动修复。
    """
    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    use_graph = bool(defaults.get("use_langgraph", True))
    free = is_codegen_free_mode(free_mode)
    mt = resolve_max_tokens(max_tokens if max_tokens is not None else defaults.get("max_tokens"))

    extra_in = extra_out = 0
    send_images_main = bool(send_images)
    # 识图/传图一律以 source_dir 递归全量为准（含子目录），避免 UI 只导入了顶层
    if (source_dir or "").strip():
        disk_paths = list_source_image_paths(source_dir)
        if disk_paths:
            image_paths = disk_paths
    expl = _prepare_explanation(
        explanation_text or "",
        on_artifact=on_artifact,
        on_status=on_status,
        lean=free,
    )
    if free and on_status:
        on_status(
            "自由模式：生成少约束（无 Rules/plan/IR）；仅注入结构范式 few-shot；"
            "成品校验加严 + 可自动修复"
        )
    if vision_assist and send_images and image_paths:
        v_provider = str(vision_assist.get("provider") or "").strip()
        v_key = str(vision_assist.get("api_key") or "").strip()
        v_model = str(vision_assist.get("model") or "").strip()
        v_endpoint = vision_assist.get("api_endpoint") or None
        if not v_provider or not v_key or not v_model:
            raise RuntimeError("已选择辅助识图，但识图 API（提供商 / Key / 模型）未配置完整")
        catalog, vin, vout = await describe_images_catalog(
            provider=v_provider,
            api_key=v_key,
            model=v_model,
            api_endpoint=v_endpoint,
            image_paths=image_paths,
            source_dir=source_dir,
            explanation_text=explanation_text or "",
            refresh_vision=bool(vision_assist.get("refresh_vision")),
            compress_images=bool(
                vision_assist.get("compress_images", compress_images)
            ),
            on_status=on_status,
            on_artifact=on_artifact,
        )
        extra_in += vin
        extra_out += vout
        if catalog.strip():
            expl = (
                f"{expl.rstrip()}\n\n"
                "## 参考图片识图结果（由辅助识图模型生成，文件名须与 _img() 一致）\n"
                f"{catalog.strip()}\n"
            )
        send_images_main = False

    if use_graph:
        try:
            from backend.script_generator.graph import run_script_gen_graph
        except ImportError as e:
            import sys
            use_graph = False
            detail = f"当前解释器: {sys.executable}；原因: {e}"
            print(f"[ScriptGenerator] LangGraph 导入失败: {detail}")
            if on_status:
                on_status(f"LangGraph 不可用，回退单次生成…（{detail}）")

    if use_graph:
        code, inp, out = await run_script_gen_graph(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            explanation_text=expl,
            image_paths=image_paths,
            source_dir=source_dir,
            send_images=send_images_main,
            compress_images=compress_images,
            enable_plan=False if free else bool(defaults.get("enable_plan", True)),
            max_fix_retries=(
                int(defaults.get("codegen_free_max_fix_retries", defaults.get("max_fix_retries", 3)))
                if free
                else int(defaults.get("max_fix_retries", 2))
            ),
            max_tokens=mt,
            free_mode=free,
            on_partial=on_partial,
            on_status=on_status,
            on_artifact=on_artifact,
        )
        _emit_generate_chat_session(
            source_dir=source_dir,
            explanation=expl,
            code=code,
            on_artifact=on_artifact,
            on_status=on_status,
        )
        return code, inp + extra_in, out + extra_out

    code, inp, out = await _generate_script_legacy(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        explanation_text=expl,
        image_paths=image_paths,
        source_dir=source_dir,
        send_images=send_images_main,
        compress_images=compress_images,
        on_partial=on_partial,
        max_tokens=mt,
        free_mode=free,
    )
    _emit_generate_chat_session(
        source_dir=source_dir,
        explanation=expl,
        code=code,
        on_artifact=on_artifact,
        on_status=on_status,
    )
    # legacy 单次生成：同样发统一校验上下文（无结构化 plan）
    if on_artifact:
        try:
            import json as _json

            on_artifact(
                "gen_ctx",
                _json.dumps(
                    {
                        "family": "legacy",
                        "plan_struct": {},
                        "free_mode": bool(free),
                        "source_dir": source_dir or "",
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
    return code, inp + extra_in, out + extra_out


def _emit_generate_chat_session(
    *,
    source_dir: str,
    explanation: str,
    code: str,
    on_artifact=None,
    on_status=None,
) -> None:
    """生成成功后归档同会话 messages，供修订续写。"""
    try:
        from backend.script_generator.chat_session import (
            build_generate_session,
            dumps_session,
        )
        system = _build_system_prompt(
            source_dir=source_dir or "",
            explanation=explanation or "",
        )
        imgs = list_source_image_names(source_dir or "")
        # 把素材白名单钉进 system，续写时也不丢
        system = system.rstrip() + "\n\n" + allowed_images_block(
            source_dir or "", names=imgs, explanation=explanation or "",
        )
        user_text = (explanation or "").rstrip()
        if imgs:
            user_text += "\n\n" + allowed_images_block(
                source_dir or "", names=imgs, explanation=explanation or "",
            )
        session = build_generate_session(
            system=system,
            user_text=user_text,
            assistant_code=code or "",
            allowed_images=imgs,
        )
        if on_artifact:
            on_artifact("chat_session", dumps_session(session))
        if on_status:
            on_status(f"已保存生成会话（{len(imgs)} 张素材白名单）")
    except Exception as e:
        print(f"[ScriptGenerator] chat_session 保存失败: {e}")


def _norm_code(code: str) -> str:
    lines = []
    for ln in (code or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    return "\n".join(lines)


def _focus_trial_log(log: str, limit: int = 6000) -> str:
    raw = (log or "").strip()
    if not raw:
        return "(none)"
    lines = raw.splitlines()
    keep = [
        ln for ln in lines
        if re.search(
            r"ERROR|异常|NameError|Traceback|\[状态\]|note_state|"
            r"未覆盖|卡住|失败|__exit__|房间|主界面",
            ln,
            re.I,
        )
    ]
    tail = lines[-80:]
    seen: set[str] = set()
    out: list[str] = []
    for ln in keep + ["--- tail ---"] + tail:
        if ln != "--- tail ---" and ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    text = "\n".join(out)
    if len(text) > limit:
        return text[-limit:]
    return text


def _local_uncovered_feedback(
    items: list[str], old_code: str, new_code: str
) -> list[str]:
    """模型审查之外的硬检查：没改代码 / 导航态误 __exit__ / 日志没加。"""
    if not items:
        return []
    old, new = old_code or "", new_code or ""
    if _norm_code(old) == _norm_code(new):
        return list(items)
    miss: list[str] = []
    home_bad = False
    try:
        tree = ast.parse(new)
        home_bad = bool(_home_nav_exit_errors(tree))
    except SyntaxError:
        return list(items)
    for item in items:
        if re.search(r"返回主界面|禁止.*__exit__", item) and home_bad:
            miss.append(item)
            continue
        if re.search(r"日志|script_log|哪一步|卡在哪", item, re.I):
            if new.count("script_log") <= old.count("script_log"):
                miss.append(item)
    seen: set[str] = set()
    out: list[str] = []
    for m in miss:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def offline_syntax_asset_review(
    code: str,
    *,
    source_dir: str = "",
    explanation: str = "",
) -> dict:
    """离线审查：仅语法 + 素材引用（无 LLM、不评反馈语义覆盖）。

    检查：compile/AST、幻觉图、介绍未点名盘内图、（有 source_dir 时）文件是否存在。
    """
    covered: list[str] = []
    uncovered: list[str] = []
    raw = code or ""
    if not raw.strip():
        return {
            "ok": False,
            "covered": [],
            "uncovered": ["生成结果为空"],
            "notes": "离线审查：语法 + 素材引用",
            "mode": "offline",
        }
    try:
        compile(raw, "<revise-review>", "exec")
        covered.append("语法：compile 通过")
    except SyntaxError as e:
        uncovered.append(f"语法错误: {e.msg}（第 {e.lineno} 行）")
        return {
            "ok": False,
            "covered": covered,
            "uncovered": uncovered,
            "notes": "离线审查：语法未过，未继续查素材",
            "mode": "offline",
        }
    try:
        tree = ast.parse(raw)
    except SyntaxError as e:
        uncovered.append(f"AST 解析失败: {e.msg}（第 {e.lineno} 行）")
        return {
            "ok": False,
            "covered": covered,
            "uncovered": uncovered,
            "notes": "离线审查：AST 失败",
            "mode": "offline",
        }

    src = (source_dir or "").strip()
    if src:
        for msg in _hallucinated_image_errors(tree, src, explanation or ""):
            uncovered.append(msg)
        for msg in _intro_unmentioned_image_errors(tree, src, explanation or ""):
            uncovered.append(msg)
        for msg in _image_file_existence_errors(tree, src):
            uncovered.append(msg)
        if not any(
            ("幻觉" in u) or ("介绍未点名" in u) or ("不存在" in u) or ("缺失" in u)
            for u in uncovered
        ):
            covered.append("素材引用：幻觉图 / 介绍过滤 / 文件存在检查通过")
    else:
        covered.append("素材引用：未指定 source_dir，跳过路径/存在性检查")

    for msg in _none_browser_image_arg_errors(tree):
        uncovered.append(msg)

    return {
        "ok": not uncovered,
        "covered": covered,
        "uncovered": uncovered,
        "notes": "离线审查：仅语法 + 素材引用（暂不评反馈语义覆盖）",
        "mode": "offline",
    }


def revise_feedback_review_mode() -> str:
    """offline = 语法+素材离线；llm = 原反馈语义审查。"""
    try:
        mode = str(
            (_load_config().get("defaults") or {}).get(
                "revise_feedback_review_mode", "offline"
            )
            or "offline"
        ).strip().lower()
    except Exception:
        mode = "offline"
    if mode in ("llm", "feedback", "semantic"):
        return "llm"
    return "offline"


async def revise_script(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    explanation_text: str,
    current_code: str,
    user_feedback: str,
    source_dir: str = "",
    trial_log: str = "",
    stop_frame_path: str = "",
    vision_assist: Optional[dict] = None,
    tool_assist: Optional[dict] = None,
    prior_summary: str = "",
    prior_diagnosis: str = "",
    chat_session: Optional[dict] = None,
    on_partial=None,
    on_status=None,
    on_artifact=None,
    max_tokens: Optional[int] = None,
    free_mode: Optional[bool] = None,
) -> tuple[str, str, int, int, dict]:
    """根据用户试运行反馈修订脚本，并再审查是否覆盖反馈。

    返回 (代码, 修改摘要含审查结论, 输入tokens, 输出tokens, meta)。
    meta 含 trial_blocked（硬校验未过不可试跑）、validation_errors、review_ok。
    """
    cfg = _load_config()
    defaults = cfg.get("defaults", {})
    # 修订沿用生成时的自由模式标记，使本地 patch / 校验与生成端策略一致
    free = is_codegen_free_mode(free_mode)
    mt = resolve_max_tokens(max_tokens if max_tokens is not None else defaults.get("max_tokens"))
    methods = ", ".join(sorted(ALLOWED_BROWSER_METHODS))
    img_dir_hint = build_img_dir_line(source_dir) if source_dir else ""
    continue_chat = bool(defaults.get("revise_continue_chat", True)) and bool(chat_session)

    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    def _artifact(kind: str, payload: str) -> None:
        if on_artifact:
            try:
                on_artifact(kind, payload)
            except Exception:
                pass

    original_code = (current_code or "").strip()
    feedback_raw = (user_feedback or "").strip()
    from backend.script_generator.feedback_opt import revise_checklist
    from backend.script_generator.api_catalog import api_contracts_block

    feedback_items = revise_checklist(feedback_raw, explanation_text or "")
    if not feedback_items:
        feedback_items = _split_feedback_items(feedback_raw) or [feedback_raw]
    feedback_block = "Address ALL of the following items (do not skip any):\n" + "\n".join(
        f"{i}. {item}" for i, item in enumerate(feedback_items, 1)
    )

    log_block = _focus_trial_log(trial_log)
    api_block = api_contracts_block(explanation=explanation_text or "")
    images_block = allowed_images_block(
        source_dir or "", explanation=explanation_text or "",
    )

    from backend.script_generator.diagnose import (
        diagnose_trial_failure,
        format_diagnosis_block,
    )

    diagnosis, din, dout = await diagnose_trial_failure(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        code=original_code,
        trial_log=trial_log,
        feedback=feedback_raw,
        explanation=explanation_text or "",
        stop_frame_path=stop_frame_path or "",
        vision_assist=vision_assist,
        max_tokens=min(mt or 2048, 2048) if mt else 2048,
        on_status=_status,
        on_artifact=_artifact,
    )
    inp = din
    out = dout
    diagnosis_block = format_diagnosis_block(diagnosis)
    if diagnosis.must_fix:
        seen_fb: set[str] = set(feedback_items)
        for item in diagnosis.must_fix:
            # 方案 A：降级项不进硬约束编号清单（仍可见于 diagnosis_block）
            if "请人工确认" in item or "已降级" in item:
                continue
            tagged = f"【诊断】{item}"
            if tagged not in seen_fb:
                seen_fb.add(tagged)
                feedback_items.append(tagged)
        feedback_block = "Address ALL of the following items (do not skip any):\n" + "\n".join(
            f"{i}. {item}" for i, item in enumerate(feedback_items, 1)
        )

    # 方案 G：匹配/阈值类反馈时提示可标定（不自动跑）
    try:
        from backend.script_generator.threshold_calibrate import should_offer_calibration
        if should_offer_calibration(feedback_raw, trial_log or ""):
            _artifact(
                "stage",
                "calibrate|hint|可标定阈值|"
                "反馈疑似点不到/误匹配；修订不自动扫阈值，"
                "可在脚本优化中调用 calibrate_image_threshold 钉死",
            )
    except Exception:
        pass

    # 方案 C：修订仍受执行清单约束
    checklist_block = ""
    try:
        from backend.script_generator.execution_checklist import (
            extract_execution_checklist,
            format_checklist_for_prompt,
        )
        ck_items = extract_execution_checklist(explanation_text or "")
        checklist_block = format_checklist_for_prompt(ck_items)
    except Exception:
        checklist_block = ""

    _status("根据反馈修订脚本…")
    _artifact(
        "stage",
        "revise|running|根据反馈修订|"
        + "\n".join(f"- {it}" for it in feedback_items[:8]),
    )

    expl_for_model = _prepare_explanation(
        explanation_text or "",
        on_artifact=_artifact,
        on_status=_status,
    )
    system = (
        "You revise Minashigo automation Python scripts based on user trial-run feedback.\n"
        "Output format (STRICT):\n"
        "<<<SUMMARY>>>\n"
        "Write a short Chinese checklist for the user (3-8 lines):\n"
        "- For EACH numbered constraint: 已改 / 未改, and one short clause naming the "
        "function/state you changed. Do not claim 已改 unless the Python actually changed.\n"
        "- If you inferred something the user did not say, mark it as 额外理解: ...\n"
        "<<<CODE>>>\n"
        "Then the FULL corrected Python source only (no markdown fences).\n"
        f"Allowed browser methods ONLY: {methods}. Do NOT invent others.\n"
        "Keep FSM shape: async def do_work(browser: UserBrowser | UserWindow) with type annotation, "
        "未知 recovery, STATES or TASK*_STATES, "
        "unknown_state must return business state names on id match, "
        "scene hub states mid-task MUST perform the task entry click "
        "(never idle with only match + return None).\n"
        "CRITICAL anti-patterns (fix if present):\n"
        "- Never share one hub-scene handler that clicks another task's entry images.\n"
        "- Each task's handlers must use that task's image stems from the introduction.\n"
        "- Nav helper success → return business state name (本步骤结束), never '__exit__'.\n"
        "- Do not use function attributes (handler.claim_count) for mutable state.\n"
        "- Popup/confirm loops and retry limits must follow the introduction verbatim.\n"
        "- Do not confuse 本步骤结束 (nav helper done) with 本任务完成 (__exit__).\n"
        "- Frame freshness is handled by runtime (invalidate after click/b_sleep; "
        "match auto-ensures). Optional: await browser.request_fps(hz) for continuous observe.\n"
        "No Chinese punctuation outside string literals and comments.\n"
        "You MUST change real control flow for EVERY checklist item. Adding comments "
        "or one extra script_log is not enough if the state transitions are wrong.\n"
        + (f"IMG_DIR MUST be exactly: {img_dir_hint}\n" if img_dir_hint else "")
        + api_block
        + "\n"
        + images_block
    )
    try:
        from backend.script_generator.authority import revise_authority_system_addendum
        system = system + revise_authority_system_addendum()
    except Exception:
        pass
    try:
        from backend.script_generator.architecture import architecture_prompt_block
        system = system + "\n" + architecture_prompt_block(explanation_text or "")
    except Exception:
        pass
    if checklist_block.strip():
        system = system + "\n" + checklist_block
    user = (
        "## Constraints to implement (highest priority PATCH; do not delete "
        "intro steps the feedback did not negate)\n"
        f"{feedback_block}\n\n"
    )
    if diagnosis_block:
        user += f"{diagnosis_block}\n\n"
    prior_sum = (prior_summary or "").strip()
    prior_diag = (prior_diagnosis or "").strip()
    if prior_sum or prior_diag:
        user += "## Prior revise context (do not regress these fixes)\n"
        if prior_sum:
            user += f"### Previous SUMMARY\n{prior_sum[:2500]}\n\n"
        if prior_diag:
            user += f"### Previous diagnosis\n{prior_diag[:2000]}\n\n"
    user += (
        "## Original script explanation (authoritative; wins over system Rules on conflict)\n"
        f"{expl_for_model}\n\n"
        "## User feedback (raw)\n"
        f"{feedback_raw}\n\n"
        "## Trial-run log (errors / states / tail)\n"
        f"{log_block}\n\n"
        "## Current code\n"
        f"```python\n{original_code}\n```\n\n"
        "Fix ALL numbered constraints. Respond with <<<SUMMARY>>> then <<<CODE>>>."
    )
    # 冷修订 user 也带白名单（与 system 双保险）
    user = images_block + "\n" + user

    use_surgical = bool(defaults.get("revise_surgical", True))
    use_revise_tools = bool(defaults.get("revise_tools", True))
    use_text_tools = bool(defaults.get("revise_text_tools", True))
    from backend.script_generator.revise_tools import resolve_revise_tool_assist
    resolved_tool_assist = resolve_revise_tool_assist(
        tool_assist=tool_assist,
        vision_assist=vision_assist,
        defaults=defaults,
    )
    summary = ""
    code: Optional[str] = None
    surgical_notes: list[str] = []
    revise_tool_calls: list[str] = []
    active_session = None
    cont_user = ""

    if use_surgical:
        try:
            from backend.script_generator.surgical_revise import (
                SURGICAL_SYSTEM_ADDENDUM,
                format_units_block,
                list_code_units,
                parse_surgical_output,
                select_target_names,
                splice_units,
            )
            targets = select_target_names(
                original_code, feedback_items or [feedback_raw], max_n=6,
            )
            units = list_code_units(original_code)
            if targets and units:
                _status("局部修订：" + ", ".join(targets))
                _artifact(
                    "stage",
                    "revise|running|局部修订|"
                    + "只改: " + ", ".join(targets),
                )
                surg_user = (
                    f"{images_block}\n"
                    "## Constraints to implement (highest priority)\n"
                    f"{feedback_block}\n\n"
                )
                if diagnosis_block:
                    surg_user += f"{diagnosis_block}\n\n"
                prior_sum = (prior_summary or "").strip()
                prior_diag = (prior_diagnosis or "").strip()
                if prior_sum or prior_diag:
                    surg_user += "## Prior revise context (do not regress)\n"
                    if prior_sum:
                        surg_user += f"### Previous SUMMARY\n{prior_sum[:2000]}\n\n"
                    if prior_diag:
                        surg_user += f"### Previous diagnosis\n{prior_diag[:1500]}\n\n"
                surg_user += (
                    f"## User feedback (raw)\n{feedback_raw}\n\n"
                    f"## Trial-run log (errors / states / tail)\n{log_block}\n\n"
                    f"## TARGET UNITS (edit ONLY these top-level names)\n"
                    + ", ".join(targets)
                    + "\n\n"
                    + format_units_block(units, targets)
                    + "\n\nRespond with <<<SUMMARY>>> then <<<FUNCS>>> (see system).\n"
                    "Prefer diagnose_log / get_unit / list_images before editing if unsure.\n"
                )
                surg_sys = system + "\n\n" + SURGICAL_SYSTEM_ADDENDUM
                msgs = [{
                    "role": "user",
                    "content": [{"type": "text", "text": surg_user}],
                }]
                if use_revise_tools:
                    from backend.script_generator.revise_tools import ReviseToolContext
                    tool_ctx = ReviseToolContext(
                        code=original_code,
                        source_dir=source_dir or "",
                        trial_log=trial_log or "",
                        feedback=feedback_raw,
                        explanation=explanation_text or "",
                        methods=methods,
                    )
                    raw_s, rin, rout = await call_llm_with_revise_tools(
                        provider=provider,
                        api_key=api_key,
                        model=model,
                        api_endpoint=api_endpoint,
                        messages=msgs,
                        system_prompt=surg_sys,
                        on_partial=on_partial,
                        on_status=_status,
                        max_tokens=mt,
                        tool_ctx=tool_ctx,
                        tool_assist=resolved_tool_assist,
                        use_text_tools=use_text_tools,
                    )
                    revise_tool_calls = list(tool_ctx.calls)
                else:
                    raw_s, rin, rout = await call_llm(
                        provider=provider,
                        api_key=api_key,
                        model=model,
                        api_endpoint=api_endpoint,
                        messages=msgs,
                        system_prompt=surg_sys,
                        on_partial=on_partial,
                        max_tokens=mt,
                    )
                inp += rin
                out += rout
                if revise_tool_calls:
                    _artifact(
                        "stage",
                        "revise|done|修订工具|"
                        + ",".join(revise_tool_calls[:20]),
                    )
                summary, repl, full_body = parse_surgical_output(raw_s)
                if repl:
                    merged, surgical_notes = splice_units(original_code, repl)
                    if any("回退" in n for n in surgical_notes):
                        _status("局部拼接失败，回退整文件修订…")
                        code = None
                    else:
                        code = enforce_img_dir(merged, source_dir)
                        if surgical_notes:
                            note = "；".join(surgical_notes[:8])
                            summary = (
                                (summary or "").rstrip()
                                + f"\n\n【局部修订】{note}"
                            )
                            _artifact(
                                "stage",
                                f"revise|done|局部修订完成|{note}",
                            )
                        if revise_tool_calls:
                            summary = (
                                (summary or "").rstrip()
                                + "\n【工具】"
                                + ",".join(revise_tool_calls[:12])
                            )
                        # 局部成功仍更新会话，便于重修订
                        if chat_session:
                            try:
                                from backend.script_generator.chat_session import (
                                    sync_last_assistant_code,
                                )
                                active_session = sync_last_assistant_code(
                                    dict(chat_session), original_code,
                                )
                                cont_user = surg_user
                            except Exception:
                                pass
                elif full_body:
                    _status("模型返回整文件，按整文件修订处理…")
                    code = enforce_img_dir(strip_code_fences(full_body), source_dir)
                else:
                    _status("局部修订无有效片段，回退整文件修订…")
        except Exception as e:
            print(f"[revise] 局部修订失败，回退整文件: {e}")
            code = None

    if code is None:
        active_session = None
        if continue_chat:
            try:
                from backend.script_generator.chat_session import (
                    REVISE_CONTINUATION_ADDENDUM,
                    append_turn,
                    session_to_llm_messages,
                    sync_last_assistant_code,
                    dumps_session,
                )
                active_session = sync_last_assistant_code(dict(chat_session), original_code)
                cont_user = (
                    "## Constraints to implement (highest priority)\n"
                    f"{feedback_block}\n\n"
                )
                if diagnosis_block:
                    cont_user += f"{diagnosis_block}\n\n"
                prior_sum = (prior_summary or "").strip()
                prior_diag = (prior_diagnosis or "").strip()
                if prior_sum or prior_diag:
                    cont_user += "## Prior revise context (do not regress)\n"
                    if prior_sum:
                        cont_user += f"### Previous SUMMARY\n{prior_sum[:2500]}\n\n"
                    if prior_diag:
                        cont_user += f"### Previous diagnosis\n{prior_diag[:2000]}\n\n"
                cont_user += (
                    "## User feedback (raw)\n"
                    f"{feedback_raw}\n\n"
                    "## Trial-run log (errors / states / tail)\n"
                    f"{log_block}\n\n"
                    "The previous assistant message is the current script "
                    "(already synced to editor). Revise it. "
                    "Respond with <<<SUMMARY>>> then <<<CODE>>>."
                )
                cont_user = images_block + "\n" + cont_user
                hist = session_to_llm_messages(active_session)
                hist.append({"role": "user", "content": [{"type": "text", "text": cont_user}]})
                cont_system = (
                    (active_session.get("system") or system)
                    + "\n\n"
                    + REVISE_CONTINUATION_ADDENDUM
                    + (f"\nIMG_DIR MUST be exactly: {img_dir_hint}\n" if img_dir_hint else "")
                    + f"\nAllowed browser methods ONLY: {methods}.\n"
                    + api_block
                    + "\n"
                    + images_block
                )
                _status("同会话续写修订…")
                _artifact("stage", "revise|running|同会话续写|沿用生成对话上下文")
                raw, rin, rout = await call_llm(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=hist,
                    system_prompt=cont_system,
                    on_partial=on_partial,
                    max_tokens=mt,
                )
            except Exception as e:
                print(f"[revise] 同会话续写失败，回退冷修订: {e}")
                continue_chat = False
                active_session = None
                raw, rin, rout = await call_llm(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
                    system_prompt=system,
                    on_partial=on_partial,
                    max_tokens=mt,
                )
        else:
            raw, rin, rout = await call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                api_endpoint=api_endpoint,
                messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
                system_prompt=system,
                on_partial=on_partial,
                max_tokens=mt,
            )
        inp += rin
        out += rout
        summary, code_raw = _parse_revise_output(raw)
        code = enforce_img_dir(strip_code_fences(code_raw), source_dir)

    code, vin, vout, summary = await _revise_validate_fix(
        code=code,
        summary=summary,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        source_dir=source_dir,
        methods=methods,
        img_dir_hint=img_dir_hint,
        mt=mt,
        on_partial=on_partial,
        on_status=_status,
        explanation=explanation_text or "",
        free_mode=free,
        raise_on_fail=False,
    )
    inp += vin
    out += vout
    _artifact(
        "stage",
        f"revise|done|修订草稿完成|{(summary or '')[:500]}",
    )

    # —— 审查：默认离线语法+素材；config revise_feedback_review_mode=llm 可恢复语义审查 ——
    review_mode = revise_feedback_review_mode()
    review, rin, rout = await _run_revise_review(
        mode=review_mode,
        code=code,
        source_dir=source_dir or "",
        explanation=explanation_text or "",
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        feedback_items=feedback_items or [feedback_raw],
        author_summary=summary,
        old_code=original_code,
        max_tokens=min(mt or 4096, 4096) if mt else 4096,
        on_status=_status,
        on_artifact=_artifact,
    )
    inp += rin
    out += rout

    # 离线模式不把「反馈语义未覆盖」塞进审查；仅保留「代码完全没改」硬信号
    if review_mode == "llm":
        local_miss = _local_uncovered_feedback(feedback_items, original_code, code)
        if local_miss:
            merged = list(review.get("uncovered") or [])
            for m in local_miss:
                if m not in merged:
                    merged.append(m)
            review["uncovered"] = merged
            review["ok"] = False
            if local_miss == feedback_items:
                notes = (review.get("notes") or "").strip()
                extra = "本地检查：代码与修订前相同，视为未改"
                review["notes"] = (notes + "；" + extra).strip("；") if notes else extra
    else:
        if _norm_code(original_code) == _norm_code(code):
            review["ok"] = False
            miss = list(review.get("uncovered") or [])
            note = "本地检查：代码与修订前相同，视为未改"
            if note not in miss:
                miss.append(note)
            review["uncovered"] = miss
            notes = (review.get("notes") or "").strip()
            review["notes"] = (notes + "；" + note).strip("；") if notes else note

    # —— 补修循环：硬校验优先，最多 2 轮 ——
    _MAX_GAP_ROUNDS = 2
    gap_round = 0
    review_attempt = 1
    while gap_round < _MAX_GAP_ROUNDS:
        code, _ = apply_codegen_patches(
            code,
            source_dir=source_dir or "",
            plan=None,
            explanation=explanation_text or "",
            free_mode=free,
        )
        val_errors = validate_generated_code(
            code,
            source_dir=source_dir or "",
            image_paths=[],
            explanation=explanation_text or "",
            free_mode=free,
        )
        # 离线审查：补修只盯语法/素材类 uncovered + 硬校验，不追反馈语义
        if review_mode != "llm":
            review = offline_syntax_asset_review(
                code,
                source_dir=source_dir or "",
                explanation=explanation_text or "",
            )
        uncovered = list(review.get("uncovered") or [])
        if not val_errors and review.get("ok"):
            break
        fix_items: list[str] = []
        seen_fix: set[str] = set()
        for e in val_errors:
            line = f"【硬校验】{e}"
            if line not in seen_fix:
                seen_fix.add(line)
                fix_items.append(line)
        for u in uncovered:
            if u not in seen_fix:
                seen_fix.add(u)
                fix_items.append(u)
        if not fix_items:
            break

        gap_round += 1
        review_attempt += 1
        kind = "硬校验+审查" if val_errors else "审查"
        _status(f"补修第 {gap_round}/{_MAX_GAP_ROUNDS} 轮（{kind}）…")
        _artifact(
            "stage",
            f"revise_gap|running|补修第{gap_round}轮|"
            + "\n".join(f"- {u}" for u in fix_items[:10]),
        )
        code, gap_sum, gin, gout = await _revise_gap_fix_round(
            code=code,
            fix_items=fix_items,
            feedback_block=feedback_block,
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            source_dir=source_dir,
            methods=methods,
            img_dir_hint=img_dir_hint,
            mt=mt,
            on_partial=on_partial,
            on_status=_status,
            explanation=explanation_text or "",
            free_mode=free,
        )
        inp += gin
        out += gout
        if gap_sum:
            summary = (summary or "").rstrip() + f"\n\n【补修·第{gap_round}轮】\n" + gap_sum.strip()
        _artifact("stage", f"revise_gap|done|补修第{gap_round}轮完成|")

        review, rin, rout = await _run_revise_review(
            mode=review_mode,
            code=code,
            source_dir=source_dir or "",
            explanation=explanation_text or "",
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            feedback_items=feedback_items or [feedback_raw],
            author_summary=summary,
            old_code=original_code,
            max_tokens=min(mt or 4096, 4096) if mt else 4096,
            on_status=_status,
            on_artifact=_artifact,
            attempt=review_attempt,
        )
        inp += rin
        out += rout
        if review_mode == "llm":
            local_miss = _local_uncovered_feedback(feedback_items, original_code, code)
            if local_miss:
                merged = list(review.get("uncovered") or [])
                for m in local_miss:
                    if m not in merged:
                        merged.append(m)
                review["uncovered"] = merged
                review["ok"] = False

    code, _ = apply_codegen_patches(
        code,
        source_dir=source_dir or "",
        plan=None,
        explanation=explanation_text or "",
        free_mode=free,
    )
    final_errors = validate_script_local(
        code,
        source_dir=source_dir or "",
        explanation=explanation_text or "",
        free_mode=free,
    )
    summary = _append_review_to_summary(summary, review)
    meta = {
        "trial_blocked": bool(final_errors),
        "validation_errors": final_errors,
        "review_ok": bool(review.get("ok")),
        "gap_rounds": gap_round,
        "diagnosis": diagnosis.to_dict() if diagnosis else {},
        "continue_chat": bool(continue_chat and active_session),
        "surgical": bool(surgical_notes) and not any(
            "回退" in n for n in surgical_notes
        ),
        "surgical_notes": surgical_notes[:12] if surgical_notes else [],
        "revise_tool_calls": revise_tool_calls[:24],
        "tool_assist": (
            {
                "provider": resolved_tool_assist.get("provider"),
                "model": resolved_tool_assist.get("model"),
            }
            if resolved_tool_assist
            else None
        ),
    }
    if active_session is not None:
        try:
            from backend.script_generator.chat_session import append_turn, dumps_session
            # 用最终代码（含补修）写入会话，便于重修订续写
            final_assistant = (
                f"<<<SUMMARY>>>\n{(summary or '')[:2000]}\n<<<CODE>>>\n{code}"
            )
            # cont_user 可能未定义（回退冷修订路径）
            turn_user = locals().get("cont_user") or user
            active_session = append_turn(
                active_session,
                user_text=turn_user,
                assistant_text=final_assistant,
            )
            meta["chat_session"] = active_session
            _artifact("chat_session", dumps_session(active_session))
        except Exception as e:
            print(f"[revise] 更新 chat_session 失败: {e}")
    if final_errors:
        block_lines = ["", "【硬校验未通过 · 不可试运行】"]
        block_lines.extend(f"- {e}" for e in final_errors[:12])
        if len(final_errors) > 12:
            block_lines.append(f"- …共 {len(final_errors)} 项")
        block_lines.append("请改反馈后点「重修订」，或修正介绍后重新生成。")
        summary = summary + "\n" + "\n".join(block_lines)
    return code, summary, inp, out, meta


async def _revise_gap_fix_round(
    *,
    code: str,
    fix_items: list[str],
    feedback_block: str,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    source_dir: str,
    methods: str,
    img_dir_hint: str,
    mt: Optional[int],
    on_partial=None,
    on_status=None,
    explanation: str = "",
    free_mode: Optional[bool] = None,
) -> tuple[str, str, int, int]:
    """针对硬校验/未覆盖项做一轮补修（优先局部函数替换）。"""
    gap_block = "\n".join(f"{i}. {u}" for i, u in enumerate(fix_items, 1))
    img_blk = allowed_images_block(
        source_dir or "", explanation=explanation or "",
    )
    gin = gout = 0
    gap_sum = ""
    new_code: Optional[str] = None

    # 优先局部补修，避免整文件重写把已修好的函数改坏
    use_surgical = True
    try:
        use_surgical = bool(
            (_load_config().get("defaults") or {}).get("revise_surgical", True)
        )
    except Exception:
        pass

    if use_surgical:
        try:
            from backend.script_generator.surgical_revise import (
                SURGICAL_SYSTEM_ADDENDUM,
                format_units_block,
                list_code_units,
                parse_surgical_output,
                select_target_names,
                splice_units,
            )
            targets = select_target_names(code, fix_items, max_n=6)
            units = list_code_units(code)
            if targets and units:
                if on_status:
                    on_status("补修·局部：" + ", ".join(targets))
                surg_sys = (
                    "You revise Minashigo automation Python to address ONLY listed fix items.\n"
                    f"Allowed browser methods ONLY: {methods}.\n"
                    "Keep FSM shape; do not regress unrelated functions.\n"
                    "CRITICAL: unknown_state scene keys MUST have handlers in each TASK_*_STATES "
                    "where that scene is relevant. "
                    "If 「返回主界面」+「主界面」 both exist, 导航成功 return '主界面' not '__exit__'.\n"
                    + (f"IMG_DIR MUST be exactly: {img_dir_hint}\n" if img_dir_hint else "")
                )
                try:
                    from backend.script_generator.api_catalog import api_contracts_block
                    surg_sys += api_contracts_block(explanation=explanation or "")
                except Exception:
                    pass
                surg_sys += "\n" + img_blk + "\n\n" + SURGICAL_SYSTEM_ADDENDUM
                surg_user = (
                    f"{img_blk}\n"
                    f"## Fix items (MUST all address)\n{gap_block}\n\n"
                    f"## Full original feedback\n{feedback_block}\n\n"
                    f"## TARGET UNITS (edit ONLY these)\n"
                    + ", ".join(targets)
                    + "\n\n"
                    + format_units_block(units, targets)
                    + "\n\nRespond with <<<SUMMARY>>> then <<<FUNCS>>>.\n"
                    "If a fix mentions a missing image: use ALLOWED filenames only.\n"
                )
                raw_s, rin, rout = await call_llm(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    api_endpoint=api_endpoint,
                    messages=[{
                        "role": "user",
                        "content": [{"type": "text", "text": surg_user}],
                    }],
                    system_prompt=surg_sys,
                    on_partial=on_partial,
                    max_tokens=mt,
                )
                gin += rin
                gout += rout
                gap_sum, repl, full_body = parse_surgical_output(raw_s)
                if repl:
                    merged, notes = splice_units(code, repl)
                    if any("回退" in n for n in notes):
                        new_code = None
                    else:
                        new_code = enforce_img_dir(merged, source_dir)
                        if notes:
                            gap_sum = (
                                (gap_sum or "").rstrip()
                                + "\n\n【局部补修】"
                                + "；".join(notes[:8])
                            )
                elif full_body:
                    new_code = enforce_img_dir(
                        strip_code_fences(full_body), source_dir,
                    )
        except Exception as e:
            print(f"[revise_gap] 局部补修失败，回退整文件: {e}")
            new_code = None

    if new_code is None:
        gap_sys = (
            "You revise Minashigo automation Python to address ONLY the listed fix items.\n"
            "Output format (STRICT):\n"
            "<<<SUMMARY>>>\nChinese checklist: each item → 已改/未改 + brief note.\n"
            "<<<CODE>>>\nFULL corrected Python source only.\n"
            f"Allowed browser methods ONLY: {methods}.\n"
            "Keep FSM shape and do not regress previous fixes.\n"
            "CRITICAL: unknown_state scene keys MUST have handlers in each TASK_*_STATES "
            "where that scene is relevant (hub 主界面/出击界面 for all tasks; "
            "房间/竞技/塔 scene only for matching task). "
            "Handler string returns must be keys in that same table.\n"
            "If 「返回主界面」+「主界面」 both exist in a task table, "
            "导航成功（本步骤结束）必须 return '主界面'，禁止 '__exit__'. "
            "仅本任务业务完成才 '__exit__'.\n"
            + (f"IMG_DIR MUST be exactly: {img_dir_hint}\n" if img_dir_hint else "")
        )
        try:
            from backend.script_generator.api_catalog import api_contracts_block
            gap_sys += api_contracts_block(explanation=explanation or "")
        except Exception:
            pass
        gap_sys += "\n" + img_blk
        gap_user = (
            f"{img_blk}\n"
            f"## Fix items (MUST all address)\n{gap_block}\n\n"
            f"## Full original feedback\n{feedback_block}\n\n"
            f"## Current code\n```python\n{code}\n```\n"
            "If a fix mentions a missing image: replace with an ALLOWED filename or "
            "remove that branch — do NOT invent png names.\n"
        )
        raw_g, rin, rout = await call_llm(
            provider=provider,
            api_key=api_key,
            model=model,
            api_endpoint=api_endpoint,
            messages=[{"role": "user", "content": [{"type": "text", "text": gap_user}]}],
            system_prompt=gap_sys,
            on_partial=on_partial,
            max_tokens=mt,
        )
        gin += rin
        gout += rout
        gap_sum, gap_code = _parse_revise_output(raw_g)
        new_code = enforce_img_dir(strip_code_fences(gap_code), source_dir)

    code, vin, vout, _ = await _revise_validate_fix(
        code=new_code,
        summary=gap_sum,
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        source_dir=source_dir,
        methods=methods,
        img_dir_hint=img_dir_hint,
        mt=mt,
        on_partial=on_partial,
        on_status=on_status,
        explanation=explanation or "",
        free_mode=free_mode,
        raise_on_fail=False,
    )
    return code, gap_sum, gin + vin, gout + vout


async def _revise_validate_fix(
    *,
    code: str,
    summary: str,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    source_dir: str,
    methods: str,
    img_dir_hint: str,
    mt: Optional[int],
    on_partial=None,
    on_status=None,
    explanation: str = "",
    free_mode: Optional[bool] = None,
    raise_on_fail: bool = True,
) -> tuple[str, int, int, str]:
    """结构校验；失败则自动修一轮。返回 (code, inp, out, summary)。"""
    code, patch_notes = apply_codegen_patches(
        code,
        source_dir=source_dir or "",
        plan=None,
        explanation=explanation or "",
        free_mode=free_mode,
    )
    if patch_notes and on_status:
        on_status(f"修订前本地补全 {len(patch_notes)} 项…")
    errors = validate_generated_code(
        code,
        source_dir=source_dir or "",
        image_paths=[],
        explanation=explanation or "",
        free_mode=free_mode,
    )
    if not errors:
        if patch_notes and summary:
            summary = summary.rstrip() + "\n\n（本地已自动补全: " + "; ".join(patch_notes[:4]) + "）"
        return code, 0, 0, summary
    if on_status:
        on_status(f"修订后校验有误，自动修复…（{errors[0]}）")
    err_block = "\n".join(f"- {e}" for e in errors)
    fix_user = (
        f"{allowed_images_block(source_dir or '', explanation=explanation or '')}\n"
        f"## Validation errors\n{err_block}\n\n"
        f"## Current code\n```python\n{code}\n```\n\n"
        "Return ONLY the complete fixed Python file (no summary, no markdown fences).\n"
        + (
            "Keep _img() stems from the ALLOWED list / introduction; do not invent "
            "home.png or other names missing from ALLOWED.\n"
            if is_img_identifiers_only()
            else "For missing image errors: use ONLY ALLOWED filenames or remove the branch.\n"
        )
    )
    fix_sys = (
        "You fix Minashigo automation Python scripts.\n"
        "Output ONLY the full corrected Python source. No markdown fences.\n"
        f"Allowed browser methods ONLY: {methods}.\n"
        "Keep FSM shape. Runtime keeps frames fresh after click/b_sleep; "
        "optional request_fps(hz) for continuous observe.\n"
        + (
            "NEVER invent _img() names not in ALLOWED _img stems "
            "(no home/1_home unless listed).\n"
            if is_img_identifiers_only()
            else "NEVER invent _img() png names not in ALLOWED image files.\n"
        )
        + (f"IMG_DIR MUST be exactly: {img_dir_hint}\n" if img_dir_hint else "")
    )
    try:
        from backend.script_generator.api_catalog import api_contracts_block
        fix_sys += api_contracts_block(explanation=explanation or "")
    except Exception:
        pass
    fix_sys += "\n" + allowed_images_block(
        source_dir or "", explanation=explanation or "",
    )
    raw2, inp2, out2 = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=[{"role": "user", "content": [{"type": "text", "text": fix_user}]}],
        system_prompt=fix_sys,
        on_partial=on_partial,
        max_tokens=mt,
    )
    code = enforce_img_dir(strip_code_fences(raw2), source_dir)
    code, _ = apply_codegen_patches(
        code,
        source_dir=source_dir or "",
        plan=None,
        explanation=explanation or "",
        free_mode=free_mode,
    )
    errors = validate_generated_code(
        code,
        source_dir=source_dir or "",
        image_paths=[],
        explanation=explanation or "",
        free_mode=free_mode,
    )
    if errors:
        if raise_on_fail:
            raise RuntimeError(f"修订后仍校验失败: {errors[0]}")
        if summary:
            summary = summary.rstrip() + f"\n\n（校验仍失败: {errors[0]}）"
        else:
            summary = f"（校验仍失败: {errors[0]}）"
        return code, inp2, out2, summary
    if summary:
        summary = summary.rstrip() + "\n\n（另：本地校验未通过，已自动做了一轮结构修复）"
    else:
        summary = "（模型未返回修改摘要；本地校验未通过，已自动做了一轮结构修复）"
    return code, inp2, out2, summary


async def _run_revise_review(
    *,
    mode: str,
    code: str,
    source_dir: str,
    explanation: str,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    feedback_items: list[str],
    author_summary: str,
    old_code: str,
    max_tokens: Optional[int],
    on_status=None,
    on_artifact=None,
    attempt: int = 1,
) -> tuple[dict, int, int]:
    """修订后审查：offline=语法+素材；llm=原反馈语义覆盖。"""
    if mode != "llm":
        if on_status:
            on_status(
                "离线审查（语法 + 素材引用）…"
                + (f"（第 {attempt} 次）" if attempt > 1 else "")
            )
        review = offline_syntax_asset_review(
            code, source_dir=source_dir or "", explanation=explanation or "",
        )
        body_lines = [
            "结论: 离线审查通过" if review.get("ok") else "结论: 离线审查未通过",
        ]
        for u in review.get("uncovered") or []:
            body_lines.append(f"- 未通过: {u}")
        for c in (review.get("covered") or [])[:6]:
            body_lines.append(f"- 已通过: {c}")
        if review.get("notes"):
            body_lines.append(f"备注: {review['notes']}")
        if on_artifact:
            try:
                key = f"review_{attempt}"
                status = "done" if review.get("ok") else "error"
                title = "离线审查通过" if review.get("ok") else "离线审查未通过"
                on_artifact("stage", f"{key}|{status}|{title}|" + "\n".join(body_lines))
            except Exception:
                pass
        return review, 0, 0
    return await _review_feedback_compliance(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        feedback_items=feedback_items,
        author_summary=author_summary,
        old_code=old_code,
        new_code=code,
        max_tokens=max_tokens,
        on_status=on_status,
        on_artifact=on_artifact,
        attempt=attempt,
    )


async def _review_feedback_compliance(
    *,
    provider: str,
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    feedback_items: list[str],
    author_summary: str,
    old_code: str,
    new_code: str,
    max_tokens: Optional[int] = 4096,
    on_status=None,
    on_artifact=None,
    attempt: int = 1,
) -> tuple[dict, int, int]:
    """独立审查：新代码是否真正覆盖用户反馈。返回 (review_dict, inp, out)。"""
    if on_status:
        on_status("审查修订是否符合反馈…" + (f"（第 {attempt} 次）" if attempt > 1 else ""))
    key = f"review_{attempt}"
    if on_artifact:
        try:
            on_artifact(
                "stage",
                f"{key}|running|审查反馈覆盖|"
                f"对照反馈与新旧代码，检查是否落实（第 {attempt} 次）",
            )
        except Exception:
            pass

    items_block = "\n".join(f"{i}. {it}" for i, it in enumerate(feedback_items, 1))
    # 控制体积：代码过长时截断两端保留头尾
    def _clip(code: str, limit: int = 14000) -> str:
        c = code or ""
        if len(c) <= limit:
            return c
        half = limit // 2
        return c[:half] + "\n\n# ... truncated ...\n\n" + c[-half:]

    system = (
        "You are an independent reviewer. Do NOT rewrite code.\n"
        "Judge whether the NEW code actually addresses each user feedback item.\n"
        "Ignore the author's SUMMARY if it conflicts with the code diff.\n"
        "Output ONLY valid JSON (no markdown):\n"
        "{\n"
        '  "ok": true/false,\n'
        '  "covered": ["short paraphrase of item that is done", ...],\n'
        '  "uncovered": ["verbatim or short restatement of item still missing", ...],\n'
        '  "notes": "optional Chinese one-liner"\n'
        "}\n"
        "ok=true only if uncovered is empty.\n"
        "Be strict: claiming 已改 in SUMMARY without a real code change → uncovered.\n"
        "If 返回主界面 handler still returns '__exit__' while 主界面 is a later state → uncovered.\n"
    )
    user = (
        f"## Feedback items\n{items_block}\n\n"
        f"## Author SUMMARY (may be wrong)\n{(author_summary or '').strip() or '(none)'}\n\n"
        f"## OLD code\n```python\n{_clip(old_code)}\n```\n\n"
        f"## NEW code\n```python\n{_clip(new_code)}\n```\n"
    )
    raw, inp, out = await call_llm(
        provider=provider,
        api_key=api_key,
        model=model,
        api_endpoint=api_endpoint,
        messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
        system_prompt=system,
        max_tokens=max_tokens,
    )
    review = _parse_review_json(raw, feedback_items)
    body_lines = []
    if review.get("ok"):
        body_lines.append("结论: 已覆盖全部反馈")
    else:
        body_lines.append("结论: 仍有未覆盖项")
        for u in review.get("uncovered") or []:
            body_lines.append(f"- 未覆盖: {u}")
    for c in (review.get("covered") or [])[:6]:
        body_lines.append(f"- 已覆盖: {c}")
    if review.get("notes"):
        body_lines.append(f"备注: {review['notes']}")
    body = "\n".join(body_lines)
    if on_artifact:
        try:
            status = "done" if review.get("ok") else "error"
            title = "审查通过" if review.get("ok") else "审查未完全通过"
            on_artifact("stage", f"{key}|{status}|{title}|{body}")
        except Exception:
            pass
    return review, inp or 0, out or 0


def _parse_review_json(raw: str, feedback_items: list[str]) -> dict:
    text = (raw or "").strip()
    data = None
    if text:
        fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    data = None
    if not isinstance(data, dict):
        return {
            "ok": False,
            "covered": [],
            "uncovered": list(feedback_items),
            "notes": "审查输出无法解析，视为未通过",
        }
    covered = [str(x).strip() for x in (data.get("covered") or []) if str(x).strip()]
    uncovered = [str(x).strip() for x in (data.get("uncovered") or []) if str(x).strip()]
    ok = bool(data.get("ok")) and not uncovered
    return {
        "ok": ok,
        "covered": covered,
        "uncovered": uncovered,
        "notes": str(data.get("notes") or "").strip(),
    }


def _append_review_to_summary(summary: str, review: dict) -> str:
    parts = [(summary or "").rstrip()]
    mode = str(review.get("mode") or "").strip().lower()
    if mode == "offline":
        lines = ["", "【离线审查 · 语法 + 素材引用】"]
        if review.get("ok"):
            lines.append("通过：语法与素材引用检查通过（未评反馈语义覆盖）。")
        else:
            lines.append("未通过：")
            for u in review.get("uncovered") or []:
                lines.append(f"- {u}")
            lines.append("（已尝试补修；语法/素材仍失败请改代码或反馈后再修订。）")
    else:
        lines = ["", "【反馈审查】"]
        if review.get("ok"):
            lines.append("通过：修订已覆盖反馈各项。")
        else:
            lines.append("未完全通过：以下反馈可能仍未落实——")
            for u in review.get("uncovered") or []:
                lines.append(f"- {u}")
            lines.append("（已尝试补修；若仍不对，请改反馈后再次修订。）")
    if review.get("covered"):
        lines.append("已确认：")
        for c in review["covered"][:8]:
            lines.append(f"- {c}")
    if review.get("notes"):
        lines.append(f"审查备注: {review['notes']}")
    parts.append("\n".join(lines))
    return "\n".join(p for p in parts if p).strip()


def _parse_revise_output(raw: str) -> tuple[str, str]:
    """从修订输出中拆出 (摘要, 代码)。兼容未按标记返回的旧格式。"""
    text = (raw or "").strip()
    if not text:
        return "（模型未返回修改摘要）", ""

    sum_m = re.search(
        r"<<<\s*SUMMARY\s*>>>\s*(.*?)\s*<<<\s*CODE\s*>>>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if sum_m:
        summary = sum_m.group(1).strip()
        code = text[sum_m.end():].strip()
        return summary or "（摘要为空）", code

    # 只有 CODE 标记
    code_m = re.search(r"<<<\s*CODE\s*>>>\s*(.*)$", text, flags=re.IGNORECASE | re.DOTALL)
    if code_m:
        before = text[: code_m.start()].strip()
        before = re.sub(r"<<<\s*SUMMARY\s*>>>", "", before, flags=re.IGNORECASE).strip()
        return before or "（模型未返回修改摘要）", code_m.group(1).strip()

    # 整段当代码
    return "（模型未按格式返回摘要，请对照代码自行确认）", text


def _split_feedback_items(text: str) -> list[str]:
    """把反馈拆成条目，便于模型逐条处理（换行 / 编号 / 分号 / 中文逗号并列）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    # 已有多行 → 按行
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) > 1:
        cleaned = []
        for ln in lines:
            ln = re.sub(r"^[\d]+[\.\)、]\s*", "", ln)
            ln = re.sub(r"^[-*•]\s*", "", ln)
            if ln:
                cleaned.append(ln)
        return cleaned or [raw]
    # 单行：按 ；; 或 「。且后面像新需求」弱拆；也支持 1. 2. 编号
    numbered = re.split(r"(?:(?<=^)|(?<=\s))(?:\d+[\.\)、]|[-*•])\s*", raw)
    numbered = [p.strip() for p in numbered if p and p.strip()]
    if len(numbered) > 1:
        return numbered
    parts = re.split(r"[；;]\s*", raw)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        return parts
    # 「……，并且/同时/另外……」常见并列
    parts = re.split(r"(?:，|,)\s*(?=并且|同时|另外|还有|以及|再|也要|还要)", raw)
    parts = [p.strip(" ，,") for p in parts if p.strip(" ，,")]
    return parts if len(parts) > 1 else [raw]


_CFG = _load_config()
_DEFAULTS = _CFG.get("defaults", {})
API_TIMEOUT = _DEFAULTS.get("api_timeout", 300)


async def _call_claude(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int, int]:
    """调用 Anthropic Claude API，返回 (代码, 输入tokens, 输出tokens)"""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("需要安装 anthropic 包: pip install anthropic")

    mt = resolve_max_tokens(max_tokens)
    client_kwargs = {"api_key": api_key, "timeout": API_TIMEOUT}
    if api_endpoint:
        client_kwargs["base_url"] = api_endpoint

    client = anthropic.AsyncAnthropic(**client_kwargs)
    # Anthropic 要求必填 max_tokens；无上限时用大额度占位
    claude_mt = mt if mt is not None else _UNLIMITED_MAX_TOKENS

    if on_partial:
        text = ""
        async with client.messages.stream(
            model=model, max_tokens=claude_mt, system=system_prompt, messages=messages,
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk
                on_partial(chunk)
        final = await stream.get_final_message()
        usage = final.usage
        if not text:
            raise RuntimeError("Claude 返回了空内容，请重试")
        return text, usage.input_tokens, usage.output_tokens
    else:
        response = await client.messages.create(
            model=model, max_tokens=claude_mt, system=system_prompt, messages=messages,
        )
        text = response.content[0].text
        if not text:
            raise RuntimeError("Claude 返回了空内容，请重试")
        usage = response.usage
        return text, usage.input_tokens, usage.output_tokens


async def _call_openai(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
    provider: str = "openai",
    max_tokens: Optional[int] = None,
    extra_headers: Optional[dict] = None,
) -> tuple[str, int, int]:
    """调用 OpenAI 兼容 API，支持流式输出"""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("需要安装 openai 包: pip install openai")

    from httpx import Timeout as HttpxTimeout
    mt = resolve_max_tokens(max_tokens)
    client_kwargs = {"api_key": api_key, "timeout": HttpxTimeout(API_TIMEOUT)}
    if api_endpoint:
        client_kwargs["base_url"] = api_endpoint
    if extra_headers:
        client_kwargs["default_headers"] = extra_headers

    client = AsyncOpenAI(**client_kwargs)
    flat_messages = _flatten_openai_messages(messages)
    system_msg = [{"role": "system", "content": system_prompt}]
    create_kwargs: dict = {
        "model": model,
        "messages": system_msg + flat_messages,
    }
    # 0/无上限：不传 max_tokens，交给服务端默认输出上限
    if mt is not None:
        create_kwargs["max_tokens"] = mt
    # DeepSeek V4 默认 thinking=on，长任务会把额度烧在 reasoning 上，content 为空
    if provider == "deepseek":
        create_kwargs["extra_body"] = _deepseek_extra_body(model)

    if on_partial:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        inp_tok = out_tok = 0
        finish_reason = ""
        stream = await client.chat.completions.create(
            **create_kwargs,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.usage:
                inp_tok = chunk.usage.prompt_tokens or inp_tok
                out_tok = chunk.usage.completion_tokens or out_tok
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if not delta:
                continue
            piece = delta.content or ""
            if piece:
                text_parts.append(piece)
                on_partial(piece)
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                reasoning_parts.append(reasoning)
        text = "".join(text_parts)
        if not text:
            raise RuntimeError(
                _empty_completion_error(
                    provider=provider,
                    model=model,
                    finish_reason=finish_reason,
                    out_tok=out_tok,
                    max_tokens=mt,
                    had_reasoning=bool(reasoning_parts),
                )
            )
        return text, inp_tok, out_tok

    response = await client.chat.completions.create(**create_kwargs)
    message = response.choices[0].message
    text = message.content or ""
    finish_reason = response.choices[0].finish_reason or ""
    reasoning = getattr(message, "reasoning_content", None) or ""
    usage = response.usage
    inp = usage.prompt_tokens if usage else 0
    out = usage.completion_tokens if usage else 0
    if not text:
        raise RuntimeError(
            _empty_completion_error(
                provider=provider,
                model=model,
                finish_reason=finish_reason,
                out_tok=out,
                max_tokens=mt,
                had_reasoning=bool(reasoning),
            )
        )
    return text, inp, out


def _empty_completion_error(
    *,
    provider: str,
    model: str,
    finish_reason: str,
    out_tok: int,
    max_tokens: Optional[int],
    had_reasoning: bool,
) -> str:
    """生成更可操作的空内容错误信息。"""
    cap = "无上限" if max_tokens is None else str(max_tokens)
    bits = [
        "API 返回了空内容",
        f"provider={provider}",
        f"model={model}",
        f"finish_reason={finish_reason or 'unknown'}",
        f"completion_tokens={out_tok}/{cap}",
    ]
    if had_reasoning:
        bits.append("had_reasoning=1")
    near_cap = (
        max_tokens is not None
        and out_tok
        and out_tok >= max_tokens * 0.95
    )
    if (finish_reason == "length" or near_cap) and had_reasoning:
        bits.append(
            "HINT:DeepSeek思考模式占满了输出额度，正文为空。"
            "已尝试自动关闭 thinking；请重试，或换 deepseek-chat / 提高 max_tokens"
        )
    elif finish_reason == "length":
        bits.append("HINT:输出被截断（token 用尽），请提高 max_tokens 或缩短提示词")
    elif provider == "deepseek":
        bits.append("HINT:DeepSeek不支持看图，请确认未依赖图片输入；可重试或换模型")
    return "；".join(bits)


async def _call_gemini(
    api_key: str,
    model: str,
    api_endpoint: Optional[str],
    messages: list[dict],
    system_prompt: str = "",
    on_partial=None,
) -> str:
    """调用 Google Gemini API"""
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("需要安装 google-generativeai 包: pip install google-generativeai")

    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(model_name=model, system_instruction=system_prompt)

    user_text = ""
    for msg in messages:
        for part in msg["content"]:
            if part.get("type") == "text":
                user_text += part["text"] + "\n"

    response = await gemini_model.generate_content_async(user_text)
    return response.text
