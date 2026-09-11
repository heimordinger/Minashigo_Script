from PySide6.QtCore import Qt, Signal, QEvent, QMimeData, QPoint
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont, QBrush, QTextCursor, QWheelEvent, QDrag,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QApplication,
)

from core.logging.events import LogLevel, LogEvent

# 任务球卡片拖拽排序
TASKBALL_MIME = "application/x-minashigo-taskball"



class BallIcon(QWidget):
    """圆形任务图标（纯绘制）"""

    SIZE = 34

    TARGET_RING = {
        "browser": QColor(76, 163, 255),   # 蓝：浏览器
        "window": QColor(255, 159, 67),    # 橙：控制窗口
    }

    def __init__(self, index: int = 0, parent=None):
        super().__init__(parent)
        self.index = index
        self.color = QColor(120, 120, 120)
        self.target_type: str | None = None  # "browser" | "window" | None
        self.setFixedSize(self.SIZE + 8, self.SIZE + 8)

    def set_color(self, color: QColor):
        self.color = color
        self.update()

    def set_target(self, target_type: str | None):
        self.target_type = target_type or None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        cx, cy = self.width() // 2, self.height() // 2
        r = self.SIZE // 2

        # 目标类型外环（浏览器蓝 / 窗口橙）
        ring = self.TARGET_RING.get(self.target_type or "")
        if ring is not None:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(ring, 3))
            pad = 1
            p.drawEllipse(cx - r - pad, cy - r - pad, (r + pad) * 2, (r + pad) * 2)

        # 球体
        p.setBrush(QBrush(self.color))
        p.setPen(QPen(self.color.lighter(140), 2))
        p.drawEllipse(cx - r, cy - r, r + r, r + r)

        # 高光
        hl = QColor(255, 255, 255, 30)
        p.setBrush(QBrush(hl))
        p.setPen(Qt.NoPen)
        hr = int(r * 0.4)
        p.drawEllipse(int(cx - r * 0.3), int(cy - r * 0.35), hr * 2, hr * 2)

        # 序号
        p.setPen(Qt.white)
        f = QFont("Segoe UI", 10, QFont.Bold)
        p.setFont(f)
        p.drawText(cx - r, cy - r, r + r, r + r, Qt.AlignCenter, str(self.index))


class _CardLogTextEdit(QTextEdit):
    """日志区滚轮仅滚动自身；到顶/底时不把事件传给外层任务球列表。"""

    def wheelEvent(self, event: QWheelEvent):
        sb = self.verticalScrollBar()
        if sb.maximum() <= sb.minimum():
            event.ignore()
            return

        delta = event.angleDelta().y()
        if delta > 0 and sb.value() <= sb.minimum():
            event.accept()
            return
        if delta < 0 and sb.value() >= sb.maximum():
            event.accept()
            return

        super().wheelEvent(event)
        event.accept()


class TaskBallCard(QWidget):
    """水平分栏卡：左侧固定球列 + 右侧日志区"""

    cardResized = Signal()

    STATUS_COLORS = {
        "运行中": QColor(76, 163, 255),
        "已完成": QColor(76, 217, 100),
        "执行异常": QColor(229, 83, 83),
        "已停止": QColor(255, 193, 58),
    }
    FALLBACK_COLOR = QColor(140, 140, 140)

    TARGET_LABELS = {
        "browser": "浏览器",
        "window": "窗口",
    }

    def __init__(self, task_name: str, index: int, parent=None, target_type: str | None = None):
        super().__init__(parent)
        self.setObjectName("TaskBallCard")
        self.task_name = task_name
        self.index = index
        self._target_type = (target_type or "").strip() or None
        self._status = None
        self._expanded = True
        self._auto_scroll_logs = True
        self.events: list[LogEvent] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ===== 左侧：球列（固定宽度） =====
        self._ball_area = QWidget()
        self._ball_area.setObjectName("BallColumn")
        self._ball_area.setFixedWidth(52)
        ba = QVBoxLayout(self._ball_area)
        ba.setContentsMargins(0, 6, 0, 0)
        ba.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self._ball = BallIcon(index=index)
        self._ball.set_target(self._target_type)
        self._ball.setToolTip("拖动可调整卡片顺序")
        self._ball.setCursor(Qt.CursorShape.OpenHandCursor)
        self._ball_area.setCursor(Qt.CursorShape.OpenHandCursor)
        self._ball_area.setToolTip("拖动可调整卡片顺序")
        self._ball_area.installEventFilter(self)
        self._ball.installEventFilter(self)
        self._drag_start: QPoint | None = None
        ba.addWidget(self._ball)

        # ===== 右侧：内容区 =====
        self._content = QWidget()
        self._content.setObjectName("CardContent")
        ca = QVBoxLayout(self._content)
        ca.setContentsMargins(0, 0, 0, 0)
        ca.setSpacing(0)

        # --- 头部（点击切换展开/收起） ---
        self._header = QWidget()
        self._header.setObjectName("CardHeader")
        self._header.setCursor(Qt.PointingHandCursor)

        self._title = QLabel(task_name)
        self._title.setObjectName("CardTitle")

        self._target_badge = QLabel()
        self._target_badge.setObjectName("CardTarget")
        self._apply_target_badge()

        self._status_label = QLabel("运行中")
        self._status_label.setObjectName("CardStatus")

        self._arrow = QLabel("▼")
        self._arrow.setObjectName("CardArrow")

        hl = QHBoxLayout(self._header)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(8)
        hl.addWidget(self._title, stretch=1)
        hl.addWidget(self._target_badge)
        hl.addWidget(self._status_label)
        hl.addWidget(self._arrow)

        self._header.mousePressEvent = lambda e: self.toggle_expand()

        # --- 日志体（有最大高度，内部滚动） ---
        self._body = QWidget()
        self._body.setObjectName("CardBody")
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(0, 0, 0, 0)

        self.log_text = _CardLogTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setObjectName("CardLog")
        bl.addWidget(self.log_text)
        self.log_text.verticalScrollBar().valueChanged.connect(self._on_log_scroll)

        ca.addWidget(self._header)
        ca.addWidget(self._body)

        layout.addWidget(self._ball_area)
        layout.addWidget(self._content, stretch=1)

        self.setAcceptDrops(True)

        # 所有子控件创建完毕后触发 setter 更新球颜色和标签
        self.status = "运行中"

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(TASKBALL_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(TASKBALL_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(TASKBALL_MIME):
            event.ignore()
            return
        panel = self._account_panel()
        if panel is None:
            event.ignore()
            return
        raw = bytes(event.mimeData().data(TASKBALL_MIME)).decode("utf-8", errors="ignore")
        try:
            src_id = int(raw)
        except ValueError:
            event.ignore()
            return
        if src_id == id(self):
            event.acceptProposedAction()
            return
        src = panel._card_by_id(src_id)
        if src is None:
            event.ignore()
            return
        # 相对本卡中线：上半插入到本卡前，下半插入到本卡后
        local_y = event.position().y()
        before = local_y < self.height() / 2
        lay = panel._card_layout
        self_index = -1
        for i in range(lay.count()):
            item = lay.itemAt(i)
            if item and item.widget() is self:
                self_index = i
                break
        if self_index < 0:
            event.ignore()
            return
        insert_at = self_index if before else self_index + 1
        panel._reorder_card(src, insert_at)
        event.acceptProposedAction()

    def _account_panel(self):
        w = self.parent()
        while w is not None:
            if w.__class__.__name__ == "AccountPanel":
                return w
            # CardContainer 挂在 panel 上
            if hasattr(w, "_panel") and w.__class__.__name__ == "_CardContainer":
                return w._panel
            w = w.parent()
        return None

    def eventFilter(self, obj, event):
        if obj in (self._ball_area, self._ball):
            et = event.type()
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_start = event.position().toPoint()
                self._ball.setCursor(Qt.CursorShape.ClosedHandCursor)
                self._ball_area.setCursor(Qt.CursorShape.ClosedHandCursor)
                return False
            if et == QEvent.Type.MouseButtonRelease:
                self._drag_start = None
                self._ball.setCursor(Qt.CursorShape.OpenHandCursor)
                self._ball_area.setCursor(Qt.CursorShape.OpenHandCursor)
                return False
            if (
                et == QEvent.Type.MouseMove
                and self._drag_start is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)
            ):
                delta = event.position().toPoint() - self._drag_start
                if delta.manhattanLength() >= QApplication.startDragDistance():
                    self._start_card_drag()
                    self._drag_start = None
                    self._ball.setCursor(Qt.CursorShape.OpenHandCursor)
                    self._ball_area.setCursor(Qt.CursorShape.OpenHandCursor)
                    return True
        return super().eventFilter(obj, event)

    def _start_card_drag(self) -> None:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(TASKBALL_MIME, str(id(self)).encode("utf-8"))
        drag.setMimeData(mime)
        pix = self.grab()
        if not pix.isNull():
            # 半透明预览，避免挡住目标位置
            drag.setPixmap(pix.scaledToWidth(min(280, pix.width()), Qt.TransformationMode.SmoothTransformation))
            drag.setHotSpot(QPoint(24, 16))
        drag.exec(Qt.DropAction.MoveAction)

    @property
    def target_type(self) -> str | None:
        return self._target_type

    def set_target_type(self, target_type: str | None):
        self._target_type = (target_type or "").strip() or None
        self._ball.set_target(self._target_type)
        self._apply_target_badge()

    def _apply_target_badge(self):
        label = self.TARGET_LABELS.get(self._target_type or "")
        if not label:
            self._target_badge.clear()
            self._target_badge.hide()
            self._ball.setToolTip("拖动可调整卡片顺序")
            self._ball_area.setToolTip("拖动可调整卡片顺序")
            return
        self._target_badge.setText(label)
        self._target_badge.show()
        tip = "控制目标：浏览器" if self._target_type == "browser" else "控制目标：桌面窗口"
        tip = f"{tip}\n拖动左侧序号球可调整卡片顺序"
        self._target_badge.setToolTip(tip)
        self._ball.setToolTip(tip)
        self._ball_area.setToolTip("拖动可调整卡片顺序")
        # 用 property 驱动样式（浏览器蓝 / 窗口橙）
        self._target_badge.setProperty("target", self._target_type)
        self._target_badge.style().unpolish(self._target_badge)
        self._target_badge.style().polish(self._target_badge)

    # =========================================================

    def toggle_expand(self):
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._arrow.setText("▼" if self._expanded else "▶")
        self.cardResized.emit()

    def set_expanded(self, expanded: bool):
        if expanded != self._expanded:
            self.toggle_expand()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.cardResized.emit()

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value: str):
        if self._status == value:
            return
        self._status = value
        color = self.STATUS_COLORS.get(value, self.FALLBACK_COLOR)
        self._ball.set_color(color)
        self._status_label.setText(value)

    AUTO_SCROLL_THRESHOLD = 24

    def _on_log_scroll(self, value: int):
        sb = self.log_text.verticalScrollBar()
        self._auto_scroll_logs = sb.maximum() - value <= self.AUTO_SCROLL_THRESHOLD

    def _apply_log_scroll(self, prev_value: int | None = None):
        sb = self.log_text.verticalScrollBar()
        if self._auto_scroll_logs:
            sb.setValue(sb.maximum())
        elif prev_value is not None:
            sb.setValue(prev_value)

    def add_event(self, event: LogEvent):
        self.events.append(event)
        self._render_event(event)

    @staticmethod
    def _is_light_theme() -> bool:
        try:
            from core.config.config import config
            return config.ui_theme != "dark"
        except Exception:
            return True

    def _color_for_level(self, level: LogLevel) -> str:
        light = self._is_light_theme()
        if level == LogLevel.ERROR:
            return "#c44b4b" if light else "#e8554d"
        if level == LogLevel.WARNING:
            return "#a87410" if light else "#f0ad4e"
        if level == LogLevel.DEBUG:
            return "#7a776f" if light else "#888888"
        # INFO / 默认：亮色用深灰，暗色用浅灰
        return "#3a3834" if light else "#c8ccd2"

    def _tag_for_level(self, level: LogLevel) -> str:
        if level == LogLevel.ERROR:
            return "ERROR"
        elif level == LogLevel.WARNING:
            return "WARN"
        elif level == LogLevel.DEBUG:
            return "DEBUG"
        return "INFO"

    def _render_event(self, event: LogEvent):
        ts = event.timestamp.strftime("%H:%M:%S")
        tag = self._tag_for_level(event.level)
        color = self._color_for_level(event.level)
        msg = event.message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        line = f'<span style="color:{color}">[{ts}] [{tag}] {msg}</span><br>'
        sb = self.log_text.verticalScrollBar()
        prev = sb.value()
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(line)
        self._apply_log_scroll(prev)

    def load_all_logs(self):
        sb = self.log_text.verticalScrollBar()
        prev = sb.value()
        self.log_text.clear()
        html_parts = []
        for ev in self.events:
            ts = ev.timestamp.strftime("%H:%M:%S")
            tag = self._tag_for_level(ev.level)
            color = self._color_for_level(ev.level)
            msg = ev.message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html_parts.append(f'<span style="color:{color}">[{ts}] [{tag}] {msg}</span><br>')
        self.log_text.setHtml("".join(html_parts))
        self._apply_log_scroll(prev)

    def refresh_log_theme(self):
        """主题切换后按当前主题色重绘已有日志。"""
        if self.events:
            self.load_all_logs()
