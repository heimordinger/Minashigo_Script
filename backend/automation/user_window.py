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
from typing import Optional, Union

from .win32_target import Win32Target
from .stuck_guard import StuckGuard
from .frame_observer import FrameObserver, HARD_CAP_FPS
from .run_recorder import (
    PseudoRecorder,
    env_pseudo_record_enabled,
)


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
        from .emulator_target import resolve_automation_target

        bound = target
        resolved = resolve_automation_target(int(target.hwnd))
        self._bound_hwnd = int(bound.hwnd)
        self._target = resolved
        self._task_ctrl = task_ctrl or _NullTaskCtrl()

        # 轮询缓存（跟 UserBrowser 一致）
        self.use_polling_temp_cache = False
        self.polling_temp_cache: dict = {}
        self.use_hotspot_roi = True

        # 最后截图帧与客户区尺寸哨兵（尺寸变化 → 视觉状态总失效）
        self._frame = None
        self._client_size: tuple[int, int] | None = None

        # 伪录制（与 UserBrowser 对齐；默认关）
        self._pseudo: PseudoRecorder | None = None

        self._stuck = StuckGuard(log_fn=lambda msg: self.script_log(msg))
        self._min_policy = self._load_minimized_policy()
        self._min_warned = False
        self._min_paused_logged = False
        title = ""
        try:
            title = str(getattr(self._target, "title", "") or "")
        except Exception:
            title = ""
        self._observer = FrameObserver(
            self._capture_raw,
            hard_cap=HARD_CAP_FPS,
            name=f"window:{title or id(self)}",
            get_frame=lambda: self._frame,
            on_frame=self._on_obs_frame,
        )
        if int(self._target.hwnd) != int(self._bound_hwnd):
            try:
                self.script_log(
                    f"[window] 已改用渲染窗 hwnd={self._target.hwnd} "
                    f"({self._target.title}) ← 绑定 {self._bound_hwnd}"
                )
            except Exception:
                pass

    def _note_frame_geometry(self, frame) -> None:
        """记录帧尺寸；变化时清空定标/hotspot/轮询缓存。"""
        if frame is None:
            return
        try:
            wh = (int(frame.shape[1]), int(frame.shape[0]))
        except Exception:
            return
        if self._client_size is None:
            self._client_size = wh
            return
        if wh == self._client_size:
            return
        old = self._client_size
        self._client_size = wh
        self.invalidate_vision_state(reason=f"窗口尺寸 {old[0]}x{old[1]} → {wh[0]}x{wh[1]}")

    def invalidate_vision_state(self, reason: str = "") -> None:
        """尺寸/目标变化后重校：尺度缓存、hotspot、轮询匹配缓存。"""
        self.polling_temp_cache = {}
        try:
            from backend.matcher.scale_calibrate import get_scale_cache

            get_scale_cache().clear()
        except Exception:
            pass
        try:
            from backend.matcher.hotspot_roi import get_hotspot_store

            get_hotspot_store().clear_all()
        except Exception:
            pass
        if reason:
            try:
                self.script_log(f"[vision] 重校：{reason}")
            except Exception:
                print(f"[vision] 重校：{reason}")

    async def _capture_raw(self):
        rec = self._pseudo
        sid = rec.begin("capture", "observer") if rec else None
        try:
            await self._ensure_window_ready()
            loop = asyncio.get_running_loop()
            frame = await loop.run_in_executor(
                None,
                lambda: self._target.screenshot(client_only=True, method="auto"),
            )
            self._frame = frame
            self._note_frame_geometry(frame)
            if rec:
                shape = None
                if frame is not None:
                    shape = [int(frame.shape[1]), int(frame.shape[0])]
                rec.end(sid, shape=shape, mode="client", source="observer")
            return frame
        except Exception:
            if rec:
                rec.end(sid, error=True, source="observer")
            raise

    def _on_obs_frame(self, frame) -> None:
        self._frame = frame
        self.polling_temp_cache = {}
        self._note_frame_geometry(frame)

    def invalidate_frame(self) -> None:
        self._observer.invalidate()
        self.polling_temp_cache = {}

    @property
    def pseudo_record(self) -> Optional[PseudoRecorder]:
        return self._pseudo

    def enable_pseudo_record(
        self,
        script_name: str = "script",
        *,
        save_keyframes: bool = True,
        force: bool = False,
        keyframe_min_interval_s: float | None = None,
    ) -> Optional[PseudoRecorder]:
        """开启全过程伪录制。force=False 时仅环境变量开启才启动。"""
        if self._pseudo is not None:
            return self._pseudo
        if not force and not env_pseudo_record_enabled():
            return None
        try:
            account = str(
                (getattr(self, "_account_override", {}) or {}).get("name") or ""
            )
        except Exception:
            account = ""
        if not account:
            try:
                account = str(getattr(self._target, "title", "") or "")
            except Exception:
                account = ""
        kw = dict(
            account=account or "unknown",
            script_name=script_name,
            save_keyframes=save_keyframes,
        )
        if keyframe_min_interval_s is not None:
            kw["keyframe_min_interval_s"] = float(keyframe_min_interval_s)
        self._pseudo = PseudoRecorder(**kw)
        self.script_log(f"[伪录制] 已开启 → {self._pseudo.dir}")
        return self._pseudo

    def finish_pseudo_record(self, *, status: str = "ok") -> Optional[Path]:
        rec = self._pseudo
        if rec is None:
            return None
        out = rec.finish(status=status)
        self._pseudo = None
        try:
            # 直接底层日志，避免再写入已关闭的 recorder
            print(f"[{getattr(self._target, 'title', 'window')}] [伪录制] 已保存 → {out}")
            if self._task_ctrl and hasattr(self._task_ctrl, "controller"):
                ctrl = self._task_ctrl.controller
                if ctrl:
                    acct = (getattr(self, "_account_override", {}) or {}).get("name", "")
                    ctrl.emit_log(
                        account=acct,
                        message=f"[伪录制] 已保存 → {out}",
                        level=LogLevel.INFO,
                        source="window",
                    )
            summary_json = out / "summary.json"
            if summary_json.is_file():
                import json

                s = json.loads(summary_json.read_text(encoding="utf-8"))
                self.script_log(
                    f"[伪录制] 黑屏={s.get('black_s')}s  "
                    f"有效={s.get('effective_s')}s  "
                    f"墙钟={s.get('total_s')}s"
                )
            summary_txt = out / "summary.txt"
            if summary_txt.is_file():
                head = summary_txt.read_text(encoding="utf-8").splitlines()[:2]
                for line in head:
                    self.script_log(f"[伪录制] {line}")
        except Exception:
            pass
        return out

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

    @staticmethod
    def _load_minimized_policy() -> str:
        """restore | pause | fail；默认 restore。"""
        try:
            from core.config.config import config
            val = str(config.get("window.minimized_policy") or "restore").strip().lower()
        except Exception:
            val = "restore"
        if val not in ("restore", "pause", "fail"):
            return "restore"
        return val

    async def _ensure_window_ready(self) -> None:
        """最小化策略：后台恢复 / 暂停等待 / 直接失败。"""
        policy = getattr(self, "_min_policy", None) or self._load_minimized_policy()
        self._min_policy = policy

        while True:
            await self._check()
            try:
                minimized = bool(self._target.is_effectively_minimized)
            except Exception:
                minimized = False
            if not minimized:
                if self._min_paused_logged:
                    self.script_log("[window] 窗口已恢复，继续脚本")
                self._min_paused_logged = False
                return

            if policy == "fail":
                raise RuntimeError(
                    "目标窗口已最小化：无法可靠识图/点击。"
                    "请恢复窗口，或在设置里将「最小化策略」改为自动恢复/暂停。"
                )

            if policy == "restore":
                did = False
                try:
                    did = bool(self._target.ensure_restored(bottom=True, settle_s=0.2))
                except Exception as e:
                    raise RuntimeError(f"无法从最小化恢复窗口: {e}") from e
                if did and not self._min_warned:
                    self._min_warned = True
                    self.script_log(
                        "[window] 检测到最小化，已后台恢复（不抢焦点、不回缩任务栏）"
                    )
                try:
                    still = bool(self._target.is_effectively_minimized)
                except Exception:
                    still = False
                if still:
                    raise RuntimeError("窗口仍处于最小化，自动恢复失败")
                return

            # pause：等到用户手动恢复
            if not self._min_paused_logged:
                self._min_paused_logged = True
                self.script_log("[window] 窗口已最小化，脚本暂停，请手动恢复窗口…")
            await asyncio.sleep(1.0)

    # ── 连接健康检查（兼容 TaskFlow 的 _get_healthy_browser）──

    async def check_connection(self) -> bool:
        """检查窗口句柄是否仍然有效。"""
        try:
            return self._target.is_valid
        except Exception:
            return False

    # ── 日志 ──

    def script_log(self, msg: str):
        rec = self._pseudo
        if rec is not None:
            try:
                rec.event("log", msg=str(msg))
            except Exception:
                pass
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
                      step: float = 0.05, *, invalidate: bool = True):
        """可中断的睡眠，与 UserBrowser 一致。"""
        planned = seconds
        if upper_limit is not None:
            if upper_limit < seconds:
                seconds, upper_limit = upper_limit, seconds
            seconds = random.uniform(seconds, upper_limit)
            planned = seconds
        rec = self._pseudo
        sid = rec.begin("sleep", planned_s=round(planned, 3)) if rec else None
        try:
            if seconds <= 0:
                if invalidate:
                    self.invalidate_frame()
                return
            self._stuck.check_idle()
            elapsed = 0.0
            while elapsed < seconds:
                await self._check()
                await asyncio.sleep(step)
                elapsed += step
            if invalidate:
                self.invalidate_frame()
        finally:
            if rec:
                rec.end(sid, planned_s=round(planned, 3))

    # ── 截图 ──

    async def update_frame(self, save_screenshot=False):
        """强制拉一帧并清空轮询缓存（日常 match 会自动 ensure）。"""
        await self._check()
        frame = await self._observer.capture_once()
        self.polling_temp_cache = {}
        rec = self._pseudo
        if rec:
            rec.maybe_keyframe(frame, reason="capture")
        return frame

    # ── 点击 ──

    def _click_client(self, x: int, y: int, *, down_time: float = 0.12) -> None:
        """统一客户区点击入口（记伪录制 click）。"""
        # 同步路径：最小化时先恢复（不回缩）
        try:
            if self._target.is_effectively_minimized:
                self._target.ensure_restored(bottom=True, settle_s=0.15)
                if not self._min_warned:
                    self._min_warned = True
                    self.script_log(
                        "[window] 点击前检测到最小化，已后台恢复"
                    )
        except Exception:
            pass
        rec = self._pseudo
        cx, cy = int(x), int(y)
        # 截图像素与真实客户区不一致时，缩放到客户区再点（MuMu 等偶发）
        try:
            tw, th = self._target._get_client_size()
            if self._frame is not None and tw > 0 and th > 0:
                fh, fw = int(self._frame.shape[0]), int(self._frame.shape[1])
                if fw > 0 and fh > 0 and (fw != tw or fh != th):
                    cx = int(round(cx * tw / fw))
                    cy = int(round(cy * th / fh))
                    self.script_log(
                        f"[click] 帧{fw}x{fh}→客户区{tw}x{th} 坐标 ({x},{y})→({cx},{cy})"
                    )
        except Exception:
            pass
        sid = rec.begin("click", x=cx, y=cy) if rec else None
        try:
            # 先 move 再点：部分模拟器忽略孤立的 down/up
            try:
                self._target.mouse_move(cx, cy)
            except Exception:
                pass
            self._target.click(cx, cy, send_mode="post")
            if down_time and down_time > 0:
                time.sleep(min(float(down_time), 0.25))
            self.invalidate_frame()
            if rec:
                rec.maybe_keyframe(self._frame, reason="click")
        finally:
            if rec:
                rec.end(sid, x=cx, y=cy)

    async def click(self, x, y, down_time=0.12, pianyi=(0, 0)):
        """后台点击（带偏移）。"""
        await self._check()
        px, py = human_offset(pianyi)
        self._click_client(x + px, y + py, down_time=down_time)

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
        rec = self._pseudo
        stem = Path(str(img_path)).stem if not str(img_path).startswith("data:") else "b64"
        sid = rec.begin("match", stem, threshold=threshold, mode=match_mode) if rec else None
        try:
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
            if rec:
                rec.end(
                    sid,
                    template=stem,
                    ok=ok,
                    score=None if score is None else round(float(score), 4),
                    threshold=threshold,
                    mode=match_mode,
                    quiet=quiet,
                )
        except Exception:
            if rec:
                rec.end(sid, template=stem, error=True)
            raise

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
            expect: str = "none",
            appear_path=None,
            confirm_timeout: float = 8.0,
            stable_ms: float = 350.0,
    ):
        """找图 → 偏移 → 后台点击；可选 expect=gone/appear 确认。"""
        await self._check()
        await self._observer.ensure_frame()

        offset = human_offset(pianyi)
        clicked = False
        score = None
        cx = cy = None

        if not self.use_polling_temp_cache:
            clicked, score, cx, cy = self._match_and_click(
                img_path=img_path, pianyi=offset, threshold=threshold,
                use_color_check=use_color_check, match_select=match_select,
                match_mode=match_mode, pixel_tol=pixel_tol,
                down_time=down_time,
            )
        else:
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
            score = (
                getattr(match, "score", None)
                or getattr(match, "max_val", None)
            ) if match else None
            if not match or match.x is None:
                self._emit_match_hud(
                    str(img_path), "fail", score, action="click",
                )
                self._stuck.note_action("click", img_path, False)
                return False
            cx = match.x + offset[0]
            cy = match.y + offset[1]
            self._click_client(cx, cy, down_time=down_time)
            clicked = True
            self._emit_match_hud(
                str(img_path), "ok", score, action="click", x=cx, y=cy,
            )
            print(
                f"{self._target.title}: 点击图片:{img_path}({cx},{cy}), "
                f"最大匹配度:{score}"
            )

        if not clicked:
            self._stuck.note_action("click", img_path, False)
            return False

        self._note_progress()
        self._stuck.note_action("click", img_path, True)
        self.invalidate_frame()

        exp = (expect or "none").strip().lower()
        if exp not in ("", "none", "off"):
            try:
                from backend.script_generator.click_confirm import confirm_after_click
                ok = await confirm_after_click(
                    self,
                    img_path,
                    expect=exp,
                    appear_path=appear_path,
                    timeout=confirm_timeout,
                    threshold=threshold,
                    stable_ms=stable_ms,
                )
                if not ok:
                    self.script_log(
                        f"[click_image] 确认失败 expect={exp} path={img_path}"
                    )
                    self._stuck.note_action("click", img_path, False)
                    return False
            except Exception as e:
                self.script_log(f"[click_image] 确认异常: {e}")
                return False

        try:
            await self._observer.ensure_frame(force=True)
        except Exception:
            self.polling_temp_cache = {}
        return True

    def _match_and_click(
            self, img_path, pianyi, threshold,
            use_color_check, match_select,
            match_mode="image", pixel_tol=8.0, down_time=0.12,
    ):
        """同步版找图+点击。返回 (ok, score, x, y)。"""
        from backend.matcher.matcher import matcher
        from backend.matcher.hotspot_roi import (
            adaptive_match,
            normalize_template_key,
            resolve_capture_mode,
        )

        self._emit_match_hud(str(img_path), "matching")

        frame = self._frame
        if frame is None or self._observer.is_stale:
            frame = self._target.screenshot(client_only=True, method="auto")
            self._frame = frame
            self._observer._note_new_frame(frame)
            self._note_frame_geometry(frame)

        mode = (match_mode or "image").lower()
        if mode not in ("image", "pixel"):
            mode = "image"
        mtype = "pixel" if mode == "pixel" else "image"

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
            enabled=self.use_hotspot_roi,
            multi=False,
        )
        score = getattr(result, "score", getattr(result, "max_val", None)) if result else None
        if not result or result.x is None:
            self._emit_match_hud(str(img_path), "fail", score, action="match")
            self._emit_match_hud(str(img_path), "fail", score, action="click")
            return False, score, None, None

        self._emit_match_hud(str(img_path), "ok", score, action="match",
                             x=result.x, y=result.y)
        x = result.x + pianyi[0]
        y = result.y + pianyi[1]
        self._click_client(x, y, down_time=down_time)
        self._emit_match_hud(
            str(img_path), "ok", score, action="click", x=x, y=y,
        )

        print(f"{self._target.title}: 点击图片:{img_path}({x},{y}), "
              f"最大匹配度:{score}")
        return True, score, x, y

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

        self._click_client(result.x, result.y)
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
