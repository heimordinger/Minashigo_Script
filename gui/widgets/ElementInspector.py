# gui/widgets/ElementInspector.py
"""UI 元素检查器 —— 示教模式第一步
加载截图后鼠标悬停，实时高亮光标处的按钮/输入框等 UI 元素。
"""

import cv2
import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QApplication, QComboBox
)
from PySide6.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QIcon
from PySide6.QtCore import Qt, Signal

from core.path import ICON_PATH, IMG_PATH


def _detect_element(frame: np.ndarray, cx: int, cy: int,
                    method: str = "边缘检测") -> dict | None:
    """在截图 (cx,cy) 处检测 UI 元素，返回边界框和属性。"""
    h, w = frame.shape[:2]
    if cx < 0 or cy < 0 or cx >= w or cy >= h:
        return None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    left, top, ew, eh = cx, cy, 1, 1

    if method == "泛洪填充":
        try:
            mask = np.zeros((h + 2, w + 2), np.uint8)
            flags = 4 | (255 << 8) | cv2.FLOODFILL_MASK_ONLY
            result = cv2.floodFill(gray, mask, (cx, cy), 0,
                                   loDiff=25, upDiff=25, flags=flags)
            rect = result[-1]
            lx, ly, rw, rh = rect
            if 8 <= rw <= w // 2 and 8 <= rh <= h // 2:
                left, top, ew, eh = lx, ly, rw, rh
        except Exception:
            pass
    else:  # 边缘检测
        def _find_edge_x(start, step):
            x = start
            while 0 <= x < w - 1:
                nx = x + step
                if abs(int(gray[cy, x]) - int(gray[cy, nx])) > 25:
                    return x
                x = nx
            return start

        def _find_edge_y(start, step):
            y = start
            while 0 <= y < h - 1:
                ny = y + step
                if abs(int(gray[y, cx]) - int(gray[ny, cx])) > 25:
                    return y
                y = ny
            return start

        left = _find_edge_x(cx, -1)
        right = _find_edge_x(cx, 1)
        top = _find_edge_y(cy, -1)
        bottom = _find_edge_y(cy, 1)
        ew, eh = right - left + 1, bottom - top + 1
        if ew < 8 or eh < 8:
            size = 60
            left = max(0, cx - size // 2)
            top = max(0, cy - size // 2)
            ew, eh = min(size, w - left), min(size, h - top)
            right, bottom = left + ew - 1, top + eh - 1

    # 2. 计算区域内颜色特征
    roi = frame[top:top + eh, left:left + ew]
    mean_color = roi.mean(axis=(0, 1))  # BGR

    # 3. 检查区域内是否有文字（用简单轮廓+面积比判断）
    roi_gray = gray[top:top + eh, left:left + ew]
    _, thresh = cv2.threshold(roi_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    has_text = False
    for c in contours:
        area = cv2.contourArea(c)
        if 20 < area < ew * eh * 0.6:
            has_text = True
            break

    return {
        "x": left, "y": top, "w": ew, "h": eh,
        "color": (int(mean_color[2]), int(mean_color[1]), int(mean_color[0])),
        "has_text": has_text,
    }


class ElementInspector(QWidget):
    """UI 元素检查器窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("UI 元素检查器")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(900, 650)

        self._frame: np.ndarray | None = None  # 原始 BGR 帧
        self._pixmap: QPixmap | None = None
        self._element: dict | None = None  # 当前高亮的元素信息

        # 布局
        layout = QVBoxLayout(self)

        # 控制栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("检测方式:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["边缘检测", "泛洪填充"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        bar.addWidget(self.mode_combo)
        bar.addSpacing(16)
        self.load_btn = QPushButton("加载截图")
        self.load_btn.clicked.connect(self._on_load)
        bar.addWidget(self.load_btn)
        bar.addStretch()
        layout.addLayout(bar)

        # 信息栏
        info_row = QHBoxLayout()
        self.info_label = QLabel("加载截图后将鼠标移动到 UI 元素上查看")
        self.pos_label = QLabel("坐标: (-, -)")
        self.pos_label.setStyleSheet("color: #888; font-family: monospace;")
        info_row.addWidget(self.info_label, stretch=1)
        info_row.addWidget(self.pos_label)
        layout.addLayout(info_row)

        # 图片显示区
        self._canvas = _InspectCanvas(self)
        layout.addWidget(self._canvas, stretch=1)

    def _on_mode_changed(self, mode: str):
        self._canvas._element = None
        self._canvas._detect_method = mode
        self._canvas.update()

    def load_frame(self, frame: np.ndarray):
        """注入一副 OpenCV BGR 帧。"""
        self._frame = frame
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg)
        self._canvas._pixmap = self._pixmap
        self._canvas._frame_shape = (h, w)
        self._canvas._element = None
        self._canvas.update()
        self.info_label.setText(f"已加载截图 {w}×{h}")

    def _on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择截图", str(IMG_PATH),
            "图片 (*.png *.jpg *.jpeg *.bmp)"
        )
        if not path:
            return
        data = np.fromfile(path, dtype=np.uint8)
        frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if frame is None:
            self.info_label.setText("无法读取图片")
            return
        self.load_frame(frame)


class _InspectCanvas(QWidget):
    """画布：显示截图 + 鼠标追踪 + 元素高亮"""

    def __init__(self, inspector):
        super().__init__()
        self._inspector = inspector
        self._pixmap: QPixmap | None = None
        self._frame_shape = (0, 0)
        self._element: dict | None = None
        self._mouse_pos: tuple | None = None
        self._detect_method = "边缘检测"
        self.setMouseTracking(True)
        self.setMinimumSize(200, 150)

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._pixmap is None:
            painter.drawText(self.rect(), Qt.AlignCenter, "请加载截图")
            return

        img_w, img_h = self._pixmap.width(), self._pixmap.height()
        scale = min(self.width() / img_w, self.height() / img_h)
        dw, dh = int(img_w * scale), int(img_h * scale)
        ox, oy = (self.width() - dw) // 2, (self.height() - dh) // 2
        painter.drawPixmap(ox, oy, dw, dh, self._pixmap)

        if self._element:
            ex, ey, ew, eh = self._element["x"], self._element["y"], self._element["w"], self._element["h"]
            pen = QPen(QColor(30, 220, 30), 2)
            painter.setPen(pen)
            painter.drawRect(
                int(ex * scale + ox), int(ey * scale + oy),
                int(ew * scale), int(eh * scale)
            )

        if self._mouse_pos:
            mx, my = self._mouse_pos
            ix = int((mx - ox) / scale)
            iy = int((my - oy) / scale)
            pen = QPen(QColor(220, 30, 30), 1)
            painter.setPen(pen)
            painter.drawLine(mx - 15, my, mx + 15, my)
            painter.drawLine(mx, my - 15, mx, my + 15)
            info = f"({ix}, {iy})"
            painter.drawText(mx + 8, my - 8, info)

    def mouseMoveEvent(self, event):
        mx, my = event.position().x(), event.position().y()
        self._mouse_pos = (mx, my)

        if self._pixmap is None:
            self._element = None
            self.update()
            return

        img_w, img_h = self._pixmap.width(), self._pixmap.height()
        scale = min(self.width() / img_w, self.height() / img_h)
        dw, dh = int(img_w * scale), int(img_h * scale)
        ox, oy = (self.width() - dw) // 2, (self.height() - dh) // 2
        ix = int((mx - ox) / scale)
        iy = int((my - oy) / scale)

        frame = self._inspector._frame
        if frame is not None and 0 <= ix < img_w and 0 <= iy < img_h:
            self._inspector.pos_label.setText(f"图片坐标: ({ix}, {iy})")
            try:
                elem = _detect_element(frame, ix, iy, self._detect_method)
                self._element = elem
                if elem:
                    rgb = elem["color"]
                    text = f"元素 ({elem['x']},{elem['y']}) {elem['w']}×{elem['h']}"
                    text += f"  RGB({rgb[0]},{rgb[1]},{rgb[2]})"
                    text += "  [有文字]" if elem["has_text"] else ""
                    self._inspector.info_label.setText(text)
                else:
                    self._inspector.info_label.setText(f"({ix},{iy}) 未检测到 UI 元素")
            except Exception:
                self._element = None
        else:
            self._element = None
            self._inspector.info_label.setText("鼠标移出图片范围")

        self.update()
