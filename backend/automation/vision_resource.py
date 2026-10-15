"""Bounded, priority-aware resources for multi-instance vision automation."""

from __future__ import annotations

import asyncio
import os
import time
import weakref
import zlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


def _env_limit(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


_CPU = os.cpu_count() or 4
CAPTURE_LIMIT = _env_limit("MINASHIGO_CAPTURE_CONCURRENCY", 2)
HOT_MATCH_LIMIT = _env_limit(
    "MINASHIGO_MATCH_CONCURRENCY", 3 if _CPU >= 12 else 2
)
HEAVY_MATCH_LIMIT = _env_limit("MINASHIGO_HEAVY_VISION_CONCURRENCY", 1)


@dataclass
class _Waiter:
    priority: int
    seq: int
    owner: str
    queued_at: float
    future: asyncio.Future


class _PriorityLimiter:
    def __init__(self, limit: int, *, max_starve_s: float = 2.0):
        self.limit = max(1, int(limit))
        self.active = 0
        self._seq = 0
        self._waiters: list[_Waiter] = []
        self._active_owners: set[str] = set()
        self._last_owner = ""
        self._owner_burst = 0
        self._max_starve_s = max(0.05, float(max_starve_s))
        self._burst_limit = 2
        self._priority_bypass = 10

    async def acquire(self, priority: int, owner: str = "") -> float:
        started = time.monotonic()
        owner = str(owner or "")
        if (
            self.active < self.limit
            and not self._waiters
            and (not owner or owner not in self._active_owners)
        ):
            self.active += 1
            if owner:
                self._active_owners.add(owner)
            self._note_grant(owner)
            return 0.0
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._seq += 1
        self._waiters.append(
            _Waiter(int(priority), self._seq, owner, started, fut)
        )
        try:
            await fut
        except BaseException:
            fut.cancel()
            self._wake()
            raise
        return time.monotonic() - started

    def release(self, owner: str = "") -> None:
        self.active = max(0, self.active - 1)
        if owner:
            self._active_owners.discard(str(owner))
        self._wake()

    def _note_grant(self, owner: str) -> None:
        if owner and owner == self._last_owner:
            self._owner_burst += 1
        else:
            self._last_owner = owner
            self._owner_burst = 1 if owner else 0

    def _pick_waiter(self) -> _Waiter | None:
        now = time.monotonic()
        eligible = [
            item for item in self._waiters
            if not item.future.done()
            and (not item.owner or item.owner not in self._active_owners)
        ]
        if not eligible:
            return None

        # 硬性防饥饿：等待超过上限后，无视原始优先级，最老者先执行。
        starved = [item for item in eligible if now - item.queued_at >= self._max_starve_s]
        if starved:
            return min(starved, key=lambda item: (item.queued_at, item.seq))

        best = min(eligible, key=lambda item: (item.priority, item.seq))
        # 同账号最多连续突发两次；之后让优先级相差不超过一级的其他账号轮转。
        if self._owner_burst >= self._burst_limit and best.owner == self._last_owner:
            alternatives = [
                item for item in eligible
                if item.owner != self._last_owner
                and item.priority <= best.priority + self._priority_bypass
            ]
            if alternatives:
                best = min(alternatives, key=lambda item: (item.priority, item.seq))
        return best

    def _wake(self) -> None:
        self._waiters = [item for item in self._waiters if not item.future.done()]
        while self.active < self.limit and self._waiters:
            item = self._pick_waiter()
            if item is None:
                break
            self._waiters.remove(item)
            self.active += 1
            if item.owner:
                self._active_owners.add(item.owner)
            self._note_grant(item.owner)
            item.future.set_result(None)

    @property
    def queued(self) -> int:
        return sum(1 for item in self._waiters if not item.future.done())


@dataclass
class _Metric:
    count: int = 0
    wait_s: float = 0.0
    run_s: float = 0.0
    max_wait_s: float = 0.0


@dataclass
class _LoopResources:
    capture: _PriorityLimiter = field(
        default_factory=lambda: _PriorityLimiter(CAPTURE_LIMIT)
    )
    hot: _PriorityLimiter = field(
        default_factory=lambda: _PriorityLimiter(HOT_MATCH_LIMIT)
    )
    heavy: _PriorityLimiter = field(
        default_factory=lambda: _PriorityLimiter(HEAVY_MATCH_LIMIT)
    )
    metrics: dict[str, _Metric] = field(default_factory=dict)


_LOOPS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopResources]" = (
    weakref.WeakKeyDictionary()
)


def _resources() -> _LoopResources:
    loop = asyncio.get_running_loop()
    out = _LOOPS.get(loop)
    if out is None:
        out = _LoopResources()
        _LOOPS[loop] = out
    return out


@asynccontextmanager
async def vision_resource(
    *, kind: str = "hot", priority: int = 20, owner: str = ""
):
    """Acquire a bounded resource.

    Kinds: ``capture`` (cross-target capture), ``hot`` (normal narrow match),
    and ``wide``/``calibration`` (shared heavy single-flight lane).
    Lower priority numbers run first; click verification should use 0–5 and
    background observers 50+.
    """

    resources = _resources()
    normalized = str(kind or "hot").lower()
    limiter = (
        resources.capture
        if normalized == "capture"
        else resources.heavy
        if normalized in ("wide", "calibration", "heavy")
        else resources.hot
    )
    owner = str(owner or "")
    waited = await limiter.acquire(priority, owner)
    began = time.monotonic()
    try:
        yield
    finally:
        ran = time.monotonic() - began
        metric = resources.metrics.setdefault(normalized, _Metric())
        metric.count += 1
        metric.wait_s += waited
        metric.run_s += ran
        metric.max_wait_s = max(metric.max_wait_s, waited)
        limiter.release(owner)


def vision_resource_stats() -> dict:
    """Return compact current-loop scheduling metrics for runtime logs."""
    resources = _resources()
    return {
        "limits": {
            "capture": resources.capture.limit,
            "hot": resources.hot.limit,
            "heavy": resources.heavy.limit,
        },
        "queued": {
            "capture": resources.capture.queued,
            "hot": resources.hot.queued,
            "heavy": resources.heavy.queued,
        },
        "metrics": {
            key: {
                "count": value.count,
                "wait_s": round(value.wait_s, 3),
                "run_s": round(value.run_s, 3),
                "max_wait_s": round(value.max_wait_s, 3),
            }
            for key, value in resources.metrics.items()
        },
    }


def account_phase_delay(owner: str, *, cycle_s: float = 1.0, slots: int = 4) -> float:
    """Stable per-account start phase; avoids simultaneous task wake-up spikes."""
    slots = max(1, int(slots))
    slot = zlib.crc32(str(owner or "default").encode("utf-8")) % slots
    return float(cycle_s) * slot / slots
