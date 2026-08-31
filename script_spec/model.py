"""脚本说明结构化模型（script_spec 隔离包）。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# 不用 \b：Python 里中文也算 word char，「.png进入」会匹配失败
_IMAGE_EXT_RE = re.compile(
    r"\.(png|jpg|jpeg|webp|bmp)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 步骤里常见动词前缀，避免吃进文件名
_LEADING_VERB_RE = re.compile(
    r"^(匹配到|匹配|点选|点击|点按|等待|等到|检查|确认|查看|识别|寻找|找到|"
    r"出现|若无|若有|如果没有|如果|没有|有无)"
)

# 与 plan_schema.IMAGE_ROLES 对齐
ROLE_ID = "id"
ROLE_BUTTON = "button"
ROLE_OTHER = "other"
IMAGE_ROLES = (ROLE_ID, ROLE_BUTTON, ROLE_OTHER)

ROLE_LABELS = {
    ROLE_ID: "标识",
    ROLE_BUTTON: "按钮",
    ROLE_OTHER: "其它",
}
LABEL_TO_ROLE = {v: k for k, v in ROLE_LABELS.items()}

_HELPER_REF_RE = re.compile(r"@([^\s@（）()\[\]【】,，;；:：→]+)")
_THRESHOLD_RE = re.compile(r"(?:阈值|threshold)\s*[:=]?\s*(0\.\d+|\d+\.\d+)", re.I)
_OFFSET_RE = re.compile(r"(?:偏移|pianyi|offset)\s*[:=]?\s*\(?\s*(-?\d+)\s*,\s*(-?\d+)\s*\)?", re.I)
_YMAX_RE = re.compile(r"y\s*最大|最大\s*y|match_image_multi", re.I)
_HOLD_RE = re.compile(r"连续\s*(\d+(?:\.\d+)?)\s*秒|持续\s*(\d+(?:\.\d+)?)\s*秒")


def ensure_image_name(name: str) -> str:
    name = (name or "").strip().replace("\\", "/")
    if not name:
        return ""
    if Path(name).suffix.lower() in IMAGE_EXTS:
        return name
    return name + ".png"


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _filename_char(ch: str) -> bool:
    # 允许 /：子目录相对路径如 room/ok.png
    return ch.isalnum() or ch in "._-/" or _is_cjk(ch)


# source_dir -> (lower_name -> real_name)；仅 refresh_dir_image_map 会扫盘写入
_DIR_IMAGE_CACHE: dict[str, dict[str, str]] = {}


def clear_image_dir_cache(source_dir: str | Path | None = None) -> None:
    if source_dir is None:
        _DIR_IMAGE_CACHE.clear()
        return
    _DIR_IMAGE_CACHE.pop(str(Path(source_dir)), None)


def refresh_dir_image_map(source_dir: str | Path | None) -> dict[str, str]:
    """扫盘并更新缓存（含子目录）。值相对 source_dir，统一用 /。

    仅应在：读取草稿后 / 用户修改图片路径后 调用。
    """
    root = Path(source_dir or "")
    key = str(root)
    if not root.is_dir():
        _DIR_IMAGE_CACHE.pop(key, None)
        return {}
    mapping: dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        # 跳过缓存/隐藏目录
        parts = p.relative_to(root).parts
        if any(part.startswith(".") for part in parts):
            continue
        rel = "/".join(parts)
        mapping[rel.lower()] = rel
    _DIR_IMAGE_CACHE[key] = mapping
    return mapping


def dir_image_map(source_dir: str | Path | None) -> dict[str, str]:
    """读取已缓存的 lower_rel -> rel；不扫盘。"""
    if not source_dir:
        return {}
    return _DIR_IMAGE_CACHE.get(str(Path(source_dir)), {})


def image_exists_in_dir(
    token: str,
    source_dir: str | Path,
    known: dict[str, str] | None = None,
) -> bool:
    """判断 token 是否对应目录中的图片文件（支持子目录相对路径）。"""
    if not (token or "").strip():
        return False
    name = token.strip().replace("\\", "/")
    lower_map = known if known is not None else dir_image_map(source_dir)
    if name.lower() in lower_map:
        return True
    if Path(name).suffix.lower() not in IMAGE_EXTS:
        for ext in IMAGE_EXTS:
            if (name + ext).lower() in lower_map:
                return True
    return False


def resolve_image_rel(
    token: str,
    source_dir: str | Path | None = None,
    known: dict[str, str] | None = None,
) -> str:
    """把 token 解析成缓存中的相对路径；找不到返回空串。"""
    if not (token or "").strip():
        return ""
    name = token.strip().replace("\\", "/")
    lower_map = known if known is not None else dir_image_map(source_dir)
    hit = lower_map.get(name.lower())
    if hit:
        return hit
    if Path(name).suffix.lower() not in IMAGE_EXTS:
        for ext in IMAGE_EXTS:
            hit = lower_map.get((name + ext).lower())
            if hit:
                return hit
    return ""


def find_image_tokens(
    text: str,
    source_dir: str | Path | None = None,
    known: dict[str, str] | None = None,
) -> list[tuple[int, int, str]]:
    """从步骤文本提取图片文件名跨度 (start, end, token)。

    优先匹配目录中真实存在的文件名，避免「匹配出击_logo.png」把「匹配」吃进去。
    """
    text = text or ""
    known_map = known if known is not None else dir_image_map(source_dir)
    spans: list[tuple[int, int, str]] = []

    for m in _IMAGE_EXT_RE.finditer(text):
        end = m.end()
        dot = m.start()

        i = dot - 1
        while i >= 0 and _filename_char(text[i]):
            i -= 1
        max_start = i + 1
        if max_start >= dot:
            continue

        candidates: list[tuple[int, int, str]] = []
        for start in range(max_start, dot):
            token = text[start:end].replace("\\", "/")
            if not token or token[0] in "._-/":
                continue
            candidates.append((start, end, token))
        if not candidates:
            continue

        # 1) 目录里存在的：取最长（只查缓存，不反复扫盘）
        existing: list[tuple[int, int, str]] = []
        for s, e, t in candidates:
            key = t.lower()
            if key in known_map:
                existing.append((s, e, known_map[key]))
                continue
            if Path(t).suffix.lower() not in IMAGE_EXTS:
                for ext in IMAGE_EXTS:
                    k = (t + ext).lower()
                    if k in known_map:
                        existing.append((s, e, known_map[k]))
                        break
        if existing:
            spans.append(max(existing, key=lambda c: c[1] - c[0]))
            continue

        # 2) 剥掉「匹配/点击」等动词前缀
        start = max_start
        while start < dot:
            vm = _LEADING_VERB_RE.match(text, start)
            if not vm or vm.end() > dot:
                break
            start = vm.end()
        token = text[start:end].replace("\\", "/")
        if start < dot and token and token[0] not in "._-/":
            spans.append((start, end, token))
            continue

        # 3) ASCII 文件名回退
        ascii_cands = [
            (s, e, t)
            for s, e, t in candidates
            if t[0].isascii() and t[0].isalnum() and all(ch.isascii() for ch in t)
        ]
        if ascii_cands:
            spans.append(max(ascii_cands, key=lambda c: c[0]))
            continue

        spans.append((max_start, end, text[max_start:end]))

    return spans


def missing_images_in_text(text: str, source_dir: str | Path) -> list[str]:
    """步骤文本里引用了但不在目录中的图片名（去重保序）。"""
    missing: list[str] = []
    seen: set[str] = set()
    known = dir_image_map(source_dir)
    for _s, _e, token in find_image_tokens(text, source_dir, known=known):
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        if not image_exists_in_dir(token, source_dir, known=known):
            missing.append(token)
    return missing


@dataclass
class SceneRow:
    """兼容旧 JSON：仅标识图 → 状态。"""
    image: str = ""
    state: str = ""


@dataclass
class ImageEntry:
    image: str = ""
    role: str = ROLE_ID  # id | button | other
    state: str = ""      # 标识图对应状态名；按钮可填所属界面（可选）
    note: str = ""       # 作用 / 偏移 / 阈值 / 点击方式


@dataclass
class HelperSpec:
    name: str = ""
    steps: str = ""


@dataclass
class TaskSpec:
    name: str = ""
    steps: str = ""  # 可含 @辅助名


@dataclass
class SpecIssue:
    level: str  # "error" | "warn"
    message: str


@dataclass
class ScriptSpec:
    goal: str = ""
    source_dir: str = ""
    images: list[ImageEntry] = field(default_factory=list)
    helpers: list[HelperSpec] = field(default_factory=list)
    tasks: list[TaskSpec] = field(default_factory=list)
    notes: str = ""

    # ── 兼容 ──

    @property
    def scenes(self) -> list[SceneRow]:
        return [
            SceneRow(image=e.image, state=e.state)
            for e in self.images
            if (e.role or ROLE_ID) == ROLE_ID
            and e.image.strip()
            and e.state.strip()
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source_dir": self.source_dir,
            "images": [asdict(x) for x in self.images],
            "helpers": [asdict(x) for x in self.helpers],
            "tasks": [asdict(t) for t in self.tasks],
            "notes": self.notes,
            # 兼容旧字段
            "scenes": [asdict(s) for s in self.scenes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScriptSpec":
        data = data or {}
        images: list[ImageEntry] = []
        raw_images = data.get("images")
        if isinstance(raw_images, list) and raw_images:
            for x in raw_images:
                if not isinstance(x, dict):
                    continue
                role = str(x.get("role") or ROLE_ID).strip().lower()
                if role in LABEL_TO_ROLE:
                    role = LABEL_TO_ROLE[role]
                if role not in IMAGE_ROLES:
                    role = ROLE_OTHER
                images.append(ImageEntry(
                    image=str(x.get("image") or ""),
                    role=role,
                    state=str(x.get("state") or ""),
                    note=str(x.get("note") or ""),
                ))
        else:
            for x in (data.get("scenes") or []):
                if not isinstance(x, dict):
                    continue
                images.append(ImageEntry(
                    image=str(x.get("image") or ""),
                    role=ROLE_ID,
                    state=str(x.get("state") or ""),
                    note="",
                ))

        helpers = [
            HelperSpec(
                name=str(x.get("name") or ""),
                steps=str(x.get("steps") or ""),
            )
            for x in (data.get("helpers") or [])
            if isinstance(x, dict)
        ]
        tasks = [
            TaskSpec(
                name=str(x.get("name") or ""),
                steps=str(x.get("steps") or ""),
            )
            for x in (data.get("tasks") or [])
            if isinstance(x, dict)
        ]
        return cls(
            goal=str(data.get("goal") or ""),
            source_dir=str(data.get("source_dir") or ""),
            images=images,
            helpers=helpers,
            tasks=tasks,
            notes=str(data.get("notes") or ""),
        )

    # ── 校验 ──

    def is_blank(self) -> bool:
        """未开始填写实质内容（仅选目录不算）时不刷校验错误。"""
        if (self.goal or "").strip() or (self.notes or "").strip():
            return False
        if any((e.image or "").strip() or (e.state or "").strip() or (e.note or "").strip()
               for e in self.images):
            return False
        if any((h.name or "").strip() or (h.steps or "").strip() for h in self.helpers):
            return False
        if any((t.name or "").strip() or (t.steps or "").strip() for t in self.tasks):
            return False
        return True

    def validate(self) -> list[SpecIssue]:
        issues: list[SpecIssue] = []
        if self.is_blank():
            return issues
        if not (self.goal or "").strip():
            issues.append(SpecIssue("error", "缺少目标：生成器不知道脚本要完成什么"))
        if not (self.source_dir or "").strip():
            issues.append(SpecIssue("warn", "未选择图片目录：无法核对图片是否存在"))

        id_rows = [
            e for e in self.images
            if (e.role or ROLE_ID) == ROLE_ID and e.image.strip()
        ]
        good_ids = [e for e in id_rows if e.state.strip()]
        if not good_ids:
            issues.append(SpecIssue(
                "error",
                "无有效场景标识：至少要有一张「标识」图并填写状态名（unknown_state 路由）",
            ))
        for e in id_rows:
            if not e.state.strip():
                issues.append(SpecIssue(
                    "warn",
                    f"标识图「{e.image}」未填状态名，导出时会被忽略",
                ))

        for e in self.images:
            if not e.image.strip():
                continue
            if (e.role or "") == ROLE_BUTTON and not (e.note or "").strip():
                issues.append(SpecIssue(
                    "warn",
                    f"按钮「{e.image}」无说明：建议写清偏移/阈值/点击方式",
                ))

        named_helpers = {(h.name or "").strip() for h in self.helpers if (h.name or "").strip()}
        for h in self.helpers:
            name = (h.name or "").strip()
            if not name:
                issues.append(SpecIssue("warn", "存在未命名的辅助步骤"))
                continue
            if not (h.steps or "").strip():
                issues.append(SpecIssue("error", f"辅助步骤「{name}」没有步骤内容"))

        # @出击限制 这类：指向图片/规则标签，不是辅助步骤名
        feature_tags = _feature_tag_names(self)

        usable_tasks = [t for t in self.tasks if (t.name or "").strip()]
        if not usable_tasks:
            issues.append(SpecIssue("error", "没有任务：至少添加一个任务并写步骤"))
        for t in usable_tasks:
            if not (t.steps or "").strip():
                issues.append(SpecIssue("error", f"任务「{t.name}」没有步骤"))
            for ref in _HELPER_REF_RE.findall(t.steps or ""):
                if ref in named_helpers or ref in feature_tags:
                    continue
                issues.append(SpecIssue(
                    "error",
                    f"任务「{t.name}」引用了不存在的辅助步骤 @{ref}",
                ))

        if self.source_dir:
            root = Path(self.source_dir)
            if root.is_dir():
                known = dir_image_map(root)
                if not known:
                    known = refresh_dir_image_map(root)
                existing = set(known.values())
                existing_lower = set(known.keys())
                for e in self.images:
                    name = ensure_image_name(e.image)
                    if not name:
                        continue
                    key = name.replace("\\", "/").lower()
                    if key not in existing_lower and name not in existing:
                        issues.append(SpecIssue(
                            "warn",
                            f"图片「{e.image}」不在目录中",
                        ))

                # 步骤文本中引用的缺失图片
                for h in self.helpers:
                    hname = (h.name or "").strip() or "未命名辅助"
                    for miss in missing_images_in_text(h.steps or "", root):
                        issues.append(SpecIssue(
                            "error",
                            f"辅助「{hname}」步骤引用了目录中不存在的图片「{miss}」",
                        ))
                for t in usable_tasks:
                    for miss in missing_images_in_text(t.steps or "", root):
                        issues.append(SpecIssue(
                            "error",
                            f"任务「{t.name}」步骤引用了目录中不存在的图片「{miss}」",
                        ))
            else:
                issues.append(SpecIssue("warn", f"图片目录不存在：{self.source_dir}"))

        return issues

    def validation_summary(self) -> str:
        if self.is_blank():
            return ""
        issues = self.validate()
        if not issues:
            return "校验通过：可以导出给生成器"
        errors = sum(1 for i in issues if i.level == "error")
        warns = sum(1 for i in issues if i.level == "warn")
        head = []
        if errors:
            head.append(f"{errors} 个错误")
        if warns:
            head.append(f"{warns} 个警告")
        lines = ["校验：" + "、".join(head) + "（生成前建议修掉错误）"]
        for i in issues:
            mark = "[错误]" if i.level == "error" else "[警告]"
            lines.append(f"  {mark} {i.message}")
        return "\n".join(lines)

    # ── 导出 ──

    def _helper_map(self) -> dict[str, HelperSpec]:
        return {
            (h.name or "").strip(): h
            for h in self.helpers
            if (h.name or "").strip()
        }

    def _expand_step_line(self, line: str, helpers: dict[str, HelperSpec]) -> list[str]:
        """把 @辅助名 展开成可读步骤；未知引用原样保留。"""
        line = line.strip()
        if not line:
            return []
        m = re.fullmatch(r"@([^\s@]+)", line)
        if m:
            name = m.group(1)
            h = helpers.get(name)
            if not h:
                return [f"@{name}（未定义辅助步骤）"]
            out = [f"执行辅助步骤「{name}」："]
            for s in (h.steps or "").splitlines():
                s = s.strip()
                if s:
                    out.append(f"  · {s}")
            if len(out) == 1:
                out.append("  · （辅助步骤为空）")
            return out
        # 行内混写：保留原文，附注引用
        refs = _HELPER_REF_RE.findall(line)
        if refs:
            known = [r for r in refs if r in helpers]
            if known:
                return [line + "  （引用：" + "、".join(f"@{r}" for r in known) + "）"]
        return [line]

    def to_explanation_text(self, *, include_validation: bool = True) -> str:
        """导出给生成 Agent 用的规范说明文本。"""
        lines: list[str] = []
        if include_validation:
            summary = self.validation_summary()
            if summary:
                lines.append(summary)
                lines.append("")

        lines.append("目标：")
        lines.append((self.goal or "").strip() or "（未填写）")
        lines.append("")

        helpers = self._helper_map()
        if helpers:
            lines.append("辅助步骤（任务中可用 @名称 引用）：")
            for i, (name, h) in enumerate(helpers.items(), 1):
                lines.append(f"（{chr(ord('a') + i - 1) if i <= 26 else i}）{name}")
                steps = (h.steps or "").strip()
                if steps:
                    for step in steps.splitlines():
                        step = step.strip()
                        if step:
                            lines.append(f"  - {step}")
                else:
                    lines.append("  - （未写步骤）")
            lines.append("")

        id_entries = [
            e for e in self.images
            if (e.role or ROLE_ID) == ROLE_ID and e.image.strip() and e.state.strip()
        ]
        btn_entries = [
            e for e in self.images
            if (e.role or "") == ROLE_BUTTON and e.image.strip()
        ]
        other_entries = [
            e for e in self.images
            if (e.role or "") == ROLE_OTHER and e.image.strip()
        ]

        lines.append("场景标识：")
        if not id_entries:
            lines.append("（未填写）")
        else:
            for e in id_entries:
                img = ensure_image_name(e.image)
                extra = f"；{e.note.strip()}" if e.note.strip() else ""
                lines.append(f"{img}：可作为「{e.state.strip()}」的标识图{extra}")
        lines.append("")

        if btn_entries or other_entries:
            lines.append("图片说明：")
            for e in btn_entries:
                img = ensure_image_name(e.image)
                bits = ["按钮"]
                if e.state.strip():
                    bits.append(f"界面「{e.state.strip()}」")
                if e.note.strip():
                    bits.append(e.note.strip())
                lines.append(f"{img}：{'；'.join(bits)}")
            for e in other_entries:
                img = ensure_image_name(e.image)
                note = e.note.strip() or "其它"
                lines.append(f"{img}：{note}")
            lines.append("")

        lines.append("任务流程：")
        usable_tasks = [t for t in self.tasks if (t.name or "").strip()]
        if not usable_tasks:
            lines.append("（未填写）")
        else:
            for i, t in enumerate(usable_tasks, 1):
                lines.append(f"（{i}）{t.name.strip()}")
                steps = (t.steps or "").strip()
                if steps:
                    for step in steps.splitlines():
                        for expanded in self._expand_step_line(step, helpers):
                            lines.append(f"  - {expanded}")
                else:
                    lines.append("  - （未写步骤）")
        lines.append("")

        rule_bits = []
        if (self.notes or "").strip():
            rule_bits.append(self.notes.strip())
        auto = self._auto_rules_from_images()
        if auto:
            rule_bits.append(auto)
        if rule_bits:
            lines.append("特殊规则：")
            lines.append("\n".join(rule_bits))
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _auto_rules_from_images(self) -> str:
        """从图片 note 里抽关键词，补进特殊规则。"""
        chunks: list[str] = []
        for e in self.images:
            note = (e.note or "").strip()
            if not note:
                continue
            img = ensure_image_name(e.image) or e.image
            tags: list[str] = []
            if _YMAX_RE.search(note):
                tags.append("点击 y 最大的匹配点（match_image_multi）")
            th = _THRESHOLD_RE.search(note)
            if th:
                tags.append(f"阈值 {th.group(1)}")
            off = _OFFSET_RE.search(note)
            if off:
                tags.append(f"点击偏移 ({off.group(1)},{off.group(2)})")
            hold = _HOLD_RE.search(note)
            if hold:
                sec = hold.group(1) or hold.group(2)
                tags.append(f"连续可见约 {sec} 秒再操作")
            if tags:
                chunks.append(f"{img}：{'；'.join(tags)}")
            elif (e.role or "") == ROLE_BUTTON and note:
                chunks.append(f"{img}：{note}")
        return "\n".join(chunks)

    def _infer_task_states(self, task: TaskSpec) -> list[str]:
        scene_states = [s.state for s in self.scenes]
        helpers = self._helper_map()
        text_parts = [task.steps or ""]
        for ref in _HELPER_REF_RE.findall(task.steps or ""):
            h = helpers.get(ref)
            if h:
                text_parts.append(h.steps or "")
                text_parts.append(ref)
        text = "\n".join(text_parts)

        ordered: list[str] = ["未知"]
        for st in scene_states:
            if st and st in text and st not in ordered:
                ordered.append(st)
        # 常见导航名若出现在步骤里也保留（即使未建标识）
        for nav in ("主界面", "出击界面", "返回主界面", "返回出击界面"):
            if nav in text and nav not in ordered:
                ordered.append(nav)
        return ordered

    def to_plan_hints(self) -> dict[str, Any]:
        """结构化提示，可直接喂给 plan / normalize_plan。"""
        scene_map = []
        for e in self.images:
            if (e.role or ROLE_ID) != ROLE_ID:
                continue
            if not e.image.strip() or not e.state.strip():
                continue
            scene_map.append({
                "image": ensure_image_name(e.image),
                "state": e.state.strip(),
            })

        image_roles = []
        for e in self.images:
            if not e.image.strip():
                continue
            role = e.role if e.role in IMAGE_ROLES else ROLE_OTHER
            image_roles.append({
                "name": ensure_image_name(e.image),
                "role": role if role != ROLE_ID else "id",
            })

        states: list[dict[str, Any]] = [
            {"name": "未知", "purpose": "场景识别 / 路由入口", "timeout": 30},
        ]
        seen = {"未知"}
        for m in scene_map:
            st = m["state"]
            if st not in seen:
                seen.add(st)
                states.append({
                    "name": st,
                    "purpose": f"场景标识 {m['image']}",
                    "timeout": 30,
                })
        for h in self.helpers:
            name = (h.name or "").strip()
            if name and name not in seen:
                seen.add(name)
                states.append({
                    "name": name,
                    "purpose": "可复用辅助步骤",
                    "timeout": 30,
                })

        tasks = []
        for t in self.tasks:
            if not (t.name or "").strip():
                continue
            tasks.append({
                "name": t.name.strip(),
                "states": self._infer_task_states(t),
                "steps": (t.steps or "").strip(),
            })

        note_parts = []
        if (self.notes or "").strip():
            note_parts.append(self.notes.strip())
        auto = self._auto_rules_from_images()
        if auto:
            note_parts.append(auto)
        if self.helpers:
            refs = "、".join(f"@{h.name}" for h in self.helpers if h.name.strip())
            note_parts.append(f"任务可用辅助引用：{refs}")

        return {
            "kind": "multi_task" if len(tasks) > 1 else "single_fsm",
            "states": states,
            "scene_map": scene_map,
            "image_roles": image_roles,
            "tasks": [{"name": t["name"], "states": t["states"]} for t in tasks],
            "notes": "\n".join(note_parts).strip(),
            "goal": self.goal.strip(),
            "helpers": [
                {"name": h.name.strip(), "steps": (h.steps or "").strip()}
                for h in self.helpers
                if h.name.strip()
            ],
            "task_steps": {t["name"]: t["steps"] for t in tasks},
        }

    def save_json(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load_json(cls, path: Path) -> "ScriptSpec":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_explanation_text(cls, text: str, *, source_dir: str = "") -> "ScriptSpec":
        """把导出的脚本介绍 txt 解析回结构化说明。"""
        sections = _split_explanation_sections(text or "")
        goal = _first_content_line(sections.get("目标", ""))
        helpers = _parse_named_blocks(sections.get("辅助步骤", ""))
        tasks = _parse_task_blocks(sections.get("任务流程", ""))
        images = _parse_scene_images(sections.get("场景标识", ""))
        images.extend(_parse_picture_notes(sections.get("图片说明", ""), images))
        notes = (sections.get("特殊规则", "") or "").strip()
        # 特殊规则里与图片 note 重复的行丢掉，只留全局约定
        if notes:
            img_notes = {
                f"{ensure_image_name(e.image)}：{e.note.strip()}"
                for e in images
                if e.image.strip() and e.note.strip()
            }
            kept = []
            for ln in notes.splitlines():
                s = ln.strip()
                if not s:
                    continue
                if s in img_notes:
                    continue
                # 「foo.png：点击偏移」这类自动规则也跳过（已在图片说明里）
                if re.match(r".+\.(?:png|jpe?g|webp|bmp)：", s, re.I) and any(
                    ensure_image_name(e.image) == s.split("：", 1)[0].strip()
                    for e in images
                ):
                    continue
                kept.append(ln.rstrip())
            notes = "\n".join(kept).strip()
        return cls(
            goal=goal,
            source_dir=source_dir or "",
            images=images,
            helpers=helpers,
            tasks=tasks,
            notes=notes,
        )


_SECTION_HEAD_RE = re.compile(
    r"^(目标|辅助步骤|场景标识|图片说明|任务流程|特殊规则)\b"
)
_PLACEHOLDER_RE = re.compile(r"^（未填写|未填写）")
_NAMED_BLOCK_RE = re.compile(r"^（(?:[a-z]|\d+)）\s*(.+)$")
_SCENE_LINE_RE = re.compile(
    r"^(.+?\.(?:png|jpe?g|webp|bmp)(?:\s*[/／、]\s*.+?\.(?:png|jpe?g|webp|bmp))*)"
    r"\s*[：:]\s*可作为「([^」]+)」的标识图(?:[；;](.*))?$",
    re.I,
)
_IMG_LINE_RE = re.compile(
    r"^((?:[^\s：:/／、]+?\.(?:png|jpe?g|webp|bmp))"
    r"(?:\s*[/／、]\s*[^\s：:/／、]+?\.(?:png|jpe?g|webp|bmp))*)"
    r"\s*[：:]\s*(.*)$",
    re.I,
)
_HELPER_EXPAND_RE = re.compile(r"^执行辅助步骤「([^」]+)」：?\s*$")
_DOT_STEP_RE = re.compile(r"^[·•]\s*")
# 图片说明里「a.png / b.png」等同义多文件名
_IMG_NAME_SPLIT_RE = re.compile(r"\s*[/／、]\s*")
_IMG_FILE_RE = re.compile(
    r"^(.+?\.(?:png|jpe?g|webp|bmp))$",
    re.I,
)


def _split_image_token(raw: str) -> list[str]:
    """拆分「a.png / b.png」→ 多个文件名。"""
    out: list[str] = []
    for part in _IMG_NAME_SPLIT_RE.split((raw or "").strip()):
        p = part.strip()
        if not p:
            continue
        m = _IMG_FILE_RE.match(p)
        if not m:
            continue
        name = ensure_image_name(m.group(1))
        if name and name not in out:
            out.append(name)
    return out


def _split_state_and_note(rest: str) -> tuple[str, str]:
    """短格式「主界面」或「竞技场列表（说明…）」→ (状态名, 备注)。"""
    s = (rest or "").strip()
    if not s:
        return "", ""
    if "（" in s:
        head, _, tail = s.partition("（")
        head = head.strip()
        note = ("（" + tail).strip() if tail else ""
        return head, note
    if "(" in s and re.search(r"\([\u4e00-\u9fff]", s):
        head, _, tail = s.partition("(")
        return head.strip(), ("(" + tail).strip() if tail else ""
    return s, ""


def _feature_tag_names(spec: "ScriptSpec") -> set[str]:
    """可被 @引用、但不是辅助步骤的标签（图片名 stem / 特殊规则里的通用名）。"""
    tags: set[str] = set()
    for e in spec.images:
        name = ensure_image_name(e.image)
        if not name:
            continue
        stem = Path(name).stem
        if stem:
            tags.add(stem)
    notes = (spec.notes or "") + "\n"
    for h in spec.helpers:
        notes += (h.steps or "") + "\n"
    # 「出击限制（通用）」等
    for m in re.finditer(r"(?:^|[\n；;·•\-])\s*([^\s：:]{1,20})（通用）", notes):
        tags.add(m.group(1).strip())
    if spec.source_dir:
        root = Path(spec.source_dir)
        if root.is_dir():
            known = dir_image_map(root) or refresh_dir_image_map(root)
            for rel in known.values():
                stem = Path(rel).stem
                if stem:
                    tags.add(stem)
    return tags


def _split_explanation_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = _SECTION_HEAD_RE.match(line.strip())
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _first_content_line(block: str) -> str:
    for ln in (block or "").splitlines():
        s = ln.strip()
        if not s or _PLACEHOLDER_RE.match(s):
            continue
        return s
    return ""


def _parse_named_blocks(block: str) -> list[HelperSpec]:
    items: list[HelperSpec] = []
    name = ""
    steps: list[str] = []

    def flush():
        nonlocal name, steps
        if name or steps:
            items.append(HelperSpec(name=name, steps="\n".join(steps).strip()))
        name, steps = "", []

    for ln in (block or "").splitlines():
        s = ln.strip()
        if not s or _PLACEHOLDER_RE.match(s):
            continue
        m = _NAMED_BLOCK_RE.match(s)
        if m:
            flush()
            name = m.group(1).strip()
            continue
        if s.startswith("- "):
            steps.append(s[2:].strip())
        elif name:
            steps.append(s)
    flush()
    return items


def _collapse_helper_steps(raw_steps: list[str]) -> str:
    out: list[str] = []
    skipping_expand = False
    for s in raw_steps:
        line = (s or "").strip()
        if not line:
            continue
        hm = _HELPER_EXPAND_RE.match(line)
        if hm:
            out.append(f"@{hm.group(1)}")
            skipping_expand = True
            continue
        if skipping_expand:
            if line.startswith(("·", "•")) or _DOT_STEP_RE.match(line):
                continue
            skipping_expand = False
        line = re.sub(r"\s*（引用：[^）]+）\s*$", "", line)
        out.append(line)
    return "\n".join(out).strip()


def _parse_task_blocks(block: str) -> list[TaskSpec]:
    items: list[TaskSpec] = []
    name = ""
    steps: list[str] = []

    def flush():
        nonlocal name, steps
        if name or steps:
            items.append(TaskSpec(name=name, steps=_collapse_helper_steps(steps)))
        name, steps = "", []

    for ln in (block or "").splitlines():
        raw = ln.rstrip()
        s = raw.strip()
        if not s or _PLACEHOLDER_RE.match(s):
            continue
        m = _NAMED_BLOCK_RE.match(s)
        if m:
            flush()
            name = m.group(1).strip()
            continue
        if s.startswith("- "):
            steps.append(s[2:])
        elif s.startswith("·") or s.startswith("•"):
            steps.append(s)
        elif name:
            steps.append(s)
    flush()
    return items


def _parse_scene_images(block: str) -> list[ImageEntry]:
    out: list[ImageEntry] = []
    seen: set[str] = set()
    for ln in (block or "").splitlines():
        s = ln.strip()
        if not s or _PLACEHOLDER_RE.match(s):
            continue
        m = _SCENE_LINE_RE.match(s)
        if m:
            names = _split_image_token(m.group(1)) or [ensure_image_name(m.group(1))]
            state = m.group(2).strip()
            note = (m.group(3) or "").strip()
            for name in names:
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(ImageEntry(
                    image=name, role=ROLE_ID, state=state, note=note,
                ))
            continue
        m2 = _IMG_LINE_RE.match(s)
        if not m2:
            continue
        # 短格式：rank.png：主界面 / jjc_logo.png：竞技场列表（…）
        names = _split_image_token(m2.group(1)) or [ensure_image_name(m2.group(1))]
        state, note = _split_state_and_note(m2.group(2) or "")
        for name in names:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(ImageEntry(
                image=name, role=ROLE_ID, state=state, note=note,
            ))
    return out


def _parse_picture_notes(block: str, existing: list[ImageEntry]) -> list[ImageEntry]:
    by_name = {ensure_image_name(e.image).lower(): e for e in existing if e.image.strip()}
    extra: list[ImageEntry] = []
    for ln in (block or "").splitlines():
        s = ln.strip()
        if not s or _PLACEHOLDER_RE.match(s):
            continue
        m = _IMG_LINE_RE.match(s)
        if not m:
            continue
        names = _split_image_token(m.group(1))
        if not names:
            names = [ensure_image_name(m.group(1))]
        rest = (m.group(2) or "").strip()
        role: str | None = None  # None = 未显式指定，不覆盖已有角色
        state = ""
        note = rest
        parts = [p.strip() for p in rest.split("；") if p.strip()]
        if parts and parts[0] in ("按钮", "标识", "其它"):
            role = LABEL_TO_ROLE.get(parts[0], ROLE_OTHER)
            parts = parts[1:]
        elif rest.startswith("按钮"):
            role = ROLE_BUTTON
            parts = [p.strip() for p in rest.split("；") if p.strip()][1:]
        if parts and parts[0].startswith("界面「") and parts[0].endswith("」"):
            state = parts[0][3:-1]
            parts = parts[1:]
        note = "；".join(parts)
        for name in names:
            key = name.lower()
            if key in by_name:
                e = by_name[key]
                # 仅当说明显式写了角色时才改；避免把场景标识降成「其它」
                if role is not None:
                    e.role = role
                if state and not e.state:
                    e.state = state
                if note and not e.note:
                    e.note = note
            else:
                extra.append(ImageEntry(
                    image=name,
                    role=role if role is not None else ROLE_OTHER,
                    state=state,
                    note=note,
                ))
                by_name[key] = extra[-1]
    return extra


def list_images(folder: str | Path) -> list[str]:
    """返回缓存中的图片文件名；需先 refresh_dir_image_map。"""
    return sorted(dir_image_map(folder).values(), key=str.lower)
