from __future__ import annotations

import asyncio
import re
from pathlib import Path

from PySide6.QtGui import (
    QFont, QPixmap, QKeySequence, QShortcut, QTextCursor, QTextDocument, QDesktopServices,
)
from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QCheckBox, QTextEdit,
    QLabel, QFileDialog, QSpinBox, QTabWidget, QSplitter,
    QMessageBox, QGroupBox, QProgressBar, QStyle, QFrame,
    QApplication, QDialog, QScrollArea, QDialogButtonBox, QProgressDialog,
    QInputDialog, QListWidget, QListWidgetItem, QSizePolicy,
)

from gui.widgets.GenTrajectory import GenTrajectory
from gui.widgets.TrialHud import TrialHud
from core.path import IMG_PATH, SCRIPTS_PATH

from gui.widgets.script_gen_panel.constants import _DEFAULT_PROFILE  # noqa: F401

class _BounceDots(QWidget):
    """忙碌时三个小圆点交错上下跳动（波浪相位）。"""

    # 高度档：0 底 → 1 中 → 2 顶 → 1 中；各点相位错开
    _WAVE = (0, 1, 2, 1)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("AgentBounceDots")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self._dots: list[QLabel] = []
        for _ in range(3):
            d = QLabel("●")
            d.setObjectName("AgentBounceDot")
            d.setFixedSize(9, 16)
            d.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            lay.addWidget(d)
            self._dots.append(d)
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(110)
        self._timer.timeout.connect(self._tick)
        self.setFixedWidth(34)
        self.setFixedHeight(18)
        self._apply_idle()

    def start(self):
        if not self._timer.isActive():
            self._timer.start()
        self._tick()

    def stop(self):
        self._timer.stop()
        self._apply_idle()

    def _apply_idle(self):
        for d in self._dots:
            d.setStyleSheet(
                "color:#9a958c;padding-top:5px;padding-bottom:0px;background:transparent;"
            )

    def _style_for_level(self, level: int) -> str:
        # level 越高越靠上
        top = max(0, 6 - level * 3)
        bottom = 6 - top
        if level >= 2:
            color = "#3b6fd8"
        elif level == 1:
            color = "#5b8def"
        else:
            color = "#93b4f0"
        return (
            f"color:{color};padding-top:{top}px;padding-bottom:{bottom}px;"
            "background:transparent;"
        )

    def _tick(self):
        n = len(self._WAVE)
        self._phase = (self._phase + 1) % n
        for i, d in enumerate(self._dots):
            # 交错：第 i 个点相位落后 i 步，形成波浪
            level = self._WAVE[(self._phase - i) % n]
            d.setStyleSheet(self._style_for_level(level))

class _NoWheelComboBox(QComboBox):
    """滚轮不改变选项，避免滚动页面时误改下拉框。"""

    def wheelEvent(self, event):
        event.ignore()

class _NoWheelSpinBox(QSpinBox):
    """滚轮不改变数值，避免滚动页面时误改。"""

    def wheelEvent(self, event):
        event.ignore()

class FullCodeDialog(QDialog):
    """完整代码查看器：默认锁定只读，可解锁编辑；支持 Ctrl+F。"""

    def __init__(self, parent=None, *, code: str = "", title: str = "完整脚本代码"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(780, 560)
        self._original = code
        self.applied_code: str | None = None  # 关闭时若应用修改则非 None
        self._force_close = False
        lay = QVBoxLayout(self)

        head = QHBoxLayout()
        self._lock_hint = QLabel("已锁定（只读）")
        self._lock_hint.setObjectName("MutedLabel")
        head.addWidget(self._lock_hint)
        head.addStretch()
        self._lock_btn = QPushButton("解锁编辑")
        self._lock_btn.setCheckable(True)
        self._lock_btn.setToolTip(
            "解锁后可直接改代码；Ctrl+Z 撤销 / Ctrl+Y 重做；关闭时若有改动会询问是否写回。"
        )
        self._lock_btn.toggled.connect(self._on_lock_toggled)
        head.addWidget(self._lock_btn)
        lay.addLayout(head)

        self._find_bar = QWidget()
        find_lay = QHBoxLayout(self._find_bar)
        find_lay.setContentsMargins(0, 0, 0, 4)
        find_lay.addWidget(QLabel("查找:"))
        self._find_edit = QLineEdit()
        self._find_edit.setPlaceholderText("输入关键词…")
        self._find_edit.returnPressed.connect(self._find_next)
        self._find_edit.textChanged.connect(self._on_find_text_changed)
        find_lay.addWidget(self._find_edit, 1)
        self._case_cb = QCheckBox("区分大小写")
        find_lay.addWidget(self._case_cb)
        prev_btn = QPushButton("上一个")
        prev_btn.clicked.connect(self._find_prev)
        find_lay.addWidget(prev_btn)
        next_btn = QPushButton("下一个")
        next_btn.clicked.connect(self._find_next)
        find_lay.addWidget(next_btn)
        self._find_status = QLabel("")
        self._find_status.setObjectName("MutedLabel")
        self._find_status.setMinimumWidth(72)
        find_lay.addWidget(self._find_status)
        close_find = QPushButton("×")
        close_find.setFixedWidth(28)
        close_find.setToolTip("关闭查找栏 (Esc)")
        close_find.clicked.connect(self._hide_find_bar)
        find_lay.addWidget(close_find)
        self._find_bar.setVisible(False)
        lay.addWidget(self._find_bar)

        self._editor = QTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setUndoRedoEnabled(True)
        self._editor.setFont(QFont("Consolas", 10))
        self._editor.setPlainText(code)
        # 初始载入不占撤销步，解锁后的编辑可用 Ctrl+Z 回滚
        self._editor.document().clearUndoRedoStacks()
        self._editor.textChanged.connect(self._on_editor_changed)
        lay.addWidget(self._editor, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._apply_btn = buttons.addButton(
            "应用修改", QDialogButtonBox.ButtonRole.ActionRole
        )
        self._apply_btn.setEnabled(False)
        self._apply_btn.setToolTip("把当前编辑写回生成结果（不关闭窗口）")
        self._apply_btn.clicked.connect(self._apply_keep_open)
        copy_btn = buttons.addButton("复制全部", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self._editor.toPlainText())
        )
        buttons.rejected.connect(self._on_close_clicked)
        buttons.accepted.connect(self._on_close_clicked)
        lay.addWidget(buttons)

        _shortcut_ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for seq, fn in [
            (QKeySequence.StandardKey.Find, self._show_find_bar),
            (QKeySequence("Ctrl+F"), self._show_find_bar),
            (QKeySequence(Qt.Key.Key_F3), self._find_next),
            (QKeySequence("Shift+F3"), self._find_prev),
            (QKeySequence(Qt.Key.Key_Escape), self._on_escape),
            (QKeySequence.StandardKey.Undo, self._undo_edit),
            (QKeySequence("Ctrl+Z"), self._undo_edit),
            (QKeySequence.StandardKey.Redo, self._redo_edit),
            (QKeySequence("Ctrl+Y"), self._redo_edit),
            (QKeySequence("Ctrl+Shift+Z"), self._redo_edit),
        ]:
            sc = QShortcut(seq, self)
            sc.setContext(_shortcut_ctx)
            sc.activated.connect(fn)
        self._editor.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self._editor and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_F and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._show_find_bar()
                return True
            if event.key() == Qt.Key.Key_Escape and self._find_bar.isVisible():
                self._hide_find_bar()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._show_find_bar()
            event.accept()
            return
        super().keyPressEvent(event)

    def _undo_edit(self):
        if self._editor.isReadOnly():
            return
        self._editor.undo()

    def _redo_edit(self):
        if self._editor.isReadOnly():
            return
        self._editor.redo()

    def is_dirty(self) -> bool:
        return self._editor.toPlainText() != self._original

    def _on_lock_toggled(self, unlocked: bool):
        self._editor.setReadOnly(not unlocked)
        if unlocked:
            self._lock_btn.setText("锁定")
            self._lock_hint.setText("已解锁 · 可编辑")
            self._lock_hint.setStyleSheet("color:#1565c0;")
            self._editor.setStyleSheet(
                "QTextEdit { border: 1px solid #1565c0; border-radius: 2px; }"
            )
            self._editor.setFocus()
        else:
            self._lock_btn.setText("解锁编辑")
            dirty = self.is_dirty()
            self._lock_hint.setText("已锁定（只读）" + (" · 有未应用修改" if dirty else ""))
            self._lock_hint.setStyleSheet("color:#b8891a;" if dirty else "")
            self._editor.setStyleSheet("")
        self._refresh_apply_btn()

    def _on_editor_changed(self):
        self._refresh_apply_btn()
        if self._lock_btn.isChecked():
            self._lock_hint.setText(
                "已解锁 · 可编辑" + (" · 已改动" if self.is_dirty() else "")
            )

    def _refresh_apply_btn(self):
        self._apply_btn.setEnabled(self.is_dirty())

    def _apply_keep_open(self):
        text = self._editor.toPlainText()
        self._original = text
        self.applied_code = text
        self._apply_btn.setEnabled(False)
        if self._lock_btn.isChecked():
            self._lock_hint.setText("已解锁 · 可编辑 · 已应用")
        else:
            self._lock_hint.setText("已锁定（只读）· 已应用")
            self._lock_hint.setStyleSheet("color:#2e7d32;")
        # 通知父级立刻写回
        parent = self.parent()
        if parent is not None and hasattr(parent, "_apply_edited_code"):
            try:
                parent._apply_edited_code(text)
            except Exception as e:
                print(f"[ScriptGenerator] 应用编辑代码失败: {e}")

    def _confirm_discard_or_apply(self) -> bool:
        """关闭前：True=继续关闭，False=取消关闭。"""
        if not self.is_dirty():
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("代码已修改")
        box.setText("完整代码已修改，是否写回当前生成结果？")
        apply_btn = box.addButton("写回并关闭", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("丢弃修改", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is apply_btn:
            text = self._editor.toPlainText()
            self.applied_code = text
            self._original = text
            parent = self.parent()
            if parent is not None and hasattr(parent, "_apply_edited_code"):
                try:
                    parent._apply_edited_code(text)
                except Exception as e:
                    print(f"[ScriptGenerator] 应用编辑代码失败: {e}")
            return True
        if clicked is discard_btn:
            return True
        return False

    def _on_close_clicked(self):
        if self._confirm_discard_or_apply():
            self._force_close = True
            self.reject()

    def closeEvent(self, event):
        if self._force_close or not self.is_dirty():
            event.accept()
            return
        if self._confirm_discard_or_apply():
            self._force_close = True
            event.accept()
        else:
            event.ignore()

    def _show_find_bar(self):
        self._find_bar.setVisible(True)
        self._find_edit.setFocus()
        if self._find_edit.text():
            self._find_edit.selectAll()

    def _hide_find_bar(self):
        self._find_bar.setVisible(False)
        self._find_status.setText("")
        self._editor.setFocus()

    def _on_escape(self):
        if self._find_bar.isVisible():
            self._hide_find_bar()
        else:
            self._on_close_clicked()

    def _find_flags(self, *, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self._case_cb.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _on_find_text_changed(self, _text: str = ""):
        self._find_status.setText("")
        if self._find_edit.text():
            cur = self._editor.textCursor()
            cur.setPosition(max(0, cur.selectionStart()))
            self._editor.setTextCursor(cur)
            self._find_next()

    def _find_next(self):
        self._do_find(backward=False)

    def _find_prev(self):
        self._do_find(backward=True)

    def _do_find(self, *, backward: bool):
        needle = self._find_edit.text()
        if not needle:
            self._find_status.setText("")
            return
        flags = self._find_flags(backward=backward)
        found = self._editor.find(needle, flags)
        if not found:
            cursor = self._editor.textCursor()
            if backward:
                cursor.movePosition(QTextCursor.MoveOperation.End)
            else:
                cursor.movePosition(QTextCursor.MoveOperation.Start)
            self._editor.setTextCursor(cursor)
            found = self._editor.find(needle, flags)
        if found:
            self._find_status.setText("已定位")
            self._find_status.setStyleSheet("color:#2e7d32;")
        else:
            self._find_status.setText("未找到")
            self._find_status.setStyleSheet("color:#c62828;")

class _PreviewCell(QGroupBox):
    """带边框的单元格"""

    def __init__(self, path: Path):
        super().__init__()
        self.setStyleSheet(CELL_STYLE)
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(2, 2, 2, 2)

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            vbox.addWidget(QLabel(f"[无法加载] {path.name}"))
            return

        w, h = pixmap.width(), pixmap.height()
        if w > MAX_W or h > MAX_H:
            scaled = pixmap.scaled(MAX_W, MAX_H,
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        else:
            scaled = pixmap

        label = QLabel()
        label.setPixmap(scaled)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vbox.addWidget(label)
        vbox.addWidget(QLabel(f"{path.name}  ({w}×{h})",
                              alignment=Qt.AlignmentFlag.AlignCenter))

class FeedbackWritebackDialog(QDialog):
    """修订成功后：勾选要写入脚本解释的约束条目。"""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择写入介绍的试运行约束")
        self.resize(620, 440)
        self._rows = []

        layout = QVBoxLayout(self)
        hint = QLabel(
            "勾选要写进「脚本解释」的新增约束："
            "约束默认勾选，一次性故障默认不选。"
            "口语反馈会整理成规范条目（有 API Key 时会调用模型）。可改字后再确认。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(4, 4, 4, 4)
        kind_label = {"constraint": "约束", "oneoff": "一次性", "duplicate": "已有"}
        for item in items:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            cb = QCheckBox()
            cb.setChecked(item.kind == "constraint")
            if item.kind == "duplicate":
                cb.setChecked(False)
                cb.setEnabled(False)
            tag = QLabel(kind_label.get(item.kind, item.kind))
            tag.setFixedWidth(48)
            edit = QLineEdit(item.rewritten)
            h.addWidget(cb)
            h.addWidget(tag)
            h.addWidget(edit, 1)
            form.addWidget(row)
            if item.original and item.original.strip() != item.rewritten.strip():
                orig = QLabel(f"原文：{item.original}")
                orig.setWordWrap(True)
                orig.setStyleSheet("color: gray; padding-left: 28px;")
                form.addWidget(orig)
            self._rows.append((cb, edit))
        form.addStretch(1)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_bullets(self) -> list[str]:
        from datetime import date
        today = date.today().isoformat()
        out = []
        for cb, edit in self._rows:
            if not cb.isChecked():
                continue
            text = edit.text().strip()
            if not text:
                continue
            out.append(f"- {today}：{text}")
        return out

def _show_scroll_message(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    icon: QMessageBox.Icon = QMessageBox.Icon.Information,
) -> None:
    """长文本提示框（带滚动条），替代无法滚动的 QMessageBox。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(640, 480)
    dlg.setMinimumSize(480, 320)

    root = QVBoxLayout(dlg)
    body = QHBoxLayout()
    if icon != QMessageBox.Icon.NoIcon:
        sp_map = {
            QMessageBox.Icon.Warning: QStyle.StandardPixmap.SP_MessageBoxWarning,
            QMessageBox.Icon.Critical: QStyle.StandardPixmap.SP_MessageBoxCritical,
            QMessageBox.Icon.Question: QStyle.StandardPixmap.SP_MessageBoxQuestion,
        }
        sp = sp_map.get(icon, QStyle.StandardPixmap.SP_MessageBoxInformation)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(QApplication.style().standardIcon(sp).pixmap(32, 32))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.addWidget(icon_lbl)

    editor = QTextEdit()
    editor.setReadOnly(True)
    editor.setAcceptRichText(False)
    editor.setPlainText(text)
    editor.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
    body.addWidget(editor, 1)
    root.addLayout(body, 1)

    btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    btns.accepted.connect(dlg.accept)
    root.addWidget(btns)
    dlg.exec()

class ImagePreviewDialog(QDialog):
    """弹窗显示图片，表格排列，列数自适应窗口宽度"""

    def __init__(self, image_paths: list[Path], parent=None):
        super().__init__(parent)
        self._paths = image_paths
        self.setWindowTitle("图片预览")
        self.resize(900, 600)
        self.setMinimumSize(440, 300)

        self._layout = QVBoxLayout(self)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._content = QWidget()
        self._layout.addWidget(self._scroll)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.close)
        self._layout.addWidget(btn_box)

        self._rebuild()

    def _rebuild(self):
        self._scroll.takeWidget()
        self._content = QWidget()
        table = QGridLayout(self._content)
        table.setSpacing(8)

        avail = self.width() - 40
        cols = max(1, avail // (MAX_W + 30))
        for i, p in enumerate(self._paths):
            r, c = divmod(i, cols)
            table.addWidget(_PreviewCell(p), r, c)

        self._scroll.setWidget(self._content)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rebuild()
