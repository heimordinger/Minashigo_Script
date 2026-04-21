from PySide6.QtCore import Qt, QFile, QIODevice
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTabWidget,
    QLabel, QStatusBar, QPushButton, QTabBar, QSystemTrayIcon
)
from PySide6.QtGui import QIcon, QFont, QImage, QShortcut, QKeySequence
from pathlib import Path

from core.config.config import config
from core.logging.events import LogEvent
from core.state.events import StateEvent, StateDomain
from core.state.unified_event import UnifiedEvent
from gui.facade_impl import FacadeImpl
from gui.panels.settings_panel import SettingsPanel
from gui.tabs.StartTab import StartTab
from gui.tabs.AccountManagerTab import AccountManagerTab
from gui.panels.account_panel import AccountPanel
from core.path import ICON_PATH, PROJECT_ROOT


class MainWindow(QWidget):

    def __init__(self, facadeImpl: FacadeImpl, loop):
        super().__init__()
        self.account_panels: dict[str, AccountPanel] = {}
        self.pending_logs: dict[str, list[str]] = {}
        self.loop = loop
        self.setup_window()
        self.load_stylesheet()
        self.facade = facadeImpl
        self.facade.subscribe(self.render)
        self.facade.controller.log_signal.connect(
            self.on_log_signal,
            type=Qt.QueuedConnection
        )
        self.facade.controller.state_event.connect(
            self.on_state_event,
            Qt.QueuedConnection
        )
        self.facade.controller.screenshot_ready.connect(
            self.on_screenshot_ready
        )
        self.setup_tabs()
        self.setup_status_bar()
        self.setup_layout()
        self.setup_signals()
        self.setup_shortcuts()

    def setup_window(self):
        font = QFont()
        font.setPointSize(13)
        self.setFont(font)

        self.setWindowTitle(f"Minashigo_Script-{config.project_version}")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(900, 600)
        self.setObjectName("MainWindow")

    def load_stylesheet(self):
        style_path = PROJECT_ROOT / "gui" / "styles" / "main.qss"

        style_file = QFile(str(style_path))
        if style_file.exists() and style_file.open(QIODevice.ReadOnly | QIODevice.Text):
            stylesheet = style_file.readAll().data().decode('utf-8')
            self.setStyleSheet(stylesheet)
            style_file.close()
            print(f"样式表加载成功: {style_path}")
        else:
            print(f"警告: 样式表文件不存在 - {style_path}")

    def setup_tabs(self):
        self.tabs = QTabWidget()
        self.tabs.setMovable(False)
        self.tabs.setTabsClosable(False)
        self.tabs.setObjectName("MainTabs")

        self.start_tab = StartTab(self.facade)
        self.account_manager_tab = AccountManagerTab(self.facade)
        self.settings_tab = SettingsPanel()

        self.tabs.addTab(self.start_tab, "开始")
        self.tabs.addTab(self.account_manager_tab, "账号管理")
        self.tabs.addTab(self.settings_tab, "设置")

        self.account_manager_tab.accounts_changed.connect(self.start_tab.render)

    def setup_status_bar(self):
        """底部状态栏"""
        self.status_bar = QStatusBar()
        self.status_bar.setObjectName("MainStatusBar")

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusLabel")
        self.status_bar.addWidget(self.status_label)

    def setup_layout(self):
        """主窗口布局"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self.tabs)
        layout.addWidget(self.status_bar)

    def setup_signals(self):
        """UI → 后端信号绑定"""
        self.start_tab.account_submitted.connect(self.on_account_added)
        self.tray = QSystemTrayIcon(QIcon(str(ICON_PATH)), self)
        self.tray.setToolTip("Minashigo 自动化")
        self.tray.show()

    def render(self, snapshots: dict):
        """Facade 主渲染入口"""
        if snapshots:
            any_snapshot = next(iter(snapshots.values()))
            self.status_label.setText(any_snapshot.message or "")
        else:
            self.status_label.setText("Ready")

        for name, snap in snapshots.items():
            panel = self.account_panels.get(name)
            if not panel:
                continue
            if snap.message:
                panel.append_log(snap.message)

    def on_log_signal(self, account: str, event: LogEvent):
        panel = self.account_panels.get(account)
        if panel:
            panel.append_event(event)
        else:
            self.pending_logs.setdefault(account, []).append(event)

    def on_state_event(self, event):
        if not isinstance(event, UnifiedEvent):
            return

        if event.type == "task":
            self._handle_task_snapshot(event.payload)
        elif event.type == "runtime":
            self._handle_runtime_event(event.payload)

    def _handle_task_snapshot(self, snapshot):
        panel = self.account_panels.get(snapshot.browser)
        if not panel:
            return

        if snapshot.message:
            self.status_label.setText(snapshot.message)

        panel.apply_task_snapshot(snapshot)

    def _handle_runtime_event(self, payload: StateEvent):
        if payload.domain != StateDomain.BROWSER:
            return

        panel = self.account_panels.get(payload.account)
        if panel:
            panel.apply_state_event(payload)

    def on_account_added(self, account: dict):
        name = account["name"]
        if name in self.account_panels:
            self.tabs.setCurrentWidget(self.account_panels[name])
            return
        tasks = self.facade.scan_process_tasks()
        panel = AccountPanel(account, tasks)
        self.account_panels[name] = panel
        panel.start_task.connect(self.facade.start_task)
        panel.stop_task.connect(self.facade.stop_task)
        panel.start_browser.connect(self.facade.start_browser)
        panel.close.connect(self.facade.close_browser)
        panel.browser_ready_notify.connect(self.on_browser_ready)
        panel.refresh_tasks.connect(self.on_refresh_tasks)
        panel.request_screenshot.connect(self.on_request_screenshot)
        panel.reconnect.connect(self.on_reconnect)
        insert_index = self.tabs.count() - 2
        self.tabs.insertTab(insert_index, panel, name)
        self.tabs.setCurrentWidget(panel)
        self.add_tab_close_button(account, insert_index)

        print("已添加账号:", name)

    def on_reconnect(self, account: dict):
        name = account["name"]
        print(f"请求重连: {name}")
        self.facade.reconnect_browser(account)

    def add_tab_close_button(self, account: dict, index: int):
        """为账号 Tab 添加关闭按钮"""
        close_btn = QPushButton("✕")
        close_btn.setObjectName("TabCloseButton")
        close_btn.setFixedSize(16, 16)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setProperty("accountTab", True)

        close_btn.clicked.connect(
            lambda _, a=account: self._close_account_tab(a)
        )

        self.tabs.tabBar().setTabButton(
            index,
            QTabBar.ButtonPosition.RightSide,
            close_btn
        )

    def _close_account_tab(self, account: dict):
        name = account['name']
        panel = self.account_panels.pop(name, None)
        if not panel:
            return

        index = self.tabs.indexOf(panel)
        if index != -1:
            self.tabs.removeTab(index)

        self.facade.close_browser(account)
        print("已关闭账号页:", name)

    def on_browser_ready(self, account_name: str):
        print(f"{account_name}就绪")

        from PySide6.QtCore import QTimer
        QTimer.singleShot(2000, lambda: self.tray.showMessage(
            "浏览器已就绪",
            f"账号「{account_name}」浏览器连接完成",
            QSystemTrayIcon.MessageIcon.Information,
            8000
        ))

    def on_refresh_tasks(self, account: dict):
        tasks = self.facade.scan_process_tasks()
        panel = self.account_panels.get(account["name"])
        if panel:
            panel.update_tasks(tasks)

    def on_request_screenshot(self, account: dict):
        self.facade.controller.capture_screenshot(account["name"])

    def on_screenshot_ready(self, account_name: str, frame):
        panel = self.account_panels.get(account_name)
        if not panel:
            return

        def numpy_to_qimage(frame):
            h, w, ch = frame.shape
            bytes_per_line = ch * w
            return QImage(
                frame.data, w, h, bytes_per_line, QImage.Format_BGR888
            ).copy()

        panel.show_screenshot(numpy_to_qimage(frame))

    def setup_shortcuts(self):
        shortcut_f5 = QShortcut(QKeySequence("F5"), self)
        shortcut_f5.activated.connect(self.reload_all)
        print("样式重载快捷键已设置: F5")

    def reload_stylesheet(self):
        print("正在重载样式表...")
        self.load_stylesheet()

        for widget in self.findChildren(QWidget):
            widget.setStyleSheet("")
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

        self.settings_tab.reload_all()

        print("样式表重载完成")

    def closeEvent(self, event):
        try:
            self.facade.controller.state_event.disconnect()
            self.facade.controller.log_signal.disconnect()
        except Exception:
            pass

        self.facade.shutdown()

        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        else:
            print("loop already stopped or not initialized")

        event.accept()

    def reload_all(self):
        print("正在热重载（配置 + 样式）...")

        from core.config.config import config
        config.load()

        self.reload_stylesheet()

        if hasattr(self, "settings_tab"):
            self.settings_tab.reload_all()

        print("热重载完成")
