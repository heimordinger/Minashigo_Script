import json
from pathlib import Path
from PySide6.QtCore import Signal, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox,
    QScrollArea, QFrame, QDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
)

from core.path import SCRIPTS_PATH

from core.logging.events import LogLevel, LogEvent, LogSource
from core.state.events import StateEvent, StateDomain

from gui.panels.screenshot_viewer import ScreenshotViewer
from gui.panels.task_ball import TaskBallCard
from core.taskflow_manager import taskflow_manager


class _CardScrollArea(QScrollArea):
    """QScrollArea whose min size is NOT driven by content widgets inside."""
    def minimumSizeHint(self):
        return QSize(200, 100)


class ScriptPickerDialog(QDialog):
    """脚本选择对话框，带文件夹树折叠效果"""
    def __init__(self, paths: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择脚本")
        self.setMinimumSize(420, 480)
        self.selected_path: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        layout.addWidget(self.tree)

        # 构建树
        root = {"children": {}, "files": []}
        for p in paths:
            parts = p.split("/")
            node = root
            for i, part in enumerate(parts[:-1]):
                if part not in node["children"]:
                    node["children"][part] = {"children": {}, "files": []}
                node = node["children"][part]
            node["files"].append(p)

        def add_items(parent_widget, node):
            for name, sub in sorted(node["children"].items()):
                item = QTreeWidgetItem(parent_widget)
                item.setText(0, f"📁 {name}")
                item.setExpanded(False)
                add_items(item, sub)
            for fp in sorted(node["files"]):
                item = QTreeWidgetItem(parent_widget)
                item.setText(0, f"📄 {fp.split('/')[-1]}")
                item.setData(0, Qt.UserRole, fp)

        add_items(self.tree.invisibleRootItem(), root)

        btn_row = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_ok.clicked.connect(self._accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        self.tree.itemDoubleClicked.connect(self._accept)

    def _accept(self):
        item = self.tree.currentItem()
        if item:
            fp = item.data(0, Qt.UserRole)
            if fp:
                self.selected_path = fp
                self.accept()


class AccountPanel(QWidget):
    start_task = Signal(dict, str)
    stop_task = Signal(dict)
    browser_ready_notify = Signal(str)

    reconnect = Signal(dict)
    start_browser = Signal(dict)
    select_window = Signal(dict)
    close = Signal(dict)

    open_taskflow = Signal(dict)

    refresh_tasks = Signal(dict)
    request_screenshot = Signal(dict)
    target_changed = Signal(dict)

    def __init__(self, account: dict, tasks: list[str]):
        super().__init__()
        self.setObjectName("AccountPanel")
        self.account = account
        self.tasks = tasks

        self.current_task: str | None = None
        self.running = False
        self.stopping = False
        self.browser_ready = False
        self._browser_started = False   # 是否启动过浏览器（独立于连接状态）
        self._ball_counter = 0
        self._current_card: TaskBallCard | None = None

        self._full_url = ""
        self._full_title = ""
        title = QLabel(f"账号：{account['name']}")
        title.setObjectName("AccountTitle")
        self.task_paths = tasks
        self._script_btn = QPushButton(tasks[0] if tasks else "（无脚本）")
        self._script_btn.setObjectName("ScriptSelectCombo")
        self._script_btn.setMinimumHeight(28)
        self._script_btn.clicked.connect(self._open_script_picker)
        if tasks:
            self.current_task = tasks[0]
        else:
            self.current_task = None

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
        script_select_layout.addWidget(self._script_btn)
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
        self.select_window_btn = QPushButton("选择窗口")
        self.select_window_btn.setToolTip("选择已启动的桌面窗口作为自动化目标")
        self.close_btn = QPushButton("关闭")
        self.reconnect_btn.setEnabled(True)
        self.start_browser_btn.setEnabled(True)
        self.select_window_btn.setEnabled(True)
        self.close_btn.setEnabled(False)

        self.start_btn.clicked.connect(self.on_start_clicked)
        self.stop_btn.clicked.connect(self.on_stop_clicked)

        self.reconnect_btn.clicked.connect(self.on_reconnect_clicked)
        self.start_browser_btn.clicked.connect(self.on_start_browser)
        self.select_window_btn.clicked.connect(self.on_select_window)
        self.close_btn.clicked.connect(self.on_close_clicked)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setStyleSheet("color: #3f3f3f;")

        op_row = QHBoxLayout()
        op_row.addWidget(self.start_btn)
        op_row.addWidget(self.stop_btn)
        op_row.addSpacing(4)

        # 脚本目标选择
        target_label = QLabel("目标:")
        target_label.setStyleSheet("color: #888;")
        op_row.addWidget(target_label)
        self._target_combo = QComboBox()
        self._target_combo.setMinimumWidth(120)
        self._target_combo.setToolTip("选择脚本执行时使用的控制目标")
        self._target_combo.addItem("未就绪")
        self._target_combo.currentIndexChanged.connect(self._on_target_changed)
        op_row.addWidget(self._target_combo)

        op_row.addSpacing(8)
        op_row.addWidget(divider)
        op_row.addSpacing(8)
        op_row.addWidget(self.reconnect_btn)
        op_row.addWidget(self.start_browser_btn)
        op_row.addWidget(self.select_window_btn)
        op_row.addWidget(self.close_btn)

        op_row.addSpacing(16)
        self.taskflow_btn = QPushButton("TaskFlow")
        self.taskflow_btn.setToolTip("TaskFlow服务器启动中…")
        self.taskflow_btn.setEnabled(False)
        self.taskflow_btn.clicked.connect(self.on_taskflow_clicked)
        op_row.addWidget(self.taskflow_btn)

        op_row.addStretch()

        info_row = QVBoxLayout()
        info_row.addWidget(self.title_label)
        info_row.addWidget(self.url_label)

        debug_row = QHBoxLayout()

        self.mouse_pos_label = QLabel("鼠标坐标：(-, -)")
        self.mouse_pos_label.setObjectName("MousePosLabel")

        self.screenshot_btn = QPushButton("获取截图")
        self.screenshot_btn.setObjectName("ScreenshotBtn")
        self.screenshot_btn.clicked.connect(self._request_screenshot)
        self.screenshot_btn.setEnabled(False)

        debug_row.addWidget(self.mouse_pos_label)
        debug_row.addStretch()
        debug_row.addWidget(self.screenshot_btn)

        # ========== 任务卡片列表（可滚动） ==========
        self._card_container = QWidget()
        self._card_container.setObjectName("CardContainer")
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)
        self._card_layout.setAlignment(Qt.AlignTop)

        self._card_scroll = _CardScrollArea()
        self._card_scroll.setObjectName("CardScroll")
        self._card_scroll.setWidgetResizable(False)
        self._card_scroll.setWidget(self._card_container)
        self._card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._card_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(top_row_widget)
        layout.addLayout(info_row)
        layout.addLayout(op_row)
        layout.addWidget(self._card_scroll, stretch=1)
        layout.addLayout(debug_row)

        self._all_buttons = [
            self.start_btn,
            self.stop_btn,
            self.reconnect_btn,
            self.start_browser_btn,
            self.close_btn,
            self.refresh_btn,
            self.screenshot_btn,
            self.taskflow_btn,
        ]

        # 定时检查 TaskFlow 服务器是否就绪
        self._tf_ready_timer = QTimer(self)
        self._tf_ready_timer.timeout.connect(self._check_taskflow_ready)
        self._tf_ready_timer.start(2000)

        self.setStyleSheet("""
QWidget#AccountPanel {
    background-color: transparent;
}

QLabel#AccountTitle {
    font-size: 14px;
    font-weight: bold;
    color: #ffffff;
}

QWidget#ScriptSelect {
    background-color: #323232;
    border: 1px solid #3f3f3f;
    border-radius: 6px;
}

QLabel#ScriptSelectLabel {
    padding: 4px 8px;
    background-color: #2b2b2b;
    border-right: 1px solid #3f3f3f;
    color: #cfcfcf;
}

QComboBox#ScriptSelectCombo {
    border: none;
    background-color: transparent;
    color: #ffffff;
    padding: 4px 6px;
}

QWidget#ScriptSelect:hover,
QWidget#ScriptSelect:focus-within {
    border-color: #4aa3ff;
}

QScrollArea#CardScroll {
    border: none;
    background-color: #1a1a1a;
}

#CardScroll QScrollBar:vertical {
    background: #2a2a2a;
    width: 10px;
    margin: 0;
}

#CardScroll QScrollBar::handle:vertical {
    background: #555555;
    min-height: 30px;
    border-radius: 5px;
}

#CardScroll QScrollBar::handle:vertical:hover {
    background: #777777;
}

#CardScroll QScrollBar::add-line:vertical,
#CardScroll QScrollBar::sub-line:vertical {
    height: 0;
}

#CardScroll QScrollBar::add-page:vertical,
#CardScroll QScrollBar::sub-page:vertical {
    background: none;
}

QWidget#CardContainer {
    background-color: #1a1a1a;
}

/* ===== TaskBallCard 水平分栏 ===== */
QWidget#TaskBallCard {
    background-color: transparent;
    border-bottom: 1px solid #2a2a2a;
}

/* 左侧球列 */
QWidget#BallColumn {
    background-color: #1a1a1a;
    border-right: 1px solid #2a2a2a;
}

/* 右侧内容区 */
QWidget#CardContent {
    background-color: #1e1e1e;
}

/* 头部 */
QWidget#CardHeader {
    background-color: #242424;
    border-bottom: 1px solid #2a2a2a;
}

QWidget#CardHeader:hover {
    background-color: #2c2c2c;
}

QLabel#CardTitle {
    color: #e0e0e0;
    font-weight: bold;
    font-size: 12px;
}

QLabel#CardStatus {
    color: #7bd88f;
    font-size: 11px;
}

QLabel#CardArrow {
    color: #666666;
    font-size: 11px;
}

/* 日志体 */
QWidget#CardBody {
    background-color: #181818;
}

QTextEdit#CardLog {
    background-color: #181818;
    border: none;
    font-family: Consolas;
    font-size: 10pt;
    color: #d4d4d4;
    padding: 2px 8px;
}

#AccountHeader {
    background-color: transparent;
}

#CloseXButton {
    background: transparent;
    color: #aaaaaa;
    border: none;
    font-weight: bold;
    padding: 2px 6px;
}

#CloseXButton:hover {
    color: #ffffff;
    background-color: #e05a5a;
    border-radius: 8px;
}

QPushButton#TabCloseButton[accountTab="true"] {
    margin-left: 4px;
}
""")

        self.append_log(f"{account['name']} 已添加")

    def _scroll_to_top(self):
        self._card_scroll.verticalScrollBar().setValue(0)

    def _mark_pending_cards_stopped(self):
        """把所有还在"运行中"的旧卡片标为已停止"""
        for i in range(self._card_layout.count()):
            item = self._card_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if isinstance(w, TaskBallCard) and w.status == "运行中":
                    w.status = "已停止"

    def on_start_browser(self):
        # 创建浏览器启动任务卡，捕获启动过程日志
        self._ball_counter += 1
        card = TaskBallCard("启动浏览器", index=self._ball_counter)
        self._card_for_layout(card)
        self._current_card = card

        card.add_event(LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message="正在启动浏览器...",
            source=LogSource.SYSTEM,
        ))

        self._browser_started = True
        self.start_browser.emit(self.account)
        self.start_browser_btn.setEnabled(False)
        self._update_target_combo()
        # 启动浏览器后默认使用浏览器目标
        br_idx = self._target_combo.findData("browser")
        if br_idx >= 0:
            self._target_combo.setCurrentIndex(br_idx)

    def on_select_window(self):
        from gui.widgets.WindowPicker import WindowPickerDialog
        target = WindowPickerDialog.pick(self)
        if target:
            self.account["window_hwnd"] = target.hwnd
            self.account["window_title"] = target.title
            self.select_window.emit(self.account)
            self.select_window_btn.setText(f"窗口: {target.title[:20]}")
            self.set_browser_state(f"已选窗口: {target.title[:30]}")
            self._update_target_combo()
            # 选中窗口后默认使用窗口目标，强制切到窗口项
            win_idx = self._target_combo.findData("window")
            if win_idx >= 0:
                self._target_combo.setCurrentIndex(win_idx)
            self._update_buttons()
        elif self.account.get("window_hwnd"):
            # 取消 → 清除之前的选择
            self.account.pop("window_hwnd", None)
            self.account.pop("window_title", None)
            self.select_window_btn.setText("选择窗口")
            self.set_browser_state("空闲")
            self._update_target_combo()
            self._update_buttons()

    def on_task_changed(self, task: str):
        self.current_task = task

    def on_start_clicked(self):
        if not self.current_task or self.running:
            return

        self.running = True
        self._update_buttons()
        self.set_browser_state(f'正在运行 "{self.current_task}"')

        # 创建新任务卡（插到最前）
        self._ball_counter += 1
        card = TaskBallCard(self.current_task, index=self._ball_counter)
        self._card_for_layout(card)
        self._current_card = card

        # 记录启动日志
        ev = LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message=f"开始执行：{self.current_task}",
            source=LogSource.SYSTEM,
        )
        card.add_event(ev)

        # 限定卡片数量，移除最旧的
        while self._card_layout.count() > 50:
            item = self._card_layout.takeAt(self._card_layout.count() - 1)
            if item and item.widget():
                item.widget().deleteLater()
        self._resize_content()

        # 记录选中的脚本目标（browser / window），供 TaskController 使用
        target_type = self._target_combo.currentData()
        print(f"[AccountPanel] currentData={target_type!r}, 下拉框文本="
              f"{self._target_combo.currentText()!r}")
        self.account["_target"] = target_type or "browser"
        print(f"[AccountPanel] _target={self.account['_target']!r}")
        self.start_task.emit(self.account, self.current_task)

    def on_reconnect_clicked(self):
        # 把之前还在"运行中"的旧卡片标为已停止
        self._mark_pending_cards_stopped()

        self._ball_counter += 1
        card = TaskBallCard("重新连接", index=self._ball_counter)
        self._card_for_layout(card)
        self._current_card = card

        card.add_event(LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message="请求重新连接浏览器...",
            source=LogSource.SYSTEM,
        ))

        self.running = False
        self.set_browser_ready(False)
        self._update_buttons()

        self.reconnect.emit(self.account)

    def on_close_clicked(self):
        self._ball_counter += 1
        card = TaskBallCard("关闭", index=self._ball_counter)
        self._card_for_layout(card)
        self._current_card = card

        card.add_event(LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message="正在关闭浏览器...",
            source=LogSource.SYSTEM,
        ))

        self.close.emit(self.account)

    def on_taskflow_clicked(self):
        if not taskflow_manager.server_running:
            self.append_log("TaskFlow服务器尚未就绪，请稍后重试")
            return
        self.append_log("正在打开TaskFlow可视化工作流...")
        self.open_taskflow.emit(self.account)

    def _check_taskflow_ready(self):
        """定时检查 TaskFlow 服务器是否已就绪"""
        if taskflow_manager.server_running:
            self.taskflow_btn.setEnabled(True)
            self.taskflow_btn.setToolTip("打开TaskFlow可视化工作流")
            self._tf_ready_timer.stop()

    def _on_target_changed(self):
        """下拉框切换时同步 _target 并通知外部。"""
        target_type = self._target_combo.currentData()
        self.account["_target"] = target_type or "browser"
        self.target_changed.emit(self.account)

    def _request_screenshot(self):
        """获取截图前把目标选择写入 account，确保 controller 知道截哪个。"""
        target_type = self._target_combo.currentData()
        self.account["_target"] = target_type or "browser"
        self.request_screenshot.emit(self.account)

    def _update_target_combo(self):
        """根据当前绑定的 browser/window 刷新目标选择下拉框。"""
        # 清空前记住当前选中的 target，重填后恢复
        prev_target = self._target_combo.currentData()

        self._target_combo.blockSignals(True)
        self._target_combo.clear()

        has_browser = self._browser_started
        has_window = self.account.get("window_hwnd") is not None

        if has_browser:
            self._target_combo.addItem("浏览器", "browser")
        if has_window:
            win_title = self.account.get("window_title", "窗口")
            self._target_combo.addItem(f"窗口: {win_title[:20]}", "window")

        if self._target_combo.count() == 0:
            self._target_combo.addItem("未就绪", "")

        # 恢复之前的选中项；如果已被移除（如取消窗口）则默认第一项
        if prev_target:
            idx = self._target_combo.findData(prev_target)
            if idx >= 0:
                self._target_combo.setCurrentIndex(idx)
            else:
                self._target_combo.setCurrentIndex(0)
        else:
            self._target_combo.setCurrentIndex(0)

        self._target_combo.blockSignals(False)

    def _update_buttons(self):
        # 先统一将 taskflow 按钮设为不可用（后面单独覆盖）
        self.taskflow_btn.setEnabled(False)

        has_window = self.account.get("window_hwnd") is not None

        if not self.browser_ready and not has_window:
            for w in self._all_buttons:
                w.setEnabled(False)

            self.reconnect_btn.setEnabled(True)
            self.start_browser_btn.setEnabled(True)
        else:
            for w in self._all_buttons:
                w.setEnabled(True)

            # 窗口模式：没有浏览器可重连，但允许再启动浏览器
            if has_window and not self.browser_ready:
                self.reconnect_btn.setEnabled(False)

            if self.stopping:
                self.start_btn.setEnabled(False)
                self.stop_btn.setEnabled(False)
            else:
                self.start_btn.setEnabled(not self.running)
                self.stop_btn.setEnabled(self.running)

        # taskflow 按钮由服务器状态独立决定，不受浏览器状态影响
        self.taskflow_btn.setEnabled(taskflow_manager.server_running)
        self.taskflow_btn.setToolTip(
            "打开TaskFlow可视化工作流" if taskflow_manager.server_running
            else "TaskFlow服务器启动中…"
        )

    def on_stop_clicked(self):
        if not self.running or self.stopping:
            return
        self.stopping = True
        self._update_buttons()
        self.set_browser_state("停止中...")
        self.stop_task.emit(self.account)

    def _resize_content(self):
        """Match container width to viewport, height to total card height."""
        total_h = 0
        spacing = self._card_layout.spacing()
        count = self._card_layout.count()
        for i in range(count):
            item = self._card_layout.itemAt(i)
            if item and item.widget():
                total_h += item.widget().sizeHint().height()
        if count > 1:
            total_h += spacing * (count - 1)
        w = self._card_scroll.viewport().width()
        self._card_container.resize(w, max(total_h, 0))
        self._card_scroll.verticalScrollBar().setValue(0)

    def _card_for_layout(self, card: TaskBallCard):
        """Helper: add a card to layout, connect signal, resize content."""
        self._card_layout.insertWidget(0, card)
        card.cardResized.connect(self._resize_content)
        self._scroll_to_top()
        self._resize_content()

    def append_log(self, text: str):
        """便捷方法：文本→LogEvent→发送到当前卡片"""
        event = LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message=text,
            source="legacy"
        )
        self.append_event(event)

    def append_event(self, event: LogEvent):
        """追加日志到当前任务卡（无折叠，全部保留）"""
        if self._current_card is not None:
            self._current_card.add_event(event)

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

        elif event.domain in (StateDomain.TASK, StateDomain.RUNTIME):
            if event.key == "running":
                self.set_running(bool(event.value))

    def set_running(self, running: bool):
        if self.running == running and not self.stopping:
            return
        self.running = running
        if not running:
            self.stopping = False
        self._update_buttons()
        self.status_label.setText("状态：运行中" if running else "状态：已停止")

    def set_browser_ready(self, ready: bool):
        if self.browser_ready == ready:
            return

        self.browser_ready = ready
        self._update_buttons()

        if ready:
            self.set_browser_state("浏览器就绪")
            if self._current_card is not None:
                self._current_card.status = "已完成"
            self.browser_ready_notify.emit(self.account["name"])
        else:
            self.set_browser_state("浏览器未就绪")

    def apply_task_snapshot(self, snapshot):
        """
        snapshot: TaskSnapshot
        """
        status = getattr(snapshot.status, "value", snapshot.status)
        status = str(status).strip().lower()
        step = str(getattr(snapshot, "step", "") or "").strip().lower()

        terminal_statuses = {"idle", "finished", "stopped", "error"}
        terminal_steps = {"finished", "stopped", "exception", "idle"}
        running = status not in terminal_statuses and step not in terminal_steps
        self.set_running(running)

        # 更新任务球状态
        state_map = {
            "running": "运行中",
            "stopping": "停止中...",
            "stopped": "已停止",
            "finished": "已完成",
            "error": "执行异常",
            "idle": "待命",
        }
        if step in state_map:
            display = state_map[step]
            self.set_browser_state(display)
            if self._current_card is not None:
                self._current_card.status = display
        elif status in state_map:
            display = state_map[status]
            self.set_browser_state(display)
            if self._current_card is not None:
                self._current_card.status = display

        if snapshot.message:
            self.append_log(snapshot.message)

    def update_tasks(self, tasks: list[str]):
        self.task_paths = tasks
        if not tasks:
            self.current_task = None
            self._script_btn.setText("（无脚本）")
            return
        if self.current_task and self.current_task in tasks:
            self._script_btn.setText(self.current_task)
        else:
            self.current_task = tasks[0]
            self._script_btn.setText(tasks[0])

    def _open_script_picker(self):
        if not self.task_paths:
            return
        dlg = ScriptPickerDialog(self.task_paths, self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_path:
            self.current_task = dlg.selected_path
            self._script_btn.setText(dlg.selected_path)
            self.on_task_changed(dlg.selected_path)

    def show_screenshot(self, qimage):
        self.append_log("加载截图中... ...")
        from PySide6.QtGui import QPixmap
        pixmap = QPixmap.fromImage(qimage)

        if hasattr(self, '_screenshot_viewer') and self._screenshot_viewer is not None:
            try:
                self._screenshot_viewer.update_image(pixmap)
                self._screenshot_viewer.raise_()
                self._screenshot_viewer.activateWindow()
                self._screenshot_viewer.show()  # 关闭后重开必需
                self.append_log("截图已更新")
                return
            except (RuntimeError, AttributeError):
                # 窗口已被销毁，重新创建
                pass

        self._screenshot_viewer = ScreenshotViewer(image=pixmap, account=self.account)
        self._screenshot_viewer.refresh_requested.connect(
            self._request_screenshot
        )
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
