"""协作工作台 UI + CollaboratorEngine（正式版：GUI 只渲染 ViewUpdate）。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QRect, QMimeData, QUrl, QSize
from PySide6.QtGui import (
    QFont, QPixmap, QPainter, QPen, QColor, QMouseEvent, QDrag, QDragEnterEvent, QDropEvent,
    QTextOption, QIntValidator,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QScrollArea, QLineEdit, QSizePolicy, QSplitter,
    QFileDialog, QMessageBox, QApplication, QMenu, QToolButton, QInputDialog,
    QTextEdit, QLayout,
)

from core.path import PROJECT_ROOT

_STATUS_TEXT = {
    "draft": "草稿",
    "preview": "预览中",
    "acked": "已确认",
    "running": "试跑至此",
    "failed": "失败",
}

_COLLAB_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
_MIME_ASSET_ID = "application/x-minashigo-collab-asset"


def _hold(widget) -> None:
    """按文字宽度站住。横向不够时由外层换行，不把字压扁。"""
    widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    w = max(widget.sizeHint().width(), widget.minimumSizeHint().width(), 1)
    h = max(24, widget.sizeHint().height(), widget.minimumSizeHint().height())
    widget.setFixedSize(w, h)


class _FlowLayout(QLayout):
    """按钮行：放得下就横排，放不下就换行，绝不压缩子控件。"""

    def __init__(self, parent=None, spacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def clear(self) -> None:
        while self._items:
            item = self._items.pop()
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do(rect, test=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        w = 0
        h = 0
        gap = self.spacing()
        for item in self._items:
            s = item.sizeHint()
            w += s.width() + gap
            h = max(h, s.height())
        if self._items:
            w = max(0, w - gap)
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect: QRect, *, test: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_h = 0
        gap = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            nxt = x + hint.width() + gap
            if nxt - gap > effective.right() + 1 and line_h > 0:
                x = effective.x()
                y += line_h + gap
                nxt = x + hint.width() + gap
                line_h = 0
            if not test:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = nxt
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class _FlowHost(QWidget):
    """给工具按钮用的换行条。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.flow = _FlowLayout(self, spacing=4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def add(self, widget, *, lock: bool = True) -> None:
        if lock:
            _hold(widget)
        self.flow.addWidget(widget)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.flow.heightForWidth(width)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        h = self.heightForWidth(max(1, event.size().width()))
        if h > 0 and self.minimumHeight() != h:
            self.setMinimumHeight(h)
            self.updateGeometry()


def _image_paths_from_mime(mime) -> list[Path]:
    """从拖放 mime 提取本地图片路径。"""
    out: list[Path] = []
    if mime is None:
        return out
    if mime.hasUrls():
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in _COLLAB_IMAGE_EXTS:
                out.append(p)
    return out


def _mime_has_images(mime) -> bool:
    if mime is None:
        return False
    if mime.hasFormat(_MIME_ASSET_ID):
        return True
    return bool(_image_paths_from_mime(mime))


_SYSTEM_TEXT_HINTS = (
    "已有画面",
    "已载入画面",
    "已截取",
    "已加载",
    "已抓取",
    "已写入",
    "正在抓取",
    "正在分析",
    "窗口将暂时",
    "已取消",
    "无效画面",
    "抓取失败",
    "抓取超时",
    "抓取未开始",
    "探针失败",
    "协作引擎已就绪",
    "请先在「1. API",
    "无法打开",
    "上一轮还在思考",
    "写入生成页失败",
    "没有可用画面",
    "LLM 调用失败",
    "账号「",
)


def _infer_bubble_role(role: str, text: str) -> str:
    r = (role or "").strip() or "agent"
    if r in ("user", "system", "process"):
        return r
    t = (text or "").strip()
    if any(t.startswith(h) for h in _SYSTEM_TEXT_HINTS):
        return "system"
    return "agent"


def _set_md_label(label: QLabel, text: str) -> None:
    """Agent/用户气泡：用 Qt 内置 Markdown 渲染（**粗体**、列表等）。"""
    label.setTextFormat(Qt.TextFormat.MarkdownText)
    label.setText(text or "")


# 与右侧「画面预览」工具条重复的芯片，对话区不再展示，避免挤占输入区
_FRAME_ACTION_CHIPS = frozenset(
    {
        "用当前窗口",
        "开始截图",
        "重新截图",
        "截一张图",
        "截图",
        "框选截图",
        "选已有截图",
        "选已有图",
        "换一张图",
        "选截图",
    }
)
# 用户不应看到「选技能」话术芯片
_SKILL_MENU_CHIPS = frozenset(
    {
        "选技能",
        "选择技能",
        "选一个技能",
    }
)


def _filter_chat_chips(labels: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in labels or []:
        t = str(raw or "").strip()
        if not t or t in _FRAME_ACTION_CHIPS or t in _SKILL_MENU_CHIPS or t in seen:
            continue
        if t.endswith("技能") and ("选" in t or "选择" in t):
            continue
        seen.add(t)
        out.append(t)
    return out


def _draft_target_path(root: Path, stem: str, code: str = "") -> Path:
    """协作草稿落盘路径：同名不覆盖，内容一致则复用原文件。"""
    root = Path(root)
    stem = (stem or "collab_script").strip() or "collab_script"
    cand = root / f"{stem}.py"
    for i in range(2, 60):
        if not cand.is_file():
            return cand
        try:
            if cand.read_text(encoding="utf-8", errors="replace") == (code or ""):
                return cand
        except Exception:
            return cand
        cand = root / f"{stem}_{i}.py"
    return root / f"{stem}_{int(time.time())}.py"


class CollabChipBar(QWidget):
    """选项芯片条。"""

    chip_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabChipBar")
        self.setMinimumWidth(0)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)
        self._lay.addStretch(1)

    def set_chips(self, labels: list[str]) -> None:
        while self._lay.count() > 1:
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for text in labels:
            btn = QPushButton(text)
            btn.setObjectName("CollabChip")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.setSizePolicy(
                QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
            )
            btn.clicked.connect(lambda _=False, t=text: self.chip_clicked.emit(t))
            self._lay.insertWidget(self._lay.count() - 1, btn)
        self.setVisible(bool(labels))


class CollabChatInput(QTextEdit):
    """对话输入：按控件宽度软换行（不往文本里插换行符）；高度随内容涨。

    Enter 发送；Shift+Enter 才插入真正换行。
    """

    send_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabChatInput")
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setPlaceholderText("用一句话说目标，例如：刷这个关卡直到金币不够…")
        self.document().contentsChanged.connect(self._adjust_height)
        self._min_h = 36
        self._max_h = 120
        self.setFixedHeight(self._min_h)
        QTimer.singleShot(0, self._adjust_height)

    def text(self) -> str:
        return self.toPlainText()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & (
                Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
            ):
                # Shift/Ctrl+Enter：插入真正换行
                super().keyPressEvent(event)
                return
            event.accept()
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._adjust_height()

    def _adjust_height(self) -> None:
        doc = self.document()
        # 按当前可视宽度算文档高度（软换行），不改动纯文本内容
        doc.setTextWidth(max(40.0, float(self.viewport().width())))
        h = int(doc.size().height() + self.contentsMargins().top()
                + self.contentsMargins().bottom() + 12)
        h = max(self._min_h, min(self._max_h, h))
        if self.height() != h:
            self.setFixedHeight(h)


class CollabChatPane(QFrame):
    """左侧对话轨（Cursor 风：上记录、下 composer / switch）。"""

    send_requested = Signal(str)
    chip_clicked = Signal(str)
    new_session_clicked = Signal()
    history_picked = Signal(str)
    history_delete_requested = Signal(str)
    history_clear_requested = Signal()
    history_menu_about_to_show = Signal()
    permission_resolved = Signal(str, bool)  # key, allowed
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabChatPane")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        from backend.script_generator.collaborator.probe_script import default_permissions

        self._granted = default_permissions()
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(420)
        self._activity_timer.timeout.connect(self._pulse_activity)
        self._pulse_on = False
        self._stream_body = None
        self._stream_holder = None
        self._hint_host = None
        self._hint_lay = None
        self._hint_stream = None
        self._hint_last = ""
        self._last_bubble_key = None
        self._pending_perm_key = ""
        self._sending = False

        # ── 上：会话记录 ──
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setObjectName("CollabChatScroll")
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._bubble_host = QWidget()
        self._bubble_host.setObjectName("CollabBubbleHost")
        self._bubble_host.setMinimumWidth(0)
        self._bubble_host.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._bubble_lay = QVBoxLayout(self._bubble_host)
        self._bubble_lay.setContentsMargins(4, 4, 4, 8)
        self._bubble_lay.setSpacing(8)
        self._bubble_lay.addStretch(1)
        self._scroll.setWidget(self._bubble_host)
        lay.addWidget(self._scroll, 1)
        self.setMinimumWidth(0)

        # 活动条贴在 composer 上方（内部过程，不占记录区）
        self._activity = QFrame()
        self._activity.setObjectName("CollabActivityBar")
        self._activity.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._activity.hide()
        act_lay = QHBoxLayout(self._activity)
        act_lay.setContentsMargins(10, 5, 10, 5)
        act_lay.setSpacing(8)
        self._activity_dot = QLabel("●")
        self._activity_dot.setObjectName("CollabActivityDot")
        act_lay.addWidget(self._activity_dot)
        self._activity_label = QLabel("")
        self._activity_label.setObjectName("CollabActivityText")
        self._activity_label.setWordWrap(True)
        act_lay.addWidget(self._activity_label, 1)
        lay.addWidget(self._activity)

        # 已授权摘要 / 升权卡：紧挨输入框，像 Cursor 的 context 卡
        self._perm_status = QLabel("")
        self._perm_status.setObjectName("CollabPermStatus")
        self._perm_status.setWordWrap(True)
        self._perm_status.hide()
        lay.addWidget(self._perm_status)

        self._perm_ask = QFrame()
        self._perm_ask.setObjectName("CollabPermAsk")
        self._perm_ask.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._perm_ask.hide()
        ask_lay = QVBoxLayout(self._perm_ask)
        ask_lay.setContentsMargins(10, 8, 10, 8)
        ask_lay.setSpacing(6)
        self._perm_ask_title = QLabel("")
        self._perm_ask_title.setObjectName("CollabPermAskTitle")
        self._perm_ask_title.setWordWrap(True)
        ask_lay.addWidget(self._perm_ask_title)
        self._perm_ask_body = QLabel("")
        self._perm_ask_body.setObjectName("CollabPermAskBody")
        self._perm_ask_body.setWordWrap(True)
        ask_lay.addWidget(self._perm_ask_body)
        ask_btns = QHBoxLayout()
        ask_btns.setSpacing(8)
        self._perm_allow_btn = QPushButton("允许（本会话）")
        self._perm_allow_btn.setObjectName("PrimaryButton")
        self._perm_allow_btn.setFixedHeight(28)
        self._perm_deny_btn = QPushButton("拒绝")
        self._perm_deny_btn.setObjectName("GhostButton")
        self._perm_deny_btn.setFixedHeight(28)
        self._perm_ask_granted = False
        ask_btns.addWidget(self._perm_allow_btn)
        ask_btns.addWidget(self._perm_deny_btn)
        ask_btns.addStretch(1)
        ask_lay.addLayout(ask_btns)
        self._perm_allow_btn.clicked.connect(self._on_perm_allow)
        self._perm_deny_btn.clicked.connect(self._on_perm_deny)
        lay.addWidget(self._perm_ask)

        # ── 下：Cursor 风 composer（switch + 输入）──
        self._composer = QFrame()
        self._composer.setObjectName("CollabComposer")
        self._composer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        comp = QVBoxLayout(self._composer)
        comp.setContentsMargins(10, 8, 10, 8)
        comp.setSpacing(6)

        self._chips = CollabChipBar()
        self._chips.chip_clicked.connect(self.chip_clicked.emit)
        self._chips.hide()
        comp.addWidget(self._chips)

        self._input = CollabChatInput()
        self._input.setObjectName("CollabChatInput")
        self._input.send_requested.connect(self._emit_send)
        self._input.document().contentsChanged.connect(self._refresh_send_label)
        comp.addWidget(self._input)

        switch = QHBoxLayout()
        switch.setContentsMargins(0, 0, 0, 0)
        switch.setSpacing(4)
        self._hist_btn = QToolButton()
        self._hist_btn.setText("历史")
        self._hist_btn.setObjectName("CollabSwitchBtn")
        self._hist_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._hist_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._hist_btn.setFixedHeight(24)
        self._hist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hist_btn.setToolTip("切换或删除以往协作会话")
        self._hist_menu = QMenu(self._hist_btn)
        self._hist_btn.setMenu(self._hist_menu)
        self._hist_menu.aboutToShow.connect(self.history_menu_about_to_show.emit)
        switch.addWidget(self._hist_btn)

        self._new_btn = QToolButton()
        self._new_btn.setText("新会话")
        self._new_btn.setObjectName("CollabSwitchBtn")
        self._new_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._new_btn.setFixedHeight(24)
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.setToolTip("开始新协作（当前会话已自动保存）")
        self._new_btn.clicked.connect(self.new_session_clicked.emit)
        switch.addWidget(self._new_btn)

        switch.addStretch(1)

        self._attach_btn = QToolButton()
        self._attach_btn.setText("截图")
        self._attach_btn.setObjectName("CollabSwitchBtn")
        self._attach_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._attach_btn.setFixedHeight(24)
        self._attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._attach_btn.setToolTip("隐藏窗口后暗幕框选截图")
        switch.addWidget(self._attach_btn)

        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("CollabSendBtn")
        self._send_btn.setFixedHeight(24)
        self._send_btn.setMinimumWidth(52)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setToolTip("Enter 发送 · Shift+Enter 换行")
        self._send_btn.clicked.connect(self._emit_send)
        switch.addWidget(self._send_btn)
        comp.addLayout(switch)

        lay.addWidget(self._composer)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_bubble_widths()

    def _chat_viewport_width(self) -> int:
        return max(120, int(self._scroll.viewport().width()) - 4)

    def _sync_bubble_widths(self) -> None:
        """气泡随会话栏宽度收缩，避免出现水平滚动条。"""
        vw = self._chat_viewport_width()
        self._bubble_host.setMaximumWidth(vw + 4)
        agent_w = max(120, int(vw * 0.94))
        user_w = max(120, int(vw * 0.88))
        sys_w = max(120, vw)
        for frame in self._bubble_host.findChildren(QFrame):
            name = frame.objectName()
            if name == "CollabBubbleAgent":
                frame.setMaximumWidth(agent_w)
                frame.setMinimumWidth(0)
            elif name == "CollabBubbleUser":
                frame.setMaximumWidth(user_w)
                frame.setMinimumWidth(0)
            elif name in ("CollabBubbleSystem", "CollabBubbleProcess"):
                frame.setMaximumWidth(sys_w)
                frame.setMinimumWidth(0)
        for lab in self._bubble_host.findChildren(QLabel):
            if lab.objectName() in (
                "CollabBubbleBody",
                "CollabBubbleSystemBody",
                "CollabProcessBody",
            ):
                lab.setMinimumWidth(0)
                lab.setWordWrap(True)

    def _prepare_bubble_label(self, body: QLabel) -> None:
        body.setWordWrap(True)
        body.setMinimumWidth(0)
        body.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )

    def set_sending(self, busy: bool) -> None:
        """等待模型时，发送钮改成停止。"""
        self._sending = bool(busy)
        btn = self._send_btn
        if self._sending:
            btn.setText("停止")
            btn.setToolTip("停止当前回答")
            btn.setProperty("busy", "true")
        else:
            btn.setText("发送")
            btn.setToolTip("Enter 发送 · Shift+Enter 换行")
            btn.setProperty("busy", "false")
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        btn.update()

    def set_continue_step(self, step: str) -> None:
        """计划没做完时，空输入的发送钮变成继续。"""
        self._continue_step = (step or "").strip()
        self._refresh_send_label()

    def _refresh_send_label(self) -> None:
        if getattr(self, "_sending", False):
            return
        btn = getattr(self, "_send_btn", None)
        if btn is None:
            return
        step = getattr(self, "_continue_step", "") or ""
        typed = bool(self._input.text().strip()) if getattr(self, "_input", None) else False
        if step and not typed:
            btn.setText("继续")
            btn.setToolTip(step or "继续计划里的下一步")
        else:
            btn.setText("发送")
            btn.setToolTip("Enter 发送 · Shift+Enter 换行")
        btn.setProperty("busy", "false")
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        btn.update()

    def _emit_send(self) -> None:
        if getattr(self, "_sending", False):
            self.stop_requested.emit()
            return
        text = self._input.text().strip()
        step = getattr(self, "_continue_step", "") or ""
        if not text and step:
            text = "继续：" + step
        if not text:
            return
        self.set_continue_step("")
        self._input.clear()
        self._input._adjust_height()
        self.send_requested.emit(text)

    def permissions(self) -> dict:
        from backend.script_generator.collaborator.probe_script import default_permissions

        base = default_permissions()
        granted = getattr(self, "_granted", None) or {}
        if isinstance(granted, dict):
            base.update({k: bool(v) for k, v in granted.items() if k in base})
        return base

    def set_permission(self, key: str, enabled: bool = True) -> bool:
        from backend.script_generator.collaborator.probe_script import (
            PERM_LABELS,
            default_permissions,
        )

        k = str(key or "").strip()
        alias = {
            "临时脚本": "probe_script",
            "写文件": "write_files",
            "控制窗口": "runtime_control",
        }
        k = alias.get(k, k)
        if k not in default_permissions():
            return False
        if not isinstance(getattr(self, "_granted", None), dict):
            self._granted = default_permissions()
        self._granted[k] = bool(enabled)
        self._refresh_perm_status()
        return True

    def show_permission_ask(self, key: str, *, reason: str = "") -> None:
        from backend.script_generator.collaborator.probe_script import (
            PERM_LABELS,
            PERM_REASONS,
        )

        k = str(key or "").strip()
        if not k:
            return
        label = PERM_LABELS.get(k, k)
        granted = bool(self.permissions().get(k))
        self._perm_ask_granted = granted
        self._pending_perm_key = k
        why = (reason or "").strip()
        if granted:
            self._perm_ask_title.setText(f"下一步 · {label}")
            body = "这项权限本会话已经开了。"
            if why:
                body += f"\n下一步：{why}"
            body += "\n点「继续」就做这一步。不想现在做，点「先不」。"
            self._perm_ask_body.setText(body)
            self._perm_allow_btn.setText("继续")
            self._perm_deny_btn.setText("先不")
        else:
            detail = why or PERM_REASONS.get(k, "Agent 需要更高权限才能继续。")
            self._perm_ask_title.setText(f"需要权限 · {label}")
            self._perm_ask_body.setText(f"{detail}\n允许后仅对本会话有效，可随时收回。")
            self._perm_allow_btn.setText("允许（本会话）")
            self._perm_deny_btn.setText("拒绝")
        self._perm_ask.show()

    def hide_permission_ask(self) -> None:
        self._pending_perm_key = ""
        self._perm_ask_granted = False
        self._perm_ask.hide()

    def _on_perm_allow(self) -> None:
        key = str(getattr(self, "_pending_perm_key", "") or "")
        granted = bool(getattr(self, "_perm_ask_granted", False))
        self.hide_permission_ask()
        if not key:
            return
        if not granted:
            self.set_permission(key, True)
        self.permission_resolved.emit(key, True)

    def _on_perm_deny(self) -> None:
        key = str(getattr(self, "_pending_perm_key", "") or "")
        granted = bool(getattr(self, "_perm_ask_granted", False))
        self.hide_permission_ask()
        if granted:
            self.add_process("先不跑这一步。")
            return
        if key:
            self.permission_resolved.emit(key, False)

    def _refresh_perm_status(self) -> None:
        from backend.script_generator.collaborator.probe_script import (
            PERM_LABELS,
            default_permissions,
        )

        base = default_permissions()
        elevated = []
        for k, default_on in base.items():
            if default_on:
                continue
            if self.permissions().get(k):
                elevated.append(PERM_LABELS.get(k, k))
        if not elevated:
            self._perm_status.hide()
            self._perm_status.setText("")
            return
        self._perm_status.setText(
            "本会话已授权：" + "、".join(elevated) + "  （点此收回）"
        )
        self._perm_status.show()
        # 点击收回：简单用 mousePress 重绑
        self._perm_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self._perm_status.mousePressEvent = self._on_perm_status_click  # type: ignore

    def _on_perm_status_click(self, _event=None) -> None:
        from backend.script_generator.collaborator.probe_script import default_permissions

        base = default_permissions()
        for k, default_on in base.items():
            if not default_on:
                self.set_permission(k, False)
        self.add_bubble("system", "已收回本会话的升权授权。")

    def set_chips(self, labels: list[str]) -> None:
        self._chips.set_chips(_filter_chat_chips(labels))

    def add_bubble(self, role: str, text: str) -> None:
        """role: user | agent | system | process"""
        if role == "process":
            self.add_process(text)
            return
        role = _infer_bubble_role(role, text)
        # 系统信息条不进聊天记录，避免「已写入 / 已抓取」把对话挤满
        if role == "system":
            return
        # 界面层也去重：连续相同文案不重复刷
        last = getattr(self, "_last_bubble_key", None)
        key = (role, (text or "").strip())
        if key[1] and key == last:
            return
        self._last_bubble_key = key

        card = QFrame()
        card.setObjectName("CollabBubbleUser" if role == "user" else "CollabBubbleAgent")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setMinimumWidth(0)
        vw = self._chat_viewport_width()
        card.setMaximumWidth(
            max(120, int(vw * (0.88 if role == "user" else 0.94)))
        )
        inner = QVBoxLayout(card)
        inner.setContentsMargins(12, 9, 12, 9)
        inner.setSpacing(4)
        who = QLabel("你" if role == "user" else "Agent")
        who.setObjectName(
            "CollabBubbleRoleUser" if role == "user" else "CollabBubbleRoleAgent"
        )
        body = QLabel()
        self._prepare_bubble_label(body)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setObjectName("CollabBubbleBody")
        if role == "agent":
            _set_md_label(body, text)
        else:
            body.setTextFormat(Qt.TextFormat.PlainText)
            body.setText(text or "")
        inner.addWidget(who)
        inner.addWidget(body)

        idx = max(0, self._bubble_lay.count() - 1)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            wrap.addStretch(1)
            wrap.addWidget(card, 0)
        else:
            wrap.addWidget(card, 0)
            wrap.addStretch(1)
        holder = QWidget()
        holder.setMinimumWidth(0)
        holder.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum
        )
        holder.setLayout(wrap)
        self._bubble_lay.insertWidget(idx, holder)
        QTimer.singleShot(0, self._scroll_to_bottom)
        QTimer.singleShot(0, self._sync_bubble_widths)

    def begin_agent_stream(self) -> None:
        """开一个可原地更新的 Agent 气泡（流式）。"""
        self.end_agent_stream(keep_text=False)
        card = QFrame()
        card.setObjectName("CollabBubbleAgent")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setMinimumWidth(0)
        card.setMaximumWidth(max(120, int(self._chat_viewport_width() * 0.94)))
        inner = QVBoxLayout(card)
        inner.setContentsMargins(10, 8, 10, 8)
        inner.setSpacing(2)
        who = QLabel("Agent")
        who.setObjectName("CollabBubbleRoleAgent")
        body = QLabel()
        self._prepare_bubble_label(body)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setObjectName("CollabBubbleBody")
        _set_md_label(body, "…")
        inner.addWidget(who)
        inner.addWidget(body)
        idx = max(0, self._bubble_lay.count() - 1)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(card, 0)
        wrap.addStretch(1)
        holder = QWidget()
        holder.setMinimumWidth(0)
        holder.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum
        )
        holder.setLayout(wrap)
        holder.setProperty("collabStream", True)
        self._bubble_lay.insertWidget(idx, holder)
        self._stream_body = body
        self._stream_holder = holder
        self._last_bubble_key = None
        QTimer.singleShot(0, self._scroll_to_bottom)
        QTimer.singleShot(0, self._sync_bubble_widths)

    def set_agent_stream_text(self, text: str) -> None:
        body = getattr(self, "_stream_body", None)
        if body is None:
            self.begin_agent_stream()
            body = self._stream_body
        try:
            _set_md_label(body, text or "…")
        except RuntimeError:
            self._stream_body = None
            self.begin_agent_stream()
            _set_md_label(self._stream_body, text or "…")
        QTimer.singleShot(0, self._scroll_to_bottom)

    def end_agent_stream(self, *, keep_text: bool = True, final_text: str | None = None) -> bool:
        """结束流式引用。返回本轮是否用过流式气泡。"""
        had = getattr(self, "_stream_body", None) is not None
        if had and final_text is not None:
            try:
                _set_md_label(self._stream_body, final_text)
            except RuntimeError:
                pass
        if not keep_text:
            holder = getattr(self, "_stream_holder", None)
            if holder is not None:
                try:
                    self._bubble_lay.removeWidget(holder)
                    holder.deleteLater()
                except Exception:
                    pass
        self._stream_body = None
        self._stream_holder = None
        return had

    def begin_turn_hints(self) -> None:
        """本轮思考/执行：聊天里只堆小字提示，不出大气泡。"""
        self.clear_turn_hints()
        host = QWidget()
        host.setObjectName("CollabHintHost")
        host.setMinimumWidth(0)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        idx = max(0, self._bubble_lay.count() - 1)
        self._bubble_lay.insertWidget(idx, host)
        self._hint_host = host
        self._hint_lay = lay
        self._hint_stream = None
        self._hint_last = ""

    def push_hint(self, text: str) -> None:
        text = (text or "").strip()
        if not text or text == getattr(self, "_hint_last", ""):
            return
        if getattr(self, "_hint_host", None) is None:
            self.begin_turn_hints()
        lab = QLabel(text)
        lab.setObjectName("CollabHintLine")
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        stream = getattr(self, "_hint_stream", None)
        idx = self._hint_lay.indexOf(stream) if stream is not None else -1
        if idx >= 0:
            self._hint_lay.insertWidget(idx, lab)
        else:
            self._hint_lay.addWidget(lab)
        self._hint_last = text
        while self._hint_lay.count() > 8:
            item = self._hint_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                if w is getattr(self, "_hint_stream", None):
                    self._hint_stream = None
                w.deleteLater()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def update_stream_hint(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if getattr(self, "_hint_host", None) is None:
            self.begin_turn_hints()
        lab = getattr(self, "_hint_stream", None)
        if lab is None:
            lab = QLabel()
            lab.setObjectName("CollabHintLine")
            lab.setWordWrap(True)
            self._hint_lay.addWidget(lab)
            self._hint_stream = lab
        lab.setText(text)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def clear_stream_hint(self) -> None:
        lab = getattr(self, "_hint_stream", None)
        lay = getattr(self, "_hint_lay", None)
        if lab is not None and lay is not None:
            lay.removeWidget(lab)
            lab.deleteLater()
        self._hint_stream = None

    def clear_turn_hints(self) -> None:
        host = getattr(self, "_hint_host", None)
        if host is not None:
            try:
                self._bubble_lay.removeWidget(host)
                host.deleteLater()
            except Exception:
                pass
        self._hint_host = None
        self._hint_lay = None
        self._hint_stream = None
        self._hint_last = ""

    def add_process(self, text: str) -> None:
        """过程只留在输入框上方的活动条，不往聊天记录里插信息条。"""
        text = (text or "").strip()
        if not text:
            return
        self.set_activity(text)
        self.push_hint(text)

    def set_activity(self, text: str | None) -> None:
        """顶栏实时活动；空则隐藏。"""
        text = (text or "").strip()
        if not text:
            self._activity.hide()
            self._activity_timer.stop()
            return
        self._activity_label.setText(text)
        self._activity.show()
        if not self._activity_timer.isActive():
            self._activity_timer.start()

    def _pulse_activity(self) -> None:
        self._pulse_on = not self._pulse_on
        self._activity_dot.setText("●" if self._pulse_on else "○")

    def clear_process_bubbles(self) -> None:
        """清掉本轮过程气泡（可选，一般保留作轨迹）。"""
        pass

    def _scroll_to_bottom(self) -> None:
        try:
            bar = self._scroll.verticalScrollBar()
        except RuntimeError:
            return
        bar.setValue(bar.maximum())

    def clear_bubbles(self) -> None:
        self._stream_body = None
        self._stream_holder = None
        self.clear_turn_hints()
        self._last_bubble_key = None
        while self._bubble_lay.count() > 1:
            item = self._bubble_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def set_messages(self, messages: list[dict]) -> None:
        self.clear_bubbles()
        for m in messages:
            role = str(m.get("role") or "agent")
            text = str(m.get("text") or "")
            if text:
                self.add_bubble(role, text)

    def fill_history_menu(self, items: list[dict]) -> None:
        self._hist_menu.clear()
        if not items:
            act = self._hist_menu.addAction("暂无历史会话")
            act.setEnabled(False)
            return
        for it in items:
            title = str(it.get("title") or "未命名")
            n = int(it.get("n_messages") or 0)
            ns = int(it.get("n_steps") or 0)
            label = f"{title}  · {n} 条"
            if ns:
                label += f" · {ns} 步"
            sid = str(it.get("id") or "")
            sub = self._hist_menu.addMenu(label)
            open_act = sub.addAction("打开")
            open_act.triggered.connect(lambda _=False, s=sid: self.history_picked.emit(s))
            del_act = sub.addAction("删除")
            del_act.triggered.connect(
                lambda _=False, s=sid: self.history_delete_requested.emit(s)
            )
        self._hist_menu.addSeparator()
        clear_act = self._hist_menu.addAction("清空全部历史…")
        clear_act.triggered.connect(self.history_clear_requested.emit)

class CollabAssetRail(QFrame):
    """共同视口素材区：多模板/截图缩略图；可拖入图片、拖到视口讨论、右键移出。"""

    import_file_clicked = Signal()
    import_dir_clicked = Signal()
    add_current_clicked = Signal()
    asset_clicked = Signal(str)  # asset_id — 左键聚焦
    asset_remove_clicked = Signal(str)  # asset_id — 移出素材区
    asset_rename_clicked = Signal(str)  # asset_id — 请求重命名（父级弹对话框）
    files_dropped = Signal(list)  # list[str] 外部拖入的图片路径
    set_frame_requested = Signal(str, str)  # asset_id, path — 设为共同视口底图
    clear_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabAssetRail")
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        head = QHBoxLayout()
        title = QLabel("素材区")
        tf = QFont()
        tf.setBold(True)
        title.setFont(tf)
        title.setObjectName("CollabSectionTitle")
        head.addWidget(title)
        tools = _FlowHost()
        self._cur_btn = QPushButton("加入")
        self._cur_btn.setObjectName("GhostButton")
        self._cur_btn.setToolTip("把共同视口当前底图加入素材区")
        self._add_btn = QPushButton("文件")
        self._add_btn.setObjectName("GhostButton")
        self._add_btn.setToolTip("加文件")
        self._dir_btn = QPushButton("目录")
        self._dir_btn.setObjectName("GhostButton")
        self._dir_btn.setToolTip("导入目录")
        self._clear_btn = QPushButton("清空")
        self._clear_btn.setObjectName("GhostButton")
        for btn in (self._cur_btn, self._add_btn, self._dir_btn, self._clear_btn):
            tools.add(btn)
        head.addWidget(tools, 1)
        lay.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumHeight(72)
        self._scroll.setMaximumHeight(156)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setAcceptDrops(False)
        self._flow = _FlowHost()
        self._flow.setAcceptDrops(False)
        self._scroll.setWidget(self._flow)
        lay.addWidget(self._scroll)

        self._hint = QLabel("拖入图片加入素材区；拖缩略图到上方视口可共同讨论")
        self._hint.setObjectName("MutedLabel")
        lay.addWidget(self._hint)

        self._cur_btn.clicked.connect(self.add_current_clicked.emit)
        self._add_btn.clicked.connect(self.import_file_clicked.emit)
        self._dir_btn.clicked.connect(self.import_dir_clicked.emit)
        self._clear_btn.clicked.connect(self.clear_clicked.emit)

        self._drag_start: QPoint | None = None
        self._drag_aid: str = ""
        self._drag_path: str = ""
        self._did_drag: bool = False

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        # 内部素材拖到视口时不在此接收；仅外部文件
        if event.mimeData() and event.mimeData().hasFormat(_MIME_ASSET_ID):
            event.ignore()
            return
        if _mime_has_images(event.mimeData()):
            event.acceptProposedAction()
            self.setProperty("dragOver", True)
            self.style().unpolish(self)
            self.style().polish(self)
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = _image_paths_from_mime(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit([str(p) for p in paths])
            return
        event.ignore()

    def set_assets(
        self,
        assets: list[dict],
        focused: list[str] | None = None,
    ) -> None:
        while self._flow.flow.count():
            item = self._flow.flow.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        focus = set(focused or [])
        for a in assets or []:
            aid = str(a.get("id") or "")
            name = str(a.get("name") or Path(str(a.get("path") or "")).name)
            role = str(a.get("role") or "template")
            path = Path(str(a.get("path") or ""))
            btn = QToolButton()
            btn.setObjectName("CollabAssetThumb")
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setFixedSize(64, 68)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.setProperty("asset_id", aid)
            btn.setProperty("asset_path", str(path) if path.is_file() else "")
            if path.is_file():
                pm = QPixmap(str(path))
                if not pm.isNull():
                    btn.setIcon(pm.scaled(48, 40, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
                    from PySide6.QtCore import QSize
                    btn.setIconSize(QSize(48, 40))
            star = "★" if aid in focus else ""
            label = (star + name)[:10]
            btn.setText(label)
            role_hint = "截图" if role == "screenshot" else "模板"
            btn.setToolTip(
                f"{name}\n{role_hint}\n{a.get('caption') or ''}\nid={aid}\n"
                "左键：聚焦　拖到上方视口：设为底图　右键：重命名/移出"
            )
            btn.clicked.connect(lambda _=False, i=aid: self.asset_clicked.emit(i))
            btn.customContextMenuRequested.connect(
                lambda _pos, i=aid, b=btn: self._popup_asset_menu(b, i)
            )
            btn.installEventFilter(self)
            if aid in focus:
                btn.setProperty("focused", True)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
            self._flow.add(btn, lock=False)
        n = len(assets or [])
        self._hint.setText(
            f"共 {n} 张 · ★焦点 {len(focus)}/2 · 拖入加图 · 拖到视口讨论 · 右键移出"
            if n
            else "拖入图片加入素材区；拖缩略图到上方视口可共同讨论"
        )
        self._fit_thumb_height()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_thumb_height()

    def _fit_thumb_height(self) -> None:
        """缩略图按当前宽度换行，高度跟着走，不出现横向滚动条。"""
        view_w = self._scroll.viewport().width()
        if view_w < 8:
            return
        h = self._flow.heightForWidth(view_w)
        h = max(72, min(156, int(h) + 6))
        if self._scroll.height() != h:
            self._scroll.setFixedHeight(h)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if isinstance(obj, QToolButton) and obj.objectName() == "CollabAssetThumb":
            et = event.type()
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_start = event.position().toPoint()
                self._drag_aid = str(obj.property("asset_id") or "")
                self._drag_path = str(obj.property("asset_path") or "")
                self._did_drag = False
            elif et == QEvent.Type.MouseMove and self._drag_start is not None:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    delta = event.position().toPoint() - self._drag_start
                    if delta.manhattanLength() >= QApplication.startDragDistance():
                        self._begin_asset_drag(obj)
                        self._drag_start = None
                        self._did_drag = True
                        return True
            elif et == QEvent.Type.MouseButtonRelease:
                self._drag_start = None
                if self._did_drag:
                    self._did_drag = False
                    return True  # 吞掉释放，避免拖完还触发聚焦点击
            elif et == QEvent.Type.MouseButtonDblClick:
                # 双击也可设为底图
                aid = str(obj.property("asset_id") or "")
                path = str(obj.property("asset_path") or "")
                if aid and path and Path(path).is_file():
                    self.set_frame_requested.emit(aid, path)
                    return True
        return super().eventFilter(obj, event)

    def _begin_asset_drag(self, btn: QToolButton) -> None:
        aid = self._drag_aid or str(btn.property("asset_id") or "")
        path = self._drag_path or str(btn.property("asset_path") or "")
        if not aid or not path or not Path(path).is_file():
            return
        mime = QMimeData()
        mime.setData(_MIME_ASSET_ID, aid.encode("utf-8"))
        mime.setUrls([QUrl.fromLocalFile(path)])
        mime.setText(path)
        drag = QDrag(btn)
        drag.setMimeData(mime)
        icon = btn.icon()
        if not icon.isNull():
            drag.setPixmap(icon.pixmap(48, 40))
            drag.setHotSpot(QPoint(24, 20))
        drag.exec(Qt.DropAction.CopyAction)

    def _popup_asset_menu(self, btn: QToolButton, asset_id: str) -> None:
        menu = QMenu(btn)
        act_rename = menu.addAction("重命名…")
        act_view = menu.addAction("设为共同视口底图")
        act_rm = menu.addAction("移出素材区")
        chosen = menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if chosen == act_rm:
            self.asset_remove_clicked.emit(asset_id)
        elif chosen == act_view:
            path = str(btn.property("asset_path") or "")
            if path and Path(path).is_file():
                self.set_frame_requested.emit(asset_id, path)
        elif chosen == act_rename:
            self.asset_rename_clicked.emit(asset_id)


class _CollabPreviewLabel(QLabel):
    """底图预览：支持画排除区（归一化矩形）。"""

    ban_drawn = Signal(float, float, float, float)  # nx0,ny0,nx1,ny1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._draw_ban = False
        self._drag_origin: QPoint | None = None
        self._drag_current: QPoint | None = None
        self._content_rect = QRect()  # pixmap 在 label 内的实际区域

    def set_draw_ban(self, enabled: bool) -> None:
        self._draw_ban = bool(enabled)
        self._drag_origin = None
        self._drag_current = None
        self.setCursor(
            Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def set_content_rect(self, rect: QRect) -> None:
        self._content_rect = QRect(rect)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._draw_ban and event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position().toPoint()
            self._drag_current = self._drag_origin
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._draw_ban and self._drag_origin is not None:
            self._drag_current = event.position().toPoint()
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._draw_ban
            and event.button() == Qt.MouseButton.LeftButton
            and self._drag_origin is not None
            and self._drag_current is not None
        ):
            cr = self._content_rect
            if cr.width() > 4 and cr.height() > 4:
                x0 = min(self._drag_origin.x(), self._drag_current.x())
                y0 = min(self._drag_origin.y(), self._drag_current.y())
                x1 = max(self._drag_origin.x(), self._drag_current.x())
                y1 = max(self._drag_origin.y(), self._drag_current.y())
                nx0 = (x0 - cr.x()) / cr.width()
                ny0 = (y0 - cr.y()) / cr.height()
                nx1 = (x1 - cr.x()) / cr.width()
                ny1 = (y1 - cr.y()) / cr.height()
                if abs(nx1 - nx0) > 0.01 and abs(ny1 - ny0) > 0.01:
                    self.ban_drawn.emit(nx0, ny0, nx1, ny1)
            self._drag_origin = None
            self._drag_current = None
            self.update()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._draw_ban and self._drag_origin and self._drag_current:
            p = QPainter(self)
            pen = QPen(QColor("#e85d5d"))
            pen.setWidth(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRect(QRect(self._drag_origin, self._drag_current).normalized())
            p.end()


class CollabFramePane(QFrame):
    """右侧上方：共同视口底图 + 标注层。"""

    ban_mode_changed = Signal(bool)
    ban_drawn = Signal(float, float, float, float)
    clear_overlays_clicked = Signal()
    image_dropped = Signal(str)  # 本地图片路径：外部文件或素材区拖入

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabFramePane")
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("共同视口")
        tf = QFont()
        tf.setBold(True)
        tf.setPointSize(11)
        title.setFont(tf)
        title.setObjectName("CollabSectionTitle")
        head.addWidget(title)
        tools = _FlowHost()
        self._use_win_btn = QPushButton("当前窗")
        self._use_win_btn.setObjectName("GhostButton")
        self._use_win_btn.setToolTip("用当前窗口")
        self._pick_btn = QPushButton("截图")
        self._pick_btn.setObjectName("GhostButton")
        self._pick_btn.setToolTip("选截图")
        self._ban_btn = QPushButton("排除")
        self._ban_btn.setObjectName("GhostButton")
        self._ban_btn.setCheckable(True)
        self._ban_btn.setToolTip("在底图上拖拽矩形，标记点击排除区")
        self._clear_ov_btn = QPushButton("清标")
        self._clear_ov_btn.setObjectName("GhostButton")
        self._clear_ov_btn.setToolTip("清标注")
        self._clear_btn = QPushButton("清空")
        self._clear_btn.setObjectName("GhostButton")
        self._clear_btn.setToolTip("清空底图")
        for btn in (
            self._use_win_btn,
            self._pick_btn,
            self._ban_btn,
            self._clear_ov_btn,
            self._clear_btn,
        ):
            tools.add(btn)
        head.addWidget(tools, 1)
        lay.addLayout(head)

        self._image = _CollabPreviewLabel(
            "绑窗口或拖一张图到这里共同讨论\n"
            "底图=双方共看的场景；下方素材区可拖入图片；匹配/排除区叠在底图上"
        )
        self._image.setObjectName("CollabFramePlaceholder")
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setMinimumSize(1, 120)
        self._image.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        self._image.setScaledContents(False)
        self._image.setAcceptDrops(False)
        self._image.ban_drawn.connect(self.ban_drawn.emit)
        self._image_host = QWidget()
        self._image_host.setObjectName("CollabViewportHost")
        host_lay = QVBoxLayout(self._image_host)
        host_lay.setContentsMargins(0, 0, 0, 0)
        host_lay.addWidget(self._image)
        lay.addWidget(self._image_host, 1)

        self._caption = QLabel("")
        self._caption.setObjectName("MutedLabel")
        self._caption.setWordWrap(True)
        self._caption.setMaximumHeight(48)
        lay.addWidget(self._caption)

        self._pixmap: QPixmap | None = None
        self._image_path: Path | None = None
        self._overlays: list[dict] = []
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._ban_btn.toggled.connect(self._on_ban_toggled)
        self._clear_ov_btn.clicked.connect(self.clear_overlays_clicked.emit)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if _mime_has_images(event.mimeData()):
            event.acceptProposedAction()
            self.setProperty("dragOver", True)
            self.style().unpolish(self)
            self.style().polish(self)
            return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)
        mime = event.mimeData()
        paths = _image_paths_from_mime(mime)
        if paths:
            event.acceptProposedAction()
            self.image_dropped.emit(str(paths[0]))
            return
        event.ignore()

    def _on_ban_toggled(self, on: bool) -> None:
        self._image.set_draw_ban(on)
        self.ban_mode_changed.emit(on)

    def set_overlays(self, overlays: list[dict] | None) -> None:
        self._overlays = list(overlays or [])
        self._relayout_pixmap()

    def set_image_path(self, path: Path | None, caption: str = "") -> None:
        self._image_path = path
        if path is None or not path.is_file():
            self._pixmap = None
            self._image.setPixmap(QPixmap())
            self._image.setText(
                "绑窗口或拖一张游戏截图到这里\n"
                "底图=双方共看的场景；点「截图」可暗幕框选"
            )
            self._caption.setText(caption or "")
            self._image.set_content_rect(QRect())
            return
        pm = QPixmap(str(path))
        if pm.isNull():
            from PySide6.QtGui import QImage
            import numpy as np
            import cv2

            data = np.fromfile(str(path), dtype=np.uint8)
            bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if bgr is None:
                self._image.setText(f"无法读取：{path.name}")
                return
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
            pm = QPixmap.fromImage(qimg)
        self._pixmap = pm
        self._image.setText("")
        self._relayout_pixmap()
        cap = (caption or path.name).replace("\n", " ")
        if len(cap) > 100:
            cap = cap[:100] + "…"
        self._caption.setText(cap)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._relayout_pixmap()
    def _preview_target_size(self):
        from PySide6.QtCore import QSize

        # 尽量铺满视口，避免上下大块留白
        w = max(1, self._image.width())
        h = max(1, self._image.height())
        if w < 16 or h < 16:
            host = getattr(self, "_image_host", None)
            if host is not None:
                w = max(160, host.width())
                h = max(120, host.height())
            else:
                w = max(160, self.width() - 24)
                h = max(120, self.height() - 48)
        return QSize(w, h)

    def _compose_with_overlays(self, base: QPixmap) -> QPixmap:
        if not self._overlays:
            return base
        out = QPixmap(base)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bw, bh = out.width(), out.height()
        for o in self._overlays:
            kind = o.get("kind")
            color = QColor(str(o.get("color") or ("#e85d5d" if kind == "ban" else "#5b8def")))
            pen = QPen(color)
            pen.setWidth(2)
            p.setPen(pen)
            if kind == "ban" and "nx0" in o:
                x0 = int(float(o["nx0"]) * bw)
                y0 = int(float(o["ny0"]) * bh)
                x1 = int(float(o["nx1"]) * bw)
                y1 = int(float(o["ny1"]) * bh)
                fill = QColor(color)
                fill.setAlpha(60)
                p.fillRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0), fill)
                p.drawRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0))
            # 匹配框/置信度/文件名不画进底图；名字只留在左下角说明
            continue
        p.end()
        return out

    def _relayout_pixmap(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        composed = self._compose_with_overlays(self._pixmap)
        target = self._preview_target_size()
        scaled = composed.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image.setPixmap(scaled)
        # 居中放置时的 content rect（label 可能比 pixmap 大）
        lw, lh = self._image.width(), self._image.height()
        ox = max(0, (lw - scaled.width()) // 2)
        oy = max(0, (lh - scaled.height()) // 2)
        self._image.set_content_rect(QRect(ox, oy, scaled.width(), scaled.height()))


class LogicStepCard(QFrame):
    """单条逻辑步骤卡：缩略图 + 标题；点开看说明。"""

    wrong_clicked = Signal(str)
    param_changed = Signal(str, str, int)

    def __init__(self, step: dict, *, expanded: bool = False, parent=None):
        super().__init__(parent)
        self.step_id = str(step.get("id") or "")
        status = str(step.get("status") or "draft")
        self.setObjectName("LogicStepCard")
        self.setProperty("stepStatus", status)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._chev = QLabel("▾" if expanded else "▸")
        self._chev.setObjectName("MutedLabel")
        self._chev.setFixedWidth(14)
        row.addWidget(self._chev)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(1)
        shown = str(step.get("display_label") or step.get("label") or self.step_id)
        title = QLabel(shown)
        tf = QFont()
        tf.setBold(True)
        title.setFont(tf)
        title.setObjectName("LogicStepTitle")
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_col.addWidget(title)
        thumb_name = str(step.get("thumb_name") or "").strip()
        if thumb_name and thumb_name not in shown:
            sub = QLabel(thumb_name)
            sub.setObjectName("MutedLabel")
            sub.setWordWrap(True)
            sub.setMinimumWidth(0)
            title_col.addWidget(sub)
        row.addLayout(title_col, 1)

        badge = QLabel(_STATUS_TEXT.get(status, status))
        badge.setObjectName("LogicStepBadge")
        badge.setProperty("stepStatus", status)
        badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        badge.setFixedWidth(max(28, badge.sizeHint().width()))
        row.addWidget(badge)

        # 缩略图固定在行尾，不跟输入框抢宽度
        self._thumb = QLabel()
        self._thumb.setObjectName("LogicStepThumb")
        self._thumb.setFixedSize(22, 22)
        self._thumb.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setScaledContents(False)
        self._set_thumb(str(step.get("thumb_path") or "").strip())
        row.addWidget(self._thumb)
        lay.addLayout(row)

        param = step.get("param") if isinstance(step.get("param"), dict) else None
        if param and param.get("key"):
            prow = QHBoxLayout()
            prow.setContentsMargins(16, 0, 0, 0)
            prow.setSpacing(4)
            plab = QLabel(str(param.get("label") or ""))
            plab.setObjectName("MutedLabel")
            box = QLineEdit()
            box.setObjectName("LogicStepParam")
            box.setFixedWidth(44)
            box.setFixedHeight(22)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setValidator(QIntValidator(0, 600, box))
            box.setToolTip("改这个秒数；生成脚本时按数字写")
            try:
                start = int(round(float(param.get("value") or 0)))
            except (TypeError, ValueError):
                start = 0
            start = max(0, min(600, start))
            box.setText(str(start))
            unit = QLabel(str(param.get("unit") or "秒"))
            unit.setObjectName("MutedLabel")
            key = str(param.get("key"))
            last = [start]

            def _commit(b=box, k=key, memo=last) -> None:
                raw = b.text().strip()
                try:
                    val = int(raw)
                except ValueError:
                    b.setText(str(memo[0]))
                    return
                val = max(0, min(600, val))
                b.setText(str(val))
                if val == memo[0]:
                    return
                memo[0] = val
                self.param_changed.emit(self.step_id, k, val)

            box.editingFinished.connect(_commit)
            prow.addWidget(plab)
            prow.addWidget(box)
            prow.addWidget(unit)
            prow.addStretch(1)
            lay.addLayout(prow)

        self._detail = QWidget()
        dlay = QVBoxLayout(self._detail)
        dlay.setContentsMargins(16, 2, 0, 2)
        dlay.setSpacing(4)
        hint = QLabel(str(step.get("hint") or ""))
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        hint.setVisible(bool(step.get("hint")))
        dlay.addWidget(hint)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        wrong = QPushButton("这一步不对")
        wrong.setObjectName("GhostButton")
        _hold(wrong)
        wrong.clicked.connect(lambda: self.wrong_clicked.emit(self.step_id))
        btn_row.addWidget(wrong)
        dlay.addLayout(btn_row)
        lay.addWidget(self._detail)
        self.set_expanded(expanded)

    def _set_thumb(self, path: str) -> None:
        p = Path(path) if path else None
        if p is None or not p.is_file():
            self._thumb.setText("·")
            self._thumb.setPixmap(QPixmap())
            self._thumb.setToolTip("未绑定素材")
            return
        pm = QPixmap(str(p))
        if pm.isNull():
            self._thumb.setText("·")
            self._thumb.setToolTip(p.name)
            return
        scaled = pm.scaled(
            22,
            22,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 居中裁成方图
        w, h = scaled.width(), scaled.height()
        x = max(0, (w - 22) // 2)
        y = max(0, (h - 22) // 2)
        cropped = scaled.copy(x, y, min(22, w), min(22, h))
        self._thumb.setPixmap(cropped)
        self._thumb.setText("")
        self._thumb.setToolTip(p.name)

    def set_expanded(self, on: bool) -> None:
        self._expanded = bool(on)
        self._detail.setVisible(self._expanded)
        self._chev.setText("▾" if self._expanded else "▸")

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            w = self.childAt(event.pos())
            cur = w
            while cur is not None and cur is not self:
                if isinstance(cur, (QPushButton, QLineEdit)):
                    super().mousePressEvent(event)
                    return
                cur = cur.parentWidget()
            self.set_expanded(not getattr(self, "_expanded", False))
            event.accept()
            return
        super().mousePressEvent(event)


class CollabLogicPane(QFrame):
    """右侧逻辑步骤抽屉内容：步骤列表 + 主 CTA。"""

    ack_clicked = Signal()
    problem_clicked = Signal()
    step_wrong = Signal(str)
    param_changed = Signal(str, str, int)
    collapse_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabLogicPane")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("逻辑步骤")
        tf = QFont()
        tf.setBold(True)
        tf.setPointSize(11)
        title.setFont(tf)
        title.setObjectName("CollabSectionTitle")
        head.addWidget(title)
        self._count = QLabel("")
        self._count.setObjectName("MutedLabel")
        head.addWidget(self._count, 1)
        self._shape = QLabel("")
        self._shape.setObjectName("MutedLabel")
        self._shape.setWordWrap(True)
        head.addWidget(self._shape)
        self._hide_btn = QPushButton("收起")
        self._hide_btn.setObjectName("GhostButton")
        self._hide_btn.setToolTip("收起到右侧细条，避免挡住视口按钮")
        self._hide_btn.clicked.connect(self.collapse_requested.emit)
        _hold(self._hide_btn)
        head.addWidget(self._hide_btn)
        lay.addLayout(head)

        self._hint_overflow = QLabel("点一步展开说明")
        self._hint_overflow.setObjectName("MutedLabel")
        self._hint_overflow.setWordWrap(True)
        self._hint_overflow.hide()
        lay.addWidget(self._hint_overflow)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._host = QWidget()
        self._host.setMinimumWidth(0)
        self._host_lay = QVBoxLayout(self._host)
        self._host_lay.setContentsMargins(0, 0, 0, 0)
        self._host_lay.setSpacing(8)
        self._host_lay.addStretch(1)
        self._scroll.setWidget(self._host)
        self._scroll.setMinimumHeight(120)
        lay.addWidget(self._scroll, 1)

        # 竖排 CTA，窄栏时不被挤扁
        cta = QVBoxLayout()
        cta.setSpacing(6)
        self._ack_btn = QPushButton("确认生成")
        self._ack_btn.setObjectName("PrimaryButton")
        self._ack_btn.setMinimumHeight(36)
        self._ack_btn.setToolTip("看起来对，生成脚本")
        self._ack_btn.clicked.connect(self.ack_clicked.emit)
        cta.addWidget(self._ack_btn)
        lay.addLayout(cta)
        self._ack_btn.setEnabled(False)
        self._cards: list[LogicStepCard] = []
        self._expand_all = None
        self._collapse_all = None

    def content_min_width(self) -> int:
        """放下缩略图、状态和秒数、且一行标题不被截断所需的宽度。"""
        fm = self.fontMetrics()
        title_w = fm.horizontalAdvance("等待")
        for card in self._cards:
            lab = card.findChild(QLabel, "LogicStepTitle")
            if lab is not None and lab.text():
                title_w = max(title_w, fm.horizontalAdvance(lab.text()))
        # 特别长的标题允许换行，避免抽屉把视口挤没
        title_w = min(title_w, 240)
        badge = fm.horizontalAdvance("草稿") + 16
        # 卡片边距 + 序号箭头 + 标题 + 状态 + 缩略图 + 抽屉内边距
        row = 20 + 14 + 6 + title_w + 6 + badge + 6 + 22 + 20
        param = fm.horizontalAdvance("最多等") + fm.horizontalAdvance("秒") + 44 + 40
        ack = self._ack_btn.sizeHint().width() + 28
        return max(row, param, ack, 260)

    def set_steps(self, steps: list[dict], shape_label: str = "") -> None:
        while self._host_lay.count() > 1:
            item = self._host_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cards = []
        n = len(steps)
        # 默认紧凑：不把「这一步不对」全摊开
        compact = True
        self._host_lay.setSpacing(4 if n > 4 else 6)
        focus_status = {"preview", "failed", "running"}
        focus_index = -1
        for i, step in enumerate(steps):
            data = dict(step)
            shown = str(data.get("display_label") or data.get("label") or "")
            data["display_label"] = f"{i + 1}  {shown}".rstrip()
            data["label"] = f"{i + 1}  {data.get('label', '')}"
            st = str(data.get("status") or "draft")
            expanded = st in focus_status
            if expanded and focus_index < 0:
                focus_index = i
            card = LogicStepCard(data, expanded=expanded)
            card.wrong_clicked.connect(self.step_wrong.emit)
            card.param_changed.connect(self.param_changed.emit)
            self._host_lay.insertWidget(self._host_lay.count() - 1, card)
            self._cards.append(card)
        self._shape.setText(shape_label)
        acked = sum(1 for s in steps if str(s.get("status")) == "acked")
        failed = sum(1 for s in steps if str(s.get("status")) == "failed")
        if n:
            bits = [f"{n} 步"]
            if acked:
                bits.append(f"已确认 {acked}")
            if failed:
                bits.append(f"需看 {failed}")
            self._count.setText(" · ".join(bits))
        else:
            self._count.setText("")
        self._hint_overflow.setVisible(n > 6)
        if focus_index >= 0:
            QTimer.singleShot(0, lambda i=focus_index: self._scroll_to_index(i))

    def _set_all_expanded(self, on: bool) -> None:
        for c in self._cards:
            c.set_expanded(on)

    def _scroll_to_index(self, index: int) -> None:
        if not (0 <= index < len(self._cards)):
            return
        try:
            self._scroll.ensureWidgetVisible(self._cards[index], 0, 24)
        except RuntimeError:
            return

    def set_cta(
        self,
        *,
        ack_text: str = "确认生成",
        ack_enabled: bool = False,
        problem_enabled: bool = True,
    ) -> None:
        raw = (ack_text or "确认生成").strip()
        # 窄栏用短文案，完整意思放 tooltip
        if len(raw) > 8 or "看起来对" in raw or "生成脚本" in raw:
            self._ack_btn.setText("确认生成")
            self._ack_btn.setToolTip(raw)
        else:
            self._ack_btn.setText(raw)
            self._ack_btn.setToolTip(raw)
        self._ack_btn.setEnabled(ack_enabled)


class CollabLogicRail(QWidget):
    """右侧悬挂逻辑抽屉：叠在画布列上；可拖左缘调宽；默认先收成细条。"""

    ack_clicked = Signal()
    problem_clicked = Signal()
    step_wrong = Signal(str)
    param_changed = Signal(str, str, int)
    drawer_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # 必须先于子控件 / eventFilter：布局时 Qt 会立刻回调
        self._has_steps = False
        self._open = False
        self._drawer_w = 0
        self._user_sized = False
        self._drag_origin_x = 0
        self._drag_origin_w = 0
        self._dragging = False
        self._steps_sig = ""
        self._steps_changed = False
        self._flash_left = 0
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(420)
        self._flash_timer.timeout.connect(self._tick_flash)
        self.setObjectName("CollabLogicRail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._grip = QFrame()
        self._grip.setObjectName("CollabLogicGrip")
        self._grip.setFixedWidth(6)
        self._grip.setCursor(Qt.CursorShape.SizeHorCursor)
        self._grip.setToolTip("拖动调节逻辑栏宽度")
        self._grip.hide()
        self._grip.installEventFilter(self)
        lay.addWidget(self._grip)

        self._edge = QToolButton()
        self._edge.setObjectName("CollabLogicEdge")
        self._edge.setText("逻\n辑")
        self._edge.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._edge.setFixedWidth(28)
        self._edge.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )
        self._edge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edge.setToolTip("展开逻辑步骤（右侧悬挂）")
        self._edge.clicked.connect(self.open_drawer)
        self._edge.hide()
        lay.addWidget(self._edge)

        self.pane = CollabLogicPane()
        self.pane.setMinimumWidth(0)
        self.pane.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.pane.ack_clicked.connect(self.ack_clicked.emit)
        self.pane.problem_clicked.connect(self.problem_clicked.emit)
        self.pane.step_wrong.connect(self.step_wrong.emit)
        self.pane.param_changed.connect(self.param_changed.emit)
        self.pane.collapse_requested.connect(self.close_drawer)
        lay.addWidget(self.pane, 1)

        self.pane.hide()
        self.hide()

    def _bounds(self, pw: int) -> tuple[int, int]:
        """抽屉宽度：跟着画布走，但不窄于内容，避免横向滚动或切掉缩略图。"""
        grip = 6
        try:
            need = int(self.pane.content_min_width()) + grip
        except Exception:
            need = 280
        # 视口至少留一点，按钮不够就换行，不靠横向滚动
        cap = max(200, pw - 140)
        floor = min(max(240, need), cap)
        return floor, max(floor, cap)

    def occupied_width(self) -> int:
        """画布列需要为抽屉预留的右边距（避免挡住工具按钮）。"""
        if not self._has_steps:
            return 0
        if self._open:
            return max(180, int(self.width() or self._drawer_w))
        return 28

    def set_steps(self, steps: list[dict], shape_label: str = "") -> None:
        """更新步骤。首次出现只亮右侧细条，避免一出来就盖住视口。"""
        sig = "\n".join(
            "|".join(
                str(s.get(k) or "")
                for k in ("id", "label", "when", "do", "else_goto")
            )
            for s in (steps or [])
            if isinstance(s, dict)
        )
        self._steps_changed = sig != self._steps_sig
        self._steps_sig = sig
        self.pane.set_steps(steps, shape_label=shape_label)
        had = self._has_steps
        self._has_steps = bool(steps)
        if not self._has_steps:
            self._open = False
            self._stop_flash()
            self.pane.hide()
            self._edge.hide()
            self._grip.hide()
            self.hide()
            self.drawer_changed.emit()
            return
        if not had:
            self.close_drawer()
        else:
            self.reposition()

    def set_cta(self, **kwargs) -> None:
        self.pane.set_cta(**kwargs)

    def take_steps_changed(self) -> bool:
        changed = bool(self._steps_changed)
        self._steps_changed = False
        return changed

    def nudge(self) -> None:
        """逻辑有更新或脚本已写出时，细条闪几下，点开即停。"""
        if not self._has_steps:
            return
        self._flash_left = 8
        self._edge.setToolTip("逻辑步骤有更新，点开看")
        self.raise_()
        if not self._flash_timer.isActive():
            self._flash_timer.start()
        self._tick_flash()

    def _tick_flash(self) -> None:
        if self._flash_left <= 0 or not self._has_steps:
            self._stop_flash()
            return
        self._flash_left -= 1
        self._set_alert(self._flash_left % 2 == 1)
        if self._flash_left <= 0:
            self._stop_flash()

    def _stop_flash(self) -> None:
        self._flash_left = 0
        if self._flash_timer.isActive():
            self._flash_timer.stop()
        self._set_alert(False)
        self._edge.setToolTip("展开逻辑步骤（右侧悬挂）")

    def _set_alert(self, on: bool) -> None:
        flag = "true" if on else "false"
        for widget in (self, self._edge, self.pane):
            if widget.property("alert") == flag:
                continue
            widget.setProperty("alert", flag)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def open_drawer(self) -> None:
        if not self._has_steps:
            return
        self._stop_flash()
        self._open = True
        self.pane.show()
        self._grip.show()
        self._edge.hide()
        self.show()
        self.reposition()
        self.drawer_changed.emit()

    def close_drawer(self) -> None:
        """有步骤时收成右侧细条；无步骤则完全隐藏。"""
        self._open = False
        self.pane.hide()
        self._grip.hide()
        if self._has_steps:
            self._edge.show()
            self.show()
        else:
            self._edge.hide()
            self.hide()
        self.reposition()
        self.drawer_changed.emit()

    def reposition(self) -> None:
        """相对画布列贴右悬挂；宽度可拖。"""
        parent = self.parentWidget()
        if parent is None:
            return
        if not self._has_steps:
            self.hide()
            return
        ph = max(1, parent.height())
        pw = max(1, parent.width())
        if self._open:
            floor, cap = self._bounds(pw)
            if self._user_sized and self._drawer_w:
                w = min(cap, max(floor, int(self._drawer_w)))
            else:
                w = min(cap, max(floor, int(pw * 0.4)))
            self._drawer_w = w
            self.setGeometry(pw - w, 0, w, ph)
        else:
            self.setGeometry(pw - 28, 0, 28, ph)
        self.show()
        self.raise_()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        grip = getattr(self, "_grip", None)
        if obj is grip and getattr(self, "_open", False):
            et = event.type()
            if et == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._dragging = True
                self._drag_origin_x = int(event.globalPosition().x())
                self._drag_origin_w = self.width()
                return True
            if et == QEvent.Type.MouseMove and self._dragging:
                parent = self.parentWidget()
                if parent is not None:
                    dx = self._drag_origin_x - int(event.globalPosition().x())
                    floor, cap = self._bounds(parent.width())
                    self._drawer_w = max(floor, min(cap, self._drag_origin_w + dx))
                    self._user_sized = True
                    self.reposition()
                    self.drawer_changed.emit()
                return True
            if et == QEvent.Type.MouseButtonRelease and self._dragging:
                self._dragging = False
                self.drawer_changed.emit()
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            getattr(self, "_open", False)
            and event.button() == Qt.MouseButton.LeftButton
            and float(event.position().x()) <= 8
        ):
            self._dragging = True
            self._drag_origin_x = int(event.globalPosition().x())
            self._drag_origin_w = self.width()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if getattr(self, "_dragging", False) and getattr(self, "_open", False):
            parent = self.parentWidget()
            if parent is None:
                return
            dx = self._drag_origin_x - int(event.globalPosition().x())
            floor, cap = self._bounds(parent.width())
            self._drawer_w = max(floor, min(cap, self._drag_origin_w + dx))
            self._user_sized = True
            self.reposition()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _CollabStage(QWidget):
    """画布宿主；逻辑抽屉叠在上面悬挂。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabStage")
        self._logic: CollabLogicRail | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._body = lay

    def set_logic_rail(self, rail: CollabLogicRail) -> None:
        self._logic = rail
        rail.setParent(self)
        rail.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._logic is not None:
            self._logic.reposition()
            hook = getattr(self, "_after_logic_resize", None)
            if callable(hook):
                hook()


class CollabTrialBar(QFrame):
    """底部试跑条：切到试运行页 / 现象级反馈。"""

    trial_clicked = Signal()
    ok_clicked = Signal()
    bad_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabTrialBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        lay.addWidget(QLabel("试跑"))
        self._hint = QLabel("确认逻辑并生成脚本后，可在此试跑")
        self._hint.setObjectName("MutedLabel")
        self._hint.setMinimumWidth(0)
        lay.addWidget(self._hint, 1)
        self._trial_btn = QPushButton("去试运行")
        self._trial_btn.setEnabled(False)
        self._ok_btn = QPushButton("能用")
        self._ok_btn.setEnabled(False)
        self._bad_btn = QPushButton("不行")
        self._bad_btn.setEnabled(False)
        self._ok_btn.setObjectName("GhostButton")
        self._bad_btn.setObjectName("GhostButton")
        self._trial_btn.clicked.connect(self.trial_clicked.emit)
        self._ok_btn.clicked.connect(self.ok_clicked.emit)
        self._bad_btn.clicked.connect(self.bad_clicked.emit)
        for btn in (self._trial_btn, self._ok_btn, self._bad_btn):
            _hold(btn)
            lay.addWidget(btn)

    def set_ready(self, ready: bool, hint: str = "") -> None:
        self._trial_btn.setEnabled(ready)
        self._ok_btn.setEnabled(ready)
        self._bad_btn.setEnabled(ready)
        if hint:
            self._hint.setText(hint)
        elif ready:
            self._hint.setText("脚本已就绪 · 去试运行验证")
        else:
            self._hint.setText("确认逻辑并生成脚本后，可在此试跑")


class CollabWorkbench(QWidget):
    """协作主工作台：IO（截图/抓窗）在此，业务状态在 CollaboratorEngine。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CollabWorkbench")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._banner = QLabel("")
        self._banner.setObjectName("CollabPreviewBanner")
        self._banner.setWordWrap(True)
        root.addWidget(self._banner)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("CollabSplitter")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(8)
        split.setToolTip("拖中间分隔条：调节对话 / 共同视口宽度")

        self.chat = CollabChatPane()
        self.chat.setMinimumWidth(180)
        split.addWidget(self.chat)

        # 画布宿主：共同视口+素材；逻辑抽屉只挂在这一列右侧
        self._canvas_host = _CollabStage()
        self._canvas_host.setMinimumWidth(200)
        self._canvas_inner = QWidget()
        self._canvas_lay = QVBoxLayout(self._canvas_inner)
        self._canvas_lay.setContentsMargins(0, 0, 0, 0)
        self._canvas_lay.setSpacing(4)
        self.frame = CollabFramePane()
        self.assets = CollabAssetRail()
        self._canvas_lay.addWidget(self.frame, 1)
        self._canvas_lay.addWidget(self.assets, 0)
        self._canvas_host._body.addWidget(self._canvas_inner)
        self.logic = CollabLogicRail()
        self._canvas_host.set_logic_rail(self.logic)
        self._canvas_host._after_logic_resize = self._sync_logic_pad
        split.addWidget(self._canvas_host)

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setSizes([300, 700])
        self._main_split = split
        root.addWidget(split, 1)

        self.trial_bar = CollabTrialBar()
        root.addWidget(self.trial_bar)
        self.trial_bar.trial_clicked.connect(self._goto_trial_tab)
        self.trial_bar.ok_clicked.connect(self._on_trial_ok)
        self.trial_bar.bad_clicked.connect(self._on_trial_bad)

        from backend.script_generator.collaborator import CollaboratorEngine
        from backend.script_generator.collaborator.history import CollabHistoryStore

        self.engine = CollaboratorEngine()
        self._history = CollabHistoryStore()
        self._host_panel = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self._persist_session)
        self._llm_worker = None
        self._llm_epoch = 0
        self._stream_raw = ""
        self._stream_flush = QTimer(self)
        self._stream_flush.setSingleShot(True)
        self._stream_flush.setInterval(48)
        self._stream_flush.timeout.connect(self._flush_llm_stream)

        self.chat.send_requested.connect(self._on_user_send)
        self.chat.stop_requested.connect(self._stop_llm_turn)
        self.chat.chip_clicked.connect(self._on_chip)
        self.chat.new_session_clicked.connect(self._on_new_session)
        self.chat.history_picked.connect(self._on_history_picked)
        self.chat.history_delete_requested.connect(self._on_history_delete)
        self.chat.history_clear_requested.connect(self._on_history_clear)
        self.chat.history_menu_about_to_show.connect(self._fill_history_menu)
        self.chat.permission_resolved.connect(self._on_permission_resolved)
        self._retry_after_perm: str = ""
        self.chat._attach_btn.clicked.connect(self._start_region_snip)
        self.frame._pick_btn.clicked.connect(self._pick_image)
        self.frame._clear_btn.clicked.connect(self._clear_frame)
        self.frame._use_win_btn.clicked.connect(self._toast_window)
        self.frame.ban_drawn.connect(self._on_ban_drawn)
        self.frame.clear_overlays_clicked.connect(self._on_clear_overlays)
        self.assets.import_file_clicked.connect(self._import_asset_file)
        self.assets.import_dir_clicked.connect(self._import_asset_dir)
        self.assets.add_current_clicked.connect(self._add_current_frame_as_asset)
        self.assets.files_dropped.connect(self._on_assets_files_dropped)
        self.assets.set_frame_requested.connect(self._on_asset_set_frame)
        self.assets.asset_clicked.connect(self._on_asset_focus)
        self.assets.asset_remove_clicked.connect(self._on_asset_remove)
        self.assets.asset_rename_clicked.connect(self._on_asset_rename)
        self.assets.clear_clicked.connect(self._on_clear_assets)
        self.frame.image_dropped.connect(self._on_frame_image_dropped)
        self.logic.ack_clicked.connect(self._on_logic_cta)
        self.logic.problem_clicked.connect(self._on_problem)
        self.logic.step_wrong.connect(self._on_step_wrong)
        self.logic.param_changed.connect(self._on_step_param)
        self.logic.drawer_changed.connect(self._sync_logic_splitter)

        self._restore_or_welcome()

    def _restore_or_welcome(self) -> None:
        """打开协作页：优先恢复最近一次会话，没有才欢迎语。"""
        try:
            loaded = self._history.load_latest()
        except Exception:
            loaded = None
        if loaded is None:
            self._apply_view(self.engine.welcome(), clear_chat=True)
            return
        view = self.engine.restore_session(loaded)
        self.chat.set_messages(self.engine.session.messages)
        self._apply_view(view, clear_chat=False)

    def bind_host(self, panel) -> None:
        self._host_panel = panel
        try:
            from backend.script_generator.collaborator.facade_runtime import (
                FacadeRuntimeProvider,
            )
            from backend.script_generator.collaborator.runtime_bridge import (
                set_runtime_provider,
            )

            set_runtime_provider(FacadeRuntimeProvider(panel))
        except Exception as e:
            print(f"[Collab] 运行时桥接入失败: {e}")

    def _apply_view(self, view, *, clear_chat: bool = False) -> None:
        if view is None:
            return
        if clear_chat:
            self.chat.end_agent_stream(keep_text=False)
            lay = self.chat._bubble_lay
            while lay.count() > 1:
                item = lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()

        self._banner.setText(view.banner or "")
        host = self._host_panel
        if host is not None and hasattr(host, "_set_agent_idle") and view.banner:
            try:
                host._set_agent_idle(view.banner)
            except Exception:
                pass

        if view.append_agent and view.agent_message:
            kind = getattr(view, "bubble_kind", None) or "agent"
            self.chat.add_bubble(kind, view.agent_message)
        self.chat.set_chips(list(view.chips or []))
        from backend.script_generator.collaborator.canvas import enrich_logic_steps_for_ui

        raw_steps = list(view.steps or [])
        if not raw_steps and self.engine.session.logic.steps:
            raw_steps = self.engine.session.logic.to_ui_steps()
        ui_steps = enrich_logic_steps_for_ui(
            raw_steps, list(self.engine.session.assets or [])
        )
        self.logic.set_steps(ui_steps, shape_label=view.shape_label or "")
        self.logic.set_cta(
            ack_text=view.cta_text or "看起来对，生成脚本",
            ack_enabled=bool(view.cta_enabled),
            problem_enabled=bool(view.problem_enabled),
        )
        # 不在每次 LLM 回复后重算 splitter：仅抽屉开合时由 drawer_changed → _sync_logic_splitter
        self.trial_bar.set_ready(bool(view.trial_ready), view.trial_hint or "")

        if view.preview_path:
            p = Path(view.preview_path)
            if p.is_file():
                self.frame.set_image_path(p, caption=view.preview_caption or p.name)
        self._sync_canvas_from_session(view)

        if view.code:
            self._push_code_to_host(
                view.code,
                intro=view.intro,
                script_name=view.script_name,
            )
        self._schedule_persist()

    def _sync_logic_pad(self) -> None:
        logic = getattr(self, "logic", None)
        lay = getattr(self, "_canvas_lay", None)
        if logic is None or lay is None:
            return
        pad = int(logic.occupied_width())
        lay.setContentsMargins(0, 0, pad, 0)

    def _sync_logic_splitter(self) -> None:
        """逻辑悬挂时：给画布内容留右边距，避免工具按钮被挡住。"""
        logic = getattr(self, "logic", None)
        if logic is None:
            return
        try:
            logic.reposition()
            self._sync_logic_pad()
        except Exception:
            pass

    def _sync_canvas_from_session(self, view=None) -> None:
        sess = self.engine.session
        assets = list(getattr(view, "assets", None) or sess.assets or [])
        overlays = list(getattr(view, "overlays", None) or sess.overlays or [])
        focused = list(
            getattr(view, "focused_asset_ids", None) or sess.focused_asset_ids or []
        )
        self.assets.set_assets(assets, focused)
        self.frame.set_overlays(overlays)

    def _schedule_persist(self) -> None:
        self._save_timer.start()

    def _persist_session(self) -> None:
        try:
            self.engine.session.ensure_id()
            self._history.save(self.engine.session)
        except Exception:
            pass

    def _fill_history_menu(self) -> None:
        try:
            items = self._history.list_summaries(18)
        except Exception:
            items = []
        self.chat.fill_history_menu(items)

    def _on_new_session(self) -> None:
        self._persist_session()
        self.frame.set_image_path(None)
        from backend.script_generator.collaborator.probe_script import default_permissions

        self.chat._granted = default_permissions()
        self.chat.hide_permission_ask()
        self.chat._refresh_perm_status()
        self._retry_after_perm = ""
        self._apply_view(self.engine.welcome(), clear_chat=True)

    def _on_history_picked(self, session_id: str) -> None:
        self._persist_session()
        loaded = self._history.load(session_id)
        if loaded is None:
            self.chat.add_bubble("system", "无法打开该历史会话。")
            return
        view = self.engine.restore_session(loaded)
        self.chat.set_messages(self.engine.session.messages)
        self._apply_view(view, clear_chat=False)

    def _on_history_delete(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        title = sid
        try:
            for it in self._history.list_summaries(40):
                if str(it.get("id") or "") == sid:
                    title = str(it.get("title") or sid)
                    break
        except Exception:
            pass
        ret = QMessageBox.question(
            self,
            "删除历史会话",
            f"确定删除会话「{title}」？\n删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        cur = str(self.engine.session.session_id or "")
        ok = self._history.delete(sid)
        if not ok:
            self.chat.add_bubble("system", "删除失败，请检查文件权限。")
            return
        if cur == sid:
            # 当前会话被删：不落盘，直接新开
            if self._save_timer.isActive():
                self._save_timer.stop()
            self.frame.set_image_path(None)
            self._apply_view(self.engine.welcome(), clear_chat=True)
            self.chat.add_bubble("system", "当前会话已删除，已开始新会话。")
        else:
            self.chat.add_bubble("system", f"已删除历史会话「{title}」。")

    def _on_history_clear(self) -> None:
        ret = QMessageBox.question(
            self,
            "清空全部历史",
            "确定清空全部协作历史会话？\n删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        n = self._history.delete_all()
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.frame.set_image_path(None)
        self._apply_view(self.engine.welcome(), clear_chat=True)
        self.chat.add_bubble("system", f"已清空 {n} 条历史；当前也为新会话。")

    def _note_user(self, text: str) -> None:
        self.engine.note_user(text)
        self._schedule_persist()

    def _on_user_send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._perm_auto_key = ""
        if self._llm_worker is not None and self._llm_worker.isRunning():
            self.chat.set_activity("上一轮还在思考，请稍候")
            return
        self._retry_after_perm = text
        self.chat.hide_permission_ask()
        self.chat.add_bubble("user", text)
        params = self._llm_params_from_host()
        if not params:
            print("[Collab] 未读到 LLM 参数，走本地规则路径")
            self._apply_view(
                self.engine.on_user_text_local(text, llm_configured=False)
            )
            return
        self.engine.begin_user_turn(text)
        self._start_llm_turn(params)

    def _llm_params_from_host(self) -> dict | None:
        host = self._host_panel
        if host is None:
            print("[Collab] host 未绑定，无法读 API 配置")
            return None
        getter = getattr(host, "_collab_llm_params", None)
        if callable(getter):
            try:
                params = getter()
            except Exception as e:
                print(f"[Collab] 读取 API 配置异常: {e}")
                params = None
            if params:
                return params
        # 测连成功后的缓存：避免控件瞬时空读误判「未配置」
        cached = getattr(host, "_collab_llm_params_cache", None)
        if isinstance(cached, dict) and cached.get("api_key") and cached.get("model"):
            print("[Collab] 使用测连缓存的 LLM 参数")
            return dict(cached)
        return None

    def _start_llm_turn(self, params: dict, *, resume_permission: str = "") -> None:
        from gui.widgets.script_gen_panel.workers import CollabLlmWorker

        self.chat.set_chips([])
        self._last_process_status = ""
        self._turn_trace: list[str] = []
        self._stream_raw = ""
        self.chat.end_agent_stream(keep_text=False)
        # 用户层不刷「本轮开始」；活动条提示即可
        if resume_permission:
            self.chat.set_activity("已授权，补做被拦住的一步…")
        else:
            self.chat.set_activity("Agent 处理中…")
        self.chat.set_sending(True)
        self.chat.begin_turn_hints()
        self._banner.setText("协作 · Agent 工作中…")
        host = self._host_panel
        if host is not None and hasattr(host, "_set_agent_idle"):
            try:
                host._set_agent_idle("协作 · Agent 工作中…")
            except Exception:
                pass

        payload = {
            **params,
            "session": self.engine.session.to_dict(),
            "permissions": self.chat.permissions(),
            "resume_permission": resume_permission,
        }
        self._llm_epoch += 1
        self._llm_worker = CollabLlmWorker(payload)
        self._llm_worker._epoch = self._llm_epoch
        self._llm_worker.status.connect(self._on_llm_status)
        self._llm_worker.partial.connect(self._on_llm_partial)
        self._llm_worker.finished.connect(self._on_llm_finished)
        self._llm_worker.error.connect(self._on_llm_error)
        self._llm_worker.start()

    def _on_llm_partial(self, piece: str) -> None:
        if not piece:
            return
        self._stream_raw = (getattr(self, "_stream_raw", "") or "") + piece
        if not self._stream_flush.isActive():
            self._stream_flush.start()

    def _flush_llm_stream(self) -> None:
        from backend.script_generator.collaborator.canvas import render_asset_refs_for_user
        from backend.script_generator.collaborator.llm_turn import stream_visible_preview

        preview = stream_visible_preview(getattr(self, "_stream_raw", "") or "")
        try:
            assets = list(getattr(self.engine.session, "assets", None) or [])
            preview = render_asset_refs_for_user(preview, assets)
        except Exception:
            pass
        if preview.strip():
            line = preview.strip().splitlines()[0].strip()
            if len(line) > 42:
                line = line[:42] + "…"
            self.chat.update_stream_hint(f"正在回复 · {line}")
        else:
            self.chat.set_activity(getattr(self, "_last_process_status", "") or "正在回复")

    def _on_llm_status(self, msg: str) -> None:
        msg = (msg or "").strip()
        if not msg:
            return
        # 底层 tools loop 会再推一条英文工具名，与中文轨迹重复，丢掉
        if msg.startswith("🔧 工具：") and " " not in msg.split("：", 1)[-1]:
            return
        self.chat.set_activity(msg)
        self._banner.setText(f"协作 · {msg}")
        host = self._host_panel
        if host is not None and hasattr(host, "_set_agent_idle"):
            try:
                host._set_agent_idle(f"协作 · {msg}")
            except Exception:
                pass
        # 过程气泡：去重连续相同文案
        if msg != getattr(self, "_last_process_status", ""):
            self._last_process_status = msg
            self.chat.add_process(msg)
            self._turn_trace.append(msg)

    def _llm_turn_current(self) -> bool:
        sender = self.sender()
        epoch = getattr(sender, "_epoch", None) if sender is not None else None
        if epoch is None:
            return True
        return int(epoch) == int(getattr(self, "_llm_epoch", 0))

    def _stop_llm_turn(self) -> None:
        """用户点停止：丢掉这一轮，不把半截回复写进对话。"""
        self._llm_epoch = int(getattr(self, "_llm_epoch", 0)) + 1
        worker = self._llm_worker
        self._llm_worker = None
        if worker is not None:
            try:
                worker.finished.disconnect(self._on_llm_finished)
                worker.error.disconnect(self._on_llm_error)
            except Exception:
                pass
            if worker.isRunning():
                worker.requestInterruption()
                worker.terminate()
                worker.wait(800)
        if self._stream_flush.isActive():
            self._stream_flush.stop()
        self._stream_raw = ""
        self.chat.set_sending(False)
        self.chat.end_agent_stream(keep_text=False)
        self.chat.clear_stream_hint()
        self.chat.push_hint("已停止")
        self.chat.set_activity("已停止")
        self._banner.setText("协作 · 已停止")

    def _on_llm_finished(self, visible: str, meta: object, session_dict: object) -> None:
        if not self._llm_turn_current():
            return
        self.chat.set_sending(False)
        try:
            if self._stream_flush.isActive():
                self._stream_flush.stop()
            self.chat.set_activity(None)
            from backend.script_generator.collaborator.collab_form import form_ui_state

            meta_d = meta if isinstance(meta, dict) else {}
            ui = form_ui_state(meta_d, self.engine.session)
            trace = meta_d.pop("_trace", None) or getattr(self, "_turn_trace", [])
            # 内部多轮不刷明细；用户层只留一句收束
            tools = [t for t in trace if "调用工具" in t or t.startswith("🔧")]
            if ui["mode"] == "ask":
                self.chat.add_process("—— 等你操作后继续 ——")
                self.chat.set_continue_step("")
            elif ui["mode"] == "continue":
                self.chat.set_activity(ui["activity"])
                self.chat.set_continue_step(ui["continue_step"])
            elif tools:
                self.chat.add_process(
                    "—— 本轮完成（用过："
                    + " → ".join(
                        t.replace("🔧 调用工具 · ", "").split("（")[0] for t in tools[:4]
                    )
                    + "）——"
                )
            else:
                self.chat.add_process("—— 本轮完成 ——")
            if ui["mode"] not in ("ask", "continue"):
                self.chat.set_continue_step("")
            if isinstance(session_dict, dict):
                if session_dict.get("skill_id"):
                    self.engine.session.skill_id = (
                        str(session_dict.get("skill_id") or "") or None
                    )
                # 工具可能改了 overlays / assets / focus
                if "overlays" in session_dict:
                    self.engine.session.overlays = list(session_dict.get("overlays") or [])
                if "assets" in session_dict:
                    self.engine.session.assets = list(session_dict.get("assets") or [])
                if "focused_asset_ids" in session_dict:
                    self.engine.session.focused_asset_ids = list(
                        session_dict.get("focused_asset_ids") or []
                    )
                art = session_dict.get("artifacts")
                if isinstance(art, dict):
                    if not isinstance(self.engine.session.artifacts, dict):
                        self.engine.session.artifacts = {}
                    self.engine.session.artifacts.update(art)
            final = str(visible or "")
            view = self.engine.apply_llm_reply(final, meta_d)
            self.chat.clear_stream_hint()
            self.chat.end_agent_stream(keep_text=False)
            self._apply_view(view)
            steps_changed = self.logic.take_steps_changed()
            if self.logic._has_steps and (
                steps_changed or bool((view.code or "").strip())
            ):
                self.logic.nudge()
            need_perm = str(meta_d.get("need_permission") or "").strip()
            if need_perm not in ("write_files", "runtime_control", "probe_script"):
                chip_perm = {
                    "打开写文件权限": "write_files",
                    "打开控制窗口权限": "runtime_control",
                    "打开临时脚本权限": "probe_script",
                }
                for chip in meta_d.get("chips") or []:
                    hit = chip_perm.get(str(chip))
                    if hit:
                        need_perm = hit
                        break
            if not need_perm and str(meta_d.get("turn") or "") != "stop":
                # GUI 兜底：正文里要了权但 meta 漏了。stop 不再从正文改口。
                from backend.script_generator.collaborator.llm_turn import (
                    _infer_need_permission_from_text,
                )

                need_perm = _infer_need_permission_from_text(final)
                if need_perm:
                    meta_d["need_permission"] = need_perm
            # 已授权：走和用户点「允许」同一条回复，不另开一套跳过逻辑
            if need_perm and self.chat.permissions().get(need_perm) and str(meta_d.get("turn") or "") != "ask":
                if getattr(self, "_perm_auto_key", "") == need_perm:
                    meta_d["need_permission"] = None
                    if str(meta_d.get("turn") or "") == "ask":
                        meta_d["turn"] = "act"
                    from backend.script_generator.collaborator.collab_form import (
                        form_ui_state,
                    )

                    ui = form_ui_state(meta_d, self.engine.session)
                    self.chat.set_chips(list(ui.get("chips") or []))
                    self.chat.set_continue_step(ui.get("continue_step") or "")
                    if ui.get("activity"):
                        self.chat.set_activity(ui["activity"])
                else:
                    self._perm_auto_key = need_perm
                    key = need_perm
                    QTimer.singleShot(
                        0, lambda k=key: self._reply_permission(k, True, auto=True)
                    )
                need_perm = ""
            if need_perm:
                if not (getattr(self, "_retry_after_perm", "") or "").strip():
                    for m in reversed(self.engine.session.messages or []):
                        if str(m.get("role") or "") == "user":
                            self._retry_after_perm = str(m.get("text") or "")
                            break
                step = ""
                plan = meta_d.get("plan") if isinstance(meta_d.get("plan"), list) else []
                if plan:
                    item = plan[0]
                    step = str(item.get("step") if isinstance(item, dict) else item or "").strip()
                self.chat.show_permission_ask(need_perm, reason=step)
                try:
                    self.chat._perm_ask.setVisible(True)
                    self.chat._perm_ask.raise_()
                except Exception:
                    pass
                self.chat.add_process(f"—— 请授权「{need_perm}」后继续 ——")
            self._stream_raw = ""
        finally:
            self._llm_worker = None

    def _resume_after_permission(self, key: str) -> None:
        """授权后只补被拦住的一步，不把上一句用户话再记一遍。"""
        if self._llm_worker is not None and self._llm_worker.isRunning():
            return
        if not (key or "").strip():
            return
        if not (getattr(self, "_retry_after_perm", "") or "").strip():
            for m in reversed(self.engine.session.messages or []):
                if str(m.get("role") or "") == "user":
                    self._retry_after_perm = str(m.get("text") or "")
                    break
        params = self._llm_params_from_host()
        if not params:
            return
        self._start_llm_turn(params, resume_permission=key)

    def _reply_permission(self, key: str, allowed: bool, *, auto: bool = False) -> None:
        """授权回复的唯一入口。用户点允许，或已授权自动回复，都走这里。"""
        from backend.script_generator.collaborator.probe_script import PERM_LABELS

        label = PERM_LABELS.get(key, key)
        if not allowed:
            self.chat.add_bubble("system", f"已拒绝「{label}」权限。")
            self._retry_after_perm = ""
            self._perm_auto_key = ""
            return
        if auto:
            self.chat.add_process(f"—— 已授权「{label}」，自动继续 ——")
        else:
            self.chat.add_bubble("system", f"已允许「{label}」（本会话）。正在继续…")
        self._resume_after_permission(key)

    def _on_permission_resolved(self, key: str, allowed: bool) -> None:
        self._perm_auto_key = ""
        self._reply_permission(key, allowed, auto=False)

    def _on_llm_error(self, err: str) -> None:
        if not self._llm_turn_current():
            return
        self.chat.set_sending(False)
        if self._stream_flush.isActive():
            self._stream_flush.stop()
        self._llm_worker = None
        self.chat.set_activity(None)
        self.chat.end_agent_stream(keep_text=False)
        self._stream_raw = ""
        self.chat.add_process(f"✗ 失败：{err}")
        self.chat.add_bubble(
            "system",
            f"LLM 调用失败：{err}\n"
            "请检查「1. API 配置」的端点 / Key / 模型；也可点芯片走本地技能。",
        )
        self.chat.set_chips(["去经典生成"])
        self._banner.setText("协作 · LLM 调用失败")
        self._schedule_persist()

    def _on_chip(self, text: str) -> None:
        if text == "继续":
            step = getattr(self.chat, "_continue_step", "") or ""
            self._on_user_send("继续：" + step if step else "继续未完成的计划")
            return
        if text == "用当前窗口":
            self.chat.add_bubble("user", text)
            self._note_user(text)
            self._toast_window()
            return
        if text in ("开始截图", "重新截图"):
            self.chat.add_bubble("user", text)
            self._note_user(text)
            self._start_region_snip()
            return
        if text in ("选已有截图", "换一张图"):
            self.chat.add_bubble("user", text)
            self._note_user(text)
            self._pick_image()
            return
        if text == "去试运行":
            self.chat.add_bubble("user", text)
            self._note_user(text)
            self._goto_trial_tab()
            return
        if text == "不用":
            self.chat.add_bubble("user", text)
            self.chat.set_chips([])
            return

        perm_chips = {
            "打开临时脚本权限": "probe_script",
            "打开写文件权限": "write_files",
            "打开控制窗口权限": "runtime_control",
        }
        if text in perm_chips:
            key = perm_chips[text]
            self.chat.add_bubble("user", text)
            self._note_user(text)
            self.chat.set_chips([])
            if self.chat.permissions().get(key):
                # 芯片点了但已有权限：直接续跑，不要空转
                if not (getattr(self, "_retry_after_perm", "") or "").strip():
                    for m in reversed(self.engine.session.messages or []):
                        if str(m.get("role") or "") == "user" and str(m.get("text") or "") != text:
                            self._retry_after_perm = str(m.get("text") or "")
                            break
                self.chat.add_process("—— 已有权限，自动继续 ——")
                self._reply_permission(key, True, auto=True)
                return
            self.chat.show_permission_ask(key)
            return

        self.chat.add_bubble("user", text)
        view = self.engine.on_chip(text)
        if view is not None:
            self._apply_view(view)

    def _on_logic_cta(self) -> None:
        from backend.script_generator.collaborator.phases import CollabPhase

        phase = self.engine.session.phase
        code = (self.engine.session.generated_code or "").strip()
        if phase == CollabPhase.READY_TRIAL and code:
            self._goto_trial_tab()
            return
        view = self.engine.on_ack()
        self._apply_view(view)
        if getattr(view, "request_script", False):
            self._start_script_write()

    def _start_script_write(self) -> None:
        """确认逻辑后请模型写 script，用户只看到「下一步」那一句。"""
        from backend.script_generator.collaborator.phases import CollabPhase

        if self._llm_worker is not None and self._llm_worker.isRunning():
            return
        params = self._llm_params_from_host()
        if not params:
            self.chat.add_bubble(
                "system",
                "还没配好模型，写不了脚本。下一步：去「1. API 配置」，或点「去经典生成」。",
            )
            self.chat.set_chips(["去经典生成"])
            return
        self.engine.session.add_message(
            "user",
            "逻辑步骤我确认了。请把当前逻辑写成可运行脚本，填 script 和 script_name。"
            "reply 只说写完了，下一步去试运行。turn=stop。",
        )
        self.engine.session.phase = CollabPhase.CODEGEN
        self._start_llm_turn(params)

    def _on_problem(self) -> None:
        self._apply_view(self.engine.on_problem())
        self.chat._input.setFocus()

    def _on_step_wrong(self, step_id: str) -> None:
        self.chat.add_bubble("user", f"步骤不对：{step_id}")
        self._apply_view(self.engine.on_step_wrong(step_id))

    def _on_step_param(self, step_id: str, key: str, value: int) -> None:
        from backend.script_generator.logic_graph import apply_step_param

        step = self.engine.session.logic.get(step_id)
        if step is None:
            return
        apply_step_param(step, key, int(value))
        self.engine.session.touch()
        self._schedule_persist()

    def _on_trial_ok(self) -> None:
        self.chat.add_bubble("user", "能用")
        self._apply_view(self.engine.on_trial_ok())

    def _on_trial_bad(self) -> None:
        self.chat.add_bubble("user", "不行")
        self._apply_view(self.engine.on_trial_bad())
        self.chat._input.setFocus()

    def _clear_frame(self) -> None:
        self.frame.set_image_path(None)
        self.frame.set_overlays([])

    def _on_assets_files_dropped(self, paths: list) -> None:
        """外部图片拖入素材区。"""
        from backend.script_generator.collaborator.canvas import add_asset_from_path

        added = []
        for raw in paths or []:
            p = Path(str(raw))
            if not p.is_file():
                continue
            item = add_asset_from_path(
                self.engine.session,
                p,
                role="template",
                caption="拖入素材",
                group="拖入",
            )
            added.append(item)
        if not added:
            return
        self._sync_canvas_from_session()
        names = "、".join(str(a.get("name") or "") for a in added[:5])
        more = f" 等 {len(added)} 张" if len(added) > 5 else ""
        self.chat.add_bubble("system", f"已拖入素材区：{names}{more}")
        self._schedule_persist()

    def _on_asset_set_frame(self, asset_id: str, path: str) -> None:
        """素材区右键 / 拖到视口：设为共同讨论底图。"""
        p = Path(path)
        if not p.is_file():
            self.chat.add_bubble("system", "素材文件不存在，无法设为底图。")
            return
        from backend.script_generator.collaborator.canvas import set_focused

        self.frame.set_image_path(p, caption=f"讨论 · {p.name}")
        self._apply_view(self.engine.on_frame(p))
        if asset_id:
            set_focused(self.engine.session, [asset_id])
            self._sync_canvas_from_session()
        self.chat.add_bubble("system", f"已将「{p.name}」设为共同视口底图，可一起讨论。")
        self._schedule_persist()

    def _on_frame_image_dropped(self, path: str) -> None:
        """共同视口接收拖入：设为底图；若来自外部文件则同步进素材区。"""
        from backend.script_generator.collaborator.canvas import add_asset_from_path

        p = Path(path)
        if not p.is_file():
            return
        # 是否已在素材区
        already = False
        for a in self.engine.session.assets or []:
            if Path(str(a.get("path") or "")) == p:
                already = True
                aid = str(a.get("id") or "")
                self._on_asset_set_frame(aid, str(p))
                return
        item = add_asset_from_path(
            self.engine.session,
            p,
            role="screenshot" if "snip" in p.name.lower() or "screen" in p.name.lower() else "template",
            caption="拖入共同视口",
            group="拖入",
        )
        self._on_asset_set_frame(str(item.get("id") or ""), str(p))
        if not already:
            # _on_asset_set_frame 已有气泡；补一句素材区
            pass

    def _import_asset_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "添加模板素材",
            str(PROJECT_ROOT / "assets" / "images"),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not path:
            return
        from backend.script_generator.collaborator.canvas import add_asset_from_path

        item = add_asset_from_path(self.engine.session, path)
        self._sync_canvas_from_session()
        self.chat.add_bubble("system", f"已加入素材：{item.get('name')}")
        self._schedule_persist()

    def _import_asset_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "导入素材目录",
            str(PROJECT_ROOT / "assets" / "images"),
        )
        if not path:
            return
        from backend.script_generator.collaborator.canvas import import_assets_dir

        added = import_assets_dir(self.engine.session, path)
        self._sync_canvas_from_session()
        self.chat.add_bubble(
            "system",
            f"已从目录导入 {len(added)} 张素材（复用识图目录图注，未做 OpenCV 全扫）。",
        )
        self._schedule_persist()

    def _on_asset_focus(self, asset_id: str) -> None:
        from backend.script_generator.collaborator.canvas import set_focused

        cur = list(self.engine.session.focused_asset_ids or [])
        aid = str(asset_id or "").strip()
        if not aid:
            return
        if aid in cur:
            cur = [x for x in cur if x != aid]
        else:
            cur = ([aid] + cur)[:2]
        set_focused(self.engine.session, cur)
        self._sync_canvas_from_session()
        self._schedule_persist()

    def _add_current_frame_as_asset(self) -> None:
        """把共同视口当前底图加入素材区（截图角色）。"""
        from backend.script_generator.collaborator.canvas import add_asset_from_path

        path = self.frame._image_path
        if path is None or not Path(path).is_file():
            # 尝试会话里的 frame / preview
            sess = self.engine.session
            for cand in (
                getattr(sess, "frame_path", None),
                getattr(sess, "last_preview_path", None),
            ):
                if cand and Path(cand).is_file():
                    path = Path(cand)
                    break
        if path is None or not Path(path).is_file():
            # 仅有内存 pixmap：落盘后再加入
            pm = getattr(self.frame, "_pixmap", None)
            if pm is None or pm.isNull():
                self.chat.add_bubble("system", "当前没有底图可加入素材区，请先截图或选图。")
                return
            from datetime import datetime

            out_dir = PROJECT_ROOT / "screenshots" / "collab"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = out_dir / f"snip_{stamp}.png"
            if not pm.save(str(path), "PNG"):
                self.chat.add_bubble("system", "截图落盘失败，无法加入素材区。")
                return
            self.frame._image_path = path

        item = add_asset_from_path(
            self.engine.session,
            path,
            role="screenshot",
            caption="用户截图",
            group="截图",
        )
        self._sync_canvas_from_session()
        self.chat.add_bubble("system", f"已加入素材区：{item.get('name')}")
        self._schedule_persist()

    def _on_asset_remove(self, asset_id: str) -> None:
        from backend.script_generator.collaborator.canvas import remove_asset

        removed = remove_asset(self.engine.session, asset_id)
        self._sync_canvas_from_session()
        if removed:
            self.chat.add_bubble(
                "system", f"已移出素材区：{removed.get('name') or asset_id}"
            )
        self._schedule_persist()

    def _on_asset_rename(self, asset_id: str) -> None:
        """右键重命名：只改交流别名，不改文件/不触发识图。"""
        from backend.script_generator.collaborator.canvas import rename_asset

        aid = str(asset_id or "").strip()
        cur = None
        for a in self.engine.session.assets or []:
            if str(a.get("id")) == aid:
                cur = a
                break
        if cur is None:
            return
        fname = str(cur.get("file_name") or Path(str(cur.get("path") or "")).name)
        old = str(cur.get("name") or fname)
        text, ok = QInputDialog.getText(
            self,
            "重命名素材",
            f"交流用别名（不改文件，不重新识图）\n原文件：{fname}",
            text=old,
        )
        if not ok:
            return
        new_name = str(text or "").strip()
        if not new_name or new_name == old:
            return
        updated = rename_asset(self.engine.session, aid, new_name)
        if not updated:
            self.chat.add_bubble("system", "重命名失败（空名称？）。")
            return
        self._sync_canvas_from_session()
        # 逻辑步骤文案 + 缩略图跟着别名走
        from backend.script_generator.collaborator.canvas import enrich_logic_steps_for_ui

        self.logic.set_steps(
            enrich_logic_steps_for_ui(
                self.engine.session.logic.to_ui_steps(),
                list(self.engine.session.assets or []),
            ),
            shape_label=self.engine.session.logic.title or "脚本草稿",
        )
        self.chat.add_bubble(
            "system",
            f"素材已改名为「{updated.get('name')}」（文件仍是 {fname}，未重新识图）。",
        )
        self._schedule_persist()

    def _on_clear_assets(self) -> None:
        self.engine.session.assets = []
        self.engine.session.focused_asset_ids = []
        self._sync_canvas_from_session()
        self.chat.add_bubble("system", "已清空素材区。")
        self._schedule_persist()

    def _on_ban_drawn(self, nx0: float, ny0: float, nx1: float, ny1: float) -> None:
        from backend.script_generator.collaborator.canvas import (
            add_ban_overlay,
            ban_overlays_as_script_snippet,
        )

        n_ban = len(
            [o for o in (self.engine.session.overlays or []) if o.get("kind") == "ban"]
        )
        item = add_ban_overlay(
            self.engine.session,
            nx0=nx0,
            ny0=ny0,
            nx1=nx1,
            ny1=ny1,
            label=f"排除区{n_ban + 1}",
        )
        if not item:
            return
        snippet = ban_overlays_as_script_snippet(self.engine.session.overlays or [])
        if not isinstance(self.engine.session.artifacts, dict):
            self.engine.session.artifacts = {}
        self.engine.session.artifacts["ban_script_snippet"] = snippet
        self._sync_canvas_from_session()
        self.chat.add_bubble(
            "system",
            f"已标注排除区（归一化 "
            f"{item.get('nx0'):.2f},{item.get('ny0'):.2f}-"
            f"{item.get('nx1'):.2f},{item.get('ny1'):.2f}）。"
            "生成/改脚本时可注入 `_COLLAB_BAN_ZONES`。",
        )
        if self.frame._ban_btn.isChecked():
            self.frame._ban_btn.setChecked(False)
        self._schedule_persist()

    def _on_clear_overlays(self) -> None:
        from backend.script_generator.collaborator.canvas import clear_overlays

        clear_overlays(self.engine.session)
        if isinstance(self.engine.session.artifacts, dict):
            self.engine.session.artifacts.pop("ban_script_snippet", None)
        self._sync_canvas_from_session()
        self.chat.add_bubble("system", "已清除共同视口标注。")
        self._schedule_persist()

    def _start_region_snip(self) -> None:
        from gui.widgets.script_gen_panel.region_snip import run_region_snip

        hide = [self.window()]

        def _ok(path, pm) -> None:
            if path is not None:
                p = Path(path)
                self.frame.set_image_path(p, caption=f"截图 {p.name}")
                self._apply_view(self.engine.on_frame(p))
            elif pm is not None and not pm.isNull():
                self.frame._pixmap = pm
                self.frame._image_path = None
                self.frame._image.setText("")
                self.frame._relayout_pixmap()
                self.frame._caption.setText("截图（未落盘）")
                self.chat.add_bubble("system", "已截取区域。请再存盘或选文件以便分析。")

        def _cancel() -> None:
            self.chat.add_bubble("system", "已取消截图。")

        self.chat.add_bubble("system", "窗口将暂时隐藏，请拖拽选择区域（Esc 取消）…")
        run_region_snip(hide_widgets=hide, on_captured=_ok, on_cancelled=_cancel)

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择游戏截图",
            str(PROJECT_ROOT / "screenshots"),
            "Images (*.png *.jpg *.jpeg *.bmp)",
        )
        if not path:
            return
        p = Path(path)
        self.frame.set_image_path(p, caption=p.name)
        self._apply_view(self.engine.on_frame(p))

    def _toast_window(self) -> None:
        host = self._host_panel
        if host is None or getattr(host, "_facade", None) is None:
            QMessageBox.information(
                self,
                "用当前窗口",
                "请从主程序打开「脚本生成」，并先在「开始」页启动/绑定账号。",
            )
            return
        acc = host._selected_account()
        if not acc or not acc.get("name"):
            QMessageBox.information(
                self,
                "用当前窗口",
                "请先在窗口顶部选择已启动的账号，再点「用当前窗口」。",
            )
            return
        name = str(acc.get("name") or "")
        ctrl = host._facade.controller
        has_win = name in getattr(ctrl, "_window_instances", {})
        has_browser = name in getattr(ctrl, "_browsers", {})
        if has_win and not has_browser:
            acc["_target"] = "window"
        elif has_browser and not has_win:
            acc["_target"] = "browser"
        elif has_win and acc.get("_target") not in ("browser", "window"):
            acc["_target"] = "window"
        if not has_win and not has_browser:
            self.chat.add_bubble(
                "system",
                f"账号「{name}」尚未绑定窗口/浏览器，请先在「开始」页绑定后再抓。",
            )
            return
        host._collab_capture_pending = True
        host._collab_capture_account = name
        self.chat.add_bubble("system", f"正在抓取账号「{name}」的客户区…")
        try:
            ok = ctrl.capture_screenshot(acc)
            if ok is False:
                host._collab_capture_pending = False
                host._collab_capture_account = ""
                self.chat.add_bubble(
                    "system",
                    "抓取未开始：找不到对应截图实例（请确认顶栏账号是「· 窗口」且已绑定）。",
                )
                return

            def _timeout():
                if not getattr(host, "_collab_capture_pending", False):
                    return
                if getattr(host, "_collab_capture_account", "") != name:
                    return
                host._collab_capture_pending = False
                host._collab_capture_account = ""
                self.chat.add_bubble(
                    "system",
                    "抓取超时。可改用右侧「选截图」，或输入旁「截图」框选。",
                )

            QTimer.singleShot(10000, _timeout)
        except Exception as e:
            host._collab_capture_pending = False
            host._collab_capture_account = ""
            self.chat.add_bubble("system", f"抓取失败：{e}")

    def _on_bound_window_frame(self, path: Path, account_name: str) -> None:
        self.frame.set_image_path(path, caption=f"窗口 · {account_name}")
        if self.engine.session.skill_id:
            self._apply_view(self.engine.on_frame(path))
        else:
            self.engine.session.frame_path = path
            self.chat.add_bubble(
                "system",
                f"已抓取「{account_name}」客户区：{path.name}。直接说你想做什么即可。",
            )
            self.chat.set_chips(["去经典生成"])

    def _goto_trial_tab(self) -> None:
        host = self._host_panel
        if host is not None and hasattr(host, "_tabs"):
            for i in range(host._tabs.count()):
                if "试运行" in host._tabs.tabText(i):
                    host._tabs.setCurrentIndex(i)
                    break

    def _push_code_to_host(
        self,
        code: str,
        *,
        intro: str | None = None,
        script_name: str | None = None,
    ) -> None:
        host = self._host_panel
        if host is None:
            return
        try:
            art = self.engine.session.artifacts if isinstance(
                self.engine.session.artifacts, dict
            ) else {}
            ban = str(art.get("ban_script_snippet") or "").strip()
            if ban and "_COLLAB_BAN_ZONES" not in (code or ""):
                code = ban + "\n\n" + (code or "")
            host._generated_code = code
            note_origin = getattr(host, "_note_script_origin", None)
            if callable(note_origin):
                note_origin(code, collab=True)
            else:
                host._script_origin = "collab"
            if hasattr(host, "_live_code") and host._live_code is not None:
                host._live_code.setPlainText(code)
                if hasattr(host, "_live_code_hint"):
                    notes = (
                        art.get("script_normalize_notes")
                        if isinstance(art.get("script_normalize_notes"), list)
                        else []
                    )
                    tail = (
                        " · 已自动" + "、".join(str(n) for n in notes[:3])
                        if notes
                        else ""
                    )
                    host._live_code_hint.setText(
                        f"协作 · {script_name or 'generated'}{tail}"
                    )
            if script_name and hasattr(host, "_script_name"):
                host._script_name.setText(script_name)
            if intro and hasattr(host, "_explanation"):
                try:
                    host._explanation.setPlainText(intro)
                except Exception:
                    pass
            from gui.widgets.script_gen_panel.constants import _COLLAB_DIR

            name = script_name or "collab_script"
            if name.lower().endswith(".py"):
                name = name[:-3]
            out = _draft_target_path(_COLLAB_DIR, name, code)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(code, encoding="utf-8")
            write_trial = getattr(host, "_write_trial_file", None)
            trial_path = write_trial() if callable(write_trial) else None
            msg = (
                f"已写入协作草稿：\n· {out}"
                "\n（草稿目录；确定要用时把它复制到 scripts/<游戏>/ 即可）"
            )
            if trial_path:
                msg += f"\n· 试运行：{trial_path}"
            self.chat.add_bubble("system", msg)
            if hasattr(host, "_update_trial_availability"):
                try:
                    host._update_trial_availability()
                except Exception:
                    pass
        except Exception as e:
            self.chat.add_bubble("system", f"写入生成页失败：{e}")


class CollabMixin:
    """ScriptGenerator 混入：协作页。"""

    def _build_page_collab(self) -> QWidget:
        self._collab = CollabWorkbench()
        self._collab.bind_host(self)
        return self._collab

    def _collab_llm_params(self) -> dict | None:
        """从 API 配置页收集协作 LLM 参数；缺 Key/模型则返回 None。"""
        provider = ""
        model = ""
        api_key = ""
        endpoint = None
        max_tokens = 2048
        try:
            provider = (self._current_provider() or "").strip()
            model = (self._model.currentText() or "").strip()
            api_key = (self._api_key.text() or "").strip()
            ep = (self._endpoint.text() or "").strip()
            endpoint = ep or None
            max_tokens = int(self._max_tokens.value())
            if max_tokens <= 0:
                max_tokens = 2048
            else:
                max_tokens = min(max_tokens, 4096)
        except Exception as e:
            print(f"[Collab] _collab_llm_params 读取失败: {e}")
            return None
        if not provider or provider in ("（未选择）", "未选择"):
            return None
        if not model or not api_key:
            return None
        params = {
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "api_endpoint": endpoint,
            "max_tokens": max_tokens,
        }
        # 辅助识图：主模型看不了图时协作回合会自动调用
        try:
            v_provider = (self._current_vision_provider() or "").strip()
            v_model = (self._vision_model.currentText() or "").strip()
            v_key = (self._vision_api_key.text() or "").strip()
            v_ep = (self._vision_endpoint.text() or "").strip() or None
            if v_provider and v_provider not in ("（未选择）", "未选择") and v_model and v_key:
                params["vision_assist"] = {
                    "provider": v_provider,
                    "model": v_model,
                    "api_key": v_key,
                    "api_endpoint": v_ep,
                }
        except Exception as e:
            print(f"[Collab] 读取辅助识图配置失败: {e}")
        self._collab_llm_params_cache = dict(params)
        return params
