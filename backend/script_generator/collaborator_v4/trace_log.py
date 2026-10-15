"""Durable per-main-turn diagnostics for the V4 form runtime."""

from __future__ import annotations

import hashlib
import json
import traceback
from pathlib import Path
from typing import Any

from core.logging.runtime_backup import RuntimeLogBackup


def _bounded(value: Any, *, limit: int = 12_000) -> Any:
    """Keep useful local evidence without allowing one event to explode."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… <截断 {len(value) - limit} 字符>"
    if isinstance(value, dict):
        return {str(k): _bounded(v, limit=limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = list(value)
        tail = [_bounded(x, limit=limit) for x in items[:80]]
        if len(items) > 80:
            tail.append(f"<另有 {len(items) - 80} 项>")
        return tail
    return _bounded(str(value), limit=limit)


class V4TraceLog:
    def __init__(self, *, session_id: str, user_text: str, root: Path | str | None = None) -> None:
        root = Path(root) if root is not None else Path(__file__).resolve().parents[3] / "logs" / "runtime"
        self._backup = RuntimeLogBackup(root, keep_runs=12)
        self.path = self._backup.begin_run(label="collab_v4", account=session_id or "collab_v4")
        self.session_id = str(session_id or "")
        self.user_hash = hashlib.sha256(str(user_text or "").encode("utf-8")).hexdigest()[:16]
        self.event(
            "trace_started",
            session_id=self.session_id,
            user_text=str(user_text or ""),
            user_text_hash=self.user_hash,
            log_path=str(self.path or ""),
        )

    def event(self, event: str, *, level: str = "INFO", **fields: Any) -> None:
        body = {"event": str(event), **{k: _bounded(v) for k, v in fields.items()}}
        self._backup.record(
            account=self.session_id or "collab_v4",
            level=level,
            source="collab_v4",
            message=json.dumps(body, ensure_ascii=False, default=str),
        )

    def exception(self, event: str, exc: BaseException, **fields: Any) -> None:
        self.event(
            event,
            level="ERROR",
            error_type=type(exc).__name__,
            error=str(exc),
            traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            **fields,
        )
