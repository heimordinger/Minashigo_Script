"""
UserWindow —— Win32Target 的脚本层包装，接口与 UserBrowser 对齐。

脚本中统一用 async/await 调用，跟 UserBrowser 写法一致。
"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.logging.events import LogLevel
from typing import Union

from .win32_target import Win32Target
from .stuck_guard import StuckGuard
from .frame_observer import FrameObserver, HARD_CAP_FPS


def human_offset(pianyi: Union[None, int, tuple[int, int]] = None) -> tuple[int, int]:
    """随机偏移，兼容 UserBrowser 的同名函数。"""
    if pianyi is None:
        r = 6
        return random.randint(-r, r), random.randint(-r, r)
    if isinstance(pianyi, int):
        return random.randint(-pianyi, pianyi), random.randint(-pianyi, pianyi)
    if isinstance(pianyi, tuple) and len(pianyi) == 2:
        return pianyi
    raise TypeError(f"非法 pianyi 类型: {pianyi!r}")


class UserWindow:
    """Win32Target 的脚本层包装，接口与 UserBrowser 对齐。

    脚本中统一用 async/await:
        await win.click(x, y)
        await win.update_frame()
        result = await win.match_image("button.png")
    """

    def __init__(self, target: Win32Target, task_ctrl=None):
        self._target = target
        self._task_ctrl = task_ctrl or _NullTaskCtrl()

        # 轮询缓存（跟 UserBrowser 一致）
        self.use_polling_temp_cache = False
        self.polling_temp_cache: dict = {}
        self.use_hotspot_roi = True

        # 最后截图帧
        self._frame = None

        self._stuck = StuckGuard(log_fn=lambda msg: self.script_log(msg))
        title = ""
        try:
            title = str(getattr(target, "title", "") or "")
        except Exception:
            title = ""
        self._observer = FrameObserver(
            self._capture_raw,
            hard_cap=HARD_CAP_FPS,
            name=f"window:{title or id(self)}",
            get_frame=lambda: self._frame,
            on_frame=self._on_obs_frame,
        )

    async def _capture_raw(self):
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(
            None,
            lambda: self._target.screenshot(client_only=True, method="auto"),
        )
        self._frame = frame
        return frame

    def _on_obs_frame(self, frame) -> None:
        self._frame = frame
        self.polling_temp_cache = {}

    def invalidate_frame(self) -> None:
        self._observer.invalidate()
        self.polling_temp_cache = {}

    async def request_fps(self, fps: float, *, key: str = "script") -> float:
        """声明本调用方期望的截图频率(Hz)。多需求取 max；无人声明则停截。"""
        return await self._observer.request_fps(key, fps)

    async def release_fps(self, key: str = "script") -> float:
        return await self._observer.release_fps(key)

    def observation_fps(self) -> float:
        return self._observer.effective_fps

    def note_state(self, name: str | None):
        self._stuck.note_state(name)

    def note_progress(self):
        self._stuck.note_progress(clear_actions=True)

    def _note_progress(self):
        self._stuck.note_progress(clear_actions=True)

    # ── 代理：所有 Win32Target 属性/方法 ──

    def __getattr__(self, name):
        """未定义的方法代理到 Win32Target，保持 async 统一。"""
        attr = getattr(self._target, name)

        if not callable(attr):
            return attr

        # sync 方法 → 包装为 async（检查 task_ctrl）
        async def async_wrapper(*args, **kwargs):
            await self._check()
            return attr(*args, **kwargs)

        return async_wrapper

    async def _check(self):
        """检查任务是否被中断。"""
        if self._task_ctrl:
            if hasattr(self._task_ctrl, "check"):
                c = self._task_ctrl.check()
                if inspect.iscoroutine(c):
                    await c

    # ── 连接健康检查（兼容 TaskFlow 的 _get_healthy_browser）──

    async def check_connection(self) -> bool:
        """检查窗口句柄是否仍然有效。"""
        try:
            return self._target.is_valid
        except Exception:
            return False

    # ── 日志 ──

    def script_log(self, msg: str):
        print(f"[{self._target.title}] {msg}")
        # 同时发送到客户端的日志系统
        if self._task_ctrl and hasattr(self._task_ctrl, 'controller'):
            ctrl = self._task_ctrl.controller
            if ctrl:
                ctrl.emit_log(
                    account=getattr(self, '_account_override', {}).get('name', ''),
                    message=msg,
                    level=LogLevel.INFO,
                    source="window",
                )

    # ── 睡眠 ──

    async def b_sleep(self, seconds: float, upper_limit: float | None = None,
                      step: float = 0.05):
        """可中断的睡眠，与 UserBrowser 一致。"""
        if upper_limit is not None:
            if upper_limit < seconds:
                seconds, upper_limit = upper_limit, seconds
            seconds = random.uniform(seconds, upper_limit)
        if seconds <= 0:
            self.invalidate_frame()
            return
        self._stuck.check_idle()
        elapsed = 0.0
        while elapsed < seconds:
            await self._check()
            await asyncio.sleep(step)
            elapsed += step
        self.invalidate_frame()

    # ── 截图 ──

    async def update_frame(self, save_screenshot=False):
        """强制拉一帧并清空轮询缓存（日常 match 会自动 ensure）。"""
        await self._check()
        frame = await self._observer.capture_once()
        self.polling_temp_cache = {}
        return frame

    # ── 点击 ──

    async def click(self, x, y, down_time=0.12, pianyi=(0, 0)):
        """后台点击（带偏移）。"""
        await self._check()
        px, py = human_offset(pianyi)
        self._target.click(x + px, y + py)
        self.invalidate_frame()

    # ── 图像匹配 ──

    async def match_image(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
            match_mode: str = "image",
            pixel_tol: float = 8.0,
            quiet: bool = False,
            use_hotspot_roi: bool | None = None,
    ):
        """在最新截图中找模板图。"""
        await self._check()
        await self._observer.ensure_frame()

        mode = (match_mode or "image").lower()
        if mode not in ("image", "pixel"):
            mode = "image"
        mtype = "pixel" if mode == "pixel" else "image"

        key = (
            str(img_path),
            threshold,
            use_color_check,
            match_select,
            mode,
            pixel_tol,
        )

        if self.use_polling_temp_cache and key in self.polling_temp_cache:
            return self.polling_temp_cache[key]

        from backend.matcher.matcher import matcher
        from backend.matcher.hotspot_roi import (
            adaptive_match,
            normalize_template_key,
            resolve_capture_mode,
        )

        self._emit_match_hud(str(img_path), "matching")

        frame = self._frame
        if frame is None:
            frame = await self._observer.ensure_frame(force=True)

        hotspot_on = (
            self.use_hotspot_roi
            if use_hotspot_roi is None
            else bool(use_hotspot_roi)
        )
        result = adaptive_match(
            matcher,
            frame,
            img_path,
            threshold=threshold,
            match_type=mtype,
            use_color_check=use_color_check if mode == "image" else False,
            match_select=match_select,
            use_orb=(mode == "image"),
            pixel_tol=pixel_tol,
            template_key=normalize_template_key(img_path),
            capture_mode=resolve_capture_mode(self),
            enabled=hotspot_on,
            multi=False,
        )

        score = getattr(result, "score", getattr(result, "max_val", None))
        ok = bool(result and result.x is not None and getattr(result, "match_success", True))
        if result and hasattr(result, "score") and result.score is not None:
            ok = bool(result.x is not None and result.score >= threshold)
        mx = getattr(result, "x", None) if result else None
        my = getattr(result, "y", None) if result else None
        self._emit_match_hud(
            str(img_path), "ok" if ok else "fail", score,
            x=mx, y=my,
        )
        if ok:
            self._stuck.note_action("match", img_path, True)

        if self.use_polling_temp_cache:
            self.polling_temp_cache[key] = result

        return result

    def _emit_match_hud(self, img_path: str, status: str, score=None,
                        action: str = "match", x=None, y=None):
        ctrl = None
        if self._task_ctrl and hasattr(self._task_ctrl, "controller"):
            ctrl = self._task_ctrl.controller
        if not ctrl or not hasattr(ctrl, "emit_match_event"):
            return
        account = getattr(self, "_account_override", {}) or {}
        name = account.get("name")
        if not name:
            return
        ctrl.emit_match_event(
            account=name,
            img_path=img_path,
            status=status,
            score=score,
            action=action,
            x=x,
            y=y,
        )

    async def click_image(
            self,
            img_path: Union[str, Path],
            pianyi=(0, 0),
            down_time=0.12,
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
            max_delay: float | None = None,
            match_mode: str = "image",
            pixel_tol: float = 8.0,
    ):
        """找图 → 偏移 → 后台点击。"""
        _t0 = time.time()
        await self._check()

        if not self.use_polling_temp_cache:
            await self._observer.ensure_frame()
            offset = human_offset(pianyi)
            result = self._match_and_click(
                img_path=img_path, pianyi=offset, threshold=threshold,
                use_color_check=use_color_check, match_select=match_select,
                match_mode=match_mode, pixel_tol=pixel_tol,
            )
            self._stuck.note_action("click", img_path, bool(result))
            if result:
                self._note_progress()
                self.invalidate_frame()
                try:
                    await self._observer.ensure_frame(force=True)
                except Exception:
                    self.polling_temp_cache = {}
            return result

        key = (
            str(img_path),
            threshold,
            use_color_check,
            match_select,
            match_mode,
            pixel_tol,
        )

        if key not in self.polling_temp_cache:
            self.polling_temp_cache[key] = await self.match_image(
                img_path=img_path, threshold=threshold,
                use_color_check=use_color_check, match_select=match_select,
                match_mode=match_mode, pixel_tol=pixel_tol,
            )

        match = self.polling_temp_cache[key]
        if not match or match.x is None:
            self._emit_match_hud(
                str(img_path), "fail",
                getattr(match, "score", None) if match else None,
                action="click",
            )
            self._stuck.note_action("click", img_path, False)
            return False

        offset = human_offset(pianyi)
        x = match.x + offset[0]
        y = match.y + offset[1]

        self._target.click(x, y)
        self._note_progress()
        self._stuck.note_action("click", img_path, True)
        self._emit_match_hud(
            str(img_path), "ok",
            getattr(match, "score", None),
            action="click", x=x, y=y,
        )

        print(f"{self._target.title}: 点击图片:{img_path}({x},{y}), "
              f"最大匹配度:{match.score if hasattr(match, 'score') else '?'}")

        self.invalidate_frame()
        try:
            await self._observer.ensure_frame(force=True)
        except Exception:
            self.polling_temp_cache = {}
        return True

    def _match_and_click(self, img_path, pianyi, threshold,
                         use_color_check, match_select,
                         match_mode="image", pixel_tol=8.0):
        """同步版找图+点击。"""
        from backend.matcher.matcher import matcher

        self._emit_match_hud(str(img_path), "matching")

        frame = self._frame
        if frame is None or self._observer.is_stale:
            frame = self._target.screenshot(client_only=True, method="auto")
            self._frame = frame
            self._observer._note_new_frame(frame)

        mode = (match_mode or "image").lower()
        if mode not in ("image", "pixel"):
            mode = "image"
        mtype = "pixel" if mode == "pixel" else "image"

        result = matcher.match(
            target=frame, template=img_path, threshold=threshold,
            match_type=mtype,
            use_color_check=use_color_check if mode == "image" else False,
            match_select=match_select,
            use_orb=(mode == "image"),
            pixel_tol=pixel_tol,
        )
        score = getattr(result, "score", getattr(result, "max_val", None)) if result else None
        if not result or result.x is None:
            self._emit_match_hud(str(img_path), "fail", score, action="match")
            self._emit_match_hud(str(img_path), "fail", score, action="click")
            return False

        self._emit_match_hud(str(img_path), "ok", score, action="match",
                             x=result.x, y=result.y)
        x = result.x + pianyi[0]
        y = result.y + pianyi[1]
        self._target.click(x, y)
        self._emit_match_hud(
            str(img_path), "ok", score, action="click", x=x, y=y,
        )

        print(f"{self._target.title}: 点击图片:{img_path}({x},{y}), "
              f"最大匹配度:{result.score if hasattr(result, 'score') else '?'}")
        return True

    # ── 等待 ──

    async def wait_image(self, img_path, timeout=0):
        """等待图片出现，timeout≤0 表示无限等待。"""
        deadline = datetime.now() + timedelta(seconds=timeout) if timeout > 0 else None

        while True:
            await self._check()
            await self._observer.ensure_frame(force=True)
            result = await self.match_image(img_path=img_path)
            if result and result.x is not None:
                return True
            if deadline and datetime.now() >= deadline:
                return False
            await self.b_sleep(0.5)

    async def click_until_gone(self, img_path, timeout=10):
        """点击直到图片消失。"""
        deadline = datetime.now() + timedelta(seconds=timeout)
        while datetime.now() < deadline:
            await self._check()
            await self._observer.ensure_frame(force=True)
            if await self.click_image(img_path=img_path):
                continue
            else:
                return True
        return False

    # ── 文本匹配与点击（兼容 UserBrowser 接口）──

    async def click_text(self, text: str):
        """在截图中找到文本并点过去。"""
        await self._check()
        from backend.matcher.matcher import matcher

        frame = self._frame
        if frame is None:
            frame = self._target.screenshot(client_only=True, method="auto")
            self._frame = frame

        result = matcher.match(target=frame, template=None, text=text)
        if not result or result.x is None:
            return None

        self._target.click(result.x, result.y)
        return result

    # ── URL/标题（兼容 UserBrowser 接口）──

    async def get_url(self) -> str:
        """窗口无 URL 概念，返回空字符串。"""
        return ""

    async def get_title(self) -> str:
        """返回窗口标题。"""
        return self.title

    # ── 便利属性 ──

    @property
    def title(self) -> str:
        return self._target.title

    @property
    def hwnd(self) -> int:
        return self._target.hwnd

    @property
    def account(self) -> dict:
        """兼容 UserBrowser 的 account 属性（空字典兜底）。"""
        return getattr(self, '_account_override', {})

    @account.setter
    def account(self, value: dict):
        self._account_override = value


class _NullTaskCtrl:
    """无操作的任务控制器，避免 task_ctrl 为 None 时出错。"""
    async def check(self):
        pass

    def emit_log(self, account, message, level, source):
        pass
