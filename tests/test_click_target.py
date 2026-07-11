"""
测试目标窗口 —— 用于验证 Win32Target 后台点击。

窗口上有一个随机移动的按钮，点击后按钮换位置。
显示当前按钮位置，方便与图像匹配结果对比。


WindowPicker 中选择 "后台点击测试" 窗口进行测试。
"""

import sys
import random
from pathlib import Path

# ── 项目根路径（用于 import backend） ──
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QFrame,
)
from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QFont, QPainter, QColor, QPen


class ClickTargetWindow(QWidget):
    """后台点击测试窗口。

    按钮每次被点击后随机换位置，窗口实时显示按钮坐标。
    """

    def __init__(self):
        super().__init__()
        self._click_count = 0
        self._last_click_pos = None  # 记录上次点击位置（窗口客户区坐标）
        self._click_marker_alpha = 0  # 标记透明度（淡出用）
        self._setup_ui()
        self._move_button()

        self.setWindowTitle("后台点击测试")
        self.setFixedSize(500, 400)

    def _setup_ui(self):
        self.setStyleSheet("""
            QWidget { background: #1e1e1e; color: #ccc; font-size: 14px; }
            QPushButton {
                background: #0077b6;
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 16px;
                font-weight: bold;
                padding: 8px 20px;
            }
            QPushButton:hover { background: #00b4d8; }
            QPushButton:pressed { background: #005f8a; }
            QLabel { color: #aaa; }
            QFrame { background: #2d2d2d; border: 1px solid #444; border-radius: 4px; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 标题
        title = QLabel("后台点击测试")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #fff;")
        layout.addWidget(title)

        # 说明
        info = QLabel(
            "用 WindowPicker 选中此窗口 → 截图 → 图像匹配 → 后台点击按钮\n"
            "按钮每次被点击后随机变换位置"
        )
        info.setAlignment(Qt.AlignCenter)
        info.setStyleSheet("color: #888; font-size: 12px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        # ── 按钮区域（带边框，显示点击区域范围） ──
        self._area = QFrame()
        self._area.setMinimumHeight(200)
        self._area_layout = QVBoxLayout(self._area)
        self._area_layout.setContentsMargins(0, 0, 0, 0)
        # 按钮放在一个容器中，用绝对定位
        self._area.setLayout(self._area_layout)
        layout.addWidget(self._area, 1)

        # ── 按钮（绝对定位在 _area 内） ──
        self._btn = QPushButton("点我")
        self._btn.setFixedSize(100, 40)
        self._btn.setParent(self._area)
        self._btn.clicked.connect(self._on_click)
        self._btn.show()

        # ── 底部信息面板 ──
        info_panel = QFrame()
        info_panel.setStyleSheet(
            "QFrame { background: #2d2d2d; border: 1px solid #444; border-radius: 4px; }"
            "QLabel { color: #aaa; padding: 2px; }"
        )
        info_layout = QHBoxLayout(info_panel)
        info_layout.setContentsMargins(12, 8, 12, 8)

        self._pos_label = QLabel("按钮位置: —")
        self._pos_label.setStyleSheet(
            "font-family: Consolas, monospace; color: #00b4d8;"
        )
        self._pos_label.setMinimumWidth(300)
        info_layout.addWidget(self._pos_label)

        self._count_label = QLabel("点击次数: 0")
        self._count_label.setStyleSheet(
            "font-family: Consolas, monospace; color: #faa;"
        )
        self._count_label.setMinimumWidth(200)
        info_layout.addWidget(self._count_label)

        self._status_label = QLabel("等待点击…")
        self._status_label.setAlignment(Qt.AlignRight)
        info_layout.addWidget(self._status_label, 1)

        layout.addWidget(info_panel)

    def _move_button(self):
        """把按钮移到区域内随机位置。"""
        area_w = self._area.width() or 480
        area_h = self._area.height() or 180
        bw, bh = 100, 40

        max_x = max(area_w - bw - 10, 10)
        max_y = max(area_h - bh - 10, 10)
        x = random.randint(10, max_x)
        y = random.randint(10, max_y)

        self._btn.move(x, y)
        self._update_info()

    def _on_click(self):
        """按钮被点击（真实点击或 PostMessage 后台点击均可触发）。"""
        self._click_count += 1

        # 高亮反馈
        self._btn.setStyleSheet(
            "background: #e63946; color: white; border: none; "
            "border-radius: 6px; font-size: 16px; font-weight: bold;"
        )
        QTimer.singleShot(100, self._reset_btn_style)

        # 换位置
        self._move_button()
        self._update_info()

    def _reset_btn_style(self):
        self._btn.setStyleSheet("""
            QPushButton {
                background: #0077b6; color: white; border: none;
                border-radius: 6px; font-size: 16px; font-weight: bold;
            }
            QPushButton:hover { background: #00b4d8; }
            QPushButton:pressed { background: #005f8a; }
        """)

    def _update_info(self):
        # 按钮在窗口客户区中的位置
        btn_window = self._btn.mapTo(self, QPoint(0, 0))
        btn_center = self._btn.mapTo(self, self._btn.rect().center())
        self._pos_label.setText(
            f"按钮 窗口内:({btn_window.x()}, {btn_window.y()})  "
            f"中心:({btn_center.x()}, {btn_center.y()})"
        )
        last = ""
        if self._last_click_pos:
            last = f"  上次点击:({self._last_click_pos.x()}, {self._last_click_pos.y()})"
        self._count_label.setText(f"点击次数: {self._click_count}{last}")
        self._status_label.setText("✓ 点击成功" if self._click_count > 0 else "等待点击…")

    def resizeEvent(self, event):
        """窗口大小变化后重排按钮位置。"""
        super().resizeEvent(event)
        self._move_button()


    # ── 鼠标追踪：捕获所有点击（真实或后台发送） ──

    def mousePressEvent(self, event):
        """捕获窗口内的所有鼠标点击（真实或 PostMessage）。"""
        pos = event.position().toPoint()
        self._last_click_pos = pos
        self._click_marker_alpha = 200

        # 显示点击位置 + 按钮当前位置
        btn_window = self._btn.mapTo(self, QPoint(0, 0))
        print(f"[点击] 窗口坐标: ({pos.x()}, {pos.y()})"
              f"  全局: ({self.mapToGlobal(pos).x()}, {self.mapToGlobal(pos).y()})"
              f"  按钮区域: ({btn_window.x()},{btn_window.y()})~"
              f"({btn_window.x()+self._btn.width()},{btn_window.y()+self._btn.height()})"
              f"  客户区尺寸: ({self.width()},{self.height()})")

        self._update_info()
        self.update()  # 触发 paintEvent 画标记

        # 100ms 后标记淡出
        QTimer.singleShot(100, lambda: self._fade_marker(150))
        QTimer.singleShot(300, lambda: self._fade_marker(80))
        QTimer.singleShot(600, lambda: self._fade_marker(0))

        super().mousePressEvent(event)

    def _fade_marker(self, alpha: int):
        self._click_marker_alpha = alpha
        self.update()

    def paintEvent(self, event):
        """在点击位置画一个红色十字标记。"""
        super().paintEvent(event)
        if self._last_click_pos and self._click_marker_alpha > 5:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(255, 50, 50, self._click_marker_alpha))
            pen.setWidth(2)
            p.setPen(pen)

            x, y = self._last_click_pos.x(), self._last_click_pos.y()
            r = 15  # 十字半径
            p.drawLine(x - r, y, x + r, y)
            p.drawLine(x, y - r, x, y + r)
            # 画圆圈
            p.drawEllipse(x - r, y - r, r * 2, r * 2)
            p.end()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ClickTargetWindow()
    win.show()
    sys.exit(app.exec())
