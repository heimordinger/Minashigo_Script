import asyncio
import importlib
import inspect
import sys
import traceback

from typing import get_origin, get_args

from backend.browser.user_browser import UserBrowser
from controller.task_state import TaskStatus
from core.logging.events import LogLevel


def _resolve_type_name(annot) -> str:
    """从类型注解中提取可读的名称字符串。"""
    if isinstance(annot, str):
        return annot
    # 尝试取 __name__（常规类）或 _name（GenericAlias）
    return getattr(annot, '__name__', getattr(annot, '_name', str(annot)))


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
            # 先加载脚本，通过 do_work 的参数类型注解决定目标
            script = self._load_script()
            prefer_window = self._detect_prefer_window(script)

            # ========== 按注解类型 + _target 倾向取实例 ==========
            user_browser = None

            if prefer_window is None:
                # Union(UserBrowser | UserWindow)：看 _target 倾向，不存在则 fallback
                want_window = self.account.get("_target") == "window"
                if want_window:
                    user_browser = (self.controller._window_instances.get(name)
                                    or self.controller._browser_instances.get(name))
                else:
                    user_browser = (self.controller._browser_instances.get(name)
                                    or self.controller._window_instances.get(name))

                if user_browser is None:
                    self.controller.emit_log(
                        account=name,
                        message="浏览器和窗口均未就绪，请先启动浏览器或选择窗口",
                        level=LogLevel.ERROR,
                        source="runner",
                    )
                    raise RuntimeError("浏览器/窗口均未就绪")

            else:
                # 单类型标注：_target 可覆盖注解
                manual_target = self.account.get("_target")
                if manual_target is not None:
                    prefer_window = manual_target == "window"

                instances = (self.controller._window_instances
                             if prefer_window else self.controller._browser_instances)
                user_browser = instances.get(name)
                if user_browser is None:
                    hint = "请先选择窗口" if prefer_window else "请先启动浏览器"
                    self.controller.emit_log(
                        account=name,
                        message=f"脚本需要{'窗口' if prefer_window else '浏览器'}目标，{hint}",
                        level=LogLevel.ERROR,
                        source="runner",
                    )
                    raise RuntimeError(f"脚本需要{'窗口' if prefer_window else '浏览器'}目标，{hint}")

            # 日志确认实际使用的对象类型
            type_name = type(user_browser).__name__
            if hasattr(user_browser, '_obj'):
                type_name = f"MainLoopProxy({type(user_browser._obj).__name__})"
            print(f"[TaskController] 使用 {type_name} 执行脚本 {self.task_name}")

            # 重置看门狗：脚本启动时开始计时，而非浏览器创建时
            _target = getattr(user_browser, '_obj', user_browser)
            if hasattr(_target, '_last_successful_click'):
                _target._last_successful_click = __import__('time').time()
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
        # 将相对路径（如 孤儿/孤儿登录.py）转为模块名（如 scripts.孤儿.孤儿登录）
        module_name = "scripts." + self.task_name.replace("/", ".").replace(".py", "")

        importlib.invalidate_caches()

        # 强制删除 .pyc 缓存，确保下次 import 重新编译
        try:
            from pathlib import Path
            from core.path import SCRIPTS_PATH
            script_file = SCRIPTS_PATH / self.task_name
            if not script_file.exists():
                script_file = script_file.with_suffix(".py")
            cache_path = importlib.util.cache_from_source(str(script_file))
            if Path(cache_path).exists():
                Path(cache_path).unlink()
        except Exception:
            pass

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

        # 校验 do_work 参数类型标注
        self._validate_do_work_annotation(module.do_work, module_name)

        return module

    @staticmethod
    def _validate_do_work_annotation(func, module_name: str):
        """校验 do_work 的第一个参数标注了 UserBrowser 或 UserWindow。"""
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        if not params:
            raise RuntimeError(
                f"{module_name}.do_work 缺少参数，需要 (browser: UserBrowser) 或 (win: UserWindow)"
            )

        annot = params[0].annotation
        if annot is inspect.Parameter.empty:
            raise RuntimeError(
                f"{module_name}.do_work 的第一个参数缺少类型标注，"
                f"请添加 (browser: UserBrowser) 或 (win: UserWindow)"
            )

        # 解析 Union 类型（UserBrowser | UserWindow）
        from typing import get_origin, get_args
        origin = get_origin(annot)
        if origin is not None:
            # Union / UnionType → 取出所有成员类型名
            member_names = {_resolve_type_name(a) for a in get_args(annot)}
            if not member_names.issubset({'UserBrowser', 'UserWindow', 'Browser'}):
                invalid = member_names - {'UserBrowser', 'UserWindow', 'Browser'}
                raise RuntimeError(
                    f"{module_name}.do_work 的 Union 标注中包含了不支持的类型: {invalid}"
                )
            return  # Union 类型通过校验

        # 单个类型
        type_name = _resolve_type_name(annot)
        valid_types = ('UserBrowser', 'UserWindow')
        if type_name == 'Browser':  # 旧式标注，兼容
            return
        if type_name not in valid_types:
            raise RuntimeError(
                f"{module_name}.do_work 的参数类型标注必须为 UserBrowser 或 UserWindow，"
                f"当前为 {type_name}"
            )

    @staticmethod
    def _detect_prefer_window(script_module) -> bool | None:
        """通过 do_work 第一个参数的类型注解决定用窗口还是浏览器。
        返回 None 表示注解兼容两者（Union），由调用方决定。
        """
        sig = inspect.signature(script_module.do_work)
        params = list(sig.parameters.values())
        if not params:
            return False

        annot = params[0].annotation
        if annot is inspect.Parameter.empty:
            return False

        # Union 类型 → 兼容两者
        origin = get_origin(annot)
        if origin is not None:
            return None

        # 单个类型
        type_name = _resolve_type_name(annot)
        if type_name == 'UserWindow':
            return True
        return False

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
