from PySide6.QtCore import Qt, QUrl, Signal, QEvent, QThread
from PySide6.QtGui import QDesktopServices, QStandardItemModel, QStandardItem
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QMessageBox, QFileDialog, QCheckBox,
    QScrollArea, QComboBox,
)

from core.config.config import config, _DEFAULT_CONFIG
from core.demo_mode import demo_settings_paths, is_demo_mode
from core.path import PROJECT_ROOT
from gui.widgets.AboutWidget import AboutWidget


class _CacheSizeWorker(QThread):
    finished_ok = Signal(object)  # dict[str, int]
    failed = Signal(str)

    def run(self):
        try:
            from backend.maintenance.cache_cleanup import estimate_sizes

            self.finished_ok.emit(estimate_sizes())
        except Exception as e:
            self.failed.emit(str(e))


class _CacheClearWorker(QThread):
    finished_ok = Signal(object)  # list[ClearItemResult]
    failed = Signal(str)

    def __init__(self, keys: list, parent=None):
        super().__init__(parent)
        self._keys = list(keys)

    def run(self):
        try:
            from backend.maintenance.cache_cleanup import run_clear

            self.finished_ok.emit(run_clear(self._keys))
        except Exception as e:
            self.failed.emit(str(e))


class CheckableComboBox(QComboBox):
    """下拉多选：项带勾选，展示区显示已选摘要。"""

    selection_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(QStandardItemModel(self))
        self.view().viewport().installEventFilter(self)
        self._updating = False
        self.setEditable(True)
        le = self.lineEdit()
        if le is not None:
            le.setReadOnly(True)

    def add_check_item(self, text: str, data, *, checked: bool = False, tip: str = ""):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        item.setData(data, Qt.UserRole)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        if tip:
            item.setToolTip(tip)
        self.model().appendRow(item)
        self._refresh_display()

    def eventFilter(self, obj, event):
        if obj is self.view().viewport() and event.type() == QEvent.MouseButtonRelease:
            pos = (
                event.position().toPoint()
                if hasattr(event, "position")
                else event.pos()
            )
            idx = self.view().indexAt(pos)
            if idx.isValid():
                item = self.model().itemFromIndex(idx)
                if item is not None and item.flags() & Qt.ItemIsUserCheckable:
                    item.setCheckState(
                        Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
                    )
                    self._refresh_display()
                    self.selection_changed.emit()
                    return True
        return super().eventFilter(obj, event)

    def checked_keys(self) -> list:
        keys = []
        for i in range(self.model().rowCount()):
            item = self.model().item(i)
            if item is not None and item.checkState() == Qt.Checked:
                keys.append(item.data(Qt.UserRole))
        return keys

    def checked_labels(self) -> list[str]:
        labels = []
        for i in range(self.model().rowCount()):
            item = self.model().item(i)
            if item is not None and item.checkState() == Qt.Checked:
                labels.append(item.text())
        return labels

    def _refresh_display(self):
        if self._updating:
            return
        self._updating = True
        try:
            labels = self.checked_labels()
            if not labels:
                text = "（未选择）"
            elif len(labels) == 1:
                text = labels[0].split("（")[0]
            else:
                shorts = [x.split("（")[0] for x in labels]
                text = f"已选 {len(labels)} 项：" + "、".join(shorts)
            le = self.lineEdit()
            if le is not None:
                le.setText(text)
            else:
                self.setCurrentText(text)
        finally:
            self._updating = False


class SettingsPanel(QWidget):
    theme_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache_sizes: dict[str, int] = {}
        self._size_worker: _CacheSizeWorker | None = None
        self._clear_worker: _CacheClearWorker | None = None
        self._build_ui()
        self.reload_all()

    def _build_ui(self):
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setObjectName("SettingsScroll")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        appearance_box = QGroupBox("外观")
        appearance_layout = QVBoxLayout(appearance_box)
        appearance_layout.addWidget(QLabel("界面主题："))
        theme_row = QHBoxLayout()
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("亮色", "light")
        self.theme_combo.addItem("暗色", "dark")
        self.theme_combo.setToolTip("亮色 / 暗色两套纸感工作室主题，切换后立即生效并写入配置")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_combo_changed)
        theme_row.addWidget(self.theme_combo, 1)
        appearance_layout.addLayout(theme_row)
        tip = QLabel("两套主题结构相同，只换配色；也可按 F5 手动重载样式。")
        tip.setObjectName("MutedLabel")
        tip.setWordWrap(True)
        appearance_layout.addWidget(tip)

        browser_box = QGroupBox("浏览器设置")
        browser_layout = QVBoxLayout(browser_box)

        browser_layout.addWidget(QLabel("浏览器启动地址："))

        path_layout = QHBoxLayout()

        self.browser_path_edit = QLineEdit()
        self.browser_path_edit.setReadOnly(True)
        self.browser_path_edit.setPlaceholderText("请选择浏览器可执行文件（.exe）")

        self.browser_browse_btn = QPushButton("浏览…")
        self.browser_browse_btn.clicked.connect(self._select_browser_exe)

        path_layout.addWidget(self.browser_path_edit)
        path_layout.addWidget(self.browser_browse_btn)

        browser_layout.addLayout(path_layout)

        browser_layout.addWidget(QLabel("浏览器数据目录："))

        user_data_layout = QHBoxLayout()

        self.user_data_edit = QLineEdit()
        self.user_data_edit.setReadOnly(True)
        self.user_data_edit.setPlaceholderText("请选择浏览器数据目录")

        self.user_data_btn = QPushButton("浏览…")
        self.user_data_btn.clicked.connect(self._select_browser_data_dir)

        user_data_layout.addWidget(self.user_data_edit)
        user_data_layout.addWidget(self.user_data_btn)

        browser_layout.addLayout(user_data_layout)

        loading_box = QGroupBox("加载动画设置")
        loading_layout = QVBoxLayout(loading_box)

        self.loading_topmost_checkbox = QCheckBox("加载动画始终置顶")
        self.loading_topmost_checkbox.setToolTip("开启后，加载动画将始终显示在最上层")
        loading_layout.addWidget(self.loading_topmost_checkbox)

        window_box = QGroupBox("窗口自动化")
        window_layout = QVBoxLayout(window_box)
        window_layout.addWidget(QLabel("最小化策略："))
        self.min_policy_combo = QComboBox()
        self.min_policy_combo.addItem("自动后台恢复（推荐）", "restore")
        self.min_policy_combo.addItem("暂停等待手动恢复", "pause")
        self.min_policy_combo.addItem("直接失败并停止", "fail")
        self.min_policy_combo.setToolTip(
            "窗口/模拟器被最小化时：\n"
            "· 自动恢复：不抢焦点、拉起后不回缩任务栏\n"
            "· 暂停：日志提示，恢复窗口后继续\n"
            "· 失败：抛错停止任务"
        )
        window_layout.addWidget(self.min_policy_combo)
        tip_win = QLabel(
            "MuMu 等模拟器最小化后常停渲染；默认会后台恢复并保持显示（可被挡住）。"
        )
        tip_win.setObjectName("MutedLabel")
        tip_win.setWordWrap(True)
        window_layout.addWidget(tip_win)

        cache_box = QGroupBox("清理缓存")
        cache_layout = QVBoxLayout(cache_box)
        tip_cache = QLabel(
            "下拉勾选要清的项，体积随选项变化；不影响脚本与素材 PNG。"
        )
        tip_cache.setObjectName("MutedLabel")
        tip_cache.setWordWrap(True)
        cache_layout.addWidget(tip_cache)

        from backend.maintenance.cache_cleanup import CLEAR_OPTIONS

        cache_row = QHBoxLayout()
        cache_row.addWidget(QLabel("清理项："))
        self.cache_combo = CheckableComboBox()
        self.cache_combo.setMinimumWidth(280)
        for key, label, tip_text, _fn in CLEAR_OPTIONS:
            self.cache_combo.add_check_item(
                label, key, checked=(key == "match"), tip=tip_text
            )
        self.cache_combo.selection_changed.connect(self._update_cache_size_label)
        cache_row.addWidget(self.cache_combo, 1)
        cache_layout.addLayout(cache_row)

        cache_btn_row = QHBoxLayout()
        self.cache_size_label = QLabel("")
        self.cache_size_label.setObjectName("MutedLabel")
        self.cache_size_label.setWordWrap(True)
        self.refresh_cache_btn = QPushButton("刷新体积")
        self.refresh_cache_btn.clicked.connect(self._refresh_cache_sizes)
        self.clear_cache_btn = QPushButton("立即清理")
        self.clear_cache_btn.setObjectName("PrimaryButton")
        self.clear_cache_btn.clicked.connect(self._clear_selected_caches)
        cache_btn_row.addWidget(self.cache_size_label, 1)
        cache_btn_row.addWidget(self.refresh_cache_btn)
        cache_btn_row.addWidget(self.clear_cache_btn)
        cache_layout.addLayout(cache_btn_row)

        about_box = QGroupBox("关于")
        about_layout = QVBoxLayout(about_box)
        title = QLabel("Minashigo Script")
        title.setObjectName("TitleLabel")
        self.desc_label = QLabel()
        self.desc_label.setWordWrap(True)
        contact_layout = QHBoxLayout()
        contact_title = QLabel("联系我:")
        qq_btn = QPushButton("QQ")
        qq_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://qm.qq.com/q/fALtYzXmdG"))
        )

        contact_layout.addWidget(qq_btn)
        contact_layout.addStretch()
        about_layout.addWidget(title)
        about_widget = AboutWidget()
        about_layout.addWidget(about_widget)
        about_layout.addSpacing(8)
        about_layout.addWidget(contact_title)
        about_layout.addLayout(contact_layout)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.clicked.connect(self._reset_browser_path)

        self.save_btn = QPushButton("保存设置")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.clicked.connect(self._save_config)

        btn_layout.addWidget(self.reset_btn)
        btn_layout.addWidget(self.save_btn)

        layout.addWidget(appearance_box)
        layout.addWidget(browser_box)
        layout.addWidget(loading_box)
        layout.addWidget(window_box)
        layout.addWidget(cache_box)
        layout.addWidget(about_box)
        layout.addStretch()
        layout.addLayout(btn_layout)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

    def _set_style(self, edit: QLineEdit, is_user: bool):
        edit.setObjectName("UserConfigValue" if is_user else "DefaultConfigValue")
        edit.style().unpolish(edit)
        edit.style().polish(edit)

    def _refresh_about(self):
        self.desc_label.setText(
            f"作者：{config.author}\n\n{config.about}"
        )

    def _load_config(self):
        if is_demo_mode():
            browser_path, data_dir = demo_settings_paths(project_root=PROJECT_ROOT)
            self.browser_path_edit.setText(browser_path)
            self._set_style(self.browser_path_edit, False)
            self.user_data_edit.setText(data_dir)
            self._set_style(self.user_data_edit, False)
            self.browser_browse_btn.setEnabled(False)
            self.user_data_btn.setEnabled(False)
            self.reset_btn.setEnabled(False)
        else:
            self.browser_browse_btn.setEnabled(True)
            self.user_data_btn.setEnabled(True)
            self.reset_btn.setEnabled(True)
            browser_cfg = config.data.get("browser", {})
            default_cfg = _DEFAULT_CONFIG["browser"]
            user_val = browser_cfg.get("browser_path")
            default_val = default_cfg["browser_path"]

            if user_val:
                self.browser_path_edit.setText(user_val)
                self._set_style(self.browser_path_edit, True)
            else:
                self.browser_path_edit.setText(default_val)
                self._set_style(self.browser_path_edit, False)
            user_val = browser_cfg.get("browser_data_dir")
            default_val = default_cfg["browser_data_dir"]

            if user_val:
                self.user_data_edit.setText(user_val)
                self._set_style(self.user_data_edit, True)
            else:
                self.user_data_edit.setText(default_val)
                self._set_style(self.user_data_edit, False)

        loading_cfg = config.data.get("loading", {})
        default_loading_cfg = _DEFAULT_CONFIG.get("loading", {"topmost": True})
        topmost_val = loading_cfg.get("topmost", default_loading_cfg.get("topmost", True))
        self.loading_topmost_checkbox.setChecked(topmost_val)

        theme = config.ui_theme
        self.theme_combo.blockSignals(True)
        idx = self.theme_combo.findData(theme)
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.theme_combo.blockSignals(False)

        pol = config.window_minimized_policy
        pidx = self.min_policy_combo.findData(pol)
        self.min_policy_combo.setCurrentIndex(pidx if pidx >= 0 else 0)

    def _on_theme_combo_changed(self, _index: int):
        theme = self.theme_combo.currentData()
        if not theme:
            return
        try:
            config.set("ui.theme", theme)
            config.save()
        except Exception as e:
            QMessageBox.warning(self, "主题切换失败", str(e))
            return
        self.theme_changed.emit(theme)

    def _select_browser_exe(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择浏览器", "", "Executable (*.exe)"
        )
        if not file_path:
            return

        self.browser_path_edit.setText(file_path)
        self._set_style(self.browser_path_edit, True)

    def _select_browser_data_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "选择用户数据目录")
        if not dir_path:
            return

        self.user_data_edit.setText(dir_path)
        self._set_style(self.user_data_edit, True)

    def _reset_browser_path(self):
        browser_cfg = config.data.setdefault("browser", {})
        browser_cfg.pop("browser_path", None)

        default_val = _DEFAULT_CONFIG["browser"]["browser_path"]
        self.browser_path_edit.setText(default_val)
        self._set_style(self.browser_path_edit, False)

    def _save_config(self):
        if is_demo_mode():
            QMessageBox.information(
                self,
                "演示模式",
                "演示模式下不保存浏览器路径；可切换主题预览，但不会写入配置。",
            )
            return
        path = self.browser_path_edit.text().strip()
        user_data = self.user_data_edit.text().strip()
        theme = self.theme_combo.currentData() or "light"

        try:
            config.set("browser.browser_path", path)
            config.set("browser.browser_data_dir", user_data)
            config.set("loading.topmost", self.loading_topmost_checkbox.isChecked())
            config.set("ui.theme", theme)
            config.set(
                "window.minimized_policy",
                self.min_policy_combo.currentData() or "restore",
            )
        except Exception as e:
            QMessageBox.warning(self, "配置错误", str(e))
            return

        config.save()
        self.theme_changed.emit(theme)
        QMessageBox.information(self, "已保存", "设置已保存")

    def _update_cache_size_label(self):
        from backend.maintenance.cache_cleanup import CLEAR_OPTIONS, format_size

        keys = self.cache_combo.checked_keys()
        if not keys:
            self.cache_size_label.setText("未选择清理项")
            return
        label_by_key = {k: lab for k, lab, _t, _f in CLEAR_OPTIONS}
        parts = []
        total = 0
        for key in keys:
            n = int(self._cache_sizes.get(key) or 0)
            total += n
            short = label_by_key.get(key, key).split("（")[0]
            parts.append(f"{short} {format_size(n)}")
        self.cache_size_label.setText(
            f"将清除约 {format_size(total)}　|　" + "　".join(parts)
        )

    def _set_cache_busy(self, busy: bool, status: str = "") -> None:
        self.cache_combo.setEnabled(not busy)
        self.refresh_cache_btn.setEnabled(not busy)
        self.clear_cache_btn.setEnabled(not busy)
        if busy and status:
            self.cache_size_label.setText(status)

    def _refresh_cache_sizes(self):
        if self._size_worker is not None and self._size_worker.isRunning():
            return
        if self._clear_worker is not None and self._clear_worker.isRunning():
            return
        self._set_cache_busy(True, "正在统计体积…")
        worker = _CacheSizeWorker(self)
        self._size_worker = worker

        def _ok(sizes):
            self._cache_sizes = sizes if isinstance(sizes, dict) else {}
            self._set_cache_busy(False)
            self._update_cache_size_label()
            self._size_worker = None

        def _fail(msg: str):
            self._set_cache_busy(False)
            self.cache_size_label.setText(f"体积统计失败: {msg}")
            self._size_worker = None

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_fail)
        worker.start()

    def _clear_selected_caches(self):
        if self._clear_worker is not None and self._clear_worker.isRunning():
            return
        if self._size_worker is not None and self._size_worker.isRunning():
            return
        keys = self.cache_combo.checked_keys()
        if not keys:
            QMessageBox.information(self, "清理缓存", "请先在下拉框中勾选至少一项。")
            return
        labels = self.cache_combo.checked_labels()
        reply = QMessageBox.question(
            self,
            "确认清理",
            "将清除：\n- " + "\n- ".join(labels) + "\n\n此操作不可恢复，继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._set_cache_busy(True, "正在后台清理，可继续使用其它功能…")
        worker = _CacheClearWorker(keys, self)
        self._clear_worker = worker

        def _ok(results):
            self._clear_worker = None
            self._set_cache_busy(False)
            try:
                from backend.maintenance.cache_cleanup import format_size

                lines = []
                freed = 0
                for r in results or []:
                    mark = "✓" if r.ok else "✗"
                    lines.append(
                        f"{mark} {r.label}：{r.detail}（{format_size(r.bytes_freed)}）"
                    )
                    freed += int(r.bytes_freed or 0)
                self._refresh_cache_sizes()
                QMessageBox.information(
                    self,
                    "清理完成",
                    "\n".join(lines) + f"\n\n约释放 {format_size(freed)}",
                )
            except Exception as e:
                QMessageBox.warning(self, "清理完成但展示失败", str(e))
                self._refresh_cache_sizes()

        def _fail(msg: str):
            self._clear_worker = None
            self._set_cache_busy(False)
            QMessageBox.warning(self, "清理失败", msg)
            self._refresh_cache_sizes()

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_fail)
        worker.start()

    def reload_all(self):
        config.load()
        self._load_config()
        self._refresh_about()
        self._refresh_cache_sizes()
