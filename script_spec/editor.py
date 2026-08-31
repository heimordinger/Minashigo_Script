"""
脚本说明编辑器（script_spec 隔离包）
====================================
图角色 / 辅助步骤 / 任务 / 校验 → 导出脚本说明文本。
草稿保存在 script_spec/drafts/。主窗口右下角「脚本说明」与 python -m script_spec 共用本编辑器。
"""
from __future__ import annotations

import sys
from pathlib import Path
import json
from datetime import datetime

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import (
    Qt, Signal, QSize, QTimer, QPoint, QEvent, QRect,
)
from PySide6.QtGui import (
    QFont, QIcon, QPixmap, QTextCursor, QPalette, QColor, QTextCharFormat,
    QCursor, QSyntaxHighlighter, QPainter, QStandardItemModel, QStandardItem,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTextEdit, QPlainTextEdit,
    QPushButton, QFileDialog, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QSplitter, QMessageBox,
    QApplication, QListWidget, QListWidgetItem, QInputDialog, QFrame,
    QTabWidget, QMenu, QSizePolicy, QCompleter, QToolButton, QScrollArea,
)

from script_spec.model import (
    ScriptSpec, ImageEntry, HelperSpec, TaskSpec, IMAGE_EXTS,
    ROLE_ID, ROLE_BUTTON, ROLE_OTHER, ROLE_LABELS, LABEL_TO_ROLE,
    find_image_tokens, image_exists_in_dir, dir_image_map, refresh_dir_image_map,
    resolve_image_rel,
)
from core.path import IMG_PATH

_THUMB_SIZE = 40
_COMP_THUMB_SIZE = 32  # 步骤补全弹层缩略图
_HOVER_THUMB = 160
_ROLE_ORDER = (ROLE_ID, ROLE_BUTTON, ROLE_OTHER)
# 草稿落在本隔离目录下，不写进主工程 user_data
_DRAFT_PATH = Path(__file__).resolve().parent / "drafts" / "draft.json"
_DRAFT_AUTOSAVE_MS = 800
_PREVIEW_DEBOUNCE_MS = 220


def _ui():
    """跟主窗口纸感主题对齐的绘制色（QSS 管不到的 Painter / 弹出层）。"""
    dark = False
    try:
        from gui.styles.theme import current_theme_from_config
        dark = current_theme_from_config() == "dark"
    except Exception:
        dark = False
    if dark:
        return {
            "gutter_bg": "#1c1f24",
            "gutter_line": "#2c3138",
            "muted": "#8b929c",
            "danger": "#d16b6b",
            "surface": "#121418",
            "text": "#e8eaed",
            "hover": "#23272e",
            "sel_bg": "#1e2a3e",
            "sel_text": "#e8eaed",
            "accent": "#5b8def",
            "border": "#2c3138",
            "popup_bg": "#1c1f24",
        }
    return {
        "gutter_bg": "#ebe8e1",
        "gutter_line": "#ddd8ce",
        "muted": "#7a776f",
        "danger": "#c44b4b",
        "surface": "#ffffff",
        "text": "#2a2a28",
        "hover": "#f0eee8",
        "sel_bg": "#e8effc",
        "sel_text": "#1f1f1d",
        "accent": "#3b6fd8",
        "border": "#ddd8ce",
        "popup_bg": "#ffffff",
    }


def _is_filename_char(ch: str) -> bool:
    return ch.isalnum() or ch in "._-/" or ("\u4e00" <= ch <= "\u9fff")


def _is_ascii_filename_char(ch: str) -> bool:
    """补全替换向右扩展时只用 ASCII，避免吃掉后面的中文正文。"""
    return ch.isascii() and (ch.isalnum() or ch in "._-/")


def _is_helper_name_char(ch: str) -> bool:
    """@辅助名：中文/字母数字/下划线；不含 . 以免吃掉后面说明。"""
    return ch.isalnum() or ch in "_-" or ("\u4e00" <= ch <= "\u9fff")


class NoWheelComboBox(QComboBox):
    """禁止滚轮切换选项，避免滚表格时误改。"""

    def wheelEvent(self, event):
        event.ignore()


class _ScrollImageMenu(QFrame):
    """可滚动的图片选择弹层；子目录在右侧再开同款可滚动面板。"""

    picked = Signal(str)

    def __init__(
        self,
        paths: list[str] | None = None,
        icon_fn=None,
        parent=None,
        *,
        node: dict | None = None,
        root: '_ScrollImageMenu | None' = None,
        show_empty: bool = False,
    ):
        super().__init__(
            parent,
            # Tool 而非 Popup：避免右侧子层抢焦点时本层被系统自动关掉
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._icon_fn = icon_fn
        self._root = root or self
        self._child: _ScrollImageMenu | None = None
        self._folder_nodes: dict[QToolButton, dict] = {}
        self._app_filter_installed = False
        self._node_id = id(node) if node is not None else None
        c = _ui()
        self.setStyleSheet(
            f"QFrame {{ background:{c['popup_bg']}; border:1px solid {c['border']}; }}"
        )

        if paths is not None:
            node = self._path_tree(paths)
            show_empty = True

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        max_h = self._max_panel_height()
        scroll.setMaximumHeight(max_h)
        scroll.setMinimumWidth(240)
        root_lay.addWidget(scroll)

        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(1)

        node = node or {'_files': [], '_dirs': {}}
        if show_empty:
            v.addWidget(self._make_file_btn('（空）', '', icon=QIcon()))
            if (node.get('_files') or node.get('_dirs')):
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setStyleSheet(f"color:{c['border']};")
                v.addWidget(line)

        for name in sorted((node.get('_dirs') or {}).keys(), key=str.lower):
            v.addWidget(self._make_folder_btn(name, node['_dirs'][name]))
        for rel in sorted(node.get('_files') or [], key=str.lower):
            icon = icon_fn(rel) if icon_fn else QIcon()
            v.addWidget(self._make_file_btn(Path(rel).name, rel, icon=icon))
        v.addStretch(1)
        scroll.setWidget(inner)

        inner.adjustSize()
        w = max(240, inner.sizeHint().width() + 24)
        h = min(max_h, inner.sizeHint().height() + 12)
        self.resize(w, h)

    @staticmethod
    def _max_panel_height() -> int:
        screen = QApplication.primaryScreen()
        if screen is None:
            return 360
        return max(240, min(480, int(screen.availableGeometry().height() * 0.55)))

    @staticmethod
    def _path_tree(paths: list[str]) -> dict:
        root: dict = {'_files': [], '_dirs': {}}
        for rel in paths:
            parts = rel.split('/')
            node = root
            for part in parts[:-1]:
                node = node['_dirs'].setdefault(part, {'_files': [], '_dirs': {}})
            node['_files'].append(rel)
        return root

    def _item_style(self) -> str:
        c = _ui()
        return (
            'QToolButton {'
            f"  background:transparent; color:{c['muted']}; border:none;"
            f"  text-align:left; padding:6px 10px; min-height:28px;"
            '}'
            f"QToolButton:hover {{ background:{c['sel_bg']}; color:{c['sel_text']}; }}"
        )

    def _make_file_btn(self, label: str, rel: str, *, icon: QIcon) -> QToolButton:
        btn = QToolButton()
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        btn.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        btn.setIcon(icon)
        btn.setText(label)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setStyleSheet(self._item_style())
        btn.clicked.connect(lambda: self._emit_pick(rel))
        btn.installEventFilter(self)
        return btn

    def _make_folder_btn(self, name: str, node: dict) -> QToolButton:
        btn = QToolButton()
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        btn.setText(f'{name}/  ›')
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setStyleSheet(self._item_style())
        self._folder_nodes[btn] = node
        btn.installEventFilter(self)
        btn.clicked.connect(lambda checked=False, b=btn, n=node: self._open_side_panel(b, n))
        return btn

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.Enter and obj is not self:
            node = self._folder_nodes.get(obj)
            if node is not None:
                self._open_side_panel(obj, node)
            elif isinstance(obj, QToolButton) and self.isAncestorOf(obj):
                self._hide_side_panel()
        elif et == QEvent.Type.MouseButtonPress and self._root is self and self.isVisible():
            gp = event.globalPosition().toPoint()  # type: ignore[attr-defined]
            if not self._hit_popup_tree(gp):
                self.close()
                return False
        return super().eventFilter(obj, event)

    def _hit_popup_tree(self, gp: QPoint) -> bool:
        panel: _ScrollImageMenu | None = self._root
        while panel is not None:
            try:
                if panel.isVisible() and panel.frameGeometry().contains(gp):
                    return True
                panel = panel._child
            except RuntimeError:
                break
        return False

    def _hide_side_panel(self):
        if self._child is not None:
            try:
                self._child._hide_side_panel()
                self._child.close()
            except RuntimeError:
                pass
            self._child = None

    def _open_side_panel(self, btn: QToolButton, node: dict):
        if (
            self._child is not None
            and getattr(self._child, '_node_id', None) is id(node)
            and self._child.isVisible()
        ):
            return
        self._hide_side_panel()
        child = _ScrollImageMenu(
            icon_fn=self._icon_fn,
            parent=self._root.parent(),
            node=node,
            root=self._root,
            show_empty=False,
        )
        child._node_id = id(node)
        child.picked.connect(self._root.picked.emit)
        self._child = child

        top_left = btn.mapToGlobal(QPoint(0, 0))
        x = self.mapToGlobal(QPoint(self.width() - 2, 0)).x()
        y = top_left.y()
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            if x + child.width() > geo.right():
                x = self.mapToGlobal(QPoint(0, 0)).x() - child.width() + 2
            if y + child.height() > geo.bottom():
                y = max(geo.top(), geo.bottom() - child.height())
            if y < geo.top():
                y = geo.top()
        child.move(QPoint(x, y))
        child.show()
        child.raise_()

    def _emit_pick(self, rel: str):
        self._root.picked.emit(rel or '')
        self._root.close()

    def popup_below(self, anchor: QWidget):
        pos = anchor.mapToGlobal(QPoint(0, anchor.height()))
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            if pos.y() + self.height() > geo.bottom():
                pos.setY(max(geo.top(), anchor.mapToGlobal(QPoint(0, 0)).y() - self.height()))
            if pos.x() + self.width() > geo.right():
                pos.setX(max(geo.left(), geo.right() - self.width()))
        self.move(pos)
        self.show()
        self.raise_()
        app = QApplication.instance()
        if app is not None and not self._app_filter_installed:
            app.installEventFilter(self)
            self._app_filter_installed = True

    def closeEvent(self, event):
        self._hide_side_panel()
        if self._root is self:
            app = QApplication.instance()
            if app is not None and self._app_filter_installed:
                app.removeEventFilter(self)
                self._app_filter_installed = False
        super().closeEvent(event)


class ImagePathPicker(QWidget):
    """图片路径选择：按钮弹出可滚动列表；子目录悬停旁开。"""

    currentTextChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._paths: list[str] = []
        self._icon_fn = None
        self._path = ""
        self._popup: _ScrollImageMenu | None = None

        self._btn = QToolButton(self)
        self._btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._btn.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        self._btn.setIconSize(QSize(_THUMB_SIZE, _THUMB_SIZE))
        self._btn.setMinimumHeight(_THUMB_SIZE + 8)
        self._btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._btn.clicked.connect(self._show_popup)
        self._apply_btn_style(selected=False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._btn)
        self.setMinimumHeight(_THUMB_SIZE + 8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._refresh_button()

    def currentText(self) -> str:
        return self._path

    def set_row_selected(self, selected: bool):
        self._apply_btn_style(selected)

    def _apply_btn_style(self, selected: bool):
        c = _ui()
        if selected:
            self._btn.setStyleSheet(
                "QToolButton {"
                f"  background:{c['sel_bg']}; color:{c['sel_text']};"
                f"  border:1px solid {c['accent']}; border-radius:4px; padding:4px 8px;"
                f"  text-align:left;"
                "}"
                "QToolButton::menu-indicator { width:12px; }"
            )
        else:
            self._btn.setStyleSheet(
                "QToolButton {"
                f"  background:{c['surface']}; color:{c['text']};"
                f"  border:1px solid {c['border']}; border-radius:4px; padding:4px 8px;"
                f"  text-align:left;"
                "}"
                f"QToolButton:hover {{ border-color:{c['accent']}; background:{c['hover']}; }}"
                "QToolButton::menu-indicator { width:12px; }"
            )

    def set_image_paths(self, paths: list[str], icon_fn, selected: str = ""):
        self._paths = list(paths)
        self._icon_fn = icon_fn
        self._path = (selected or "").replace("\\", "/")
        self._refresh_button()

    def _rebuild_menu(self):
        # 兼容主题刷新调用；弹层按需重建
        self._refresh_button()

    def _show_popup(self):
        if self._popup is not None:
            try:
                self._popup.close()
            except RuntimeError:
                pass
            self._popup = None
        pop = _ScrollImageMenu(self._paths, self._icon_fn, parent=self.window())
        pop.picked.connect(self._pick)
        self._popup = pop
        pop.popup_below(self._btn)

    def _refresh_button(self):
        if self._path:
            icon = self._icon_fn(self._path) if self._icon_fn else QIcon()
            self._btn.setIcon(icon)
            self._btn.setText(self._path)
        else:
            self._btn.setIcon(QIcon())
            self._btn.setText("选择图片…")

    def _pick(self, rel: str):
        rel = (rel or "").replace("\\", "/")
        if rel == self._path:
            return
        self._path = rel
        self._refresh_button()
        self.currentTextChanged.emit(self._path)


class ImageMentionHighlighter(QSyntaxHighlighter):
    """存在的图片名灰下划线；目录中不存在的红色报错样式。"""

    def __init__(self, document, get_source_dir):
        super().__init__(document)
        self._get_source_dir = get_source_dir  # callable() -> str
        self._ok_fmt = QTextCharFormat()
        self._ok_fmt.setFontUnderline(True)
        self._bad_fmt = QTextCharFormat()
        self._bad_fmt.setFontUnderline(True)
        self.apply_theme()

    def apply_theme(self):
        c = _ui()
        self._ok_fmt.setUnderlineColor(QColor(c["muted"]))
        self._bad_fmt.setUnderlineColor(QColor(c["danger"]))
        self._bad_fmt.setForeground(QColor(c["danger"]))
        self.rehighlight()

    def highlightBlock(self, text: str):
        if not text:
            return
        source_dir = self._get_source_dir() or ""
        known = dir_image_map(source_dir) if source_dir else {}
        for start, end, token in find_image_tokens(text, source_dir, known=known):
            ok = (not source_dir) or image_exists_in_dir(
                token, source_dir, known=known
            )
            self.setFormat(start, end - start, self._ok_fmt if ok else self._bad_fmt)

class _StepNumberArea(QWidget):
    def __init__(self, editor: "StepTextEdit"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.step_number_width(), 0)

    def paintEvent(self, event):
        self._editor.paint_step_numbers(event)


class StepTextEdit(QPlainTextEdit):
    """带步骤序号 + 图片文件名 / @辅助 补全的步骤编辑框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._narea = _StepNumberArea(self)
        self.blockCountChanged.connect(self._update_narea_width)
        self.updateRequest.connect(self._update_narea)
        self._update_narea_width(0)

        self._img_model = QStandardItemModel(self)
        self._helper_model = QStandardItemModel(self)
        self._img_lower: set[str] = set()
        self._helper_lower: set[str] = set()
        self._icon_fn = None
        # 当前补全弹层对应的替换区间 [start, end)
        self._replace_start = 0
        self._replace_end = 0
        self._suppress_comp = False
        self._completer = QCompleter(self)
        self._completer.setModel(self._img_model)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setMaxVisibleItems(10)
        self._completer.setCompletionRole(Qt.ItemDataRole.DisplayRole)
        self._completer.activated.connect(self._insert_completion)
        popup = self._completer.popup()
        popup.setIconSize(QSize(_COMP_THUMB_SIZE, _COMP_THUMB_SIZE))
        popup.setUniformItemSizes(True)
        self._style_completer_popup(popup)
        # 键抬起后再弹：避免与当前 key 事件/输入法抢焦点导致「只有删除才出现」
        self._comp_timer = QTimer(self)
        self._comp_timer.setSingleShot(True)
        self._comp_timer.setInterval(0)
        self._comp_timer.timeout.connect(self._on_comp_timer)
        self.textChanged.connect(self._schedule_completion)

    def apply_theme(self):
        popup = self._completer.popup()
        self._style_completer_popup(popup)
        self._narea.update()

    @staticmethod
    def _style_completer_popup(popup):
        c = _ui()
        row_h = _COMP_THUMB_SIZE + 10
        popup.setStyleSheet(
            "QListView {"
            f"  background:{c['popup_bg']}; color:{c['text']};"
            f"  border:1px solid {c['border']}; outline:none;"
            f"  selection-background-color:{c['sel_bg']}; selection-color:{c['sel_text']};"
            "}"
            f"QListView::item {{ padding:4px 10px 4px 6px; min-height:{row_h}px; }}"
            f"QListView::item:hover {{ background:{c['hover']}; }}"
        )
        popup.setIconSize(QSize(_COMP_THUMB_SIZE, _COMP_THUMB_SIZE))

    def set_image_names(self, names: list[str], icon_fn=None):
        ordered = sorted(names, key=str.lower)
        self._icon_fn = icon_fn
        self._img_model.clear()
        for name in ordered:
            item = QStandardItem(name)
            item.setEditable(False)
            if icon_fn is not None:
                icon = icon_fn(name)
                if icon is not None and not icon.isNull():
                    item.setIcon(icon)
            self._img_model.appendRow(item)
        self._img_lower = {n.lower() for n in ordered}

    def set_helper_names(self, names: list[str]):
        """任务步骤里输入 @ 时的辅助名补全列表。"""
        ordered = sorted(
            {n.strip() for n in names if n and str(n).strip()},
            key=str.lower,
        )
        self._helper_model.clear()
        for name in ordered:
            item = QStandardItem(f"@{name}")
            item.setEditable(False)
            self._helper_model.appendRow(item)
        self._helper_lower = {f"@{n}".lower() for n in ordered}

    def step_number_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits + 8

    def _update_narea_width(self, _=0):
        self.setViewportMargins(self.step_number_width(), 0, 0, 0)

    def _update_narea(self, rect: QRect, dy: int):
        if dy:
            self._narea.scroll(0, dy)
        else:
            self._narea.update(0, rect.y(), self._narea.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_narea_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._narea.setGeometry(
            QRect(cr.left(), cr.top(), self.step_number_width(), cr.height())
        )

    def paint_step_numbers(self, event):
        painter = QPainter(self._narea)
        c = _ui()
        painter.fillRect(event.rect(), QColor(c["gutter_bg"]))
        painter.setPen(QColor(c["gutter_line"]))
        painter.drawLine(
            self._narea.width() - 1,
            event.rect().top(),
            self._narea.width() - 1,
            event.rect().bottom(),
        )

        block = self.firstVisibleBlock()
        block_num = block.blockNumber()
        geo = self.blockBoundingGeometry(block).translated(self.contentOffset())
        top = round(geo.top())
        bottom = top + round(self.blockBoundingRect(block).height())
        font = self.font()
        painter.setFont(font)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor(c["muted"]))
                painter.drawText(
                    0,
                    top,
                    self._narea.width() - 8,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(block_num + 1),
                )
            block = block.next()
            block_num += 1
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
        painter.end()

    def _token_under_cursor(self) -> tuple[int, int, str]:
        """光标处文件名片段 (token_start, token_end, prefix_to_cursor)。

        向左可含中文文件名；向右只吃 ASCII 文件名字符，避免
        「ta_chuji.进入…」补全时把后面中文一并删掉。
        """
        tc = self.textCursor()
        text = self.toPlainText()
        pos = tc.position()
        start = pos
        while start > 0 and _is_filename_char(text[start - 1]):
            start -= 1
        end = pos
        while end < len(text) and _is_ascii_filename_char(text[end]):
            end += 1
        return start, end, text[start:pos]

    def _helper_ref_under_cursor(self) -> tuple[int, int, str] | None:
        """光标若在 @辅助 引用内，返回 (at_pos, token_end, @后前缀)。"""
        tc = self.textCursor()
        text = self.toPlainText()
        pos = tc.position()
        start = pos
        while start > 0 and _is_helper_name_char(text[start - 1]):
            start -= 1
        if start <= 0 or text[start - 1] != "@":
            return None
        at_pos = start - 1
        end = pos
        while end < len(text) and _is_helper_name_char(text[end]):
            end += 1
        return at_pos, end, text[start:pos]

    def _resolve_helper_completion(self, force: bool) -> tuple[int, int, str] | None:
        href = self._helper_ref_under_cursor()
        if href is None:
            return None
        if self._helper_model.rowCount() == 0:
            return None
        at_pos, token_end, after = href
        prefix = "@" + after
        if not force and prefix.lower() in self._helper_lower:
            return None
        self._completer.setModel(self._helper_model)
        self._completer.setCompletionPrefix(prefix)
        if self._completer.completionCount() > 0:
            return at_pos, token_end, prefix
        if force:
            self._completer.setCompletionPrefix("@")
            if self._completer.completionCount() > 0:
                return at_pos, token_end, "@"
        return None

    def _resolve_completion_range(self, force: bool) -> tuple[int, int, str] | None:
        """选出补全前缀：优先 @辅助，否则图片名最长可匹配后缀。

        例如「点击ho」→ 用「ho」匹配 home.png，替换区间只覆盖 ho，保留「点击」。
        """
        helper = self._resolve_helper_completion(force)
        if helper is not None:
            return helper
        # 光标在 @… 内但无可用辅助时，不要误弹图片补全
        if self._helper_ref_under_cursor() is not None:
            return None

        self._completer.setModel(self._img_model)
        token_start, token_end, full_prefix = self._token_under_cursor()
        if self._img_model.rowCount() == 0:
            return None
        if not force and not full_prefix:
            return None
        if not force and full_prefix.lower() in self._img_lower:
            return None

        # 从完整前缀起，逐次去掉左侧字符，直到有匹配（或 force 时空前缀列出全部）
        for i in range(len(full_prefix) + (1 if force else 0)):
            prefix = full_prefix[i:]
            start = token_start + i
            if not force and not prefix:
                continue
            self._completer.setCompletionPrefix(prefix)
            if self._completer.completionCount() > 0:
                return start, token_end, prefix
        return None

    def _insert_completion(self, completion):
        if hasattr(completion, "data"):
            text = str(completion.data(Qt.ItemDataRole.DisplayRole) or "")
        else:
            text = str(completion)
        if not text:
            return
        self._suppress_comp = True
        self._comp_timer.stop()
        try:
            tc = self.textCursor()
            start = self._replace_start
            end = max(self._replace_end, tc.position())
            tc.setPosition(start)
            tc.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            tc.insertText(text)
            self.setTextCursor(tc)
        finally:
            self._suppress_comp = False
        self._completer.popup().hide()

    def _schedule_completion(self):
        if self._suppress_comp or not self.hasFocus():
            return
        self._comp_timer.start()

    def _on_comp_timer(self):
        if self._suppress_comp or not self.hasFocus():
            return
        self._show_completion(force=False)

    def _place_completion_popup(self):
        """把补全框放到光标行下方（空间不够则上方），并留出间距避免挡住当前输入。"""
        popup = self._completer.popup()
        if not popup.isVisible():
            return
        gap = 10
        cr = self.cursorRect()
        # 略向右偏，少挡中文正文
        anchor = cr.bottomLeft() + QPoint(0, gap)
        above = cr.topLeft() - QPoint(0, gap)
        g_below = self.mapToGlobal(anchor)
        g_above = self.mapToGlobal(above)
        pw = popup.width()
        ph = popup.height()
        screen = QApplication.screenAt(g_below) or QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        x = min(max(g_below.x(), geo.left()), geo.right() - pw)
        if g_below.y() + ph <= geo.bottom():
            y = g_below.y()
        else:
            y = max(geo.top(), g_above.y() - ph)
        popup.move(x, y)

    def _show_completion(self, *, force: bool):
        resolved = self._resolve_completion_range(force)
        if not resolved:
            self._completer.popup().hide()
            return
        start, end, prefix = resolved
        self._replace_start = start
        self._replace_end = end
        self._completer.setCompletionPrefix(prefix)
        cr = self.cursorRect()
        popup = self._completer.popup()
        cr.setWidth(
            max(
                280,
                popup.sizeHintForColumn(0)
                + popup.verticalScrollBar().sizeHint().width()
                + _COMP_THUMB_SIZE
                + 28,
            )
        )
        # 先交给 QCompleter 算出尺寸并显示，再挪到行外留白处
        self._completer.complete(cr)
        self._place_completion_popup()

    def inputMethodEvent(self, event):
        super().inputMethodEvent(event)
        if event.commitString():
            self._schedule_completion()

    def keyPressEvent(self, event):
        popup = self._completer.popup()
        if popup.isVisible():
            if event.key() in (
                Qt.Key.Key_Enter,
                Qt.Key.Key_Return,
                Qt.Key.Key_Escape,
                Qt.Key.Key_Tab,
                Qt.Key.Key_Backtab,
            ):
                event.ignore()
                return
        # Alt+/ 强制补全（避开中文输入法占用的 Ctrl+Space）
        if (
            event.key() == Qt.Key.Key_Slash
            and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            self._show_completion(force=True)
            return
        super().keyPressEvent(event)
        # 实际弹出由 textChanged → 下一拍定时器触发（见 _schedule_completion）


class _ThumbHoverPopup(QFrame):
    """悬停图片名时显示的缩略图浮层。"""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setObjectName("ThumbHoverPopup")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self._img = QLabel()
        self._img.setObjectName("ThumbImage")
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setMinimumSize(_HOVER_THUMB, _HOVER_THUMB)
        self._name = QLabel()
        self._name.setObjectName("ThumbName")
        self._name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._img)
        lay.addWidget(self._name)
        self.apply_theme()
        self.hide()

    def apply_theme(self):
        try:
            from gui.styles.theme import current_theme_from_config, load_theme_qss
            self.setStyleSheet(load_theme_qss(current_theme_from_config()))
        except Exception:
            pass

    def show_image(self, path: Path, global_pos: QPoint):
        pix = QPixmap(str(path))
        if pix.isNull():
            self.hide()
            return
        scaled = pix.scaled(
            _HOVER_THUMB, _HOVER_THUMB,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._img.setPixmap(scaled)
        self._name.setText(path.name)
        self.adjustSize()
        # 右下方偏移，避免挡住光标
        x, y = global_pos.x() + 16, global_pos.y() + 20
        screen = QApplication.screenAt(global_pos)
        if screen:
            geo = screen.availableGeometry()
            if x + self.width() > geo.right():
                x = global_pos.x() - self.width() - 12
            if y + self.height() > geo.bottom():
                y = global_pos.y() - self.height() - 12
        self.move(x, y)
        self.show()



class SpecEditor(QWidget):
    """隔离的说明编辑器。"""

    spec_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SpecEditor")
        self._images: list[str] = []
        self._source_dir = ""
        self._thumb_cache: dict[str, QIcon] = {}
        self._helpers_data: list[HelperSpec] = []
        self._tasks_data: list[TaskSpec] = []
        self._updating = False
        self._draft_loading = False
        self._prev_helper_row = -1
        self._prev_task_row = -1
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.timeout.connect(self._autosave_draft)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._refresh_preview)
        self._thumb_popup = _ThumbHoverPopup(None)
        self._hover_edit: QPlainTextEdit | None = None
        self._span_cache_key: tuple[str, str] | None = None
        self._span_cache: list[tuple[int, int, str]] = []
        self._build_ui()
        self._install_image_hover(self._helper_steps)
        self._install_image_hover(self._task_steps)
        self.apply_theme()
        self._refresh_preview()
        self._refresh_draft_chip()
        QTimer.singleShot(0, self._restore_draft_on_start)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("SpecHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(12)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        t = QLabel("脚本IDE")
        t.setObjectName("TitleLabel")
        s = QLabel("图角色 · 辅助步骤 · 自动草稿 → 脚本介绍")
        s.setObjectName("MutedLabel")
        title_col.addWidget(t)
        title_col.addWidget(s)
        hl.addLayout(title_col, stretch=1)

        self._draft_chip = QLabel("无草稿")
        self._draft_chip.setObjectName("DraftChip")
        self._draft_chip.setProperty("hasDraft", "false")
        self._draft_chip.setToolTip(str(_DRAFT_PATH))
        hl.addWidget(self._draft_chip)

        self._folder_chip = QLabel("未选择图片目录")
        self._folder_chip.setObjectName("FolderChip")
        self._folder_chip.setMinimumWidth(180)
        hl.addWidget(self._folder_chip)

        btn_dir = QPushButton("选择目录")
        btn_dir.setObjectName("PrimaryButton")
        btn_dir.clicked.connect(self._pick_folder)
        hl.addWidget(btn_dir)
        root.addWidget(header)

        goal_row = QHBoxLayout()
        gl = QLabel("目标")
        gl.setObjectName("MutedLabel")
        gl.setFixedWidth(36)
        self._goal = QLineEdit()
        self._goal.setPlaceholderText("一句话描述脚本要完成什么…")
        self._goal.textChanged.connect(self._on_edited)
        goal_row.addWidget(gl)
        goal_row.addWidget(self._goal, stretch=1)
        root.addLayout(goal_row)

        self._validation_bar = QLabel("开始填写后自动校验")
        self._validation_bar.setObjectName("ValidationBar")
        self._validation_bar.setWordWrap(True)
        self._validation_bar.setProperty("level", "idle")
        root.addWidget(self._validation_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(8)

        left = QWidget()
        left.setMinimumWidth(280)
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_images_tab(), "① 图片")
        self._tabs.addTab(self._build_helpers_tab(), "② 辅助")
        self._tabs.addTab(self._build_tasks_tab(), "③ 任务")
        self._tabs.addTab(self._build_notes_tab(), "④ 规则")
        left_l.addWidget(self._tabs)
        splitter.addWidget(left)

        preview = QFrame()
        preview.setObjectName("PreviewPane")
        preview.setMinimumWidth(200)
        pl = QVBoxLayout(preview)
        pl.setContentsMargins(12, 12, 12, 12)
        pl.setSpacing(8)

        phead = QHBoxLayout()
        pt = QLabel("脚本说明预览")
        pt.setObjectName("PreviewTitle")
        phead.addWidget(pt)
        phead.addStretch()
        btn_copy = QPushButton("复制")
        btn_copy.setObjectName("GhostButton")
        btn_copy.clicked.connect(self._copy_preview)
        phead.addWidget(btn_copy)
        pl.addLayout(phead)

        self._preview = QTextEdit()
        self._preview.setObjectName("PreviewBody")
        self._preview.setReadOnly(True)
        mono = QFont("Consolas", 11)
        mono.setStyleHint(QFont.Monospace)
        self._preview.setFont(mono)
        pl.addWidget(self._preview, stretch=1)

        actions_top = QHBoxLayout()
        actions_top.setSpacing(8)
        for text, slot in (
            ("恢复草稿", self._load_draft_clicked),
            ("存草稿", self._save_draft_clicked),
            ("清除", self._clear_editor_clicked),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            actions_top.addWidget(b)
        actions_top.addStretch()
        pl.addLayout(actions_top)

        actions_bot = QHBoxLayout()
        actions_bot.setSpacing(8)
        for text, slot, primary in (
            ("加载介绍", self._import_script, False),
            ("保存 JSON", self._save_json, False),
            ("导出 txt", self._export_txt, True),
        ):
            b = QPushButton(text)
            if primary:
                b.setObjectName("PrimaryButton")
            b.clicked.connect(slot)
            actions_bot.addWidget(b)
        actions_bot.addStretch()
        pl.addLayout(actions_bot)

        splitter.addWidget(preview)
        # 左右分栏：拖中间滑块调整「编写区 / 预览区」宽度
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([640, 480])
        self._splitter = splitter
        root.addWidget(splitter, stretch=1)

        self._dir_edit = QLineEdit()
        self._dir_edit.hide()

    # ── Tabs ──

    def _build_images_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 14, 12, 12)
        lay.setSpacing(10)

        hint = QLabel(
            "标识图 → 状态名（unknown_state 路由）；按钮/其它写说明（偏移、阈值、y最大…）。"
            "点击「角色」表头可按角色筛选（类似 Excel）。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._role_filter_key = ""  # "" = 全部
        self._image_table = QTableWidget(0, 4)
        self._image_table.setHorizontalHeaderLabels(["图片", "角色 ▾", "状态/界面", "说明"])
        hdr = self._image_table.horizontalHeader()
        hdr.setMinimumSectionSize(64)
        hdr.setSectionsClickable(True)
        hdr.sectionClicked.connect(self._on_image_header_clicked)
        # 角色固定宽；图片/说明吃剩余；状态可拖
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        self._image_table.setColumnWidth(1, 88)
        hdr.setSectionResizeMode(2, QHeaderView.Interactive)
        self._image_table.setColumnWidth(2, 140)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self._image_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._image_table.setShowGrid(False)
        self._image_table.verticalHeader().setVisible(False)
        self._image_table.setAlternatingRowColors(True)
        # 图片/角色列是控件；禁止点空单元格时变成可编辑输入框
        self._image_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        # 避免单元格下拉按最长文件名撑死整表最小宽度，导致分栏几乎拖不动
        self._image_table.setMinimumWidth(0)
        self._image_table.horizontalHeader().setMinimumSectionSize(48)
        self._image_table.itemChanged.connect(self._on_edited)
        self._image_table.itemSelectionChanged.connect(self._sync_row_combo_selection)
        lay.addWidget(self._image_table, stretch=1)

        row = QHBoxLayout()
        add = QPushButton("+ 添加图片")
        add.setObjectName("GhostButton")
        add.clicked.connect(lambda: self._add_image_row())
        rem = QPushButton("删除所选")
        rem.clicked.connect(self._del_image_row)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        lay.addLayout(row)
        return w

    def _build_helpers_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 14, 12, 12)
        lay.setSpacing(12)

        side = QVBoxLayout()
        sl = QLabel("辅助步骤")
        sl.setObjectName("MutedLabel")
        side.addWidget(sl)
        self._helper_list = QListWidget()
        self._helper_list.setMinimumWidth(140)
        self._helper_list.setMaximumWidth(200)
        self._helper_list.currentRowChanged.connect(self._on_helper_selected)
        side.addWidget(self._helper_list, stretch=1)
        sbtns = QHBoxLayout()
        add_h = QPushButton("+")
        add_h.setObjectName("GhostButton")
        add_h.setFixedWidth(40)
        add_h.clicked.connect(self._add_helper)
        del_h = QPushButton("−")
        del_h.setFixedWidth(40)
        del_h.clicked.connect(self._del_helper)
        sbtns.addWidget(add_h)
        sbtns.addWidget(del_h)
        sbtns.addStretch()
        side.addLayout(sbtns)
        lay.addLayout(side)

        detail = QVBoxLayout()
        detail.addWidget(self._field_label("名称（任务里写 @名称 引用）"))
        self._helper_name = QLineEdit()
        self._helper_name.setPlaceholderText("例如：返回主界面")
        self._helper_name.textChanged.connect(self._on_helper_fields_edited)
        detail.addWidget(self._helper_name)
        detail.addWidget(self._field_label(
            "步骤（每行一步；输入图片名自动补全，Alt+/ 强制；悬停可预览）"
        ))
        self._helper_steps = StepTextEdit()
        self._helper_steps.setPlaceholderText(
            "若无 home.png 且已在主界面则结束\n"
            "点击 home.png\n"
            "等到 rank.png 出现"
        )
        self._helper_steps.textChanged.connect(self._on_helper_fields_edited)
        detail.addWidget(self._helper_steps, stretch=1)
        lay.addLayout(detail, stretch=1)
        return w

    def _build_tasks_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 14, 12, 12)
        lay.setSpacing(12)

        side = QVBoxLayout()
        side.addWidget(self._field_label("任务列表"))
        self._task_list = QListWidget()
        self._task_list.setMinimumWidth(140)
        self._task_list.setMaximumWidth(200)
        self._task_list.currentRowChanged.connect(self._on_task_selected)
        side.addWidget(self._task_list, stretch=1)
        sbtns = QHBoxLayout()
        add_t = QPushButton("+")
        add_t.setObjectName("GhostButton")
        add_t.setFixedWidth(40)
        add_t.clicked.connect(self._add_task)
        del_t = QPushButton("−")
        del_t.setFixedWidth(40)
        del_t.clicked.connect(self._del_task)
        sbtns.addWidget(add_t)
        sbtns.addWidget(del_t)
        sbtns.addStretch()
        side.addLayout(sbtns)
        lay.addLayout(side)

        detail = QVBoxLayout()
        detail.addWidget(self._field_label("任务名"))
        self._task_name = QLineEdit()
        self._task_name.setPlaceholderText("例如：房间领体力")
        self._task_name.textChanged.connect(self._on_task_fields_edited)
        detail.addWidget(self._task_name)

        step_head = QHBoxLayout()
        step_head.addWidget(self._field_label(
            "步骤（输入 @ 补全辅助；图片名补全 Alt+/；悬停可预览）"
        ))
        step_head.addStretch()
        self._btn_insert_helper = QPushButton("插入 @辅助")
        self._btn_insert_helper.setObjectName("GhostButton")
        self._btn_insert_helper.clicked.connect(self._insert_helper_ref)
        step_head.addWidget(self._btn_insert_helper)
        detail.addLayout(step_head)

        self._task_steps = StepTextEdit()
        self._task_steps.setPlaceholderText(
            "@返回主界面\n"
            "点击 room.png 进入房间\n"
            "若无 room_收取奖励.png → 任务结束\n"
            "点击收取 → 点 room_ok.png"
        )
        self._task_steps.textChanged.connect(self._on_task_fields_edited)
        detail.addWidget(self._task_steps, stretch=1)
        lay.addLayout(detail, stretch=1)
        return w

    def _build_notes_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 14, 12, 12)
        hint = QLabel(
            "全局特殊规则。图片表「说明」里的阈值/偏移/y最大也会自动抽进导出的特殊规则。"
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self._notes = QTextEdit()
        self._notes.setPlaceholderText("其它全局约定…")
        self._notes.textChanged.connect(self._on_edited)
        lay.addWidget(self._notes, stretch=1)
        return w

    @staticmethod
    def _field_label(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setObjectName("MutedLabel")
        return lb

    def apply_theme(self):
        """窗口套主题 QSS 之后，刷新 Painter / 弹出层颜色。"""
        if hasattr(self, "_thumb_popup") and self._thumb_popup is not None:
            self._thumb_popup.apply_theme()
        for edit in self.findChildren(StepTextEdit):
            edit.apply_theme()
            hl = getattr(edit, "_image_highlighter", None)
            if hl is not None:
                hl.apply_theme()
        if hasattr(self, "_image_table"):
            for row in range(self._image_table.rowCount()):
                for col in (0, 1):
                    w = self._image_table.cellWidget(row, col)
                    if isinstance(w, QComboBox):
                        self._style_combo_popup(w)
                    elif isinstance(w, ImagePathPicker):
                        w._rebuild_menu()
                        w._refresh_button()
            self._sync_row_combo_selection()

    # ── 公共 API ──

    def get_spec(self) -> ScriptSpec:
        images: list[ImageEntry] = []
        for row in range(self._image_table.rowCount()):
            img_w = self._image_table.cellWidget(row, 0)
            role_w = self._image_table.cellWidget(row, 1)
            state_item = self._image_table.item(row, 2)
            note_item = self._image_table.item(row, 3)
            image = ""
            if isinstance(img_w, (ImagePathPicker, QComboBox)):
                image = img_w.currentText().strip()
            role = ROLE_ID
            if isinstance(role_w, QComboBox):
                role = LABEL_TO_ROLE.get(role_w.currentText(), ROLE_OTHER)
            images.append(ImageEntry(
                image=image,
                role=role,
                state=(state_item.text().strip() if state_item else ""),
                note=(note_item.text().strip() if note_item else ""),
            ))
        self._flush_current_helper()
        self._flush_current_task()
        return ScriptSpec(
            goal=self._goal.text().strip(),
            source_dir=self._source_dir,
            images=images,
            helpers=list(self._helpers_data),
            tasks=list(self._tasks_data),
            notes=self._notes.toPlainText().strip(),
        )

    def set_spec(self, spec: ScriptSpec):
        self._updating = True
        try:
            self._goal.setText(spec.goal or "")
            self._notes.setPlainText(spec.notes or "")
            if spec.source_dir:
                self._apply_folder(Path(spec.source_dir), reload_images=True)
            self._image_table.setRowCount(0)
            entries = list(spec.images) if spec.images else [ImageEntry()]
            for e in entries:
                self._add_image_row(e)
            self._helpers_data = list(spec.helpers) if spec.helpers else []
            self._tasks_data = list(spec.tasks) if spec.tasks else []
            self._reload_helper_list()
            self._reload_task_list()
            self._prev_helper_row = -1
            self._prev_task_row = -1
            if self._helpers_data:
                self._helper_list.setCurrentRow(0)
            else:
                self._helper_name.clear()
                self._helper_steps.clear()
            if self._tasks_data:
                self._task_list.setCurrentRow(0)
            else:
                self._task_name.clear()
                self._task_steps.clear()
        finally:
            self._updating = False
        self._sync_helper_completers()
        self._rehighlight_step_edits()
        self._refresh_preview()

    def explanation_text(self) -> str:
        return self.get_spec().to_explanation_text()

    # ── 事件 / 目录 ──

    def _on_edited(self, *_):
        if self._updating or self._draft_loading:
            return
        self._span_cache_key = None
        self._preview_timer.start(_PREVIEW_DEBOUNCE_MS)
        self.spec_changed.emit()
        self._draft_timer.start(_DRAFT_AUTOSAVE_MS)

    # ── 草稿 ──

    def _read_draft_file(self) -> tuple[ScriptSpec | None, str]:
        """返回 (spec, saved_at)。无文件或空白则 (None, '')。"""
        if not _DRAFT_PATH.is_file():
            return None, ""
        try:
            raw = json.loads(_DRAFT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None, ""
        if not isinstance(raw, dict):
            return None, ""
        saved_at = str(raw.pop("_draft_saved_at", "") or "")
        spec = ScriptSpec.from_dict(raw)
        if spec.is_blank():
            return None, saved_at
        return spec, saved_at

    def _write_draft_file(self, spec: ScriptSpec) -> str:
        _DRAFT_PATH.parent.mkdir(parents=True, exist_ok=True)
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = spec.to_dict()
        data["_draft_saved_at"] = saved_at
        _DRAFT_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return saved_at

    def _refresh_draft_chip(self, saved_at: str | None = None):
        if saved_at is None:
            saved_at = ""
            if _DRAFT_PATH.is_file():
                try:
                    raw = json.loads(_DRAFT_PATH.read_text(encoding="utf-8"))
                    saved_at = str(raw.get("_draft_saved_at") or "")
                    body = {k: v for k, v in raw.items() if k != "_draft_saved_at"}
                    if ScriptSpec.from_dict(body).is_blank():
                        saved_at = ""
                except Exception:
                    saved_at = ""
        if saved_at:
            short = saved_at[11:16] if len(saved_at) >= 16 else saved_at
            self._draft_chip.setText(f"草稿 {short}")
            self._draft_chip.setProperty("hasDraft", "true")
            self._draft_chip.setToolTip(f"自动草稿\n保存于 {saved_at}\n{_DRAFT_PATH}")
        else:
            self._draft_chip.setText("无草稿")
            self._draft_chip.setProperty("hasDraft", "false")
            self._draft_chip.setToolTip(str(_DRAFT_PATH))
        self._draft_chip.style().unpolish(self._draft_chip)
        self._draft_chip.style().polish(self._draft_chip)

    def _autosave_draft(self):
        if self._updating or self._draft_loading:
            return
        spec = self.get_spec()
        if spec.is_blank():
            return
        try:
            saved_at = self._write_draft_file(spec)
            self._refresh_draft_chip(saved_at)
        except Exception:
            pass

    def _restore_draft_on_start(self):
        spec, saved_at = self._read_draft_file()
        if not spec:
            self._refresh_draft_chip("")
            return
        self._draft_loading = True
        try:
            self.set_spec(spec)
        finally:
            self._draft_loading = False
        self._refresh_draft_chip(saved_at)

    def _load_draft_clicked(self):
        spec, saved_at = self._read_draft_file()
        if not spec:
            QMessageBox.information(self, "草稿", "还没有可恢复的草稿")
            return
        if not self.get_spec().is_blank():
            ans = QMessageBox.question(
                self,
                "恢复草稿",
                f"用草稿覆盖当前内容？\n草稿时间：{saved_at or '未知'}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._draft_loading = True
        try:
            self.set_spec(spec)
        finally:
            self._draft_loading = False
        self._refresh_draft_chip(saved_at)
        self._refresh_preview()

    def _save_draft_clicked(self):
        spec = self.get_spec()
        if spec.is_blank():
            QMessageBox.information(self, "草稿", "当前内容为空，未写入草稿")
            return
        try:
            saved_at = self._write_draft_file(spec)
        except Exception as e:
            QMessageBox.warning(self, "存草稿失败", str(e))
            return
        self._refresh_draft_chip(saved_at)
        QMessageBox.information(self, "已存草稿", f"已保存到\n{_DRAFT_PATH}")

    def _clear_editor_clicked(self):
        if self.get_spec().is_blank():
            QMessageBox.information(self, "清除", "当前内容已空")
            return
        ans = QMessageBox.question(
            self,
            "清除",
            "清空当前编辑内容？\n（图片目录与草稿文件保留）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        folder = (self._source_dir or "").strip()
        self.set_spec(ScriptSpec(source_dir=folder))

    def _pick_folder(self):
        start = str(IMG_PATH) if Path(IMG_PATH).is_dir() else ""
        path = QFileDialog.getExistingDirectory(self, "选择图片文件夹", start)
        if path:
            self._apply_folder(Path(path), reload_images=True)

    def _apply_folder(self, root: Path, *, reload_images: bool):
        self._source_dir = str(root)
        self._dir_edit.setText(str(root))
        parts = root.parts
        chip = "/".join(parts[-2:]) if len(parts) >= 2 else root.name
        self._folder_chip.setText(chip)
        self._folder_chip.setToolTip(str(root))
        if reload_images:
            # 唯一扫盘点：恢复草稿 / 用户改路径（及 load JSON 同源）
            mapping = refresh_dir_image_map(root)
            self._span_cache_key = None
            self._thumb_cache.clear()
            self._images = sorted(mapping.values(), key=str.lower)
            self._sync_step_completers()
            for row in range(self._image_table.rowCount()):
                w = self._image_table.cellWidget(row, 0)
                if isinstance(w, ImagePathPicker):
                    self._fill_image_combo(w, w.currentText())
                elif isinstance(w, QComboBox):
                    self._fill_image_combo(w, w.currentText())
            if self._image_table.rowCount() == 0:
                self._add_image_row()
            # 图片目录变化后刷新步骤里的提及高亮
            self._rehighlight_step_edits()
        self._on_edited()

    def _sync_step_completers(self):
        names = list(self._images)
        for edit in (getattr(self, "_helper_steps", None), getattr(self, "_task_steps", None)):
            if isinstance(edit, StepTextEdit):
                edit.set_image_names(names, icon_fn=self._icon_for)
        self._sync_helper_completers()

    def _sync_helper_completers(self):
        helpers = [h.name.strip() for h in self._helpers_data if h.name.strip()]
        for edit in (getattr(self, "_helper_steps", None), getattr(self, "_task_steps", None)):
            if isinstance(edit, StepTextEdit):
                edit.set_helper_names(helpers)

    # ── 步骤文本：图片名悬停缩略图 ──

    def _install_image_hover(self, edit: QPlainTextEdit):
        edit.viewport().setMouseTracking(True)
        edit.setMouseTracking(True)
        edit.viewport().installEventFilter(self)
        edit._image_highlighter = ImageMentionHighlighter(
            edit.document(),
            lambda: self._source_dir,
        )

    def _rehighlight_step_edits(self):
        for edit in (getattr(self, "_helper_steps", None), getattr(self, "_task_steps", None)):
            if edit is None:
                continue
            hl = getattr(edit, "_image_highlighter", None)
            if hl is not None:
                hl.rehighlight()

    def eventFilter(self, obj, event):
        helper_vp = getattr(self, "_helper_steps", None)
        task_vp = getattr(self, "_task_steps", None)
        helper_vp = helper_vp.viewport() if helper_vp else None
        task_vp = task_vp.viewport() if task_vp else None
        if obj in (helper_vp, task_vp):
            edit = self._helper_steps if obj is helper_vp else self._task_steps
            et = event.type()
            if et == QEvent.Type.MouseMove:
                self._on_step_hover(edit, event.position().toPoint())
            elif et in (QEvent.Type.Leave, QEvent.Type.Hide):
                self._clear_thumb_hover()
        return super().eventFilter(obj, event)

    def _resolve_image_path(self, token: str) -> Path | None:
        if not self._source_dir or not token:
            return None
        known = dir_image_map(self._source_dir)
        rel = resolve_image_rel(token, self._source_dir, known=known)
        if not rel:
            return None
        return Path(self._source_dir) / rel

    def _iter_image_spans(self, text: str) -> list[tuple[int, int, str]]:
        text = text or ""
        key = (self._source_dir, text)
        if key != self._span_cache_key:
            known = dir_image_map(self._source_dir)
            self._span_cache = find_image_tokens(text, self._source_dir, known=known)
            self._span_cache_key = key
        return self._span_cache

    def _mention_at(self, edit: QPlainTextEdit, pos: QPoint) -> tuple[int, int, str] | None:
        cursor = edit.cursorForPosition(pos)
        abs_pos = cursor.position()
        text = edit.toPlainText()
        for start, end, name in self._iter_image_spans(text):
            if start <= abs_pos < end:
                return start, end, name
        return None

    def _on_step_hover(self, edit: QPlainTextEdit, pos: QPoint):
        hit = self._mention_at(edit, pos)
        if not hit:
            self._clear_thumb_hover()
            return
        _start, _end, name = hit
        path = self._resolve_image_path(name)
        if not path:
            self._clear_thumb_hover()
            return
        self._hover_edit = edit
        self._thumb_popup.show_image(path, QCursor.pos())

    def _clear_thumb_hover(self):
        self._hover_edit = None
        self._thumb_popup.hide()

    # ── 缩略图下拉 ──

    def _icon_for(self, name: str) -> QIcon:
        if not name or not self._source_dir:
            return QIcon()
        name = name.replace("\\", "/")
        cached = self._thumb_cache.get(name)
        if cached is not None:
            return cached
        path = Path(self._source_dir) / name
        pix = QPixmap(str(path))
        if pix.isNull():
            icon = QIcon()
        else:
            scaled = pix.scaled(
                _THUMB_SIZE, _THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon = QIcon(scaled)
        self._thumb_cache[name] = icon
        return icon

    def _fill_image_combo(self, cb, selected: str = ""):
        selected = (selected or "").replace("\\", "/")
        if isinstance(cb, ImagePathPicker):
            cb.set_image_paths(self._images, self._icon_for, selected)
            return
        if not isinstance(cb, QComboBox):
            return
        cb.blockSignals(True)
        try:
            cb.clear()
            cb.addItem("")
            for name in self._images:
                cb.addItem(self._icon_for(name), name)
            if selected:
                i = cb.findText(selected)
                if i >= 0:
                    cb.setCurrentIndex(i)
                else:
                    cb.addItem(self._icon_for(selected), selected)
                    cb.setCurrentIndex(cb.count() - 1)
            else:
                cb.setCurrentIndex(0)
        finally:
            cb.blockSignals(False)

    def _style_combo_popup(self, cb: QComboBox):
        """下拉层跟主题走，避免系统白底看不清。"""
        c = _ui()
        view = cb.view()
        view.setStyleSheet(
            "QAbstractItemView {"
            f"  background:{c['popup_bg']}; color:{c['muted']};"
            f"  border:1px solid {c['border']}; outline:0;"
            "}"
            "QAbstractItemView::item {"
            f"  min-height:44px; padding:4px 8px;"
            f"  color:{c['muted']}; background:{c['popup_bg']};"
            "}"
            "QAbstractItemView::item:hover {"
            f"  background:{c['hover']}; color:{c['text']};"
            "}"
            "QAbstractItemView::item:selected {"
            f"  background:{c['sel_bg']}; color:{c['sel_text']};"
            "}"
        )
        pal = view.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(c["popup_bg"]))
        pal.setColor(QPalette.ColorRole.Text, QColor(c["muted"]))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(c["sel_bg"]))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(c["sel_text"]))
        view.setPalette(pal)
        view.setAlternatingRowColors(False)

    def _set_combo_row_selected(self, cb, selected: bool):
        """单元格里的下拉框跟随整行选中高亮。"""
        if isinstance(cb, ImagePathPicker):
            cb.set_row_selected(selected)
            return
        if not isinstance(cb, QComboBox):
            return
        if selected:
            c = _ui()
            cb.setStyleSheet(
                "QComboBox {"
                f"  background:{c['sel_bg']}; color:{c['sel_text']};"
                f"  border:1px solid {c['accent']}; border-radius:4px; padding:4px;"
                "}"
                f"QComboBox:hover {{ border-color:{c['accent']}; }}"
                "QComboBox::drop-down { border:none; width:20px; }"
            )
        else:
            cb.setStyleSheet("")

    def _sync_row_combo_selection(self):
        if not hasattr(self, "_image_table"):
            return
        selected = {idx.row() for idx in self._image_table.selectedIndexes()}
        for row in range(self._image_table.rowCount()):
            on = row in selected
            for col in (0, 1):
                w = self._image_table.cellWidget(row, col)
                if isinstance(w, (ImagePathPicker, QComboBox)):
                    self._set_combo_row_selected(w, on)

    def _make_image_combo(self, selected: str = "") -> ImagePathPicker:
        cb = ImagePathPicker()
        self._fill_image_combo(cb, selected)
        cb.currentTextChanged.connect(self._on_edited)
        return cb

    def _make_role_combo(self, role: str = ROLE_ID) -> QComboBox:
        cb = NoWheelComboBox()
        for r in _ROLE_ORDER:
            cb.addItem(ROLE_LABELS[r])
        label = ROLE_LABELS.get(role, ROLE_LABELS[ROLE_ID])
        i = cb.findText(label)
        cb.setCurrentIndex(i if i >= 0 else 0)
        cb.setFixedWidth(72)
        cb.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._style_combo_popup(cb)
        cb.currentTextChanged.connect(self._on_edited)
        cb.currentTextChanged.connect(self._apply_role_filter)
        return cb

    def _role_header_text(self) -> str:
        if not self._role_filter_key:
            return "角色 ▾"
        return f"{ROLE_LABELS.get(self._role_filter_key, '')} ▾"

    def _on_image_header_clicked(self, section: int):
        # 角色列（第 2 列，index=1）表头：Excel 式筛选
        if section != 1:
            return
        menu = QMenu(self)
        c = _ui()
        menu.setStyleSheet(
            f"QMenu {{ background:{c['popup_bg']}; color:{c['text']}; border:1px solid {c['border']}; }}"
            "QMenu::item { padding:8px 28px 8px 16px; }"
            f"QMenu::item:selected {{ background:{c['sel_bg']}; color:{c['sel_text']}; }}"
            f"QMenu::indicator:checked {{ color:{c['accent']}; }}"
        )
        options = [("", "全部")] + [(r, ROLE_LABELS[r]) for r in _ROLE_ORDER]
        for key, label in options:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._role_filter_key == key)
            act.setData(key)
        # 弹出在该列表头下方
        hdr = self._image_table.horizontalHeader()
        x = hdr.sectionViewportPosition(1)
        pos = hdr.mapToGlobal(QPoint(x, hdr.height()))
        chosen = menu.exec(pos)
        if chosen is None:
            return
        self._role_filter_key = str(chosen.data() or "")
        self._image_table.setHorizontalHeaderItem(
            1, QTableWidgetItem(self._role_header_text())
        )
        self._apply_role_filter()

    def _apply_role_filter(self, *_):
        if not hasattr(self, "_image_table"):
            return
        key = getattr(self, "_role_filter_key", "") or ""
        for row in range(self._image_table.rowCount()):
            if not key:
                self._image_table.setRowHidden(row, False)
                continue
            role_w = self._image_table.cellWidget(row, 1)
            if isinstance(role_w, QComboBox):
                role = LABEL_TO_ROLE.get(role_w.currentText(), ROLE_OTHER)
            else:
                role = ROLE_OTHER
            self._image_table.setRowHidden(row, role != key)

    def _add_image_row(self, entry: ImageEntry | None = None):
        entry = entry or ImageEntry()
        # 筛选中新增时，默认角色跟筛选一致，避免行被立刻藏掉
        if not (entry.image or "").strip():
            filt = getattr(self, "_role_filter_key", "") or ""
            if filt:
                entry.role = filt
        self._updating = True
        try:
            row = self._image_table.rowCount()
            self._image_table.insertRow(row)
            self._image_table.setRowHeight(row, _THUMB_SIZE + 12)
            self._image_table.setCellWidget(row, 0, self._make_image_combo(entry.image))
            self._image_table.setCellWidget(row, 1, self._make_role_combo(entry.role or ROLE_ID))
            state_item = QTableWidgetItem(entry.state or "")
            note_item = QTableWidgetItem(entry.note or "")
            self._image_table.setItem(row, 2, state_item)
            self._image_table.setItem(row, 3, note_item)
        finally:
            self._updating = False
        self._apply_role_filter()
        self._sync_row_combo_selection()
        self._on_edited()

    def _del_image_row(self):
        rows = sorted({i.row() for i in self._image_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self._image_table.removeRow(r)
        self._on_edited()

    # ── 辅助步骤 ──

    def _reload_helper_list(self):
        self._helper_list.blockSignals(True)
        self._helper_list.clear()
        for i, h in enumerate(self._helpers_data, 1):
            item = QListWidgetItem(f"{i}.  {h.name or '未命名'}")
            item.setSizeHint(QSize(0, 36))
            self._helper_list.addItem(item)
        self._helper_list.blockSignals(False)

    def _flush_current_helper(self):
        row = self._helper_list.currentRow()
        if row < 0 or row >= len(self._helpers_data):
            return
        self._helpers_data[row] = HelperSpec(
            name=self._helper_name.text().strip(),
            steps=self._helper_steps.toPlainText(),
        )

    def _on_helper_selected(self, row: int):
        if self._updating:
            return
        if 0 <= self._prev_helper_row < len(self._helpers_data):
            self._helpers_data[self._prev_helper_row] = HelperSpec(
                name=self._helper_name.text().strip(),
                steps=self._helper_steps.toPlainText(),
            )
            self._reload_helper_labels()
        self._prev_helper_row = row
        if row < 0 or row >= len(self._helpers_data):
            self._helper_name.clear()
            self._helper_steps.clear()
            return
        self._updating = True
        try:
            h = self._helpers_data[row]
            self._helper_name.setText(h.name)
            self._helper_steps.setPlainText(h.steps)
        finally:
            self._updating = False
        self._refresh_preview()

    def _reload_helper_labels(self):
        for i, h in enumerate(self._helpers_data):
            item = self._helper_list.item(i)
            if item:
                item.setText(f"{i + 1}.  {h.name or '未命名'}")

    def _on_helper_fields_edited(self, *_):
        if self._updating:
            return
        row = self._helper_list.currentRow()
        if row < 0:
            return
        self._helpers_data[row] = HelperSpec(
            name=self._helper_name.text().strip(),
            steps=self._helper_steps.toPlainText(),
        )
        self._helper_list.blockSignals(True)
        item = self._helper_list.item(row)
        if item:
            item.setText(f"{row + 1}.  {self._helpers_data[row].name or '未命名'}")
        self._helper_list.blockSignals(False)
        self._sync_helper_completers()
        self._on_edited()

    def _add_helper(self):
        self._flush_current_helper()
        name, ok = QInputDialog.getText(
            self, "添加辅助步骤", "名称：", text="返回主界面"
        )
        if not ok:
            return
        self._helpers_data.append(HelperSpec(name=name.strip() or "辅助步骤", steps=""))
        self._reload_helper_list()
        self._helper_list.setCurrentRow(len(self._helpers_data) - 1)
        self._sync_helper_completers()
        self._on_edited()

    def _del_helper(self):
        row = self._helper_list.currentRow()
        if row < 0:
            return
        self._helpers_data.pop(row)
        self._prev_helper_row = -1
        self._reload_helper_list()
        if self._helpers_data:
            self._helper_list.setCurrentRow(min(row, len(self._helpers_data) - 1))
        else:
            self._helper_name.clear()
            self._helper_steps.clear()
        self._sync_helper_completers()
        self._on_edited()

    def _insert_helper_ref(self):
        names = [h.name.strip() for h in self._helpers_data if h.name.strip()]
        if not names:
            QMessageBox.information(self, "提示", "请先在「② 辅助」里添加辅助步骤")
            return
        menu = QMenu(self)
        for name in names:
            act = menu.addAction(f"@{name}")
            act.setData(name)
        chosen = menu.exec(self._btn_insert_helper.mapToGlobal(
            self._btn_insert_helper.rect().bottomLeft()
        ))
        if not chosen:
            return
        ref = f"@{chosen.data()}"
        cursor = self._task_steps.textCursor()
        if cursor.hasSelection():
            cursor.insertText(ref)
        else:
            # 插到当前行；若行非空则新起一行
            block = cursor.block()
            if block.text().strip():
                cursor.movePosition(QTextCursor.EndOfBlock)
                cursor.insertText("\n" + ref)
            else:
                cursor.insertText(ref)
        self._task_steps.setTextCursor(cursor)
        self._task_steps.setFocus()

    # ── 任务 ──

    def _reload_task_list(self):
        self._task_list.blockSignals(True)
        self._task_list.clear()
        for i, t in enumerate(self._tasks_data, 1):
            item = QListWidgetItem(f"{i}.  {t.name or '未命名'}")
            item.setSizeHint(QSize(0, 36))
            self._task_list.addItem(item)
        self._task_list.blockSignals(False)

    def _flush_current_task(self):
        row = self._task_list.currentRow()
        if row < 0 or row >= len(self._tasks_data):
            return
        self._tasks_data[row] = TaskSpec(
            name=self._task_name.text().strip(),
            steps=self._task_steps.toPlainText(),
        )

    def _on_task_selected(self, row: int):
        if self._updating:
            return
        if 0 <= self._prev_task_row < len(self._tasks_data):
            self._tasks_data[self._prev_task_row] = TaskSpec(
                name=self._task_name.text().strip(),
                steps=self._task_steps.toPlainText(),
            )
            self._reload_task_list_labels()
        self._prev_task_row = row
        if row < 0 or row >= len(self._tasks_data):
            self._task_name.clear()
            self._task_steps.clear()
            return
        self._updating = True
        try:
            t = self._tasks_data[row]
            self._task_name.setText(t.name)
            self._task_steps.setPlainText(t.steps)
        finally:
            self._updating = False
        self._refresh_preview()

    def _reload_task_list_labels(self):
        for i, t in enumerate(self._tasks_data):
            item = self._task_list.item(i)
            if item:
                item.setText(f"{i + 1}.  {t.name or '未命名'}")

    def _on_task_fields_edited(self, *_):
        if self._updating:
            return
        row = self._task_list.currentRow()
        if row < 0:
            return
        self._tasks_data[row] = TaskSpec(
            name=self._task_name.text().strip(),
            steps=self._task_steps.toPlainText(),
        )
        self._task_list.blockSignals(True)
        item = self._task_list.item(row)
        if item:
            item.setText(f"{row + 1}.  {self._tasks_data[row].name or '未命名'}")
        self._task_list.blockSignals(False)
        self._on_edited()

    def _add_task(self):
        self._flush_current_task()
        name, ok = QInputDialog.getText(self, "添加任务", "任务名称：", text="新任务")
        if not ok:
            return
        self._tasks_data.append(TaskSpec(name=name.strip() or "新任务", steps=""))
        self._reload_task_list()
        self._task_list.setCurrentRow(len(self._tasks_data) - 1)
        self._on_edited()

    def _del_task(self):
        row = self._task_list.currentRow()
        if row < 0:
            return
        self._tasks_data.pop(row)
        self._prev_task_row = -1
        self._reload_task_list()
        if self._tasks_data:
            self._task_list.setCurrentRow(min(row, len(self._tasks_data) - 1))
        else:
            self._task_name.clear()
            self._task_steps.clear()
        self._on_edited()

    # ── 预览 / IO ──

    def _update_validation_bar(self, spec: ScriptSpec):
        if spec.is_blank():
            level, text, tip = "idle", "开始填写后自动校验", ""
        else:
            issues = spec.validate()
            errors = [i for i in issues if i.level == "error"]
            warns = [i for i in issues if i.level == "warn"]
            if not issues:
                level, text, tip = "ok", "校验通过 · 可导出给生成器", "OK"
            elif errors:
                level = "error"
                text = f"[错误] {len(errors)} 个" + (
                    f" · {len(warns)} 警告" if warns else ""
                ) + " — " + errors[0].message
                tip = "\n".join(f"[{i.level}] {i.message}" for i in issues)
            else:
                level = "warn"
                text = f"[警告] {len(warns)} 个 — " + warns[0].message
                tip = "\n".join(f"[{i.level}] {i.message}" for i in issues)
        self._validation_bar.setText(text)
        self._validation_bar.setToolTip(tip)
        self._validation_bar.setProperty("level", level)
        self._validation_bar.style().unpolish(self._validation_bar)
        self._validation_bar.style().polish(self._validation_bar)

    def _refresh_preview(self):
        spec = self.get_spec()
        self._update_validation_bar(spec)
        self._preview.setPlainText(spec.to_explanation_text())

    def _copy_preview(self):
        QApplication.clipboard().setText(self.explanation_text())
        QMessageBox.information(self, "已复制", "脚本说明文本已复制到剪贴板")

    def _dialog_start_dir(self) -> str:
        """文件对话框初始目录：优先当前素材夹，否则 assets/images。"""
        folder = (self._source_dir or "").strip()
        if folder and Path(folder).is_dir():
            return folder
        if Path(IMG_PATH).is_dir():
            return str(IMG_PATH)
        return ""

    def _save_json(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存脚本说明 JSON", self._dialog_start_dir(), "JSON (*.json)"
        )
        if not path:
            return
        self.get_spec().save_json(Path(path))
        QMessageBox.information(self, "已保存", path)

    def _confirm_replace(self, action: str) -> bool:
        if self.get_spec().is_blank():
            return True
        ans = QMessageBox.question(
            self,
            action,
            "当前编辑器里已有内容，加载会覆盖。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ans == QMessageBox.StandardButton.Yes

    def _import_script(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载介绍",
            self._dialog_start_dir(),
            "介绍 / 脚本 (*.txt *.py *.json);;介绍 txt (*.txt);;Python (*.py);;JSON (*.json)",
        )
        if not path:
            return
        if not self._confirm_replace("加载介绍"):
            return
        from script_spec.import_script import import_from_path
        try:
            spec, info = import_from_path(Path(path))
        except Exception as e:
            QMessageBox.warning(self, "加载失败", str(e))
            return
        self.set_spec(spec)
        self.apply_theme()
        QMessageBox.information(self, "已加载", info)

    def _export_txt(self):
        start = self._dialog_start_dir()
        default = ""
        if start:
            default = str(Path(start) / "脚本介绍.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出脚本说明 txt", default or start, "Text (*.txt)"
        )
        if not path:
            return
        Path(path).write_text(self.explanation_text(), encoding="utf-8")
        QMessageBox.information(self, "已导出", path)


def main():
    import sys
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    from gui.styles.theme import current_theme_from_config, load_theme_qss
    app.setStyleSheet(load_theme_qss(current_theme_from_config()))
    w = SpecEditor()
    w.setWindowTitle("脚本IDE")
    w.resize(1120, 740)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
