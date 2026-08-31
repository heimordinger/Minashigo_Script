"""脚本说明编辑器独立窗口（关闭=隐藏缓存，不销毁内容）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QVBoxLayout, QWidget

from core.path import ICON_PATH


class SpecEditorWindow(QWidget):
    """无模态独立窗口。关闭按钮仅隐藏并保留编辑内容；主程序退出时再销毁。"""

    _instance: SpecEditorWindow | None = None

    def __init__(self, parent=None):
        super().__init__(None, Qt.Window)
        self.setWindowTitle("脚本IDE")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setMinimumSize(720, 560)
        self.resize(1120, 740)
        self.setObjectName("SpecEditorWindow")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._force_close = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        from script_spec.editor import SpecEditor
        self.panel = SpecEditor()
        parent_qss = (parent.styleSheet() or "").strip() if parent is not None else ""
        if parent_qss:
            self.setStyleSheet(parent_qss)
        else:
            from gui.styles.theme import current_theme_from_config, load_theme_qss
            self.setStyleSheet(load_theme_qss(current_theme_from_config()))
        root.addWidget(self.panel)
        self.panel.apply_theme()

    def force_close(self):
        self._force_close = True
        self.close()

    def closeEvent(self, event):
        if self._force_close:
            if SpecEditorWindow._instance is self:
                SpecEditorWindow._instance = None
            super().closeEvent(event)
            return
        self.hide()
        event.ignore()

    @classmethod
    def open(cls, *, parent=None) -> SpecEditorWindow:
        win = cls._instance
        if win is None:
            win = cls(parent=parent)
            cls._instance = win
        win.show()
        win.raise_()
        win.activateWindow()
        return win
