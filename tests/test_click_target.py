"""
测试目标窗口 —— 用于验证 Win32Target 后台点击。

窗口上有一个随机移动的按钮，点击后按钮换位置。
增加「中心判定」：对比落点与按钮中心的 Δx/Δy/距离。

WindowPicker 中选择 "后台点击测试" 窗口进行测试。
"""

import sys
import math
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
from PySide6.QtCore import Qt, QTimer, QPoint, QObject, QEvent
from PySide6.QtGui import QPainter, QColor, QPen


class ClickTargetWindow(QWidget):
    """后台点击测试窗口。

    按钮每次被点击后随机换位置，并显示相对按钮中心的点击偏差。
    """

    def __init__(self):
        super().__init__()
        self._click_count = 0
        self._last_click_pos = None  # 客户区坐标
        self._last_center_pos = None
        self._last_dx = None
        self._last_dy = None
        self._last_dist = None
        self._click_marker_alpha = 0
        self._err_samples: list[float] = []  # 历史距离（像素）
        self._setup_ui()
        self._move_button()

        self.setWindowTitle("后台点击测试")
        self.setFixedSize(560, 440)

        # 按钮上的点击也要拿到坐标（PostMessage 常直接打到子控件）
        self._btn.installEventFilter(self)

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
        layout.setSpacing(10)

        title = QLabel("后台点击测试")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #fff;")
        layout.addWidget(title)

        info = QLabel(
            "WindowPicker 选此窗 → 截图/匹配/后台点击「点我」\n"
            "中心判定：落点相对按钮中心的 Δx/Δy/距离（像素）"
        )
        info.setAlignment(Qt.AlignCenter)
        info.setStyleSheet("color: #888; font-size: 12px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        self._area = QFrame()
        self._area.setMinimumHeight(200)
        self._area_layout = QVBoxLayout(self._area)
        self._area_layout.setContentsMargins(0, 0, 0, 0)
        self._area.setLayout(self._area_layout)
        layout.addWidget(self._area, 1)

        self._btn = QPushButton("点我")
        self._btn.setFixedSize(100, 40)
        self._btn.setParent(self._area)
        self._btn.clicked.connect(self._on_btn_clicked)
        self._btn.show()

        # ── 中心判定面板 ──
        judge = QFrame()
        judge.setStyleSheet(
            "QFrame { background: #252525; border: 1px solid #555; border-radius: 4px; }"
            "QLabel { color: #ccc; padding: 1px; font-family: Consolas, monospace; }"
        )
        jl = QVBoxLayout(judge)
        jl.setContentsMargins(12, 8, 12, 8)
        jl.setSpacing(2)

        self._center_label = QLabel("按钮中心: —")
        self._center_label.setStyleSheet("color: #00e5a0;")
        jl.addWidget(self._center_label)

        self._hit_label = QLabel("落点: —")
        self._hit_label.setStyleSheet("color: #ff6b6b;")
        jl.addWidget(self._hit_label)

        self._delta_label = QLabel("偏差: —")
        self._delta_label.setStyleSheet(
            "color: #ffd166; font-size: 15px; font-weight: bold;"
        )
        jl.addWidget(self._delta_label)

        self._stats_label = QLabel("统计: 尚无样本")
        self._stats_label.setStyleSheet("color: #9aa;")
        jl.addWidget(self._stats_label)

        layout.addWidget(judge)

        # ── 底部信息 ──
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
        info_layout.addWidget(self._pos_label)

        self._count_label = QLabel("点击次数: 0")
        self._count_label.setStyleSheet(
            "font-family: Consolas, monospace; color: #faa;"
        )
        info_layout.addWidget(self._count_label)

        self._status_label = QLabel("等待点击…")
        self._status_label.setAlignment(Qt.AlignRight)
        info_layout.addWidget(self._status_label, 1)

        layout.addWidget(info_panel)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._btn and event.type() == QEvent.Type.MouseButtonPress:
            # 按钮局部坐标 → 窗口客户区
            local = event.position().toPoint()  # type: ignore[attr-defined]
            global_on_btn = self._btn.mapTo(self, local)
            self._record_hit(global_on_btn)
        return super().eventFilter(obj, event)

    def _btn_center_in_window(self) -> QPoint:
        return self._btn.mapTo(self, self._btn.rect().center())

    def _record_hit(self, pos: QPoint):
        """记录落点并相对按钮中心算偏差（在按钮换位之前调用）。"""
        center = self._btn_center_in_window()
        dx = pos.x() - center.x()
        dy = pos.y() - center.y()
        dist = math.hypot(dx, dy)

        self._last_click_pos = QPoint(pos)
        self._last_center_pos = QPoint(center)
        self._last_dx = dx
        self._last_dy = dy
        self._last_dist = dist
        self._err_samples.append(dist)
        self._click_marker_alpha = 220

        print(
            f"[中心判定] 落点=({pos.x()},{pos.y()})  "
            f"中心=({center.x()},{center.y()})  "
            f"Δ=({dx:+d},{dy:+d})  距离={dist:.1f}px"
        )

        self._refresh_judge_labels()
        self.update()
        QTimer.singleShot(120, lambda: self._fade_marker(150))
        QTimer.singleShot(350, lambda: self._fade_marker(80))
        QTimer.singleShot(700, lambda: self._fade_marker(0))

    def _refresh_judge_labels(self):
        if self._last_center_pos is None or self._last_click_pos is None:
            return
        c = self._last_center_pos
        p = self._last_click_pos
        self._center_label.setText(f"按钮中心: ({c.x()}, {c.y()})")
        self._hit_label.setText(f"落点: ({p.x()}, {p.y()})")
        self._delta_label.setText(
            f"偏差 Δx={self._last_dx:+d}  Δy={self._last_dy:+d}  "
            f"距离={self._last_dist:.1f}px"
        )
        if self._err_samples:
            n = len(self._err_samples)
            mean = sum(self._err_samples) / n
            mx = max(self._err_samples)
            mn = min(self._err_samples)
            self._stats_label.setText(
                f"统计 n={n}  平均={mean:.1f}px  最小={mn:.1f}px  最大={mx:.1f}px"
            )

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

    def _on_btn_clicked(self):
        """按钮 clicked 信号（真实或后台点击触发）。"""
        self._click_count += 1
        self._btn.setStyleSheet(
            "background: #e63946; color: white; border: none; "
            "border-radius: 6px; font-size: 16px; font-weight: bold;"
        )
        QTimer.singleShot(100, self._reset_btn_style)
        # 若 Press 已记过落点则保留判定；否则用中心近似（少数路径无 Press）
        if self._last_click_pos is None:
            self._record_hit(self._btn_center_in_window())
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
        btn_window = self._btn.mapTo(self, QPoint(0, 0))
        btn_center = self._btn_center_in_window()
        self._pos_label.setText(
            f"按钮 左上:({btn_window.x()}, {btn_window.y()})  "
            f"中心:({btn_center.x()}, {btn_center.y()})"
        )
        self._count_label.setText(f"点击次数: {self._click_count}")
        self._status_label.setText("✓ 点击成功" if self._click_count > 0 else "等待点击…")
        # 换位后刷新「当前中心」提示（上一击偏差仍保留在判定栏）
        self._center_label.setText(
            f"按钮中心(当前): ({btn_center.x()}, {btn_center.y()})"
            + (
                f"  | 上一击中心: ({self._last_center_pos.x()}, {self._last_center_pos.y()})"
                if self._last_center_pos
                else ""
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._move_button()

    def mousePressEvent(self, event):
        """点在按钮外的客户区（或未被子控件吃掉的消息）。"""
        pos = event.position().toPoint()
        self._record_hit(pos)
        btn_window = self._btn.mapTo(self, QPoint(0, 0))
        print(
            f"[点击] 窗口坐标: ({pos.x()}, {pos.y()})"
            f"  按钮区域: ({btn_window.x()},{btn_window.y()})~"
            f"({btn_window.x() + self._btn.width()},{btn_window.y() + self._btn.height()})"
            f"  客户区: ({self.width()},{self.height()})"
        )
        self._update_info()
        super().mousePressEvent(event)

    def _fade_marker(self, alpha: int):
        self._click_marker_alpha = alpha
        self.update()

    def paintEvent(self, event):
        """画按钮中心（青）与落点（红），并连线。"""
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 始终标出当前按钮中心（淡青）
        cur = self._btn_center_in_window()
        pen_c = QPen(QColor(0, 220, 160, 160))
        pen_c.setWidth(1)
        p.setPen(pen_c)
        r0 = 8
        p.drawLine(cur.x() - r0, cur.y(), cur.x() + r0, cur.y())
        p.drawLine(cur.x(), cur.y() - r0, cur.x(), cur.y() + r0)

        if self._last_click_pos and self._click_marker_alpha > 5:
            a = self._click_marker_alpha
            # 上一击时的中心
            if self._last_center_pos:
                cx, cy = self._last_center_pos.x(), self._last_center_pos.y()
                pen_old = QPen(QColor(0, 200, 255, a))
                pen_old.setWidth(2)
                p.setPen(pen_old)
                r = 12
                p.drawLine(cx - r, cy, cx + r, cy)
                p.drawLine(cx, cy - r, cx, cy + r)
                p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

                # 连线
                pen_line = QPen(QColor(255, 200, 80, a))
                pen_line.setWidth(1)
                pen_line.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen_line)
                p.drawLine(cx, cy, self._last_click_pos.x(), self._last_click_pos.y())

            x, y = self._last_click_pos.x(), self._last_click_pos.y()
            pen = QPen(QColor(255, 50, 50, a))
            pen.setWidth(2)
            p.setPen(pen)
            r = 15
            p.drawLine(x - r, y, x + r, y)
            p.drawLine(x, y - r, x, y + r)
            p.drawEllipse(x - r, y - r, r * 2, r * 2)

            if self._last_dist is not None:
                p.setPen(QColor(255, 209, 102, a))
                p.drawText(
                    x + 18,
                    y - 10,
                    f"Δ({self._last_dx:+d},{self._last_dy:+d}) {self._last_dist:.1f}px",
                )
        p.end()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ClickTargetWindow()
    win.show()
    sys.exit(app.exec())
