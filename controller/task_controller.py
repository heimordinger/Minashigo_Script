# core/task/TaskRunner.py
import asyncio
import importlib
import sys
import traceback

from backend.browser.user_browser import UserBrowser
from controller.task_state import TaskStatus
from core.logging.events import LogLevel


class TaskStopped(Exception):
    """任务被外部请求停止"""
    pass


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
                    account=self.account["name"],
                    message="Browser 未初始化",
                    level=LogLevel.ERROR,
                    source="runner"
                )

                raise RuntimeError("Browser 未初始化")

            script = self._load_script()
            userBrowser = UserBrowser(browser=self.browser, task_ctrl=self)
            await script.do_work(userBrowser)

            self.controller.emit_task_state(
                name,
                status="任务完成",
                step="finished",
            )
            self.controller.on_task_finished(name)

        except TaskStopped:
            self.controller.emit_log(
                account=name,
                level=LogLevel.INFO,
                message="任务已停止",
                source="runner"
            )
            self.controller.emit_task_state(
                name,
                status="已停止",
                step="stopped",
            )

        except Exception:
            self.controller.emit_log(
                account=name,
                level=LogLevel.ERROR,
                message=traceback.format_exc(),
                source="runner"
            )
            self.controller.emit_task_state(
                name,
                status=TaskStatus.ERROR,
                step="exception",
            )

        finally:
            self._future = None

    # ===== 软导入 =====
    def _load_script(self):
        module_name = f"scripts.{self.task_name}"

        importlib.invalidate_caches()

        if module_name in sys.modules:
            del sys.modules[module_name]

        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            raise RuntimeError(
                f"脚本模块加载失败：{module_name}\n"
                f"请确认 scripts 目录下存在对应 .py 文件"
            ) from e

        if not hasattr(module, "do_work"):
            raise RuntimeError(f"{module_name} 缺少 do_work(browser)")

        return module

    def stop(self):
        self._stopped = True

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    async def check(self):
        while self._paused:
            await asyncio.sleep(0.2)

        if self._stopped:
            raise TaskStopped()
