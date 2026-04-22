from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Signal, QSize, Qt
from PySide6.QtGui import QTextCursor, QIcon, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QComboBox
)

from core.logging.events import LogLevel, LogEvent
from core.path import PROJECT_ROOT
from core.state.events import StateEvent, StateDomain

from gui.panels.screenshot_viewer import ScreenshotViewer


def load_qss(path) -> str:
    if isinstance(path, str):
        path = Path(path)
    return path.read_text(encoding="utf-8")


class AccountPanel(QWidget):
    start_task = Signal(dict, str)
    stop_task = Signal(dict)
    browser_ready_notify = Signal(str)

    reconnect = Signal(dict)
    start_browser = Signal(dict)
    close = Signal(dict)

    refresh_tasks = Signal(dict)
    request_screenshot = Signal(dict)

    def __init__(self, account: dict, tasks: list[str]):
        super().__init__()
        self.setObjectName("AccountPanel")
        self.account = account
        self.tasks = tasks

        self.current_task: str | None = None
        self.running = False
        self.logs: list[str] = []
        self.browser_ready = False
        self.last_log = None

        self._full_url = ""
        self._full_title = ""
        title = QLabel(f"账号：{account['name']}")
        title.setObjectName("AccountTitle")
        self.task_combo = QComboBox()
        self.task_combo.addItems(tasks)
        if tasks:
            self.current_task = tasks[0]
        self.task_combo.currentTextChanged.connect(self.on_task_changed)

        self.browser_state = "待启动"

        self.status_label = QLabel(f"状态：{self.browser_state}")
        self.status_label.setObjectName("StatusLabel")
        self.url_label = QLabel("URL：-")
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.title_label = QLabel("标题：-")

        top_row_widget = QWidget()
        top_row_widget.setObjectName("TopRow")
        top_row = QHBoxLayout(top_row_widget)
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)
        script_select = QWidget()
        script_select.setObjectName("ScriptSelect")
        script_select_layout = QHBoxLayout(script_select)
        script_select_layout.setContentsMargins(0, 0, 0, 0)
        script_select_layout.setSpacing(4)

        script_label = QLabel("脚本：")
        script_label.setObjectName("ScriptSelectLabel")

        self.task_combo.setObjectName("ScriptSelectCombo")
        self.refresh_btn = QPushButton()
        self.refresh_btn.setObjectName("ScriptRefreshBtn")
        self.refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        self.refresh_btn.setIconSize(QSize(16, 16))
        self.refresh_btn.setToolTip("重新获取脚本列表")
        self.refresh_btn.setFixedSize(22, 22)
        self.refresh_btn.clicked.connect(
            lambda: self.refresh_tasks.emit(self.account)
        )

        script_select_layout.addWidget(script_label)
        script_select_layout.addWidget(self.task_combo)
        script_select_layout.addWidget(self.refresh_btn)

        top_row.addWidget(script_select)

        top_row.addSpacing(20)
        top_row.addWidget(self.status_label)
        top_row.addStretch()

        self.start_btn = QPushButton("开始")
        self.stop_btn = QPushButton("停止")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        self.reconnect_btn = QPushButton("重新连接")
        self.reconnect_btn.setToolTip("中断当前连接并重新连接浏览器")
        self.start_browser_btn = QPushButton("启动浏览器")
        self.close_btn = QPushButton("关闭")
        self.reconnect_btn.setEnabled(True)
        self.start_browser_btn.setEnabled(True)
        self.close_btn.setEnabled(False)

        self.start_btn.clicked.connect(self.on_start_clicked)
        self.stop_btn.clicked.connect(self.on_stop_clicked)

        self.reconnect_btn.clicked.connect(self.on_reconnect_clicked)
        self.start_browser_btn.clicked.connect(self.on_start_browser)
        self.close_btn.clicked.connect(lambda: self.close.emit(self.account))

        op_row = QHBoxLayout()
        op_row.addWidget(self.start_btn)
        op_row.addWidget(self.stop_btn)
        op_row.addSpacing(20)
        op_row.addWidget(self.reconnect_btn)
        op_row.addWidget(self.start_browser_btn)
        op_row.addWidget(self.close_btn)
        op_row.addStretch()

        info_row = QVBoxLayout()
        info_row.addWidget(self.title_label)
        info_row.addWidget(self.url_label)

        debug_row = QHBoxLayout()

        self.mouse_pos_label = QLabel("鼠标坐标：(-, -)")
        self.mouse_pos_label.setObjectName("MousePosLabel")

        self.screenshot_btn = QPushButton("获取截图")
        self.screenshot_btn.setObjectName("ScreenshotBtn")
        self.screenshot_btn.clicked.connect(
            lambda: self.request_screenshot.emit(self.account)
        )
        self.screenshot_btn.setEnabled(False)

        debug_row.addWidget(self.mouse_pos_label)
        debug_row.addStretch()
        debug_row.addWidget(self.screenshot_btn)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(top_row_widget)
        layout.addLayout(info_row)
        layout.addLayout(op_row)
        layout.addWidget(self.log_view, stretch=1)
        layout.addLayout(debug_row)

        self._all_buttons = [
            self.start_btn,
            self.stop_btn,
            self.reconnect_btn,
            self.start_browser_btn,
            self.close_btn,
            self.refresh_btn,
            self.screenshot_btn,
        ]

        self.setStyleSheet(
            load_qss(PROJECT_ROOT / "gui/styles/account_panel.qss")
        )

        self.append_log(f"{account['name']} 已添加")

    def on_start_browser(self):
        self.start_browser.emit(self.account)
        self.start_browser_btn.setEnabled(False)

    def on_task_changed(self, task: str):
        self.current_task = task

    def on_start_clicked(self):
        if not self.current_task or self.running:
            return

        self.running = True
        self._update_buttons()
        self.append_log(f"开始执行：{self.current_task}")
        self.set_browser_state(f'正在运行 "{self.current_task}"')

        self.start_task.emit(self.account, self.current_task)

    def on_reconnect_clicked(self):
        self.append_log("请求重新连接浏览器")
        self.running = False
        self.set_browser_ready(False)
        self._update_buttons()

        self.reconnect.emit(self.account)

    def on_stop_clicked(self):
        if not self.running:
            return

        self.running = False
        self._update_buttons()
        self.set_browser_state("已停止")
        self.stop_task.emit(self.account)

    def _update_buttons(self):
        if not self.browser_ready:
            for w in self._all_buttons:
                w.setEnabled(False)

            self.reconnect_btn.setEnabled(True)
            self.start_browser_btn.setEnabled(True)
            return

        for w in self._all_buttons:
            w.setEnabled(True)

        self.start_btn.setEnabled(not self.running)
        self.stop_btn.setEnabled(self.running)

    def append_log(self, text: str):
        event = LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message=text,
            source="legacy"
        )
        self.append_event(event)

    def append_event(self, event: LogEvent):
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        if event.level == LogLevel.ERROR:
            prefix = "[ERROR] "
        elif event.level == LogLevel.WARNING:
            prefix = "[WARN] "
        else:
            prefix = "[INFO] "

        message = event.message
        display_base = f"[{ts}] {prefix}{message}"

        scrollbar = self.log_view.verticalScrollBar()
        at_bottom = scrollbar.value() == scrollbar.maximum()

        if self.last_log is None:
            self.last_log = {
                "message": message,
                "start_time": now,
                "count": 1
            }
            self.log_view.append(display_base)

        elif self.last_log["message"] == message:
            self.last_log["count"] += 1
            duration = int((now - self.last_log["start_time"]).total_seconds())
            updated_line = f"{display_base} (x{self.last_log['count']}) - ({duration}s)"
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.movePosition(QTextCursor.StartOfBlock, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertText(updated_line)

        else:
            self.last_log = {
                "message": message,
                "start_time": now,
                "count": 1
            }
            self.log_view.append(display_base)

        while self.log_view.document().blockCount() > 100:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.Start)
            cursor.select(QTextCursor.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def set_browser_state(self, state: str):
        if self.browser_state == state:
            return

        self.browser_state = state
        self.status_label.setText(f"状态：{state}")

    def apply_state_event(self, event: StateEvent):
        if event.domain == StateDomain.BROWSER:

            if event.key == "ready":
                self.set_browser_ready(bool(event.value))

            elif event.key == "status":
                self.set_browser_state(event.value)

            elif event.key == "page":

                if "url" in event.value or "title" in event.value:
                    url = event.value.get("url", "")
                    title = event.value.get("title", "")
                    self._full_url = url
                    self._full_title = title
                    self._update_page_labels()

            elif event.key == "mouse_viewport":
                val = event.value or {}
                x = val.get("x", -1)
                y = val.get("y", -1)
                self.update_mouse_position(x=x, y=y)

        elif event.domain == StateDomain.TASK:
            if event.key == "running":
                self.set_running(bool(event.value))

    def set_running(self, running: bool):
        if self.running == running:
            return

        self.running = running
        self._update_buttons()

        if running:
            self.status_label.setText("状态：运行中")
        else:
            self.status_label.setText("状态：已停止")

    def set_browser_ready(self, ready: bool):
        if self.browser_ready == ready:
            return

        self.browser_ready = ready
        self._update_buttons()

        if ready:
            self.set_browser_state("浏览器就绪")
            self.browser_ready_notify.emit(self.account["name"])
        else:
            self.set_browser_state("浏览器未就绪")

    def apply_task_snapshot(self, snapshot):
        """
        snapshot: TaskSnapshot
        """

        running = snapshot.status not in ("idle", "finished", "stopped", "任务完成")
        self.set_running(running)

        if snapshot.status:
            pass

        if snapshot.message:
            self.append_log(snapshot.message)

    def update_tasks(self, tasks: list[str]):
        self.tasks = tasks

        current = self.task_combo.currentText()

        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItems(tasks)
        self.task_combo.blockSignals(False)
        if current in tasks:
            self.task_combo.setCurrentText(current)
            self.current_task = current
        elif tasks:
            self.task_combo.setCurrentIndex(0)
            self.current_task = tasks[0]
        else:
            self.current_task = None

    def show_screenshot(self, qimage):
        self.append_log("加载截图中... ...")
        from PySide6.QtGui import QPixmap
        pixmap = QPixmap.fromImage(qimage)
        self._screenshot_viewer = ScreenshotViewer(image=pixmap, account=self.account)
        self._screenshot_viewer.show()
        self.append_log("截图已显示")

    def update_mouse_position(self, *, x: int, y: int):
        self.mouse_pos_label.setText(f"鼠标坐标：({x}, {y})")

    def _update_page_labels(self):
        if not self._full_url:
            self.url_label.setText("URL：-")
            self.url_label.setToolTip("")
            return

        metrics = QFontMetrics(self.url_label.font())

        available_width = self.url_label.width() - 50
        if available_width <= 0:
            available_width = 100

        elided = metrics.elidedText(
            self._full_url,
            Qt.ElideRight,
            available_width
        )

        self.url_label.setText(f"URL：{elided}")
        self.url_label.setToolTip(self._full_url)

        self.title_label.setText(f"标题：{self._full_title}")
        self.title_label.setToolTip(self._full_title)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_page_labels()
