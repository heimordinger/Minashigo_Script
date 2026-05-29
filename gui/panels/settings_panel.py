from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QMessageBox, QFileDialog, QCheckBox,
    QScrollArea,
)

from core.config.config import config, _DEFAULT_CONFIG
from gui.widgets.AboutWidget import AboutWidget


class SettingsPanel(QWidget):

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
        title.setStyleSheet("font-size:16px; font-weight:bold;")
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
        self.save_btn.clicked.connect(self._save_config)

        btn_layout.addWidget(self.reset_btn)
        btn_layout.addWidget(self.save_btn)

        layout.addWidget(browser_box)
        layout.addWidget(loading_box)
        layout.addWidget(about_box)
        layout.addStretch()
        layout.addLayout(btn_layout)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

    def _set_style(self, edit: QLineEdit, is_user: bool):
        if is_user:
            edit.setStyleSheet("color: #ffffff;")
        else:
            edit.setStyleSheet("color: #888888;")

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

        try:
            config.set("browser.browser_path", path)
            config.set("browser.browser_data_dir", user_data)
            # 保存加载动画置顶设置
            config.set("loading.topmost", self.loading_topmost_checkbox.isChecked())
        except Exception as e:
            QMessageBox.warning(self, "配置错误", str(e))
            return

        config.save()
        QMessageBox.information(self, "已保存", "设置已保存")

    def reload_all(self):
        config.load()
        self._load_config()
        self._refresh_about()
