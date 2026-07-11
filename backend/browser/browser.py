# backend/browser/browser.py
import asyncio
from pathlib import Path
from typing import Optional

import numpy as np

from .utils import get_port
from backend.matcher.matcher import matcher
from core.path import PROJECT_ROOT

from .mixins import (
    BaseMixin,
    LifecycleMixin,
    NavigationMixin,
    MouseActionsMixin,
    SlideActionsMixin,
    ScrollActionsMixin,
    KeyboardActionsMixin,
    ImageMatchingMixin,
    TextMatchingMixin,
    WaitingMixin,
    DebugMixin,
    UtilsMixin,
    MultiStepMixin,
)


class Browser(
    BaseMixin,
    LifecycleMixin,
    NavigationMixin,
    MouseActionsMixin,
    SlideActionsMixin,
    ScrollActionsMixin,
    KeyboardActionsMixin,
    ImageMatchingMixin,
    TextMatchingMixin,
    WaitingMixin,
    DebugMixin,
    UtilsMixin,
    MultiStepMixin,
):
    """
    Browser = 浏览器驱动 + 帧提供者（async 版）
    通过多重继承整合各个功能模块
    """

    def __init__(self, *, account: dict, controller, port: int | None = None):
        self.account = account
        self.controller = controller
        self.title = None
        self.url = None

        self.port = port if port is not None else get_port()

        self.user_data_dir = (
                PROJECT_ROOT /
                Path("browser_data")
                / account["name"]
        )
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        self._closed = False
        self._close_lock = asyncio.Lock()

        # playwright
        self.browser = None
        self.context = None
        self.page = None

        # frame cache
        self._frame: Optional[np.ndarray] = None
        self._frame_ts: float = 0.0

        # frame / viewport info
        self.device_pixel_ratio: float = 1.0

        self.matcher = matcher

        self._is_debug = False
        self._page_watch_task = None

        self._connect_task: asyncio.Task | None = None
        self._window_focused: bool = False  # 是否已聚焦过一次

        print(f"{self.account['name']}: 浏览器实例创建完成")

    async def check_connection(self) -> bool:
        """检测 Playwright 浏览器/页面连接是否存活"""
        if self._closed:
            return False
        if self.page is None:
            return False
        try:
            await self.page.evaluate("1 + 1")
            return True
        except Exception:
            return False