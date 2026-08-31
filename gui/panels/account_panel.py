import json
from pathlib import Path
from PySide6.QtCore import Signal, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox,
    QScrollArea, QFrame, QDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QSizePolicy,
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


class _QueueChip(QWidget):
    """排队脚本标签：短名 + 关闭按钮。"""

    removed = Signal(str)

    def __init__(self, script_path: str, parent=None):
        super().__init__(parent)
        self.script_path = script_path
        self.setObjectName("QueueChip")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 2, 4, 2)
        lay.setSpacing(4)

        short = Path(script_path).stem
        label = QLabel(short)
        label.setObjectName("QueueChipLabel")
        label.setToolTip(script_path)
        lay.addWidget(label)

        close_btn = QPushButton("×")
        close_btn.setObjectName("QueueChipClose")
        close_btn.setFixedSize(18, 18)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("从排队中移除")
        close_btn.clicked.connect(lambda: self.removed.emit(self.script_path))
        lay.addWidget(close_btn)


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
    switch_account = Signal(str)  # 在当前面板切换账号身份（不新建 Tab）

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
        self._script_queue: list[str] = []  # 最多 2 个排队脚本
        self._active_task: str | None = None  # 当前真正在跑的脚本
        self._queue_max = 2

        self._full_url = ""
        self._full_title = ""

        # 账号切换：在当前面板换账号，不新建 Tab
        title_row = QWidget()
        title_row.setObjectName("AccountHeader")
        title_lay = QHBoxLayout(title_row)
        title_lay.setContentsMargins(0, 0, 0, 0)
        title_lay.setSpacing(8)
        title_prefix = QLabel("账号：")
        title_prefix.setObjectName("AccountTitle")
        self._account_switch = QComboBox()
        self._account_switch.setObjectName("AccountSwitchCombo")
        self._account_switch.setMinimumWidth(180)
        self._account_switch.setToolTip("切换当前工作台绑定的账号（不新开面板）")
        self._account_switch.addItem(account["name"], account["name"])
        self._account_switch.currentIndexChanged.connect(self._on_account_switch)
        title_lay.addWidget(title_prefix)
        title_lay.addWidget(self._account_switch, 0)
        title_lay.addStretch()
        self._title_row = title_row

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

        # 排队标签区（红框位置：标题/URL 右侧）
        self._queue_bar = QWidget()
        self._queue_bar.setObjectName("ScriptQueueBar")
        self._queue_layout = QHBoxLayout(self._queue_bar)
        self._queue_layout.setContentsMargins(0, 0, 0, 0)
        self._queue_layout.setSpacing(6)
        self._queue_layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        queue_hint = QLabel("排队")
        queue_hint.setObjectName("MutedLabel")
        queue_hint.setToolTip("运行中可选其他脚本点「预运行」加入排队（最多 2 个）")
        self._queue_hint = queue_hint
        self._queue_layout.addWidget(queue_hint)
        self._queue_chips_host = QWidget()
        self._queue_chips_layout = QHBoxLayout(self._queue_chips_host)
        self._queue_chips_layout.setContentsMargins(0, 0, 0, 0)
        self._queue_chips_layout.setSpacing(6)
        self._queue_layout.addWidget(self._queue_chips_host)
        self._queue_hint.setVisible(False)
        self._queue_bar.setVisible(False)

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
        self.refresh_btn.setToolTip(
            "重新扫描 scripts/ 目录（不含 _trial 试运行临时脚本）"
        )
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
        self.start_btn.setObjectName("PrimaryButton")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("DangerButton")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        self.reconnect_btn = QPushButton("重新连接")
        self.reconnect_btn.setObjectName("SecondaryButton")
        self.reconnect_btn.setToolTip("中断当前连接并重新连接浏览器")
        self.start_browser_btn = QPushButton("启动浏览器")
        self.start_browser_btn.setObjectName("SecondaryButton")
        self.select_window_btn = QPushButton("选择窗口")
        self.select_window_btn.setObjectName("SecondaryButton")
        self.select_window_btn.setToolTip("选择已启动的桌面窗口作为自动化目标")
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("GhostButton")
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
        divider.setObjectName("Hairline")
        divider.setFrameShape(QFrame.VLine)
        divider.setFixedWidth(1)

        op_row = QHBoxLayout()
        op_row.addWidget(self.start_btn)
        op_row.addWidget(self.stop_btn)
        op_row.addSpacing(4)

        # 脚本目标选择
        target_label = QLabel("目标:")
        target_label.setObjectName("MutedLabel")
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
        self.taskflow_btn.setObjectName("SecondaryButton")
        self.taskflow_btn.setToolTip("TaskFlow服务器启动中…")
        self.taskflow_btn.setEnabled(False)
        self.taskflow_btn.clicked.connect(self.on_taskflow_clicked)
        op_row.addWidget(self.taskflow_btn)

        self.quick_script_btn = QPushButton("快速脚本")
        self.quick_script_btn.setObjectName("SecondaryButton")
        self.quick_script_btn.setToolTip("启动快速脚本录制悬浮窗")
        self.quick_script_btn.clicked.connect(self.on_quick_script)
        op_row.addWidget(self.quick_script_btn)

        op_row.addStretch()

        info_row = QHBoxLayout()
        info_left = QVBoxLayout()
        info_left.setSpacing(2)
        info_left.addWidget(self.title_label)
        info_left.addWidget(self.url_label)
        info_row.addLayout(info_left, 1)
        info_row.addWidget(self._queue_bar, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        debug_row = QHBoxLayout()

        self.mouse_pos_label = QLabel("鼠标坐标：(-, -)")
        self.mouse_pos_label.setObjectName("MousePosLabel")

        self.screenshot_btn = QPushButton("获取截图")
        self.screenshot_btn.setObjectName("SecondaryButton")
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
        layout.addWidget(self._title_row)
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

        self.append_log(f"{account['name']} 已添加")
        self._card_scroll.verticalScrollBar().setValue(0)

    def set_account_list(self, names: list[str]):
        """刷新账号下拉（全部账号；由 MainWindow 在开闭 Tab / 账号变更时调用）。"""
        current = self.account.get("name", "")
        self._account_switch.blockSignals(True)
        self._account_switch.clear()
        ordered = list(names) if names else [current]
        if current and current not in ordered:
            ordered = [current] + ordered
        for name in ordered:
            self._account_switch.addItem(name, name)
        idx = self._account_switch.findData(current)
        self._account_switch.setCurrentIndex(idx if idx >= 0 else 0)
        # 有其它账号即可切换
        self._account_switch.setEnabled(self._account_switch.count() > 1)
        self._account_switch.blockSignals(False)

    def rebind_account(self, account: dict):
        """把当前面板改绑到另一个账号，并清空旧浏览器/窗口运行时 UI。"""
        old = self.account.get("name", "")
        self.account = dict(account)
        self._browser_started = False
        self.browser_ready = False
        self.running = False
        self.stopping = False
        self._active_task = None
        self._script_queue.clear()
        self._rebuild_queue_chips()
        self._full_url = ""
        self._full_title = ""
        self.url_label.setText("URL：-")
        self.title_label.setText("标题：-")
        self.set_browser_state("待启动")
        self._update_target_combo()
        self._update_buttons()
        self.append_log(f"已切换账号：{old} → {self.account.get('name', '')}")
        self._refresh_account_switch_selection()

    def _refresh_account_switch_selection(self):
        current = self.account.get("name", "")
        self._account_switch.blockSignals(True)
        idx = self._account_switch.findData(current)
        if idx >= 0:
            self._account_switch.setCurrentIndex(idx)
        self._account_switch.blockSignals(False)

    def adopt_inherited_browser(self, from_panel: "AccountPanel | None" = None):
        """承接其它账号移交来的浏览器，更新本面板 UI 为已就绪。"""
        src = from_panel.account.get("name", "?") if from_panel else "?"
        self._browser_started = True
        self.account["_target"] = "browser"
        self.append_log(f"已继承浏览器（来自 {src}）")
        if from_panel is not None:
            url_text = from_panel.url_label.text()
            title_text = from_panel.title_label.text()
            if url_text and url_text != "URL：-":
                self.url_label.setText(url_text)
                self._full_url = getattr(from_panel, "_full_url", "") or ""
            if title_text and title_text != "标题：-":
                self.title_label.setText(title_text)
                self._full_title = getattr(from_panel, "_full_title", "") or ""
        self.set_browser_ready(True)
        self._update_target_combo()
        br_idx = self._target_combo.findData("browser")
        if br_idx >= 0:
            self._target_combo.setCurrentIndex(br_idx)
        self._update_buttons()

    def release_inherited_browser(self):
        """浏览器已移交给其它账号面板后，清空本面板的浏览器就绪态。"""
        self._browser_started = False
        self.browser_ready = False
        self.account.pop("_target", None)
        self.set_browser_state("浏览器已移交")
        self.append_log("浏览器已移交到其它账号")
        self._update_target_combo()
        self._update_buttons()

    def _on_account_switch(self, index: int):
        if index < 0:
            return
        name = self._account_switch.itemData(index)
        if not name or name == self.account.get("name"):
            return
        self.switch_account.emit(str(name))

    def _mark_pending_cards_stopped(self):
        """把所有还在"运行中"的旧卡片标为已停止"""
        for i in range(self._card_layout.count()):
            item = self._card_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if isinstance(w, TaskBallCard) and w.status == "运行中":
                    w.status = "已停止"

    def _selected_target(self) -> str:
        """当前下拉框选中的控制目标：browser / window / ''。"""
        return self._target_combo.currentData() or ""

    @staticmethod
    def _target_label(target_type: str | None) -> str:
        if target_type == "window":
            return "窗口"
        if target_type == "browser":
            return "浏览器"
        return "未指定"

    def on_start_browser(self):
        # 创建浏览器启动任务卡，捕获启动过程日志
        self._ball_counter += 1
        card = TaskBallCard("启动浏览器", index=self._ball_counter, target_type="browser")
        self._card_for_layout(card)
        self._current_card = card

        card.add_event(LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message="正在启动浏览器...",
            source=LogSource.SYSTEM,
        ))

        self._browser_started = True
        # 必须清掉旧会话的 ready，否则「预运行」判定进不去
        self.set_browser_ready(False)
        self.start_browser.emit(self.account)
        self.start_browser_btn.setEnabled(False)
        self._update_target_combo()
        # 启动浏览器后默认使用浏览器目标
        br_idx = self._target_combo.findData("browser")
        if br_idx >= 0:
            self._target_combo.setCurrentIndex(br_idx)
        self._update_buttons()

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

            self._ball_counter += 1
            card = TaskBallCard(
                f"绑定窗口: {target.title[:24]}",
                index=self._ball_counter,
                target_type="window",
            )
            self._card_for_layout(card)
            self._current_card = card
            card.add_event(LogEvent(
                account=self.account['name'],
                level=LogLevel.INFO,
                message=f"已选择控制窗口：{target.title}",
                source=LogSource.SYSTEM,
            ))
            card.status = "已完成"
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
        self._update_buttons()

    def _is_browser_starting(self) -> bool:
        """浏览器正在启动 / 重连，尚未就绪。"""
        has_window = self.account.get("window_hwnd") is not None
        return bool(self._browser_started and not self.browser_ready and not has_window)

    def _is_busy_for_queue(self) -> bool:
        """脚本执行中，或浏览器启动中 —— 均可预排队。"""
        return bool(self.running or self._is_browser_starting())

    def _is_pre_run_mode(self) -> bool:
        """脚本运行中或浏览器启动中 → 预运行（可排队）。"""
        if self.stopping:
            return False
        if self.running:
            return True
        if self._is_browser_starting() and self.current_task:
            return True
        return False

    def _can_enqueue(self, task: str | None = None) -> bool:
        task = task or self.current_task
        if not task or self.stopping:
            return False
        if not self._is_busy_for_queue():
            return False
        if self.running and task == self._active_task:
            return False
        if task in self._script_queue:
            return False
        return len(self._script_queue) < self._queue_max

    def _can_click_pre_run(self) -> bool:
        """运行中即使选中的是当前脚本，也可点预运行去选下一个。"""
        if self.stopping:
            return False
        if len(self._script_queue) >= self._queue_max:
            return False
        if self._can_enqueue():
            return True
        # 运行中且队列未满：允许点开选其它脚本
        return bool(self.running and self.current_task)

    def _enqueue_current_selection(self):
        task = self.current_task
        if not task:
            return
        if len(self._script_queue) >= self._queue_max:
            self.append_log(f"排队已满（最多 {self._queue_max} 个），请先删除后再添加")
            return
        if self.running and task == self._active_task:
            # 与正在跑的相同 → 弹出选择器换一个再入队
            self._pick_and_enqueue()
            return
        if task in self._script_queue:
            self.append_log(f"已在排队中：{Path(task).stem}")
            return
        if not self._is_busy_for_queue():
            self.append_log("当前空闲，请直接点「开始」")
            return
        self._script_queue.append(task)
        self._rebuild_queue_chips()
        self.append_log(f"已加入排队：{Path(task).stem}")
        self._update_buttons()

    def _pick_and_enqueue(self):
        """运行中选择其它脚本并加入排队。"""
        paths = self._rescan_task_paths()
        if not paths:
            return
        if len(self._script_queue) >= self._queue_max:
            self.append_log(f"排队已满（最多 {self._queue_max} 个）")
            return
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="files", paths=paths)
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        path = dlg.selected_path
        self.current_task = path
        self._script_btn.setText(path)
        if self.running and path == self._active_task:
            self.append_log("请选择与当前运行中不同的脚本加入排队")
            self._update_buttons()
            return
        if path in self._script_queue:
            self.append_log(f"已在排队中：{Path(path).stem}")
            self._update_buttons()
            return
        self._script_queue.append(path)
        self._rebuild_queue_chips()
        self.append_log(f"已加入排队：{Path(path).stem}")
        self._update_buttons()

    def _remove_from_queue(self, script_path: str):
        if script_path in self._script_queue:
            self._script_queue.remove(script_path)
            self._rebuild_queue_chips()
            self.append_log(f"已取消排队：{Path(script_path).stem}")
            self._update_buttons()

    def _rebuild_queue_chips(self):
        while self._queue_chips_layout.count():
            item = self._queue_chips_layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        for path in self._script_queue:
            chip = _QueueChip(path, self._queue_chips_host)
            chip.removed.connect(self._remove_from_queue)
            self._queue_chips_layout.addWidget(chip)
        has_queue = bool(self._script_queue)
        self._queue_hint.setVisible(has_queue)
        self._queue_bar.setVisible(has_queue)

    def _try_start_next_queued(self):
        """当前脚本正常结束后自动启动排队中的下一个。"""
        if self.running or self.stopping:
            return
        if not self._script_queue:
            return
        next_task = self._script_queue.pop(0)
        self._rebuild_queue_chips()
        self.current_task = next_task
        self._script_btn.setText(next_task)
        self.append_log(f"开始排队任务：{Path(next_task).stem}")
        self.on_start_clicked()

    def on_start_clicked(self):
        if self._is_pre_run_mode():
            self._enqueue_current_selection()
            return

        if not self.current_task or self.running:
            return

        # 记录选中的脚本目标（browser / window），供 TaskController 使用
        target_type = self._selected_target() or "browser"
        print(f"[AccountPanel] currentData={target_type!r}, 下拉框文本="
              f"{self._target_combo.currentText()!r}")
        self.account["_target"] = target_type
        print(f"[AccountPanel] _target={self.account['_target']!r}")

        self._active_task = self.current_task
        self.running = True
        self._update_buttons()
        target_label = self._target_label(target_type)
        self.set_browser_state(f'正在运行 "{self.current_task}"（{target_label}）')

        # 创建新任务卡（插到最前）
        self._ball_counter += 1
        card = TaskBallCard(
            self.current_task,
            index=self._ball_counter,
            target_type=target_type,
        )
        self._card_for_layout(card)
        self._current_card = card

        # 记录启动日志
        ev = LogEvent(
            account=self.account['name'],
            level=LogLevel.INFO,
            message=f"开始执行：{self.current_task}（目标：{target_label}）",
            source=LogSource.SYSTEM,
        )
        card.add_event(ev)

        # 限定卡片数量，移除最旧的
        while self._card_layout.count() > 50:
            item = self._card_layout.takeAt(self._card_layout.count() - 1)
            if item and item.widget():
                item.widget().deleteLater()
        self._resize_content()

        self.start_task.emit(self.account, self.current_task)

    def on_reconnect_clicked(self):
        # 把之前还在"运行中"的旧卡片标为已停止
        self._mark_pending_cards_stopped()

        self._ball_counter += 1
        card = TaskBallCard("重新连接", index=self._ball_counter, target_type="browser")
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
        card = TaskBallCard("关闭", index=self._ball_counter, target_type="browser")
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

    _quick_script_instance = None

    def on_quick_script(self):
        from gui.widgets.QuickScriptOverlay import QuickScriptOverlay
        if AccountPanel._quick_script_instance is None:
            AccountPanel._quick_script_instance = QuickScriptOverlay()
        AccountPanel._quick_script_instance.show()
        AccountPanel._quick_script_instance.raise_()
        AccountPanel._quick_script_instance.activateWindow()
        self.append_log("快速脚本悬浮窗已打开")

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
            # 启动过程中仍允许再点启动（若尚未点过则可用）
            self.start_browser_btn.setEnabled(not self._browser_started)
            # 浏览器启动中：开放「预运行」以便提前排队
            if self._is_pre_run_mode():
                self.start_btn.setText("预运行")
                self.start_btn.setEnabled(self._can_click_pre_run())
                self.start_btn.setToolTip(
                    "浏览器启动中，可将脚本加入排队；就绪后自动开跑"
                    if self._can_enqueue()
                    else "无法加入排队：已满或重复"
                )
            else:
                self.start_btn.setText("开始")
                self.start_btn.setEnabled(False)
                self.start_btn.setToolTip("请等待浏览器就绪，或先点「预运行」排队")
        else:
            for w in self._all_buttons:
                w.setEnabled(True)

            # 窗口模式：没有浏览器可重连，但允许再启动浏览器
            if has_window and not self.browser_ready:
                self.reconnect_btn.setEnabled(False)

            if self.stopping:
                self.start_btn.setText("开始")
                self.start_btn.setEnabled(False)
                self.stop_btn.setEnabled(False)
            elif self._is_pre_run_mode():
                self.start_btn.setText("预运行")
                self.start_btn.setEnabled(self._can_click_pre_run())
                if self._can_enqueue():
                    tip = "将当前所选脚本加入排队（最多 2 个）；当前任务结束后自动开跑"
                elif self.running and self.current_task == self._active_task:
                    tip = "点此选择其它脚本加入排队"
                else:
                    tip = "无法加入排队：已满或重复"
                self.start_btn.setToolTip(tip)
                self.stop_btn.setEnabled(self.running)
            else:
                self.start_btn.setText("开始")
                self.start_btn.setToolTip("开始执行所选脚本")
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

    def refresh_log_themes(self):
        """主题切换后重绘所有任务球日志颜色。"""
        for i in range(self._card_layout.count()):
            item = self._card_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, TaskBallCard):
                w.refresh_log_theme()

    def _scroll_to_top(self):
        self._card_scroll.verticalScrollBar().setValue(0)

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
            self._active_task = None
        self._update_buttons()
        self.status_label.setText("状态：运行中" if running else "状态：已停止")

    def set_browser_ready(self, ready: bool):
        changed = self.browser_ready != ready
        if not changed and not ready:
            return

        self.browser_ready = ready
        self._update_buttons()

        if ready:
            if changed:
                self.set_browser_state("浏览器就绪")
                if self._current_card is not None:
                    self._current_card.status = "已完成"
                self.browser_ready_notify.emit(self.account["name"])
            # 首次或补发 ready：若有预排队则尝试开跑（UserBrowser 入库后的二次 ready 也会走到这）
            if self._script_queue and not self.running and not self.stopping:
                QTimer.singleShot(50, self._try_start_next_queued)
        elif changed:
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
        was_running = self.running
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

        # 正常结束后自动开跑排队脚本；用户停止则保留排队不自动开
        if was_running and not running and status == "finished":
            QTimer.singleShot(0, self._try_start_next_queued)

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

    def _rescan_task_paths(self) -> list[str]:
        """打开选择器前重新扫描 scripts/（避免列表陈旧）。"""
        win = self.window()
        facade = getattr(win, "facade", None)
        if facade is not None and hasattr(facade, "scan_process_tasks"):
            tasks = facade.scan_process_tasks()
            self.update_tasks(tasks)
            return tasks
        return self.task_paths

    def _open_script_picker(self):
        paths = self._rescan_task_paths()
        if not paths:
            return
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="files", paths=paths)
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
