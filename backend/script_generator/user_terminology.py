"""用户个人术语词典（设计 §23）。

**本地只有一个用户**，不建设多用户系统。三层语义必须分清：

1. **系统标准语义**：``click_offset`` / ``cursor_wiggle`` / ``retry_delay_jitter`` /
   ``gesture_path_jitter`` / ``viewport_motion`` —— API、事件、校验与代码只用这套无歧义标识；
2. **用户个人词典**：某位用户通常怎么用自然语言某个词（例如本用户的"抖动"默认指
   ``click_offset``）；它是**术语偏好**，不是全局定义，也不是硬规则；
3. **任务上下文解释**：具体句子、参考脚本、当前任务可以覆盖个人默认。

硬约束（设计 §23）：

- 词义偏好**不能授予 API 权限**（本模块只回答"这句话指哪个概念"，不参与契约批准）；
- 个人词典**不得写进全局提示词**（调用方按需查询，不要把词典内容拼进 system prompt）；
- 用户明确改口 → 新版本生效、旧版本失效，但**保留审计历史**；
- LLM 自行归纳只能 ``propose_claim``（低置信、非权威），不能直接成为高置信个人偏好。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.path import PROJECT_ROOT

#: 系统标准语义（无歧义标识；代码/事件/校验只用这些）。
STANDARD_MEANINGS: tuple[str, ...] = (
    "click_offset",
    "cursor_wiggle",
    "retry_delay_jitter",
    "gesture_path_jitter",
    "viewport_motion",
)

#: 来源标记。
SOURCE_USER_EXPLICIT = "user_explicit_definition"
SOURCE_MODEL_INFERENCE = "model_inference"
SOURCE_DEFAULT_PROFILE = "local_profile_default"

#: 解释来源（按设计 §23.3 的优先顺序）。
FROM_SENTENCE = "sentence"
FROM_SCRIPT = "script_evidence"
FROM_CONTEXT = "task_context"
FROM_USER_DICTIONARY = "user_dictionary"
FROM_ASK = "needs_confirmation"

DEFAULT_SCOPE = "local_user"

STORE = PROJECT_ROOT / "user_data" / "collaborator_v3" / "user_terminology.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TermResolution:
    """一次术语解释的结果（结构化，可写进逻辑草稿供用户纠正）。"""

    term: str
    meaning: str
    source: str
    confidence: float = 1.0
    requires_confirmation: bool = False
    question: str = ""
    note: str = ""
    exceptions: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.requires_confirmation

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["exceptions"] = list(self.exceptions)
        return data

    def draft_line(self) -> str:
        """写进逻辑草稿的无歧义描述（让用户有一次纠正机会）。"""
        return "将「{}」按你常用的含义处理为 {}：{}".format(
            self.term, self.meaning, self.note or ""
        ).strip()


@dataclass
class TermEntry:
    term: str
    preferred_meaning: str
    definition: str = ""
    scope: str = DEFAULT_SCOPE
    confidence: float = 0.9
    source: str = SOURCE_DEFAULT_PROFILE
    source_event_id: str = ""
    exceptions: tuple[str, ...] = ()
    version: int = 1
    active: bool = True
    created_at: str = ""
    updated_at: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)


class TerminologyStore:
    """单用户词典（JSON 落盘；无网络、无多用户）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or STORE)
        self._entries: dict[str, TermEntry] = {}
        self.load()

    # ── 持久化 ──

    def load(self) -> None:
        self._entries = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for term, payload in (raw.get("entries") or {}).items():
            if not isinstance(payload, dict):
                continue
            data = dict(payload)
            data["exceptions"] = tuple(data.get("exceptions") or ())
            data.setdefault("term", term)
            try:
                self._entries[str(term)] = TermEntry(**data)
            except TypeError:
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "entries": {k: asdict(v) for k, v in self._entries.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ── 写入 ──

    def record_user_definition(
        self,
        term: str,
        meaning: str,
        *,
        definition: str = "",
        exceptions: tuple[str, ...] = (),
        source: str = SOURCE_USER_EXPLICIT,
        source_event_id: str = "",
        confidence: float = 0.95,
    ) -> TermEntry:
        """用户**明确**说明自己的词义 → 直接保存为个人偏好（新版本，旧版本归档）。"""
        if meaning not in STANDARD_MEANINGS:
            raise ValueError("unknown standard meaning: {!r}".format(meaning))
        key = str(term or "").strip()
        if not key:
            raise ValueError("term must not be empty")
        previous = self._entries.get(key)
        history = list(previous.history) if previous else []
        version = 1
        if previous:
            archived = asdict(previous)
            archived["active"] = False
            history.append(archived)
            version = int(previous.version) + 1
        entry = TermEntry(
            term=key,
            preferred_meaning=meaning,
            definition=definition,
            scope=DEFAULT_SCOPE,
            confidence=float(confidence),
            source=source,
            source_event_id=source_event_id,
            exceptions=tuple(exceptions),
            version=version,
            active=True,
            created_at=(previous.created_at if previous else _now()),
            updated_at=_now(),
            history=history,
        )
        self._entries[key] = entry
        self.save()
        return entry

    def propose_meaning(self, term: str, meaning: str, *, note: str = "") -> TermEntry:
        """LLM 自行归纳 → **低置信、非权威**，且不覆盖已有的用户明确偏好。"""
        key = str(term or "").strip()
        existing = self._entries.get(key)
        if existing is not None and existing.source == SOURCE_USER_EXPLICIT:
            return existing  # 用户说过就是用户说了算
        if meaning not in STANDARD_MEANINGS:
            raise ValueError("unknown standard meaning: {!r}".format(meaning))
        entry = TermEntry(
            term=key,
            preferred_meaning=meaning,
            definition=note,
            scope=DEFAULT_SCOPE,
            confidence=0.4,
            source=SOURCE_MODEL_INFERENCE,
            version=(int(existing.version) + 1) if existing else 1,
            active=True,
            created_at=(existing.created_at if existing else _now()),
            updated_at=_now(),
            history=([asdict(existing)] if existing else []),
        )
        self._entries[key] = entry
        self.save()
        return entry

    def get(self, term: str) -> TermEntry | None:
        return self._entries.get(str(term or "").strip())

    def entries(self) -> dict[str, TermEntry]:
        return dict(self._entries)

    # ── 解释 ──

    def resolve(
        self,
        term: str,
        *,
        sentence_meaning: str = "",
        script_meaning: str = "",
        context_meaning: str = "",
    ) -> TermResolution:
        """按设计 §23.3 的顺序解释一个模糊术语。

        顺序：① 本句已明确 ② 参考脚本已定义 ③ 任务上下文只允许一种含义
        ④ 用户个人词典默认 ⑤ 仍有实质歧义 → 询问用户。
        """
        key = str(term or "").strip()
        for meaning, source in (
            (sentence_meaning, FROM_SENTENCE),
            (script_meaning, FROM_SCRIPT),
            (context_meaning, FROM_CONTEXT),
        ):
            if meaning:
                return TermResolution(
                    term=key,
                    meaning=str(meaning),
                    source=source,
                    confidence=1.0,
                    note="由{}确定".format(source),
                )
        entry = self._entries.get(key)
        if entry is not None and entry.active:
            return TermResolution(
                term=key,
                meaning=entry.preferred_meaning,
                source=FROM_USER_DICTIONARY,
                confidence=float(entry.confidence),
                note=entry.definition,
                exceptions=tuple(entry.exceptions),
            )
        return TermResolution(
            term=key,
            meaning="",
            source=FROM_ASK,
            confidence=0.0,
            requires_confirmation=True,
            question=(
                "你说的「{}」是指点击坐标小偏移（click_offset）、只移动鼠标"
                "（cursor_wiggle），还是等待时间随机化（retry_delay_jitter）？".format(key)
            ),
        )


def default_store(scope_id: str = DEFAULT_SCOPE, *, path: str | Path | None = None) -> TerminologyStore:
    """取当前**本地用户**的词典（首次调用会种入本用户的默认偏好）。

    设计 §23.1：当前用户日常所说的"抖动"默认是 ``click_offset``。
    这是**用户作用域**的偏好，不是全局定义。
    """
    store = TerminologyStore(path)
    if store.get("抖动") is None:
        store.record_user_definition(
            "抖动",
            "click_offset",
            definition="点击时增加小幅随机坐标偏移，使点击位置产生自然变化",
            exceptions=(
                "讨论滑动轨迹时可能指 gesture_path_jitter",
                "讨论等待间隔时可能指 retry_delay_jitter",
                "讨论页面/窗口晃动时指 viewport_motion",
                "明确说「不要点击，只抖动鼠标」时指 cursor_wiggle",
            ),
            source=SOURCE_DEFAULT_PROFILE,
            confidence=0.9,
        )
    return store


def grants_api_permission(_resolution: TermResolution) -> bool:
    """词义偏好**永远不能**授予 API 权限（设计 §23.2）。调用方不得据此放行任何能力。"""
    return False
