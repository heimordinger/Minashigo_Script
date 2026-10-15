"""Bridge the V4 MVP to the existing collaborative GUI worker contract."""

from __future__ import annotations

import asyncio
import traceback
from typing import Any


def run_v4_gui_turn(params: dict[str, Any]) -> tuple[str, dict, dict, int, int]:
    from backend.script_generator.collaborator.session import CollabSession
    from backend.script_generator.collaborator_v4.runtime import run_v4_turn
    from backend.script_generator.collaborator_v4.trace_log import V4TraceLog

    session = CollabSession.from_dict(dict(params.get("session") or {}))
    user_text = next(
        (str(m.get("text") or "") for m in reversed(session.messages or []) if str(m.get("role") or "") == "user"),
        "",
    )
    trace_log = V4TraceLog(session_id=session.ensure_id(), user_text=user_text)
    try:
        visible, meta, inp, out = asyncio.run(
            run_v4_turn(
                provider=str(params.get("provider") or ""),
                api_key=str(params.get("api_key") or ""),
                model=str(params.get("model") or ""),
                api_endpoint=params.get("api_endpoint"),
                max_tokens=params.get("max_tokens"),
                session=session,
                permissions=dict(params.get("permissions") or {}),
                vision_assist=params.get("vision_assist"),
                on_status=params.get("on_status"),
                on_partial=params.get("on_partial"),
                trace_log=trace_log,
                supplement_box=params.get("supplement_box"),
            )
        )
    except Exception as exc:
        trace_log.exception("main_turn_failed", exc, session_id=session.session_id)
        if not isinstance(session.artifacts, dict):
            session.artifacts = {}
        session.artifacts["v4_last_error"] = {
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "log_path": str(trace_log.path or ""),
        }
        suffix = f"（V4 日志：{trace_log.path}）" if trace_log.path else ""
        raise RuntimeError(f"{exc}{suffix}") from exc
    return visible, meta, session.to_dict(), inp, out
