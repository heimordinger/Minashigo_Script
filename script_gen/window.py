"""脚本生成独立窗口（关闭=隐藏缓存，不销毁内容）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QVBoxLayout, QWidget

from core.path import ICON_PATH


def _create_panel_class():
    """首次创建时热重载后端 + UI，返回 ScriptGenerator 类。"""
    try:
        from backend.script_generator.reload import reload_script_generator
        reload_script_generator(include_gui=True)
    except Exception as e:
        print(f"[ScriptGenWindow] 热重载失败: {e}")
    from gui.widgets.ScriptGenerator import ScriptGenerator
    return ScriptGenerator


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
        self.setMinimumSize(720, 560)
        self.resize(900, 720)
        self.setObjectName("ScriptGenWindow")
        # 关闭时不销毁，便于再次打开恢复内容
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._style_parent = parent
        self._force_close = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        Panel = _create_panel_class()
        self.panel = Panel()
        # 有主窗样式则继承；独立启动时自行套主题
        parent_qss = (parent.styleSheet() or "").strip() if parent is not None else ""
        if parent_qss:
            self.setStyleSheet(parent_qss)
        else:
            from gui.styles.theme import current_theme_from_config, load_theme_qss
            self.setStyleSheet(load_theme_qss(current_theme_from_config()))
        if facade is not None:
            self.panel.set_facade(facade)
        root.addWidget(self.panel)

    def set_facade(self, facade):
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
        if self._busy():
            print("[ScriptGenWindow] 正在生成/试运行，跳过面板重建")
            if facade is not None:
                self.set_facade(facade)
            return
        Panel = _create_panel_class()
        old = self.panel
        new_panel = Panel()
        fac = facade
        if fac is None and hasattr(old, "_facade"):
            fac = old._facade
        if fac is not None:
            new_panel.set_facade(fac)
        lay = self.layout()
        lay.replaceWidget(old, new_panel)
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
        # 普通关闭：隐藏并缓存，内容保留
        self.hide()
        event.ignore()

    @classmethod
    def open(cls, *, facade=None, parent=None) -> ScriptGenWindow:
        win = cls._instance
        if win is None:
            win = cls(parent=parent, facade=facade)
            cls._instance = win
        else:
            # 复用已缓存窗口与面板，不重建（避免清空 API/描述/轨迹/代码）
            if facade is not None:
                win.set_facade(facade)
            try:
                from backend.script_generator.reload import reload_script_generator
                # 只热重载后端逻辑，不动 UI 面板状态
                reload_script_generator(include_gui=False)
            except Exception as e:
                print(f"[ScriptGenWindow] 后端热重载失败: {e}")
        win.show()
        win.raise_()
        win.activateWindow()
        return win
