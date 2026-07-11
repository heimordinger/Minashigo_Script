# backend/browser/user_browser.py
import inspect
import asyncio
import random
from datetime import datetime, timedelta

from pathlib import Path
from backend.browser.browser import Browser
from typing import Tuple, Union


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

        self._watchdog_idle: float = 300.0   # 5 分钟无操作视为卡死
        self._last_successful_click: float = __import__('time').time()

    def script_log(self, msg: str):
        self._browser.script_log(msg)

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

    async def b_sleep(self, seconds: float, upper_limit: float | None = None, step: float = 0.05):
        if upper_limit is not None:
            if upper_limit < seconds:
                seconds, upper_limit = upper_limit, seconds
            seconds = random.uniform(seconds, upper_limit)
        if seconds <= 0:
            return
        # 看门狗：超过 watchdog_idle 秒无有效点击 → 视为卡死
        idle = __import__('time').time() - self._last_successful_click
        if idle > self._watchdog_idle:
            raise RuntimeError(
                f"看门狗触发：{idle:.0f}秒内无有效操作，"
                f"超过上限 {self._watchdog_idle:.0f}s，脚本可能已卡死"
            )
        elapsed = 0.0
        while elapsed < seconds:
            await self._task_ctrl.check()
            await asyncio.sleep(step)
            elapsed += step

    async def click(self, x, y, down_time=0.12, pianyi=(0, 0)):
        await self._task_ctrl.check()
        pianyi = human_offset(pianyi)
        await self._browser.click(
            x=x,
            y=y,
            pianyi=pianyi,
            down_time=down_time
        )

    async def update_frame(self, save_screenshot=False):
        """
        获取浏览器视口帧，但是每次获取时会重置一遍轮询临时结果。目的是在脚本每次循环时重置一遍，避免轮询错误引用上一轮循环的结果
        """
        await self._task_ctrl.check()
        await self._browser.update_frame(save_screenshot=save_screenshot)
        self.polling_temp_cache = {}

    async def match_image(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
    ):
        await self._task_ctrl.check()

        key = (
            str(img_path),
            threshold,
            use_color_check,
            match_select,
        )

        if self.use_polling_temp_cache and key in self.polling_temp_cache:
            return self.polling_temp_cache[key]

        result = await self._browser.match_image(
            img_path=img_path,
            threshold=threshold,
            use_color_check=use_color_check,
            match_select=match_select,
        )

        if self.use_polling_temp_cache:
            self.polling_temp_cache[key] = result

        return result

    async def click_image(
            self,
            img_path: Union[str, Path],
            pianyi=(0, 0),
            down_time=0.12,
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
            max_delay: float | None = None,
    ):
        import time
        _t0 = time.time()
        print(f"[UserBrowser.click_image] entered t=0", flush=True)
        await self._task_ctrl.check()

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
            )
            print(f"[UserBrowser.click_image] _browser.click_image done t={time.time()-_t0:.3f}s result={result}", flush=True)
            if result:
                self._last_successful_click = __import__('time').time()
            return result

        key = (
            str(img_path),
            threshold,
            use_color_check,
            match_select,
        )

        if key not in self.polling_temp_cache:
            self.polling_temp_cache[key] = await self.match_image(
                img_path=img_path,
                threshold=threshold,
                use_color_check=use_color_check,
                match_select=match_select,
            )

        match = self.polling_temp_cache[key]

        if not match or match.x is None:
            return False

        offset = human_offset(pianyi)
        x = match.x + offset[0]
        y = match.y + offset[1]

        if getattr(self, "_is_debug", False):
            await self.draw_click_point(x, y, color="red")

        await self.click(x=x, y=y, down_time=down_time)
        self._last_successful_click = __import__('time').time()

        print(
            f"{self.account['name']}: 点击图片:{img_path}({x},{y}), "
            f"最大匹配度:{match.max_val}"
        )

        return True

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

    async def dmm_login(self, *, timeout=30_000):
        """填邮箱 → 填密码 → 点登录按钮，脚本负责循环逻辑"""
        await self._task_ctrl.check()
        page = self.page
        start = asyncio.get_event_loop().time()
        self._log("开始 DMM 登录检测")

        email_input = page.locator('#login_id')
        password_input = page.locator('#password')
        submit_btn = page.locator('button[type="submit"]')

        # 等待邮箱输入框
        while True:
            await self._task_ctrl.check()
            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            if elapsed > timeout:
                # 超时了还没出现输入框 — 检查是否已经登录成功
                cur_url = page.url
                if "login" not in cur_url.lower() and "accounts.dmm" not in cur_url.lower():
                    self._log("邮箱输入框未出现，但已过登录页，视为已登录")
                    return
                raise TimeoutError("等待邮箱输入框超时")
            if await email_input.count() > 0 and await email_input.is_visible():
                break
            await self.b_sleep(0.2)

        # 填写邮箱（检查当前值，避免重复填入触发自动提交）
        current_email = await email_input.input_value()
        if current_email != self.account['email']:
            await email_input.fill(self.account['email'])
            self._log(f"填写邮箱：{self.account['email']}")
        else:
            self._log(f"邮箱已填写：{self.account['email']}")

        # 等待密码输入框
        while True:
            await self._task_ctrl.check()
            if (asyncio.get_event_loop().time() - start) * 1000 > timeout:
                raise TimeoutError("等待密码输入框超时")
            if await password_input.count() > 0 and await password_input.is_visible():
                break
            await self.b_sleep(0.2)

        # 填写密码（同上）
        current_pw = await password_input.input_value()
        if current_pw != self.account['password']:
            await password_input.fill(self.account['password'])
            self._log("填写密码")
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
            if "login" not in cur_url.lower():
                break
            self._log(f"登录未生效，第 {attempt + 1} 次重试点击")

        self._log("DMM 登录完成")

    async def wait_image(self, img_path, timeout=0):
        #0或负数表示无限等待
        deadline = datetime.now() + timedelta(seconds=timeout) if timeout > 0 else None

        while True:
            await self._task_ctrl.check()
            await self._browser.update_frame()
            
            if await self._browser.match_image(img_path=img_path):
                return True

            if deadline and datetime.now() >= deadline:
                return False

            await self.b_sleep(0.5)

    async def click_until_gone(self, img_path, timeout=10):
        deadline = datetime.now() + timedelta(seconds=timeout)
        while datetime.now() < deadline:
            await self._task_ctrl.check()
            await self._browser.update_frame()
            if await self.click_image(img_path=img_path):
                continue
            else:
                return True
        return False

    @property
    async def get_url(self):
        return self.url

    @property
    async def get_title(self):
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


