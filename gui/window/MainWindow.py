from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTabWidget,
    QLabel, QStatusBar, QPushButton, QTabBar, QSystemTrayIcon,
    QMessageBox, QApplication,
)
from PySide6.QtGui import QIcon, QFont, QImage, QShortcut, QKeySequence

from core.app_info import VERSION as APP_VERSION
from core.config.config import config
from core.logging.events import LogEvent
from core.state.events import StateEvent, StateDomain
from core.state.unified_event import UnifiedEvent
from gui.facade_impl import FacadeImpl
from gui.panels.settings_panel import SettingsPanel
from gui.tabs.StartTab import StartTab
from gui.tabs.AccountManagerTab import AccountManagerTab
from gui.widgets.MatchDebugWindow import MatchDebugWindow
from gui.panels.account_panel import AccountPanel
from core.error_handler import safe_call
from core.path import ICON_PATH


class MainWindow(QWidget):

    def __init__(self, facadeImpl: FacadeImpl, loop):
        super().__init__()
        self.account_panels: dict[str, AccountPanel] = {}
        self.pending_logs: dict[str, list[str]] = {}
        self._prev_task_status: dict[str, str] = {}
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
        self.facade.controller.match_event.connect(
            self.on_match_event,
            type=Qt.QueuedConnection,
        )
        self._match_debug = None
        self._script_gen = None
        self._script_spec = None
        self._setup_start_tab()
        self.setup_status_bar()
        self.setup_layout()
        self.setup_signals()
        self.setup_shortcuts()
        QTimer.singleShot(0, self._lazy_init_tabs)

    def setup_window(self):
        font = QFont()
        font.setPointSize(13)
        self.setFont(font)

        from core.demo_mode import demo_window_title_suffix

        self.setWindowTitle(f"Minashigo_Script-{APP_VERSION}{demo_window_title_suffix()}")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(900, 600)
        self.setObjectName("MainWindow")

    def load_stylesheet(self):
        from gui.styles.theme import current_theme_from_config, load_theme_qss
        theme = current_theme_from_config()
        self.setStyleSheet(load_theme_qss(theme))
        print(f"样式表加载完成: theme={theme}")

    def _setup_start_tab(self):
        """只创建首屏必要的「开始」tab"""
        self.tabs = QTabWidget()
        self.tabs.setMovable(False)
        self.tabs.setTabsClosable(False)
        self.tabs.setObjectName("MainTabs")
        self.start_tab = StartTab(self.facade)
        self.tabs.addTab(self.start_tab, "开始")

    def _lazy_init_tabs(self):
        """事件循环启动后懒加载其余 tab"""
        self.account_manager_tab = safe_call(self, "AccountManagerTab", AccountManagerTab, self.facade)
        if self.account_manager_tab is not None:
            self.tabs.addTab(self.account_manager_tab, "账号管理")

        self.settings_tab = safe_call(self, "SettingsPanel", SettingsPanel)
        if self.settings_tab is not None:
            self.tabs.addTab(self.settings_tab, "设置")
            self.settings_tab.theme_changed.connect(self.on_theme_changed)

        if self.account_manager_tab is not None:
            self.account_manager_tab.accounts_changed.connect(self.start_tab.render)
            self.account_manager_tab.accounts_changed.connect(self._refresh_account_switchers)

    def setup_status_bar(self):
        """底部状态栏"""
        self.status_bar = QStatusBar()
        self.status_bar.setObjectName("MainStatusBar")

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusLabel")
        self.status_bar.addWidget(self.status_label)

        self.match_debug_btn = QPushButton("匹配调试")
        self.match_debug_btn.setObjectName("GhostButton")
        self.match_debug_btn.setToolTip("打开 match/click 调试窗口 (Ctrl+Shift+D)")
        self.match_debug_btn.setFlat(True)
        self.match_debug_btn.clicked.connect(self.open_match_debug)
        self.status_bar.addPermanentWidget(self.match_debug_btn)

        self.spec_editor_btn = QPushButton("脚本IDE")
        self.spec_editor_btn.setObjectName("GhostButton")
        self.spec_editor_btn.setToolTip("打开脚本IDE（编写脚本介绍）")
        self.spec_editor_btn.setFlat(True)
        self.spec_editor_btn.clicked.connect(self.open_script_spec)
        self.status_bar.addPermanentWidget(self.spec_editor_btn)

        self.script_gen_btn = QPushButton("脚本生成")
        self.script_gen_btn.setObjectName("GhostButton")
        self.script_gen_btn.setToolTip("打开脚本生成 / 试运行窗口 (Ctrl+Shift+G)")
        self.script_gen_btn.setFlat(True)
        self.script_gen_btn.clicked.connect(self.open_script_gen)
        self.status_bar.addPermanentWidget(self.script_gen_btn)

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

        terminal = ("finished", "error", "stopped")
        prev = self._prev_task_status.get(snapshot.browser)
        status_val = snapshot.status.value if hasattr(snapshot.status, 'value') else snapshot.status
        if status_val in terminal and prev != status_val:
            icons = {
                "finished": QSystemTrayIcon.MessageIcon.Information,
                "error":    QSystemTrayIcon.MessageIcon.Critical,
                "stopped":  QSystemTrayIcon.MessageIcon.Information,
            }
            msgs = {
                "finished": "执行完成",
                "error":    "执行异常",
                "stopped":  "已停止",
            }
            title = f"「{snapshot.browser}」{msgs.get(status_val, status_val)}"
            body = snapshot.message or snapshot.script or ""
            icon = icons.get(status_val, QSystemTrayIcon.MessageIcon.Information)

            QTimer.singleShot(500, lambda t=title, b=body, i=icon: self.tray.showMessage(t, b, i, 8000))

        self._prev_task_status[snapshot.browser] = status_val

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
        panel.select_window.connect(self.on_select_window)
        panel.close.connect(self._close_account_tab)
        panel.browser_ready_notify.connect(self.on_browser_ready)
        panel.refresh_tasks.connect(self.on_refresh_tasks)
        panel.request_screenshot.connect(self.on_request_screenshot)
        panel.target_changed.connect(self.on_target_changed)
        panel.reconnect.connect(self.on_reconnect)
        panel.open_taskflow.connect(self.on_open_taskflow)
        panel.switch_account.connect(self.on_switch_account_tab)
        insert_index = self.tabs.count() - 2
        self.tabs.insertTab(insert_index, panel, name)
        self.tabs.setCurrentWidget(panel)
        self.add_tab_close_button(account, insert_index)
        self._refresh_account_switchers()

        print("已添加账号:", name)

    def on_switch_account_tab(self, account_name: str):
        """在当前 AccountPanel 上切换账号身份，不新建 Tab。

        若目标账号已有打开的面板，则跳到那一块（避免同一账号两块面板）。
        """
        source_panel = self.sender()
        if not isinstance(source_panel, AccountPanel):
            return

        account = next(
            (a for a in self.facade.list_accounts() if a.get("name") == account_name),
            None,
        )
        if account is None:
            source_panel.set_account_list(
                [a.get("name", "") for a in self.facade.list_accounts() if a.get("name")]
            )
            return

        existing = self.account_panels.get(account_name)
        if existing is not None and existing is not source_panel:
            self.tabs.setCurrentWidget(existing)
            names = [a.get("name", "") for a in self.facade.list_accounts() if a.get("name")]
            source_panel.set_account_list(names)
            return

        old_account = source_panel.account
        old_name = old_account.get("name", "")
        if old_name == account_name:
            return

        # 旧账号的浏览器不能跟到新账号：否则 ready 仍为 True，启动新浏览器时无法预运行，
        # 还可能占着 user-data-dir 导致 CDP 卡住。
        self.facade.close_browser(old_account)

        self.facade.select_account(account)
        self.facade.add_account_to_tasks(account)

        source_panel.rebind_account(account)
        if old_name in self.account_panels:
            self.account_panels.pop(old_name, None)
        self.account_panels[account_name] = source_panel

        index = self.tabs.indexOf(source_panel)
        if index != -1:
            self.tabs.setTabText(index, account_name)
            self.add_tab_close_button(account, index)

        if old_name in self.pending_logs:
            self.pending_logs.setdefault(account_name, []).extend(
                self.pending_logs.pop(old_name, [])
            )
        if old_name in self._prev_task_status:
            self._prev_task_status[account_name] = self._prev_task_status.pop(old_name)

        self._refresh_account_switchers()
        print(f"已切换账号面板: {old_name} → {account_name}")

    def _refresh_account_switchers(self):
        names = [a.get("name", "") for a in self.facade.list_accounts() if a.get("name")]
        for panel in self.account_panels.values():
            panel.set_account_list(names)

    def on_reconnect(self, account: dict):
        name = account["name"]
        print(f"请求重连: {name}")
        self.facade.reconnect_browser(account)

    def on_select_window(self, account: dict):
        name = account["name"]
        hwnd = account.get("window_hwnd")
        title = account.get("window_title", "?")
        print(f"选择窗口: {name} hwnd={hwnd} title={title}")
        self.status_label.setText(f"已选窗口: {title[:40]}")
        self.facade.register_window_target(account)

    def on_open_taskflow(self, account: dict):
        name = account["name"]
        print(f"打开TaskFlow: {name}")
        self.facade.register_account_to_taskflow(account)
        self.facade.open_taskflow_browser(account)

    def add_tab_close_button(self, account: dict, index: int):
        """为账号 Tab 添加关闭按钮"""
        close_btn = QPushButton("✕")
        close_btn.setObjectName("TabCloseButton")
        close_btn.setFixedSize(16, 16)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setProperty("accountTab", True)

        close_btn.clicked.connect(
            lambda _, p=self.tabs.widget(index): self._close_account_tab(p.account)
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
        self._refresh_account_switchers()
        print("已关闭账号页:", name)

    def on_browser_ready(self, account_name: str):
        print(f"{account_name}就绪")

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
        self.facade.controller.capture_screenshot(account)

    def on_target_changed(self, account: dict):
        self.facade.controller.sync_taskflow_target(account)

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

    def on_match_event(self, account_name: str, payload: dict):
        # 未打开时也创建实例并缓冲，关窗后仍继续采集
        if self._match_debug is None:
            win = MatchDebugWindow(parent=self)
            MatchDebugWindow._instance = win
            self._match_debug = win
        self._match_debug.append_event(account_name, payload or {})

    def open_match_debug(self):
        self._match_debug = MatchDebugWindow.open(parent=self)

    def open_script_gen(self):
        from script_gen.window import ScriptGenWindow
        self._script_gen = ScriptGenWindow.open(facade=self.facade, parent=self)

    def open_script_spec(self):
        from script_spec.window import SpecEditorWindow
        self._script_spec = SpecEditorWindow.open(parent=self)

    def on_theme_changed(self, theme: str):
        self.reload_stylesheet()
        for panel in self.account_panels.values():
            panel.refresh_log_themes()
        if self._script_gen is not None:
            try:
                from gui.styles.theme import load_theme_qss
                self._script_gen.setStyleSheet(load_theme_qss(theme))
            except Exception:
                pass
        if self._script_spec is not None:
            try:
                from gui.styles.theme import load_theme_qss
                self._script_spec.setStyleSheet(load_theme_qss(theme))
                panel = getattr(self._script_spec, "panel", None)
                if panel is not None and hasattr(panel, "apply_theme"):
                    panel.apply_theme()
            except Exception:
                pass
        if self._match_debug is not None:
            try:
                from gui.styles.theme import load_theme_qss
                self._match_debug.setStyleSheet(load_theme_qss(theme))
            except Exception:
                pass

    def setup_shortcuts(self):
        shortcut_f5 = QShortcut(QKeySequence("F5"), self)
        shortcut_f5.activated.connect(self.reload_all)
        print("样式重载快捷键已设置: F5")

        shortcut_debug = QShortcut(QKeySequence("Ctrl+Shift+D"), self)
        shortcut_debug.activated.connect(self.open_match_debug)

        shortcut_gen = QShortcut(QKeySequence("Ctrl+Shift+G"), self)
        shortcut_gen.activated.connect(self.open_script_gen)

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

        if hasattr(self, "settings_tab") and self.settings_tab is not None:
            self.settings_tab.reload_all()

        print("样式表重载完成")

    def _bring_to_front(self):
        """被其它窗口挡住时强制置顶并激活（任务栏关闭确认等场景）。"""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            if not hwnd:
                return
            fg = user32.GetForegroundWindow()
            if fg == hwnd:
                return
            fg_tid = user32.GetWindowThreadProcessId(fg, None)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != cur_tid:
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
            user32.BringWindowToTop(hwnd)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception:
            pass

    def closeEvent(self, event):
        if getattr(self, "_closing_confirmed", False):
            self._shutdown_and_accept(event)
            return

        # 任务栏关闭时先置顶，否则确认框会落在其它窗口后面，看起来像没反应
        self._bring_to_front()
        QApplication.processEvents()

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("确认退出")
        box.setText("确定要退出程序吗？\n将同时关闭脚本生成等所有相关窗口。")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.raise_()
        box.activateWindow()
        reply = box.exec()
        if reply != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self._closing_confirmed = True
        self._close_all_other_windows()
        self._shutdown_and_accept(event)

    def _close_all_other_windows(self):
        """关闭除主窗口外的其它顶层窗口（脚本生成、调试窗等）。"""
        # 脚本生成窗：强制销毁（否则 close 只会隐藏）
        try:
            from script_gen.window import ScriptGenWindow
            inst = ScriptGenWindow._instance
            if inst is not None:
                inst.force_close()
        except Exception:
            pass
        try:
            from script_spec.window import SpecEditorWindow
            inst = SpecEditorWindow._instance
            if inst is not None:
                inst.force_close()
        except Exception:
            pass
        try:
            from gui.widgets.MatchDebugWindow import MatchDebugWindow
            inst = MatchDebugWindow._instance
            if inst is not None:
                inst.force_close()
        except Exception:
            pass
        app = QApplication.instance()
        if app is None:
            return
        for w in list(app.topLevelWidgets()):
            if w is self:
                continue
            try:
                if not w.isWindow():
                    continue
                if w.objectName() in ("ScriptGenWindow", "SpecEditorWindow", "MatchDebugWindow"):
                    # 已 force_close
                    continue
                w.close()
            except Exception:
                pass
        self._script_gen = None
        self._script_spec = None
        self._match_debug = None

    def _shutdown_and_accept(self, event):
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

        config.load()
        self.reload_stylesheet()

        if hasattr(self, "settings_tab") and self.settings_tab is not None:
            self.settings_tab.reload_all()

        for panel in self.account_panels.values():
            panel.refresh_log_themes()

        print("热重载完成")
