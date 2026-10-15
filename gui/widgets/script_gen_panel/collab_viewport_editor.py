"""共同视口大图标记窗口：编辑独立覆盖层，不改原图。"""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class ViewportMarkCanvas(QLabel):
    """按原图归一化坐标绘制矩形和笔迹。"""

    changed = Signal()
    zoom_step_requested = Signal(int)

    def __init__(self, image_path: Path, marks: list[dict], parent=None):
        super().__init__(parent)
        self._source = QPixmap(str(image_path))
        self._marks = [dict(m) for m in marks if isinstance(m, dict)]
        self._mode = "rect"
        self._content_rect = QRect()
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._stroke: list[QPoint] = []
        self._zoom = 1.0
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._source.isNull():
            self.setFixedSize(320, 240)
        else:
            # 双击查看的权威尺度是原图像素。小素材不放大，大截图不缩小；
            # 超出窗口的部分交给外层 QScrollArea，而不是改变坐标系。
            self.setFixedSize(self._source.size())
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ("rect", "brush") else "rect"
        self.setCursor(Qt.CursorShape.CrossCursor)

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        zoom = max(0.25, min(8.0, float(zoom or 1.0)))
        if abs(zoom - self._zoom) < 1e-9:
            return
        self._zoom = zoom
        if not self._source.isNull():
            self.setFixedSize(
                max(1, int(round(self._source.width() * zoom))),
                max(1, int(round(self._source.height() * zoom))),
            )
        self.update()

    def marks(self) -> list[dict]:
        return [dict(m) for m in self._marks]

    def undo(self) -> None:
        if self._marks:
            self._marks.pop()
            self.changed.emit()
            self.update()

    def clear_marks(self) -> None:
        if self._marks:
            self._marks.clear()
            self.changed.emit()
            self.update()

    def _fit_rect(self) -> QRect:
        if self._source.isNull():
            return QRect()
        return QRect(0, 0, self.width(), self.height())

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta:
            self.zoom_step_requested.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def _norm_point(self, point: QPoint) -> list[float]:
        rect = self._content_rect
        return [
            max(0.0, min(1.0, (point.x() - rect.x()) / max(1, rect.width()))),
            max(0.0, min(1.0, (point.y() - rect.y()) / max(1, rect.height()))),
        ]

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._content_rect.contains(event.position().toPoint()):
            return super().mousePressEvent(event)
        self._origin = event.position().toPoint()
        self._current = self._origin
        self._stroke = [self._origin] if self._mode == "brush" else []
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._origin is None:
            return super().mouseMoveEvent(event)
        point = event.position().toPoint()
        x = max(self._content_rect.left(), min(self._content_rect.right(), point.x()))
        y = max(self._content_rect.top(), min(self._content_rect.bottom(), point.y()))
        self._current = QPoint(x, y)
        if self._mode == "brush":
            self._stroke.append(self._current)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None or self._current is None:
            return super().mouseReleaseEvent(event)
        if self._mode == "rect":
            a, b = self._norm_point(self._origin), self._norm_point(self._current)
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            if x1 - x0 >= 0.005 and y1 - y0 >= 0.005:
                self._marks.append(
                    {
                        "id": f"mark_{uuid.uuid4().hex[:8]}",
                        "kind": "mark",
                        "shape": "rect",
                        "label": f"矩形标记{len(self._marks) + 1}",
                        "nx0": x0,
                        "ny0": y0,
                        "nx1": x1,
                        "ny1": y1,
                        "color": "#f0a33b",
                    }
                )
        elif len(self._stroke) >= 2:
            points: list[list[float]] = []
            for point in self._stroke:
                norm = self._norm_point(point)
                if not points or abs(norm[0] - points[-1][0]) + abs(norm[1] - points[-1][1]) > 0.002:
                    points.append(norm)
            if len(points) >= 2:
                self._marks.append(
                    {
                        "id": f"mark_{uuid.uuid4().hex[:8]}",
                        "kind": "mark",
                        "shape": "stroke",
                        "label": f"画笔标记{len(self._marks) + 1}",
                        "points": points,
                        "color": "#f0a33b",
                    }
                )
        self._origin = self._current = None
        self._stroke = []
        self.changed.emit()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        self._content_rect = self._fit_rect()
        if self._source.isNull() or self._content_rect.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawPixmap(self._content_rect, self._source)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._draw_marks(painter, self._marks)
        if self._origin is not None and self._current is not None:
            pen = QPen(QColor("#f0a33b"), 3 if self._mode == "brush" else 2)
            pen.setStyle(Qt.PenStyle.SolidLine if self._mode == "brush" else Qt.PenStyle.DashLine)
            painter.setPen(pen)
            if self._mode == "rect":
                painter.drawRect(QRect(self._origin, self._current).normalized())
            else:
                for a, b in zip(self._stroke, self._stroke[1:]):
                    painter.drawLine(a, b)
        painter.end()

    def _draw_marks(self, painter: QPainter, marks: list[dict]) -> None:
        rect = self._content_rect
        for mark in marks:
            pen = QPen(QColor(str(mark.get("color") or "#f0a33b")), 3)
            painter.setPen(pen)
            if mark.get("shape") == "rect":
                x0 = rect.x() + int(float(mark.get("nx0") or 0) * rect.width())
                y0 = rect.y() + int(float(mark.get("ny0") or 0) * rect.height())
                x1 = rect.x() + int(float(mark.get("nx1") or 0) * rect.width())
                y1 = rect.y() + int(float(mark.get("ny1") or 0) * rect.height())
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawRect(QRect(QPoint(x0, y0), QPoint(x1, y1)).normalized())
            elif mark.get("shape") == "stroke":
                points = mark.get("points") or []
                for a, b in zip(points, points[1:]):
                    painter.drawLine(
                        QPoint(rect.x() + int(float(a[0]) * rect.width()), rect.y() + int(float(a[1]) * rect.height())),
                        QPoint(rect.x() + int(float(b[0]) * rect.width()), rect.y() + int(float(b[1]) * rect.height())),
                    )


class CollabViewportEditor(QDialog):
    """双击共同视口后打开的 1:1 原图标记窗口。"""

    def __init__(self, image_path: Path, marks: list[dict], parent=None):
        super().__init__(parent)
        self._discard_requested = False
        self._zoom_levels = (0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)
        self._zoom_index = self._zoom_levels.index(1.0)
        self.setWindowTitle(f"共同视口标记 · {image_path.name}")
        self.setModal(True)
        self.resize(1100, 760)
        root = QVBoxLayout(self)
        tools = QHBoxLayout()
        self._rect = QPushButton("矩形框")
        self._brush = QPushButton("自由画笔")
        self._undo = QPushButton("撤销")
        self._clear = QPushButton("清除标记")
        self._reset_zoom = QPushButton("原始尺寸")
        self._zoom_label = QLabel("100%")
        self._zoom_label.setMinimumWidth(48)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tools.addWidget(self._rect)
        tools.addWidget(self._brush)
        tools.addWidget(self._undo)
        tools.addWidget(self._clear)
        tools.addWidget(self._reset_zoom)
        tools.addWidget(self._zoom_label)
        tools.addStretch(1)
        hint = QLabel("1:1 原图像素 · 可滚动查看 · 标记保存在独立覆盖层")
        tools.addWidget(hint)
        root.addLayout(tools)

        self.canvas = ViewportMarkCanvas(image_path, marks, self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.canvas)
        root.addWidget(self.scroll, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        cancel = QPushButton("放弃本次修改")
        save = QPushButton("保存并关闭")
        save.setDefault(True)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        root.addLayout(bottom)

        self._rect.clicked.connect(lambda: self._set_mode("rect"))
        self._brush.clicked.connect(lambda: self._set_mode("brush"))
        self._undo.clicked.connect(self.canvas.undo)
        self._clear.clicked.connect(self.canvas.clear_marks)
        self._reset_zoom.clicked.connect(self.reset_zoom)
        self.canvas.zoom_step_requested.connect(self._step_zoom)
        cancel.clicked.connect(self.discard_changes)
        save.clicked.connect(self.accept)
        self._set_mode("rect")

    def _step_zoom(self, direction: int) -> None:
        if not direction:
            return
        new_index = max(0, min(len(self._zoom_levels) - 1, self._zoom_index + (1 if direction > 0 else -1)))
        self._set_zoom_index(new_index)

    def _set_zoom_index(self, index: int) -> None:
        index = max(0, min(len(self._zoom_levels) - 1, int(index)))
        if index == self._zoom_index and abs(self.canvas.zoom() - self._zoom_levels[index]) < 1e-9:
            self._zoom_label.setText(f"{round(self._zoom_levels[index] * 100)}%")
            return
        hbar = self.scroll.horizontalScrollBar()
        vbar = self.scroll.verticalScrollBar()
        viewport = self.scroll.viewport()
        old_w = max(1, self.canvas.width())
        old_h = max(1, self.canvas.height())
        center_x = (hbar.value() + viewport.width() / 2) / old_w
        center_y = (vbar.value() + viewport.height() / 2) / old_h
        self._zoom_index = index
        zoom = self._zoom_levels[index]
        self.canvas.set_zoom(zoom)
        self._zoom_label.setText(f"{round(zoom * 100)}%")

        def restore_center() -> None:
            hbar.setValue(int(center_x * self.canvas.width() - viewport.width() / 2))
            vbar.setValue(int(center_y * self.canvas.height() - viewport.height() / 2))

        QTimer.singleShot(0, restore_center)

    def reset_zoom(self) -> None:
        self._set_zoom_index(self._zoom_levels.index(1.0))

    def _set_mode(self, mode: str) -> None:
        self.canvas.set_mode(mode)
        self._rect.setProperty("active", mode == "rect")
        self._brush.setProperty("active", mode == "brush")
        for button in (self._rect, self._brush):
            button.style().unpolish(button)
            button.style().polish(button)

    def marks(self) -> list[dict]:
        return self.canvas.marks()

    def discard_changes(self) -> None:
        """只有明确点“放弃”才不保存；普通关闭和 Esc 都默认提交。"""
        self._discard_requested = True
        super().reject()

    def reject(self) -> None:
        """QDialog 的右上角关闭与 Esc 会走 reject；这里改为默认保存。"""
        if self._discard_requested:
            super().reject()
        else:
            self.accept()
