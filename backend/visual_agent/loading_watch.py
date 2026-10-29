"""Learn how long one application needs to load, then wait it out locally.

A black frame is not evidence of a hang: on a slow network the game can stay
black for a minute or two while assets download, with no repaint at all.  This
module keeps at most one "loading episode" per run, waits inside it without
spending model calls, and records the measured duration per application scope so
later runs start from an observed distribution instead of a hard-coded number.

The distinction that matters is activity, not elapsed time: a black screen that
still shows a spinner, a progress bar or any frame change stays alive, while a
screen that has been pixel-identical for longer than this application's historical
loading time is allowed to be reported as suspicious.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any, Callable

#: Grace window used before any measurement exists for an application.
DEFAULT_GRACE_SECONDS = 120.0
#: Never give up sooner than this, even if history says loading is quick.
MIN_GRACE_SECONDS = 45.0
#: Never wait longer than this on one episode.
MAX_GRACE_SECONDS = 300.0
#: Local (token-free) backoff between observations inside an episode.
BACKOFF_SECONDS = (1.0, 1.5, 2.0, 3.0, 4.0)
#: How many measured episodes to keep per application.
MAX_SAMPLES = 40
BLACK_RATIO_THRESHOLD = 0.985


class LoadProfile:
    """Per-application record of how long loading actually took."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._samples: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for scope, values in raw.items():
            if not isinstance(values, list):
                continue
            cleaned = [
                round(float(value), 1)
                for value in values
                if isinstance(value, (int, float)) and 1.0 <= float(value) <= 900.0
            ]
            if cleaned:
                self._samples[str(scope)] = cleaned[-MAX_SAMPLES:]

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._samples, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def record(self, scope: str, seconds: float) -> None:
        value = float(seconds)
        if not 1.0 <= value <= 900.0:
            return
        samples = self._samples.setdefault(str(scope), [])
        samples.append(round(value, 1))
        del samples[:-MAX_SAMPLES]
        self._save()

    def stats(self, scope: str) -> dict[str, Any]:
        samples = self._samples.get(str(scope)) or []
        if not samples:
            return {"samples": 0, "p50": None, "p95": None}
        ordered = sorted(samples)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return {
            "samples": len(ordered),
            "p50": round(float(statistics.median(ordered)), 1),
            "p95": round(float(ordered[index]), 1),
        }

    def grace_for(self, scope: str) -> float:
        """Observed p95 with a safety factor, or the default before any history."""
        stats = self.stats(scope)
        if not stats["samples"]:
            return DEFAULT_GRACE_SECONDS
        return min(MAX_GRACE_SECONDS, max(MIN_GRACE_SECONDS, float(stats["p95"]) * 1.5))


class LoadingWatch:
    """Track one black/loading episode and say whether waiting is still sane."""

    def __init__(
        self,
        profile: LoadProfile,
        scope: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._profile = profile
        self._scope = str(scope or "unknown")
        self._clock = clock
        self._open = False
        self._started = 0.0
        self._last_activity = 0.0
        self._waits = 0
        self._exhausted_announced = False

    @property
    def scope(self) -> str:
        return self._scope

    def observe(self, *, black_ratio: float, changed: bool) -> dict[str, Any]:
        """Feed one observation and return the loading state for this step."""
        now = self._clock()
        is_black = float(black_ratio) >= BLACK_RATIO_THRESHOLD

        if not is_black:
            finished = None
            if self._open:
                finished = round(max(0.0, now - self._started), 1)
                self._profile.record(self._scope, finished)
                self._open = False
                self._waits = 0
                self._exhausted_announced = False
            return {
                "phase": "none",
                "waited": 0.0,
                "just_entered": False,
                "just_finished": finished,
                "activity": bool(changed),
                "grace": self._profile.grace_for(self._scope),
                "stalled": 0.0,
                "exhausted": False,
                "announce_exhausted": False,
                "backoff": 0.0,
                "waits": 0,
                "history": self._profile.stats(self._scope),
            }

        just_entered = False
        if not self._open:
            self._open = True
            self._started = now
            self._last_activity = now
            self._waits = 0
            self._exhausted_announced = False
            just_entered = True
        elif changed:
            # A spinner, a progress bar, a logo flash: the application is alive.
            self._last_activity = now

        grace = self._profile.grace_for(self._scope)
        stalled = max(0.0, now - self._last_activity)
        exhausted = stalled >= grace
        announce = exhausted and not self._exhausted_announced
        self._exhausted_announced = exhausted
        backoff = BACKOFF_SECONDS[min(self._waits, len(BACKOFF_SECONDS) - 1)]
        self._waits += 1
        return {
            "phase": "loading",
            "waited": round(max(0.0, now - self._started), 1),
            "just_entered": just_entered,
            "just_finished": None,
            "activity": bool(changed),
            "grace": grace,
            "stalled": round(stalled, 1),
            "exhausted": exhausted,
            "announce_exhausted": announce,
            "backoff": backoff,
            "waits": self._waits,
            "history": self._profile.stats(self._scope),
        }