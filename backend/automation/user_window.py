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
        # 尺度与热点缓存的 key 本身已经包含帧尺寸。切到新尺寸时会自然使用
        # 新桶；删除旧尺寸桶只会导致以后切回来再次冷定标。这里只清除当前轮询
        # 快照，保留两个尺寸各自学到的视觉 profile。
        self.polling_temp_cache = {}
        try:
            self.script_log(
                f"[vision] 尺寸 profile 切换：{old[0]}x{old[1]} → "
                f"{wh[0]}x{wh[1]}（保留旧尺寸缓存）"
            )
        except Exception:
            pass

    def invalidate_vision_state(self, reason: str = "", old_size=None) -> None:
        """尺寸/目标变化后重校：尺度缓存、hotspot、轮询匹配缓存。

        ``old_size`` 给了旧尺寸时**只清旧尺寸那一桶**：尺度缓存与 hotspot 都是按
        「模板|帧宽x帧高|抓帧模式」分桶的，旧尺寸的样本固然过期，别的尺寸仍然有效。
        以前这里一律全清（全局单例、跨账号共用），于是窗口尺寸一变（窗口在
        1036x583 / 1158x651 / 1730x974 之间切）每个模板都要重新做一次冷定标
        ——实测一次冷定标 6.6s（见 tools\\_scratch\\bench_yys_match.py）。
        """
        self.polling_temp_cache = {}
        if old_size is not None:
            try:
                ow, oh = int(old_size[0]), int(old_size[1])
            except Exception:
                ow = oh = None
        else:
            ow = oh = None
        try:
            from backend.matcher.scale_calibrate import get_scale_cache

            cache = get_scale_cache()
            if ow is None:
                cache.clear()
            else:
                cache.clear_frame_size(ow, oh)
        except Exception:
            pass
        try:
            from backend.matcher.hotspot_roi import get_hotspot_store

            store = get_hotspot_store()
            if ow is None:
                store.clear_all()
            else:
                store.clear_frame_size(ow, oh)
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
        # 状态名变化打一行标记（同一状态每轮都调，不能每轮刷屏）：
        # 协作页据此把逻辑步骤卡走成「试跑至此」。
        key = None if name is None else str(name).strip()
        if key == getattr(self, "_last_noted_state", "__init__"):
            return
        self._last_noted_state = key
        if key:
            from backend.script_generator.collaborator.trial_cursor import (
                step_mark_message,
            )

            self.script_log(step_mark_message(key))

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

    def _ensure_cold_calibration_sink(self) -> None:
        """只登记一次：让 matcher 层的"冷定标耗时"日志能进运行日志。

        以前放在 `script_log` 里每次都登记（靠注册表去重兜着），虽然去重后
        不会再重复播报，但每条日志都要过一次锁 + 建 WeakMethod，没必要。
        """
        try:
            from backend.matcher.scale_calibrate import register_cold_calibration_sink

            register_cold_calibration_sink(self.script_log, account=self._log_account_name())
        except Exception:
            pass

    def _log_account_name(self) -> str:
        return str((getattr(self, "_account_override", {}) or {}).get("name") or "")

    def unregister_cold_calibration_sink(self) -> None:
        """注销本窗口的冷定标日志槽（任务结束时调用）。

        不注销的话，脚本停了之后 `adaptive_match` 若还留在
        `asyncio.to_thread` 的线程里跑完，仍会往 GUI 日志吐冷定标消息。
        """
        self._calib_sink_ready = False
        try:
            from backend.matcher.scale_calibrate import unregister_cold_calibration_sink

            unregister_cold_calibration_sink(self.script_log)
        except Exception:
            pass

    def script_log(self, msg: str):
        if not getattr(self, "_calib_sink_ready", False):
            self._calib_sink_ready = True
            self._ensure_cold_calibration_sink()
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

    async def update_frame(self, save_screenshot=False, *, priority: int = 20):
        """强制拉一帧并清空轮询缓存（日常 match 会自动 ensure）。"""
        await self._check()
        frame = await self._observer.capture_once(priority=priority)
        self.polling_temp_cache = {}
        rec = self._pseudo
        if rec:
            rec.maybe_keyframe(frame, reason="capture")
        return frame

    async def capture_region(self, x, y, width, height):
        """用后台 PrintWindow 刷新整帧，再在内存中取指定客户区。"""
        frame = await self.update_frame()
        fh, fw = frame.shape[:2]
        x1 = max(0, int(round(float(x))))
        y1 = max(0, int(round(float(y))))
        x2 = min(fw, int(round(float(x) + float(width))))
        y2 = min(fh, int(round(float(y) + float(height))))
        if x2 <= x1 or y2 <= y1:
            raise ValueError(
                f"局部截图区域无效: ({x}, {y}, {width}, {height}) frame={fw}x{fh}"
            )
        return frame[y1:y2, x1:x2].copy()

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
            cheap: bool = False,
            force_wide: bool = False,
            vision_priority: int = 20,
    ):
        """在最新截图中找模板图。

        ``cheap=True``：跳过宽搜兜底（省 1.5~1.8s/次）。给"空帧里看一眼"用 ——
        空帧的定义就是没有素材可匹配，全尺度兜底没有意义。
        """
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
            force_wide,
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
            # 多开：多个账号共用同一个事件循环，匹配是同步 CPU 活，
            # 必须丢到线程里跑，否则会卡住另一个账号。
            from backend.matcher.scale_calibrate import cache_key, get_scale_cache
            from .vision_resource import vision_resource

            capture_mode = resolve_capture_mode(self)
            vision_kind = "wide" if force_wide else "hot"
            if not force_wide:
                try:
                    from backend.matcher.match_ref import resolve_scale_for_frame

                    fh, fw = frame.shape[:2]
                    skey = cache_key(
                        normalize_template_key(img_path), fw, fh, capture_mode
                    )
                    if (
                        get_scale_cache().get(skey) is None
                        and resolve_scale_for_frame(img_path, fw, fh) is None
                    ):
                        vision_kind = "calibration"
                except Exception:
                    pass

            owner = self._log_account_name() or f"hwnd:{self._bound_hwnd}"
            async with vision_resource(
                kind=vision_kind, priority=vision_priority, owner=owner
            ):
                result = await asyncio.to_thread(
                    adaptive_match,
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
                    capture_mode=capture_mode,
                    enabled=hotspot_on,
                    multi=False,
                    # 冷定标日志只落进这个账号的日志里（双开时别互相串）
                    log_account=self._log_account_name(),
                    cheap=cheap,
                    force_wide=force_wide,
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

    async def match_image_multi(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_mode: str = "image",
            pixel_tol: float = 8.0,
            use_hotspot_roi: bool | None = None,
    ):
        """在桌面窗口当前帧中查找模板的全部命中。

        与 ``UserBrowser.match_image_multi`` 使用同一调用模板和返回结构：
        ``list[dict]``，每项包含客户端坐标 ``x`` / ``y`` 与 ``score``。
        """
        await self._check()
        await self._observer.ensure_frame()

        mode = (match_mode or "image").lower()
        if mode not in ("image", "pixel"):
            mode = "image"
        mtype = "pixel_multi" if mode == "pixel" else "image_multi"

        from backend.matcher.matcher import matcher
        from backend.matcher.hotspot_roi import (
            adaptive_match,
            normalize_template_key,
            resolve_capture_mode,
        )
        from .vision_resource import vision_resource

        frame = self._frame
        if frame is None:
            frame = await self._observer.ensure_frame(force=True)
        hotspot_on = (
            self.use_hotspot_roi
            if use_hotspot_roi is None
            else bool(use_hotspot_roi)
        )
        self._emit_match_hud(str(img_path), "matching", action="match_multi")
        rec = self._pseudo
        stem = Path(str(img_path)).stem if not str(img_path).startswith("data:") else "b64"
        sid = rec.begin("match_multi", stem, threshold=threshold, mode=match_mode) if rec else None
        try:
            owner = self._log_account_name() or f"hwnd:{self._bound_hwnd}"
            async with vision_resource(kind="hot", priority=20, owner=owner):
                results = await asyncio.to_thread(
                    adaptive_match,
                    matcher,
                    frame,
                    img_path,
                    threshold=threshold,
                    match_type=mtype,
                    use_color_check=use_color_check if mode == "image" else False,
                    use_orb=(mode == "image"),
                    pixel_tol=pixel_tol,
                    template_key=normalize_template_key(img_path),
                    capture_mode=resolve_capture_mode(self),
                    enabled=hotspot_on,
                    multi=True,
                    log_account=self._log_account_name(),
                )
        except Exception:
            if rec:
                rec.end(sid, template=stem, error=True)
            raise

        converted = []
        for item in results or []:
            if isinstance(item, dict):
                x, y = item.get("x"), item.get("y")
                score = item.get("score", item.get("max_val", 0.0))
            else:
                x, y = getattr(item, "x", None), getattr(item, "y", None)
                score = getattr(item, "score", getattr(item, "max_val", 0.0))
            if x is None or y is None:
                continue
            converted.append({"x": int(x), "y": int(y), "score": float(score or 0.0)})

        if rec:
            rec.end(sid, template=stem, ok=bool(converted), count=len(converted), threshold=threshold, mode=match_mode)
        best = max((item["score"] for item in converted), default=None)
        self._emit_match_hud(
            str(img_path), "ok" if converted else "fail", best,
            action="match_multi", detail={"count": len(converted)},
        )
        if converted:
            self._stuck.note_action("match_multi", img_path, True)
        return converted

    def _hud_account(self) -> str:
        """当前账号名；取不到返回空串（此时 HUD 事件本就无处归位）。"""
        return str((getattr(self, "_account_override", {}) or {}).get("name") or "")

    def _emit_match_hud(self, img_path: str, status: str, score=None,
                        action: str = "match", x=None, y=None, detail=None):
        ctrl = None
        if self._task_ctrl and hasattr(self._task_ctrl, "controller"):
            ctrl = self._task_ctrl.controller
        if not ctrl or not hasattr(ctrl, "emit_match_event"):
            return
        name = self._hud_account()
        if not name:
            return
        try:
            ctrl.emit_match_event(
                account=name,
                img_path=img_path,
                status=status,
                score=score,
                action=action,
                x=x,
                y=y,
                # 执行器自己算出来的补充事实（视觉变化/预期场景/关键帧引用）；
                # 取不到就传 None，让登记侧一律留 None，不冒充（设计 §19.3）。
                detail=detail,
            )
        except TypeError:
            # 老控制器（不接受 detail）时退回基础字段，遥测降级但不影响执行
            ctrl.emit_match_event(
                account=name,
                img_path=img_path,
                status=status,
                score=score,
                action=action,
                x=x,
                y=y,
            )

    def _save_hud_key_frame(self, frame, *, source: str) -> str:
        """把关键帧存进当前试运行；拿不到就返回空串（绝不打断执行）。"""
        name = self._hud_account()
        if not name or frame is None:
            return ""
        try:
            from backend.script_generator.collaborator_v3.runtime.trial_frames import (
                save_key_frame,
            )

            return str(save_key_frame(name, frame, source=source) or "")
        except Exception:  # noqa: BLE001 - 关键帧是加固，不是主路径
            return ""

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
        # 要确认预期场景时，点击记录必须等确认结果一起写：一次点击 = 一条记录
        exp = (expect or "none").strip().lower()
        confirm = exp not in ("", "none", "off")

        if not self.use_polling_temp_cache:
            # 同上：匹配+点击是同步 CPU 活，放线程里跑，别卡住另一个账号
            from .vision_resource import vision_resource

            owner = self._log_account_name() or f"hwnd:{self._bound_hwnd}"
            async with vision_resource(kind="hot", priority=0, owner=owner):
                clicked, score, cx, cy = await asyncio.to_thread(
                    self._match_and_click,
                    img_path=img_path, pianyi=offset, threshold=threshold,
                    use_color_check=use_color_check, match_select=match_select,
                    match_mode=match_mode, pixel_tol=pixel_tol,
                    down_time=down_time,
                    emit_click_hud=not confirm,
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
            if not confirm:
                self._emit_match_hud(
                    str(img_path), "ok", score, action="click", x=cx, y=cy,
                    detail={
                        "raw_xy": (float(match.x), float(match.y)),
                        "click_offset": (float(offset[0]), float(offset[1])),
                        "final_xy": (float(cx), float(cy)),
                    },
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

        if confirm:
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
            except Exception as e:
                self.script_log(f"[click_image] 确认异常: {e}")
                ok = None
            # expected_scene_reached 是**业务步骤成功**的观察，与
            # visual_effect_observed（区域视觉变化）分开记（设计 §19.3）。
            # 确认过程中抛异常 → 留 None（没观察到，不等于没达成）。
            self._emit_match_hud(
                str(img_path), "ok", score, action="click", x=cx, y=cy,
                detail={
                    "expected_scene_reached": (None if ok is None else bool(ok)),
                    "confirm_policy": exp,
                    "failed_stage": "" if ok else "confirm",
                },
            )
            if not ok:
                self.script_log(
                    f"[click_image] 确认失败 expect={exp} path={img_path}"
                )
                self._stuck.note_action("click", img_path, False)
                return False

        try:
            await self._observer.ensure_frame(force=True)
        except Exception:
            self.polling_temp_cache = {}
        return True

    async def click_image_effect(
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
            interval: float = 0.15,
            effect_threshold: float = 0.75,
    ) -> bool:
        """点击图片，并用后台新帧中的原目标区域确认视觉反馈。"""
        del max_delay
        await self._check()
        await self._observer.ensure_frame()
        match = await self.match_image(
            img_path=img_path,
            threshold=threshold,
            use_color_check=use_color_check,
            match_select=match_select,
            match_mode=match_mode,
            pixel_tol=pixel_tol,
        )
        if not match or match.x is None:
            self._stuck.note_action("click", img_path, False)
            # 连模板都没命中：这次"点击"根本没发生，但失败事实要留痕
            self._emit_match_hud(
                str(img_path), "fail", None, action="click",
                detail={"failed_stage": "match"},
            )
            return False

        width = int(getattr(match, "width", 0) or 0)
        height = int(getattr(match, "height", 0) or 0)
        if width <= 0 or height <= 0:
            self.script_log(
                f"[click_image_effect] 缺少命中尺寸，无法局部确认: {img_path}"
            )
            self._emit_match_hud(
                str(img_path), "fail", getattr(match, "score", None), action="click",
                detail={"failed_stage": "no_hit_size"},
            )
            return False

        offset = human_offset(pianyi)
        click_x = match.x + offset[0]
        click_y = match.y + offset[1]
        self._click_client(click_x, click_y, down_time=down_time)
        self._stuck.note_action("click", img_path, True)
        if interval > 0:
            await asyncio.sleep(float(interval))
        await self._check()

        left = float(match.x) - width / 2.0
        top = float(match.y) - height / 2.0
        region = await self.capture_region(left, top, width, height)
        from backend.matcher.effect_match import effect_match_score
        from backend.matcher.matcher import matcher

        score = await asyncio.to_thread(
            effect_match_score, matcher, region, img_path,
        )
        changed = score < float(effect_threshold)
        # 这就是"目标区域是否发生可观察视觉变化"的实测结果：落成
        # visual_effect_observed（设计 §19.3），未生效时附上对照区域关键帧。
        frame_after = ""
        if not changed:
            frame_after = self._save_hud_key_frame(
                region, source="click_effect_region",
            )
        self._emit_match_hud(
            str(img_path), "ok", score, action="click", x=click_x, y=click_y,
            detail={
                "visual_effect_observed": bool(changed),
                "raw_xy": (float(match.x), float(match.y)),
                "click_offset": (float(offset[0]), float(offset[1])),
                "final_xy": (float(click_x), float(click_y)),
                "frame_after": frame_after,
                "confirm_policy": "effect",
                "failed_stage": "" if changed else "effect",
            },
        )
        self.script_log(
            f"[click_image_effect] {'生效' if changed else '未观察到变化'} "
            f"path={img_path} score={score:.4f} threshold={effect_threshold:.4f} "
            f"region={width}x{height} interval={float(interval):.3f}s"
        )
        if changed:
            self._note_progress()
        return changed

    def _match_and_click(
            self, img_path, pianyi, threshold,
            use_color_check, match_select,
            match_mode="image", pixel_tol=8.0, down_time=0.12,
            emit_click_hud: bool = True,
    ):
        """同步版找图+点击。返回 (ok, score, x, y)。

        ``emit_click_hud=False``：调用方还要做 ``expect`` 确认，点击记录必须**等确认
        结果一起写**，否则一次点击会落成两条 ``input.click_completed``（一条没有
        ``expected_scene_reached``），诊断链会把同一次点击数成两次。
        """
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
        if emit_click_hud:
            self._emit_match_hud(
                str(img_path), "ok", score, action="click", x=x, y=y,
                # §19.3 三件坐标：命中点 / 偏移 / 实际点。都给出来，
                # 否则 raw_xy 会被当成"实际点"，模型看不出抖动改了多少。
                detail={
                    "raw_xy": (float(result.x), float(result.y)),
                    "click_offset": (float(pianyi[0]), float(pianyi[1])),
                    "final_xy": (float(x), float(y)),
                },
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
