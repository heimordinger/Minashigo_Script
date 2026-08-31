"""匹配调试 Tool 窗口：记录 match_image / click_image 时间线。"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QEvent, QPoint, QTimer
from PySide6.QtGui import QColor, QPixmap, QCursor, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFrame, QDialog, QScrollArea, QDialogButtonBox,
)

from core.path import ICON_PATH


_ACTION_TEXT = {"match": "match", "click": "click"}
_STATUS_TEXT = {"matching": "匹配中", "ok": "成功", "fail": "失败"}
_STATUS_COLOR = {
    "matching": QColor("#f0c040"),
    "ok": QColor("#5ecf7a"),
    "fail": QColor("#e07070"),
}

_COL_TEMPLATE = 3


def _resolve_image(path: str) -> Path | None:
    if not path or str(path).startswith("data:"):
        return None
    p = Path(path)
    if p.is_file():
        return p
    candidates = []
    if p.suffix:
        candidates.append(p)
    else:
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            candidates.append(Path(str(p) + ext))
            candidates.append(p.with_suffix(ext))
    for c in candidates:
        if c.is_file():
            return c
    return None


class _HoverPreview(QFrame):
    """鼠标悬停时的路径 + 缩略图浮层（不接收鼠标，避免抢 leave）。"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("MatchHoverPreview")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(
            "#MatchHoverPreview {"
            "  background:#2a2a2a; border:1px solid #555; border-radius:4px;"
            "}"
            "QLabel { color:#ddd; }"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setMaximumWidth(280)
        self.path_label.setStyleSheet("color:#aaa; font-size:11px;")
        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setMinimumSize(80, 40)
        self.img_label.setStyleSheet("background:#1a1a1a; border:1px solid #444;")
        lay.addWidget(self.path_label)
        lay.addWidget(self.img_label)
        self.hide()

    def show_for(self, path: str, global_pos: QPoint):
        self.path_label.setText(path or "(无路径)")
        resolved = _resolve_image(path)
        pix = QPixmap(str(resolved)) if resolved else QPixmap()
        if not pix.isNull():
            scaled = pix.scaled(220, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.img_label.setPixmap(scaled)
            self.img_label.setFixedSize(scaled.size())
            self.img_label.setText("")
        else:
            self.img_label.clear()
            self.img_label.setFixedSize(160, 48)
            self.img_label.setText("无法加载图片")
        self.adjustSize()
        self.move(global_pos)
        self.show()
        self.raise_()


class _FullPreviewDialog(QDialog):
    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(Path(path).name if path else "模板预览")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        path_lbl = QLabel(path or "")
        path_lbl.setWordWrap(True)
        path_lbl.setStyleSheet("color:#aaa; font-size:12px;")
        lay.addWidget(path_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        img = QLabel()
        img.setAlignment(Qt.AlignCenter)
        resolved = _resolve_image(path)
        pix = QPixmap(str(resolved)) if resolved else QPixmap()
        if not pix.isNull():
            # 大图：限制最大边，保留可读细节
            scaled = pix.scaled(1200, 900, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            img.setPixmap(scaled)
        else:
            img.setText("无法加载图片")
        scroll.setWidget(img)
        lay.addWidget(scroll, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)


class MatchDebugWindow(QWidget):
    """无模态独立窗口：可与主窗互相遮挡；关闭仅隐藏，继续采集。"""

    MAX_ROWS = 800
    _instance: MatchDebugWindow | None = None

    def __init__(self, parent=None):
        # 不挂 parent，避免 Tool/子窗口始终压在主窗之上
        super().__init__(None, Qt.Window)
        self.setWindowTitle("匹配调试")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setMinimumSize(720, 360)
        self.resize(860, 480)
        self.setObjectName("MatchDebugWindow")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._force_close = False

        parent_qss = (parent.styleSheet() or "").strip() if parent is not None else ""
        if parent_qss:
            self.setStyleSheet(parent_qss)
        else:
            try:
                from gui.styles.theme import current_theme_from_config, load_theme_qss
                self.setStyleSheet(load_theme_qss(current_theme_from_config()))
            except Exception:
                pass

        self._events: deque[dict] = deque(maxlen=self.MAX_ROWS)
        self._accounts: set[str] = set()
        self._hover = _HoverPreview(self)
        self._hover_row: int | None = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(60)
        self._hover_timer.timeout.connect(self._show_hover_now)
        self._pending_hover_row: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        tip = QLabel(
            "记录 match_image / click_image。模板列：悬停看缩略图，单击看大图。"
            "关闭窗口不停止采集。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#aaa; font-size:12px;")
        root.addWidget(tip)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("账号"))
        self.account_combo = QComboBox()
        self.account_combo.addItem("全部", "")
        self.account_combo.currentIndexChanged.connect(self._rebuild_table)
        bar.addWidget(self.account_combo, stretch=1)

        self.show_matching_cb = QCheckBox("显示「匹配中」")
        self.show_matching_cb.setChecked(False)
        self.show_matching_cb.toggled.connect(self._rebuild_table)
        bar.addWidget(self.show_matching_cb)

        self.auto_scroll_cb = QCheckBox("自动滚到底")
        self.auto_scroll_cb.setChecked(True)
        bar.addWidget(self.auto_scroll_cb)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.clear)
        bar.addWidget(clear_btn)
        root.addLayout(bar)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["时间", "账号", "操作", "模板", "状态", "分数", "坐标"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setMouseTracking(True)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.table.setStyleSheet(
            "QTableWidget { background:#1e1e1e; alternate-background-color:#252525; "
            "gridline-color:#333; color:#ddd; }"
            "QHeaderView::section { background:#2a2a2a; color:#ccc; padding:4px; "
            "border:1px solid #3a3a3a; }"
        )
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.viewport().installEventFilter(self)
        self.installEventFilter(self)
        root.addWidget(self.table, stretch=1)

        self.count_label = QLabel("0 条")
        self.count_label.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(self.count_label)

    def clear(self):
        self._hide_hover()
        self._events.clear()
        self._rebuild_table()

    def append_event(self, account: str, payload: dict):
        ev = {
            "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "account": account or "",
            "action": (payload or {}).get("action") or "match",
            "img_path": str((payload or {}).get("img_path") or ""),
            "status": (payload or {}).get("status") or "",
            "score": (payload or {}).get("score"),
            "x": (payload or {}).get("x"),
            "y": (payload or {}).get("y"),
        }
        self._events.append(ev)
        if account and account not in self._accounts:
            self._accounts.add(account)
            self.account_combo.addItem(account, account)

        if self.isVisible():
            self._append_row_if_visible(ev)
            self.count_label.setText(f"{self.table.rowCount()} 条显示 / 缓冲 {len(self._events)}")
            if self.auto_scroll_cb.isChecked():
                self.table.scrollToBottom()

    def showEvent(self, event):
        super().showEvent(event)
        self._rebuild_table()

    def hideEvent(self, event):
        self._hide_hover()
        super().hideEvent(event)

    def force_close(self):
        """主程序退出时真正关闭并释放。"""
        self._force_close = True
        self.close()

    def closeEvent(self, event):
        if self._force_close:
            if MatchDebugWindow._instance is self:
                MatchDebugWindow._instance = None
            super().closeEvent(event)
            return
        self.hide()
        event.ignore()

    @classmethod
    def open(cls, *, parent=None) -> MatchDebugWindow:
        win = cls._instance
        if win is None:
            win = cls(parent=parent)
            cls._instance = win
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowDeactivate:
            self._hide_hover()
        super().changeEvent(event)

    def eventFilter(self, obj, event):
        et = event.type()
        if obj is self.table.viewport():
            if et == QEvent.Type.MouseMove:
                self._on_viewport_mouse_move(event.position().toPoint())
            elif et in (
                QEvent.Type.Leave,
                QEvent.Type.Wheel,
                QEvent.Type.MouseButtonPress,
            ):
                self._hide_hover()
        elif obj is self and et == QEvent.Type.Leave:
            # 鼠标离开整个调试窗
            if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self._hide_hover()
        return super().eventFilter(obj, event)

    def _hide_hover(self):
        self._hover_timer.stop()
        self._pending_hover_row = None
        self._hover_row = None
        self._hover.hide()

    def _on_viewport_mouse_move(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid() or idx.column() != _COL_TEMPLATE:
            self._hide_hover()
            return
        row = idx.row()
        if row == self._hover_row and self._hover.isVisible():
            return
        if row == self._pending_hover_row and self._hover_timer.isActive():
            return
        self._pending_hover_row = row
        self._hover_timer.start()

    def _show_hover_now(self):
        row = self._pending_hover_row
        self._pending_hover_row = None
        if row is None:
            return
        # 定时器触发时再确认鼠标仍在该模板格上
        vp = self.table.viewport()
        pos = vp.mapFromGlobal(QCursor.pos())
        if not vp.rect().contains(pos):
            self._hide_hover()
            return
        idx = self.table.indexAt(pos)
        if not idx.isValid() or idx.column() != _COL_TEMPLATE or idx.row() != row:
            self._hide_hover()
            return
        item = self.table.item(row, _COL_TEMPLATE)
        if not item:
            self._hide_hover()
            return
        path = item.data(Qt.UserRole) or ""
        # 出现在光标附近（右下），浮层已穿透鼠标不会抢事件
        global_pos = QCursor.pos() + QPoint(14, 18)
        self._hover_row = row
        self._hover.show_for(str(path), global_pos)

    def _on_cell_clicked(self, row: int, col: int):
        if col != _COL_TEMPLATE:
            return
        item = self.table.item(row, col)
        if not item:
            return
        path = item.data(Qt.UserRole) or ""
        if not path:
            return
        self._hide_hover()
        dlg = _FullPreviewDialog(str(path), self)
        dlg.exec()
        self._hide_hover()
    def _filter_ok(self, ev: dict) -> bool:
        want = self.account_combo.currentData()
        if want and ev.get("account") != want:
            return False
        if not self.show_matching_cb.isChecked() and ev.get("status") == "matching":
            return False
        return True

    def _rebuild_table(self):
        self._hide_hover()
        self.table.setRowCount(0)
        for ev in self._events:
            if self._filter_ok(ev):
                self._append_row_if_visible(ev, force=True)
        self.count_label.setText(f"{self.table.rowCount()} 条显示 / 缓冲 {len(self._events)}")
        if self.auto_scroll_cb.isChecked():
            self.table.scrollToBottom()

    def _append_row_if_visible(self, ev: dict, force: bool = False):
        if not force and not self._filter_ok(ev):
            return
        row = self.table.rowCount()
        self.table.insertRow(row)

        img_path = ev.get("img_path") or ""
        name = Path(img_path).name if img_path and not img_path.startswith("data:") else "(内存图)"
        score = ev.get("score")
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else ""
        xy_s = ""
        x, y = ev.get("x"), ev.get("y")
        try:
            if x is not None and y is not None:
                xy_s = f"({int(x)},{int(y)})"
        except (TypeError, ValueError):
            xy_s = ""

        vals = [
            ev.get("ts", ""),
            ev.get("account", ""),
            _ACTION_TEXT.get(ev.get("action"), ev.get("action", "")),
            name,
            _STATUS_TEXT.get(ev.get("status"), ev.get("status", "")),
            score_s,
            xy_s,
        ]
        for col, text in enumerate(vals):
            item = QTableWidgetItem(text)
            if col == _COL_TEMPLATE:
                item.setData(Qt.UserRole, img_path)
                item.setToolTip("")  # 用自定义悬停预览，避免挡图
                item.setForeground(QColor("#7ec8e3"))
            elif col == 4:
                color = _STATUS_COLOR.get(ev.get("status"))
                if color:
                    item.setForeground(color)
            self.table.setItem(row, col, item)
