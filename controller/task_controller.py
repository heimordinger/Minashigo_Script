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
        return annot.strip()
    # 尝试取 __name__（常规类）或 _name（GenericAlias）
    return getattr(annot, '__name__', getattr(annot, '_name', str(annot)))


def _annotation_member_names(annot) -> set[str] | None:
    """解析 do_work 参数注解为类型名集合。

    支持：
      - UserBrowser / UserWindow（含字符串推迟注解）
      - UserBrowser | UserWindow / Union[...]（含 `from __future__ import annotations`）
    返回 None 表示无法解析。
    """
    if annot is inspect.Parameter.empty:
        return None

    # 推迟注解：整段是字符串
    if isinstance(annot, str):
        text = annot.strip().replace(" ", "")
        if not text:
            return None
        if "|" in text:
            return {p.strip() for p in text.split("|") if p.strip()}
        return {text}

    origin = get_origin(annot)
    if origin is not None:
        return {_resolve_type_name(a) for a in get_args(annot)}

    return {_resolve_type_name(annot)}


_VALID_DO_WORK_TYPES = frozenset({"UserBrowser", "UserWindow", "Browser"})


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
        #: 本次协作脚本试运行的登记信息 ``(task_id, run_id, 是否由本控制器收尾)``。
        self._collab_trial: tuple[str, str, bool] | None = None

    # ── 协作脚本试运行：**由 TaskController 自动登记**（设计 §19.6）──
    #
    # 脚本侧不需要调用任何录制 API；面板会在 start_task 之前先把同一份上下文
    # 放进 ``account["_collab_trial"]``。面板那次登记成功后本控制器只做"认领"，
    # 面板没登记成（异常/换了启动路径）时本控制器补登记——两条路的**输入完全
    # 相同**（同一个 task_id / script_name / code），所以 script_hash 不会分叉，
    # 「复测证据 ↔ 主轮」的比对依然成立。

    def _collab_trial_context(self) -> dict | None:
        ctx = (self.account or {}).get("_collab_trial")
        if not isinstance(ctx, dict):
            return None
        if not str(ctx.get("task_id") or "").strip():
            return None
        if not str(ctx.get("code") or "").strip():
            return None
        return ctx

    def begin_collab_trial(self) -> str:
        """登记（或认领）本次试运行；返回 run_id（非协作来源返回空串）。"""
        self._collab_trial = None
        ctx = self._collab_trial_context()
        if ctx is None:
            return ""
        name = str((self.account or {}).get("name") or "")
        if not name:
            return ""
        try:
            from backend.script_generator.collaborator_v3.runtime import trial_runs

            existing = trial_runs.active_run(name)
            if existing:
                # 面板已登记：认领即可，**不再造第二条**
                self._collab_trial = (str(existing[0]), str(existing[1]), False)
                return str(existing[1])
            run_id = trial_runs.start_run(
                task_id=str(ctx.get("task_id") or ""),
                session_id=str(ctx.get("session_id") or ""),
                script_name=str(ctx.get("script_name") or ""),
                code=str(ctx.get("code") or ""),
                account=name,
                main_turn_id=str(ctx.get("main_turn_id") or ""),
            )
            if not run_id:
                return ""
            trial_runs.bind_active_run(name, str(ctx.get("task_id") or ""), run_id)
            self._collab_trial = (str(ctx.get("task_id") or ""), run_id, True)
            return run_id
        except Exception as exc:  # noqa: BLE001 - 登记失败不该拦住脚本执行
            print(f"[TaskController] 试运行登记失败: {exc}")
            self._collab_trial = None
            return ""

    def end_collab_trial(self, status: str) -> None:
        """收尾：只有**自己登记**的那次才由自己 finish/解绑。"""
        info = self._collab_trial
        self._collab_trial = None
        # 上下文用完了就清掉，避免下一次 start 复用上一次的 run 归属
        try:
            (self.account or {}).pop("_collab_trial", None)
        except Exception:  # noqa: BLE001
            pass
        if not info:
            return
        task_id, run_id, owned = info
        if not owned:
            return  # 面板登记的由面板收尾，别抢它的活
        try:
            from backend.script_generator.collaborator_v3.runtime import trial_runs

            # 兜底路径同样要把伪录制接进结构化遥测（设计 §19.4/§19.6）
            try:
                from backend.script_generator.collaborator_v3.runtime import pseudo_import

                pseudo_import.import_pseudo_record_for_run(
                    task_id,
                    run_id,
                    account_hint=str((self.account or {}).get("name") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[TaskController] 伪录制导入失败: {exc}")
            trial_runs.finish_run(
                task_id, run_id, status=str(status or "unknown")
            )
            trial_runs.clear_active_run(str((self.account or {}).get("name") or ""))
            # 同上：跨 run 不继承 match→click 的关联记忆
            trial_runs.clear_match_links(str((self.account or {}).get("name") or ""))
        except Exception as exc:  # noqa: BLE001
            print(f"[TaskController] 试运行收尾失败: {exc}")

    def start(self, *, task_name):
        if self._future is not None:
            if not self._future.done():
                print(
                    f"[TaskController] 忽略重复 start：{self.account['name']} "
                    f"任务仍在运行 ({self.task_name})"
                )
                return
            self._future = None
        self._stopped = False
        self._paused = False
        self.task_name = task_name
        self._future = self.controller.submit(self._run_async())

    async def _archive_stop_frame(self, target, *, reason: str, note: str = "") -> None:
        if target is None:
            return
        from backend.automation.stop_frame_archive import save_task_stop_frame

        # 带上最近日志（脚本自己打的坐标/状态日志都在里面），失败时的堆栈另附。
        # 但如果本次运行的完整日志已经由 RuntimeLogBackup 落盘了，就不要再贴一份 40 行
        # 的截断版 —— 那既是重复，又和完整日志对不上。
        log_saved = False
        if (getattr(self.controller, "runtime_log_path", None) is not None
                and callable(self.controller.runtime_log_path)):
            try:
                log_saved = self.controller.runtime_log_path() is not None
            except Exception:
                log_saved = False

        tail = ""
        if not log_saved:
            try:
                name = str((self.account or {}).get("name") or "")
                buf = getattr(self.controller, "_log_buffer", None)
                if name and isinstance(buf, dict):
                    dq = buf.get(name) or []
                    lines = []
                    for ev in list(dq)[-40:]:
                        msg = getattr(ev, "message", None) or str(ev)
                        lines.append(str(msg).strip())
                    tail = "\n".join(lines)
            except Exception:
                tail = ""
        full_note = "\n".join(x for x in (note.strip(), "--- 最近日志 ---", tail) if x)

        path = await save_task_stop_frame(
            target,
            account=self.account,
            reason=reason,
            task_name=self.task_name,
            note=full_note,
            log_saved=log_saved,
        )
        if path:
            self.controller.emit_log(
                account=self.account["name"],
                level=LogLevel.INFO,
                message=f"结束帧已归档: {path}",
                source="runner",
            )

    def _record_trial_exception(self, phase: str, message: str) -> None:
        """把执行期异常写进该次试运行的结构化事件（设计 §19.6 / 验收场景 11）。

        「脚本第一行就炸」也必须留下 run + 异常事件，否则 `RUNTIME_EXCEPTION`
        这个诊断码在生产里永远不可能出现（`record_exception` 以前只有测试在调）。
        留痕失败不影响主流程：脚本已经炸了，这里只是补记录。
        """
        info = self._collab_trial
        if not info:
            return
        task_id, run_id = str(info[0]), str(info[1])
        if not (task_id and run_id):
            return
        try:
            from backend.script_generator.collaborator_v3.runtime import trial_runs

            trial_runs.record_exception(
                task_id, run_id, phase=str(phase or "unknown"), message=str(message or "")
            )
        except Exception as exc:  # noqa: BLE001 - 留痕失败不该盖住原异常
            print(f"[TaskController] 试运行异常留痕失败: {exc}")

    async def _run_async(self):
        name = self.account["name"]
        run_target = None
        # ⚠️ 必须在 try **之前**初始化：`_load_script()` 抛异常时下面的 finally
        # 会引用 user_browser，晚初始化会把原始异常盖成 NameError（很难查）。
        user_browser = None
        trial_status = "unknown"
        #: 异常发生的阶段（import / target / run）——写进 runtime.exception 事件
        trial_phase = "import"

        try:
            # 协作脚本试运行登记（设计 §19.6）：脚本不需要主动调用任何录制 API。
            # 放在最前面，连"脚本加载就炸"的一次也留下记录。
            self.begin_collab_trial()
            # 先加载脚本，通过 do_work 的参数类型注解决定目标
            script = self._load_script()
            prefer_window = self._detect_prefer_window(script)
            trial_phase = "target"

            # ========== 按注解类型 + _target 倾向取实例 ==========

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
                if manual_target:
                    prefer_window = manual_target == "window"

                primary = (
                    self.controller._window_instances
                    if prefer_window
                    else self.controller._browser_instances
                )
                fallback = (
                    self.controller._browser_instances
                    if prefer_window
                    else self.controller._window_instances
                )
                user_browser = primary.get(name)
                if user_browser is None:
                    user_browser = fallback.get(name)
                    if user_browser is not None:
                        used = "窗口" if not prefer_window else "浏览器"
                        want = "窗口" if prefer_window else "浏览器"
                        self.controller.emit_log(
                            account=name,
                            message=(
                                f"脚本注解偏向{want}，但仅{used}就绪，已自动改用{used}"
                            ),
                            level=LogLevel.WARNING,
                            source="runner",
                        )
                if user_browser is None:
                    hint = "请先选择窗口" if prefer_window else "请先启动浏览器"
                    # 两者都没有时给更完整提示
                    if (
                        name not in self.controller._browser_instances
                        and name not in self.controller._window_instances
                    ):
                        hint = "请先启动浏览器或选择窗口"
                    self.controller.emit_log(
                        account=name,
                        message=f"脚本需要运行目标，{hint}",
                        level=LogLevel.ERROR,
                        source="runner",
                    )
                    raise RuntimeError(f"脚本需要运行目标，{hint}")

            run_target = user_browser

            # 日志确认实际使用的对象类型
            type_name = type(user_browser).__name__
            if hasattr(user_browser, '_obj'):
                type_name = f"MainLoopProxy({type(user_browser._obj).__name__})"
            print(f"[TaskController] 使用 {type_name} 执行脚本 {self.task_name}")

            # 重置卡住检测：新任务重新计时（避免跨任务「未知」累计）
            _target = getattr(user_browser, '_obj', user_browser)
            if hasattr(_target, '_stuck') and hasattr(_target._stuck, 'reset'):
                _target._stuck.reset()
            if hasattr(_target, 'note_progress'):
                try:
                    _target.note_progress()
                except Exception:
                    pass

            # 运行时日志备份：本次任务单独一份文件（省得排查时手工贴日志）
            try:
                self.controller.begin_log_run(label=self.task_name, account=name)
            except Exception:
                pass

            # 按需观察：脚本运行期间默认声明 floor Hz；结束即释放（无人则停截）
            from backend.automation.frame_observer import DEFAULT_SCRIPT_FPS
            _obs = user_browser
            try:
                # 任务开始 → 解除冷定标日志静音（任务结束时会在 finally 里静音）
                from backend.matcher.scale_calibrate import set_cold_calibration_muted

                set_cold_calibration_muted(False)
            except Exception:
                pass
            try:
                if hasattr(_obs, "request_fps"):
                    await _obs.request_fps(DEFAULT_SCRIPT_FPS, key="script")
            except Exception as e:
                print(f"[TaskController] request_fps 失败: {e}")

            # 从这里开始是"脚本真的在跑"：之后抛异常都算运行期异常
            trial_phase = "run"
            try:
                await script.do_work(user_browser)
            finally:
                try:
                    if hasattr(_obs, "release_fps"):
                        await _obs.release_fps("script")
                except Exception as e:
                    print(f"[TaskController] release_fps 失败: {e}")
            trial_phase = "finish"

            self.controller.on_task_finished(name)
            trial_status = "finished"

        except (TaskStopped, asyncio.CancelledError):
            trial_status = "stopped"
            # 停止也留一条结构化事件：否则"为什么停了"只能去翻文本日志
            self._record_trial_exception("stopped", "任务被停止")
            await self._archive_stop_frame(run_target, reason="stopped")
            self.controller.emit_log(
                account=name,
                level=LogLevel.INFO,
                message="任务已停止",
                source="runner",
            )
            self.controller.on_task_stopped(name)

        except Exception:
            trial_status = "error"
            # 验收场景 11：脚本第一行就炸时，run 与异常事件都要留下
            self._record_trial_exception(trial_phase, traceback.format_exc())
            await self._archive_stop_frame(
                run_target, reason="error", note=traceback.format_exc()
            )
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
            from core.state.events import StateEvent, StateDomain
            self.controller.emit_state(
                StateEvent(
                    account=name,
                    domain=StateDomain.RUNTIME,
                    key="running",
                    value=False,
                )
            )

        finally:
            self._future = None
            self._stopped = False
            self._paused = False
            # 试运行收尾：登记是自己做的才由自己 finish（面板登记的由面板收尾）
            self.end_collab_trial(trial_status)
            # 注销冷定标日志槽 + 静音：任务结束后 adaptive_match 有可能还留在
            # asyncio.to_thread 的线程里跑完（协程 cancel 掉了，线程取消不掉），
            # 不收拾干净的话脚本停了日志还在刷。
            for tgt in (run_target, user_browser):
                unreg = getattr(tgt, "unregister_cold_calibration_sink", None)
                if callable(unreg):
                    try:
                        unreg()
                    except Exception:
                        pass
            try:
                from backend.matcher.scale_calibrate import set_cold_calibration_muted

                set_cold_calibration_muted(True)
            except Exception:
                pass

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
        names = _annotation_member_names(annot)
        if names is None:
            raise RuntimeError(
                f"{module_name}.do_work 的第一个参数缺少类型标注，"
                f"请添加 (browser: UserBrowser) 或 (win: UserWindow)"
            )

        if not names.issubset(_VALID_DO_WORK_TYPES):
            invalid = names - _VALID_DO_WORK_TYPES
            raise RuntimeError(
                f"{module_name}.do_work 的参数类型标注必须为 UserBrowser 或 UserWindow"
                f"（可用 UserBrowser | UserWindow），不支持: {invalid or names}"
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

        names = _annotation_member_names(params[0].annotation)
        if not names:
            return False
        # 联合类型 → 兼容两者
        if len(names) > 1:
            return None
        return next(iter(names)) == "UserWindow"

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
