"""Local before/after measurement for visual-agent actions.

The LLM never decides whether a repeated action had an effect.  This module
compares stable frames, tracks no-effect streaks per scene/control, and keeps
diagnostic frames only in a per-run temporary directory.
"""
from __future__ import annotations

from collections import deque
import shutil
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _gray(frame: np.ndarray, size: tuple[int, int] = (320, 180)) -> np.ndarray:
    value = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return cv2.resize(value, size, interpolation=cv2.INTER_AREA)


def _difference(before: np.ndarray, after: np.ndarray) -> dict[str, float | bool]:
    old, new = _gray(before), _gray(after)
    diff = cv2.absdiff(old, new)
    changed_ratio = float(np.mean(diff > 18))
    mean_delta = float(np.mean(diff)) / 255.0
    return {
        "changed_ratio": round(changed_ratio, 4),
        "mean_delta": round(mean_delta, 4),
        "changed": changed_ratio >= 0.004 or mean_delta >= 0.006,
    }


def _crop(frame: np.ndarray, region: object) -> np.ndarray | None:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(round(float(value))) for value in region)
    except (TypeError, ValueError):
        return None
    height, width = frame.shape[:2]
    x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
    y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return frame[y1:y2, x1:x2]


class ActionEffectTracker:
    """Classify repeated actions using local visual evidence."""

    def __init__(self, *, no_effect_limit: int = 2, ring_size: int = 5):
        self.no_effect_limit = max(2, int(no_effect_limit))
        self._streaks: dict[str, int] = {}
        self._attempts: dict[str, int] = {}
        self._recent = deque(maxlen=max(3, int(ring_size)))
        self._tmpdir = Path(tempfile.mkdtemp(prefix="minashigo-visual-effect-"))

    @staticmethod
    def action_key(action: dict[str, Any], frame_size: tuple[int, int]) -> str:
        width, height = frame_size
        direction = str(action.get("option_direction") or "").strip().lower()
        region = str(action.get("target_region") or "").strip().lower()
        container = str(action.get("target_container") or "").strip().lower()
        if direction in {"left", "right"}:
            # Selector arrows retain identity across changing option labels and
            # minor coordinate jitter; LLM prose must not reset no-effect streaks.
            return f"ordered-selector|{container or region or 'main_content'}|{direction}"
        state = str(action.get("current_state") or "unknown").strip().lower()
        target = str(action.get("target") or action.get("message") or action.get("action")).strip().lower()
        try:
            x, y = float(action["x"]), float(action["y"])
            position = f"{min(4, max(0, int(5*x/max(1, width))))}-{min(4, max(0, int(5*y/max(1, height))))}"
        except (KeyError, TypeError, ValueError):
            position = ""
        return f"{state}|{target}|{position}"

    def evaluate(self, *, before: np.ndarray, after: np.ndarray,
                 action: dict[str, Any], stable: bool) -> dict[str, Any]:
        key = self.action_key(action, (before.shape[1], before.shape[0]))
        whole = _difference(before, after)
        before_region = _crop(before, action.get("progress_region"))
        after_region = _crop(after, action.get("progress_region"))
        content = (
            _difference(before_region, after_region)
            if before_region is not None and after_region is not None
            else dict(whole)
        )
        self._attempts[key] = int(self._attempts.get(key) or 0) + 1
        changed = bool(content["changed"])
        if not stable:
            classification = "unsettled_after_action"
            self._streaks[key] = 0
        elif changed:
            self._streaks[key] = 0
            classification = "productive_repeat" if self._attempts[key] > 1 else "effective_change"
        elif bool(whole["changed"]):
            # The proposed ROI may be incomplete or background animation may still
            # be active.  Never promote this ambiguous case to no_effect.
            self._streaks[key] = 0
            classification = "visual_change_outside_region"
        else:
            self._streaks[key] = int(self._streaks.get(key) or 0) + 1
            classification = (
                "confirmed_no_effect"
                if self._streaks[key] >= self.no_effect_limit
                else "suspected_no_effect"
            )
            self._save_probe(key, before, after, self._streaks[key])
        result = {
            "action_key": key,
            "classification": classification,
            "stable_after_action": bool(stable),
            "whole_frame": whole,
            "content_region": content,
            "consecutive_no_effects": int(self._streaks.get(key) or 0),
            "attempt_count": self._attempts[key],
            "same_action_forbidden": classification == "confirmed_no_effect",
        }
        self._recent.append(result)
        return result

    def _save_probe(self, key: str, before: np.ndarray, after: np.ndarray, streak: int) -> None:
        stem = str(abs(hash(key)))
        cv2.imwrite(str(self._tmpdir / f"{stem}-{streak}-before.jpg"), before)
        cv2.imwrite(str(self._tmpdir / f"{stem}-{streak}-after.jpg"), after)

    def close(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)
