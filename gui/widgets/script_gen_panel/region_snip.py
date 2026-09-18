"""区域截图：全屏冻结画面 + 半透明暗幕拖拽选区（同快速脚本固定帧交互）。

高 DPI 注意：
- 鼠标 / 选框用逻辑像素（与 QWidget 一致）
- grabWindow 得到的底图含 devicePixelRatio，底层缓冲是物理像素
- 裁切必须按 dpr 映射到物理像素，不能直接用逻辑矩形去 copy（否则框与结果错位）
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, QTimer, QPoint
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPen,
    QPixmap,
    QKeySequence,
    QShortcut,
    QScreen,
)
from PySide6.QtWidgets import QApplication, QWidget

from core.path import PROJECT_ROOT


class RegionSnipOverlay(QWidget):
    """全屏暗幕选区。完成后发出 captured(QPixmap)；取消发出 cancelled。"""

    captured = Signal(QPixmap)
    cancelled = Signal()

    def __init__(self, background: QPixmap, screen: QScreen, parent=None):
        super().__init__(parent)
        self.setObjectName("RegionSnipOverlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._screen = screen
        self._bg = background
        self._dpr = float(background.devicePixelRatio() or screen.devicePixelRatio() or 1.0)
        # 逻辑尺寸以底图为准（与绘制、裁切同一套坐标）
        self._logical_w = max(1, int(background.width()))
        self._logical_h = max(1, int(background.height()))

        geo = screen.geometry()
        # 窗口尺寸跟底图逻辑尺寸对齐，避免 geometry 与 grab 差 1～2px 或 DPR 取整误差
        self.setGeometry(geo.x(), geo.y(), self._logical_w, self._logical_h)

        self._drag_start: Optional[tuple[int, int]] = None
        self._drag_end: Optional[tuple[int, int]] = None
        self._finished = False

        tip = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        tip.activated.connect(self._cancel)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 按逻辑像素 1:1 画底图，避免 stretch 导致「看见的框」和「裁到的图」错位
        if not self._bg.isNull():
            painter.drawPixmap(0, 0, self._bg)
        painter.fillRect(0, 0, self._logical_w, self._logical_h, QColor(0, 0, 0, 140))

        sel = self._selection_rect()
        if sel is not None and sel.width() >= 2 and sel.height() >= 2:
            painter.setClipRect(sel)
            painter.drawPixmap(0, 0, self._bg)
            painter.setClipping(False)
            pen = QPen(QColor(220, 50, 50), 2)
            painter.setPen(pen)
            painter.drawRect(sel)
            painter.setPen(QColor(255, 255, 200))
            painter.setFont(QFont("Microsoft YaHei UI", 10))
            painter.drawText(
                sel.x(),
                max(16, sel.y() - 6),
                f"{sel.width()} × {sel.height()}  · 松开关闭 · Esc 取消",
            )
        else:
            painter.setPen(QColor(255, 255, 220))
            painter.setFont(QFont("Microsoft YaHei UI", 12))
            painter.drawText(24, 36, "拖拽选择区域 · Esc 取消")

    def _clamp_local(self, x: int, y: int) -> tuple[int, int]:
        return (
            max(0, min(self._logical_w - 1, x)),
            max(0, min(self._logical_h - 1, y)),
        )

    def _pos_to_logical(self, event) -> tuple[int, int]:
        """鼠标 → 相对本屏左上角的逻辑像素（与底图坐标系一致）。"""
        # global 相对该屏 geometry，避免 fullscreen / 多屏原点偏移
        gp = event.globalPosition().toPoint()
        origin = self._screen.geometry().topLeft()
        return self._clamp_local(gp.x() - origin.x(), gp.y() - origin.y())

    def _selection_rect(self) -> Optional[QRect]:
        if not self._drag_start or not self._drag_end:
            return None
        sx, sy = self._drag_start
        ex, ey = self._drag_end
        return QRect(min(sx, ex), min(sy, ey), abs(ex - sx), abs(ey - sy))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = self._pos_to_logical(event)
            self._drag_end = self._drag_start
            self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_start is not None:
            self._drag_end = self._pos_to_logical(event)
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._finished:
            return
        self._drag_end = self._pos_to_logical(event)
        sel = self._selection_rect()
        self._drag_start = None
        self._drag_end = None
        if sel is None or sel.width() < 8 or sel.height() < 8:
            self.update()
            return
        self._finish_capture(sel)

    def _finish_capture(self, sel: QRect) -> None:
        if self._finished:
            return
        self._finished = True
        crop = self._crop_logical(sel)
        self.hide()
        self.captured.emit(crop)
        self.close()

    def _crop_logical(self, sel: QRect) -> QPixmap:
        """逻辑选区 → 物理像素裁切（与快速脚本一致）。"""
        dpr = self._dpr if self._dpr > 0 else 1.0
        img = self._bg.toImage()
        phys = QRect(
            int(round(sel.x() * dpr)),
            int(round(sel.y() * dpr)),
            max(1, int(round(sel.width() * dpr))),
            max(1, int(round(sel.height() * dpr))),
        ).intersected(img.rect())
        if phys.width() < 1 or phys.height() < 1:
            return QPixmap()
        out = QPixmap.fromImage(img.copy(phys))
        # 保存时按 1:1 像素写出，避免二次缩放；预览也按真实像素显示
        out.setDevicePixelRatio(1.0)
        return out

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.hide()
        self.cancelled.emit()
        self.close()


def _pick_snip_screen(hide_widgets: list[QWidget]) -> QScreen:
    """优先用被隐藏窗口所在屏，其次鼠标所在屏，最后主屏。"""
    for w in hide_widgets:
        if w is None:
            continue
        tw = w.window() if hasattr(w, "window") else w
        try:
            sc = tw.screen() if tw is not None else None
        except Exception:
            sc = None
        if sc is not None:
            return sc
    pt = QGuiApplication.primaryScreen()
    try:
        from PySide6.QtGui import QCursor

        sc = QGuiApplication.screenAt(QCursor.pos())
        if sc is not None:
            return sc
    except Exception:
        pass
    return pt or QGuiApplication.screens()[0]


def grab_screen(screen: QScreen) -> QPixmap:
    if screen is None:
        return QPixmap()
    # winId 0 = 整屏；返回的 pixmap 带 devicePixelRatio
    return screen.grabWindow(0)


def grab_primary_screen() -> QPixmap:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QPixmap()
    return grab_screen(screen)


def save_snip_pixmap(pm: QPixmap, directory: Optional[Path] = None) -> Path:
    out_dir = directory or (PROJECT_ROOT / "screenshots" / "collab_snips")
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    path = out_dir / f"snip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    qimg = pm.toImage()
    if not qimg.save(str(path), "PNG"):
        raise OSError(f"保存截图失败: {path}")
    return path


def run_region_snip(
    *,
    hide_widgets: list[QWidget],
    on_captured,
    on_cancelled=None,
) -> None:
    """先隐藏窗口 → 截全屏 → 暗幕选区 → 恢复窗口 → 回调。

    on_captured(path: Path, pixmap: QPixmap)
    """
    targets = [w for w in hide_widgets if w is not None]
    tops: list[QWidget] = []
    seen = set()
    for w in targets:
        tw = w.window() if hasattr(w, "window") else w
        if tw is None:
            continue
        key = id(tw)
        if key in seen:
            continue
        seen.add(key)
        tops.append(tw)

    was_visible = [(tw, tw.isVisible()) for tw in tops]
    for tw, vis in was_visible:
        if vis:
            tw.hide()

    QApplication.processEvents()
    screen = _pick_snip_screen(tops)

    def _restore() -> None:
        for tw, vis in was_visible:
            if vis:
                tw.show()
                tw.raise_()
                tw.activateWindow()

    def _start_overlay() -> None:
        bg = grab_screen(screen)
        if bg.isNull():
            _restore()
            if on_cancelled:
                on_cancelled()
            return
        # 统一 dpr：以屏幕为准，避免 grab 偶发 dpr=1 导致错位
        dpr = float(screen.devicePixelRatio() or 1.0)
        if abs(float(bg.devicePixelRatio() or 1.0) - dpr) > 0.01:
            bg.setDevicePixelRatio(dpr)

        overlay = RegionSnipOverlay(bg, screen)
        run_region_snip._overlay = overlay  # type: ignore[attr-defined]

        def _ok(pm: QPixmap) -> None:
            _restore()
            try:
                path = save_snip_pixmap(pm)
            except Exception:
                path = None
            on_captured(path, pm)

        def _cancel() -> None:
            _restore()
            if on_cancelled:
                on_cancelled()

        overlay.captured.connect(_ok)
        overlay.cancelled.connect(_cancel)
        # 不用 showFullScreen：避免跳到别的屏 / 改写 geometry 导致坐标错位
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()

    QTimer.singleShot(120, _start_overlay)
