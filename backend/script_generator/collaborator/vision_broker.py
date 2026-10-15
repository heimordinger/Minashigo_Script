"""V2 侧视觉证据适配器：L1 缓存 + 遥测落点（设计 §5 / Phase B2）。

Phase B2 的规则只有一份（``visual_evidence.VisualEvidenceBroker``）；这里只提供
"V2 会话怎么存"的适配：

- **L1**：``session.artifacts["vision_cache"]``（键 = 证据指纹）；
- **遥测**：``session.artifacts["vision_telemetry"]``（最近 40 条）。

``vision_cache`` 在 Phase B2 之前用的是"路径+mtime+模型+整张任务单"的键；
现在换成指纹键 —— 老条目不会再被命中（也不会被误当成有效证据），
随 24 条上限自然淘汰。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

L1_KEY = "vision_cache"
TELEMETRY_KEY = "vision_telemetry"
TELEMETRY_LIMIT = 40


class SessionVisionCache:
    """``session.artifacts["vision_cache"]`` 的 L1 适配器。"""

    label = "session_l1"

    def __init__(self, session: Any) -> None:
        self.session = session

    def _store(self) -> dict[str, Any]:
        if not isinstance(getattr(self.session, "artifacts", None), dict):
            self.session.artifacts = {}
        cache = self.session.artifacts.get(L1_KEY)
        if not isinstance(cache, dict):
            cache = {}
            self.session.artifacts[L1_KEY] = cache
        return cache

    def get(self, fingerprint: str) -> Optional[dict[str, Any]]:
        value = self._store().get(str(fingerprint or ""))
        return dict(value) if isinstance(value, Mapping) else None

    def put(self, fingerprint: str, value: Mapping[str, Any]) -> None:
        cache = self._store()
        cache[str(fingerprint or "")] = dict(value)
        if len(cache) > 24:
            for old_key in list(cache)[:-24]:
                cache.pop(old_key, None)

    def clear(self, *, fingerprints: Iterable[str] = (), keep: int = 24) -> int:
        cache = self._store()
        if fingerprints:
            removed = 0
            for key in list(fingerprints):
                if cache.pop(str(key), None) is not None:
                    removed += 1
            return removed
        removed = len(cache)
        cache.clear()
        return removed

    def entries(self) -> dict[str, Any]:
        return dict(self._store())


class SessionVisionTelemetry:
    """把 Broker 的遥测事件记进会话（内存里最近 40 条，够排障用）。"""

    def __init__(self, session: Any, *, limit: int = TELEMETRY_LIMIT) -> None:
        self.session = session
        self.limit = int(limit)

    def __call__(self, event: Mapping[str, Any]) -> None:
        if not isinstance(getattr(self.session, "artifacts", None), dict):
            self.session.artifacts = {}
        items = self.session.artifacts.get(TELEMETRY_KEY)
        if not isinstance(items, list):
            items = []
            self.session.artifacts[TELEMETRY_KEY] = items
        items.append(dict(event))
        del items[: max(0, len(items) - self.limit)]
