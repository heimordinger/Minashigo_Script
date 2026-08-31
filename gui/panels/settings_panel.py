from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QMessageBox, QFileDialog, QCheckBox,
    QScrollArea, QComboBox,
)

from core.config.config import config, _DEFAULT_CONFIG
from gui.widgets.AboutWidget import AboutWidget


class SettingsPanel(QWidget):
    theme_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
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

        # 加载动画设置
        loading_box = QGroupBox("加载动画设置")
        loading_layout = QVBoxLayout(loading_box)

        self.loading_topmost_checkbox = QCheckBox("加载动画始终置顶")
        self.loading_topmost_checkbox.setToolTip("开启后，加载动画将始终显示在最上层")
        loading_layout.addWidget(self.loading_topmost_checkbox)

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

        # 加载动画置顶设置
        loading_cfg = config.data.get("loading", {})
        default_loading_cfg = _DEFAULT_CONFIG.get("loading", {"topmost": True})
        topmost_val = loading_cfg.get("topmost", default_loading_cfg.get("topmost", True))
        self.loading_topmost_checkbox.setChecked(topmost_val)

        theme = config.ui_theme
        self.theme_combo.blockSignals(True)
        idx = self.theme_combo.findData(theme)
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.theme_combo.blockSignals(False)

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
        path = self.browser_path_edit.text().strip()
        user_data = self.user_data_edit.text().strip()
        theme = self.theme_combo.currentData() or "light"

        try:
            config.set("browser.browser_path", path)
            config.set("browser.browser_data_dir", user_data)
            config.set("loading.topmost", self.loading_topmost_checkbox.isChecked())
            config.set("ui.theme", theme)
        except Exception as e:
            QMessageBox.warning(self, "配置错误", str(e))
            return

        config.save()
        self.theme_changed.emit(theme)
        QMessageBox.information(self, "已保存", "设置已保存")

    def reload_all(self):
        config.load()
        self._load_config()
        self._refresh_about()
