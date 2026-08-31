"""脚本生成 / 修订 / 试运行的本地材料归档，供后续分析与优化。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


SESSIONS_ROOT = Path(__file__).resolve().parent / "corpus" / "sessions"


def _slug(text: str, max_len: int = 24) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (text or "").strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:max_len] or "session")


class SessionArchive:
    """一次生成/试跑/修订流水线的材料目录。"""

    def __init__(self, root: Path | None = None):
        self.root = Path(root or SESSIONS_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self._session_id: str | None = None
        self._session_dir: Path | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    def begin(self, label: str = "run", *, reuse: bool = False) -> Path:
        if reuse and self._session_dir is not None:
            return self._session_dir
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        sid = f"{ts}_{_slug(label)}"
        self._session_id = sid
        self._session_dir = self.root / sid
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self.append_event("session_begin", label=label)
        return self._session_dir

    def ensure(self) -> Path:
        if self._session_dir is None:
            return self.begin("run")
        return self._session_dir

    def append_event(self, event: str, **fields: Any) -> None:
        d = self.ensure()
        line = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        with (d / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def write_text(self, name: str, text: str) -> Path:
        p = self.ensure() / name
        p.write_text(text or "", encoding="utf-8")
        return p

    def write_bytes(self, name: str, data: bytes) -> Path:
        p = self.ensure() / name
        p.write_bytes(data)
        return p

    def write_json(self, name: str, data: Any) -> Path:
        p = self.ensure() / name
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return p

    def merge_meta(self, patch: dict[str, Any]) -> None:
        d = self.ensure()
        path = d / "meta.json"
        base: dict[str, Any] = {}
        if path.is_file():
            try:
                base = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                base = {}
        base.update({k: v for k, v in patch.items() if v is not None})
        base.setdefault("session_id", self._session_id)
        base["updated_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def save_bgr_frame(self, name: str, frame) -> Optional[Path]:
        """保存 OpenCV BGR 帧为 PNG（兼容中文路径）。"""
        if frame is None:
            return None
        try:
            import cv2
            import numpy as np

            arr = frame if hasattr(frame, "shape") else None
            if arr is None:
                return None
            ok, buf = cv2.imencode(".png", np.asarray(arr))
            if not ok:
                return None
            return self.write_bytes(name, buf.tobytes())
        except Exception as e:
            self.append_event("screenshot_failed", name=name, error=str(e))
            return None

    def snapshot_generate(
        self,
        *,
        explanation: str,
        code: str,
        trajectory: list[dict],
        meta: dict[str, Any],
        validation_errors: list[str] | None = None,
    ) -> Path:
        d = self.ensure()
        self.write_text("explanation.txt", explanation)
        self.write_text("code_generated.py", code)
        self.write_json("trajectory.json", trajectory)
        if validation_errors:
            self.write_text(
                "validation_errors.txt",
                "\n".join(f"- {e}" for e in validation_errors),
            )
        self.merge_meta({**meta, "last_event": "generate_done"})
        self.append_event("generate_done", code_chars=len(code or ""))
        return d

    def snapshot_trial_end(
        self,
        *,
        status: str,
        trial_log: str,
        code: str,
        feedback_draft: str = "",
        meta: dict[str, Any],
        screenshot_name: str = "screenshot_trial_end.png",
        frame=None,
    ) -> Path:
        d = self.ensure()
        self.write_text("trial_log.txt", trial_log)
        self.write_text("code_at_trial.py", code)
        if feedback_draft.strip():
            self.write_text("feedback_draft.txt", feedback_draft)
        if frame is not None:
            self.save_bgr_frame(screenshot_name, frame)
        self.merge_meta({**meta, "last_event": "trial_end", "trial_status": status})
        self.append_event("trial_end", status=status, log_lines=len(trial_log.splitlines()))
        return d

    def snapshot_revise_start(
        self,
        *,
        feedback: str,
        code_before: str,
        explanation: str,
        trial_log: str,
        trajectory: list[dict],
        meta: dict[str, Any],
        screenshot_name: str = "screenshot_pre_revise.png",
        frame=None,
    ) -> Path:
        d = self.ensure()
        self.write_text("feedback.txt", feedback)
        self.write_text("code_pre_revise.py", code_before)
        self.write_text("explanation.txt", explanation)
        self.write_text("trial_log_pre_revise.txt", trial_log)
        self.write_json("trajectory_pre_revise.json", trajectory)
        if frame is not None:
            self.save_bgr_frame(screenshot_name, frame)
        self.merge_meta({**meta, "last_event": "revise_start"})
        self.append_event("revise_start", feedback_chars=len(feedback or ""))
        return d

    def snapshot_revise_done(
        self,
        *,
        code_after: str,
        summary: str,
        trajectory: list[dict],
        meta: dict[str, Any],
        validation_errors: list[str] | None = None,
        writeback_bullets: list[str] | None = None,
    ) -> Path:
        d = self.ensure()
        self.write_text("code_post_revise.py", code_after)
        self.write_text("revise_summary.txt", summary)
        self.write_json("trajectory_post_revise.json", trajectory)
        if validation_errors:
            self.write_text(
                "validation_post_revise.txt",
                "\n".join(f"- {e}" for e in validation_errors),
            )
        if writeback_bullets:
            self.write_text(
                "feedback_writeback.txt",
                "\n".join(writeback_bullets),
            )
        self.merge_meta({**meta, "last_event": "revise_done"})
        self.append_event("revise_done", summary_chars=len(summary or ""))
        return d
