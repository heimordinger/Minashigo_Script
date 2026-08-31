"""按需帧观察：需求门闩 + max(Hz) + 空闲停截。

设计要点
- 无人 request_fps → 不跑后台截图（零常驻消耗）
- 多个需求并存时取 max，再夹硬顶 hard_cap
- 主动脚本默认由 TaskController 以 key=\"script\" 声明 floor（常见 10Hz）
- 被动任务可 request_fps(60, key=\"passive_...\")，停用时 release
- click / b_sleep 后 invalidate；match 前 ensure（脏帧则补截一帧）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

CaptureFn = Callable[[], Awaitable[Any]]
OnFrameFn = Callable[[Any], None]

# 脚本任务默认需求（TaskController 注入）；被动任务自行抬高
DEFAULT_SCRIPT_FPS = 10.0
# 全局硬顶（含被动 60 的声明上限）
HARD_CAP_FPS = 60.0


class FrameObserver:
    """单目标（Browser / Window）的帧需求表 + 可选后台 grabber。"""

    def __init__(
        self,
        capture: CaptureFn,
        *,
        hard_cap: float = HARD_CAP_FPS,
        name: str = "",
        on_frame: Optional[OnFrameFn] = None,
        get_frame: Optional[Callable[[], Any]] = None,
    ):
        self._capture = capture
        self._hard_cap = max(1.0, float(hard_cap))
        self._name = name or "obs"
        self._on_frame = on_frame
        self._get_frame = get_frame
        self._demands: dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stale = True
        self._frame_id = 0
        self._frame_ts = 0.0
        self._last_error: str = ""

    # ── 查询 ──

    @property
    def effective_fps(self) -> float:
        if not self._demands:
            return 0.0
        return min(max(self._demands.values()), self._hard_cap)

    @property
    def demands(self) -> dict[str, float]:
        return dict(self._demands)

    @property
    def frame_id(self) -> int:
        return self._frame_id

    @property
    def is_stale(self) -> bool:
        return self._stale

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def current_frame(self) -> Any:
        if self._get_frame is not None:
            return self._get_frame()
        return None

    # ── 需求 API ──

    async def request_fps(self, key: str, fps: float) -> float:
        """登记/更新某调用方的目标 Hz。fps<=0 等同 release。返回生效 Hz。"""
        key = str(key or "default").strip() or "default"
        fps = float(fps)
        async with self._lock:
            if fps <= 0:
                self._demands.pop(key, None)
            else:
                self._demands[key] = min(fps, self._hard_cap)
            await self._sync_loop_locked()
        return self.effective_fps

    async def release_fps(self, key: str) -> float:
        key = str(key or "default").strip() or "default"
        async with self._lock:
            self._demands.pop(key, None)
            await self._sync_loop_locked()
        return self.effective_fps

    async def release_all(self) -> None:
        async with self._lock:
            self._demands.clear()
            await self._sync_loop_locked()

    # ── 帧新鲜度 ──

    def invalidate(self) -> None:
        """标记缓存不可用（点击 / sleep 后）。不立刻截图。"""
        self._stale = True

    async def ensure_frame(self, *, force: bool = False) -> Any:
        """保证有可用帧：非 force 且未 stale 则复用；否则截一张。"""
        if not force and not self._stale:
            frame = self.current_frame()
            if frame is not None:
                return frame
        return await self.capture_once()

    async def wait_next_frame(self, timeout: float = 2.0) -> Any:
        """等到 frame_id 前进（grabber 或主动 capture）。"""
        start_id = self._frame_id
        deadline = time.monotonic() + max(0.05, float(timeout))
        while time.monotonic() < deadline:
            if self._frame_id > start_id and not self._stale:
                return self.current_frame()
            # 无 grabber 时自己补一帧
            if self.effective_fps <= 0:
                return await self.capture_once()
            self._wake.set()
            await asyncio.sleep(0.01)
        return await self.capture_once()

    async def capture_once(self) -> Any:
        """强制截一帧并清除 stale。"""
        frame = await self._capture()
        self._note_new_frame(frame)
        return frame

    def _note_new_frame(self, frame: Any) -> None:
        self._stale = False
        self._frame_id += 1
        self._frame_ts = time.time()
        if self._on_frame is not None:
            try:
                self._on_frame(frame)
            except Exception:
                pass

    # ── grabber 生命周期 ──

    async def _sync_loop_locked(self) -> None:
        fps = self.effective_fps
        if fps <= 0:
            await self._stop_loop_locked()
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._loop(),
                name=f"frame-obs:{self._name}",
            )
        self._wake.set()

    async def _stop_loop_locked(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _loop(self) -> None:
        try:
            while True:
                fps = self.effective_fps
                if fps <= 0:
                    return
                try:
                    await self.capture_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._last_error = str(e)
                    # 截图失败时略退，避免 tight-loop 打爆日志
                    await asyncio.sleep(min(1.0, 2.0 / max(fps, 1.0)))
                    continue
                fps = self.effective_fps
                if fps <= 0:
                    return
                interval = 1.0 / fps
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
