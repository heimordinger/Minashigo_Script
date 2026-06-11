"""
窗口选择器 —— 列出所有可见窗口，预览截图，确认后返回 Win32Target。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中（支持直接 python xxx.py 运行）
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import cv2
import numpy as np
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QImage, QPixmap, QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QLineEdit, QSplitter, QWidget, QMessageBox,
    QAbstractItemView, QFrame,
)

from backend.automation.win32_target import Win32Target


class WindowPickerDialog(QDialog):
    """窗口选择对话框。

    用法::

        dialog = WindowPickerDialog(parent=self)
        if dialog.exec():
            target = dialog.selected_target
            # target 就是选中的 Win32Target
    """

    COL_TITLE = 0
    COL_CLASS = 1
    COL_SIZE = 2
    COL_HWND = 3  # 隐藏列

    def __init__(self, parent=None):
        super().__init__(parent)
        self._targets: list[Win32Target] = []
        self.selected_target: Optional[Win32Target] = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._do_preview)

        self._setup_ui()
        self._load_windows()
        self.resize(900, 620)
        self.setWindowTitle("选择目标窗口")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

    # ── 界面搭建 ─────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # 搜索栏
        search_layout = QHBoxLayout()
        search_label = QLabel("搜索:")
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("输入窗口标题关键词…")
        self._search_input.textChanged.connect(self._on_search)
        search_layout.addWidget(search_label)
        search_layout.addWidget(self._search_input, 1)
        layout.addLayout(search_layout)

        # 分割线：上=列表，下=预览
        splitter = QSplitter(Qt.Vertical)

        # ── 上：表格 ──
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["窗口标题", "类名", "客户区", "HWND"])
        self._table.setColumnHidden(self.COL_HWND, True)  # 隐藏 HWND 列
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.setSortingEnabled(True)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(self.COL_TITLE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(self.COL_CLASS, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(self.COL_SIZE, QHeaderView.ResizeToContents)

        splitter.addWidget(self._table)

        # ── 下：预览区 ──
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setContentsMargins(0, 4, 0, 0)

        preview_header = QHBoxLayout()
        self._preview_label = QLabel("选中窗口后在此预览截图")
        self._preview_label.setStyleSheet("color: #888;")
        self._preview_info = QLabel("")
        self._preview_info.setStyleSheet("color: #555; font-size: 11px;")
        preview_header.addWidget(self._preview_label, 1)
        preview_header.addWidget(self._preview_info)
        preview_layout.addLayout(preview_header)

        self._preview_image = QLabel()
        self._preview_image.setAlignment(Qt.AlignCenter)
        self._preview_image.setMinimumHeight(250)
        self._preview_image.setFrameShape(QFrame.StyledPanel)
        self._preview_image.setStyleSheet("background: #1e1e1e; border: 1px solid #444;")
        preview_layout.addWidget(self._preview_image, 1)

        splitter.addWidget(preview_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addWidget(splitter, 1)

        # ── 底部按钮 ──
        btn_layout = QHBoxLayout()
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #888;")
        btn_layout.addWidget(self._count_label, 1)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._load_windows)
        btn_layout.addWidget(self._refresh_btn)

        self._preview_btn = QPushButton("刷新预览")
        self._preview_btn.clicked.connect(self._do_preview)
        btn_layout.addWidget(self._preview_btn)

        btn_layout.addSpacing(20)

        self._ok_btn = QPushButton("确认选择")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(self._ok_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        layout.addLayout(btn_layout)

    # ── 加载窗口 ─────────────────────────────────────────────

    def _load_windows(self):
        """枚举所有可见窗口并填充表格。"""
        self._targets = Win32Target.all_visible()
        self._targets.sort(key=lambda w: (not w.is_visible, w.title.lower()))
        self._apply_filter()

    def _apply_filter(self):
        keyword = self._search_input.text().strip().lower()
        filtered = [
            t for t in self._targets
            if not keyword or keyword in t.title.lower()
               or keyword in t.class_name.lower()
        ] if keyword else self._targets

        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(filtered))

        for row, target in enumerate(filtered):
            cr = target.client_rect
            title_item = QTableWidgetItem(target.title or "(无标题)")
            title_item.setToolTip(target.title)
            self._table.setItem(row, self.COL_TITLE, title_item)
            self._table.setItem(row, self.COL_CLASS, QTableWidgetItem(target.class_name))
            size_text = f"{cr['width']}×{cr['height']}" if cr['width'] > 0 else "(最小化)"
            self._table.setItem(row, self.COL_SIZE, QTableWidgetItem(size_text))
            self._table.setItem(row, self.COL_HWND, QTableWidgetItem(str(target.hwnd)))

        self._table.setSortingEnabled(True)
        self._count_label.setText(f"共 {len(filtered)} 个窗口")

    def _on_search(self, _text):
        self._apply_filter()

    # ── 选中 / 预览 ─────────────────────────────────────────

    def _on_selection_changed(self):
        target = self._get_selected_target()
        if target:
            self._preview_info.setText(f"{target.client_rect['width']}×{target.client_rect['height']}")
            self._preview_label.setText(f"预览: {target.title}")
            # 防抖：停300ms再截图（避免频繁切换时大量截取）
            self._preview_timer.start()
        else:
            self._preview_label.setText("选中窗口后在此预览截图")
            self._preview_info.setText("")
            self._preview_image.clear()

    def _do_preview(self):
        target = self._get_selected_target()
        if target is None:
            return

        try:
            frame = target.screenshot(client_only=True)
            self._show_frame(frame)
        except Exception as e:
            self._preview_image.setText(f"截图失败: {e}")
            self._preview_image.setStyleSheet(
                "background: #1e1e1e; border: 1px solid #444; color: #c44; padding: 20px;"
            )

    def _show_frame(self, frame: np.ndarray):
        """将 OpenCV BGR 矩阵显示到 QLabel 上。"""
        h, w = frame.shape[:2]
        if h == 0 or w == 0:
            self._preview_image.setText("窗口内容为空")
            return

        # BGR → RGB
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # 缩放适应预览区
        max_w = self._preview_image.width() - 4 or 600
        max_h = self._preview_image.height() - 4 or 250
        scale = min(max_w / w, max_h / h, 1.0)
        if scale < 1.0:
            new_w, new_h = int(w * scale), int(h * scale)
            rgb = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # 转 QPixmap
        h2, w2 = rgb.shape[:2]
        qimg = QImage(rgb.data, w2, h2, w2 * 3, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)

        self._preview_image.setPixmap(pixmap)
        self._preview_image.setStyleSheet("background: #1e1e1e; border: 1px solid #444;")

    # ── 确认 ────────────────────────────────────────────────

    def _get_selected_target(self) -> Optional[Win32Target]:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        hwnd_item = self._table.item(row, self.COL_HWND)
        if hwnd_item is None:
            return None
        hwnd = int(hwnd_item.text())
        for t in self._targets:
            if t.hwnd == hwnd:
                return t
        return None

    def _on_confirm(self):
        target = self._get_selected_target()
        if target is None:
            QMessageBox.warning(self, "提示", "请先选择一个窗口")
            return
        self.selected_target = target
        self.accept()

    # ── 外部调用接口 ─────────────────────────────────────────

    @staticmethod
    def pick(parent=None) -> Optional[Win32Target]:
        """弹出窗口选择器，返回选中的 Win32Target（取消返回 None）。

        用法::

            target = WindowPickerDialog.pick()
            if target:
                frame = target.screenshot()
        """
        dlg = WindowPickerDialog(parent)
        if dlg.exec():
            return dlg.selected_target
        return None


if __name__ == "__main__":


    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    target = WindowPickerDialog.pick()
    if target:
        print(f"\n选中窗口: [{target.title}]")
        print(f"  hwnd:      {target.hwnd}")
        print(f"  类名:      {target.class_name}")
        print(f"  PID:       {target.pid}")
        print(f"  窗口位置:  {target.rect}")
        print(f"  客户区:    {target.client_rect['width']}×{target.client_rect['height']}")
    else:
        print("用户取消了选择")

