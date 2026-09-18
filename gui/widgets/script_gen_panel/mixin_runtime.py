from __future__ import annotations

import asyncio
import re
from pathlib import Path

from PySide6.QtGui import (
    QFont, QPixmap, QKeySequence, QShortcut, QTextCursor, QTextDocument, QDesktopServices,
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QCheckBox, QTextEdit,
    QLabel, QFileDialog, QSpinBox, QTabWidget, QSplitter,
    QMessageBox, QGroupBox, QProgressBar, QStyle, QFrame,
    QApplication, QDialog, QScrollArea, QDialogButtonBox, QProgressDialog,
    QInputDialog, QListWidget, QListWidgetItem, QSizePolicy,
)

from gui.widgets.GenTrajectory import GenTrajectory
from gui.widgets.TrialHud import TrialHud
from core.path import IMG_PATH, SCRIPTS_PATH

from gui.widgets.script_gen_panel.constants import (
    _OPTIMIZE_PAGE_SIZE, _TRIAL_DIR, _TRIAL_REL,
)
from gui.widgets.script_gen_panel.widgets import (
    FeedbackWritebackDialog, FullCodeDialog,
)
from gui.widgets.script_gen_panel.workers import OptimizeWorker, ReviseWorker
from core.path import SCRIPTS_PATH


class RuntimeMixin:
    def _update_trial_availability(self):
        has_code = bool((self._generated_code or "").strip())
        has_facade = self._facade is not None
        has_feedback = bool(self._feedback.toPlainText().strip())
        revise_busy = self._revise_worker is not None and self._revise_worker.isRunning()
        optimize_busy = self._optimize_worker is not None and self._optimize_worker.isRunning()
        gen_busy = revise_busy or optimize_busy
        if not has_facade:
            self._trial_hint.setText(
                "当前未连接主程序：可生成/保存，但无法试运行。"
                "请从主窗口状态栏打开「脚本生成」。"
            )
        elif self._trial_blocked:
            reason = self._trial_block_reason or "脚本检查未通过"
            self._trial_hint.setText(
                f"脚本检查未通过：{reason}。"
                "请在「查看完整代码」改路径/结构后保存，会自动重新检查。"
            )
        else:
            if not self._trial_hint.text().startswith("当前未连接"):
                self._trial_hint.setText("")
        has_ready_account = bool(self._selected_account())
        can_trial = (
            has_code
            and has_facade
            and has_ready_account
            and not self._trial_running
            and not self._trial_blocked
        )
        self._trial_btn.setEnabled(can_trial)
        self._stop_trial_btn.setEnabled(bool(has_facade and self._trial_running))
        self._account_combo.setEnabled(has_facade)
        self._refresh_acc_btn.setEnabled(has_facade)
        if hasattr(self, "_global_account_combo"):
            self._global_account_combo.setEnabled(has_facade)
        if hasattr(self, "_global_refresh_acc_btn"):
            self._global_refresh_acc_btn.setEnabled(has_facade)
        if has_code:
            self._revise_btn.setEnabled(not self._trial_running and not gen_busy)
            self._confirm_btn.setEnabled(not self._trial_running)
        else:
            self._revise_btn.setEnabled(False)
            self._confirm_btn.setEnabled(False)
        if hasattr(self, "_rerevise_btn"):
            # 修订失败后常无反馈、也未必 trial_blocked；只要还有代码就应能重修订
            can_rerevise = (
                has_code
                and not gen_busy
                and not self._trial_running
                and (
                    has_feedback
                    or self._trial_blocked
                    or bool(getattr(self, "_last_revise_error", ""))
                )
            )
            self._rerevise_btn.setEnabled(can_rerevise)
        self._update_optimize_availability()

    def _update_optimize_availability(self):
        if not hasattr(self, "_optimize_btn"):
            return
        has_code = bool((self._optimize_code or "").strip())
        gen_busy = (
            (self._revise_worker is not None and self._revise_worker.isRunning())
            or (self._optimize_worker is not None and self._optimize_worker.isRunning())
        )
        self._optimize_btn.setEnabled(has_code and not gen_busy)
        if hasattr(self, "_optimize_save_btn"):
            self._optimize_save_btn.setEnabled(
                has_code and self._optimize_script_path is not None
            )
        if hasattr(self, "_optimize_hint"):
            if not has_code:
                self._optimize_hint.setText("请从列表点选脚本（或「加载选中」）。")
            else:
                self._optimize_hint.setText("")

    def _account_ready(self, account: dict) -> bool:
        if not self._facade or not account:
            return False
        name = account.get("name") or ""
        if not name:
            return False
        ctrl = self._facade.controller
        return (
            name in getattr(ctrl, "_browser_instances", {})
            or name in getattr(ctrl, "_window_instances", {})
        )

    def _fill_account_combo(self, combo: QComboBox, current: dict | None) -> None:
        combo.clear()
        if self._facade is None:
            combo.addItem("（无主程序）", None)
            return
        accounts = list(self._facade.list_accounts() or [])
        ready_accounts = [acc for acc in accounts if self._account_ready(acc)]
        if not ready_accounts:
            combo.addItem("（暂无已启动账号）", None)
            return
        for acc in ready_accounts:
            name = acc.get("name") or ""
            ctrl = self._facade.controller
            kinds = []
            if name in getattr(ctrl, "_browser_instances", {}):
                kinds.append("浏览器")
            if name in getattr(ctrl, "_window_instances", {}):
                kinds.append("窗口")
            suffix = " + ".join(kinds) if kinds else "已启动"
            combo.addItem(f"{name}  · {suffix}", acc)
        if current:
            for i in range(combo.count()):
                data = combo.itemData(i)
                if isinstance(data, dict) and data.get("name") == current.get("name"):
                    combo.setCurrentIndex(i)
                    break

    def _refresh_accounts(self):
        current = self._selected_account()
        if hasattr(self, "_global_account_combo"):
            self._global_account_combo.blockSignals(True)
            self._fill_account_combo(self._global_account_combo, current)
            self._global_account_combo.blockSignals(False)
        if hasattr(self, "_account_combo"):
            self._account_combo.blockSignals(True)
            self._fill_account_combo(self._account_combo, current)
            self._account_combo.blockSignals(False)
        if self._facade is None:
            self._update_trial_availability()
            return
        accounts = list(self._facade.list_accounts() or [])
        ready_accounts = [acc for acc in accounts if self._account_ready(acc)]
        if hasattr(self, "_trial_hint"):
            if not ready_accounts and accounts:
                self._trial_hint.setText(
                    "列表里只显示已启动浏览器或已绑定窗口的账号。"
                    "请先在「开始」页启动后再点顶栏「刷新」。"
                )
                self._trial_btn.setEnabled(False)
            elif self._trial_hint.text().startswith("列表里只显示"):
                self._trial_hint.setText("")
        self._update_trial_availability()

    def _on_global_account_changed(self, *_args):
        """顶栏账号变更时，同步试运行下拉。"""
        current = None
        if hasattr(self, "_global_account_combo"):
            data = self._global_account_combo.currentData()
            if isinstance(data, dict) and data.get("name"):
                current = data
        if hasattr(self, "_account_combo"):
            self._account_combo.blockSignals(True)
            self._fill_account_combo(self._account_combo, current)
            self._account_combo.blockSignals(False)
        self._update_trial_availability()

    def _on_trial_account_changed(self, *_args):
        """试运行页改账号时，回写顶栏全局绑定。"""
        data = self._account_combo.currentData() if hasattr(self, "_account_combo") else None
        current = data if isinstance(data, dict) and data.get("name") else None
        if hasattr(self, "_global_account_combo"):
            self._global_account_combo.blockSignals(True)
            self._fill_account_combo(self._global_account_combo, current)
            self._global_account_combo.blockSignals(False)
        self._update_trial_availability()

    def _selected_account(self) -> dict | None:
        if hasattr(self, "_global_account_combo"):
            data = self._global_account_combo.currentData()
            if isinstance(data, dict) and data.get("name"):
                return data
        if hasattr(self, "_account_combo"):
            data = self._account_combo.currentData()
            if isinstance(data, dict) and data.get("name"):
                return data
        return None

    def _trial_script_name_hint(self) -> str:
        if self._optimize_script_path is not None:
            return self._optimize_script_path.stem
        name = (self._script_name.text().strip() if hasattr(self, "_script_name") else "") or ""
        if name.lower().endswith(".py"):
            name = name[:-3]
        return name or "trial"

    def _write_trial_file(self) -> Path:
        raw = (self._generated_code or "").strip()
        if not raw:
            raise RuntimeError("没有可试运行的代码")
        from backend.script_generator.pseudo_codegen import inject_pseudo_record_for_trial

        injected = inject_pseudo_record_for_trial(
            raw, script_name=self._trial_script_name_hint()
        )
        mode = "injected"
        chosen = injected
        if not self._code_compiles(injected):
            # 注入版语法损坏：回退原文，宁可没有伪录制也不能让试运行直接 SyntaxError
            mode = "raw-fallback"
            chosen = raw
            self._append_trial_log("[试运行] ⚠ 伪录制注入版编译失败，已回退未注入版本")
        if not self._code_compiles(chosen):
            raise RuntimeError(
                "试运行代码存在语法错误（原文编译失败）："
                "请回生成页查看完整代码或点「重修订」后再试运行。"
            )
        _TRIAL_DIR.mkdir(parents=True, exist_ok=True)
        init_py = _TRIAL_DIR / "__init__.py"
        if not init_py.exists():
            init_py.write_text("# trial package\n", encoding="utf-8")
        path = SCRIPTS_PATH / _TRIAL_REL
        # 原子写：避免任务在写盘中途 import 到半截文件
        import os as _os

        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(chosen, encoding="utf-8")
        _os.replace(tmp, path)
        # 双归档：raw / injected 都留，事后可复现注入问题
        if getattr(self, "_archive", None) is not None and self._archive.session_dir:
            try:
                self._archive.write_text("code_at_trial_raw.py", raw)
                self._archive.write_text("code_at_trial_injected.py", chosen)
            except Exception as e:
                print(f"[ScriptGenerator] 归档试运行文件失败: {e}")
        self._append_trial_log(f"[试运行] 写入模式: {mode}")
        return path

    @staticmethod
    def _code_compiles(code: str) -> bool:
        try:
            compile(code or "", "<trial>", "exec")
            return True
        except SyntaxError:
            return False

    def _run_local_script_validate(self, *, log: bool = False) -> list[str]:
        """本地脚本检查：结构 + 素材（写入试运行文件后自动执行）。"""
        code = (self._generated_code or "").strip()
        if not code:
            self._trial_blocked = True
            self._trial_block_reason = "无代码"
            self._update_trial_availability()
            return ["无代码"]
        from backend.script_generator.agent import (
            patch_missing_handler_return_keys,
            validate_script_local,
        )

        # 生成侧偶发漏补 return 目标键：试运行前再补一次，避免「生成成功却不能试跑」
        patched, patch_notes = patch_missing_handler_return_keys(code)
        if patch_notes and patched.strip() and patched != code:
            self._generated_code = patched if patched.endswith("\n") else patched + "\n"
            code = self._generated_code.strip()
            try:
                self._write_trial_file()
            except Exception:
                pass
            if log:
                preview = "；".join(patch_notes[:4])
                more = f" 等{len(patch_notes)}处" if len(patch_notes) > 4 else ""
                self._append_trial_log(f"[脚本检查] 已自动补状态路由：{preview}{more}")

        # 协作脚本走协作自己的闸：不带经典页的图片目录 / 自由模式开关，
        # 否则同一份脚本在协作里通过、到试跑页却被历史设置卡住。
        collab_code = self._trial_script_from_collab()
        errs = validate_script_local(
            code,
            explanation="" if collab_code else self._explanation.toPlainText(),
            source_dir="" if collab_code else str(self._source_dir or ""),
            free_mode=True if collab_code else bool(self._free_mode_cb.isChecked()),
            require_fsm=False,
        )
        self._trial_blocked = bool(errs)
        self._trial_block_reason = errs[0] if errs else ""
        self._update_trial_availability()
        if log:
            if errs:
                lines = "\n".join(f"- {e}" for e in errs[:8])
                if len(errs) > 8:
                    lines += f"\n…共 {len(errs)} 项"
                self._append_trial_log(f"[脚本检查] 未通过\n{lines}")
            else:
                self._append_trial_log("[脚本检查] 通过（结构 + 素材）")
        return errs

    def _sync_trial_code(self):
        """写入试运行临时文件并自动做本地脚本检查。"""
        try:
            if (self._generated_code or "").strip():
                self._write_trial_file()
                self._run_local_script_validate(log=True)
        except Exception as e:
            print(f"[ScriptGenerator] 写入试运行临时文件失败: {e}")

    def _selected_opt_account(self) -> dict | None:
        """优化页不再选账号；仅复用试运行页已选账号作伪录制匹配提示。"""
        return self._selected_account()

    def _on_tab_changed(self, index: int):
        if index == self.TAB_TRIAL and (self._generated_code or "").strip():
            # 回到试运行页时重检：避免「生成已修好但仍沿用上次拦截」
            prev_blocked = bool(self._trial_blocked)
            errs = self._run_local_script_validate(log=False)
            if prev_blocked and not errs:
                self._append_trial_log("[脚本检查] 重新检查已通过 · 可试运行")
                if hasattr(self, "_feedback"):
                    fb = self._feedback.toPlainText().strip()
                    if fb.startswith("修复本地校验错误"):
                        self._feedback.clear()
                        self._set_feedback_stale(False)
            elif errs and not prev_blocked:
                self._append_trial_log(
                    f"[脚本检查] 未通过：{errs[0]}"
                )
        if index == self.TAB_OPTIMIZE:
            self._refresh_optimize_script_list()

    def _init_optimize_folder_combo(self):
        if not hasattr(self, "_optimize_folder_combo"):
            return
        self._optimize_folder_combo.blockSignals(True)
        self._optimize_folder_combo.clear()
        self._optimize_folder_combo.addItem("全部", "")
        if SCRIPTS_PATH.is_dir():
            for p in sorted(SCRIPTS_PATH.iterdir(), key=lambda x: x.name.lower()):
                if p.is_dir() and p.name not in ("__pycache__",):
                    self._optimize_folder_combo.addItem(p.name, p.name)
        self._optimize_folder_combo.blockSignals(False)

    def _collect_optimize_py_files(self) -> list[Path]:
        root = SCRIPTS_PATH
        if not root.is_dir():
            return []
        folder = (self._optimize_folder or "").strip()
        search = (
            self._optimize_search.text().strip().lower()
            if hasattr(self, "_optimize_search")
            else ""
        )
        base = root / folder if folder else root
        if not base.is_dir():
            return []
        skip_parts = {"__pycache__"}
        out: list[Path] = []
        for p in sorted(base.rglob("*.py"), key=lambda x: str(x).lower()):
            if any(part in skip_parts for part in p.parts):
                continue
            if p.name == "__init__.py":
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if search and search not in rel.lower():
                continue
            out.append(p)
        return out

    def _refresh_optimize_script_list(self):
        if not hasattr(self, "_optimize_script_list"):
            return
        self._optimize_script_files = self._collect_optimize_py_files()
        total = len(self._optimize_script_files)
        pages = max(1, (total + _OPTIMIZE_PAGE_SIZE - 1) // _OPTIMIZE_PAGE_SIZE)
        if self._optimize_page >= pages:
            self._optimize_page = max(0, pages - 1)
        start = self._optimize_page * _OPTIMIZE_PAGE_SIZE
        chunk = self._optimize_script_files[start : start + _OPTIMIZE_PAGE_SIZE]

        self._optimize_script_list.clear()
        root = SCRIPTS_PATH
        for p in chunk:
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = p.name
            item = QListWidgetItem(rel)
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            self._optimize_script_list.addItem(item)

        self._optimize_page_label.setText(
            f"第 {self._optimize_page + 1}/{pages} 页 · 共 {total} 个脚本"
        )
        self._optimize_prev_btn.setEnabled(self._optimize_page > 0)
        self._optimize_next_btn.setEnabled(self._optimize_page < pages - 1)

    def _optimize_change_page(self, delta: int):
        self._optimize_page = max(0, self._optimize_page + int(delta))
        self._refresh_optimize_script_list()

    def _on_optimize_folder_changed(self):
        data = self._optimize_folder_combo.currentData()
        self._optimize_folder = str(data or "")
        self._optimize_page = 0
        self._refresh_optimize_script_list()

    def _append_optimize_log(self, line: str):
        if not line:
            return
        self._optimize_log_lines.append(line)
        if hasattr(self, "_optimize_log"):
            self._optimize_log.append(line)
            self._optimize_log.moveCursor(QTextCursor.MoveOperation.End)

    def _load_optimize_script(self, path: Path) -> bool:
        path = Path(path)
        if not path.is_file():
            QMessageBox.warning(self, "文件不存在", str(path))
            return False
        # 点选同一文件不重复读盘
        if (
            self._optimize_script_path is not None
            and path.resolve() == self._optimize_script_path.resolve()
            and (self._optimize_code or "").strip()
        ):
            return True
        try:
            code = path.read_text(encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))
            return False
        self._optimize_code = code
        self._optimize_script_path = path
        self._generated_code = code
        self._note_script_origin(code)
        self._stream_buf = code
        try:
            rel = path.relative_to(SCRIPTS_PATH).as_posix()
        except ValueError:
            rel = path.name
        self._optimize_path_label.setText(rel)
        self._script_name.setText(path.name)
        self._append_optimize_log(f"[加载] {rel}（{len(code)} 字符）")
        # 加载只读入内存；写入试跑临时文件 + 本地校验延后到「去试运行」
        self._trial_blocked = False
        self._trial_block_reason = ""
        self._update_optimize_availability()
        self._update_trial_availability()
        return True

    def _on_optimize_script_activated(self, item: QListWidgetItem):
        raw = item.data(Qt.ItemDataRole.UserRole)
        if raw:
            self._load_optimize_script(Path(str(raw)))

    def _on_optimize_load_selected(self):
        item = self._optimize_script_list.currentItem()
        if not item:
            QMessageBox.information(self, "未选择", "请先在列表中选中脚本。")
            return
        raw = item.data(Qt.ItemDataRole.UserRole)
        if raw:
            self._load_optimize_script(Path(str(raw)))

    def _on_optimize_pick_file(self):
        start = str(SCRIPTS_PATH if SCRIPTS_PATH.is_dir() else Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Python 脚本", start, "Python (*.py);;All (*.*)",
        )
        if path:
            self._load_optimize_script(Path(path))

    def _on_optimize_save(self):
        code = (self._optimize_code or "").strip()
        if not code:
            QMessageBox.warning(self, "无代码", "没有可保存的内容。")
            return
        path = self._optimize_script_path
        if path is None:
            path_str, _ = QFileDialog.getSaveFileName(
                self,
                "保存脚本",
                str(SCRIPTS_PATH / "my_script.py"),
                "Python (*.py)",
            )
            if not path_str:
                return
            path = Path(path_str)
        try:
            path.write_text(code, encoding="utf-8")
            self._optimize_script_path = path
            self._append_optimize_log(f"[保存] {path}")
            QMessageBox.information(self, "已保存", str(path))
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _view_optimize_code(self):
        code = (self._optimize_code or self._generated_code or "").strip()
        if not code:
            QMessageBox.information(self, "无代码", "请先加载脚本。")
            return
        dlg = FullCodeDialog(self, code=code, title="优化中的脚本")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.applied_code is not None:
            self._optimize_code = dlg.applied_code
            self._generated_code = dlg.applied_code
            self._stream_buf = dlg.applied_code
            self._trial_blocked = False
            self._trial_block_reason = ""
            self._update_optimize_availability()
            self._update_trial_availability()

    def _on_optimize_go_trial(self):
        if not (self._optimize_code or "").strip():
            QMessageBox.information(self, "无代码", "请先加载脚本。")
            return
        self._generated_code = self._optimize_code
        self._sync_trial_code()
        self._tabs.setCurrentIndex(self.TAB_TRIAL)

    def _on_trial_run(self):
        if self._facade is None:
            QMessageBox.warning(self, "无法试运行", "请从主窗口打开脚本生成器后再试运行。")
            return
        if not (self._generated_code or "").strip():
            QMessageBox.warning(self, "无代码", "请先生成脚本。")
            return
        account = self._selected_account()
        if not account:
            QMessageBox.warning(self, "未选账号", "请先选择账号。")
            return
        name = account["name"]
        ctrl = self._facade.controller
        ready = (
            name in getattr(ctrl, "_browser_instances", {})
            or name in getattr(ctrl, "_window_instances", {})
        )
        if not ready:
            QMessageBox.warning(
                self,
                "账号未就绪",
                f"账号「{name}」尚未启动浏览器或绑定窗口。\n"
                "请先在「开始」页启动后再试运行。",
            )
            self._refresh_accounts()
            return
        # 试跑：按实际就绪目标写入 _target，避免生成脚本 UserBrowser 注解锁死浏览器
        has_br = name in getattr(ctrl, "_browser_instances", {})
        has_win = name in getattr(ctrl, "_window_instances", {})
        if has_win and not has_br:
            account["_target"] = "window"
        elif has_br and not has_win:
            account["_target"] = "browser"
        elif has_win and account.get("_target") not in ("browser", "window"):
            account["_target"] = "window"
        task_ctrl = getattr(ctrl, "_task_ctrls", {}).get(name)
        if task_ctrl is not None and getattr(task_ctrl, "_future", None):
            QMessageBox.warning(
                self,
                "账号忙碌",
                f"账号「{name}」已有任务在跑。\n请先停止该账号当前任务，再试运行。",
            )
            return
        try:
            path = self._write_trial_file()
        except Exception as e:
            QMessageBox.critical(self, "写入临时文件失败", str(e))
            return

        self._trial_log_lines.clear()
        self._trial_log.clear()
        self._trial_account_name = name
        self._trial_running = True
        self._last_trial_frame = None
        self._stop_frame_path = ""
        self._tabs.setCurrentIndex(self.TAB_TRIAL)
        self._set_trial_chip("write", "done")
        self._set_trial_chip("run", "active")
        self._set_trial_chip("frame", "idle")
        self._set_trial_chip("feas", "idle")
        if getattr(self, "_trial_hud", None) is not None:
            self._trial_hud.start_run(name)
        if hasattr(self, "_trial_frame_lbl"):
            self._trial_frame_lbl.setPixmap(QPixmap())
            self._trial_frame_lbl.setText("运行中…\n结束或停止后显示停帧")
        self._set_agent_status_text(f"试运行 · {name}")
        if hasattr(self, "_trajectory") and self._trajectory._steps:
            self._trajectory.begin_phase(f"试运行 · {name}", kind="Trial")
        # 再试跑时反馈框若仍是上次内容 → 标成「陈旧」样式，提醒改写/清空
        if self._feedback.toPlainText().strip():
            self._set_feedback_stale(True)
        self._append_trial_log(f"[试运行] 临时文件: {path}")
        self._append_trial_log(f"[试运行] 账号: {name} · 模块: {_TRIAL_REL}")
        self._append_trial_log(
            "[试运行] 已自动开启伪录制（确认完成并保存时会从正式脚本去掉）"
        )
        self._set_trial_pseudo_env(True)
        self._update_trial_availability()
        try:
            self._facade.start_task(account, _TRIAL_REL)
        except Exception as e:
            self._trial_running = False
            self._set_trial_pseudo_env(False)
            hud = getattr(self, "_trial_hud", None)
            if hud is not None:
                hud.set_terminal("error")
            self._update_trial_availability()
            QMessageBox.critical(self, "试运行失败", str(e))

    def _on_stop_trial(self):
        if self._facade is None or not self._trial_account_name:
            return
        # 先缓存停帧，再 stop（避免任务结束后帧被清掉）
        self._cache_stop_frame_now(also_capture=True)
        account = self._selected_account()
        if account is None or account.get("name") != self._trial_account_name:
            account = {"name": self._trial_account_name}
        try:
            self._facade.stop_task(account)
        except Exception as e:
            QMessageBox.warning(self, "停止失败", str(e))
        self._append_trial_log("[试运行] 已请求停止")

    def _append_trial_log(self, line: str):
        self._trial_log_lines.append(line)
        self._trial_log.append(line)

    def _on_trial_log(self, account: str, event):
        if not self._trial_running:
            return
        if account != self._trial_account_name:
            return
        msg = getattr(event, "message", None) or str(event)
        level = getattr(event, "level", "")
        prefix = f"[{level}] " if level else ""
        self._append_trial_log(f"{prefix}{msg}")
        hud = getattr(self, "_trial_hud", None)
        if hud is not None:
            hud.on_log_line(f"{prefix}{msg}")

    def _on_trial_state(self, event):
        if not self._trial_running:
            return
        # UnifiedEvent: type == "task", payload 含 browser / status
        try:
            if getattr(event, "type", None) != "task":
                return
            snap = event.payload
            browser = getattr(snap, "browser", None) or getattr(snap, "account", None)
            if browser != self._trial_account_name:
                return
            status_raw = getattr(snap, "status", "")
            status = getattr(status_raw, "value", status_raw)
            status = str(status or "").lower()
            message = getattr(snap, "message", "") or ""
            if message:
                self._append_trial_log(f"[状态] {message}")
                hud = getattr(self, "_trial_hud", None)
                if hud is not None:
                    hud.on_log_line(f"[状态] {message}")
            if status in ("finished", "stopped", "error", "idle"):
                self._trial_running = False
                self._set_trial_pseudo_env(False)
                hud = getattr(self, "_trial_hud", None)
                if hud is not None:
                    hud.set_terminal(status)
                # 自然结束/停止结束时再确保有停帧
                if not self._stop_frame_path:
                    self._cache_stop_frame_now(also_capture=True)
                else:
                    self._try_cache_trial_frame()
                    self._persist_stop_frame()
                self._update_trial_availability()
                self._append_trial_log(f"[试运行] 结束 ({status})")
                self._set_trial_chip("run", "done")
                self._update_trial_frame_preview()
                self._archive_trial_end(status)
                self._set_trial_chip("feas", "active")
                self._report_trial_feasibility(status)
                self._set_trial_chip("feas", "done")
                self._set_agent_status_text(f"试运行结束 · {status}")
                if hasattr(self, "_trajectory") and self._trajectory._steps:
                    self._trajectory.succeed_run(f"试运行结束 ({status})")
        except Exception:
            pass

    def _note_script_origin(self, code: str = "", *, collab: bool | None = None) -> None:
        """记下当前试运行脚本从哪来。协作推送强制 collab；经典生成/加载按正文标记刷新。"""
        if collab is True:
            self._script_origin = "collab"
            return
        if collab is False:
            self._script_origin = ""
            return
        self._script_origin = (
            "collab" if "minashigo-collab-linear" in (code or "") else ""
        )

    def _trial_script_from_collab(self) -> bool:
        if getattr(self, "_script_origin", "") == "collab":
            return True
        code = self._generated_code or ""
        if "minashigo-collab-linear" in code:
            return True
        if code.strip():
            return False
        try:
            path = SCRIPTS_PATH / _TRIAL_REL
            if not path.is_file():
                return False
            head = path.read_text(encoding="utf-8", errors="replace")[:8000]
            return "minashigo-collab-linear" in head
        except Exception:
            return False

    def _revise_via_collab_chat(self) -> None:
        """协作来源的试运行反馈 = 在协作聊天里发一条用户消息。"""
        feedback = ""
        if hasattr(self, "_feedback"):
            feedback = self._feedback.toPlainText().strip()
        if not feedback:
            QMessageBox.warning(
                self,
                "缺少反馈",
                "请先填写试运行问题。这条会直接发到协作对话，不再走经典修订。",
            )
            return
        collab = getattr(self, "_collab", None)
        send = getattr(collab, "_on_user_send", None)
        if collab is None or not callable(send):
            QMessageBox.warning(self, "无法发送", "协作页还没就绪。")
            return
        worker = getattr(collab, "_llm_worker", None)
        if worker is not None and worker.isRunning():
            QMessageBox.information(
                self,
                "请稍候",
                "协作里上一轮还在思考，稍后再发这条反馈。",
            )
            return
        self._tabs.setCurrentIndex(self.TAB_COLLAB)
        send(feedback)
        self._feedback.clear()
        self._set_feedback_stale(False)
        self._append_trial_log("[试运行] 反馈已发到协作对话")

    def _on_revise(self):
        if self._trial_script_from_collab():
            self._revise_via_collab_chat()
            return
        self._launch_revise(run_label="根据反馈修订")

    def _on_rerevise(self):
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._launch_revise(run_label="重修订")

    def _on_optimize(self):
        code = (self._optimize_code or self._generated_code or "").strip()
        if not code:
            QMessageBox.warning(self, "无代码", "请先从脚本库加载 .py 或生成脚本。")
            return
        api_key = self._api_key.text().strip()
        model = self._model.currentText().strip()
        if not api_key or not model:
            QMessageBox.warning(self, "缺少配置", "请填写 API Key 和模型。")
            return

        self._generated_code = code
        user_feedback = ""
        if hasattr(self, "_optimize_feedback"):
            user_feedback = self._optimize_feedback.toPlainText().strip()

        from backend.script_generator.pseudo_analyze import find_latest_pseudo_record

        script_hint = (
            self._optimize_script_path.stem
            if self._optimize_script_path
            else (self._script_name.text().strip() or "_gen_trial")
        )
        # 账号仅用于匹配伪录制目录名，可为空
        opt_acc = self._selected_opt_account()
        account = (opt_acc or {}).get("name") or self._trial_account_name or ""
        rec = find_latest_pseudo_record(script_hint=script_hint, account_hint=account)
        if rec is None:
            rec = find_latest_pseudo_record(script_hint=script_hint)

        force = bool(user_feedback)
        if rec is None and not user_feedback:
            ok = QMessageBox.question(
                self,
                "未找到伪录制",
                "未找到匹配的伪录制，且未填写优化方向。\n\n"
                "仍仅按代码做保守优化？\n"
                "（也可取消后先写优化方向再提交。）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ok != QMessageBox.StandardButton.Yes:
                return
        elif rec is None and user_feedback:
            self._append_optimize_log("[优化] 无伪录制，将按填写的优化方向修订")

        try:
            self._persist_explanation(self._explanation.toPlainText())
        except Exception as e:
            print(f"[ScriptGenerator] 优化前保存介绍失败: {e}")

        if self._archive.session_dir is None:
            self._archive.begin("optimize")

        params = {
            "provider": self._current_provider(),
            "api_key": api_key,
            "model": model,
            "api_endpoint": self._endpoint.text().strip() or None,
            "explanation_text": self._explanation.toPlainText(),
            "current_code": code,
            "source_dir": str(self._source_dir or ""),
            "script_name_hint": script_hint,
            "account_hint": account,
            "user_feedback": user_feedback,
            "trial_log": "\n".join(
                (self._optimize_log_lines + self._trial_log_lines)[-200:]
            ),
            "pseudo_record_dir": str(rec) if rec else "",
            "force": force,
            "max_tokens": self._max_tokens.value() if hasattr(self, "_max_tokens") else None,
        }

        self._generate_btn.setEnabled(False)
        self._revise_btn.setEnabled(False)
        if hasattr(self, "_optimize_btn"):
            self._optimize_btn.setEnabled(False)
        if hasattr(self, "_rerevise_btn"):
            self._rerevise_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._set_agent_busy("优化", "分析伪录制 / 边界评估…", indeterminate=True)
        self._keep_prev_live_code(phase="优化")
        self._token_label.setText("")
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._trajectory.begin_run("脚本优化", fresh=False)
        if self._archive.session_dir:
            self._archive.append_event(
                "optimize_begin",
                script_hint=script_hint,
                has_feedback=bool(user_feedback),
            )
        if user_feedback:
            self._append_optimize_log("[优化方向]\n" + user_feedback[:500])

        # 保存优化前试跑日志，供再试跑后做可靠性对比
        self._pre_optimize_trial_log = "\n".join(self._trial_log_lines)
        self._awaiting_reliability_retrial = False
        self._pre_optimize_record_dir = ""
        try:
            from backend.script_generator.pseudo_analyze import find_latest_pseudo_record

            hint = ""
            if self._optimize_script_path:
                hint = self._optimize_script_path.stem
            rec = find_latest_pseudo_record(script_hint=hint)
            if rec:
                self._pre_optimize_record_dir = str(rec)
        except Exception:
            pass

        self._optimize_worker = OptimizeWorker(params)
        self._optimize_worker.finished.connect(self._on_optimize_success)
        self._optimize_worker.partial.connect(self._on_partial)
        self._optimize_worker.status.connect(self._on_status)
        self._optimize_worker.artifact.connect(self._on_artifact)
        self._optimize_worker.token_info.connect(self._on_token_info)
        self._optimize_worker.error.connect(self._on_optimize_error)
        self._optimize_worker.start()

    def _on_optimize_success(self, code: str, summary: str = "", meta=None):
        meta = meta if isinstance(meta, dict) else {}
        self._optimize_code = code
        self._generated_code = code
        self._note_script_origin(code or "")
        self._stream_buf = code or ""
        self._set_live_code(code or "")
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        skipped = bool((meta or {}).get("skipped_llm"))
        n_chars = len(code or "")
        idle = "✓ 优化完成 · 建议再试运行" if not skipped else "已接近优化边界"
        self._set_agent_idle(idle, success=not skipped)
        if hasattr(self, "_live_code_hint"):
            self._live_code_hint.setText(
                f"{'已优化' if not skipped else '未改写'} · {n_chars:,} 字符"
            )
        self._set_ok_banner(
            ("✓ 优化完成　　" if not skipped else "边界评估完成　　")
            + f"{n_chars:,} 字符　　建议再试运行对比"
        )
        # 不在此处同步试跑校验；用户点「去试运行」时再写入临时文件
        self._update_trial_availability()
        self._update_optimize_availability()

        summary = (summary or "").strip() or "（无摘要）"
        ceiling = meta.get("ceiling") or {}
        baseline = meta.get("baseline") or {}
        if baseline:
            self._append_optimize_log(
                "[优化 baseline] "
                f"total={baseline.get('total_s')}s "
                f"black={baseline.get('black_s')}s "
                f"effective={baseline.get('effective_s')}s"
            )
        if meta.get("pseudo_record_dir"):
            self._append_optimize_log(f"[优化] 伪录制: {meta['pseudo_record_dir']}")
        self._append_optimize_log("[优化摘要]\n" + summary)
        self._trajectory.succeed_run("优化完成" if not skipped else "边界评估完成")
        self._flash_taskbar("脚本优化完成" if not skipped else "已接近优化边界")

        val_errors = meta.get("validation_errors") or []
        near = bool(ceiling.get("near_ceiling"))
        if not skipped:
            self._awaiting_reliability_retrial = True
            self._append_optimize_log(
                "[可靠性] 请再试跑一次；结束后将自动对比优化前/后轨迹信号。"
            )

        if skipped:
            title = "已接近脚本优化边界"
            tip = summary
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Information)
        elif val_errors:
            title = "优化完成 — 脚本检查未通过"
            tip = (
                "代码已更新，但本地校验有警告，请核对后再试跑。\n\n"
                f"{summary}\n\n"
                + "\n".join(f"- {e}" for e in val_errors[:6])
                + "\n\n建议再试跑一次，核对可靠性是否下降。"
            )
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Warning)
        elif near:
            _show_scroll_message(
                self,
                "优化完成 — 可能已接近边界",
                "代码已更新。\n\n"
                f"{summary}\n\n"
                "请再试跑一次：对比 effective，并核对可靠性是否下降；"
                "若 effective 变化 <5%，建议停止反复优化。",
                icon=QMessageBox.Icon.Information,
            )
        else:
            _show_scroll_message(
                self,
                "优化完成",
                "代码已更新。\n\n"
                f"{summary}\n\n"
                "请点「去试运行」再跑一轮：会自动对比优化前/后可靠性；"
                "也可先「保存到文件」。",
            )

        if self._archive.session_dir:
            try:
                self._archive.write_text("code_post_optimize.py", code)
                self._archive.write_text("optimize_summary.txt", summary)
                if hasattr(self, "_optimize_feedback"):
                    fb = self._optimize_feedback.toPlainText().strip()
                    if fb:
                        self._archive.write_text("optimize_feedback.txt", fb)
                if meta.get("pseudo_record_dir"):
                    self._archive.merge_meta({"pseudo_record_dir": meta["pseudo_record_dir"]})
                self._archive.append_event("optimize_done")
            except Exception as e:
                print(f"[ScriptGenerator] 归档优化失败: {e}")

    def _on_optimize_error(self, msg: str):
        translated = self._translate_error(msg)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        reason = (translated.split("\n", 1)[0] or "优化失败").strip()
        self._set_agent_idle(f"优化失败 · {reason}")
        self._update_trial_availability()
        self._trajectory.update_step(
            "optimize",
            "Error",
            reason,
            status="error",
            body=translated,
        )
        self._trajectory.fail_run(reason)
        QMessageBox.critical(self, "优化失败", translated)

    def _launch_revise(self, *, run_label: str = "根据反馈修订"):
        api_key = self._api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key")
            return
        feedback = self._feedback.toPlainText().strip()
        if not feedback and self._trial_blocked:
            reason = self._trial_block_reason or "硬校验未通过"
            feedback = f"修复本地校验错误：{reason}"
        if not feedback and getattr(self, "_last_revise_error", ""):
            feedback = (
                "上次修订失败，请基于原代码继续修复。\n"
                f"失败原因：{self._last_revise_error}"
            )
        if not feedback:
            QMessageBox.warning(
                self,
                "缺少反馈",
                "请先在试运行页填写反馈，或保留上次反馈后再修订。\n\n"
                "若刚因硬校验失败，可直接点「重修订」（会自动带上错误说明）。",
            )
            return
        if not (self._generated_code or "").strip():
            # 界面被清空时尽量从试运行临时文件恢复
            recovered = self._try_recover_code_from_trial()
            if not recovered:
                QMessageBox.warning(self, "无代码", "请先生成脚本。")
                return
            feedback = feedback  # keep

        try:
            self._persist_explanation()
        except Exception as e:
            print(f"[ScriptGenerator] 修订前保存介绍失败: {e}")

        if self._archive.session_dir is None:
            self._archive.begin("revise")
        # 修订前再确保停帧落盘（截图异步晚到时）
        if self._last_trial_frame is not None:
            self._persist_stop_frame()
        self._archive_revise_start(feedback)

        params = {
            "provider": self._current_provider(),
            "api_key": api_key,
            "model": self._model.currentText().strip(),
            "api_endpoint": self._endpoint.text().strip() or None,
            "explanation_text": self._explanation.toPlainText().strip(),
            "current_code": self._generated_code,
            "user_feedback": feedback,
            "source_dir": str(self._source_dir) if self._source_dir else "",
            "trial_log": "\n".join(self._trial_log_lines[-200:]),
            "max_tokens": int(self._max_tokens.value()),
            "free_mode": bool(self._free_mode_cb.isChecked()),
            "stop_frame_path": self._stop_frame_path or "",
            "prior_summary": getattr(self, "_last_revise_summary", "") or "",
            "prior_diagnosis": getattr(self, "_last_diagnosis_json", "") or "",
            "chat_session": getattr(self, "_chat_session", None),
        }
        v_key = self._vision_api_key.text().strip()
        v_model = self._vision_model.currentText().strip()
        if v_key and v_model:
            params["vision_assist"] = {
                "provider": self._current_vision_provider(),
                "api_key": v_key,
                "model": v_model,
                "api_endpoint": self._vision_endpoint.text().strip() or None,
                "compress_images": self._compress_img_cb.isChecked(),
            }
        elif self._send_img_cb.isChecked():
            # 未配辅助识图时，若主模型支持看图则回退主模型
            params["vision_assist"] = {
                "provider": self._current_provider(),
                "api_key": api_key,
                "model": self._model.currentText().strip(),
                "api_endpoint": self._endpoint.text().strip() or None,
                "compress_images": self._compress_img_cb.isChecked(),
            }

        self._generate_btn.setEnabled(False)
        self._revise_btn.setEnabled(False)
        self._confirm_btn.setEnabled(False)
        self._trial_btn.setEnabled(False)
        if hasattr(self, "_rerevise_btn"):
            self._rerevise_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._set_agent_busy("修订", run_label, indeterminate=True)
        self._keep_prev_live_code(phase="修订")
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._trajectory.begin_run(run_label, fresh=False)
        if self._archive.session_dir is not None:
            self._archive.append_event("revise_begin")
        self._trajectory.update_step(
            "revise",
            "Revise",
            run_label,
            status="running",
            body=(feedback[:400] + ("…" if len(feedback) > 400 else "")),
        )

        self._revise_worker = ReviseWorker(params)
        self._revise_worker.finished.connect(self._on_revise_success)
        self._revise_worker.partial.connect(self._on_partial)
        self._revise_worker.status.connect(self._on_status)
        self._revise_worker.artifact.connect(self._on_artifact)
        self._revise_worker.token_info.connect(self._on_token_info)
        self._revise_worker.error.connect(self._on_revise_error)
        self._revise_worker.start()

    def _feedback_bullets(self, feedback: str) -> list[str]:
        from datetime import date
        today = date.today().isoformat()
        lines = [ln.strip() for ln in (feedback or "").splitlines() if ln.strip()]
        if not lines:
            return []
        bullets = []
        for ln in lines:
            ln = re.sub(r"^[-*•]\s*", "", ln)
            ln = re.sub(r"^[\d]+[\.\)、]\s*", "", ln)
            if len(ln) > 400:
                ln = ln[:400] + "…"
            bullets.append(f"- {today}：{ln}")
        return bullets

    def _confirm_and_write_feedback(self, feedback: str) -> tuple[str | None, list[str]]:
        """分类压句后弹勾选框，确认才写入脚本解释。"""
        from backend.script_generator.feedback_opt import (
            distill_feedback,
            optimize_feedback_sync,
        )

        items = distill_feedback(feedback, self._explanation.toPlainText())
        if not items:
            return None, []

        api_key = self._api_key.text().strip()
        model = self._model.currentText().strip()
        if api_key and model and any(it.kind == "constraint" for it in items):
            progress = QProgressDialog("正在整理约束条目…", None, 0, 0, self)
            progress.setWindowTitle("脚本解释")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.setCancelButton(None)
            progress.show()
            QApplication.processEvents()
            try:
                items = optimize_feedback_sync(
                    items,
                    self._explanation.toPlainText(),
                    provider=self._current_provider(),
                    api_key=api_key,
                    model=model,
                    api_endpoint=self._endpoint.text().strip() or None,
                )
            except Exception as e:
                self._append_trial_log(f"[介绍] 约束整理失败，使用本地规则: {e}")
            finally:
                progress.close()

        dlg = FeedbackWritebackDialog(items, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._append_trial_log("[介绍] 已跳过写回（未确认）")
            return None, []
        bullets = dlg.selected_bullets()
        if not bullets:
            self._append_trial_log("[介绍] 未勾选任何条目，不写回")
            return None, []
        msg = self._merge_feedback_into_explanation(feedback, bullets=bullets)
        return msg, bullets

    def _merge_feedback_into_explanation(self, feedback: str, bullets: list[str] | None = None) -> str | None:
        """把试运行反馈并进介绍（编辑框 + 原 txt），避免下次生成再踩同样的坑。"""
        new_lines = list(bullets) if bullets is not None else self._feedback_bullets(feedback)
        if not new_lines:
            return None
        current = self._explanation.toPlainText().rstrip()
        header = f"## {self._FEEDBACK_SECTION}"
        to_add = []
        for line in new_lines:
            body = line.split("：", 1)[-1].strip()
            if line in current or (body and body in current):
                continue
            to_add.append(line)
        if not to_add:
            return None
        if header in current:
            merged = current + "\n" + "\n".join(to_add)
        else:
            merged = current + ("\n\n" if current else "") + header + "\n" + "\n".join(to_add)
        self._explanation.setPlainText(merged)
        self._explanation_text = merged

        try:
            path = self._persist_explanation(merged)
        except Exception as e:
            self._append_trial_log(f"[介绍] 写回失败: {e}")
            return f"介绍已更新，但写文件失败：{e}"
        if path is None:
            return "已写入当前介绍（未绑定 txt，仅本次窗口有效；请加载介绍或选择图片文件夹后再保存）"
        msg = f"已把反馈写入 {path}"
        self._append_trial_log(f"[介绍] {msg}")
        return msg

    def _on_revise_success(self, code: str, summary: str = "", meta=None):
        meta = meta if isinstance(meta, dict) else {}

        self._generated_code = code
        self._note_script_origin(code or "")
        self._stream_buf = code or ""
        self._set_live_code(code or "")
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        n_chars = len(code or "")
        self._last_revise_summary = (summary or "").strip()
        if isinstance(meta, dict) and meta.get("chat_session"):
            self._chat_session = meta["chat_session"]
        if isinstance(meta, dict) and meta.get("continue_chat"):
            self._append_trial_log("[修订] 已使用生成同会话续写")
        if isinstance(meta, dict) and meta.get("diagnosis"):
            try:
                import json as _json
                self._last_diagnosis_json = _json.dumps(
                    meta["diagnosis"], ensure_ascii=False, indent=2,
                )
            except Exception:
                pass
        self._sync_trial_code()
        self._reset_trial_surface_after_new_code()
        trial_blocked = self._trial_blocked
        if trial_blocked:
            reason = self._trial_block_reason or "脚本检查未通过"
            self._set_agent_idle("修订完成 · 校验未过不可试运行", success=False)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(f"已修订 · {n_chars:,} 字符 · 校验未过")
            self._set_warn_banner(f"修订完成但不可试运行　　{reason}")
            self._append_trial_log(
                "[修订] 脚本检查未通过，不可试运行。"
                + (f" ({reason})" if reason else "")
            )
        else:
            self._set_agent_idle("✓ 修订完成 · 可去试运行", success=True)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(f"已修订 · {n_chars:,} 字符 · 可保存或试运行")
            self._set_ok_banner(
                f"✓ 修订完成　　{n_chars:,} 字符　　可保存或去试运行验证"
            )
            if hasattr(self, "_to_trial_btn"):
                self._to_trial_btn.setProperty("ready", True)
                self._to_trial_btn.style().unpolish(self._to_trial_btn)
                self._to_trial_btn.style().polish(self._to_trial_btn)
                self._to_trial_btn.setText("去试运行 → 验证脚本")
        self._update_trial_availability()
        summary = (summary or "").strip() or "（无摘要）"
        self._append_trial_log("[修订摘要]\n" + summary)
        done_summary = f"约 {n_chars:,} 字符。\n{summary}"
        self._trajectory.succeed_run(
            "修订完成" if not trial_blocked else "修订完成（校验未过）",
            summary=done_summary,
        )
        self._flash_taskbar("反馈修订完成")
        self._last_revise_error = ""

        # 先展示修订结果，再弹「新增约束」确认（避免结果被写回对话框挡住）
        review_fail = not meta.get("review_ok", True) or "未完全通过" in summary
        if trial_blocked:
            title = "修订完成 — 脚本检查未通过"
            tip = (
                "代码已写入试运行临时文件，但脚本检查未通过，不可试运行。\n\n"
                f"{summary}\n\n"
                f"首项：{self._trial_block_reason or '见试运行日志'}\n\n"
                "请在「查看完整代码」改 _img 路径或结构后保存，会自动重新检查。\n"
                "关闭本窗后可选择是否把反馈写入脚本介绍。"
            )
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Critical)
        elif review_fail:
            title = "修订完成 — 审查未完全通过"
            tip = (
                "代码已更新并同步到试运行临时文件。\n\n"
                f"{summary}\n\n"
                "审查认为仍有反馈未落实（已自动补修至多 2 轮）。"
                "可改反馈后「重修订」或试运行验证。\n"
                "关闭本窗后可选择是否把反馈写入脚本介绍。"
            )
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Warning)
        else:
            title = "修订完成 — 请核对"
            tip = (
                "代码已更新并同步到试运行临时文件。\n\n"
                f"{summary}\n\n"
                "若仍与预期不符，请改反馈后「重修订」或再次修订。\n"
                "关闭本窗后可选择是否把反馈写入脚本介绍。"
            )
            _show_scroll_message(self, title, tip)

        feedback = self._feedback.toPlainText().strip()
        wrote = None
        writeback: list[str] = []
        if feedback:
            wrote, writeback = self._confirm_and_write_feedback(feedback)
        self._archive_revise_done(code, summary, writeback=writeback or None)
        if wrote:
            QMessageBox.information(self, "介绍已更新", wrote)

        if self._feedback.toPlainText().strip():
            self._set_feedback_stale(True)

    def _on_feedback_edited(self):
        if getattr(self, "_feedback_stale", False):
            self._set_feedback_stale(False)
        self._update_trial_availability()

    def _set_feedback_stale(self, stale: bool):
        self._feedback_stale = bool(stale)
        if not hasattr(self, "_feedback"):
            return
        self._feedback.setProperty("stale", "true" if stale else "false")
        self._feedback.style().unpolish(self._feedback)
        self._feedback.style().polish(self._feedback)
        if hasattr(self, "_feedback_stale_hint"):
            self._feedback_stale_hint.setVisible(stale)

    def _try_recover_code_from_trial(self) -> bool:
        """修订失败清空预览后，从试运行临时文件恢复内存中的代码。"""
        try:
            path = SCRIPTS_PATH / _TRIAL_REL
            if not path.is_file():
                return False
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                return False
            self._generated_code = text if text.endswith("\n") else text + "\n"
            self._stream_buf = self._generated_code
            self._set_live_code(self._generated_code)
            self._view_code_btn.setEnabled(True)
            self._save_btn.setEnabled(True)
            self._copy_btn.setEnabled(True)
            return True
        except Exception as e:
            print(f"[ScriptGenerator] 从试运行文件恢复代码失败: {e}")
            return False

    def _restore_code_preview_after_agent_fail(self) -> None:
        """生成/修订失败时恢复右侧代码预览与操作按钮（不丢 _generated_code）。"""
        code = (self._generated_code or "").strip()
        if not code:
            self._try_recover_code_from_trial()
            code = (self._generated_code or "").strip()
        if not code:
            return
        self._stream_buf = self._generated_code or code
        self._set_live_code(self._stream_buf)
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)

    def _on_revise_error(self, msg: str):
        translated = self._translate_error(msg)
        self._last_revise_error = translated
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        reason = (translated.split("\n", 1)[0] or "修订失败").strip()
        self._set_agent_idle(f"修订失败 · {reason}")
        self._restore_code_preview_after_agent_fail()
        self._update_trial_availability()
        self._trajectory.update_step(
            "revise",
            "Error",
            reason,
            status="error",
            body=translated,
        )
        self._trajectory.fail_run(reason)
        QMessageBox.critical(self, "修订失败", translated)

    def _set_trial_pseudo_env(self, enabled: bool) -> None:
        """试跑期间设置环境变量，供脚本内 getenv 分支启用伪录制。"""
        import os
        from backend.automation.run_recorder import ENV_FLAG

        if enabled:
            os.environ[ENV_FLAG] = "1"
        else:
            os.environ.pop(ENV_FLAG, None)

    def _report_trial_feasibility(self, status: str = "") -> None:
        """试跑结束后输出 L1 可行性评估；优化后再试跑时对比可靠性。"""
        try:
            from backend.script_generator.feasibility import (
                assess_trial_feasibility,
                compare_reliability,
            )
            from backend.script_generator.pseudo_analyze import find_latest_pseudo_record

            log = "\n".join(self._trial_log_lines)
            record_dir = None
            try:
                hint = ""
                if getattr(self, "_optimize_script_path", None):
                    hint = self._optimize_script_path.stem
                elif (self._optimize_code or self._generated_code or "").strip():
                    # 试跑临时脚本名不稳定时仍尽量用最近录制
                    hint = ""
                account = getattr(self, "_trial_account_name", "") or ""
                rec = find_latest_pseudo_record(
                    script_hint=hint, account_hint=account,
                )
                if rec is None and hint:
                    rec = find_latest_pseudo_record(script_hint=hint)
                if rec is None:
                    rec = find_latest_pseudo_record()
                record_dir = rec
            except Exception:
                record_dir = None

            feas = assess_trial_feasibility(
                log, status=status, record_dir=record_dir,
            )
            msg = feas.get("user_message") or ""
            if msg:
                self._append_trial_log("[可行性]\n" + msg)
            if hasattr(self, "_append_optimize_log"):
                self._append_optimize_log("[试跑可行性]\n" + msg)

            if (
                self._awaiting_reliability_retrial
                and (self._pre_optimize_trial_log or "").strip()
            ):
                cmp = compare_reliability(
                    self._pre_optimize_trial_log,
                    log,
                    before_record_dir=getattr(self, "_pre_optimize_record_dir", None) or None,
                    after_record_dir=record_dir,
                )
                self._awaiting_reliability_retrial = False
                if cmp.get("ok"):
                    note = "[可靠性对比] 相对优化前未见明显劣化（匹配/终态信号）。"
                else:
                    degraded = cmp.get("degraded") or []
                    note = (
                        "[可靠性对比] ⚠️ 相对优化前可能下降：\n"
                        + "\n".join(f"· {d}" for d in degraded)
                        + "\n建议回退或再修订，勿只追求速度。"
                    )
                self._append_trial_log(note)
                if hasattr(self, "_append_optimize_log"):
                    self._append_optimize_log(note)
                feas = dict(feas)
                feas["reliability_compare"] = {
                    "ok": cmp.get("ok"),
                    "degraded": cmp.get("degraded"),
                    "before_level": (cmp.get("before") or {}).get("level"),
                    "after_level": (cmp.get("after") or {}).get("level"),
                    "before_match_source": (
                        ((cmp.get("before") or {}).get("signals") or {}).get("match_source")
                    ),
                    "after_match_source": (
                        ((cmp.get("after") or {}).get("signals") or {}).get("match_source")
                    ),
                }

            if getattr(self, "_trial_hud", None) is not None:
                self._trial_hud.set_feasibility(feas)
            if self._archive.session_dir:
                try:
                    self._archive.write_text(
                        "feasibility.json",
                        __import__("json").dumps(feas, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[ScriptGenerator] 可行性评估失败: {e}")

    def _strip_pseudo_from_working_code(self) -> str:
        """从当前工作副本去掉伪录制，写回内存（正式保存用）。"""
        from backend.script_generator.pseudo_codegen import strip_pseudo_record_for_save

        code = strip_pseudo_record_for_save(self._generated_code or "")
        self._generated_code = code
        self._stream_buf = code
        if hasattr(self, "_optimize_code") and (self._optimize_code or "").strip():
            self._optimize_code = strip_pseudo_record_for_save(self._optimize_code)
        return code

    def _on_confirm_done(self):
        if not (self._generated_code or "").strip():
            QMessageBox.warning(self, "无代码", "没有可保存的脚本。")
            return
        if self._trial_running:
            QMessageBox.warning(self, "仍在试运行", "请先停止试运行再确认保存。")
            return
        reply = QMessageBox.question(
            self,
            "确认保存",
            "确定将当前脚本保存为正式文件吗？\n"
            "保存时会去掉试运行用的伪录制开关。\n"
            "保存后可在脚本列表中使用。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._strip_pseudo_from_working_code()
        self._save_script()
