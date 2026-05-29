# controller/ctrl.py
import asyncio
import threading
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from controller.task_state import TaskStatus, TaskSnapshot
from core.logging.events import LogEvent, LogLevel
from core.state.events import StateEvent, StateDomain
from core.state.unified_event import UnifiedEvent


class Controller(QObject):
    log_signal = Signal(str, LogEvent)
    log_event = Signal(dict)
    state_event = Signal(object)
    show_overlay_signal = Signal(str)
    close_overlay_signal = Signal(str)
    screenshot_ready = Signal(str, object)

    def __init__(self):
        _t0 = __import__('time').time()
        def _ts(step):
            print(f"[Ctrl]  {step}: {__import__('time').time()-_t0:.3f}s")

        super().__init__(); _ts("QObject.__init__")

        self._pw = None
        self._mouse_watch_on = False
        self._mouse_watch_task = None

        # ---------- listeners ----------
        self._listeners = []
        self._log_listeners = []

        # ---------- logging ----------
        self._log_buffer: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

        # ---------- runtime objects ----------
        self._browsers: dict = {}  # name -> Browser
        self._tasks = {}  # name -> mutable dict
        self._task_ctrls: dict = {}  # name -> TaskController
        self._running = {}  # name -> asyncio.Future
        self._connect_futures: dict[str, asyncio.Future] = {}

        # ---------- port management ----------
        from core.port_manager import port_manager
        self._port_manager = port_manager
        self._browser_port_counter = 0  # 用于分配浏览器端口

        # ---------- Playwright 初始化标记 ----------
        self._pw_failed = False  # Playwright 初始化是否已失败
        _ts("准备工作完成")

        # ---------- asyncio loop ----------
        self._loop = asyncio.new_event_loop(); _ts("new_event_loop")
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True
        )
        self._thread.start(); _ts("thread.start")

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
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        except Exception as e:
            print(f"[Controller] Playwright 初始化失败: {e}")
            self._pw = None
            self._pw_failed = True

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
        # ====== 等待 Playwright 就绪（异步初始化可能还没完成） ======
        start_ts = asyncio.get_event_loop().time()
        while self._pw is None and not self._pw_failed:
            await asyncio.sleep(0.1)
            if asyncio.get_event_loop().time() - start_ts > 120:
                raise TimeoutError("Playwright init timeout (全局)")

        # 获取浏览器调试端口（原子分配，无竞态）
        debug_port = self._port_manager.reserve_browser_port(self._browser_port_counter)
        self._browser_port_counter += 1

        print(f"[Controller] 为账号 {account['name']} 分配调试端口: {debug_port}")

        # 传入 port，不再调用 get_port()，避免竞态条件
        from backend.browser.browser import Browser
        browser = Browser(account=account, controller=self, port=debug_port)

        # 提前注册，这样即使 connect 失败，reconnect 能找到它，复用同一端口
        self.register_browser(account["name"], browser)

        await asyncio.to_thread(browser.start)

        connect_task = asyncio.create_task(browser.connect())
        self._connect_futures[account["name"]] = connect_task

        try:
            await connect_task

            # 浏览器连接成功后，创建UserBrowser实例并存储到全局browsers字典
            print(f"[Controller] 浏览器连接成功，创建UserBrowser实例")

            import sys
            from pathlib import Path
            taskflow_path = Path(__file__).parent.parent / "taskflow"
            if str(taskflow_path) not in sys.path:
                sys.path.insert(0, str(taskflow_path))

            from run_taskflow import browsers
            from backend.browser.user_browser import UserBrowser

            class TempTaskCtrl:
                async def check(self):
                    pass
                def emit_log(self, account, message, level, source):
                    pass

            temp_task_ctrl = TempTaskCtrl()
            user_browser = UserBrowser(browser=browser, task_ctrl=temp_task_ctrl)

            account_email = account.get('email', '')
            if account_email:
                browsers[account_email] = user_browser
                print(f"[Controller] 已存储UserBrowser到browsers字典: {account_email}")
            else:
                print(f"[Controller] 账号缺少邮箱信息: {account}")

        except asyncio.CancelledError:
            self.emit_log(
                account=account["name"],
                message="连接被取消",
                level=LogLevel.WARNING,
                source="browser"
            )
            return
        except Exception:
            # connect 失败：清理 Chrome 进程，避免 user-data-dir 被锁
            try:
                await browser.close()
            except Exception:
                pass
            self._browsers.pop(account["name"], None)
            self._connect_futures.pop(account["name"], None)
            raise

        from controller.task_controller import TaskController
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
            # 首次重连且无 Browser 记录，新建一个（用 PortManager 分配端口）
            debug_port = self._port_manager.reserve_browser_port(self._browser_port_counter)
            self._browser_port_counter += 1
            from backend.browser.browser import Browser
            browser = Browser(account=account, controller=self, port=debug_port)
            self._browsers[name] = browser
        else:
            # 复用已有 Browser（port 不变），先清理旧 Playwright 状态
            old_page = getattr(browser, 'page', None)
            if old_page is not None:
                try:
                    await old_page.close()
                except Exception:
                    pass
            old_ctx = getattr(browser, 'context', None)
            if old_ctx is not None:
                try:
                    await old_ctx.close()
                except Exception:
                    pass
            old_bw = getattr(browser, 'browser', None)
            if old_bw is not None:
                try:
                    await old_bw.close()
                except Exception:
                    pass
            browser.page = None
            browser.context = None
            browser.browser = None

        try:
            # 先确保 Chrome 进程在运行，再连接
            await asyncio.to_thread(browser.start)
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

        # 重连成功后确保 TaskController 存在（旧的可能随异常被清理了）
        if name not in self._task_ctrls:
            from controller.task_controller import TaskController
            task_ctrl = TaskController(
                account=account,
                task_name="",
                controller=self,
            )
            self._task_ctrls[name] = task_ctrl

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
            self.emit_task_state(
                name,
                status="stopping",
                step="stopping",
                message="正在停止任务"
            )
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

    def on_task_stopped(self, account_name: str):
        task = self._tasks.get(account_name)
        if not task:
            return

        script = task.get("script") or "任务"
        self.emit_task_state(
            account_name,
            status=TaskStatus.STOPPED,
            step="stopped",
            message=f"{script} 已停止"
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

    def capture_screenshot(self, account_name: str):
        browser = self._browsers.get(account_name)
        if not browser:
            return

        future = self.submit(browser.update_frame())
        future.add_done_callback(lambda f: self._on_screenshot_done(account_name, f))

    def _on_screenshot_done(self, account_name: str, future):
        try:
            frame = future.result()
        except Exception as e:
            print(f"截图失败: {e}")
            return
        self.screenshot_ready.emit(account_name, frame)
