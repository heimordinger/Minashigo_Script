"""精品 few-shot + v2 模板检索：按标签 / 介绍 / 图片目录打分，注入 prompt。"""

from __future__ import annotations

import json
import re
from pathlib import Path

_CORPUS_ROOT = Path(__file__).parent / "corpus"
_INDEX_PATH = _CORPUS_ROOT / "index.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 中文/英文关键词 → 检索标签
_KEYWORD_TAGS: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"登录|login", re.I), ["login", "fsm", "unknown", "routing"]),
    (re.compile(r"推本|选关|备战|剧情", re.I), ["push", "fsm", "click", "transition"]),
    (re.compile(r"日常|房间|竞技场|爬塔|multi|多任务", re.I), ["daily", "multi", "run_task", "日常", "room"]),
    (re.compile(r"room_ok|收取奖励|room_收取|弹窗", re.I), ["room", "popup", "click", "daily"]),
    (re.compile(r"未知|场景识别|并发|识场景|场景", re.I), ["unknown", "routing", "fsm", "scene", "architecture", "v2", "semantic"]),
    (re.compile(r"分支|有则|无则|否则|回到第", re.I), ["branch", "fsm", "routing", "scene", "v2"]),
    (re.compile(r"阈值|threshold|nav_threshold|icon", re.I), ["threshold", "nav", "icon"]),
    (re.compile(r"结束|完成|退出|__exit__|本任务|本步骤", re.I), ["exit", "goal", "done"]),
    (re.compile(r"match_image_multi|x最大|最靠右|jjc_段位", re.I), ["match_multi", "jjc", "multi"]),
    (re.compile(r"update_frame|旧帧|刷新|截图缓存|polling", re.I), ["frame", "update_frame", "click"]),
    (re.compile(r"范式|骨架|结构范式|minimal", re.I), ["paradigm", "minimal", "structure", "architecture"]),
    (re.compile(r"孤儿|minashigo", re.I), ["minashigo"]),
]

_IMG_RE = re.compile(r"[\w\u4e00-\u9fff-]+\.(?:png|jpe?g)", re.I)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
_STOP_TOKENS = {
    "png", "jpg", "jpeg", "流程", "要求", "目的", "以下", "场景", "标识", "按钮",
    "窗口", "出现", "点击", "进入", "完成", "脚本", "解释", "介绍", "阶段",
}

_TEMPLATE_SCORE_MIN = 12
_GENERIC_TEMPLATE_TAGS = {
    "fsm", "unknown", "routing", "click", "wait", "transition", "dmm",
}
_COMMON_IMAGE_STEMS = {
    "err1", "err1_1", "err2", "err2_2",
}


def corpus_root() -> Path:
    return _CORPUS_ROOT


def project_root() -> Path:
    return _PROJECT_ROOT


def load_index() -> dict:
    if not _INDEX_PATH.is_file():
        return {"few_shot": [], "few_shot_max": 3, "templates": [], "template_max": 1, "golden": []}
    return json.loads(_INDEX_PATH.read_text(encoding="utf-8"))


def tags_from_text(text: str, extra: list[str] | None = None) -> set[str]:
    tags: set[str] = set(extra or [])
    for pat, mapped in _KEYWORD_TAGS:
        if pat.search(text or ""):
            tags.update(mapped)
    return tags


def _norm_path(p: str) -> str:
    return str(p or "").replace("\\", "/").lower()


def _folder_key(p: str) -> str:
    n = _norm_path(p).rstrip("/")
    if not n:
        return ""
    return n.split("/")[-1]


def _read_project_file(rel: str) -> str:
    path = Path(rel)
    if not path.is_absolute():
        path = _PROJECT_ROOT / rel
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _tokens(text: str) -> set[str]:
    out = set()
    for t in _TOKEN_RE.findall(text or ""):
        tl = t.lower()
        if tl in _STOP_TOKENS or len(tl) < 2:
            continue
        out.add(tl)
    return out


def _image_stems(text: str) -> set[str]:
    return {m.group(0).rsplit(".", 1)[0].lower() for m in _IMG_RE.finditer(text or "")}


_PARADIGM_ID = "minimal_multitask_paradigm"
_MULTI_TASK_RE = re.compile(r"[（(]\d+[）)]")


def _explanation_task_count(explanation: str) -> int:
    text = explanation or ""
    tm = re.search(
        r"任务流程：\n([\s\S]*?)(?=\n特殊规则：|\n## |\Z)",
        text,
    )
    if not tm:
        return 0
    return len(_MULTI_TASK_RE.findall(tm.group(1)))


def _needs_structure_paradigm(explanation: str, query_tags: set[str]) -> bool:
    if query_tags & {"multi", "run_task", "scene", "architecture", "daily", "日常", "paradigm"}:
        return True
    if _explanation_task_count(explanation) >= 2:
        return True
    if re.search(r"场景标识", explanation or ""):
        return True
    return False


def _load_paradigm_few_shot() -> dict | None:
    index = load_index()
    for item in index.get("few_shot") or []:
        if item.get("id") != _PARADIGM_ID:
            continue
        path = _CORPUS_ROOT / item["file"]
        if not path.is_file():
            return None
        return {
            "id": item.get("id"),
            "title": item.get("title") or item.get("id"),
            "tags": item.get("tags") or [],
            "score": 99,
            "content": path.read_text(encoding="utf-8").strip(),
        }
    return None


def _count_tasks_in_explanation(explanation: str) -> int:
    tm = re.search(
        r"任务流程：\n([\s\S]*?)(?=\n特殊规则：|\n## |\Z)",
        explanation or "",
    )
    if not tm:
        return 0
    return len(re.findall(r"[（(]\d+[）)]", tm.group(1)))


def build_paradigm_block(
    *,
    explanation: str = "",
    tags: list[str] | None = None,
) -> str:
    """仅注入最小结构范式（自由模式 / 骨架锚点）。"""
    shot = _load_paradigm_few_shot()
    parts: list[str] = []
    if shot:
        parts.append(format_few_shots_for_prompt([shot]))
    query_tags = tags_from_text(explanation, tags)
    if "run_task" in query_tags or "multi" in query_tags or _count_tasks_in_explanation(explanation) >= 2:
        mt_path = _CORPUS_ROOT / "few_shot" / "03_multi_task_dispatch.py"
        if mt_path.is_file():
            mt_content = mt_path.read_text(encoding="utf-8").strip()
            if mt_content:
                parts.append(
                    "### Multi-task dispatch pattern (hub entry + run_task loop)\n"
                    f"```python\n{mt_content}\n```"
                )
        jjc_path = _CORPUS_ROOT / "few_shot" / "13_jjc_fight_wait.py"
        if jjc_path.is_file() and re.search(
            r"过场|loading|加载|战斗结束|长时间等待", explanation or "", re.I
        ):
            jjc_content = jjc_path.read_text(encoding="utf-8").strip()
            if jjc_content:
                parts.append(
                    "### Long wait after loading transition (dedicated 等待* step)\n"
                    f"```python\n{jjc_content}\n```"
                )
    return "\n\n".join(parts)


def select_few_shots(
    *,
    explanation: str = "",
    tags: list[str] | None = None,
    max_items: int | None = None,
) -> list[dict]:
    """返回选中的 few-shot 条目（含 title/id/content），按相关度排序。"""
    index = load_index()
    items = index.get("few_shot") or []
    limit = max_items if max_items is not None else int(index.get("few_shot_max") or 3)
    query_tags = tags_from_text(explanation, tags)
    need_paradigm = _needs_structure_paradigm(explanation, query_tags)

    scored: list[tuple[int, dict, str]] = []
    for item in items:
        item_tags = {str(t).lower() for t in (item.get("tags") or [])}
        score = len(query_tags & item_tags)
        # 无标签命中时仍给 unknown_routing 保底分，保证至少有一条范文
        if score == 0 and item.get("id") == "unknown_routing":
            score = 1
        if score <= 0:
            continue
        path = _CORPUS_ROOT / item["file"]
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        scored.append((score, item, content))

    scored.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    out = []
    for score, item, content in scored[:limit]:
        out.append({
            "id": item.get("id"),
            "title": item.get("title") or item.get("id"),
            "tags": item.get("tags") or [],
            "score": score,
            "content": content.strip(),
        })

    # 日常/房间任务强制带上 update_frame 范文（旧帧是高频问题）
    need_frame = bool(query_tags & {"room", "daily", "日常", "multi", "popup"})
    ids = {o.get("id") for o in out}
    if need_frame and "update_frame_refresh" not in ids:
        for score, item, content in scored:
            if item.get("id") != "update_frame_refresh":
                continue
            if len(out) >= limit:
                out[-1] = {
                    "id": item.get("id"),
                    "title": item.get("title") or item.get("id"),
                    "tags": item.get("tags") or [],
                    "score": score,
                    "content": content.strip(),
                }
            else:
                out.append({
                    "id": item.get("id"),
                    "title": item.get("title") or item.get("id"),
                    "tags": item.get("tags") or [],
                    "score": score,
                    "content": content.strip(),
                })
            break

    def _force_shot(shot_id: str) -> None:
        nonlocal out
        ids_now = {o.get("id") for o in out}
        if shot_id in ids_now:
            return
        for score, item, content in scored:
            if item.get("id") != shot_id:
                continue
            entry = {
                "id": item.get("id"),
                "title": item.get("title") or item.get("id"),
                "tags": item.get("tags") or [],
                "score": score,
                "content": content.strip(),
            }
            if len(out) >= limit:
                out[-1] = entry
            else:
                out.append(entry)
            break

    if need_paradigm or _count_tasks_in_explanation(explanation) >= 2:
        _force_shot("fsm_branch_dispatch")
    if query_tags & {"room", "daily", "日常"} or "房间" in explanation:
        _force_shot("room_ok_popup")
    if query_tags & {"jjc", "arena"} or re.search(r"jjc|竞技", explanation or "", re.I):
        _force_shot("jjc_fight_wait")
    if re.search(r"jjc_段位", explanation or "", re.I) and re.search(
        r"x\s*最大|x最大|最靠右", explanation or "", re.I
    ):
        _force_shot("match_multi_dict")

    if need_paradigm:
        paradigm = _load_paradigm_few_shot()
        if paradigm:
            out = [o for o in out if o.get("id") != paradigm.get("id")]
            if len(out) >= limit:
                out = out[: max(0, limit - 1)]
            out.insert(0, paradigm)
    return out


def _annotation_text(item: dict) -> str:
    bits = [
        item.get("title") or "",
        item.get("summary") or "",
        item.get("when_to_use") or "",
        " ".join(str(x) for x in (item.get("keywords") or [])),
        " ".join(str(x) for x in (item.get("states") or [])),
        " ".join(str(x) for x in (item.get("images") or [])),
        " ".join(str(x) for x in (item.get("copy") or [])),
    ]
    return "\n".join(b for b in bits if b)


def _score_template(
    item: dict,
    *,
    explanation: str,
    tags: list[str] | None,
    source_dir: str,
    tmpl_expl: str,
) -> int:
    score = 0
    src = _norm_path(source_dir)
    item_src = _norm_path(item.get("source_dir") or "")
    src_folder = _folder_key(source_dir)
    item_folder = _folder_key(item.get("source_dir") or "")
    if item_src and src:
        if item_src in src or src in item_src:
            score += 50
        elif src_folder and src_folder == item_folder:
            score += 50
    query_tags = {t.lower() for t in tags_from_text(explanation, tags)} - _GENERIC_TEMPLATE_TAGS
    item_tags = {str(t).lower() for t in (item.get("tags") or [])} - _GENERIC_TEMPLATE_TAGS
    score += 4 * len(query_tags & item_tags)
    imgs_q = (_image_stems(explanation) | _image_stems(src)) - _COMMON_IMAGE_STEMS
    imgs_t = (
        _image_stems(tmpl_expl)
        | {str(n).lower() for n in (item.get("images") or [])}
    ) - _COMMON_IMAGE_STEMS
    overlap_imgs = imgs_q & imgs_t
    score += 5 * len(overlap_imgs)
    ann = _annotation_text(item)
    query_toks = _tokens(explanation) | _tokens(src) | {t.lower() for t in (tags or [])}
    kws = {str(k).lower() for k in (item.get("keywords") or [])}
    score += 3 * len(query_toks & kws)
    toks = query_toks & (_tokens(tmpl_expl) | _tokens(ann))
    score += min(8, len(toks))
    # 没有目录命中时，至少要有 2 张业务图名重合，避免日常/出击误挂登录或推本
    if score < 50 and len(overlap_imgs) < 2:
        return 0
    return score


def select_templates(
    *,
    explanation: str = "",
    tags: list[str] | None = None,
    source_dir: str = "",
    max_items: int | None = None,
) -> list[dict]:
    """按介绍 / 图片目录检索 v2 模板样例（介绍 + 完整脚本成对）。"""
    index = load_index()
    items = index.get("templates") or []
    limit = max_items if max_items is not None else int(index.get("template_max") or 1)
    scored: list[tuple[int, dict, str, str]] = []
    for item in items:
        expl = _read_project_file(item.get("explanation") or "")
        code = _read_project_file(item.get("script") or "")
        if not expl.strip() or not code.strip():
            continue
        score = _score_template(
            item,
            explanation=explanation,
            tags=tags,
            source_dir=source_dir,
            tmpl_expl=expl,
        )
        if score < _TEMPLATE_SCORE_MIN:
            continue
        scored.append((score, item, expl, code))
    scored.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    out = []
    for score, item, expl, code in scored[: max(0, limit)]:
        out.append({
            "id": item.get("id"),
            "title": item.get("title") or item.get("id"),
            "tags": item.get("tags") or [],
            "script": item.get("script"),
            "explanation_path": item.get("explanation"),
            "source_dir": item.get("source_dir"),
            "score": score,
            "summary": item.get("summary") or "",
            "when_to_use": item.get("when_to_use") or "",
            "copy": list(item.get("copy") or []),
            "do_not_copy": list(item.get("do_not_copy") or []),
            "states": list(item.get("states") or []),
            "images": list(item.get("images") or []),
            "keywords": list(item.get("keywords") or []),
            "explanation": expl.strip(),
            "content": code.strip(),
        })
    return out


def format_few_shots_for_prompt(shots: list[dict]) -> str:
    if not shots:
        return ""
    parts = [
        "Study these curated examples. Copy the *patterns* (routing / wait / multi-task), "
        "adapt names and images to the current task. Do NOT paste unused helpers."
    ]
    for i, s in enumerate(shots, 1):
        sid = s.get("id") or ""
        title = s.get("title") or sid
        if sid == _PARADIGM_ID:
            parts.append(
                "\n### STRUCTURE PARADIGM (mandatory skeleton — keep wiring; replace names only)\n"
                f"**{title}**\n```python\n{s['content']}\n```"
            )
            continue
        parts.append(f"\n### Example {i}: {title}\n```python\n{s['content']}\n```")
    return "\n".join(parts)


def _format_annotation_card(t: dict) -> str:
    copy_bits = t.get("copy") or []
    skip_bits = t.get("do_not_copy") or []
    states = t.get("states") or []
    images = t.get("images") or []
    kws = t.get("keywords") or []
    lines = [
        f"- id: `{t.get('id') or ''}`",
        f"- summary: {t.get('summary') or t.get('title') or ''}",
        f"- when_to_use: {t.get('when_to_use') or ''}",
    ]
    if states:
        lines.append("- states: " + " → ".join(str(s) for s in states))
    if images:
        lines.append("- images: " + ", ".join(str(x) for x in images))
    if copy_bits:
        lines.append("- copy: " + "; ".join(str(x) for x in copy_bits))
    if skip_bits:
        lines.append("- do_not_copy: " + "; ".join(str(x) for x in skip_bits))
    if kws:
        lines.append("- keywords: " + ", ".join(str(x) for x in kws))
    return "\n".join(lines)


def format_templates_for_prompt(tmpls: list[dict]) -> str:
    if not tmpls:
        return ""
    parts = [
        "Below is a production v2 script paired with the original player explanation. "
        "Read the annotation card first (when_to_use / copy / do_not_copy), then the script. "
        "Copy architecture only. Adapt image names, IMG_DIR, URLs, and states to the CURRENT task.",
    ]
    for i, t in enumerate(tmpls, 1):
        title = t.get("title") or t.get("id")
        script_name = Path(str(t.get("script") or "")).name or "template.py"
        parts.append(
            f"\n### Template {i}: {title} ({script_name})\n"
            f"**Annotation:**\n{_format_annotation_card(t)}\n\n"
            f"**Original explanation:**\n```text\n{t['explanation']}\n```\n"
            f"**Reference script:**\n```python\n{t['content']}\n```"
        )
    return "\n".join(parts)


def build_few_shot_block(
    *,
    explanation: str = "",
    tags: list[str] | None = None,
    source_dir: str = "",
) -> str:
    pattern_block = format_few_shots_for_prompt(
        select_few_shots(explanation=explanation, tags=tags)
    )
    template_block = format_templates_for_prompt(
        select_templates(explanation=explanation, tags=tags, source_dir=source_dir)
    )
    parts = []
    if template_block.strip():
        parts.append("## Production v2 templates (paired explanation + script)\n\n" + template_block)
    if pattern_block.strip():
        parts.append(pattern_block)
    return "\n\n".join(parts)
