import cv2
import numpy as np
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QMessageBox, QComboBox, QSpinBox,
                               QDoubleSpinBox, QCheckBox, QLabel, QFileDialog)
from PySide6.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QIcon, QShortcut, QKeySequence
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QRect
from core.coord.viewport_context import viewport_ctx
import math
from backend.matcher.matcher import matcher
import os

from core.path import ICON_PATH, IMG_PATH


class _ToastLabel(QLabel):
    """画布上短暂显示的气泡提示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "QLabel {"
            "  background: rgba(32, 32, 32, 210);"
            "  color: #fff;"
            "  padding: 10px 20px;"
            "  border-radius: 10px;"
            "  font-size: 14px;"
            "}"
        )
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, ms: int = 1000):
        self.setText(text)
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(ms)

    def _reposition(self):
        p = self.parentWidget()
        if p is None:
            return
        self.move(
            max(0, (p.width() - self.width()) // 2),
            max(0, (p.height() - self.height()) // 2),
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition()


class _ImageCanvas(QWidget):
    """截图显示画布，处理绘制和鼠标交互"""
    DRAG_THRESHOLD = 5

    def __init__(self, viewer, image):
        super().__init__()
        self.viewer = viewer
        self.setMouseTracking(True)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(self.sizePolicy().Policy.Expanding, self.sizePolicy().Policy.Expanding)

        if isinstance(image, QImage):
            self.original_pixmap = QPixmap.fromImage(image)
        elif isinstance(image, QPixmap):
            self.original_pixmap = image
        else:
            raise TypeError("image must be QImage or QPixmap")

        self.mark_img_pos = None
        self.mark_css_pos = None
        self.rect_start_pos = None
        self.rect_end_pos = None
        self.active_mode = None
        self.dragging = False
        self.dpr = None
        self.press_img_pos = None

        self.img_w = self.original_pixmap.width()
        self.img_h = self.original_pixmap.height()
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.match_rects = []

        self._toast = _ToastLabel(self)
        self._update_scale_offset()

    def set_pixmap(self, pixmap):
        self.original_pixmap = pixmap
        self.img_w = pixmap.width()
        self.img_h = pixmap.height()
        self.match_rects.clear()
        self.mark_img_pos = None
        self.mark_css_pos = None
        self.rect_start_pos = None
        self.rect_end_pos = None
        self._update_scale_offset()
        self.update()

    def _update_scale_offset(self):
        view_w, view_h = self.width(), self.height()
        img_w, img_h = self.img_w, self.img_h
        if img_w == 0 or img_h == 0 or view_w == 0 or view_h == 0:
            return
        self.scale = min(view_w / img_w, view_h / img_h)
        draw_w, draw_h = img_w * self.scale, img_h * self.scale
        self.offset_x = (view_w - draw_w) / 2
        self.offset_y = (view_h - draw_h) / 2

    def _map_to_image(self, x, y):
        x -= self.offset_x
        y -= self.offset_y
        if x < 0 or y < 0:
            return None
        draw_w, draw_h = self.img_w * self.scale, self.img_h * self.scale
        if x > draw_w or y > draw_h:
            return None
        return x / self.scale, y / self.scale

    def _adjust_text_pos(self, x, y, text_width=100, text_height=20, padding=5):
        pos_x = x + padding
        pos_y = y - padding

        if pos_x + text_width > self.width():
            pos_x = x - text_width - padding
        if pos_x < 0:
            pos_x = padding
        if pos_y < 0:
            pos_y = y + padding + text_height
        if pos_y + text_height > self.height():
            pos_y = self.height() - text_height - padding

        return pos_x, pos_y

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        draw_w, draw_h = int(self.img_w * self.scale), int(self.img_h * self.scale)
        ox, oy = int(self.offset_x), int(self.offset_y)
        painter.drawPixmap(ox, oy, draw_w, draw_h, self.original_pixmap)

        # 有匹配结果时：压暗非匹配区域，匹配块保持原色 + 绿框
        if self.match_rects:
            painter.fillRect(ox, oy, draw_w, draw_h, QColor(0, 0, 0, 150))
            for match in self.match_rects:
                match_x, match_y, width, height, _score = match
                sx = max(0, int(match_x))
                sy = max(0, int(match_y))
                sw = max(0, int(width))
                sh = max(0, int(height))
                if sw <= 0 or sh <= 0:
                    continue
                if sx >= self.img_w or sy >= self.img_h:
                    continue
                sw = min(sw, self.img_w - sx)
                sh = min(sh, self.img_h - sy)
                dest = QRect(
                    int(sx * self.scale + self.offset_x),
                    int(sy * self.scale + self.offset_y),
                    int(sw * self.scale),
                    int(sh * self.scale),
                )
                painter.drawPixmap(dest, self.original_pixmap, QRect(sx, sy, sw, sh))

        self.dpr = viewport_ctx.get_dpr(account=self.viewer.account)
        for match in self.match_rects:
            match_x, match_y, width, height, score = match
            painter.setPen(QPen(QColor(30, 220, 30), 2))
            painter.drawRect(match_x * self.scale + self.offset_x, match_y * self.scale + self.offset_y,
                             width * self.scale, height * self.scale)
            painter.setPen(QPen(QColor(30, 220, 30), 2))
            painter.drawText(match_x * self.scale + self.offset_x + width * self.scale + 5,
                             match_y * self.scale + self.offset_y, f"匹配度: {score:.2f}")
        if self.rect_start_pos and self.rect_end_pos:
            start_x, start_y = self.rect_start_pos
            end_x, end_y = self.rect_end_pos

            rect_width = end_x - start_x
            rect_height = end_y - start_y

            painter.setPen(QPen(QColor(220, 30, 30), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(start_x * self.scale + self.offset_x,
                             start_y * self.scale + self.offset_y,
                             rect_width * self.scale,
                             rect_height * self.scale)
            painter.setPen(QPen(QColor(220, 30, 30), 2))
            start_text = f"({int(start_x)}, {int(start_y)})"
            end_text = f"({int(end_x)}, {int(end_y)})"
            painter.drawText(start_x * self.scale + self.offset_x, start_y * self.scale + self.offset_y - 10,
                             start_text)
            painter.drawText(end_x * self.scale + self.offset_x, end_y * self.scale + self.offset_y + 10, end_text)
        if self.active_mode == "point" and self.mark_img_pos:
            img_x, img_y = self.mark_img_pos
            view_x = img_x * self.scale + self.offset_x
            view_y = img_y * self.scale + self.offset_y
            pen = QPen(QColor(220, 30, 30))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(QColor(220, 30, 30))
            radius = 4
            painter.drawEllipse(int(view_x - radius), int(view_y - radius), radius * 2, radius * 2)
            css_x, css_y = self.mark_css_pos
            text_x, text_y = self._adjust_text_pos(view_x + 6, view_y - 6)
            painter.drawText(int(text_x), int(text_y), f"({int(css_x)},{int(css_y)})")

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        img_pos = self._map_to_image(event.position().x(), event.position().y())
        if not img_pos:
            return
        self.press_img_pos = img_pos
        self.dragging = True
        self.rect_start_pos = img_pos
        self.rect_end_pos = None

    def mouseMoveEvent(self, event):
        if not self.dragging:
            return

        img_pos = self._map_to_image(event.position().x(), event.position().y())
        if not img_pos:
            return
        dx = img_pos[0] - self.press_img_pos[0]
        dy = img_pos[1] - self.press_img_pos[1]
        distance = math.hypot(dx, dy)

        if distance >= self.DRAG_THRESHOLD:
            self.rect_end_pos = img_pos
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return

        img_pos = self._map_to_image(event.position().x(), event.position().y())
        if not img_pos:
            self.dragging = False
            return
        if self.rect_start_pos and self.rect_end_pos:
            self.mark_img_pos = None
            self.mark_css_pos = None
        else:
            dpr = self.dpr
            css_x, css_y = img_pos[0] / dpr, img_pos[1] / dpr
            self.mark_img_pos = img_pos
            self.mark_css_pos = (css_x, css_y)
            self.rect_start_pos = None
            self.rect_end_pos = None
            self.active_mode = "point"

        self.dragging = False
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scale_offset()
        if self._toast.isVisible():
            self._toast._reposition()
        self.update()

    def show_toast(self, text: str, ms: int = 1000):
        self._toast.show_message(text, ms)


class ScreenshotViewer(QWidget):
    DRAG_THRESHOLD = 5
    refresh_requested = Signal()

    def __init__(self, *, image, account: dict):
        super().__init__()
        self.account = account
        self.setWindowTitle("截图预览")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1200, 600)
        self.setMinimumSize(400, 300)
        self.refresh_shortcut = QShortcut(QKeySequence(Qt.Key_F5), self)
        self.refresh_shortcut.activated.connect(self._emit_refresh)

        # 主布局：控制栏固定在上方，画布填充剩余空间
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)

        # ====== 控制栏 ======
        control_widget = QWidget()
        control_widget.setObjectName("ControlBar")
        control_widget.setStyleSheet("#ControlBar { background: #f0f0f0; border-bottom: 1px solid #ccc; }")
        control_layout = QHBoxLayout(control_widget)
        control_layout.setContentsMargins(4, 4, 4, 4)

        self.match_type_combo = QComboBox(self)
        self.match_type_combo.addItems(["图片匹配", "像素匹配", "文字匹配"])
        self.match_type_combo.setCurrentIndex(0)
        self.match_type_combo.currentTextChanged.connect(self.on_match_type_changed)
        control_layout.addWidget(QLabel("匹配类型:"))
        control_layout.addWidget(self.match_type_combo)
        control_layout.addWidget(QLabel("目标:"))

        self.target_input = QLineEdit(self)
        self.target_input.setPlaceholderText("输入或选择目标图片路径")
        control_layout.addWidget(self.target_input)
        self.select_img_btn = QPushButton("选择图片")
        self.select_img_btn.clicked.connect(self.on_select_image)
        control_layout.addWidget(self.select_img_btn)
        self.clear_img_btn = QPushButton("清空")
        self.clear_img_btn.clicked.connect(lambda: self.target_input.clear())
        control_layout.addWidget(self.clear_img_btn)
        self.threshold_input = QDoubleSpinBox(self)
        self.threshold_input.setRange(0.0, 1.0)
        self.threshold_input.setSingleStep(0.05)
        self.threshold_input.setValue(0.9)
        self.threshold_input.setToolTip("图片匹配:0~1, 文字匹配:0~100")
        control_layout.addWidget(QLabel("最低匹配度:"))
        control_layout.addWidget(self.threshold_input)
        self.color_check = QCheckBox("颜色一致", self)
        self.color_check.setChecked(False)
        control_layout.addWidget(self.color_check)
        self.region_check = QCheckBox("区域匹配", self)
        self.region_check.setChecked(False)
        control_layout.addWidget(self.region_check)
        self.match_button = QPushButton("开始匹配", self)
        self.clear_button = QPushButton("清除", self)
        self.screenshot_refresh_btn = QPushButton("重新获取", self)
        self.save_btn = QPushButton("保存截图", self)
        control_layout.addWidget(self.match_button)
        control_layout.addWidget(self.clear_button)
        control_layout.addWidget(self.screenshot_refresh_btn)
        control_layout.addWidget(self.save_btn)
        layout.addWidget(control_widget)

        self.match_button.clicked.connect(self.on_match_button_click)
        self.clear_button.clicked.connect(self.on_clear_click)
        self.screenshot_refresh_btn.clicked.connect(self._emit_refresh)
        self.save_btn.clicked.connect(self.on_save_click)

        # ====== 截图画布 ======
        self.canvas = _ImageCanvas(self, image)
        layout.addWidget(self.canvas, stretch=1)

        self.on_match_type_changed(self.match_type_combo.currentText())

    def update_image(self, pixmap):
        """外部更新截图内容"""
        self.canvas.set_pixmap(pixmap)

    @Slot(str)
    def on_match_type_changed(self, text):
        if text == "图片匹配":
            self.target_input.setPlaceholderText("输入目标图片路径")
            self.threshold_input.setRange(0.0, 1.0)
            self.threshold_input.setValue(0.9)
            self.threshold_input.setToolTip("多尺度模板相似度阈值 (0~1)")
            self.color_check.setEnabled(True)
            self.color_check.setVisible(True)
        elif text == "像素匹配":
            self.target_input.setPlaceholderText("输入目标图片路径（须与运行帧同缩放）")
            self.threshold_input.setRange(0.0, 1.0)
            self.threshold_input.setValue(0.98)
            self.threshold_input.setToolTip(
                "像素级相似度 (0~1)；1:1 不缩放，建议 ≥0.95"
            )
            self.color_check.setChecked(False)
            self.color_check.setEnabled(False)
            self.color_check.setVisible(True)
        else:
            self.target_input.setPlaceholderText("输入要匹配的文字")
            self.threshold_input.setRange(0.0, 100.0)
            self.threshold_input.setValue(60.0)
            self.threshold_input.setToolTip("文字匹配置信度阈值 (0~100)")
            self.color_check.setChecked(False)
            self.color_check.setEnabled(False)
            self.color_check.setVisible(True)

    def on_match_button_click(self):
        match_type = self.match_type_combo.currentText()
        target_text = self.target_input.text().strip()
        threshold = self.threshold_input.value()
        use_color = self.color_check.isChecked() and match_type == "图片匹配"
        use_region = self.region_check.isChecked()

        if match_type in ("图片匹配", "像素匹配") and not target_text:
            self.show_error_message("输入无效", "请输入目标图片路径")
            return
        if match_type == "文字匹配":
            self.show_error_message("提示", "截图预览暂仅支持图片/像素匹配")
            return
        qimg = self.canvas.original_pixmap.toImage()
        buf = qimg.bits().tobytes()
        full_img = np.frombuffer(buf, np.uint8).reshape(
            qimg.height(), qimg.width(), 4
        )[:, :, :3]

        roi_offset_x = 0
        roi_offset_y = 0
        crop_top_left = None
        crop_bottom_right = None
        if use_region:
            if not (self.canvas.rect_start_pos and self.canvas.rect_end_pos):
                self.show_error_message("未绘制区域", "请先在截图上绘制矩形区域")
                return
            crop_top_left = (int(self.canvas.rect_start_pos[0]), int(self.canvas.rect_start_pos[1]))
            crop_bottom_right = (int(self.canvas.rect_end_pos[0]), int(self.canvas.rect_end_pos[1]))
        if not os.path.exists(target_text):
            self.show_error_message("图片路径无效", "模板图片不存在")
            return

        def imread_unicode(path):
            data = np.fromfile(path, dtype=np.uint8)
            if data.size == 0:
                return None
            return cv2.imdecode(data, cv2.IMREAD_COLOR)

        template_bgr = imread_unicode(target_text)
        if template_bgr is None:
            self.show_error_message("模板错误", "无法读取模板图片")
            return

        mtype = "pixel_multi" if match_type == "像素匹配" else "image_multi"
        try:
            results = matcher.match(
                target=full_img,
                template=template_bgr,
                match_type=mtype,
                threshold=threshold,
                use_color_check=use_color,
                color_tol=30.0 if use_color else 30.0,
                crop_top_left=crop_top_left,
                crop_bottom_right=crop_bottom_right,
                use_orb=(match_type == "图片匹配"),
                pixel_tol=8.0,
            )
        except Exception as e:
            self.show_error_message("匹配出错", str(e))
            return

        self.canvas.match_rects.clear()

        if not results:
            self.canvas.update()
            self.canvas.show_toast("匹配到 0 项", 1000)
            return

        h, w = template_bgr.shape[:2]

        for r in results:
            self.canvas.match_rects.append(
                (
                    int(r['x'] - w / 2 + roi_offset_x),
                    int(r['y'] - h / 2 + roi_offset_y),
                    w,
                    h,
                    r['score']
                )
            )

        self.canvas.update()
        self.canvas.show_toast(f"匹配到 {len(results)} 项", 1000)

    def _emit_refresh(self):
        self.refresh_requested.emit()

    def on_save_click(self):
        from core.path import PROJECT_ROOT
        name = self.account.get('name', 'unknown')
        save_dir = PROJECT_ROOT / "screenshots" / name
        save_dir.mkdir(parents=True, exist_ok=True)
        file_path = save_dir / f"screenshot_{int(__import__('time').time())}.png"
        self.canvas.original_pixmap.save(str(file_path))

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle("保存成功")
        msg.setText(f"截图已保存\n{file_path}")
        msg.exec()

    def on_clear_click(self):
        self.canvas.match_rects = []
        self.canvas.mark_img_pos = None
        self.canvas.mark_css_pos = None
        self.canvas.rect_start_pos = None
        self.canvas.rect_end_pos = None
        self.canvas.update()
        self._update_title_size()

    def _update_title_size(self):
        self.setWindowTitle(f"截图预览 ({self.width()}×{self.height()})")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_title_size()

    def show_error_message(self, title, message):
        """弹窗显示错误信息"""
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle(title)
        msg.setText(message)
        msg.exec()

    def on_select_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择模板图片",
            str(IMG_PATH),
            "Images (*.png *.jpg *.jpeg *.bmp)"
        )

        if not file_path:
            return

        self.target_input.setText(file_path)
