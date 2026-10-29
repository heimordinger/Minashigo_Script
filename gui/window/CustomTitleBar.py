from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget


class CustomTitleBar(QFrame):
    """A compact title bar that delegates moving back to the window manager."""

    close_requested = Signal()

    def __init__(self, window: QWidget):
        super().__init__(window)
        self._window = window
        self.setObjectName("CustomTitleBar")
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(7)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("TitleBarIcon")
        self.icon_label.setFixedSize(16, 16)
        layout.addWidget(self.icon_label)

        self.title_label = QLabel(window.windowTitle())
        self.title_label.setObjectName("TitleBarTitle")
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        self.minimize_button = self._button("TitleBarMinimize", "—", "最小化")
        self.maximize_button = self._button("TitleBarMaximize", "□", "最大化")
        self.close_button = self._button("TitleBarClose", "X", "关闭")
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(self.close_requested)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)
        self.sync_from_window()

    def _button(self, name: str, text: str, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(name)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedSize(46, 32)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def sync_from_window(self) -> None:
        self.title_label.setText(self._window.windowTitle())
        icon = self._window.windowIcon()
        if icon.isNull():
            self.icon_label.clear()
        else:
            pixmap: QPixmap = icon.pixmap(16, 16)
            self.icon_label.setPixmap(pixmap)
        maximized = self._window.isMaximized()
        self.maximize_button.setText("❐" if maximized else "□")
        self.maximize_button.setToolTip("向下还原" if maximized else "最大化")

    def toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self.sync_from_window()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)


class WindowResizeHandle(QWidget):
    """Thin client-side handle that delegates resizing to the OS."""

    def __init__(self, window: QWidget, edges: Qt.Edge):
        super().__init__(window)
        self._window = window
        self._edges = edges
        self.setObjectName("WindowResizeHandle")
        self.setCursor(self._cursor_for_edges(edges))

    @staticmethod
    def _cursor_for_edges(edges: Qt.Edge) -> Qt.CursorShape:
        if edges in (
            Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
            Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
        ):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (
            Qt.Edge.TopEdge | Qt.Edge.RightEdge,
            Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
        ):
            return Qt.CursorShape.SizeBDiagCursor
        if edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemResize(self._edges):
                event.accept()
                return
        super().mousePressEvent(event)
