"""脚本生成轨迹：类 Cursor Agent 的线性任务流（图标 + 标签 · 摘要，默认收缩可展开）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QCursor, QFont, QPainter, QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


# kind → (accent, 中文标签, 行内图标字符)
_KIND_META = {
    "Think": ("#5b8def", "思考", "◈"),
    "Plan": ("#7c5cbf", "规划", "▣"),
    "Generate": ("#2f9e6b", "生成", "✦"),
    "Task": ("#0891b2", "任务", "▸"),
    "Merge": ("#6366f1", "合并", "⧉"),
    "Validate": ("#c48a2a", "校验", "◎"),
    "Fix": ("#d97706", "修复", "⚒"),
    "Revise": ("#0ea5e9", "修订", "↺"),
    "Review": ("#db2777", "审查", "※"),
    "Vision": ("#a855f7", "识图", "◉"),
    "Info": ("#6b7280", "信息", "·"),
    "Error": ("#dc2626", "错误", "✕"),
}

_STATUS_LABEL = {
    "pending": "等待",
    "running": "进行中",
    "done": "完成",
    "error": "失败",
}


class _IconBadge(QWidget):
    """左侧小图标圆点。"""

    def __init__(self, glyph: str, color: str, parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self._color = QColor(color)
        self.setFixedSize(22, 22)

    def set_color(self, color: str):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 淡底圆
        bg = QColor(self._color)
        bg.setAlpha(36)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawEllipse(1, 1, 20, 20)
        # 字形
        p.setPen(self._color)
        f = QFont()
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._glyph)
        p.end()


class _StepRow(QFrame):
    """单行任务：图标 标签 · 摘要  [状态] [▸]，点击展开正文。"""

    def __init__(self, kind: str, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("GenTrajectoryStep")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        accent, label, glyph = _KIND_META.get(kind, _KIND_META["Info"])
        self._kind = kind
        self._accent = accent
        self._status = "running"
        self._full_body = ""
        self._expanded = False
        self._title_text = title or ""
        self._hovered = False

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)

        self._icon = _IconBadge(glyph, accent)
        head.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)

        self._kind_lbl = QLabel(label)
        kf = QFont()
        kf.setBold(True)
        kf.setPointSize(10)
        self._kind_lbl.setFont(kf)
        self._kind_lbl.setStyleSheet(f"color:{accent};background:transparent;")
        head.addWidget(self._kind_lbl, 0, Qt.AlignmentFlag.AlignTop)

        self._dot = QLabel("·")
        self._dot.setStyleSheet("color:#9a958c;background:transparent;padding:0 4px;")
        head.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignTop)

        self._summary = QLabel()
        self._summary.setObjectName("GenTrajectorySummary")
        self._summary.setStyleSheet("background:transparent;font-size:12px;")
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        head.addWidget(self._summary, 1)

        self._state = QLabel("")
        self._state.setStyleSheet("font-size:11px;background:transparent;")
        head.addWidget(self._state, 0, Qt.AlignmentFlag.AlignTop)

        self._chevron = QLabel("")
        self._chevron.setFixedWidth(14)
        self._chevron.setStyleSheet("color:#9a958c;font-size:12px;background:transparent;")
        self._chevron.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignTop)

        root.addLayout(head)

        self._body_card = QFrame()
        self._body_card.setObjectName("GenTrajectoryBodyCard")
        card_lay = QVBoxLayout(self._body_card)
        card_lay.setContentsMargins(12, 8, 12, 10)
        card_lay.setSpacing(0)
        self._body = QLabel("")
        self._body.setObjectName("GenTrajectoryBody")
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.setStyleSheet("background:transparent;")
        card_lay.addWidget(self._body)
        self._body_card.hide()
        root.addWidget(self._body_card)

        self._apply_row_style(False)
        self._refresh_summary()
        self._refresh_chevron()
        self.set_running(title)

    def _is_dark(self) -> bool:
        try:
            from gui.styles.theme import current_theme_from_config
            return current_theme_from_config() == "dark"
        except Exception:
            from PySide6.QtGui import QPalette
            c = self.palette().color(QPalette.ColorRole.Window)
            return c.lightness() < 140

    def _apply_row_style(self, hover: bool | None = None):
        if hover is not None:
            self._hovered = hover
        dark = self._is_dark()
        if self._hovered:
            bg = "#23272e" if dark else "#f7f4ee"
        else:
            bg = "transparent"
        self.setStyleSheet(
            f"QFrame#GenTrajectoryStep {{"
            f"background-color:{bg};"
            f"border:none;"
            f"border-radius:8px;"
            f"}}"
        )

    def enterEvent(self, event):
        self._apply_row_style(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_row_style(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_expand()
        super().mousePressEvent(event)

    def toggle_expand(self):
        if not (self._full_body or "").strip():
            return
        self._expanded = not self._expanded
        self._body_card.setVisible(self._expanded)
        self._refresh_chevron()

    def _refresh_chevron(self):
        has = bool((self._full_body or "").strip())
        self._chevron.setVisible(has)
        if not has:
            return
        self._chevron.setText("▾" if self._expanded else "▸")

    def _one_line(self, text: str, limit: int = 72) -> str:
        t = (text or "").replace("\n", " ").strip()
        if len(t) > limit:
            return t[: limit - 1] + "…"
        return t

    def _refresh_summary(self):
        # 摘要优先用正文首行，否则用标题
        body = (self._full_body or "").strip()
        if body:
            first = body.splitlines()[0].strip()
            summary = self._one_line(first)
        else:
            summary = self._one_line(self._title_text)
        self._summary.setText(summary)
        self._summary.setToolTip(self._title_text if self._title_text else summary)

    def _set_status_ui(self, status: str):
        self._status = status
        text = {
            "pending": "等待",
            "running": "…",
            "done": "",
            "error": "失败",
        }.get(status, "")
        color = {
            "pending": "#9a958c",
            "running": "#7a776f",
            "done": "#2f9a5f",
            "error": "#c44b4b",
        }.get(status, "#9a958c")
        self._state.setText(text)
        self._state.setStyleSheet(f"color:{color};font-size:11px;background:transparent;")
        self._state.setVisible(bool(text))

    def set_running(self, title: str | None = None):
        if title is not None:
            self._title_text = title
            self._refresh_summary()
        self._set_status_ui("running")

    def set_pending(self, title: str | None = None, body: str | None = None):
        if title is not None:
            self._title_text = title
        if body is not None:
            self.set_body(body, expand=False)
        else:
            self._refresh_summary()
        self._set_status_ui("pending")

    def set_done(self, title: str | None = None, body: str | None = None):
        if title is not None:
            self._title_text = title
        if body is not None:
            self.set_body(body, expand=False)
        else:
            self._refresh_summary()
        self._set_status_ui("done")

    def set_error(self, title: str | None = None, body: str | None = None):
        if title is not None:
            self._title_text = title
        if body is not None:
            # 失败默认展开，方便看原因
            self.set_body(body, expand=True)
        else:
            self._refresh_summary()
        self._set_status_ui("error")

    def set_body(self, body: str, *, expand: bool | None = None):
        text = (body or "").strip()
        self._full_body = text
        if not text:
            self._body.hide()
            self._body.clear()
            self._body_card.hide()
            self._expanded = False
            self._refresh_chevron()
            self._refresh_summary()
            return

        # 展开区保留较完整正文；过长仍截断避免卡 UI
        max_chars = 6000 if self._kind == "Plan" else 3000
        shown = text if len(text) <= max_chars else text[:max_chars] + "\n…"
        self._body.setText(shown)
        if expand is True:
            self._expanded = True
        elif expand is False:
            self._expanded = False
        # expand is None → 保持当前展开状态
        self._body_card.setVisible(self._expanded)
        self._refresh_chevron()
        self._refresh_summary()

    def export_dict(self) -> dict:
        return {
            "kind": self._kind,
            "title": self._title_text,
            "status": self._status,
            "body": self._full_body,
        }


class GenTrajectory(QWidget):
    """线性可滚动任务流。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("GenTrajectory")
        self._steps: list[_StepRow] = []
        self._by_key: dict[str, _StepRow] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        head = QHBoxLayout()
        title = QLabel("生成轨迹")
        tf = QFont()
        tf.setBold(True)
        title.setFont(tf)
        head.addWidget(title)
        self._hint = QLabel("生成或修订时逐步显示 · 点击行可展开")
        self._hint.setObjectName("MutedLabel")
        head.addWidget(self._hint, 1)

        self._scroll_bottom_btn = QToolButton()
        self._scroll_bottom_btn.setObjectName("GhostButton")
        self._scroll_bottom_btn.setText("↓")
        self._scroll_bottom_btn.setToolTip("滚到最新")
        self._scroll_bottom_btn.setFixedSize(QSize(24, 24))
        self._scroll_bottom_btn.clicked.connect(self._scroll_to_bottom)
        head.addWidget(self._scroll_bottom_btn)
        root.addLayout(head)

        self._empty = QLabel(
            "点上方「生成脚本」后，规划 / 分任务 / 合并 / 校验\n"
            "会按步骤出现在这里。点击任一行可展开详情。"
        )
        self._empty.setObjectName("GenTrajectoryEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        root.addWidget(self._empty, 1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}"
        )
        self._scroll.hide()

        self._inner = QWidget()
        self._inner.setObjectName("GenTrajectoryInner")
        self._inner.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._inner.setStyleSheet("background:transparent;")
        self._list = QVBoxLayout(self._inner)
        self._list.setContentsMargins(2, 4, 2, 4)
        self._list.setSpacing(2)
        self._list.addStretch(1)
        self._scroll.setWidget(self._inner)
        root.addWidget(self._scroll, 1)

        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._show_empty(True)

    def _show_empty(self, empty: bool):
        self._empty.setVisible(empty)
        self._scroll.setVisible(not empty)

    def set_status_hint(self, text: str):
        base = " · 点击行可展开"
        t = (text or "").strip()
        self._hint.setText((t + base) if t else ("生成或修订时逐步显示" + base))

    def _scroll_to_bottom(self):
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear(self):
        for w in self._steps:
            self._list.removeWidget(w)
            w.deleteLater()
        self._steps.clear()
        self._by_key.clear()
        self.set_status_hint("生成或修订时逐步显示")
        self._show_empty(True)

    def begin_run(self, label: str = "开始生成"):
        self.clear()
        self._show_empty(False)
        self.set_status_hint(label)
        self.add_step("Info", label, key="run", running=True)

    def add_step(
        self,
        kind: str,
        title: str,
        *,
        key: str | None = None,
        body: str = "",
        running: bool = True,
    ) -> _StepRow:
        row = _StepRow(kind, title)
        if body:
            row.set_body(body, expand=False)
        if not running:
            row.set_done()
        idx = self._list.count() - 1
        self._list.insertWidget(max(0, idx), row)
        self._steps.append(row)
        if key:
            self._by_key[key] = row
        self._show_empty(False)
        self._scroll_to_bottom()
        return row

    def ensure_step(self, key: str, kind: str, title: str) -> _StepRow:
        row = self._by_key.get(key)
        if row is None:
            return self.add_step(kind, title, key=key, running=True)
        return row

    def update_step(
        self,
        key: str,
        kind: str,
        title: str,
        *,
        status: str = "running",
        body: str = "",
    ):
        row = self.ensure_step(key, kind, title)
        if status == "pending":
            row.set_pending(title, body if body else None)
        elif status == "running":
            row.set_running(title)
            if body:
                row.set_body(body, expand=False)
        elif status == "error":
            row.set_error(title, body)
        else:
            row.set_done(title, body if body else None)
        self._scroll_to_bottom()

    def finish_step(
        self,
        key: str,
        *,
        title: str | None = None,
        body: str = "",
        error: bool = False,
    ):
        row = self._by_key.get(key)
        if row is None:
            kind = "Error" if error else "Info"
            row = self.add_step(kind, title or key, key=key, running=False)
        if error:
            row.set_error(title, body)
        else:
            row.set_done(title, body if body else None)
        self._scroll_to_bottom()

    def fail_run(self, message: str):
        self.add_step("Error", "失败", body=message, running=False)
        if self._steps:
            last = self._steps[-1]
            last.set_error("失败", message)
        self.set_status_hint("已失败")

    def succeed_run(self, message: str = "完成"):
        if "run" in self._by_key:
            self.finish_step("run", title=message)
        self.set_status_hint(message)

    def mark_cancelled(self):
        self.add_step("Info", "已取消", running=False)
        self.set_status_hint("已取消")

    def export_snapshot(self) -> list[dict]:
        out: list[dict] = []
        for key, row in self._by_key.items():
            d = row.export_dict()
            d["key"] = key
            out.append(d)
        return out
