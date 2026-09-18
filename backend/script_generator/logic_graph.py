"""LogicGraph：协作画布与试跑游标 / 生成校验的契约。

`label` 给人看；`bind` 给 codegen / checklist；`id` 对齐 `note_state`。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


LOGIC_GRAPH_VERSION = 1


@dataclass
class StepBind:
    images: list[str] = field(default_factory=list)
    helpers: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "images": list(self.images),
            "helpers": list(self.helpers),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "StepBind":
        if not isinstance(data, dict):
            return cls()
        return cls(
            images=[str(x) for x in (data.get("images") or [])],
            helpers=[str(x) for x in (data.get("helpers") or [])],
            notes=str(data.get("notes") or ""),
        )


@dataclass
class LogicStep:
    id: str
    label: str
    hint: str = ""
    when: str = ""
    else_goto: str = ""
    do: list[str] = field(default_factory=list)
    bind: StepBind = field(default_factory=StepBind)
    # UI：draft | preview | acked | running | failed
    status: str = "draft"
    # 用户可改的少数参数（timeout / sleep），单位秒；内部键保持英文
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "hint": self.hint,
            "when": self.when,
            "else": self.else_goto,
            "do": list(self.do),
            "bind": self.bind.to_dict(),
            "status": self.status,
            "params": dict(self.params or {}),
        }

    def to_ui_card(self) -> dict[str, Any]:
        """协作页 LogicStepCard 所需字段。"""
        hint = self.hint
        if not hint and self.when:
            hint = self.when
        images = list(self.bind.images or []) if self.bind else []
        return {
            "id": self.id,
            "label": self.label,
            "hint": hint,
            "status": self.status or "draft",
            "images": images,
            "bind_images": images,
            "params": dict(self.params or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogicStep":
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            hint=str(data.get("hint") or ""),
            when=str(data.get("when") or ""),
            else_goto=str(data.get("else") or data.get("else_goto") or ""),
            do=[str(x) for x in (data.get("do") or [])],
            bind=StepBind.from_dict(data.get("bind") if isinstance(data.get("bind"), dict) else None),
            status=str(data.get("status") or "draft"),
            params=dict(data.get("params") or {}) if isinstance(data.get("params"), dict) else {},
        )


@dataclass
class LogicGraph:
    title: str = ""
    architecture: str = "scene_driven"
    steps: list[LogicStep] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    version: int = LOGIC_GRAPH_VERSION
    meta: dict[str, Any] = field(default_factory=dict)

    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps if s.id]

    def get(self, step_id: str) -> Optional[LogicStep]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def mark(self, step_id: str, status: str) -> None:
        s = self.get(step_id)
        if s is not None:
            s.status = status

    def mark_all(self, status: str) -> None:
        for s in self.steps:
            s.status = status

    def to_ui_steps(self) -> list[dict[str, Any]]:
        return [s.to_ui_card() for s in self.steps]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "title": self.title,
            "architecture": self.architecture,
            "steps": [s.to_dict() for s in self.steps],
            "edges": [[a, b] for a, b in self.edges],
            "meta": dict(self.meta),
        }

    def to_intro_lines(self) -> list[str]:
        """自动介绍草稿（给人 / 经典轨）。"""
        lines = [
            f"# {self.title or '协作脚本'}",
            "",
            f"架构：{self.architecture}",
            "",
            "步骤：",
        ]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i}. {s.label}")
            if s.hint:
                lines.append(f"   （{s.hint}）")
            if s.bind.helpers:
                lines.append(f"   @helper {', '.join(s.bind.helpers)}")
            if s.bind.images:
                lines.append(f"   素材：{', '.join(s.bind.images)}")
        return lines

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "LogicGraph":
        if not isinstance(data, dict):
            return cls()
        edges_raw = data.get("edges") or []
        edges: list[tuple[str, str]] = []
        for e in edges_raw:
            if isinstance(e, (list, tuple)) and len(e) >= 2:
                edges.append((str(e[0]), str(e[1])))
        steps = [
            LogicStep.from_dict(s)
            for s in (data.get("steps") or [])
            if isinstance(s, dict)
        ]
        return cls(
            title=str(data.get("title") or ""),
            architecture=str(data.get("architecture") or "scene_driven"),
            steps=steps,
            edges=edges,
            version=int(data.get("version") or LOGIC_GRAPH_VERSION),
            meta=dict(data.get("meta") or {}),
        )

    @classmethod
    def from_ui_steps(
        cls,
        steps: list[dict[str, Any]],
        *,
        title: str = "",
        architecture: str = "scene_driven",
    ) -> "LogicGraph":
        logic_steps = [LogicStep.from_dict(s) for s in steps]
        edges = []
        for a, b in zip(logic_steps, logic_steps[1:]):
            if a.id and b.id:
                edges.append((a.id, b.id))
        return cls(title=title, architecture=architecture, steps=logic_steps, edges=edges)


_TIMEOUT_RE = re.compile(r"timeout\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_SLEEP_RE = re.compile(r"(?:^|\s)sleep\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_VERB_ZH = (
    ("wait", "等待"),
    ("click", "点击"),
    ("sleep", "停顿"),
    ("match", "匹配"),
    ("find", "查找"),
    ("swipe", "滑动"),
    ("press", "按键"),
)


def _num_token(value: Any) -> str:
    n = float(value)
    if n == int(n):
        return str(int(n))
    return f"{n:g}"


def parse_step_params(label: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """从步骤标题抽出 timeout / sleep（秒）。已写入的 params 优先于标题里的旧数字。"""
    out: dict[str, Any] = {}
    text = label or ""
    m = _TIMEOUT_RE.search(text)
    if m:
        out["timeout"] = int(float(m.group(1))) if float(m.group(1)) == int(float(m.group(1))) else float(m.group(1))
    m2 = _SLEEP_RE.search(text)
    if m2:
        raw = float(m2.group(1))
        out["sleep"] = int(raw) if raw == int(raw) else raw
    stored = params or {}
    for key in ("timeout", "sleep"):
        if key not in stored or stored[key] in (None, ""):
            continue
        try:
            raw = float(stored[key])
        except (TypeError, ValueError):
            continue
        out[key] = int(raw) if raw == int(raw) else raw
    return out


def display_action(label: str) -> str:
    """给人看的动作句：去掉 timeout=，英文动词换成中文。数字不写在这句里。"""
    text = _TIMEOUT_RE.sub("", label or "")
    text = re.sub(r"\s+", " ", text).strip()
    low = text.lower()
    for en, zh in _VERB_ZH:
        if low == en or low.startswith(en + " "):
            rest = text[len(en):].strip()
            if en == "sleep":
                rest = re.sub(r"^\d+(?:\.\d+)?\s*", "", rest).strip()
            if not rest:
                return zh
            if rest.startswith("「"):
                return f"{zh}{rest}"
            return f"{zh} {rest}"
    return text


def param_for_ui(params: Optional[dict[str, Any]], *, label: str = "") -> Optional[dict[str, Any]]:
    """只开放用户能改的秒数。中文是展示，key 仍是 timeout / sleep。"""
    p = params or {}
    low = (label or "").strip().lower()
    is_sleep = low.startswith("sleep") or low.startswith("停") or low.startswith("休眠")
    if is_sleep and "sleep" in p:
        return {"key": "sleep", "label": "停", "unit": "秒", "value": p["sleep"]}
    if "timeout" in p:
        return {"key": "timeout", "label": "最多等", "unit": "秒", "value": p["timeout"]}
    if "sleep" in p:
        return {"key": "sleep", "label": "停", "unit": "秒", "value": p["sleep"]}
    return None


def apply_step_param(step: LogicStep, key: str, value: Any) -> None:
    """改秒数：params 记英文键，并只替换标题里对应的 timeout= / sleep 数字。"""
    key = str(key or "").strip()
    if key not in ("timeout", "sleep"):
        return
    try:
        num = float(value)
    except (TypeError, ValueError):
        return
    num = max(0.0, min(600.0, num))
    stored: Any = int(num) if num == int(num) else num
    token = _num_token(stored)
    step.params[key] = stored
    label = step.label or ""
    if key == "timeout":
        if _TIMEOUT_RE.search(label):
            label = _TIMEOUT_RE.sub(f"timeout={token}", label, count=1)
        else:
            label = f"{label} timeout={token}".strip()
    elif _SLEEP_RE.search(label):
        label = re.sub(
            r"((?:^|\s)sleep\s+)\d+(?:\.\d+)?",
            rf"\g<1>{token}",
            label,
            count=1,
            flags=re.IGNORECASE,
        )
    elif re.match(r"^\s*sleep\b", label, re.IGNORECASE):
        label = f"sleep {token}"
    else:
        label = f"sleep {token}"
    step.label = re.sub(r"\s+", " ", label).strip()


def step_line_for_model(step: LogicStep) -> str:
    """给模型 / 生成用的一行：保证 timeout / sleep 仍是英文键，不写「最多等」。"""
    label = str(getattr(step, "label", "") or "")
    params = parse_step_params(label, getattr(step, "params", None))
    if "timeout" in params and not _TIMEOUT_RE.search(label):
        label = f"{label} timeout={_num_token(params['timeout'])}".strip()
    if "sleep" in params and not _SLEEP_RE.search(label):
        if re.match(r"^\s*sleep\b", label, re.IGNORECASE):
            label = re.sub(
                r"^\s*sleep\b\s*",
                f"sleep {_num_token(params['sleep'])} ",
                label,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
        else:
            label = f"{label} sleep {_num_token(params['sleep'])}".strip()
    return re.sub(r"\s+", " ", label).strip()
