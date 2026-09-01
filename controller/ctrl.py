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
    match_event = Signal(str, dict)  # account_name, {img_path, status, score}

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
        self._browsers: dict = {}  # name -> Browser (Playwright)
        self._browser_instances: dict = {}  # name -> UserBrowser（浏览器模式脚本调用对象）
        self._window_instances: dict = {}  # name -> UserWindow（窗口模式脚本调用对象）
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
        from taskflow.backend_handler import set_main_loop
        set_main_loop(self._loop); _ts("set_main_loop")
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

    def transfer_browser(self, from_account: dict, to_account: dict) -> bool:
        """把已启动的浏览器运行时从 from 账号迁到 to 账号（不重启 Chrome）。"""
        from_name = from_account.get("name", "")
        to_name = to_account.get("name", "")
        if not from_name or not to_name or from_name == to_name:
            return False

        has_from = from_name in self._browsers or from_name in self._browser_instances
        has_to = to_name in self._browsers or to_name in self._browser_instances
        if not has_from or has_to:
            return False

        self.stop_task(from_account)

        browser = self._browsers.pop(from_name, None)
        if browser is not None:
            browser.account = to_account
            self._browsers[to_name] = browser

        inst = self._browser_instances.pop(from_name, None)
        if inst is not None:
            self._browser_instances[to_name] = inst

        task_ctrl = self._task_ctrls.pop(from_name, None)
        if task_ctrl is not None:
            task_ctrl.account = to_account
            self._task_ctrls[to_name] = task_ctrl
        else:
            self._ensure_task_ctrl(to_account)

        task_state = self._tasks.pop(from_name, None)
        if task_state is not None:
            self._tasks[to_name] = task_state
        elif to_name not in self._tasks:
            self._tasks[to_name] = {
                "script": None,
                "status": "idle",
                "step": "",
                "message": "",
            }

        fut = self._connect_futures.pop(from_name, None)
        if fut is not None:
            self._connect_futures[to_name] = fut

        running = self._running.pop(from_name, None)
        if running is not None:
            self._running[to_name] = running

        win = getattr(self, "_window_instances", None)
        if isinstance(win, dict) and from_name in win:
            self._window_instances[to_name] = self._window_instances.pop(from_name)

        try:
            import sys
            from pathlib import Path
            taskflow_path = Path(__file__).parent.parent / "taskflow"
            if str(taskflow_path) not in sys.path:
                sys.path.insert(0, str(taskflow_path))
            from run_taskflow import browsers as tf_browsers
            old_email = from_account.get("email", "")
            new_email = to_account.get("email", "")
            if old_email and old_email in tf_browsers:
                wrapped = tf_browsers.pop(old_email)
                if new_email:
                    tf_browsers[new_email] = wrapped
        except Exception as e:
            print(f"[Controller] 移交 TaskFlow browsers 失败: {e}")

        if "_target" in from_account:
            to_account["_target"] = from_account.get("_target")

        self.emit_log(
            account=to_name,
            message=f"已继承浏览器（来自 {from_name}）",
            level=LogLevel.INFO,
            source="browser",
        )
        self.emit_log(
            account=from_name,
            message=f"浏览器已移交给 {to_name}",
            level=LogLevel.INFO,
            source="browser",
        )
        print(f"[Controller] 浏览器已移交: {from_name} → {to_name}")
        return True

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

        # 在 connect（会发 ready）之前注册 TaskController，避免 UI 收到 ready 后立刻 start_task 时 KeyError
        self._ensure_task_ctrl(account)

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
            from taskflow.backend_handler import MainLoopProxy
            from backend.browser.user_browser import UserBrowser

            task_ctrl = self._task_ctrls[account["name"]]
            user_browser = UserBrowser(browser=browser, task_ctrl=task_ctrl)
            wrapped = MainLoopProxy(user_browser)

            account_email = account.get('email', '')
            if account_email:
                browsers[account_email] = wrapped
                self._browser_instances[account['name']] = wrapped
                print(f"[Controller] 已存储UserBrowser到_browser_instances和run_taskflow.browsers: {account_email}")
            else:
                print(f"[Controller] 账号缺少邮箱信息: {account}")

            # connect() 内部会先发 ready；此时 UserBrowser 可能尚未入库。
            # 入库后再发一次 ready，确保 UI/排队开跑时实例已可用。
            from core.state.events import StateEvent, StateDomain
            self.emit_state(
                StateEvent(
                    account=account["name"],
                    domain=StateDomain.BROWSER,
                    key="ready",
                    value=True,
                    message="UserBrowser 已就绪",
                )
            )

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

    def _ensure_task_ctrl(self, account: dict):
        """确保账号有 _tasks / _task_ctrls 槽位（兼容旧调用顺序与竞态）。"""
        name = account["name"]
        if name not in self._tasks:
            self._tasks[name] = {
                "script": None,
                "status": "idle",
                "step": "",
                "message": "",
            }
        if name not in self._task_ctrls:
            from controller.task_controller import TaskController
            self._task_ctrls[name] = TaskController(
                account=account,
                task_name="",
                controller=self,
            )

    def register_window_target(self, account: dict):
        """窗口模式：注册账号并创建 UserWindow，存储到 _window_instances。"""
        name = account["name"]
        if name not in self._tasks:
            self._tasks[name] = {
                "script": None,
                "status": "idle",
                "step": "",
                "message": ""
            }
        if name not in self._task_ctrls:
            from controller.task_controller import TaskController
            self._task_ctrls[name] = TaskController(
                account=account,
                task_name="",
                controller=self,
            )

        # 创建并存储 UserWindow，与 _browser_instances 分开
        hwnd = account.get("window_hwnd")
        if hwnd:
            from backend.automation.user_window import UserWindow
            from backend.automation.win32_target import Win32Target
            target = Win32Target.from_hwnd(hwnd)
            user_window = UserWindow(target=target, task_ctrl=self._task_ctrls[name])
            user_window.account = account
            self._window_instances[name] = user_window

            # 注册到 TaskFlow 的全局 browsers 池，让 WebSocket 命令也能找到窗口目标
            # 注册到 TaskFlow 全局池
            try:
                import sys
                from pathlib import Path
                tf_path = Path(__file__).parent.parent / "taskflow"
                if str(tf_path) not in sys.path:
                    sys.path.insert(0, str(tf_path))
                from run_taskflow import browsers as tf_browsers
                email = account.get('email', '')
                if email:
                    tf_browsers[email] = user_window
                    print(f"[Controller] 已注册 UserWindow 到 TaskFlow browsers: {email}")
            except Exception as e:
                print(f"[Controller] 注册 UserWindow 到 TaskFlow 失败: {e}")

            print(f"[Controller] 已创建 UserWindow (hwnd={hwnd})")
        else:
            self._window_instances.pop(name, None)

        print(f"[Controller] 窗口目标已注册: {name}")

    def sync_taskflow_target(self, account: dict):
        """当下拉框目标切换时，同步 TaskFlow 的 browsers 池。"""
        name = account["name"]
        want_window = account.get("_target") == "window"
        instance = self._window_instances.get(name) if want_window else self._browser_instances.get(name)
        if instance is None:
            return

        try:
            import sys
            from pathlib import Path
            tf_path = Path(__file__).parent.parent / "taskflow"
            if str(tf_path) not in sys.path:
                sys.path.insert(0, str(tf_path))
            from run_taskflow import browsers as tf_browsers
            email = account.get('email', '')
            if email:
                tf_browsers[email] = instance
                print(f"[Controller] 同步 TaskFlow 目标: {'窗口' if want_window else '浏览器'}")
        except Exception as e:
            print(f"[Controller] 同步 TaskFlow 目标失败: {e}")

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

        # 防御：ready 早于 UserBrowser 注册、或旧路径未建 TaskController
        self._ensure_task_ctrl(account)

        # connect() 发 ready 时 UserBrowser 可能还没入库；短等再开跑
        if name not in self._browser_instances and name not in self._window_instances:
            async def _wait_then_start():
                for _ in range(50):  # ~5s
                    if name in self._browser_instances or name in self._window_instances:
                        break
                    await asyncio.sleep(0.1)
                if name not in self._browser_instances and name not in self._window_instances:
                    self.emit_log(
                        account=name,
                        message="浏览器/窗口尚未就绪，无法启动任务",
                        level=LogLevel.ERROR,
                        source="runner",
                    )
                    return
                self._tasks[name]["script"] = task_name
                self.emit_task_state(
                    name,
                    status=TaskStatus.RUNNING,
                    script=task_name,
                    message=f"开始执行任务：{task_name}",
                )
                self._task_ctrls[name].start(task_name=task_name)

            self.submit(_wait_then_start())
            return

        self._tasks[name]["script"] = task_name

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
        try:
            from core.demo_mode import is_demo_mode, mask_sensitive

            if is_demo_mode():
                message = mask_sensitive(message)
        except Exception:
            pass
        event = LogEvent(
            account=account,
            level=level,
            message=message,
            source=source
        )

        self._log_buffer[account].append(event)
        self.log_signal.emit(account, event)

    def emit_match_event(
            self,
            *,
            account: str,
            img_path: str,
            status: str,
            score: float | None = None,
            action: str = "match",
            x: float | int | None = None,
            y: float | int | None = None,
    ):
        """匹配/点击调试事件。status = matching | ok | fail；action = match | click"""
        self.match_event.emit(account, {
            "img_path": str(img_path),
            "status": status,
            "score": score,
            "action": action,
            "x": x,
            "y": y,
        })

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

    def capture_screenshot(self, account: dict):
        """按 account['_target'] 选择截图来源。"""
        account_name = account["name"]

        if account.get("_target") == "window":
            win = self._window_instances.get(account_name)
            if not win:
                return
            future = self.submit(win.update_frame())
        else:
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
