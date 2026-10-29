"""Simple visual-agent workbench UI (runtime planning is connected later)."""

from __future__ import annotations

import asyncio
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QTextBrowser,
    QLineEdit,
    QTabWidget,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from backend.mcp_integration.execution_bridge import ExecutionBridge
from core.ai.schemes import AISchemeStore
from core.path import ICON_PATH
from gui.styles.theme import current_theme_from_config, normalize_theme

_LOG_TAGS = {"execution": "[执行]", "decision": "[决策]", "system": "[系统]"}
_LOG_COLORS = {
    "light": {"execution": "#16665e", "decision": "#3f3a9e", "system": "#6b6b6b"},
    "dark": {"execution": "#6fc9bb", "decision": "#b0adff", "system": "#8b929c"},
}


class VisualAgentWindow(QWidget):
    """Modeless GUI for configuring and observing one AI execution task."""

    result_ready = Signal(str, object)
    followup_submitted = Signal(str)
    agent_event = Signal(str, object)
    agent_runtime_ready = Signal(object)

    def __init__(self, controller, parent=None):
        super().__init__(None, Qt.Window)
        self.setWindowTitle("AI 执行")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setObjectName("VisualAgentWindow")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setMinimumSize(700, 560)
        self.resize(820, 680)

        if parent is not None and parent.styleSheet():
            self.setStyleSheet(parent.styleSheet())

        self._controller = controller
        self._bridge = ExecutionBridge(controller)
        self._schemes = AISchemeStore()
        self._log_theme = current_theme_from_config()
        self._account: dict = {}
        self._running = False
        self._agent_runtime = None
        self._agent_future = None
        self._last_task_context = ""
        self._suppress_next_task_echo = False
        self.result_ready.connect(self._on_result)
        self.agent_event.connect(self._on_agent_event)
        self.agent_runtime_ready.connect(self._on_agent_runtime_ready)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("AI 视觉执行")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.status_label = QLabel("● 待命")
        self.status_label.setStyleSheet("color: #18a058; font-weight: 600;")
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(self.status_label)
        root.addLayout(title_row)

        subtitle = QLabel("指定任务后，AI 将通过统一执行层观察并操作当前目标。")
        subtitle.setObjectName("MutedLabel")
        root.addWidget(subtitle)

        target_card = QFrame()
        target_card.setObjectName("Card")
        target_layout = QVBoxLayout(target_card)
        target_layout.setContentsMargins(12, 10, 12, 10)
        target_layout.setSpacing(8)

        target_top = QHBoxLayout()
        self.account_label = QLabel("账号：-")
        self.account_label.setStyleSheet("font-weight: 600;")
        self.target_combo = QComboBox()
        self.target_combo.addItem("自动选择", "auto")
        self.target_combo.addItem("浏览器", "browser")
        self.target_combo.addItem("窗口", "window")
        self.refresh_btn = QPushButton("刷新信息")
        self.refresh_btn.setObjectName("SecondaryButton")
        self.refresh_btn.clicked.connect(self.refresh_target_info)
        self.center_btn = QPushButton("居中游戏")
        self.center_btn.setObjectName("SecondaryButton")
        self.center_btn.clicked.connect(self.center_game)
        target_top.addWidget(self.account_label)
        target_top.addStretch()
        target_top.addWidget(QLabel("目标："))
        target_top.addWidget(self.target_combo)
        target_top.addWidget(self.refresh_btn)
        target_top.addWidget(self.center_btn)
        target_layout.addLayout(target_top)

        scheme_row = QHBoxLayout()
        scheme_row.addWidget(QLabel("AI 方案："))
        self.scheme_combo = QComboBox()
        self.scheme_combo.setMinimumWidth(220)
        self.scheme_combo.currentIndexChanged.connect(self._on_scheme_changed)
        scheme_row.addWidget(self.scheme_combo, 1)
        self.scheme_info = QLabel("")
        self.scheme_info.setObjectName("MutedLabel")
        scheme_row.addWidget(self.scheme_info)
        target_layout.addLayout(scheme_row)

        self.target_info = QLabel("尚未读取目标信息")
        self.target_info.setWordWrap(True)
        self.target_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        target_layout.addWidget(self.target_info)
        root.addWidget(target_card)

        self.content_splitter = QSplitter(Qt.Vertical)
        self.content_splitter.setChildrenCollapsible(False)
        # Keep the task/output divider subtle while retaining a draggable hit area.
        self.content_splitter.setHandleWidth(3)

        task_pane = QWidget()
        task_layout = QVBoxLayout(task_pane)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.setSpacing(8)
        task_label = QLabel("任务目标")
        task_label.setStyleSheet("font-weight: 600;")
        task_layout.addWidget(task_label)
        self.task_input = QTextEdit()
        self.task_input.setPlaceholderText(
            "例如：完成游戏中的每日任务；遇到付费、验证码或无法判断的界面时停止并询问。"
        )
        self.task_input.setMinimumHeight(58)
        task_layout.addWidget(self.task_input, 1)

        control_row = QHBoxLayout()
        self.start_btn = QPushButton("开始执行")
        self.start_btn.setObjectName("PrimaryButton")
        self.start_btn.clicked.connect(self.start_task)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("DangerButton")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_task)
        self.clear_btn = QPushButton("清空日志")
        self.clear_btn.setObjectName("SecondaryButton")
        self.clear_btn.clicked.connect(self._clear_output)
        control_row.addWidget(self.start_btn)
        control_row.addWidget(self.stop_btn)
        control_row.addStretch()
        control_row.addWidget(self.clear_btn)
        task_layout.addLayout(control_row)
        task_pane.setMinimumHeight(118)
        self.content_splitter.addWidget(task_pane)

        self.output_tabs = QTabWidget()

        reply_page = QWidget()
        reply_layout = QVBoxLayout(reply_page)
        reply_layout.setContentsMargins(8, 8, 8, 8)
        reply_layout.setSpacing(8)
        self.llm_view = QTextBrowser()
        self.llm_view.setOpenExternalLinks(False)
        self.llm_view.setPlaceholderText("AI 的观察结论、询问和任务结果会显示在这里。")
        reply_layout.addWidget(self.llm_view, 1)
        followup_row = QHBoxLayout()
        self.followup_input = QLineEdit()
        self.followup_input.setPlaceholderText("补充要求或回答 AI 的问题……")
        self.followup_input.returnPressed.connect(self._submit_followup)
        self.followup_btn = QPushButton("发送")
        self.followup_btn.setObjectName("PrimaryButton")
        self.followup_btn.clicked.connect(self._submit_followup)
        followup_row.addWidget(self.followup_input, 1)
        followup_row.addWidget(self.followup_btn)
        reply_layout.addLayout(followup_row)
        self.output_tabs.addTab(reply_page, "AI 回复")

        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(8, 8, 8, 8)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("目标读取、AI 决策和工具调用将显示在这里。")
        log_layout.addWidget(self.log_view)
        self.output_tabs.addTab(log_page, "执行记录")
        self.output_tabs.setMinimumHeight(190)
        self.content_splitter.addWidget(self.output_tabs)
        self.content_splitter.setStretchFactor(0, 0)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setSizes([135, 330])
        root.addWidget(self.content_splitter, 1)

        footer = QHBoxLayout()
        endpoint = QLabel("MCP：http://127.0.0.1:8765/mcp")
        endpoint.setObjectName("MutedLabel")
        self.safety_label = QLabel("单步观察 → 单步执行 → 重新观察")
        self.safety_label.setObjectName("MutedLabel")
        footer.addWidget(endpoint)
        footer.addStretch()
        footer.addWidget(self.safety_label)
        root.addLayout(footer)

    def set_account(self, account: dict) -> None:
        self._account = dict(account or {})
        name = str(self._account.get("name") or "-")
        self.account_label.setText(f"账号：{name}")
        preferred = str(self._account.get("_target") or "auto")
        index = self.target_combo.findData(preferred)
        self.target_combo.setCurrentIndex(index if index >= 0 else 0)
        self._reload_schemes()
        self.refresh_target_info()

    def _reload_schemes(self) -> None:
        selected = self._schemes.selected_id("visual_agent")
        schemes = self._schemes.list_schemes("visual_agent")
        self.scheme_combo.blockSignals(True)
        self.scheme_combo.clear()
        for scheme in schemes:
            label = f"{scheme.name} · {scheme.vision_model or scheme.main_model or '未配置模型'}"
            self.scheme_combo.addItem(label, scheme.id)
        index = self.scheme_combo.findData(selected)
        self.scheme_combo.setCurrentIndex(index if index >= 0 else (0 if schemes else -1))
        self.scheme_combo.blockSignals(False)
        self._update_scheme_info()

    def _update_scheme_info(self) -> None:
        scheme_id = str(self.scheme_combo.currentData() or "")
        scheme = self._schemes.get(scheme_id)
        if scheme is None:
            self.scheme_info.setText("没有可用的视觉方案")
            return
        model = scheme.vision_model or scheme.main_model
        provider = scheme.vision_provider or scheme.main_provider
        self.scheme_info.setText(f"{provider} / {model}")

    def _on_scheme_changed(self, _index: int) -> None:
        self._update_scheme_info()
        scheme_id = str(self.scheme_combo.currentData() or "")
        if not scheme_id:
            return
        try:
            self._schemes.select("visual_agent", scheme_id)
        except Exception as exc:
            self._append_log(f"保存 AI 方案选择失败：{exc}")

    def _submit(self, operation: str, coroutine) -> None:
        try:
            future = self._controller.submit(coroutine)
        except Exception as exc:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            self._append_log(f"{operation}提交失败：{exc}")
            return

        def done(completed):
            try:
                result = completed.result()
            except Exception as exc:  # result is rendered on the Qt thread
                result = {"success": False, "error": str(exc)}
            self.result_ready.emit(operation, result)

        future.add_done_callback(done)

    def refresh_target_info(self) -> None:
        account = str(self._account.get("name") or "")
        if not account:
            return
        self.refresh_btn.setEnabled(False)
        target = str(self.target_combo.currentData() or "auto")
        self._submit("target_info", self._bridge.get_target_info(account, target))

    def center_game(self) -> None:
        account = str(self._account.get("name") or "")
        if not account:
            return
        self.center_btn.setEnabled(False)
        self._append_log("正在调整页面滚动位置……")
        self._submit("center_game", self._bridge.center_game(account, "browser"))

    def start_task(self) -> None:
        task = self.task_input.toPlainText().strip()
        if not task:
            QMessageBox.information(self, "AI 执行", "请先填写任务目标。")
            return
        scheme_id = str(self.scheme_combo.currentData() or "")
        if not scheme_id:
            QMessageBox.information(
                self,
                "AI 执行",
                "没有支持视觉的 AI 方案，请先在脚本生成的 API 配置中保存视觉模型。",
            )
            return
        try:
            model = self._schemes.resolve(scheme_id, "visual_agent")
        except Exception as exc:
            QMessageBox.warning(self, "AI 执行", f"读取 AI 方案失败：{exc}")
            return
        if not model.configured:
            QMessageBox.information(
                self,
                "AI 执行",
                f"方案「{scheme_id}」缺少 {model.role} 模型、提供商或 API Key。",
            )
            return
        suppress_echo = self._suppress_next_task_echo
        self._suppress_next_task_echo = False
        self._last_task_context = task
        self._running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("● 已创建")
        self.status_label.setStyleSheet("color: #d08a00; font-weight: 600;")
        self._append_log(f"方案：{scheme_id}（{model.provider} / {model.model}）")
        self._append_log(f"任务：{task}")
        self._append_log("正在后台初始化视觉执行器；界面可继续操作。")
        if not suppress_echo:
            self._append_llm_message("user", task)
        self._append_llm_message("assistant", "任务已收到，正在观察当前游戏画面。")
        self.output_tabs.setCurrentIndex(0)
        account = str(self._account.get("name") or "")
        target = str(self.target_combo.currentData() or "auto")
        model_params = model.as_runtime_params()

        def build_runtime():
            # Imports OpenCV/NumPy and the large shared LLM module.  Importing and
            # constructing this object on the Qt thread previously froze the whole
            # window whenever an AI run was started.
            from time import perf_counter
            started = perf_counter()
            from backend.visual_agent.dev_loader import load_runtime_class, source_hot_reload_enabled
            VisualAgentRuntime = load_runtime_class()
            runtime = VisualAgentRuntime(
                self._controller, account=account, target=target, task=task,
                model=model_params,
                on_event=lambda kind, data: self.agent_event.emit(kind, data),
                max_steps=None,
                decision_layer_enabled=True,
                same_frame_timeout=10.0,
            )
            runtime._bootstrap_seconds = perf_counter() - started
            runtime._hot_reloaded = source_hot_reload_enabled()
            return runtime

        async def bootstrap_and_run():
            runtime = await asyncio.to_thread(build_runtime)
            self.agent_runtime_ready.emit(runtime)
            return await runtime.run()

        try:
            self._agent_future = self._controller.submit(bootstrap_and_run())
        except Exception as exc:
            self._finish_agent({"status": "error", "message": str(exc)})
            return

        def done(completed):
            # User stop cancels the submitted Future after stop_task() has already
            # rendered the terminal "stopped" state.  A cancelled callback is not
            # an execution failure and must not overwrite it with bare "error".
            if completed.cancelled() or not self._running:
                return
            try:
                result = completed.result()
            except BaseException as exc:
                result = {"status": "error", "message": str(exc)}
            self.agent_event.emit("done", result)

        self._agent_future.add_done_callback(done)

    def _on_agent_runtime_ready(self, runtime: object) -> None:
        """Receive the worker-built runtime without touching Qt from that worker."""
        if not self._running:
            stop = getattr(runtime, "stop", None)
            if callable(stop):
                stop()
            return
        self._agent_runtime = runtime
        elapsed = float(getattr(runtime, "_bootstrap_seconds", 0.0) or 0.0)
        reload_note = "，执行后端已热重载" if bool(getattr(runtime, "_hot_reloaded", False)) else ""
        self._append_log(
            f"视觉执行器后台初始化完成（{elapsed:.2f}s{reload_note}），开始观察目标画面。"
        )
        self.status_label.setText("● 正在观察")
        self.status_label.setStyleSheet("color: #d08a00; font-weight: 600;")

    def stop_task(self) -> None:
        if self._agent_runtime is not None:
            self._agent_runtime.stop()
        if self._agent_future is not None and not self._agent_future.done():
            self._agent_future.cancel()
        self._finish_agent({"status": "stopped", "message": "已停止当前 AI 执行任务。"})

    def _on_result(self, operation: str, result: object) -> None:
        data = result if isinstance(result, dict) else {"success": False, "error": str(result)}
        if operation == "target_info":
            self.refresh_btn.setEnabled(True)
            if not data.get("success"):
                self.target_info.setText(f"目标信息读取失败：{data.get('error')}")
                return
            target = data.get("target", "-")
            execution = data.get("execution") or {}
            if target == "browser":
                page = data.get("page") or {}
                scroll = data.get("scroll") or {}
                game = data.get("game_frame") or {}
                progress = float(scroll.get("progress_y") or 0) * 100
                self.target_info.setText(
                    f"浏览器 · {page.get('title') or '-'}\n"
                    f"视口：{(data.get('viewport') or {}).get('width', '-')} × "
                    f"{(data.get('viewport') or {}).get('height', '-')}　"
                    f"滚动：{progress:.1f}%　"
                    f"游戏区域：{'已找到' if game.get('found') else '未找到'}　"
                    f"{'已居中' if game.get('centered') else '未居中'}"
                )
            else:
                window = data.get("window") or {}
                self.target_info.setText(
                    f"窗口 · {window.get('title') or '-'}\n"
                    f"客户区：{window.get('client_width', '-')} × {window.get('client_height', '-')}　"
                    f"状态：{'可执行' if execution.get('input_allowed') else '不可执行'}"
                )
            return

        if operation == "center_game":
            self.center_btn.setEnabled(True)
            if data.get("success"):
                after = data.get("after") or {}
                progress = float((after.get("scroll") or {}).get("progress_y") or 0) * 100
                self._append_log(f"游戏区域已调整，当前滚动进度 {progress:.1f}%")
                self.refresh_target_info()
            else:
                self._append_log(f"游戏区域居中失败：{data.get('error') or '未找到游戏区域'}")

    def apply_theme(self, theme: str | None = None) -> None:
        """Pick the palette for subsequent log lines.

        Lines already written keep their colours; only new lines adopt the
        new theme.
        """
        self._log_theme = normalize_theme(theme or current_theme_from_config())

    def _append_log(self, text: str, layer: str = "system") -> None:
        colors = _LOG_COLORS.get(getattr(self, "_log_theme", "light"), _LOG_COLORS["light"])
        base = QTextCharFormat()
        base.setForeground(QColor(colors.get(layer, colors["system"])))
        tag_fmt = QTextCharFormat(base)
        if layer == "decision":
            tag_fmt.setFontWeight(600)
        stamp = datetime.now().strftime("%H:%M:%S")
        tag = _LOG_TAGS.get(layer, _LOG_TAGS["system"])
        bar = self.log_view.verticalScrollBar()
        # Only follow the tail when the user is already reading the tail;
        # otherwise new lines must not yank the view away from old records.
        follow = bar.value() >= bar.maximum() - 2
        cursor = QTextCursor(self.log_view.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(f"[{stamp}] ", base)
        cursor.insertText(f"{tag} ", tag_fmt)
        cursor.insertText(f"{text}\n", base)
        if follow:
            self.log_view.setTextCursor(cursor)
            self.log_view.ensureCursorVisible()

    def _on_agent_event(self, kind: str, data: object) -> None:
        if kind == "reply":
            self.append_llm_reply(str(data))
        elif kind == "log":
            entry = data if isinstance(data, dict) else {"text": str(data)}
            self._append_log(str(entry.get("text") or ""), str(entry.get("layer") or "execution"))
        elif kind == "status":
            self.status_label.setText(f"● {data}")
            self.status_label.setStyleSheet("color: #d08a00; font-weight: 600;")
        elif kind == "usage" and isinstance(data, dict):
            self._append_log(
                f"模型用量：输入 {data.get('input', 0)} / 输出 {data.get('output', 0)} tokens",
                str(data.get("layer") or "system"),
            )
        elif kind == "evidence" and isinstance(data, dict):
            passed = bool(data.get("success"))
            evidence = [str(item) for item in data.get("evidence") or []]
            uncertainty = [str(item) for item in data.get("uncertainty") or []]
            confidence = float(data.get("confidence") or 0) * 100
            lines = [
                "最终验证：" + ("通过" if passed else "未通过"),
                str(data.get("summary") or "未提供总结"),
            ]
            if evidence:
                lines.append("可见证据：" + "；".join(evidence))
            if uncertainty:
                lines.append("不确定项：" + "；".join(uncertainty))
            lines.append(f"验证置信度：{confidence:.0f}%")
            self.append_llm_reply("\n".join(lines))
            report_path = str(data.get("report_path") or "")
            if report_path:
                self._append_log(f"执行证据已保存：{report_path}")
        elif kind == "done":
            self._finish_agent(data if isinstance(data, dict) else {"status":"error","message":str(data)})

    def _finish_agent(self, result: dict) -> None:
        status = str(result.get("status") or "error")
        message = str(result.get("message") or status)
        if status == "error" and message.strip().lower() == "error":
            last_state = result.get("last_state") or {}
            report_path = str(result.get("report_path") or "")
            details = []
            if last_state:
                details.append(f"最后状态：{last_state}")
            if report_path:
                details.append(f"诊断报告：{report_path}")
            message = "视觉执行异常，但底层服务只返回了 error，未提供具体原因。"
            if details:
                message += "\n" + "\n".join(details)
        if "model_not_found" in message or "Model not exist" in message:
            message = (
                "模型不存在或当前账号无权调用。当前方案的视觉模型标识可能已经失效。\n"
                "请到 设置 → AI 配置 → 模型，将视觉模型改为百炼控制台当前可用的模型，"
                "例如 qwen3-vl-plus，并确认 API 服务地域与 Key 一致。"
            )
        self._running = False
        self._agent_runtime = None
        self._agent_future = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if status == "finished":
            label, color = "● 已完成", "#18a058"
        elif status == "needs_user":
            label, color = "● 等待用户", "#d08a00"
        elif status == "stopped":
            label, color = "● 已停止", "#c53b3b"
        else:
            label, color = "● 执行结束", "#c53b3b"
        self.status_label.setText(label)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self._append_log(f"任务结束：{status} · {message}")
        if status in ("needs_user", "limit", "error"):
            self.append_llm_reply(message)

    def _append_llm_message(self, role: str, text: str) -> None:
        is_user = role == "user"
        label = "你" if is_user else "AI"
        cursor = self.llm_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.llm_view.document().isEmpty():
            cursor.insertBlock()
        block = QTextBlockFormat()
        block.setAlignment(Qt.AlignRight if is_user else Qt.AlignLeft)
        block.setTopMargin(6)
        block.setBottomMargin(6)
        cursor.setBlockFormat(block)
        who = QTextCharFormat()
        who.setForeground(QBrush(QColor("#777777")))
        who.setFontPointSize(9)
        cursor.insertText(label + "\n", who)
        bubble = QTextCharFormat()
        bubble.setForeground(QBrush(QColor("#202020")))
        bubble.setBackground(QBrush(QColor("#dce9ff" if is_user else "#eeeeee")))
        bubble.setFontPointSize(11)
        cursor.insertText(str(text or ""), bubble)
        self.llm_view.setTextCursor(cursor)
        self.llm_view.ensureCursorVisible()

    def append_llm_reply(self, text: str) -> None:
        """Public UI hook for the future visual-agent runtime."""
        self._append_llm_message("assistant", text)
        # Do not steal the user's active tab.  start_task() intentionally opens
        # AI replies once; subsequent background replies may arrive while the
        # user is inspecting the execution log.

    def _submit_followup(self) -> None:
        text = self.followup_input.text().strip()
        if not text:
            return
        self.followup_input.clear()
        self.followup_submitted.emit(text)
        if not self._running:
            if not self._last_task_context:
                self._append_llm_message("user", text)
                self._append_llm_message("assistant", "当前没有可继续的任务，请先填写任务目标并开始执行。")
                return
            self._append_llm_message("user", text)
            continued = (
                f"此前任务：{self._last_task_context}\n"
                f"用户对上次结果的补充或纠正：{text}\n"
                "请重新观察当前画面，以用户最新补充为准继续完成任务；"
                "不得沿用上次未经充分证明的完成结论。"
            )
            self.task_input.setPlainText(continued)
            self._suppress_next_task_echo = True
            self._append_log("上次运行已结束；正以用户纠正后的任务重新观察并续跑。")
            self.start_task()
            return
        self._append_llm_message("user", text)
        self._append_log(f"用户补充：{text}")

    def _clear_output(self) -> None:
        self.log_view.clear()
        self.llm_view.clear()
