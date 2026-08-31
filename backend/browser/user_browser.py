# backend/browser/user_browser.py
import inspect
import asyncio
import random
from datetime import datetime, timedelta

from pathlib import Path
from backend.browser.browser import Browser
from backend.automation.stuck_guard import StuckGuard
from backend.automation.frame_observer import (
    FrameObserver,
    HARD_CAP_FPS,
)
from backend.automation.run_recorder import (
    PseudoRecorder,
    env_pseudo_record_enabled,
)
from typing import Tuple, Union, Optional


def human_offset(pianyi: Union[None, int, Tuple[int, int]] = None) -> Tuple[int, int]:
    """
    pianyi 语义：
    - None        -> 默认随机偏移（半径 6）
    - int         -> 以该值为半径的随机偏移
    - (x, y)      -> 固定偏移
    """
    if pianyi is None:
        radius = 6
        return (
            random.randint(-radius, radius),
            random.randint(-radius, radius),
        )

    if isinstance(pianyi, int):
        return (
            random.randint(-pianyi, pianyi),
            random.randint(-pianyi, pianyi),
        )

    if isinstance(pianyi, tuple) and len(pianyi) == 2:
        return pianyi

    raise TypeError(f"非法 pianyi 类型: {pianyi!r}")


class UserBrowser:
    def __init__(self, browser: Browser, task_ctrl):
        self._browser = browser
        self._task_ctrl = task_ctrl

        # ===== 轮询缓存 =====
        self.use_polling_temp_cache = False
        self.polling_temp_cache = {}
        # 登录等场景：后台截帧也裁剪到 GameCanvas
        self.use_game_frame_capture = False
        # 历史热点自适应 ROI（落到 Browser mixin）
        self._browser.use_hotspot_roi = True
        # 伪录制（默认关；脚本/环境变量开启）
        self._pseudo: PseudoRecorder | None = None

        self._stuck = StuckGuard(log_fn=lambda msg: self.script_log(msg))
        acct = ""
        try:
            acct = str(getattr(browser, "account", {}) or {}).get("name") or ""
        except Exception:
            acct = ""
        self._observer = FrameObserver(
            self._capture_raw,
            hard_cap=HARD_CAP_FPS,
            name=f"browser:{acct or id(self)}",
            get_frame=lambda: getattr(self._browser, "_frame", None),
            on_frame=self._on_obs_frame,
        )

    @property
    def use_hotspot_roi(self) -> bool:
        return bool(getattr(self._browser, "use_hotspot_roi", True))

    @use_hotspot_roi.setter
    def use_hotspot_roi(self, value: bool) -> None:
        self._browser.use_hotspot_roi = bool(value)

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
            account = str((getattr(self._browser, "account", {}) or {}).get("name") or "")
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
        # 直接打底层日志，避免再写入已关闭的 recorder
        try:
            self._browser.script_log(f"[伪录制] 已保存 → {out}")
            summary_json = out / "summary.json"
            if summary_json.is_file():
                import json

                s = json.loads(summary_json.read_text(encoding="utf-8"))
                self._browser.script_log(
                    f"[伪录制] 黑屏={s.get('black_s')}s  "
                    f"有效={s.get('effective_s')}s  "
                    f"墙钟={s.get('total_s')}s"
                )
            summary_txt = out / "summary.txt"
            if summary_txt.is_file():
                # 只打首行摘要，避免刷屏
                head = summary_txt.read_text(encoding="utf-8").splitlines()[:2]
                for line in head:
                    self._browser.script_log(f"[伪录制] {line}")
        except Exception:
            pass
        return out

    async def _capture_raw(self):
        rec = self._pseudo
        sid = rec.begin("capture", "observer") if rec else None
        try:
            if self.use_game_frame_capture:
                frame = await self._browser.update_frame(
                    save_screenshot=False,
                    crop_game_canvas=True,
                )
            else:
                frame = await self._browser.update_frame(save_screenshot=False)
            if rec:
                shape = None
                if frame is not None:
                    shape = [int(frame.shape[1]), int(frame.shape[0])]
                mode = getattr(self._browser, "_frame_capture_mode", None) or "full"
                rec.end(sid, shape=shape, mode=mode, source="observer")
            return frame
        except Exception:
            if rec:
                rec.end(sid, error=True, source="observer")
            raise

    def _on_obs_frame(self, _frame) -> None:
        # 新帧使轮询匹配缓存失效
        self.polling_temp_cache = {}

    def invalidate_frame(self) -> None:
        self._observer.invalidate()
        self.polling_temp_cache = {}

    async def request_fps(self, fps: float, *, key: str = "script") -> float:
        """声明本调用方期望的截图频率(Hz)。多需求取 max，受 hard_cap 限制。

        fps<=0 等同 release_fps(key)。无人声明时后台停截。
        """
        return await self._observer.request_fps(key, fps)

    async def release_fps(self, key: str = "script") -> float:
        """释放某调用方的频率需求；全部释放后停止后台截图。"""
        return await self._observer.release_fps(key)

    def observation_fps(self) -> float:
        """当前生效的截图 Hz（0=未在观察）。"""
        return self._observer.effective_fps

    def script_log(self, msg: str):
        rec = self._pseudo
        if rec is not None:
            try:
                rec.event("log", msg=str(msg))
            except Exception:
                pass
        self._browser.script_log(msg)

    def note_state(self, name: str | None):
        """FSM 每轮上报当前状态；用于状态停滞检测。"""
        self._stuck.note_state(name)

    def note_progress(self):
        """脚本可手动标记有进展（可选）。"""
        self._stuck.note_progress(clear_actions=True)

    def _note_progress(self):
        self._stuck.note_progress(clear_actions=True)

    def __getattr__(self, name):
        attr = getattr(self._browser, name)

        if not callable(attr):
            return attr

        if inspect.iscoroutinefunction(attr):
            async def async_wrapper(*args, **kwargs):
                await self._task_ctrl.check()
                return await attr(*args, **kwargs)

            return async_wrapper

        return attr

    async def b_sleep(
        self,
        seconds: float,
        upper_limit: float | None = None,
        step: float = 0.05,
        *,
        invalidate: bool = True,
    ):
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
                await self._task_ctrl.check()
                await asyncio.sleep(step)
                elapsed += step
            # sleep 期间画面可能已变；默认不强制截图，等下次 match/ensure
            if invalidate:
                self.invalidate_frame()
        finally:
            if rec:
                rec.end(sid, planned_s=round(planned, 3))

    async def click(self, x, y, down_time=0.12, pianyi=(0, 0)):
        await self._task_ctrl.check()
        pianyi = human_offset(pianyi)
        rec = self._pseudo
        sid = rec.begin("click", x=x, y=y) if rec else None
        try:
            await self._browser.click(
                x=x,
                y=y,
                pianyi=pianyi,
                down_time=down_time
            )
            self.invalidate_frame()
            if rec:
                frame = getattr(self._browser, "_frame", None)
                rec.maybe_keyframe(frame, reason="click")
        finally:
            if rec:
                rec.end(sid, x=x, y=y)

    async def update_frame(self, save_screenshot=False, crop_game_canvas: bool = False):
        """强制拉一帧并清空轮询缓存（高级用法；日常 match 会自动 ensure）。"""
        await self._task_ctrl.check()
        rec = self._pseudo
        # 走 observer.capture_once 时由 _capture_raw 记 capture，避免重复
        direct = bool(
            save_screenshot or crop_game_canvas or self.use_game_frame_capture
        )
        sid = rec.begin("capture", "update_frame") if (rec and direct) else None
        try:
            if direct:
                frame = await self._browser.update_frame(
                    save_screenshot=save_screenshot,
                    crop_game_canvas=crop_game_canvas or self.use_game_frame_capture,
                )
                self._observer._note_new_frame(frame)
                self.polling_temp_cache = {}
            else:
                frame = await self._observer.capture_once()
                self.polling_temp_cache = {}
            if rec and direct:
                shape = None
                if frame is not None:
                    shape = [int(frame.shape[1]), int(frame.shape[0])]
                mode = getattr(self._browser, "_frame_capture_mode", None) or "full"
                rec.end(sid, shape=shape, mode=mode, source="update_frame")
            if rec:
                rec.maybe_keyframe(frame, reason="capture")
            return frame
        except Exception:
            if rec and direct:
                rec.end(sid, error=True, source="update_frame")
            raise

    async def update_game_frame(self, save_screenshot=False):
        """截取 GameCanvas 区域并刷新观察器缓存。"""
        return await self.update_frame(
            save_screenshot=save_screenshot,
            crop_game_canvas=True,
        )

    async def align_game_viewport(self) -> bool:
        """滚动对齐游戏区（登录识图前调用）。"""
        from backend.browser.game_frame_capture import align_game_viewport

        meta = await align_game_viewport(self._browser.page)
        if not meta.get("found"):
            self.script_log("未找到 GameCanvas，仍使用当前视口")
            return False
        if meta.get("scrolled"):
            self.script_log("已滚动对齐游戏区")
            self.invalidate_frame()
        return True

    async def match_image(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
            quiet: bool = False,
            match_mode: str = "image",
            pixel_tol: float = 8.0,
            use_hotspot_roi: bool | None = None,
    ):
        await self._task_ctrl.check()
        await self._observer.ensure_frame()

        key = (
            str(img_path),
            threshold,
            use_color_check,
            match_select,
            quiet,
            match_mode,
            pixel_tol,
            use_hotspot_roi,
        )

        if self.use_polling_temp_cache and key in self.polling_temp_cache:
            return self.polling_temp_cache[key]

        kw = dict(
            img_path=img_path,
            threshold=threshold,
            use_color_check=use_color_check,
            match_select=match_select,
            quiet=quiet,
            match_mode=match_mode,
            pixel_tol=pixel_tol,
        )
        if use_hotspot_roi is not None:
            kw["use_hotspot_roi"] = use_hotspot_roi

        rec = self._pseudo
        stem = Path(str(img_path)).stem if not str(img_path).startswith("data:") else "b64"
        sid = rec.begin("match", stem, threshold=threshold, mode=match_mode) if rec else None
        try:
            result = await self._browser.match_image(**kw)
            if rec:
                ok = bool(result)
                score = getattr(result, "score", getattr(result, "max_val", None))
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

        if self.use_polling_temp_cache:
            self.polling_temp_cache[key] = result

        # 仅成功匹配计入循环检测；失败是正常等待
        if result:
            self._stuck.note_action("match", img_path, True)

        return result

    async def match_images_parallel(
            self,
            specs: list[tuple],
            *,
            quiet: bool = True,
    ):
        """一次 ensure_frame 后并行匹配多模板（场景探测用）。"""
        await self._task_ctrl.check()
        await self._observer.ensure_frame()

        async def _one(spec: tuple):
            path, threshold, *rest = spec
            kw = rest[0] if rest else {}
            key = (
                str(path),
                threshold,
                kw.get("use_color_check", False),
                kw.get("match_select", "best"),
                quiet,
                kw.get("match_mode", "image"),
                kw.get("pixel_tol", 8.0),
            )
            if self.use_polling_temp_cache and key in self.polling_temp_cache:
                return self.polling_temp_cache[key]
            hit = await self._browser.match_image(
                path,
                threshold=threshold,
                quiet=quiet,
                **kw,
            )
            if self.use_polling_temp_cache:
                self.polling_temp_cache[key] = hit
            if hit:
                self._stuck.note_action("match", path, True)
            return hit

        return list(await asyncio.gather(*[_one(s) for s in specs]))

    async def match_image_multi(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_mode: str = "image",
            pixel_tol: float = 8.0,
    ):
        await self._task_ctrl.check()
        await self._observer.ensure_frame()
        return await self._browser.match_image_multi(
            img_path=img_path,
            threshold=threshold,
            use_color_check=use_color_check,
            match_mode=match_mode,
            pixel_tol=pixel_tol,
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
        import time
        _t0 = time.time()
        print(f"[UserBrowser.click_image] entered t=0", flush=True)
        await self._task_ctrl.check()
        await self._observer.ensure_frame()

        if not self.use_polling_temp_cache:
            pianyi = human_offset(pianyi)
            print(f"[UserBrowser.click_image] calling _browser.click_image t={time.time()-_t0:.3f}s", flush=True)
            result = await self._browser.click_image(
                img_path=img_path,
                pianyi=pianyi,
                down_time=down_time,
                threshold=threshold,
                use_color_check=use_color_check,
                match_select=match_select,
                match_mode=match_mode,
                pixel_tol=pixel_tol,
            )
            print(f"[UserBrowser.click_image] _browser.click_image done t={time.time()-_t0:.3f}s result={result}", flush=True)
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
                img_path=img_path,
                threshold=threshold,
                use_color_check=use_color_check,
                match_select=match_select,
                match_mode=match_mode,
                pixel_tol=pixel_tol,
            )

        match = self.polling_temp_cache[key]

        # 与 match_image 一致：低于阈值时 match 为 falsy，即使残留坐标也不可点
        if not match or match.x is None:
            self._emit_click_hud(str(img_path), False, getattr(match, "max_val", None) if match else None)
            self._stuck.note_action("click", img_path, False)
            return False

        offset = human_offset(pianyi)
        x = match.x + offset[0]
        y = match.y + offset[1]

        if getattr(self, "_is_debug", False):
            await self.draw_click_point(x, y, color="red")

        await self.click(x=x, y=y, down_time=down_time)
        self._note_progress()
        self._stuck.note_action("click", img_path, True)

        print(
            f"{self.account['name']}: 点击图片:{img_path}({x},{y}), "
            f"最大匹配度:{match.max_val}"
        )
        self._emit_click_hud(str(img_path), True, match.max_val, x=x, y=y)

        self.invalidate_frame()
        try:
            await self._observer.ensure_frame(force=True)
        except Exception:
            self.polling_temp_cache = {}
        return True

    def _emit_click_hud(self, img_path, ok, score=None, x=None, y=None):
        ctrl = getattr(self, "controller", None)
        if not ctrl and hasattr(self, "_browser"):
            ctrl = getattr(self._browser, "controller", None)
        if not ctrl or not hasattr(ctrl, "emit_match_event"):
            return
        try:
            account = self.account["name"]
        except Exception:
            return
        ctrl.emit_match_event(
            account=account,
            img_path=img_path,
            status="ok" if ok else "fail",
            score=score,
            action="click",
            x=x,
            y=y,
        )

    async def click_text(
            self,
            text: str,
            threshold: int = 60,
            pianyi=(0, 0),
            match_select: str = "best",
    ):
        await self._task_ctrl.check()
        pianyi = human_offset(pianyi)
        await self._browser.click_text(
            text=text,
            threshold=threshold,
            pianyi=pianyi,
            match_select=match_select,
        )

    # ===== 其余代码保持不变 =====

    async def dmm_login(self, *, timeout=30_000, eager: bool = False):
        """填邮箱 → 填密码 → 点登录按钮，脚本负责循环逻辑。

        eager=True：DOM 里出现输入框就填（不必等 fully visible），贴近「加载未完成就抢登录」。
        """
        await self._task_ctrl.check()
        page = self.page
        start = asyncio.get_event_loop().time()
        self._log("开始 DMM 登录检测" + ("（eager）" if eager else ""))

        def _on_login_page(url: str) -> bool:
            u = (url or "").lower()
            return "accounts.dmm" in u or "/login" in u or "login" in u

        # 已不在登录页（常见：缓存 URL 过期导致误调）直接跳过
        if not _on_login_page(page.url):
            self._log(f"当前不在 DMM 登录页，跳过（{page.url}）")
            return

        email_input = page.locator('#login_id')
        password_input = page.locator('#password')
        submit_btn = page.locator('button[type="submit"]')

        async def _ready(loc) -> bool:
            if await loc.count() <= 0:
                return False
            if eager:
                return True
            try:
                return await loc.is_visible()
            except Exception:
                return False

        async def _safe_fill(loc, selector: str, value: str, label: str) -> None:
            try:
                await loc.fill(value, timeout=5_000, force=True)
                self._log(label)
                return
            except Exception as e:
                self._log(f"{label} fill 失败，改 JS：{e}")
            try:
                await page.evaluate(
                    """([sel, v]) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        el.focus();
                        el.value = v;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }""",
                    [selector, value],
                )
                self._log(label + "（JS）")
            except Exception as e2:
                raise TimeoutError(f"{label} 失败: {e2}") from e2

        # 等待邮箱输入框
        while True:
            await self._task_ctrl.check()
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            if elapsed > timeout:
                # 超时了还没出现输入框 — 检查是否已经登录成功
                cur_url = page.url
                if not _on_login_page(cur_url):
                    self._log("邮箱输入框未出现，但已过登录页，视为已登录")
                    return
                raise TimeoutError("等待邮箱输入框超时")
            if not _on_login_page(page.url):
                self._log("等待中已离开登录页，视为已登录")
                return
            if await _ready(email_input):
                break
            await self.b_sleep(0.15 if eager else 0.2)

        if not _on_login_page(page.url):
            self._log("填写前已离开登录页，跳过")
            return

        # 填写邮箱（检查当前值，避免重复填入触发自动提交）
        try:
            current_email = await email_input.input_value(timeout=3_000)
        except Exception:
            current_email = ""
        if current_email != self.account['email']:
            await _safe_fill(
                email_input, '#login_id', self.account['email'],
                f"填写邮箱：{self.account['email']}",
            )
        else:
            self._log(f"邮箱已填写：{self.account['email']}")

        # 等待密码输入框
        while True:
            await self._task_ctrl.check()
            if (asyncio.get_event_loop().time() - start) * 1000 > timeout:
                raise TimeoutError("等待密码输入框超时")
            if not _on_login_page(page.url):
                self._log("等待密码时已离开登录页，视为已登录")
                return
            if await _ready(password_input):
                break
            await self.b_sleep(0.15 if eager else 0.2)

        # 填写密码（同上）
        try:
            current_pw = await password_input.input_value(timeout=3_000)
        except Exception:
            current_pw = ""
        if current_pw != self.account['password']:
            await _safe_fill(
                password_input, '#password', self.account['password'],
                "填写密码",
            )
        else:
            self._log("密码已填写")

        # 预取 reCAPTCHA token（如有），避免点击后因 token 未就绪导致提交被拒
        try:
            await page.evaluate('''() => {
                return new Promise(resolve => {
                    if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise) {
                        grecaptcha.enterprise.execute(
                            "6LfZLQEVAAAAAC-8pKwFNuzVoJW4tfUCghBX_7ZE",
                            {action: "PASSWORD_LOGIN"}
                        ).then(token => {
                            const el = document.querySelector('input[name="recaptchaToken"]');
                            if (el) el.value = token;
                            resolve(true);
                        }).catch(() => resolve(false));
                    } else {
                        resolve(false);
                    }
                });
            }''')
            self._log("reCAPTCHA token 已处理")
        except Exception:
            pass

        for attempt in range(2):
            if not _on_login_page(page.url):
                break
            try:
                await page.evaluate(
                    '() => document.querySelector("button[type=\'submit\']").click()'
                )
                self._log("登录按钮已点击 (JS evaluate)")
            except Exception as e:
                self._log(f"JS evaluate 失败: {e}")
                try:
                    await submit_btn.click(timeout=5000, force=True)
                    self._log("登录按钮已点击 (Playwright)")
                except Exception as e2:
                    self._log(f"Playwright click 也失败: {e2}")

            # 等待导航发生；如果页面 URL 仍含 login 说明提交未生效
            await self.b_sleep(1.0)
            cur_url = page.url
            if not _on_login_page(cur_url):
                break
            self._log(f"登录未生效，第 {attempt + 1} 次重试点击")

        if _on_login_page(page.url):
            self._log("DMM 登录结束（仍在登录页，交由外层重试）")
        else:
            self._log("DMM 登录完成")

    async def wait_image(self, img_path, timeout=0, threshold: float = 0.9):
        #0或负数表示无限等待
        deadline = datetime.now() + timedelta(seconds=timeout) if timeout > 0 else None

        while True:
            await self._task_ctrl.check()
            await self._observer.ensure_frame(force=True)

            if await self.match_image(img_path=img_path, threshold=threshold):
                return True

            if deadline and datetime.now() >= deadline:
                return False

            await self.b_sleep(0.5)

    async def click_until_gone(self, img_path, timeout=10):
        deadline = datetime.now() + timedelta(seconds=timeout)
        while datetime.now() < deadline:
            await self._task_ctrl.check()
            await self._observer.ensure_frame(force=True)
            if await self.click_image(img_path=img_path):
                continue
            else:
                return True
        return False

    @property
    async def get_url(self):
        # 必须读实时 URL：底层 _page_state_watcher 默认 15s 才刷缓存，
        # 登录跳转后若仍用 self.url 会误判还在 accounts.dmm 而反复 dmm_login。
        try:
            return self.page.url
        except Exception:
            return self.url

    @property
    async def get_title(self):
        try:
            return await self.page.title()
        except Exception:
            return self.title

    async def clear_session(self):
        """清除浏览器登录态（Cookie + Storage），用于切换平台账号。"""
        await self._browser.context.clear_cookies()
        try:
            await self._browser.page.evaluate("localStorage.clear()")
        except Exception:
            pass
        try:
            await self._browser.page.evaluate("sessionStorage.clear()")
        except Exception:
            pass


