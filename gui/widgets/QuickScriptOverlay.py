# gui/widgets/QuickScriptOverlay.py
"""快速脚本 —— 置顶悬浮窗（无全屏覆盖，通过 pynput 全局监听）"""

from pathlib import Path

import time
import cv2
import numpy as np
from pynput import mouse as pynput_mouse, keyboard as pynput_kb

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QColorDialog,
    QComboBox, QDialog, QInputDialog, QApplication,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QColor, QFont, QIcon, QCursor,
    QShortcut, QKeySequence,
)

from core.path import ICON_PATH


# ── 选框指示器（小窗口，跟随鼠标，不拦截鼠标事件） ──────────

class _SelectionBox(QWidget):
    """画选框的小窗口，完全穿透鼠标。"""

    def __init__(self, overlay):
        super().__init__()
        self._overlay = overlay
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setGeometry(-200, -200, 0, 0)
        self._rect: tuple | None = None
        try:
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
        except Exception:
            pass

    def show_at(self, rect: tuple[int, int, int, int]):
        self._rect = rect
        x, y, w, h = rect
        pad = 4
        self.setGeometry(x - pad, y - pad, w + pad * 2, h + pad * 2)
        self.show()
        self.raise_()
        self.update()

    def hide_box(self):
        self._rect = None
        self.hide()

    def paintEvent(self, event):
        if not self._rect:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self._overlay._box_color
        pen = QPen(QColor(*color), 3)
        painter.setPen(pen)
        painter.drawRect(4, 4, self.width() - 8, self.height() - 8)


# ── 固定帧覆盖层（全屏半透明，显示冻结画面） ──────────────

class _FrozenOverlay(QWidget):
    """固定帧时全屏覆盖，半透明遮罩 + 选框。"""

    def __init__(self, overlay):
        super().__init__()
        self._overlay = overlay
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setGeometry(QApplication.primaryScreen().geometry())
        self._element_rect: tuple | None = None
        self._drag_start: tuple | None = None
        self._drag_end: tuple | None = None
        self._frame: np.ndarray | None = None
        self._pixmap: QPixmap | None = None
        self._mouse_screen_pos: tuple | None = None
    def _close_freeze(self):
        """ESC 关闭固定帧。"""
        self._overlay._frame_frozen = False
        self._overlay._frozen_overlay.hide()
        self._overlay.freeze_btn.setChecked(False)
        self._overlay.freeze_btn.setText("固定帧 [F8]")

    def set_frame(self, frame: np.ndarray):
        self._frame = frame.copy()
        self._rgb = cv2.cvtColor(self._frame, cv2.COLOR_BGR2RGB)
        h, w, ch = self._rgb.shape
        qimg = QImage(self._rgb.data, w, h, ch * w, QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg).copy()
        print(f"[FrozenOverlay] set_frame: {w}x{h}, pixmap valid={not self._pixmap.isNull() if self._pixmap else False}")
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 先画冻结画面
        if self._pixmap is not None:
            screen = QApplication.primaryScreen().geometry()
            painter.drawPixmap(0, 0, screen.width(), screen.height(), self._pixmap)

        # 半透明灰色遮罩（降低亮度，提示已冻结）
        painter.fillRect(self.rect(), QColor(0, 0, 0, 140))

        # 手动拖拽矩形框（红色实线）
        if self._drag_start and self._drag_end:
            sx, sy = self._drag_start
            ex, ey = self._drag_end
            x1, y1 = min(sx, ex), min(sy, ey)
            x2, y2 = max(sx, ex), max(sy, ey)
            pen = QPen(QColor(220, 50, 50), 3)
            painter.setPen(pen)
            painter.drawRect(x1, y1, x2 - x1, y2 - y1)

        # 自动检测框（绿色实线）
        if self._element_rect and not self._drag_start:
            ex, ey, ew, eh = self._element_rect
            pen = QPen(QColor(30, 220, 30), 3)
            painter.setPen(pen)
            painter.drawRect(ex, ey, ew, eh)

        # ── 放大镜（类似 QQ 截图，跟随鼠标） ──
        if self._mouse_screen_pos and self._frame is not None:
            mx, my = self._mouse_screen_pos
            dpr = QApplication.primaryScreen().devicePixelRatio()
            fmx, fmy = int(mx * dpr), int(my * dpr)

            mag_size = 100
            src_size = 16
            half = src_size // 2
            sx = max(0, fmx - half)
            sy = max(0, fmy - half)
            crop = self._frame[sy:sy + src_size, sx:sx + src_size]
            if crop.size > 0:
                mag = cv2.resize(crop, (mag_size, mag_size), interpolation=cv2.INTER_NEAREST)
                rgb = cv2.cvtColor(mag, cv2.COLOR_BGR2RGB)
                qimg = QImage(rgb.data, mag_size, mag_size, mag_size * 3, QImage.Format_RGB888)
                pix = QPixmap.fromImage(qimg)

                # 定位：优先右下角，超出屏幕则左上角，适当拉开距离
                gap = 20
                ox, oy = mx + gap, my + gap
                sw, sh = QApplication.primaryScreen().geometry().width(), QApplication.primaryScreen().geometry().height()
                if ox + mag_size > sw - 10 or oy + mag_size > sh - 10:
                    ox, oy = mx - mag_size - gap, my - mag_size - gap
                    if ox < 10: ox = mx + gap
                    if oy < 10: oy = my + gap

                # 白色边框
                painter.setPen(QPen(QColor(180, 180, 180), 1))
                painter.drawRect(ox, oy, mag_size, mag_size)
                painter.drawPixmap(ox + 1, oy + 1, mag_size - 2, mag_size - 2, pix)

                # 十字线（放大镜中心）
                cx, cy = ox + mag_size // 2, oy + mag_size // 2
                painter.setPen(QPen(QColor(255, 80, 80), 1))
                painter.drawLine(cx - 8, cy, cx + 8, cy)
                painter.drawLine(cx, cy - 8, cx, cy + 8)

                # 坐标（放大镜上方）
                painter.setPen(QColor(255, 255, 200))
                painter.setFont(QFont("monospace", 9))
                painter.drawText(ox, oy - 4, f"({mx},{my})")

    def _detect_at(self, sx, sy):
        """在屏幕坐标 (sx, sy) 处检测元素，更新选框。"""
        if self._frame is None:
            return
        h, w = self._frame.shape[:2]
        if sx < 0 or sy < 0 or sx >= w or sy >= h:
            self._element_rect = None
            self.update()
            return

        dpr = QApplication.primaryScreen().devicePixelRatio()
        fmx, fmy = int(sx * dpr), int(sy * dpr)
        elem = _detect_element_fast(self._frame, fmx, fmy)

        if elem:
            px, py, pw, ph = elem["x"], elem["y"], elem["w"], elem["h"]
            # 选框用逻辑像素显示
            if dpr > 1:
                lx, ly = int(px / dpr), int(py / dpr)
                lw, lh = int(pw / dpr), int(ph / dpr)
            else:
                lx, ly, lw, lh = px, py, pw, ph
            self._element_rect = (lx, ly, lw, lh)
            # 录制存物理像素（帧是物理分辨率）
            if self._overlay._recording:
                self._overlay._last_frame = self._frame
                self._overlay._last_elem_rect = (px, py, pw, ph)
        else:
            size = 60
            self._element_rect = (sx - size // 2, sy - size // 2, size, size)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = (int(event.globalPosition().x()), int(event.globalPosition().y()))
            self._drag_end = None

    def mouseMoveEvent(self, event):
        self._mouse_screen_pos = (int(event.globalPosition().x()), int(event.globalPosition().y()))
        if self._drag_start is not None:
            self._drag_end = self._mouse_screen_pos
            self._element_rect = None
            self.update()
        else:
            self._detect_at(*self._mouse_screen_pos)

    def mouseReleaseEvent(self, event):
        if self._drag_start and self._drag_end and self._overlay._recording:
            sx, sy = self._drag_start
            ex, ey = self._drag_end
            x1, y1 = min(sx, ex), min(sy, ey)
            x2, y2 = max(sx, ex), max(sy, ey)
            w, h = x2 - x1, y2 - y1
            if w >= 10 and h >= 10:
                self._element_rect = (x1, y1, w, h)
                # 坐标转物理像素（帧是物理分辨率）
                dpr = QApplication.primaryScreen().devicePixelRatio()
                px, py, pw, ph = int(x1 * dpr), int(y1 * dpr), int(w * dpr), int(h * dpr)
                self._overlay._last_frame = self._frame
                self._overlay._last_elem_rect = (px, py, pw, ph)
                if self._overlay._recording and (self._overlay._monitoring or self._overlay._frame_frozen):
                    self._overlay._on_pynput_click(x1, y1, pynput_mouse.Button.left, True)
        self._drag_start = None
        self._drag_end = None


# ── 折叠标签（独立小窗口，仅折叠时显示） ──────────────

class _CollapseTab(QWidget):
    """折叠时显示在屏幕右侧的小按钮窗口。"""

    def __init__(self, overlay):
        super().__init__()
        self._overlay = overlay
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFixedSize(22, 70)

        btn = QPushButton("◀", self)
        btn.setGeometry(0, 0, 22, 70)
        btn.setStyleSheet(
            "QPushButton {"
            "  border: none; background: #4a90d9;"
            "  border-top-left-radius: 8px;"
            "  border-bottom-left-radius: 8px;"
            "  font-size: 14px; font-weight: bold; color: white;"
            "}"
            "QPushButton:hover { background: #357abd; }"
        )
        self._btn = btn
        self._btn.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._drag_start = None
        self._was_drag = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = int(event.globalPosition().y())
            self._was_drag = False

    def mouseMoveEvent(self, event):
        if self._drag_start is None:
            return
        screen_y = int(event.globalPosition().y())
        dy = screen_y - self._drag_start
        if abs(dy) > 5:
            self._was_drag = True
        if dy:
            self.move(self.x(), self.y() + dy)
            self._drag_start = screen_y

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._was_drag:
                self._overlay._toggle_collapse()
        self._drag_start = None

    def closeEvent(self, event):
        self._overlay.close()


# ── 悬浮控制面板 ──────────────────────────────────

class QuickScriptOverlay(QWidget):
    """快速脚本置顶悬浮窗。"""
    _capture_triggered = Signal()
    CONTENT_W = 320
    BTN_W = 22
    W = CONTENT_W + BTN_W  # 342 窗口总宽

    def __init__(self):
        super().__init__()
        self.setWindowTitle("快速脚本")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool |
                            Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setFixedWidth(self.W)
        self.resize(self.W, 500)

        self._monitoring = False
        self._recording = False
        self._box_color = (30, 220, 30)
        self._frame_frozen = False
        self._save_dir = Path("screenshots/quick_script")
        self._steps: list[dict] = []
        self._step_counter = 0
        self._last_frame: np.ndarray | None = None
        self._last_elem_rect: tuple | None = None
        self._last_cursor_pos: tuple | None = None
        self._pending_rect: tuple | None = None
        self._pending_frame: np.ndarray | None = None
        self._yolo = None
        self._yolo_conf = 0.5
        self._capture_triggered.connect(self._process_pending_capture,
                                        type=Qt.QueuedConnection)
        self._collapsed = False
        self._saved_y = self.y()
        self._saved_h = self.height()
        self._collapse_tab = _CollapseTab(self)
        self._collapse_tab.hide()
        self._frozen_overlay = _FrozenOverlay(self)
        self._frozen_overlay.hide()
        self._init_detect_vars()
        QApplication.processEvents()
        self._ensure_frame()  # 预热截图，避免首次拾取卡顿

        screen = QApplication.primaryScreen().geometry()
        self.move(screen.right() - self.W, self.y())

        # 选框指示器
        self._sel_box = _SelectionBox(self)
        self._sel_box.hide()

        # pynput 监听
        self._pynput_listener: pynput_mouse.Listener | None = None

        # ESC 关闭固定帧（pynput 后台线程）
        self._esc_listener = pynput_kb.Listener(on_press=self._on_esc_press)
        self._esc_listener.start()

        # 截图检测定时器
        self._detect_timer = QTimer(self)
        self._detect_timer.timeout.connect(self._poll_mouse)
        self._detect_timer.setInterval(200)

        # F6 快捷键
        self._f6_shortcut = QShortcut(QKeySequence("F6"), self)
        self._f6_shortcut.setContext(Qt.ApplicationShortcut)
        self._f6_shortcut.activated.connect(self._toggle_monitor_hotkey)

        # ── 主布局（按钮左 | 内容右） ──
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 收缩按钮（蓝色，铺满左侧整列高度）
        self._collapse_btn = QPushButton("▶")
        self._collapse_btn.setFixedSize(22, 70)
        self._collapse_btn.setStyleSheet(
            "QPushButton {"
            "  border: none; background: #4a90d9;"
            "  border-top-left-radius: 8px;"
            "  border-bottom-left-radius: 8px;"
            "  font-size: 14px; font-weight: bold; color: white;"
            "}"
            "QPushButton:hover { background: #357abd; }"
        )
        self._collapse_btn.clicked.connect(self._toggle_collapse)
        main_layout.addWidget(self._collapse_btn, 0, Qt.AlignVCenter)

        # 内容面板（白色背景）
        content = QWidget()
        content.setObjectName("QSContent")
        content.setStyleSheet(
            "#QSContent { background: #f5f5f5; color: #333; }"
        )
        inner = QVBoxLayout(content)
        inner.setContentsMargins(8, 4, 8, 8)
        inner.setSpacing(6)
        self._content_widget = content
        main_layout.addWidget(content, stretch=1)

        title = QLabel("快速脚本  (F6 切换拾取)")
        title.setFont(QFont("", 12, QFont.Weight.Bold))
        inner.addWidget(title)

        row1 = QHBoxLayout()
        self.monitor_btn = QPushButton("▶ 拾取 [F6]")
        self.monitor_btn.setCheckable(True)
        self.monitor_btn.clicked.connect(self._toggle_monitor)
        self.record_btn = QPushButton("● 录制 [F7]")
        self.record_btn.setCheckable(True)
        self.record_btn.clicked.connect(self._toggle_record)
        self._f7_shortcut = QShortcut(QKeySequence("F7"), self)
        self._f7_shortcut.activated.connect(
            self.record_btn.toggle
        )
        self.freeze_btn = QPushButton("固定帧 [F8]")
        self.freeze_btn.setCheckable(True)
        self.freeze_btn.setChecked(False)
        self.freeze_btn.clicked.connect(self._toggle_freeze)
        row1.addWidget(self.monitor_btn)
        row1.addWidget(self.record_btn)
        row1.addWidget(self.freeze_btn)
        inner.addLayout(row1)

        row2 = QHBoxLayout()
        self.color_btn = QPushButton("选框颜色")
        self.color_btn.clicked.connect(self._pick_color)
        self.path_btn = QPushButton("保存路径")
        self.path_btn.clicked.connect(self._pick_path)
        self.path_label = QLabel(str(self._save_dir))
        self.path_label.setStyleSheet("color: #888; font-size: 10px;")
        row2.addWidget(self.color_btn)
        row2.addWidget(self.path_btn)
        inner.addLayout(row2)
        inner.addWidget(self.path_label)
        self.path_label.setWordWrap(True)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("帧率:"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["15FPS", "30FPS", "60FPS", "90FPS", "120FPS"])
        self.fps_combo.setCurrentIndex(2)
        self.fps_combo.currentIndexChanged.connect(self._on_fps_changed)
        row3.addWidget(self.fps_combo)
        row3.addSpacing(8)
        row3.addWidget(QLabel("阈值:"))
        self.threshold_combo = QComboBox()
        self.threshold_combo.addItems(["高(0.6)", "中(0.5)", "低(0.3)"])
        self.threshold_combo.setCurrentIndex(1)
        self.threshold_combo.currentIndexChanged.connect(self._on_threshold_changed)
        row3.addWidget(self.threshold_combo)
        inner.addLayout(row3)

        # ── 手动插入滚动步骤 ──
        scroll_row = QHBoxLayout()
        self._scroll_down_btn = QPushButton("↓ 向下滚动")
        self._scroll_down_btn.clicked.connect(lambda: self._add_scroll_step("down"))
        self._scroll_up_btn = QPushButton("↑ 向上滚动")
        self._scroll_up_btn.clicked.connect(lambda: self._add_scroll_step("up"))
        self._scroll_btm_btn = QPushButton("⤓ 滚到底部")
        self._scroll_btm_btn.clicked.connect(lambda: self._add_scroll_step("bottom"))
        self._scroll_top_btn = QPushButton("⤒ 滚到顶部")
        self._scroll_top_btn.clicked.connect(lambda: self._add_scroll_step("top"))
        scroll_row.addWidget(self._scroll_down_btn)
        scroll_row.addWidget(self._scroll_up_btn)
        scroll_row.addWidget(self._scroll_btm_btn)
        scroll_row.addWidget(self._scroll_top_btn)
        inner.addLayout(scroll_row)

        inner.addWidget(QLabel("录制序列:"))
        self.step_list = QListWidget()
        self.step_list.setAlternatingRowColors(True)
        self.step_list.setDragDropMode(QListWidget.InternalMove)
        self.step_list.setDefaultDropAction(Qt.MoveAction)
        self.step_list.model().rowsMoved.connect(self._on_steps_reordered)
        inner.addWidget(self.step_list, stretch=1)

        self.export_btn = QPushButton("生成脚本 [Ctrl+S]")
        self.export_btn.clicked.connect(self._export_script)
        self.export_btn.setEnabled(False)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(
            lambda: self._export_script() if self.export_btn.isEnabled() else None
        )
        inner.addWidget(self.export_btn)

        self.close_btn = QPushButton("关闭 [Ctrl+W]")
        self.close_btn.clicked.connect(self._hide_overlay)
        QShortcut(QKeySequence("Ctrl+W"), self).activated.connect(self._hide_overlay)
        inner.addWidget(self.close_btn)

    # ── 折叠 ──

    def showEvent(self, event):
        super().showEvent(event)
        QApplication.processEvents()

    def _toggle_collapse(self):
        screen = QApplication.primaryScreen().geometry()
        if not self._collapsed:
            self._collapsed = True
            # 用 client 区坐标（geometry().y()）而非框架坐标（y()）
            cy = self.geometry().y() + (self.height() - 70) // 2
            self._collapse_tab.setGeometry(screen.right() - 22, cy, 22, 70)
            self._collapse_tab.show()
            self.move(screen.right() + 1, self.geometry().y())
        else:
            by = self._collapse_tab.y()
            self._collapse_tab.hide()
            self._collapsed = False
            h = self.height()
            wy = by + (70 - h) // 2
            self.setGeometry(screen.right() - self.W, wy, self.W, h)

    # ── 鼠标轮询 ──

    _FRAME_TTL = 0.5  # 截图缓存有效期 0.5 秒

    def _init_detect_vars(self):
        self._cached_frame = None
        self._cached_dpr = 1.0
        self._cached_ts = 0.0

    def _ensure_frame(self):
        """返回 (frame, dpr)，固定帧时用缓存，过期才重新截图。"""
        if self._frame_frozen and self._cached_frame is not None:
            return self._cached_frame, self._cached_dpr
        import time
        now = time.time()
        if self._cached_frame is not None and now - self._cached_ts < self._FRAME_TTL:
            return self._cached_frame, self._cached_dpr
        try:
            pixmap = QApplication.primaryScreen().grabWindow(0)
            qimg = pixmap.toImage()
            ptr = qimg.bits()
            ptr.setsize(qimg.sizeInBytes())
            arr = np.frombuffer(bytes(ptr), dtype=np.uint8).reshape(
                qimg.height(), qimg.width(), 4
            ).copy()
            self._cached_frame = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
            self._cached_dpr = pixmap.devicePixelRatio()
            self._cached_ts = now
        except Exception:
            try:
                import pyautogui
                img = pyautogui.screenshot()
                self._cached_frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                self._cached_dpr = QApplication.primaryScreen().devicePixelRatio()
                self._cached_ts = now
            except Exception:
                pass
        return self._cached_frame, self._cached_dpr

    def _poll_mouse(self):
        if not self._monitoring:
            return
        if self._frame_frozen:
            return  # 覆盖层自己通过 mouseMoveEvent 驱动
        pos = QCursor.pos()

        # 每次鼠标移动都检测（截图缓存的，无需等待）
        frame, dpr = self._ensure_frame()
        if frame is None:
            return

        mx = int(pos.x() * dpr)
        my = int(pos.y() * dpr)

        elem = None
        if self._yolo is None:
            try:
                from backend.quick_script.yolo_detector import YoloDetector
                self._yolo = YoloDetector()
            except Exception:
                pass
        if self._yolo and self._yolo.available:
            elem = self._yolo.detect_near(frame, mx, my, self._yolo_conf)
        if elem is None:
            elem = _detect_element_fast(frame, mx, my)

        if elem:
            px, py, pw, ph = elem["x"], elem["y"], elem["w"], elem["h"]
            lx, ly = (int(px / dpr), int(py / dpr)) if dpr > 1 else (px, py)
            self._sel_box.show_at((lx, ly, int(pw / dpr) if dpr > 1 else pw,
                                   int(ph / dpr) if dpr > 1 else ph))
            if self._recording:
                self._last_frame = frame
                self._last_elem_rect = (px, py, pw, ph)
        else:
            size = 60
            lx = int(pos.x()) - size // 2
            ly = int(pos.y()) - size // 2
            self._sel_box.show_at((lx, ly, size, size))

    # ── pynput 点击 ──

    def _on_pynput_click(self, x, y, button, pressed):
        if not pressed or button != pynput_mouse.Button.left:
            return
        if not self._recording:
            return
        geom = self.geometry()
        if geom.x() <= x <= geom.x() + geom.width() and geom.y() <= y <= geom.y() + geom.height():
            return
        if self._last_elem_rect is not None and self._last_frame is not None:
            self._pending_rect = self._last_elem_rect
            self._pending_frame = self._last_frame
            self._capture_triggered.emit()

    def _capture_element(self, rect, frame):
        x, y, w, h = rect
        if w < 5 or h < 5:
            return
        template = frame[y:y + h, x:x + w]
        if template.size == 0:
            return
        self._step_counter += 1
        name = f"step_{self._step_counter:03d}"
        self._save_dir.mkdir(parents=True, exist_ok=True)
        filepath = self._save_dir / f"{name}.png"
        cv2.imwrite(str(filepath), template)
        step_id = f"click_{self._step_counter}_{int(time.time())}"
        self._steps.append({"id": step_id, "action": "click", "name": f"{name}.png", "x": x, "y": y, "w": w, "h": h, "path": str(filepath)})

        from PySide6.QtWidgets import QWidget as _Qw, QHBoxLayout as _HL
        widget = _Qw()
        hlayout = _HL(widget)
        hlayout.setContentsMargins(2, 1, 2, 1)
        hlayout.setSpacing(4)
        icon_label = QLabel()
        try:
            pix = QPixmap(str(filepath))
            if not pix.isNull():
                icon_label.setPixmap(pix.scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception:
            pass
        hlayout.addWidget(icon_label)
        info = QLabel(f"步骤{self._step_counter}\n{w}×{h}")
        info.setStyleSheet("font-size: 10px;")
        hlayout.addWidget(info, stretch=1)
        preview_btn = QPushButton("预览")
        preview_btn.setFixedSize(40, 28)
        preview_btn.clicked.connect(lambda checked, fp=filepath: self._preview_image(fp))
        hlayout.addWidget(preview_btn)
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(28, 28)
        del_btn.setStyleSheet("color: #ff4444;")
        del_btn.clicked.connect(lambda checked, idx=len(self._steps) - 1: self._delete_step(idx))
        hlayout.addWidget(del_btn)
        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        item.setData(Qt.UserRole, step_id)
        self.step_list.addItem(item)
        self.step_list.setItemWidget(item, widget)
        self.step_list.scrollToBottom()
        self.export_btn.setEnabled(True)

    # ── 手动插入滚动步骤 ──

    def _add_scroll_step(self, direction: str):
        """手动插入一个滚动步骤到序列末尾。"""
        self._step_counter += 1
        step_id = f"scroll_{self._step_counter}_{int(time.time())}"

        if direction == "down":
            step = {"id": step_id, "action": "scroll", "delta_y": -300, "label": "↓向下滚动300px"}
            label_text = "↓向下滚动 300px"
        elif direction == "up":
            step = {"id": step_id, "action": "scroll", "delta_y": 300, "label": "↑向上滚动300px"}
            label_text = "↑向上滚动 300px"
        elif direction == "bottom":
            step = {"id": step_id, "action": "scroll_to_bottom", "label": "⤓滚动到底部"}
            label_text = "⤓滚动到底部"
        elif direction == "top":
            step = {"id": step_id, "action": "scroll_to_top", "label": "⤒滚动到顶部"}
            label_text = "⤒滚动到顶部"
        else:
            return

        self._steps.append(step)

        from PySide6.QtWidgets import QWidget as _Qw, QHBoxLayout as _HL
        widget = _Qw()
        hlayout = _HL(widget)
        hlayout.setContentsMargins(2, 1, 2, 1)
        hlayout.setSpacing(4)

        icon_label = QLabel("🖱")
        icon_label.setStyleSheet("font-size: 20px; padding: 8px;")
        hlayout.addWidget(icon_label)
        info = QLabel(f"步骤{self._step_counter}\n{label_text}")
        info.setStyleSheet("font-size: 10px; color: #2a7;")
        hlayout.addWidget(info, stretch=1)
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(28, 28)
        del_btn.setStyleSheet("color: #ff4444;")
        del_btn.clicked.connect(lambda checked, idx=len(self._steps) - 1: self._delete_step(idx))
        hlayout.addWidget(del_btn)
        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        item.setData(Qt.UserRole, step_id)
        self.step_list.addItem(item)
        self.step_list.setItemWidget(item, widget)
        self.step_list.scrollToBottom()
        self.export_btn.setEnabled(True)

    def _process_pending_capture(self):
        if self._pending_rect is not None and self._pending_frame is not None:
            rect, self._pending_rect = self._pending_rect, None
            frame, self._pending_frame = self._pending_frame, None
            self._capture_element(rect, frame)

    # ── 步骤操作 ──

    def _preview_image(self, filepath):
        from PySide6.QtWidgets import QDialog, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("预览")
        dlg.resize(400, 300)
        layout = QVBoxLayout(dlg)
        label = QLabel()
        pix = QPixmap(str(filepath))
        if not pix.isNull():
            label.setPixmap(pix.scaled(380, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(label)
        dlg.exec()

    def _export_script(self):
        """生成 do_work 脚本。"""
        if not self._steps:
            return
        # 选择保存目录（优先 scripts/）
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        from core.path import SCRIPTS_PATH
        dlg = ResourcePickerDialog(self, mode="folders", root_path=str(SCRIPTS_PATH), show_recursive=False)
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        save_dir = Path(dlg.selected_path)

        # 输入脚本名称
        name, ok = QInputDialog.getText(self, "脚本名称", "名称（不含 .py）:", text="quick_script")
        if not ok or not name:
            return
        name = name.strip()
        if not name:
            return

        # 复制模板图片到 assets/images/{name}/（仅复制有图片的步骤）
        from core.path import IMG_PATH
        img_out = IMG_PATH / name
        img_out.mkdir(parents=True, exist_ok=True)
        import shutil
        for step in self._steps:
            if "path" not in step:
                continue
            src = Path(step["path"])
            dst = img_out / src.name
            shutil.copy2(str(src), str(dst))

        # 模板图片所在目录（已自动复制到 assets/images/{name}/）
        img_dir = str(img_out.resolve())
        lines = [
            "import asyncio",
            "from backend.browser.user_browser import UserBrowser",
            "",
            "",
            "async def do_work(browser: UserBrowser):",
            f'    """快速脚本: {name} ({len(self._steps)} 步)"""',
            f'    img_dir = r"{img_dir}"',
            "",
        ]
        for i, step in enumerate(self._steps, 1):
            action = step.get("action", "click")
            if action == "scroll":
                delta_y = step.get("delta_y", -300)
                direction = "向下" if delta_y < 0 else "向上"
                lines.append(f"    # ── 步骤{i}：{direction}滚动 {abs(delta_y)}px ──")
                lines.append(f"    await browser.scroll(delta_y={delta_y})")
                lines.append(f"    await asyncio.sleep(0.3)")
            elif action == "scroll_to_bottom":
                lines.append(f"    # ── 步骤{i}：滚动到底部 ──")
                lines.append(f"    await browser.scroll_to_bottom()")
                lines.append(f"    await asyncio.sleep(0.3)")
            elif action == "scroll_to_top":
                lines.append(f"    # ── 步骤{i}：滚动到顶部 ──")
                lines.append(f"    await browser.scroll_to_top()")
                lines.append(f"    await asyncio.sleep(0.3)")
            else:
                fp = Path(step["path"]).name
                lines.append(f"    # ── 步骤{i}：在 ({step['x']},{step['y']}) 处点击 {step['w']}×{step['h']} 的按钮 ──")
                lines.append(f'    await browser.click_image(')
                lines.append(f'        img_path=r"img_dir\{fp}",')
                lines.append(f'        threshold=0.85,')
                lines.append(f'    )')
                lines.append(f'    await asyncio.sleep(0.5)')
            lines.append("")

        # 写入文件
        py_path = save_dir / f"{name}.py"
        py_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[QuickScript] 已生成脚本: {py_path} ({len(self._steps)} 步)")

    def _on_steps_reordered(self):
        """拖拽重排后同步 _steps 顺序。"""
        new_steps = []
        for i in range(self.step_list.count()):
            item = self.step_list.item(i)
            if item:
                sid = item.data(Qt.UserRole)
                step = next((s for s in self._steps if s.get("id") == sid), None)
                if step:
                    new_steps.append(step)
        if new_steps:
            self._steps = new_steps

    def _delete_step(self, idx):
        if 0 <= idx < len(self._steps):
            self._steps.pop(idx)
        item = self.step_list.takeItem(idx)
        if item:
            self.step_list.removeItemWidget(item)
            del item

    # ── 控制 ──

    def _toggle_monitor(self, checked):
        self._monitoring = checked
        self._sel_box.setVisible(checked)
        if checked:
            self.monitor_btn.setText("■ 停止拾取 [F6]")
            self._detect_timer.start()
            if self._pynput_listener is None or not self._pynput_listener.running:
                self._pynput_listener = pynput_mouse.Listener(on_click=self._on_pynput_click)
                self._pynput_listener.start()
        else:
            self.monitor_btn.setText("▶ 拾取 [F6]")
            self._detect_timer.stop()
            self._sel_box.hide_box()
            self._recording = False
            self.record_btn.setText("● 录制")

    def _toggle_monitor_hotkey(self):
        self.monitor_btn.toggle()

    def _toggle_record(self, checked):
        self._recording = checked
        self.record_btn.setText("● 录制中..." if checked else "● 录制")

    def _toggle_freeze(self, checked):
        print(f"[ToggleFreeze] checked={checked}, frame_frozen was={self._frame_frozen}")
        if checked:
            self._cached_frame = None
            self._cached_ts = 0
            frame, dpr = self._ensure_frame()
            print(f"[ToggleFreeze] frame={frame is not None}, dpr={dpr}")
            if frame is None:
                print("[ToggleFreeze] frame is None, aborting")
                return
            self._frame_frozen = True
            self.freeze_btn.setText("❄ 固定帧 [F8]")
            self.freeze_btn.setChecked(True)
            self._frozen_overlay.set_frame(frame)
            print(f"[ToggleFreeze] showing overlay, visible={self._frozen_overlay.isVisible()}")
            self._frozen_overlay.showFullScreen()
            self._frozen_overlay.raise_()
            self._frozen_overlay.activateWindow()
            QApplication.processEvents()
            print(f"[ToggleFreeze] after show, visible={self._frozen_overlay.isVisible()}")
            pos = QCursor.pos()
            self._frozen_overlay._detect_at(pos.x(), pos.y())
        else:
            print("[ToggleFreeze] hiding overlay")
            self._frame_frozen = False
            self.freeze_btn.setText("固定帧 [F8]")
            self.freeze_btn.setChecked(False)
            self._frozen_overlay.hide()
            self._sel_box.hide_box()

    def _on_esc_press(self, key):
        """ESC 关闭固定帧（pynput 线程）。"""
        try:
            print(f"[pynput] key={key!r}, type={type(key).__name__}, frozen={self._frame_frozen}")
            if key == pynput_kb.Key.esc and self._frame_frozen:
                print("[pynput] ESC detected, closing freeze")
                QTimer.singleShot(0, self._do_close_freeze)
        except Exception as e:
            print(f"[pynput] error: {e}")

    def _do_close_freeze(self):
        """主线程：关闭固定帧。"""
        self._frame_frozen = False
        self.freeze_btn.setChecked(False)
        self.freeze_btn.setText("固定帧 [F8]")
        self._frozen_overlay.hide()
        self._sel_box.hide_box()

    def _pick_color(self):
        color = QColorDialog.getColor(QColor(*self._box_color), self, "选择选框颜色")
        if color.isValid():
            self._box_color = (color.red(), color.green(), color.blue())

    def _on_fps_changed(self, idx):
        fps = [15, 30, 60, 90, 120][idx]
        self._detect_timer.setInterval(int(1000 / fps))

    def _on_threshold_changed(self, idx):
        thresholds = [0.6, 0.5, 0.3]
        self._yolo_conf = thresholds[idx]

    def _pick_path(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        from core.path import PROJECT_ROOT
        dlg = ResourcePickerDialog(self, mode="folders", root_path=str(PROJECT_ROOT), show_recursive=False)
        if dlg.exec() == QDialog.Accepted and dlg.selected_path:
            self._save_dir = Path(dlg.selected_path)
            self.path_label.setText(str(self._save_dir))

    def _hide_overlay(self):
        """隐藏前先展开（避免下次显示时停留在折叠状态）。"""
        if self._collapsed:
            self._toggle_collapse()
        self._monitoring = False
        self._detect_timer.stop()
        self._sel_box.hide_box()
        self._frozen_overlay.hide()
        self.hide()

    def _on_close(self):
        if getattr(self, '_closing', False):
            return
        self._closing = True
        self._monitoring = False
        self._detect_timer.stop()
        self._collapse_tab.close()
        self._frozen_overlay.close()
        try:
            self._esc_listener.stop()
        except Exception:
            pass
        self.close()
        if self._pynput_listener:
            try:
                self._pynput_listener.stop()
            except Exception:
                pass
            self._pynput_listener = None
        self._sel_box.close()

    def closeEvent(self, event):
        self._on_close()
        # 如果是独立运行（仅此窗口），退出应用
        if len(QApplication.instance().topLevelWidgets()) <= 1:
            QApplication.instance().quit()


# ── 快速元素检测 ────────────────────────────────

def _detect_element_fast(frame: np.ndarray, cx: int, cy: int) -> dict | None:
    """边缘检测（与 ElementInspector 一致）。"""
    h, w = frame.shape[:2]
    if cx < 0 or cy < 0 or cx >= w or cy >= h:
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    def find_edge_x(start, step):
        x = start
        while 0 <= x < w - 1:
            nx = x + step
            if abs(int(gray[cy, x]) - int(gray[cy, nx])) > 25:
                return x
            x = nx
        return start

    def find_edge_y(start, step):
        y = start
        while 0 <= y < h - 1:
            ny = y + step
            if abs(int(gray[y, cx]) - int(gray[ny, cx])) > 25:
                return y
            y = ny
        return start

    left = find_edge_x(cx, -1)
    right = find_edge_x(cx, 1)
    top = find_edge_y(cy, -1)
    bottom = find_edge_y(cy, 1)
    ew, eh = right - left + 1, bottom - top + 1
    if ew < 8 or eh < 8:
        size = 60
        left = max(0, cx - size // 2)
        top = max(0, cy - size // 2)
        ew = min(size, w - left)
        eh = min(size, h - top)
        right = left + ew - 1
        bottom = top + eh - 1
    return {"x": left, "y": top, "w": ew, "h": eh}
