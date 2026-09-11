"""脚本生成独立窗口（关闭=隐藏缓存，不销毁内容）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QCursor
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from core.path import ICON_PATH


class ScriptGenWindow(QWidget):
    """无模态独立窗口：生成 → 试运行 → 反馈修订 → 确认保存。
    使用 Qt.Window（非 Tool），可与主窗口互相遮挡、切换焦点。
    关闭按钮仅隐藏窗口并保留面板状态；主程序退出时再真正销毁。
    """

    _instance: ScriptGenWindow | None = None

    def __init__(self, parent=None, facade=None):
        # 不挂 parent，避免 Tool/子窗口始终压在主窗之上
        super().__init__(None, Qt.Window)
        self.setWindowTitle("脚本生成")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setMinimumSize(720, 480)
        self.resize(900, 720)
        self.setObjectName("ScriptGenWindow")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._style_parent = parent
        self._force_close = False
        self._facade = facade
        self.panel = None
        self._mounted = False

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._placeholder = QLabel("正在加载脚本生成…")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setObjectName("MutedLabel")
        self._root.addWidget(self._placeholder)

        parent_qss = (parent.styleSheet() or "").strip() if parent is not None else ""
        if parent_qss:
            self.setStyleSheet(parent_qss)
        else:
            from gui.styles.theme import current_theme_from_config, load_theme_qss
            self.setStyleSheet(load_theme_qss(current_theme_from_config()))

        # 先亮窗，下一拍再挂重面板，避免主界面长时间无响应
        QTimer.singleShot(0, self._mount_panel)

    def _mount_panel(self) -> None:
        if self._mounted:
            return
        self._mounted = True
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        QApplication.processEvents()
        try:
            # 首次只 import，不在打开时全量 reload（生成 Worker 内仍会热重载）
            from gui.widgets.ScriptGenerator import ScriptGenerator

            panel = ScriptGenerator()
            if self._facade is not None:
                panel.set_facade(self._facade)
            if self._placeholder is not None:
                self._root.removeWidget(self._placeholder)
                self._placeholder.deleteLater()
                self._placeholder = None
            self.panel = panel
            self._root.addWidget(panel)
        except Exception as e:
            if self._placeholder is not None:
                self._placeholder.setText(f"加载失败: {e}")
            print(f"[ScriptGenWindow] 面板加载失败: {e}")
        finally:
            QApplication.restoreOverrideCursor()

    def set_facade(self, facade):
        self._facade = facade
        if self.panel is not None:
            self.panel.set_facade(facade)

    def _busy(self) -> bool:
        p = getattr(self, "panel", None)
        if p is None:
            return False
        for attr in ("_worker", "_revise_worker", "_test_worker", "_vision_test_worker"):
            w = getattr(p, attr, None)
            if w is not None and hasattr(w, "isRunning") and w.isRunning():
                return True
        if getattr(p, "_trial_running", False):
            return True
        return False

    def rebuild_panel(self, facade=None):
        """显式换新面板（会丢失当前编辑内容；仅调试/强制刷新时用）。"""
        if self.panel is None:
            if facade is not None:
                self._facade = facade
            self._mount_panel()
            return
        if self._busy():
            print("[ScriptGenWindow] 正在生成/试运行，跳过面板重建")
            if facade is not None:
                self.set_facade(facade)
            return
        from gui.widgets.ScriptGenerator import ScriptGenerator

        old = self.panel
        new_panel = ScriptGenerator()
        fac = facade if facade is not None else self._facade
        if fac is None and hasattr(old, "_facade"):
            fac = old._facade
        if fac is not None:
            new_panel.set_facade(fac)
        self._root.replaceWidget(old, new_panel)
        old.deleteLater()
        self.panel = new_panel

    def force_close(self):
        """主程序退出时真正关闭并释放。"""
        self._force_close = True
        self.close()

    def closeEvent(self, event):
        if self._force_close:
            if ScriptGenWindow._instance is self:
                ScriptGenWindow._instance = None
            super().closeEvent(event)
            return
        self.hide()
        event.ignore()

    @classmethod
    def open(cls, *, facade=None, parent=None) -> ScriptGenWindow:
        win = cls._instance
        if win is None:
            win = cls(parent=parent, facade=facade)
            cls._instance = win
        else:
            # 复用已缓存窗口与面板；打开时不再全量热重载（避免卡顿）
            if facade is not None:
                win.set_facade(facade)
        win.show()
        win.raise_()
        win.activateWindow()
        return win
