from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QBrush, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit,
)

from core.logging.events import LogLevel, LogEvent



class BallIcon(QWidget):
    """圆形任务图标（纯绘制）"""

    SIZE = 34

    def __init__(self, index: int = 0, parent=None):
        super().__init__(parent)
        self.index = index
        self.color = QColor(120, 120, 120)
        self.setFixedSize(self.SIZE + 8, self.SIZE + 8)

    def set_color(self, color: QColor):
        self.color = color
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        cx, cy = self.width() // 2, self.height() // 2
        r = self.SIZE // 2

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

    def __init__(self, task_name: str, index: int, parent=None):
        super().__init__(parent)
        self.setObjectName("TaskBallCard")
        self.task_name = task_name
        self.index = index
        self._status = None
        self._expanded = True
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

        self._status_label = QLabel("运行中")
        self._status_label.setObjectName("CardStatus")

        self._arrow = QLabel("▼")
        self._arrow.setObjectName("CardArrow")

        hl = QHBoxLayout(self._header)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(8)
        hl.addWidget(self._title, stretch=1)
        hl.addWidget(self._status_label)
        hl.addWidget(self._arrow)

        self._header.mousePressEvent = lambda e: self.toggle_expand()

        # --- 日志体（有最大高度，内部滚动） ---
        self._body = QWidget()
        self._body.setObjectName("CardBody")
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(0, 0, 0, 0)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setObjectName("CardLog")
        bl.addWidget(self.log_text)

        ca.addWidget(self._header)
        ca.addWidget(self._body)

        layout.addWidget(self._ball_area)
        layout.addWidget(self._content, stretch=1)

        # 所有子控件创建完毕后触发 setter 更新球颜色和标签
        self.status = "运行中"

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

    def add_event(self, event: LogEvent):
        self.events.append(event)
        self._render_event(event)

    def _color_for_level(self, level: LogLevel) -> str:
        if level == LogLevel.ERROR:
            return "#e8554d"
        elif level == LogLevel.WARNING:
            return "#f0ad4e"
        elif level == LogLevel.DEBUG:
            return "#888888"
        return "#d4d4d4"

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
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(line)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def load_all_logs(self):
        self.log_text.clear()
        html_parts = []
        for ev in self.events:
            ts = ev.timestamp.strftime("%H:%M:%S")
            tag = self._tag_for_level(ev.level)
            color = self._color_for_level(ev.level)
            msg = ev.message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html_parts.append(f'<span style="color:{color}">[{ts}] [{tag}] {msg}</span><br>')
        self.log_text.setHtml("".join(html_parts))
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())
