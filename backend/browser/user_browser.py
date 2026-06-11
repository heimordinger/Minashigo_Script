# backend/browser/user_browser.py
import inspect
import asyncio
import random
from datetime import datetime, timedelta

from pathlib import Path
from backend.browser.browser import Browser
from core.anti_ban.click_penalty import ClickPenalty
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
        self.click_penalty = ClickPenalty(self)

        # ===== 轮询缓存 =====
        self.use_polling_temp_cache = False
        self.polling_temp_cache = {}

        # 游戏属性
        self.minashigo_info = {
            "属性": None,
        }
        self.minashigo_attrs = {
            "光": "light",
            "暗": "dark",
            "水": "water",
            "火": "fire",
            "雷": "lightning",
            "风": "wind",
        }

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
        elapsed = 0.0
        while elapsed < seconds:
            await self._task_ctrl.check()
            await asyncio.sleep(step)
            elapsed += step

    async def click(self, x, y, down_time=0.12, pianyi=(0, 0), max_delay=None, start_count=0):
        await self._task_ctrl.check()
        await self.click_penalty.before(
            key=f"click({x}, {y})",
            max_delay=max_delay,
            start_count=start_count
        )
        pianyi = human_offset(pianyi)
        await self._browser.click(
            x=x,
            y=y,
            pianyi=pianyi,
            down_time=down_time
        )

    async def update_frame(self):
        """
        获取浏览器视口帧，但是每次获取时会重置一遍轮询临时结果。目的是在脚本每次循环时重置一遍，避免轮询错误引用上一轮循环的结果
        """
        await self._task_ctrl.check()
        await self._browser.update_frame()
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
            max_delay=None,
            start_count=0,
            use_color_check: bool = False,
            match_select: str = "best",
    ):
        await self._task_ctrl.check()

        if not self.use_polling_temp_cache:
            await self.click_penalty.before(
                key=f"click_image({img_path})",
                max_delay=max_delay,
                start_count=start_count,
            )
            pianyi = human_offset(pianyi)
            return await self._browser.click_image(
                img_path=img_path,
                pianyi=pianyi,
                down_time=down_time,
                threshold=threshold,
                use_color_check=use_color_check,
                match_select=match_select,
            )

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
            max_delay=None,
            start_count=0,
            match_select: str = "best",
    ):
        await self._task_ctrl.check()
        await self.click_penalty.before(
            key=f"click_text({text})",
            max_delay=max_delay,
            start_count=start_count,
        )
        pianyi = human_offset(pianyi)
        await self._browser.click_text(
            text=text,
            threshold=threshold,
            pianyi=pianyi,
            match_select=match_select,
        )

    # ===== 其余代码保持不变 =====

    async def dmm_login(self, *, game_name: str, timeout=30_000):
        await self._task_ctrl.check()
        page = self.page
        start = asyncio.get_event_loop().time()

        self._log("开始 DMM 登录检测")

        email_input = page.locator('#login_id')
        password_input = page.locator('#password')
        submit_btn = page.locator('form[name="loginForm"] button[type="submit"]')

        while True:
            await self._task_ctrl.check()
            if await email_input.count() > 0 and await password_input.count() > 0:
                if await email_input.is_visible() and await password_input.is_visible():
                    break
            if (asyncio.get_event_loop().time() - start) * 1000 > timeout:
                raise TimeoutError("等待登录表单超时")
            await self.b_sleep(0.2)

        self._log("检测到登录页，开始填充账号")
        await self.fill_input("login_id", self.account['email'], type="id")
        await self.fill_input("password", self.account['password'], type="id")
        await submit_btn.click()
        self._log("登录表单已提交")

        while True:
            await self._task_ctrl.check()
            if "accounts.dmm.co.jp" not in self._browser.url:
                break
            if (asyncio.get_event_loop().time() - start) * 1000 > timeout:
                break
            await self.b_sleep(0.3)

        while True:
            await self._task_ctrl.check()
            if game_name in self._browser.url and "Loading" not in self._browser.title:
                break
            if (asyncio.get_event_loop().time() - start) * 1000 > timeout:
                break
            await self.b_sleep(0.5)

        self._log("DMM 登录完成")

    async def wait_image(self, img_path, timeout=0):
        #0或负数表示无限等待
        deadline = datetime.now() + timedelta(seconds=timeout) if timeout > 0 else None

        while True:
            await self._task_ctrl.check()
            if await self._browser.match_image(img_path=img_path):
                return True

            if deadline and datetime.now() >= deadline:
                return False

            await self.b_sleep(0.5)

    async def click_until_gone(self, img_path, timeout=10, start_count=0):
        deadline = datetime.now() + timedelta(seconds=timeout)
        while datetime.now() < deadline:
            await self._task_ctrl.check()
            await self._browser.update_frame()
            if await self.click_image(img_path=img_path, start_count=start_count):
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


browser111 = Browser(controller=None, account={
    "name": "空实例",
    "email": "<EMAIL>",
    "password": "<PASSWORD>",
})
userBrowser = UserBrowser(browser=browser111, task_ctrl=None)
