# controller/ctrl.py
import asyncio
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal
from playwright.async_api import async_playwright

from backend.browser.browser import Browser
from controller.task_controller import TaskController
from controller.task_state import TaskStatus, TaskSnapshot
from core.logging.events import LogEvent, LogLevel
from core.state.events import StateEvent, StateDomain
from core.state.unified_event import UnifiedEvent


class Controller(QObject):
    # =========================
    # Qt Signals
    # =========================
    log_signal = Signal(str, LogEvent)
    log_event = Signal(dict)
    state_event = Signal(object)
    show_overlay_signal = Signal(str)
    close_overlay_signal = Signal(str)
    screenshot_ready = Signal(str, object)

    # =========================
    # Lifecycle
    # =========================
    def __init__(self):
        super().__init__()

        self._pw = None
        self._mouse_watch_on = False
        self._mouse_watch_task = None

        # ---------- listeners ----------
        self.template_cache: dict[Path, np.ndarray] = {}
        self._listeners = []
        self._log_listeners = []

        # ---------- logging ----------
        self._log_buffer: dict[str, list[str]] = defaultdict(list)

        # ---------- runtime objects ----------
        self._browsers: dict[str, Browser] = {}  # name -> Browser
        self._tasks = {}  # name -> mutable dict
        self._task_ctrls: dict[str, TaskController] = {}  # name -> TaskController
        self._running = {}  # name -> asyncio.Future
        self._connect_futures: dict[str, asyncio.Future] = {}

        # ---------- asyncio loop ----------
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True
        )
        self._thread.start()

    def shutdown(self):
        for fut in self._running.values():
            fut.cancel()

        for browser in self._browsers.values():
            self.submit(browser.close())

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    # =========================
    # Asyncio Event Loop
    # =========================
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._init_playwright())
        self._loop.run_forever()

    def get_playwright(self):
        return self._pw

    async def _init_playwright(self):
        self._pw = await async_playwright().start()

    def submit(self, coro):
        """从 GUI 线程安全提交 async 任务"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # =========================
    # Browser Management
    # =========================
    def register_browser(self, name: str, browser):
        self._browsers[name] = browser
        self._tasks[name] = {
            "script": None,
            "status": 'idle',
            "step": "",
            "message": ""
        }

    def start_browser_async(self, account: dict):
        asyncio.run_coroutine_threadsafe(
            self._start_browser(account),
            self._loop
        )

    async def _start_browser(self, account: dict):
        browser = Browser(account=account, controller=self)

        await asyncio.to_thread(browser.start)

        connect_task = asyncio.create_task(browser.connect())
        self._connect_futures[account["name"]] = connect_task

        try:
            await connect_task
        except asyncio.CancelledError:
            self.emit_log(
                account=account["name"],
                message="连接被取消",
                level=LogLevel.WARNING,
                source="browser"
            )
            return

        self.register_browser(account["name"], browser)

        task_ctrl = TaskController(
            account=account,
            task_name="",
            controller=self,
        )
        self._task_ctrls[account['name']] = task_ctrl

    def cancel_connect(self, account: dict):
        fut = self._connect_futures.get(account['name'])
        if not fut:
            return

        fut.cancel()

    def reconnect_browser(self, account: dict):
        name = account["name"]

        self.emit_log(
            account=name,
            message="请求重新连接浏览器",
            level=LogLevel.INFO,
            source="controller"
        )

        self.cancel_connect(account)

        asyncio.run_coroutine_threadsafe(
            self._reconnect_async(account),
            self._loop
        )

    async def _reconnect_async(self, account: dict):
        name = account["name"]

        browser = self._browsers.get(name)
        if not browser:
            browser = Browser(account=account, controller=self)
            self._browsers[name] = browser

        try:
            task = asyncio.create_task(browser.connect())
            self._connect_futures[name] = task
            await task
        except asyncio.CancelledError:
            self.emit_log(
                account=name,
                message="重连已取消",
                level=LogLevel.WARNING,
                source="browser"
            )
            return
        except Exception as e:
            self.emit_log(
                account=name,
                message=f"重连失败: {e}",
                level=LogLevel.ERROR,
                source="browser"
            )
            return
        finally:
            await self._connect_futures.pop(name, None)

        self.emit_log(
            account=name,
            message="浏览器重连成功",
            level=LogLevel.INFO,
            source="browser"
        )

    def subscribe(self, fn):
        """
        UI / Facade 注册状态监听
        fn: Callable[[dict[str, TaskSnapshot]], None]
        """
        self._listeners.append(fn)

    def start_task(self, account: dict, task_name: str):
        name = account["name"]

        self._tasks[name]["script"] = task_name
        browser = self._browsers.get(name)
        if not browser:
            raise RuntimeError("Browser 未初始化")

        self.emit_task_state(
            name,
            status=TaskStatus.RUNNING,
            script=task_name,
            message=f"开始执行任务：{task_name}"
        )

        self._task_ctrls[name].start(task_name=task_name)

    def stop_task(self, account: dict):
        name = account["name"]
        task_ctrl = self._task_ctrls.get(name)
        if task_ctrl:
            task_ctrl.stop()

    def on_task_finished(self, account_name: str, result="执行完成"):
        task = self._tasks.get(account_name)
        if not task:
            return

        self.emit_task_state(
            account_name,
            status=TaskStatus.FINISHED,
            step="finished",
            message=f"{task['script']} {result}"
        )

        self.emit_state(
            StateEvent(
                account=account_name,
                domain=StateDomain.RUNTIME,
                key="running",
                value=False
            )
        )

    def subscribe_log(self, fn):
        """fn: Callable[[LogEvent], None]"""
        self._log_listeners.append(fn)

    def emit_log(self, *, account: str, message: str, level, source):
        event = LogEvent(
            account=account,
            level=level,
            message=message,
            source=source
        )

        self._log_buffer[account].append(event)
        self.log_signal.emit(account, event)

    def emit_state(self, data):
        if isinstance(data, dict):
            event = UnifiedEvent(
                type="task",
                payload=data
            )
        elif isinstance(data, StateEvent):
            event = UnifiedEvent(
                type="runtime",
                payload=data
            )
        else:
            event = UnifiedEvent(
                type="legacy",
                payload=data
            )

        self.state_event.emit(event)
        print("event:", event)

    def get_browser_by_account_name(self, account_name: str):
        return self._browsers.get(account_name)

    def emit_task_state(
            self,
            account_name: str,
            *,
            status: TaskStatus | str,
            step: str = "",
            message: str = "",
            script: str | None = None,
    ):
        task = self._tasks.get(account_name)
        if not task:
            return

        if isinstance(status, TaskStatus):
            status_str = status.value
        elif isinstance(status, str):
            status_str = status
        else:
            raise TypeError(...)

        task["status"] = status_str
        task["step"] = step
        task["message"] = message

        snapshot = TaskSnapshot(
            browser=account_name,
            script=script or task.get("script"),
            status=status_str,
            step=step,
            message=message
        )

        self.state_event.emit(
            UnifiedEvent(
                type="task",
                payload=snapshot
            )
        )

    def get_template(self, path: Path, loader):
        if path not in self.template_cache:
            self.template_cache[path] = loader(path)
        return self.template_cache[path]

    def capture_screenshot(self, account_name: str):
        browser = self._browsers.get(account_name)
        if not browser:
            return

        future = self.submit(browser.update_frame())
        frame = future.result()

        self.screenshot_ready.emit(account_name, frame)
