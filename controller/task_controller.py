import asyncio
import importlib
import sys
import traceback

from backend.browser.user_browser import UserBrowser
from controller.task_state import TaskStatus
from core.logging.events import LogLevel


class TaskStopped(Exception):
    """Task was stopped by an external request."""


class TaskController:
    def __init__(self, *, account: dict, task_name: str, controller):
        self.account = account
        self.task_name = task_name
        self.controller = controller
        self.browser = controller.get_browser_by_account_name(account["name"])
        self._future = None
        self._paused = False
        self._stopped = False

    def start(self, *, task_name):
        if self._future:
            return
        self._stopped = False
        self._paused = False
        self.task_name = task_name
        self._future = self.controller.submit(self._run_async())

    async def _run_async(self):
        name = self.account["name"]

        try:
            if self.browser is None:
                self.controller.emit_log(
                    account=name,
                    message="Browser 未初始化",
                    level=LogLevel.ERROR,
                    source="runner",
                )
                raise RuntimeError("Browser 未初始化")

            script = self._load_script()
            
            # 从全局browsers池获取UserBrowser实例
            import sys
            from pathlib import Path
            taskflow_path = Path(__file__).parent.parent / "taskflow"
            if str(taskflow_path) not in sys.path:
                sys.path.insert(0, str(taskflow_path))
            
            from run_taskflow import browsers
            account_email = self.account.get('email', '')
            
            if account_email and account_email in browsers:
                user_browser = browsers[account_email]
                print(f"[TaskController] 从全局browsers池获取UserBrowser: {account_email}")
            else:
                print(f"[TaskController] 无法找到UserBrowser: {account_email}")
                print(f"[TaskController] 可用的browsers: {list(browsers.keys())}")
                # 如果找不到，创建临时的（这种情况不应该发生）
                user_browser = UserBrowser(browser=self.browser, task_ctrl=self)
                print(f"[TaskController] 创建临时UserBrowser实例")
            
            await script.do_work(user_browser)

            self.controller.on_task_finished(name)

        except (TaskStopped, asyncio.CancelledError):
            self.controller.emit_log(
                account=name,
                level=LogLevel.INFO,
                message="任务已停止",
                source="runner",
            )
            self.controller.on_task_stopped(name)

        except Exception:
            self.controller.emit_log(
                account=name,
                level=LogLevel.ERROR,
                message=traceback.format_exc(),
                source="runner",
            )
            self.controller.emit_task_state(
                name,
                status=TaskStatus.ERROR,
                step="exception",
                message="任务执行异常",
            )

        finally:
            self._future = None
            self._stopped = False
            self._paused = False

    def _load_script(self):
        module_name = f"scripts.{self.task_name}"

        importlib.invalidate_caches()

        if module_name in sys.modules:
            del sys.modules[module_name]

        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"脚本模块加载失败：{module_name}\n"
                f"请确认 scripts 目录下存在对应的 .py 文件"
            ) from exc

        if not hasattr(module, "do_work"):
            raise RuntimeError(f"{module_name} 缺少 do_work(browser)")

        return module

    def stop(self):
        self._stopped = True
        if self._future and not self._future.done():
            self._future.cancel()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    async def check(self):
        while self._paused:
            await asyncio.sleep(0.2)

        if self._stopped:
            raise TaskStopped()
