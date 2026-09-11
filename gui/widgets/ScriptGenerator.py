"""
ScriptGenerator 面板
====================
用户配置 API、上传脚本解释和图片、生成自动化脚本。
"""

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
from backend.script_generator.agent import generate_script, test_connection
from core.path import IMG_PATH, SCRIPTS_PATH

# 试运行写入 scripts/_trial/，由 TaskController 按 scripts._trial.* 加载
_TRIAL_REL = "_trial/_gen_trial.py"
_TRIAL_DIR = SCRIPTS_PATH / "_trial"
_OPTIMIZE_PAGE_SIZE = 20
_INTRO_FILENAMES = ("脚本介绍.txt", "脚本解释.txt")
_KEYRING_SERVICE = "Minashigo_ScriptGenerator"
_DEFAULT_PROFILE = "默认"


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


# Agent 阶段 → 进度条大致位置（仅提示用）
_STAGE_PROGRESS = {
    "plan": 12,
    "think": 18,
    "diagnose": 22,
    "generate": 28,
    "task": 42,
    "merge": 58,
    "validate": 72,
    "fix": 82,
    "revise": 55,
    "revise_gap": 78,
    "review": 88,
    "optimize": 35,
    "vision": 48,
    "stop_vision": 50,
    "vision_decide": 52,
}


# ═══════════════════════════════════════════════════════════════
# 代码查看 / 图片预览
# ═══════════════════════════════════════════════════════════════

CELL_STYLE = "QGroupBox{border:1px solid #bbb;border-radius:4px;padding:6px;margin:2px}"
MAX_W, MAX_H = 280, 200


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


# ═══════════════════════════════════════════════════════════════
# 图片预览弹窗
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# 生成线程
# ═══════════════════════════════════════════════════════════════

def _reload_generator_modules():
    """生成/测连前热重载，改 agent/graph 后无需重启整个应用。"""
    try:
        from backend.script_generator.reload import reload_script_generator
        reload_script_generator()
    except Exception as e:
        print(f"[ScriptGenerator] 热重载失败（将使用已加载模块）: {e}")


class ConnectionTestWorker(QThread):
    finished = Signal(dict)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.agent import test_connection as _test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(_test(**self.params))
            self.finished.emit(result)
        except Exception as e:
            self.finished.emit({
                "ok": False,
                "latency_ms": 0,
                "reply": "",
                "error": str(e),
                "input_tokens": 0,
                "output_tokens": 0,
            })
        finally:
            loop.close()


class GenerateWorker(QThread):
    finished = Signal(str)
    partial = Signal(str)  # 流式输出的片段
    status = Signal(str)  # LangGraph 阶段提示
    artifact = Signal(str, str)  # kind, payload（如 plan）
    token_info = Signal(int, int)  # 输入tokens, 输出tokens
    error = Signal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.agent import generate_script as _generate
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.params["on_partial"] = lambda text: self.partial.emit(text)
            self.params["on_status"] = lambda msg: self.status.emit(msg)
            self.params["on_artifact"] = lambda kind, payload: self.artifact.emit(kind, payload)
            code, inp, out = loop.run_until_complete(_generate(**self.params))
            self.token_info.emit(inp, out)
            self.finished.emit(code)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()


class ReviseWorker(QThread):
    finished = Signal(str, str, object)  # code, change_summary, meta
    partial = Signal(str)
    status = Signal(str)
    artifact = Signal(str, str)
    token_info = Signal(int, int)
    error = Signal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.agent import revise_script as _revise
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.params["on_partial"] = lambda text: self.partial.emit(text)
            self.params["on_status"] = lambda msg: self.status.emit(msg)
            self.params["on_artifact"] = lambda kind, payload: self.artifact.emit(kind, payload)
            code, summary, inp, out, meta = loop.run_until_complete(_revise(**self.params))
            self.token_info.emit(inp, out)
            self.finished.emit(code, summary or "", meta or {})
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()


class OptimizeWorker(QThread):
    finished = Signal(str, str, object)  # code, change_summary, meta
    partial = Signal(str)
    status = Signal(str)
    artifact = Signal(str, str)
    token_info = Signal(int, int)
    error = Signal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        _reload_generator_modules()
        from backend.script_generator.optimize import optimize_script as _optimize
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.params["on_partial"] = lambda text: self.partial.emit(text)
            self.params["on_status"] = lambda msg: self.status.emit(msg)
            self.params["on_artifact"] = lambda kind, payload: self.artifact.emit(kind, payload)
            code, summary, inp, out, meta = loop.run_until_complete(_optimize(**self.params))
            self.token_info.emit(inp, out)
            self.finished.emit(code, summary or "", meta or {})
        except Exception as e:
            self.error.emit(str(e))
        finally:
            loop.close()

# ═══════════════════════════════════════════════════════════════
# 主面板
# ═══════════════════════════════════════════════════════════════

class ScriptGenerator(QWidget):
    """脚本生成器面板"""

    IMG_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

    def __init__(self):
        super().__init__()
        self.setObjectName("ScriptGenPanel")
        self._image_entries: list[dict] = []
        self._source_dir: Path | None = None
        self._expl_path: Path | None = None
        self._expl_loading = False
        self._expl_save_timer = QTimer(self)
        self._expl_save_timer.setSingleShot(True)
        self._expl_save_timer.timeout.connect(self._autosave_explanation)
        self._generated_code: str = ""
        self._stream_buf: str = ""
        self._gen_ctx: dict = {}  # 生成管线校验上下文（plan/free/source_dir）
        self._explanation_text: str = ""
        self._worker: GenerateWorker | None = None
        self._revise_worker: ReviseWorker | None = None
        self._optimize_worker: OptimizeWorker | None = None
        self._optimize_code: str = ""
        self._optimize_script_path: Path | None = None
        self._optimize_script_files: list[Path] = []
        self._optimize_page = 0
        self._optimize_folder = ""
        self._optimize_log_lines: list[str] = []
        self._pre_optimize_trial_log: str = ""
        self._pre_optimize_record_dir: str = ""
        self._awaiting_reliability_retrial: bool = False
        self._facade = None
        self._trial_running = False
        self._trial_account_name: str = ""
        self._trial_log_lines: list[str] = []
        self._last_trial_frame = None
        self._stop_frame_path: str = ""
        self._last_revise_summary: str = ""
        self._last_revise_error: str = ""
        self._last_diagnosis_json: str = ""
        self._chat_session: dict | None = None
        self._last_inp_tokens = 0
        self._last_out_tokens = 0
        self._trial_blocked = False
        self._trial_block_reason = ""
        self._busy_banner_base = ""
        self._agent_busy = False
        self._agent_status_base = ""
        self._agent_busy_started = 0.0
        self._holding_prev_live_code = False
        self._agent_elapsed_timer = QTimer(self)
        self._agent_elapsed_timer.setInterval(1000)
        self._agent_elapsed_timer.timeout.connect(self._tick_agent_elapsed)
        from backend.script_generator.session_archive import SessionArchive
        self._archive = SessionArchive()
        self._settings_path = Path.home() / ".minashigo" / "script_gen_config.json"
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._build_ui()
        self._update_expl_path_label()

    def set_facade(self, facade):
        """注入主程序 Facade，用于账号列表与试运行。"""
        if self._facade is facade:
            self._refresh_accounts()
            self._update_trial_availability()
            return
        if self._facade is not None:
            try:
                self._facade.controller.log_signal.disconnect(self._on_trial_log)
            except Exception:
                pass
            try:
                self._facade.controller.state_event.disconnect(self._on_trial_state)
            except Exception:
                pass
        self._facade = facade
        if facade is not None:
            facade.controller.log_signal.connect(
                self._on_trial_log, type=Qt.QueuedConnection
            )
            facade.controller.state_event.connect(
                self._on_trial_state, type=Qt.QueuedConnection
            )
            try:
                facade.controller.screenshot_ready.connect(
                    self._on_screenshot_ready, type=Qt.QueuedConnection
                )
            except Exception:
                pass
        self._refresh_accounts()
        self._update_trial_availability()

    # ── UI 构建 ──

    # tab 索引
    TAB_API = 0
    TAB_INPUT = 1
    TAB_GEN = 2
    TAB_TRIAL = 3
    TAB_OPTIMIZE = 4

    @staticmethod
    def _wrap_scroll(inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(inner)
        return scroll

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 10)
        outer.setSpacing(6)

        outer.addWidget(self._build_agent_strip())

        self._tabs = QTabWidget()
        self._tabs.setObjectName("ScriptGenTabs")
        outer.addWidget(self._tabs, 1)

        self._tabs.addTab(self._wrap_scroll(self._build_page_api()), "1. API 配置")
        self._tabs.addTab(self._wrap_scroll(self._build_page_input()), "2. 描述与素材")
        self._tabs.addTab(self._build_page_generate(), "3. 生成")
        self._tabs.addTab(self._build_page_trial(), "4. 试运行")
        self._tabs.addTab(self._wrap_scroll(self._build_page_optimize()), "5. 脚本优化")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # 提供商列表要在控件建完后初始化（先屏蔽信号，避免 addItems 触发保存冲掉已有配置）
        self._providers_config = self._load_providers_config()
        self._fill_provider_combo(self._provider)
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        self._refresh_models()
        self._fill_provider_combo(self._vision_provider, vision_only=True)
        self._vision_provider.currentIndexChanged.connect(self._on_vision_provider_changed)
        self._refresh_vision_models()
        self._load_settings()
        self._apply_provider_hint()
        self._update_trial_availability()
        self._set_agent_idle("就绪 · 配置 API 后开始生成")

    def _build_agent_strip(self) -> QWidget:
        """跨 Tab 常驻：当前阶段 / 进度 / token / 取消 / 归档。"""
        strip = QFrame()
        strip.setObjectName("AgentRunStrip")
        strip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(10)

        self._agent_dots = _BounceDots()
        self._agent_dot = self._agent_dots  # 兼容旧属性名（busy/success 走 start/stop）
        lay.addWidget(self._agent_dots)

        self._agent_phase = QLabel("Agent")
        pf = QFont()
        pf.setBold(True)
        pf.setPointSize(11)
        self._agent_phase.setFont(pf)
        self._agent_phase.setObjectName("AgentRunPhase")
        lay.addWidget(self._agent_phase)

        self._agent_status = QLabel("就绪")
        self._agent_status.setObjectName("MutedLabel")
        self._agent_status.setWordWrap(False)
        lay.addWidget(self._agent_status, 1)

        self._progress = QProgressBar()
        self._progress.setObjectName("ScriptGenProgress")
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setMinimumWidth(100)
        self._progress.setMaximumWidth(180)
        self._progress.hide()
        lay.addWidget(self._progress)

        self._token_label = QLabel("")
        self._token_label.setObjectName("MutedLabel")
        lay.addWidget(self._token_label)

        self._strip_cancel_btn = QPushButton("取消")
        self._strip_cancel_btn.setObjectName("GhostButton")
        self._strip_cancel_btn.setEnabled(False)
        self._strip_cancel_btn.setFixedHeight(26)
        self._strip_cancel_btn.clicked.connect(self._cancel_generate)
        lay.addWidget(self._strip_cancel_btn)

        self._archive_btn = QPushButton("打开归档")
        self._archive_btn.setObjectName("GhostButton")
        self._archive_btn.setToolTip("打开本次会话材料目录（代码 / 轨迹 / 日志）")
        self._archive_btn.setFixedHeight(26)
        self._archive_btn.clicked.connect(self._open_session_archive)
        lay.addWidget(self._archive_btn)

        self._goto_traj_btn = QPushButton("轨迹")
        self._goto_traj_btn.setObjectName("GhostButton")
        self._goto_traj_btn.setToolTip("跳到生成页查看 Agent 轨迹与实时代码")
        self._goto_traj_btn.setFixedHeight(26)
        self._goto_traj_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_GEN))
        lay.addWidget(self._goto_traj_btn)

        self._agent_busy = False
        return strip

    def _build_page_api(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        # ===== 多套配置方案 =====
        profile_group = QGroupBox("配置方案")
        g_prof = QVBoxLayout(profile_group)
        _pl = QLabel(
            "一套方案 = 主模型 + 辅助识图（含各自 Key）。"
            "「默认」是空草稿：填了但没点保存时，也会写进「默认」。"
            "正式组合请用「另存为…」（如 DeepSeek+千问）。"
        )
        _pl.setObjectName("MutedLabel")
        _pl.setWordWrap(True)
        g_prof.addWidget(_pl)
        prow = QHBoxLayout()
        self._profile_combo = _NoWheelComboBox()
        self._profile_combo.setMinimumWidth(180)
        self._profile_combo.setToolTip(
            "当前启用的 API 配置方案。\n"
            "再次点选同一方案可丢弃未保存修改并恢复已保存内容。"
        )
        # activated：即使选同一项也会触发，便于「重选当前方案 → 恢复已保存」
        self._profile_combo.activated.connect(self._on_profile_activated)
        prow.addWidget(QLabel("当前方案:"), 0)
        prow.addWidget(self._profile_combo, 1)
        self._profile_save_btn = QPushButton("保存到当前")
        self._profile_save_btn.setToolTip("把下方填写的主模型/辅助识图保存进当前方案")
        self._profile_save_btn.clicked.connect(self._save_settings)
        prow.addWidget(self._profile_save_btn)
        self._profile_save_as_btn = QPushButton("另存为…")
        self._profile_save_as_btn.setToolTip("复制当前填写内容为新方案并切换过去")
        self._profile_save_as_btn.clicked.connect(self._save_profile_as)
        prow.addWidget(self._profile_save_as_btn)
        self._profile_del_btn = QPushButton("删除")
        self._profile_del_btn.setToolTip("删除当前方案（至少保留一套）")
        self._profile_del_btn.clicked.connect(self._delete_current_profile)
        prow.addWidget(self._profile_del_btn)
        g_prof.addLayout(prow)
        layout.addWidget(profile_group)
        self._profile_switching = False

        api_group = QGroupBox("API 设置（主模型）")
        g_api = QVBoxLayout(api_group)
        _al = QLabel(
            "在此填写主生成/修订用的 AI 账号。填好后建议先点「连接测试」，"
            "通过后再写脚本描述并生成代码。"
        )
        _al.setObjectName("MutedLabel")
        _al.setWordWrap(True)
        g_api.addWidget(_al)
        f = QFormLayout()
        g_api.addLayout(f)

        self._endpoint = QLineEdit()
        self._endpoint.setPlaceholderText("留空则用该提供商官方地址；中转站请填写")
        self._endpoint.setToolTip(
            "API 服务器地址。切换提供商时会填入官方默认地址。\n"
            "使用中转站 / 代理时再改成你的地址（通常以 https:// 开头）。"
        )
        f.addRow("自定义端点:", self._endpoint)

        self._provider = _NoWheelComboBox()
        self._provider.setMaxVisibleItems(24)
        self._provider.setToolTip(
            "选择 AI 服务商。必须与 API Key 来源一致。\n"
            "国内常见：DeepSeek / 通义千问 / Kimi / 智谱 / 豆包；\n"
            "聚合：OpenRouter、硅基流动；本地：Ollama、LM Studio。"
        )
        f.addRow("提供商:", self._provider)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("在官网申请的密钥，粘贴到这里")
        self._api_key.setToolTip(
            "在对应提供商官网申请的访问密钥（一串以 sk- 等开头的字符）。\n"
            "相当于登录密码，请勿泄露。按方案分别保存在系统凭据里。"
        )
        f.addRow("API Key:", self._api_key)

        self._model = _NoWheelComboBox()
        self._model.setEditable(True)
        self._model.setToolTip(
            "具体使用哪一个 AI 模型。可从列表选择，也可手动输入模型名。\n"
            "不同提供商的模型名不能混用。"
        )
        f.addRow("模型:", self._model)

        self._max_tokens = _NoWheelSpinBox()
        self._max_tokens.setRange(0, 128000)
        self._max_tokens.setSingleStep(1024)
        self._max_tokens.setSpecialValueText("无上限")
        self._max_tokens.setValue(self._default_max_tokens())
        self._max_tokens.setToolTip(
            "单次生成允许的最大输出长度（max_tokens）。\n"
            "设为 0（显示「无上限」）表示不限制输出长度。\n"
            "脚本较长时可调高（如 16384～32768）；\n"
            "DeepSeek 若正文被截断为空，也可适当调高后重试。\n"
            "数值越大通常越慢、费用越高。"
        )
        f.addRow("最大输出 tokens:", self._max_tokens)

        self._provider_hint = QLabel()
        self._provider_hint.setStyleSheet("color:#e8a000;font-size:11px;")
        self._provider_hint.setVisible(False)
        self._provider_hint.setWordWrap(True)
        f.addRow(self._provider_hint)

        btn_row = QHBoxLayout()
        self._test_btn = QPushButton("连接测试")
        self._test_btn.setToolTip("用当前提供商 / 模型 / Key 发送一条短消息验证连通性")
        self._test_btn.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self._test_btn)
        btn_row.addStretch()
        next_btn = QPushButton("下一步：描述与素材 →")
        next_btn.setObjectName("PrimaryButton")
        next_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_INPUT))
        btn_row.addWidget(next_btn)
        g_api.addLayout(btn_row)

        self._test_status = QLabel("")
        self._test_status.setWordWrap(True)
        self._test_status.setStyleSheet("color:#888;font-size:11px;")
        g_api.addWidget(self._test_status)
        self._test_worker: ConnectionTestWorker | None = None

        layout.addWidget(api_group)

        # ===== 辅助识图 API =====
        vision_group = QGroupBox("辅助识图（可选，随方案一起切换）")
        g_vis = QVBoxLayout(vision_group)
        _vl = QLabel(
            "主模型若不支持看图，可在此单独配置识图模型。"
            "生成时选「辅助识图」即可先识图再写代码。"
            "另外：主模型（如 DeepSeek）无修订 tools 时，若此处填了千问等 Key，"
            "会自动用「qwen3.5-flash」代查函数/日志，再交给主模型写补丁。"
        )
        _vl.setObjectName("MutedLabel")
        _vl.setWordWrap(True)
        g_vis.addWidget(_vl)
        vf = QFormLayout()
        g_vis.addLayout(vf)

        self._vision_endpoint = QLineEdit()
        self._vision_endpoint.setPlaceholderText("留空则用该提供商官方地址；中转站请填写")
        self._vision_endpoint.setToolTip("识图模型的自定义 API 端点。切换提供商时会填入官方默认地址。")
        vf.addRow("自定义端点:", self._vision_endpoint)

        self._vision_provider = _NoWheelComboBox()
        self._vision_provider.setMaxVisibleItems(24)
        self._vision_provider.setToolTip(
            "识图用的提供商，请选支持图片输入的模型（Claude / GPT-4o / Qwen-VL / GLM-4V 等）。"
        )
        vf.addRow("提供商:", self._vision_provider)

        self._vision_api_key = QLineEdit()
        self._vision_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._vision_api_key.setPlaceholderText("识图专用 Key；可与主模型相同")
        self._vision_api_key.setToolTip("识图模型的 API Key，可与上方主模型共用同一把。")
        vf.addRow("API Key:", self._vision_api_key)

        self._vision_model = _NoWheelComboBox()
        self._vision_model.setEditable(True)
        self._vision_model.setToolTip("具备识图能力的模型名，例如 gpt-4o、claude-sonnet-4。")
        vf.addRow("模型:", self._vision_model)

        vis_btn_row = QHBoxLayout()
        self._vision_test_btn = QPushButton("识图连接测试")
        self._vision_test_btn.setToolTip("用当前识图提供商 / 模型 / Key 做连通性测试（纯文本探测）")
        self._vision_test_btn.clicked.connect(self._on_test_vision_connection)
        vis_btn_row.addWidget(self._vision_test_btn)
        vis_btn_row.addStretch()
        g_vis.addLayout(vis_btn_row)

        self._vision_test_status = QLabel("")
        self._vision_test_status.setWordWrap(True)
        self._vision_test_status.setStyleSheet("color:#888;font-size:11px;")
        g_vis.addWidget(self._vision_test_status)
        self._vision_test_worker: ConnectionTestWorker | None = None

        layout.addWidget(vision_group)
        layout.addStretch()
        return page

    def _build_page_input(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(8)

        # ===== 脚本描述 =====
        desc_group = QGroupBox("脚本描述")
        dl = QVBoxLayout(desc_group)
        _dl = QLabel("描述脚本的功能和流程逻辑，AI 将据此生成代码")
        _dl.setObjectName("MutedLabel")
        dl.addWidget(_dl)

        h = QHBoxLayout()
        self._load_expl_btn = QPushButton("加载介绍 txt")
        self._load_expl_btn.clicked.connect(self._load_explanation)
        h.addWidget(self._load_expl_btn)
        self._save_expl_btn = QPushButton("保存到文件")
        self._save_expl_btn.setToolTip("把当前描述（含试运行反馈）写回绑定的介绍 txt")
        self._save_expl_btn.clicked.connect(self._save_explanation_clicked)
        h.addWidget(self._save_expl_btn)
        h.addStretch()
        dl.addLayout(h)

        self._expl_path_label = QLabel("未绑定文件：修改只留在本窗口，关闭即丢失")
        self._expl_path_label.setObjectName("MutedLabel")
        self._expl_path_label.setWordWrap(True)
        dl.addWidget(self._expl_path_label)

        self._explanation = QTextEdit()
        self._explanation.setPlaceholderText("粘贴脚本解释内容，或点击上方加载 .txt 文件")
        self._explanation.setMinimumHeight(140)
        self._explanation.textChanged.connect(self._on_explanation_edited)
        dl.addWidget(self._explanation, 1)

        layout.addWidget(desc_group, 1)

        # ===== 图片管理 =====
        img_group = QGroupBox("参考图片")
        il = QVBoxLayout(img_group)
        _il = QLabel("选择图片所在文件夹，生成脚本时自动引用此路径")
        _il.setObjectName("MutedLabel")
        il.addWidget(_il)

        btn_row = QHBoxLayout()
        self._add_folder_btn = QPushButton("选择图片文件夹")
        self._add_folder_btn.clicked.connect(self._add_folder)
        btn_row.addWidget(self._add_folder_btn)

        self._preview_all_btn = QPushButton("预览全部")
        self._preview_all_btn.clicked.connect(self._preview_all)
        btn_row.addWidget(self._preview_all_btn)

        self._clear_img_btn = QPushButton("清空")
        self._clear_img_btn.clicked.connect(self._clear_images)
        btn_row.addWidget(self._clear_img_btn)
        btn_row.addStretch()
        il.addLayout(btn_row)

        self._img_label = QLabel("未选择图片文件夹")
        self._img_label.setObjectName("MutedLabel")
        il.addWidget(self._img_label)

        opt_row = QHBoxLayout()
        self._send_img_cb = QCheckBox("发送图片给 AI")
        self._send_img_cb.setChecked(True)
        self._send_img_cb.toggled.connect(self._on_send_img_toggled)
        opt_row.addWidget(self._send_img_cb)
        self._compress_img_cb = QCheckBox("压缩图片（省 token）")
        self._compress_img_cb.setChecked(False)
        self._compress_img_cb.setEnabled(True)
        opt_row.addWidget(self._compress_img_cb)
        self._free_mode_cb = QCheckBox("自由模式（少约束）")
        self._free_mode_cb.setToolTip(
            "生成端放宽：关闭 Rules / plan / IR；仍注入结构范式 few-shot。\n"
            "生成时不校验素材文件是否存在；写入试运行文件时自动脚本检查（含素材）。\n"
            "成品结构校验加严；失败可自动修复（默认最多 3 轮）。\n"
            "生成/修订仍会运行结构类本地 patch（不注入业务控制流）。\n"
            "勾选会写入 config.json defaults.codegen_free_mode。"
        )
        self._free_mode_cb.setChecked(self._default_codegen_free_mode())
        self._free_mode_cb.toggled.connect(self._on_free_mode_toggled)
        opt_row.addWidget(self._free_mode_cb)
        opt_row.addStretch()
        il.addLayout(opt_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("识图方式:"))
        self._image_mode = _NoWheelComboBox()
        self._image_mode.addItem("主模型直接发图", "direct")
        self._image_mode.addItem("辅助识图（先描述再生成）", "assist")
        self._image_mode.setToolTip(
            "主模型直接发图：把图片发给上方主模型（需主模型支持识图）。\n"
            "辅助识图：先用 API 页配置的识图模型描述图片，再把文字交给主模型写代码。"
        )
        mode_row.addWidget(self._image_mode, 1)
        il.addLayout(mode_row)

        vision_refresh_row = QHBoxLayout()
        self._vision_refresh_cb = QCheckBox("本次重新识图（忽略缓存）")
        self._vision_refresh_cb.setToolTip(
            "勾选后，本次生成会重新调用识图 API，不使用 .vision_cache 与识图目录.txt。\n"
            "脚本介绍仍会作为上下文发给识图模型；完成后会覆盖缓存与识图目录。\n"
            "仅对「辅助识图」模式生效。"
        )
        self._vision_refresh_cb.setEnabled(False)
        vision_refresh_row.addWidget(self._vision_refresh_cb)
        self._clear_vision_cache_btn = QPushButton("清除识图缓存")
        self._clear_vision_cache_btn.setToolTip(
            "删除当前图片文件夹下的 .vision_cache 与识图目录.txt，不影响脚本介绍。"
        )
        self._clear_vision_cache_btn.clicked.connect(self._on_clear_vision_cache)
        vision_refresh_row.addWidget(self._clear_vision_cache_btn)
        vision_refresh_row.addStretch()
        il.addLayout(vision_refresh_row)
        self._image_mode.currentIndexChanged.connect(self._on_image_mode_changed)

        layout.addWidget(img_group)

        # ===== 输出设置 =====
        out_group = QGroupBox("输出（确认完成时的正式保存位置）")
        og = QVBoxLayout(out_group)
        _ol = QLabel("试运行用临时文件；此处仅用于最终「确认完成并保存」")
        _ol.setObjectName("MutedLabel")
        og.addWidget(_ol)
        ol = QHBoxLayout()
        ol.addWidget(QLabel("目录:"))
        self._output_dir = QLineEdit(str(SCRIPTS_PATH))
        self._output_dir.setReadOnly(True)
        ol.addWidget(self._output_dir, 1)
        self._output_dir_btn = QPushButton("选择目录")
        self._output_dir_btn.clicked.connect(self._browse_output)
        ol.addWidget(self._output_dir_btn)
        ol.addWidget(QLabel("文件名:"))
        self._script_name = QLineEdit()
        self._script_name.setPlaceholderText("my_script.py")
        self._script_name.setFixedWidth(200)
        ol.addWidget(self._script_name)
        og.addLayout(ol)
        layout.addWidget(out_group)

        nav = QHBoxLayout()
        back_btn = QPushButton("← 上一步")
        back_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_API))
        nav.addWidget(back_btn)
        nav.addStretch()
        next_btn = QPushButton("下一步：生成 →")
        next_btn.setObjectName("PrimaryButton")
        next_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_GEN))
        nav.addWidget(next_btn)
        layout.addLayout(nav)
        return page

    def _build_page_generate(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(8)

        gen_row = QHBoxLayout()
        self._generate_btn = QPushButton("生成脚本")
        self._generate_btn.setObjectName("PrimaryButton")
        self._generate_btn.clicked.connect(self._on_generate)
        gen_row.addWidget(self._generate_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_generate)
        gen_row.addWidget(self._cancel_btn)

        self._rerevise_btn = QPushButton("重修订")
        self._rerevise_btn.setToolTip(
            "用试运行页的反馈对当前代码再次修订（不重新生成）。\n"
            "硬校验未通过时可用此按钮补修；无反馈时会自动带上校验错误说明。"
        )
        self._rerevise_btn.setEnabled(False)
        self._rerevise_btn.clicked.connect(self._on_rerevise)
        gen_row.addWidget(self._rerevise_btn)
        gen_row.addStretch(1)
        layout.addLayout(gen_row)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("AgentWorkspaceSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)
        self._gen_result_banner = QLabel("")
        self._gen_result_banner.setObjectName("GenResultBanner")
        self._gen_result_banner.setWordWrap(True)
        self._gen_result_banner.hide()
        left_lay.addWidget(self._gen_result_banner)
        self._trajectory = GenTrajectory()
        left_lay.addWidget(self._trajectory, 1)
        split.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)
        code_head = QHBoxLayout()
        code_title = QLabel("实时代码")
        ctf = QFont()
        ctf.setBold(True)
        code_title.setFont(ctf)
        code_head.addWidget(code_title)
        self._live_code_hint = QLabel("流式输出会显示在这里")
        self._live_code_hint.setObjectName("MutedLabel")
        code_head.addWidget(self._live_code_hint, 1)
        right_lay.addLayout(code_head)

        self._live_code = QTextEdit()
        self._live_code.setObjectName("AgentLiveCode")
        self._live_code.setReadOnly(True)
        self._live_code.setFont(QFont("Consolas", 9))
        self._live_code.setPlaceholderText(
            "生成时这里会实时滚动代码。\n"
            "修订 / 优化等待阶段会暂显上一版，开始写代码后会替换为新内容。"
        )
        self._live_code.setMinimumWidth(280)
        right_lay.addWidget(self._live_code, 1)
        split.addWidget(right)

        split.setStretchFactor(0, 45)
        split.setStretchFactor(1, 55)
        split.setSizes([360, 440])
        layout.addWidget(split, 1)

        self._live_code_timer = QTimer(self)
        self._live_code_timer.setSingleShot(True)
        self._live_code_timer.timeout.connect(self._flush_live_code)

        act_row = QHBoxLayout()
        self._view_code_btn = QPushButton("查看完整代码")
        self._view_code_btn.setToolTip(
            "查看完整脚本（默认锁定只读，可解锁编辑；Ctrl+F 搜索）"
        )
        self._view_code_btn.setEnabled(False)
        self._view_code_btn.clicked.connect(self._view_full_code)
        act_row.addWidget(self._view_code_btn)

        self._save_btn = QPushButton("保存到文件")
        self._save_btn.clicked.connect(self._save_script)
        self._save_btn.setEnabled(False)
        act_row.addWidget(self._save_btn)

        self._copy_btn = QPushButton("复制代码")
        self._copy_btn.clicked.connect(self._copy_code)
        self._copy_btn.setEnabled(False)
        act_row.addWidget(self._copy_btn)
        act_row.addStretch()
        to_trial = QPushButton("去试运行 →")
        to_trial.setObjectName("PrimaryButton")
        to_trial.setToolTip("生成已完成时可点此进入试运行验证脚本")
        to_trial.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_TRIAL))
        self._to_trial_btn = to_trial
        act_row.addWidget(to_trial)
        layout.addLayout(act_row)
        return page

    def _build_page_trial(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(8)

        tip = QLabel(
            "从已启动账号里选一个试跑。生成后请对齐 _img 标识与素材文件名；"
            "写入试运行文件时会自动做脚本检查（结构 + 素材）。"
        )
        tip.setWordWrap(True)
        tip.setObjectName("MutedLabel")
        layout.addWidget(tip)

        self._trial_hint = QLabel("")
        self._trial_hint.setWordWrap(True)
        self._trial_hint.setObjectName("MutedLabel")
        self._trial_hint.setStyleSheet("color:#b8891a;")
        layout.addWidget(self._trial_hint)

        # 试跑阶段芯片
        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self._trial_chips: dict[str, QLabel] = {}
        for key, label in (
            ("write", "写入"),
            ("run", "运行"),
            ("frame", "停帧"),
            ("feas", "可行性"),
        ):
            chip = QLabel(label)
            chip.setObjectName("TrialStageChip")
            chip.setProperty("stage", "idle")
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setFixedHeight(22)
            chip.setMinimumWidth(52)
            self._trial_chips[key] = chip
            chip_row.addWidget(chip)
        chip_row.addStretch(1)
        layout.addLayout(chip_row)
        self._refresh_trial_chip_styles()

        acc_row = QHBoxLayout()
        acc_row.addWidget(QLabel("账号:"))
        self._account_combo = _NoWheelComboBox()
        self._account_combo.setMinimumWidth(180)
        self._account_combo.setToolTip("仅列出已启动浏览器或已绑定窗口的账号")
        acc_row.addWidget(self._account_combo, 1)
        self._refresh_acc_btn = QPushButton("刷新账号")
        self._refresh_acc_btn.clicked.connect(self._refresh_accounts)
        acc_row.addWidget(self._refresh_acc_btn)
        self._trial_btn = QPushButton("试运行")
        self._trial_btn.setObjectName("PrimaryButton")
        self._trial_btn.setEnabled(False)
        self._trial_btn.clicked.connect(self._on_trial_run)
        acc_row.addWidget(self._trial_btn)
        self._stop_trial_btn = QPushButton("停止")
        self._stop_trial_btn.setObjectName("DangerButton")
        self._stop_trial_btn.setEnabled(False)
        self._stop_trial_btn.clicked.connect(self._on_stop_trial)
        acc_row.addWidget(self._stop_trial_btn)
        layout.addLayout(acc_row)

        # 试运行 Live HUD：状态点 / 当前状态 / 最近动作 / 计时 / L1 结论
        self._trial_hud = TrialHud()
        layout.addWidget(self._trial_hud)

        mid = QSplitter(Qt.Orientation.Horizontal)
        mid.setChildrenCollapsible(False)
        mid.setHandleWidth(6)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        log_panel = QWidget()
        log_lay = QVBoxLayout(log_panel)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(4)
        log_lay.addWidget(QLabel("试运行日志:"))
        self._trial_log = QTextEdit()
        self._trial_log.setReadOnly(True)
        self._trial_log.setFont(QFont("Consolas", 9))
        self._trial_log.setPlaceholderText("试运行时的脚本日志会出现在这里…")
        self._trial_log.setMinimumHeight(80)
        log_lay.addWidget(self._trial_log, 1)

        fb_panel = QWidget()
        fb_lay = QVBoxLayout(fb_panel)
        fb_lay.setContentsMargins(0, 0, 0, 0)
        fb_lay.setSpacing(4)
        fb_head = QHBoxLayout()
        self._feedback_title = QLabel("反馈（试运行问题描述）:")
        fb_head.addWidget(self._feedback_title)
        self._feedback_stale_hint = QLabel("上次反馈 · 可改写或清空后再修订")
        self._feedback_stale_hint.setObjectName("MutedLabel")
        self._feedback_stale_hint.setStyleSheet("color:#b8891a;")
        self._feedback_stale_hint.hide()
        fb_head.addWidget(self._feedback_stale_hint, 1)
        fb_lay.addLayout(fb_head)
        self._feedback = QTextEdit()
        self._feedback.setObjectName("ScriptGenFeedback")
        self._feedback.setAcceptRichText(False)
        self._feedback.setPlaceholderText(
            "修订时会把本框反馈 + 下方试运行日志（最近约 200 行）一并发给 AI。\n"
            "建议一条一行、写清现象与期望，例如：\n"
            "1. 卡在主界面，日志里一直 match home，但从不点出击\n"
            "2. 点了竞技场入口却进了爬塔，入口点错了\n"
            "3. 识别到 logo 后不要立刻退出，应先领完奖励\n"
            "4. 某某状态超时太短，请改成 60 秒并补 script_log\n"
            "可写：卡在哪一屏 / 点了什么 / 实际去哪 / 日志关键词 / 期望行为"
        )
        self._feedback.setMinimumHeight(60)
        self._feedback.textChanged.connect(self._on_feedback_edited)
        fb_lay.addWidget(self._feedback, 1)
        self._feedback_stale = False
        self._set_feedback_stale(False)

        splitter.addWidget(log_panel)
        splitter.addWidget(fb_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([240, 160])
        mid.addWidget(splitter)

        frame_panel = QWidget()
        frame_panel.setMinimumWidth(160)
        frame_panel.setMaximumWidth(260)
        fl = QVBoxLayout(frame_panel)
        fl.setContentsMargins(4, 0, 0, 0)
        fl.setSpacing(4)
        fl.addWidget(QLabel("Agent 所见（停帧）"))
        self._trial_frame_lbl = QLabel("试跑结束或停止后\n显示画面缩略图")
        self._trial_frame_lbl.setObjectName("TrialFramePreview")
        self._trial_frame_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._trial_frame_lbl.setMinimumHeight(120)
        self._trial_frame_lbl.setWordWrap(True)
        self._trial_frame_lbl.setScaledContents(False)
        fl.addWidget(self._trial_frame_lbl, 1)
        mid.addWidget(frame_panel)
        mid.setStretchFactor(0, 4)
        mid.setStretchFactor(1, 1)
        mid.setSizes([520, 180])
        layout.addWidget(mid, 1)

        rev_row = QHBoxLayout()
        back_btn = QPushButton("← 回生成页")
        back_btn.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_GEN))
        rev_row.addWidget(back_btn)
        rev_row.addStretch()
        self._revise_btn = QPushButton("根据反馈修订")
        self._revise_btn.setObjectName("PrimaryButton")
        self._revise_btn.setEnabled(False)
        self._revise_btn.clicked.connect(self._on_revise)
        rev_row.addWidget(self._revise_btn)
        self._confirm_btn = QPushButton("确认完成并保存")
        self._confirm_btn.setObjectName("PrimaryButton")
        self._confirm_btn.setEnabled(False)
        self._confirm_btn.clicked.connect(self._on_confirm_done)
        rev_row.addWidget(self._confirm_btn)
        to_opt = QPushButton("去脚本优化 →")
        to_opt.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_OPTIMIZE))
        rev_row.addWidget(to_opt)
        layout.addLayout(rev_row)
        return page

    def _build_page_optimize(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(6)

        tip = QLabel(
            "选脚本 → 写优化方向/反馈 → 开始优化。"
            "账号与试跑均为可选；改完后再去「试运行」验证。"
            "试运行会自动开伪录制；确认保存时再去掉。"
        )
        tip.setWordWrap(True)
        tip.setObjectName("MutedLabel")
        tip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(tip)

        self._optimize_hint = QLabel("")
        self._optimize_hint.setWordWrap(True)
        self._optimize_hint.setObjectName("MutedLabel")
        self._optimize_hint.setStyleSheet("color:#b8891a;")
        self._optimize_hint.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        layout.addWidget(self._optimize_hint)

        browse = QGroupBox("脚本库（分页）")
        browse.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        bl = QVBoxLayout(browse)
        bl.setSpacing(4)
        filt = QHBoxLayout()
        filt.addWidget(QLabel("目录:"))
        self._optimize_folder_combo = _NoWheelComboBox()
        self._optimize_folder_combo.setMinimumWidth(140)
        self._optimize_folder_combo.currentIndexChanged.connect(self._on_optimize_folder_changed)
        filt.addWidget(self._optimize_folder_combo)
        filt.addWidget(QLabel("搜索:"))
        self._optimize_search = QLineEdit()
        self._optimize_search.setPlaceholderText("文件名或路径关键词…")
        self._optimize_search.returnPressed.connect(self._refresh_optimize_script_list)
        filt.addWidget(self._optimize_search, 1)
        refresh_scripts = QPushButton("刷新列表")
        refresh_scripts.clicked.connect(self._refresh_optimize_script_list)
        filt.addWidget(refresh_scripts)
        bl.addLayout(filt)

        self._optimize_script_list = QListWidget()
        self._optimize_script_list.setMinimumHeight(48)
        self._optimize_script_list.setMaximumHeight(120)
        self._optimize_script_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._optimize_script_list.itemClicked.connect(self._on_optimize_script_activated)
        bl.addWidget(self._optimize_script_list, 0)

        pager = QHBoxLayout()
        self._optimize_prev_btn = QPushButton("上一页")
        self._optimize_prev_btn.clicked.connect(lambda: self._optimize_change_page(-1))
        pager.addWidget(self._optimize_prev_btn)
        self._optimize_page_label = QLabel("第 1/1 页")
        self._optimize_page_label.setObjectName("MutedLabel")
        pager.addWidget(self._optimize_page_label)
        self._optimize_next_btn = QPushButton("下一页")
        self._optimize_next_btn.clicked.connect(lambda: self._optimize_change_page(1))
        pager.addWidget(self._optimize_next_btn)
        pager.addStretch()
        load_btn = QPushButton("加载选中")
        load_btn.clicked.connect(self._on_optimize_load_selected)
        pager.addWidget(load_btn)
        pick_btn = QPushButton("浏览…")
        pick_btn.clicked.connect(self._on_optimize_pick_file)
        pager.addWidget(pick_btn)
        bl.addLayout(pager)
        layout.addWidget(browse)

        loaded_row = QHBoxLayout()
        loaded_row.addWidget(QLabel("当前脚本:"))
        self._optimize_path_label = QLabel("（未加载）")
        self._optimize_path_label.setObjectName("MutedLabel")
        self._optimize_path_label.setWordWrap(False)
        self._optimize_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        loaded_row.addWidget(self._optimize_path_label, 1)
        self._optimize_save_btn = QPushButton("保存到文件")
        self._optimize_save_btn.setEnabled(False)
        self._optimize_save_btn.clicked.connect(self._on_optimize_save)
        loaded_row.addWidget(self._optimize_save_btn)
        layout.addLayout(loaded_row)

        # 反馈 + 日志可拖拽分配高度，避免固定最小高度把窗口顶死
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(True)

        fb_box = QGroupBox("优化方向 / 反馈（可选）")
        fb_lay = QVBoxLayout(fb_box)
        fb_lay.setContentsMargins(6, 6, 6, 6)
        self._optimize_feedback = QTextEdit()
        self._optimize_feedback.setAcceptRichText(False)
        self._optimize_feedback.setPlaceholderText(
            "写希望怎么改，例如：登录等待过长、某状态匹配太勤、减少黑屏空转匹配…\n"
            "留空则按伪录制/通用性能规则优化。"
        )
        self._optimize_feedback.setMinimumHeight(40)
        self._optimize_feedback.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        fb_lay.addWidget(self._optimize_feedback)
        split.addWidget(fb_box)

        log_box = QGroupBox("优化日志")
        log_lay = QVBoxLayout(log_box)
        log_lay.setContentsMargins(6, 6, 6, 6)
        self._optimize_log = QTextEdit()
        self._optimize_log.setReadOnly(True)
        self._optimize_log.setFont(QFont("Consolas", 9))
        self._optimize_log.setPlaceholderText("优化日志与边界评估…")
        self._optimize_log.setMinimumHeight(40)
        self._optimize_log.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        log_lay.addWidget(self._optimize_log)
        split.addWidget(log_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([120, 100])
        layout.addWidget(split, 1)

        act = QHBoxLayout()
        view_btn = QPushButton("查看代码")
        view_btn.clicked.connect(self._view_optimize_code)
        act.addWidget(view_btn)
        to_trial = QPushButton("去试运行")
        to_trial.setToolTip("可选：优化后再选账号试跑验证")
        to_trial.clicked.connect(self._on_optimize_go_trial)
        act.addWidget(to_trial)
        act.addStretch()
        self._optimize_btn = QPushButton("开始优化")
        self._optimize_btn.setObjectName("PrimaryButton")
        self._optimize_btn.setEnabled(False)
        self._optimize_btn.setToolTip(
            "按上方反馈与（若有）伪录制做 AI 修订。\n"
            "无需账号；试跑可在改完后单独进行。"
        )
        self._optimize_btn.clicked.connect(self._on_optimize)
        act.addWidget(self._optimize_btn)
        layout.addLayout(act)

        self._init_optimize_folder_combo()
        return page

    # ── 持久化存储（多方案）──

    @staticmethod
    def _default_max_tokens() -> int:
        import json
        path = Path(__file__).parent.parent.parent / "backend" / "script_generator" / "config.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            return int(cfg.get("defaults", {}).get("max_tokens", 16384))
        except Exception:
            return 16384

    @staticmethod
    def _default_codegen_free_mode() -> bool:
        import json
        path = Path(__file__).parent.parent.parent / "backend" / "script_generator" / "config.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            return bool(cfg.get("defaults", {}).get("codegen_free_mode", False))
        except Exception:
            return False

    def _on_free_mode_toggled(self, checked: bool):
        """同步到 config.json，生成/修订共用。"""
        import json
        path = Path(__file__).parent.parent.parent / "backend" / "script_generator" / "config.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            defaults = cfg.setdefault("defaults", {})
            defaults["codegen_free_mode"] = bool(checked)
            path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                f"[ScriptGenerator] codegen_free_mode="
                f"{'on' if checked else 'off'}"
            )
        except Exception as e:
            print(f"[ScriptGenerator] 写入自由模式失败: {e}")

    @staticmethod
    def _sanitize_profile_name(name: str) -> str:
        n = (name or "").strip()
        n = re.sub(r"[\r\n\t]+", " ", n)
        n = re.sub(r'[\\/:*?"<>|]+', "_", n)
        return n[:64] if n else ""

    @staticmethod
    def _keyring_slot(profile: str, kind: str) -> str:
        """kind: api_key | vision_api_key"""
        safe = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", (profile or _DEFAULT_PROFILE).strip())
        return f"profile:{safe}:{kind}"

    def _read_settings_file(self) -> dict:
        import json
        if not self._settings_path.exists():
            return {}
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_settings_file(self, data: dict) -> None:
        import json
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _migrate_settings_to_profiles(self, data: dict) -> dict:
        """旧版扁平配置 → profiles；「默认」为空草稿，有旧填写则落入默认。"""
        if not isinstance(data, dict):
            data = {}
        profiles = data.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            flat_keys = (
                "provider", "model", "endpoint", "max_tokens",
                "vision_provider", "vision_model", "vision_endpoint", "image_mode",
            )
            payload = {k: data[k] for k in flat_keys if k in data}
            # 无旧填写 → 空「默认」；有填写（未分方案）→ 整份放进「默认」
            if not str(payload.get("provider") or "").strip():
                payload = self._empty_profile_payload()
            else:
                payload.setdefault("max_tokens", self._default_max_tokens())
                payload.setdefault("image_mode", "direct")
            profiles = {_DEFAULT_PROFILE: payload}
            data["profiles"] = profiles
            data["active_profile"] = _DEFAULT_PROFILE
            try:
                import keyring
                for kind in ("api_key", "vision_api_key"):
                    old = keyring.get_password(_KEYRING_SERVICE, kind)
                    slot = self._keyring_slot(_DEFAULT_PROFILE, kind)
                    if old and not keyring.get_password(_KEYRING_SERVICE, slot):
                        keyring.set_password(_KEYRING_SERVICE, slot, old)
            except Exception:
                pass
        # 保证「默认」始终存在
        if _DEFAULT_PROFILE not in profiles:
            profiles[_DEFAULT_PROFILE] = self._empty_profile_payload()
            data["profiles"] = profiles
        if not data.get("active_profile") or data["active_profile"] not in profiles:
            data["active_profile"] = _DEFAULT_PROFILE
        return data

    def _empty_profile_payload(self) -> dict:
        return {
            "provider": "",
            "model": "",
            "endpoint": "",
            "max_tokens": self._default_max_tokens(),
            "vision_provider": "",
            "vision_model": "",
            "vision_endpoint": "",
            "image_mode": "direct",
        }

    def _normalize_profile_payload(self, data: dict | None) -> dict:
        """补齐字段；未选辅助识图时强制清空 vision_*，避免切换方案残留。"""
        base = self._empty_profile_payload()
        src = data if isinstance(data, dict) else {}
        out = dict(base)
        for k in base:
            if k in src and src[k] is not None:
                out[k] = src[k]
        if not str(out.get("vision_provider") or "").strip():
            out["vision_provider"] = ""
            out["vision_model"] = ""
            out["vision_endpoint"] = ""
        out["provider"] = str(out.get("provider") or "").strip()
        out["model"] = str(out.get("model") or "").strip()
        out["endpoint"] = str(out.get("endpoint") or "").strip()
        out["vision_provider"] = str(out.get("vision_provider") or "").strip()
        out["vision_model"] = str(out.get("vision_model") or "").strip()
        out["vision_endpoint"] = str(out.get("vision_endpoint") or "").strip()
        out["image_mode"] = str(out.get("image_mode") or "direct").strip() or "direct"
        try:
            out["max_tokens"] = int(out.get("max_tokens") or self._default_max_tokens())
        except (TypeError, ValueError):
            out["max_tokens"] = self._default_max_tokens()
        return out

    def _sync_flat_from_payload(self, data: dict, payload: dict) -> None:
        """用方案完整字段覆盖顶层扁平项（含空字符串，防止旧辅助配置残留）。"""
        for k in self._empty_profile_payload():
            data[k] = payload.get(k, "")

    def _payload_has_content(self, payload: dict | None = None) -> bool:
        p = payload if payload is not None else self._collect_profile_payload()
        if str(p.get("provider") or "").strip():
            return True
        if str(p.get("model") or "").strip():
            return True
        if str(p.get("vision_provider") or "").strip():
            return True
        if self._api_key.text().strip() or self._vision_api_key.text().strip():
            return True
        return False

    def _profile_field_snapshot(self, payload: dict) -> dict:
        keys = (
            "provider", "model", "endpoint", "max_tokens",
            "vision_provider", "vision_model", "vision_endpoint", "image_mode",
        )
        out = {}
        for k in keys:
            v = payload.get(k, "")
            out[k] = "" if v is None else v
        return out

    def _form_differs_from_saved(self, profile: str) -> bool:
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        saved = self._profile_field_snapshot(
            (data.get("profiles") or {}).get(profile) or {}
        )
        cur = self._profile_field_snapshot(self._collect_profile_payload())
        if saved != cur:
            return True
        # Key：表单有内容且与该方案凭据不同 → 视为未保存
        try:
            import keyring
            for kind, widget in (
                ("api_key", self._api_key),
                ("vision_api_key", self._vision_api_key),
            ):
                typed = widget.text().strip()
                if not typed:
                    continue
                stored = keyring.get_password(
                    _KEYRING_SERVICE, self._keyring_slot(profile, kind),
                ) or ""
                if typed != stored:
                    return True
        except Exception:
            if self._api_key.text().strip() or self._vision_api_key.text().strip():
                return True
        return False

    def _stash_unsaved_to_default(self, *, silent: bool = True) -> None:
        """未显式保存的填写写入「默认」草稿；不切换 active。"""
        if not self._payload_has_content():
            return
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        profiles = data.setdefault("profiles", {})
        payload = self._collect_profile_payload()
        profiles[_DEFAULT_PROFILE] = payload
        data["profiles"] = profiles
        self._write_settings_file(data)
        self._store_profile_keys(_DEFAULT_PROFILE, payload=payload)
        if not silent:
            print("[ScriptGenerator] 未保存内容已写入「默认」")
            if hasattr(self, "_test_status"):
                self._test_status.setText("未保存内容已写入方案「默认」")
                self._test_status.setStyleSheet("color:#e8a000;font-size:11px;")

    def _collect_profile_payload(self) -> dict:
        return self._normalize_profile_payload({
            "provider": self._current_provider(),
            "model": self._model.currentText().strip(),
            "endpoint": self._endpoint.text().strip(),
            "max_tokens": int(self._max_tokens.value()),
            "vision_provider": self._current_vision_provider(),
            "vision_model": self._vision_model.currentText().strip(),
            "vision_endpoint": self._vision_endpoint.text().strip(),
            "image_mode": self._image_mode.currentData() or "direct",
        })

    def _current_profile_name(self) -> str:
        name = ""
        if hasattr(self, "_profile_combo") and self._profile_combo.count():
            name = self._profile_combo.currentText().strip()
        if not name:
            data = self._migrate_settings_to_profiles(self._read_settings_file())
            name = str(data.get("active_profile") or _DEFAULT_PROFILE)
        return self._sanitize_profile_name(name) or _DEFAULT_PROFILE

    def _refresh_profile_combo(self, active: str | None = None) -> None:
        if not hasattr(self, "_profile_combo"):
            return
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        profiles = data.get("profiles") or {}
        active = self._sanitize_profile_name(active or data.get("active_profile") or "") or _DEFAULT_PROFILE
        if active not in profiles and profiles:
            active = next(iter(profiles.keys()))
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for name in sorted(profiles.keys(), key=lambda s: (s != _DEFAULT_PROFILE, s)):
            self._profile_combo.addItem(name)
        idx = self._profile_combo.findText(active)
        if idx < 0 and self._profile_combo.count():
            idx = 0
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.blockSignals(False)

    def _store_profile_keys(self, profile: str, *, payload: dict | None = None) -> None:
        try:
            import keyring
        except ImportError:
            print("[ScriptGenerator] 未安装 keyring，API key 不会持久化。安装: pip install keyring")
            return
        api_key = self._api_key.text().strip()
        if api_key:
            try:
                keyring.set_password(
                    _KEYRING_SERVICE, self._keyring_slot(profile, "api_key"), api_key,
                )
            except Exception as e:
                print(f"[ScriptGenerator] keyring 存储失败: {e}")
        # 未配置辅助识图：删掉该方案下的 vision key，避免切换回来又带上
        use_vision = bool(
            str((payload or {}).get("vision_provider") or self._current_vision_provider()).strip()
        )
        vision_key = self._vision_api_key.text().strip()
        if use_vision and vision_key:
            try:
                keyring.set_password(
                    _KEYRING_SERVICE,
                    self._keyring_slot(profile, "vision_api_key"),
                    vision_key,
                )
            except Exception as e:
                print(f"[ScriptGenerator] vision keyring 存储失败: {e}")
        elif not use_vision:
            try:
                keyring.delete_password(
                    _KEYRING_SERVICE, self._keyring_slot(profile, "vision_api_key"),
                )
            except Exception:
                pass

    def _load_profile_keys(self, profile: str, *, payload: dict | None = None) -> None:
        self._api_key.clear()
        self._vision_api_key.clear()
        data = payload
        if data is None:
            file_data = self._migrate_settings_to_profiles(self._read_settings_file())
            data = (file_data.get("profiles") or {}).get(profile) or {}
        use_vision = bool(str(data.get("vision_provider") or "").strip())
        try:
            import keyring
            api_key = keyring.get_password(
                _KEYRING_SERVICE, self._keyring_slot(profile, "api_key"),
            )
            if not api_key and profile == _DEFAULT_PROFILE:
                api_key = keyring.get_password(_KEYRING_SERVICE, "api_key")
            if api_key:
                self._api_key.setText(api_key)
            if not use_vision:
                return
            vision_key = keyring.get_password(
                _KEYRING_SERVICE, self._keyring_slot(profile, "vision_api_key"),
            )
            if not vision_key and profile == _DEFAULT_PROFILE:
                vision_key = keyring.get_password(_KEYRING_SERVICE, "vision_api_key")
            if vision_key:
                self._vision_api_key.setText(vision_key)
        except ImportError:
            pass
        except Exception as e:
            print(f"[ScriptGenerator] keyring 读取失败: {e}")

    def _delete_profile_keys(self, profile: str) -> None:
        try:
            import keyring
            for kind in ("api_key", "vision_api_key"):
                try:
                    keyring.delete_password(
                        _KEYRING_SERVICE, self._keyring_slot(profile, kind),
                    )
                except Exception:
                    pass
        except ImportError:
            pass

    def _apply_profile_payload(self, data: dict) -> None:
        """把方案字段填进控件（不含 Key）。空方案不强制填提供商。"""
        data = self._normalize_profile_payload(data)
        self._provider.blockSignals(True)
        self._model.blockSignals(True)
        self._vision_provider.blockSignals(True)
        self._vision_model.blockSignals(True)

        provider = str(data.get("provider") or "").strip()
        if provider:
            self._set_combo_provider(self._provider, provider)
            self._refresh_models()
        else:
            blank = self._provider.findData("")
            if blank >= 0:
                self._provider.setCurrentIndex(blank)
            self._model.clear()
            self._model.setEditText("")
            self._endpoint.clear()

        model = data.get("model", "")
        if model:
            self._model.setCurrentText(model)
        elif not provider:
            self._model.setEditText("")

        self._endpoint.setText(data.get("endpoint", "") or "")

        max_tokens = data.get("max_tokens")
        if max_tokens is not None:
            try:
                self._max_tokens.setValue(int(max_tokens))
            except (TypeError, ValueError):
                pass
        elif not provider:
            self._max_tokens.setValue(self._default_max_tokens())

        v_provider = str(data.get("vision_provider") or "").strip()
        if v_provider:
            self._set_combo_provider(self._vision_provider, v_provider)
            self._refresh_vision_models()
            v_model = data.get("vision_model", "")
            if v_model:
                self._vision_model.setCurrentText(v_model)
            self._vision_endpoint.setText(data.get("vision_endpoint", "") or "")
        else:
            blank = self._vision_provider.findData("")
            if blank >= 0:
                self._vision_provider.setCurrentIndex(blank)
            self._vision_model.clear()
            self._vision_model.setEditText("")
            self._vision_endpoint.clear()

        mode = data.get("image_mode", "direct")
        mode_idx = self._image_mode.findData(mode)
        if mode_idx >= 0:
            self._image_mode.setCurrentIndex(mode_idx)

        self._on_image_mode_changed()
        self._provider.blockSignals(False)
        self._model.blockSignals(False)
        self._vision_provider.blockSignals(False)
        self._vision_model.blockSignals(False)
        self._on_send_img_toggled(self._send_img_cb.isChecked())
        self._apply_provider_hint()

    def _persist_profile(self, profile: str, *, silent: bool = False) -> None:
        profile = self._sanitize_profile_name(profile) or _DEFAULT_PROFILE
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        profiles = data.setdefault("profiles", {})
        payload = self._collect_profile_payload()
        profiles[profile] = payload
        data["active_profile"] = profile
        data["profiles"] = profiles
        self._sync_flat_from_payload(data, payload)
        self._write_settings_file(data)
        self._store_profile_keys(profile, payload=payload)
        if not silent:
            print(f"[ScriptGenerator] 方案已保存: {profile}")
            if hasattr(self, "_test_status"):
                self._test_status.setText(f"已保存方案「{profile}」")
                self._test_status.setStyleSheet("color:#2e7d32;font-size:11px;")

    def _save_settings(self):
        name = self._current_profile_name()
        self._persist_profile(name)
        self._refresh_profile_combo(name)

    def _load_settings(self):
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        # 规范化各方案并写回，清掉「无辅助却残留 vision_*」的脏数据
        profiles = data.get("profiles") or {}
        cleaned = {
            name: self._normalize_profile_payload(p)
            for name, p in profiles.items()
        }
        data["profiles"] = cleaned
        active = str(data.get("active_profile") or _DEFAULT_PROFILE)
        if active not in cleaned:
            active = _DEFAULT_PROFILE if _DEFAULT_PROFILE in cleaned else next(iter(cleaned))
            data["active_profile"] = active
        payload = cleaned.get(active) or self._empty_profile_payload()
        self._sync_flat_from_payload(data, payload)
        self._write_settings_file(data)

        self._refresh_profile_combo(active)
        self._apply_profile_payload(payload)
        self._load_profile_keys(active, payload=payload)

    def _reload_profile_from_disk(self, name: str, *, status: str = "") -> None:
        """从磁盘加载方案到表单（丢弃未保存的表单修改）。"""
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        profiles = data.get("profiles") or {}
        if name not in profiles:
            return
        self._profile_switching = True
        try:
            payload = self._normalize_profile_payload(profiles[name])
            profiles[name] = payload
            data["profiles"] = profiles
            data["active_profile"] = name
            self._sync_flat_from_payload(data, payload)
            self._write_settings_file(data)
            self._apply_profile_payload(payload)
            self._load_profile_keys(name, payload=payload)
            msg = status or f"已加载方案「{name}」"
            if hasattr(self, "_test_status"):
                self._test_status.setText(msg)
                self._test_status.setStyleSheet("color:#1565c0;font-size:11px;")
            print(f"[ScriptGenerator] {msg}")
        finally:
            self._profile_switching = False

    def _on_profile_activated(self, index: int = 0):
        """下拉选中方案（含再次选中当前项）。"""
        if getattr(self, "_profile_switching", False):
            return
        if not hasattr(self, "_profile_combo"):
            return
        if index < 0 or index >= self._profile_combo.count():
            return
        new_name = self._profile_combo.itemText(index).strip()
        if not new_name:
            return
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        old = str(data.get("active_profile") or "")

        # 再次选中当前方案 → 恢复磁盘上的已保存配置（不把未保存改动写回该方案）
        if new_name == old:
            if self._form_differs_from_saved(old):
                # 未保存内容进「默认」草稿，再重载当前方案
                if old != _DEFAULT_PROFILE:
                    self._stash_unsaved_to_default(silent=True)
                self._reload_profile_from_disk(
                    new_name,
                    status=f"已恢复方案「{new_name}」的已保存配置（未保存修改已写入「默认」）"
                    if old != _DEFAULT_PROFILE
                    else f"已恢复方案「{new_name}」的已保存配置",
                )
            return

        # 切换到其他方案：未保存 →「默认」；正在「默认」上则更新默认
        if old == _DEFAULT_PROFILE:
            if self._payload_has_content():
                self._persist_profile(_DEFAULT_PROFILE, silent=True)
        elif old and self._form_differs_from_saved(old):
            self._stash_unsaved_to_default(silent=True)
        self._reload_profile_from_disk(
            new_name, status=f"已切换到方案「{new_name}」",
        )

    def hideEvent(self, event):
        # 关闭/隐藏面板时：未保存填写落到「默认」（不覆盖命名方案）
        try:
            if not getattr(self, "_profile_switching", False):
                active = self._current_profile_name()
                if active == _DEFAULT_PROFILE:
                    if self._payload_has_content():
                        self._persist_profile(_DEFAULT_PROFILE, silent=True)
                elif self._form_differs_from_saved(active):
                    self._stash_unsaved_to_default(silent=True)
        except Exception as e:
            print(f"[ScriptGenerator] 隐藏时写入默认草稿失败: {e}")
        super().hideEvent(event)

    def _save_profile_as(self):
        cur = self._current_profile_name()
        name, ok = QInputDialog.getText(
            self,
            "另存为配置方案",
            "新方案名称（例：DeepSeek+千问）:",
            text=f"{cur}-副本" if cur else "新方案",
        )
        if not ok:
            return
        name = self._sanitize_profile_name(name)
        if not name:
            QMessageBox.warning(self, "名称无效", "请输入有效的方案名称。")
            return
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        if name in (data.get("profiles") or {}):
            ret = QMessageBox.question(
                self,
                "覆盖方案",
                f"方案「{name}」已存在，是否覆盖？",
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self._persist_profile(name)
        self._refresh_profile_combo(name)

    def _delete_current_profile(self):
        name = self._current_profile_name()
        data = self._migrate_settings_to_profiles(self._read_settings_file())
        profiles = data.get("profiles") or {}
        if len(profiles) <= 1:
            QMessageBox.information(self, "无法删除", "至少保留一套配置方案。")
            return
        ret = QMessageBox.question(
            self,
            "删除方案",
            f"确定删除方案「{name}」？对应 Key 也会从凭据里移除。",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        profiles.pop(name, None)
        self._delete_profile_keys(name)
        next_name = next(iter(profiles.keys()))
        data["profiles"] = profiles
        data["active_profile"] = next_name
        data.update(profiles[next_name])
        self._write_settings_file(data)
        self._refresh_profile_combo(next_name)
        self._apply_profile_payload(profiles[next_name])
        self._load_profile_keys(next_name)

    # ── 提供商切换 ──

    @staticmethod
    def _load_providers_config() -> dict:
        """从 config.json 加载提供商配置"""
        import json
        path = Path(__file__).parent.parent.parent / "backend" / "script_generator" / "config.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            return cfg.get("providers", {})
        except Exception as e:
            print(f"[ScriptGenerator] 加载配置失败: {e}")
            return {"claude": {"models": ["claude-sonnet-5"], "default_endpoint": "", "hint": ""}}

    def _fill_provider_combo(self, combo: QComboBox, vision_only: bool = False):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("（未选择）", "")
        for key, info in (self._providers_config or {}).items():
            info = info or {}
            if vision_only and not info.get("supports_images", True):
                continue
            label = info.get("label") or key
            combo.addItem(str(label), key)
        combo.blockSignals(False)

    def _apply_provider_hint(self):
        info = self._providers_config.get(self._current_provider(), {})
        hint = info.get("hint", "")
        if hint:
            self._provider_hint.setText(hint)
            self._provider_hint.setVisible(True)
        else:
            self._provider_hint.setVisible(False)

    @staticmethod
    def _combo_provider_id(combo: QComboBox) -> str:
        data = combo.currentData()
        if isinstance(data, str) and data:
            return data
        return (combo.currentText() or "").strip()

    @staticmethod
    def _set_combo_provider(combo: QComboBox, provider_id: str) -> bool:
        if not provider_id:
            return False
        idx = combo.findData(provider_id)
        if idx < 0:
            idx = combo.findText(provider_id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
            return True
        return False

    def _current_provider(self) -> str:
        return self._combo_provider_id(self._provider)

    def _current_vision_provider(self) -> str:
        return self._combo_provider_id(self._vision_provider)

    def _refresh_models(self):
        p = self._current_provider()
        print(f"[ScriptGenerator] 切换提供商: {p}")
        self._model.clear()
        self._model.setEditText("")
        info = self._providers_config.get(p, {})
        models = info.get("models", [])
        if models:
            self._model.addItems(models)
        self._endpoint.setText(info.get("default_endpoint", "") or "")
        if self._model.count():
            self._model.setCurrentIndex(0)

    def _refresh_vision_models(self):
        self._vision_model.clear()
        self._vision_model.setEditText("")
        p = self._current_vision_provider()
        info = self._providers_config.get(p, {})
        models = info.get("models", [])
        if models:
            self._vision_model.addItems(models)
        self._vision_endpoint.setText(info.get("default_endpoint", "") or "")
        if self._vision_model.count():
            self._vision_model.setCurrentIndex(0)

    def _on_provider_changed(self, _index: int = 0):
        provider = self._current_provider()
        print(f"[ScriptGenerator] 提供商变更为: {provider}")
        self._apply_provider_hint()
        try:
            self._refresh_models()
        except Exception as e:
            print(f"[ScriptGenerator] 刷新模型列表失败: {e}")
            import traceback
            traceback.print_exc()
        try:
            # 自动草稿：当前是「默认」则写入默认；否则未保存改动进「默认」
            active = self._current_profile_name()
            if active == _DEFAULT_PROFILE:
                self._persist_profile(_DEFAULT_PROFILE, silent=True)
            else:
                self._stash_unsaved_to_default(silent=True)
        except Exception as e:
            print(f"[ScriptGenerator] 保存配置失败: {e}")

    def _on_vision_provider_changed(self, _index: int = 0):
        provider = self._current_vision_provider()
        print(f"[ScriptGenerator] 识图提供商变更为: {provider}")
        try:
            self._refresh_vision_models()
        except Exception as e:
            print(f"[ScriptGenerator] 刷新识图模型列表失败: {e}")
        try:
            active = self._current_profile_name()
            if active == _DEFAULT_PROFILE:
                self._persist_profile(_DEFAULT_PROFILE, silent=True)
            else:
                self._stash_unsaved_to_default(silent=True)
        except Exception as e:
            print(f"[ScriptGenerator] 保存配置失败: {e}")

    def _on_send_img_toggled(self, on: bool):
        self._compress_img_cb.setEnabled(on)
        self._image_mode.setEnabled(on)
        self._on_image_mode_changed()

    def _on_image_mode_changed(self, _index: int = 0) -> None:
        assist = (
            self._send_img_cb.isChecked()
            and (self._image_mode.currentData() or "direct") == "assist"
        )
        if hasattr(self, "_vision_refresh_cb"):
            self._vision_refresh_cb.setEnabled(assist)
            if not assist:
                self._vision_refresh_cb.setChecked(False)
        if hasattr(self, "_clear_vision_cache_btn"):
            self._clear_vision_cache_btn.setEnabled(bool(self._source_dir))

    def _on_clear_vision_cache(self) -> None:
        if not self._source_dir:
            QMessageBox.warning(
                self,
                "未选择文件夹",
                "请先选择图片文件夹，再清除识图缓存。",
            )
            return
        from backend.script_generator.vision_cache import clear_vision_cache

        removed = clear_vision_cache(self._source_dir, include_catalog_txt=True)
        if removed:
            QMessageBox.information(
                self,
                "已清除",
                f"已删除：{', '.join(removed)}\n\n"
                f"目录：{self._source_dir}",
            )
        else:
            QMessageBox.information(
                self,
                "无缓存",
                "当前文件夹下没有 .vision_cache 或识图目录.txt。",
            )

    # ── 连接测试 ──

    def _on_test_connection(self):
        api_key = self._api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key")
            return
        model = self._model.currentText().strip()
        if not model:
            QMessageBox.warning(self, "缺少模型", "请先选择或填写模型名")
            return
        if self._test_worker and self._test_worker.isRunning():
            return

        params = {
            "provider": self._current_provider(),
            "api_key": api_key,
            "model": model,
            "api_endpoint": self._endpoint.text().strip() or None,
            "max_tokens": 256 if int(self._max_tokens.value()) == 0 else min(256, int(self._max_tokens.value())),
        }
        self._test_btn.setEnabled(False)
        self._test_status.setStyleSheet("color:#888;font-size:11px;")
        self._test_status.setText(
            f"测试中… {params['provider']} / {params['model']}"
        )
        # 勿把上次生成失败的顶栏状态误当成当前连接结果
        if hasattr(self, "_set_agent_status_text"):
            self._set_agent_status_text(f"连接测试中 · {params['model']}")

        self._test_worker = ConnectionTestWorker(params)
        self._test_worker.finished.connect(self._on_test_finished)
        self._test_worker.start()

    def _on_test_vision_connection(self):
        api_key = self._vision_api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "缺少识图 API Key", "请先填写辅助识图的 API Key")
            return
        model = self._vision_model.currentText().strip()
        if not model:
            QMessageBox.warning(self, "缺少识图模型", "请先选择或填写识图模型名")
            return
        if self._vision_test_worker and self._vision_test_worker.isRunning():
            return

        params = {
            "provider": self._current_vision_provider(),
            "api_key": api_key,
            "model": model,
            "api_endpoint": self._vision_endpoint.text().strip() or None,
            "max_tokens": 256,
        }
        self._vision_test_btn.setEnabled(False)
        self._vision_test_status.setStyleSheet("color:#888;font-size:11px;")
        self._vision_test_status.setText(
            f"测试中… {params['provider']} / {params['model']}"
        )
        self._vision_test_worker = ConnectionTestWorker(params)
        self._vision_test_worker.finished.connect(self._on_vision_test_finished)
        self._vision_test_worker.start()

    def _on_vision_test_finished(self, result: dict):
        self._vision_test_btn.setEnabled(True)
        provider = self._current_vision_provider()
        model = self._vision_model.currentText().strip()
        if result.get("ok"):
            self._vision_test_status.setStyleSheet("color:#2e9e5b;font-size:11px;")
            self._vision_test_status.setText(
                f"连接成功 · {provider}/{model} · {result.get('latency_ms', 0)} ms"
            )
        else:
            err = (result.get("error") or "未知错误").strip()
            explained = self._translate_error(err)
            first_line = explained.splitlines()[0] if explained else "连接失败"
            self._vision_test_status.setStyleSheet("color:#d64545;font-size:11px;")
            self._vision_test_status.setText(
                f"连接失败 · {provider}/{model} · {first_line}"
            )
            QMessageBox.warning(self, "识图连接测试失败", explained)
    def _on_test_finished(self, result: dict):
        self._test_btn.setEnabled(True)
        provider = self._current_provider()
        model = self._model.currentText().strip()
        if result.get("ok"):
            reply = (result.get("reply") or "").replace("\n", " ")
            if len(reply) > 60:
                reply = reply[:57] + "..."
            tok = ""
            inp, out = result.get("input_tokens") or 0, result.get("output_tokens") or 0
            if inp or out:
                tok = f" · {inp} 入 / {out} 出"
            self._test_status.setStyleSheet("color:#2e9e5b;font-size:11px;")
            self._test_status.setText(
                f"连接成功 · {provider}/{model} · {result.get('latency_ms', 0)} ms"
                f"{tok} · 回复: {reply}"
            )
            if hasattr(self, "_set_agent_idle"):
                self._set_agent_idle(f"连接正常 · {model}")
            QMessageBox.information(
                self,
                "连接测试成功",
                f"已成功连上 AI 服务。\n\n"
                f"提供商：{provider}\n"
                f"模型：{model}\n"
                f"耗时：{result.get('latency_ms', 0)} 毫秒\n"
                f"回复：{reply or '(无)'}\n\n"
                f"说明：这只表示账号和网络可用，还不等于脚本一定能生成成功。",
            )
        else:
            err = (result.get("error") or "未知错误").strip()
            latency = result.get("latency_ms") or 0
            explained = self._translate_error(err)
            self._test_status.setStyleSheet("color:#d64545;font-size:11px;")
            # 状态行只放中文摘要第一行
            first_line = explained.splitlines()[0] if explained else "连接失败"
            self._test_status.setText(
                f"连接失败 · {provider}/{model}"
                + (f" · {latency} ms" if latency else "")
                + f" · {first_line}"
            )
            detail = (
                f"{explained}\n\n"
                f"────────\n"
                f"提供商：{provider}\n"
                f"模型：{model}\n"
                f"耗时：{latency} 毫秒"
            )
            QMessageBox.warning(self, "连接测试失败", detail)

    # ── 脚本描述 ──

    def _load_explanation(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="pick_file", root_path=str(IMG_PATH))
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        try:
            path = Path(dlg.selected_path)
            text = path.read_text(encoding="utf-8")
            self._expl_loading = True
            self._expl_path = path
            self._explanation.setPlainText(text)
            self._explanation_text = text
            self._update_expl_path_label()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))
        finally:
            self._expl_loading = False

    # ── 图片管理 ──

    def _add_images(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="pick_file", root_path=str(IMG_PATH), multi_select=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        for p in dlg.selected_paths:
            self._append_image(Path(p))

    def _add_folder(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="folders", root_path=str(IMG_PATH))
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        root = Path(dlg.selected_path)
        # 脚本生成素材默认含全部子目录（与白名单 / 识图一致）
        recursive = True if dlg.recursive is None else bool(dlg.recursive)
        self._source_dir = root
        self._maybe_bind_expl_file(root)
        self._on_image_mode_changed()
        if self._explanation.toPlainText().strip():
            try:
                self._persist_explanation()
            except Exception as e:
                print(f"[ScriptGenerator] 绑定介绍后保存失败: {e}")

        self._image_entries.clear()
        it = root.rglob("*") if recursive else root.iterdir()
        count = 0
        for f in sorted(it, key=lambda p: str(p).lower()):
            if not f.is_file() or f.suffix.lower() not in self.IMG_EXTENSIONS:
                continue
            try:
                parts = f.relative_to(root).parts
            except ValueError:
                parts = f.parts
            if any(part.startswith(".") for part in parts):
                continue
                self._append_image(f)
                count += 1
        if count == 0:
            QMessageBox.information(
                self,
                "无图片",
                f"文件夹内{'（含子文件夹）' if recursive else ''}未找到图片文件",
            )
        else:
            self._update_img_label()

    def _append_image(self, path: Path):
        for e in self._image_entries:
            if e["path"] == path:
                return
        self._image_entries.append({"path": path, "desc": ""})
        self._update_img_label()

    def _update_img_label(self):
        n = len(self._image_entries)
        if n == 0:
            self._img_label.setText("未选择图片文件夹")
        else:
            src = f"  📁 {self._source_dir}" if self._source_dir else ""
            self._img_label.setText(f"已导入 {n} 张图片{src}")

    def _clear_images(self):
        self._image_entries.clear()
        self._update_img_label()

    def _preview_all(self):
        if not self._image_entries:
            QMessageBox.information(self, "无图片", "没有可预览的图片")
            return
        paths = [e["path"] for e in self._image_entries]
        dlg = ImagePreviewDialog(paths, self)
        dlg.exec()

    # ── 输出 ──

    def _browse_output(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="folders", root_path=str(SCRIPTS_PATH))
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_path:
            self._output_dir.setText(dlg.selected_path)

    # ── 生成 ──

    def _on_generate(self):
        api_key = self._api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key")
            return
        self._gen_ctx = {}  # 新一轮生成，丢弃上一轮上下文

        expl_text = self._explanation.toPlainText().strip()
        if not expl_text:
            QMessageBox.warning(self, "缺少说明", "请填写脚本解释或描述")
            return

        if not self._source_dir:
            QMessageBox.warning(
                self,
                "缺少图片文件夹",
                "请先点「选择图片文件夹」。\n\n"
                "否则生成的脚本会落到默认路径 assets/images/game/script，"
                "运行时会出现「图片不存在」。",
            )
            return
        # 生成前按素材夹递归重扫，确保识图/白名单含子目录图
        try:
            from backend.script_generator.agent import list_source_image_paths
            disk = list_source_image_paths(str(self._source_dir))
            if disk:
                self._image_entries = [{"path": p, "desc": ""} for p in disk]
                self._update_img_label()
        except Exception as e:
            print(f"[ScriptGenerator] 递归扫描素材失败: {e}")
        if not self._image_entries:
            QMessageBox.warning(
                self,
                "文件夹内无图片",
                "所选文件夹里没有可用图片，请换一个包含 .png 等素材的目录。",
            )
            return

        try:
            self._persist_explanation(expl_text)
        except Exception as e:
            print(f"[ScriptGenerator] 生成前保存介绍失败: {e}")

        params = {
            "provider": self._current_provider(),
            "api_key": api_key,
            "model": self._model.currentText().strip(),
            "api_endpoint": self._endpoint.text().strip() or None,
            "explanation_text": expl_text,
            "image_paths": [e["path"] for e in self._image_entries],
            "source_dir": str(self._source_dir) if self._source_dir else "",
            "send_images": self._send_img_cb.isChecked(),
            "compress_images": self._compress_img_cb.isChecked(),
            "max_tokens": int(self._max_tokens.value()),
            "free_mode": bool(self._free_mode_cb.isChecked()),
        }
        if (
            self._send_img_cb.isChecked()
            and (self._image_mode.currentData() or "direct") == "assist"
        ):
            v_key = self._vision_api_key.text().strip()
            v_model = self._vision_model.currentText().strip()
            if not v_key or not v_model:
                QMessageBox.warning(
                    self,
                    "辅助识图未配置",
                    "已选择「辅助识图」，请先在「API 配置」页填写识图提供商 / Key / 模型，并保存。",
                )
                return
            params["vision_assist"] = {
                "provider": self._current_vision_provider(),
                "api_key": v_key,
                "model": v_model,
                "api_endpoint": self._vision_endpoint.text().strip() or None,
                "compress_images": self._compress_img_cb.isChecked(),
                "refresh_vision": self._vision_refresh_cb.isChecked(),
            }
        try:
            self._save_settings()
        except Exception:
            pass

        self._generate_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._holding_prev_live_code = False
        self._set_agent_busy("生成", "开始生成…", indeterminate=True)
        self._token_label.setText("")
        self._stream_buf = ""
        self._set_live_code("")
        self._view_code_btn.setEnabled(False)
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._archive.begin("generate")
        self._trajectory.begin_run("开始生成", fresh=True)
        self._last_revise_summary = ""
        self._last_diagnosis_json = ""
        self._chat_session = None
        self._stop_frame_path = ""
        self._last_trial_frame = None

        self._worker = GenerateWorker(params)
        self._worker.finished.connect(self._on_success)
        self._worker.partial.connect(self._on_partial)
        self._worker.status.connect(self._on_status)
        self._worker.artifact.connect(self._on_artifact)
        self._worker.token_info.connect(self._on_token_info)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_token_info(self, inp: int, out: int):
        if inp or out:
            self._last_inp_tokens = int(inp or 0)
            self._last_out_tokens = int(out or 0)
            self._token_label.setText(f"↕ {inp} 入 / {out} 出")
            if hasattr(self, "_agent_status") and self._agent_busy:
                # 保持相位文案，仅刷新 token（已在 label）
                pass

    def _on_artifact(self, kind: str, payload: str):
        if kind == "gen_ctx":
            # 生成管线的校验上下文（plan/free/source_dir）：归档与试运行沿用同一套
            try:
                import json as _json

                data = _json.loads(payload or "{}")
                if isinstance(data, dict):
                    self._gen_ctx = data
            except Exception as e:
                print(f"[ScriptGenerator] gen_ctx 解析失败: {e}")
            return
        if kind == "plan":
            # 计划正文并入轨迹「规划」步骤，不再单独占一块面板
            text = (payload or "").strip()
            self._trajectory.update_step(
                "plan",
                "Plan",
                "规划完成",
                status="done",
                body=text,
            )
        elif kind in ("explain_norm_meta", "explain_normalized"):
            text = (payload or "").strip()
            if text and getattr(self, "_archive", None) is not None:
                try:
                    name = (
                        "explain_norm_meta.json"
                        if kind == "explain_norm_meta"
                        else "explain_normalized.txt"
                    )
                    self._archive.write_text(name, text)
                except Exception as e:
                    print(f"[ScriptGenerator] 归档 {kind} 失败: {e}")
            if kind == "explain_norm_meta" and text:
                self._trajectory.update_step(
                    "explain_norm",
                    "Norm",
                    "介绍规范化",
                    status="done",
                    body=text,
                )
        elif kind == "chat_session":
            text = (payload or "").strip()
            if text:
                try:
                    from backend.script_generator.chat_session import loads_session
                    sess = loads_session(text)
                    if sess:
                        self._chat_session = sess
                        n = len(sess.get("messages") or [])
                        self._trajectory.update_step(
                            "chat_session",
                            "Chat",
                            "同会话已保存",
                            status="done",
                            body=f"messages={n}（修订将续写此对话）",
                        )
                except Exception as e:
                    print(f"[ScriptGenerator] chat_session 解析失败: {e}")
                if getattr(self, "_archive", None) is not None:
                    try:
                        self._archive.write_text("chat_session.json", text)
                    except Exception as e:
                        print(f"[ScriptGenerator] 归档 chat_session 失败: {e}")
        elif kind == "stage":
            self._apply_stage_artifact(payload or "")
        elif kind == "diagnosis":
            text = (payload or "").strip()
            if text:
                self._last_diagnosis_json = text
            if text and getattr(self, "_archive", None) is not None:
                try:
                    self._archive.write_text("diagnosis.json", text)
                except Exception as e:
                    print(f"[ScriptGenerator] 归档 diagnosis 失败: {e}")
            if text:
                self._trajectory.update_step(
                    "diagnose",
                    "Think",
                    "试跑诊断",
                    status="done",
                    body=text,
                )

    def _apply_stage_artifact(self, payload: str):
        """解析 stage 载荷: key|status|title|body"""
        parts = (payload or "").split("|", 3)
        if len(parts) < 3:
            return
        key, status, title = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        prefix = key.split("_", 1)[0]
        kind_map = {
            "plan": "Plan",
            "generate": "Generate",
            "task": "Task",
            "merge": "Merge",
            "validate": "Validate",
            "fix": "Fix",
            "revise": "Revise",
            "review": "Review",
            "optimize": "Optimize",
            "diagnose": "Think",
            "vision_decide": "Think",
            "stop_vision": "Vision",
            "think": "Think",
            "vision": "Vision",
        }
        kind = kind_map.get(prefix, "Info")
        self._trajectory.update_step(
            key, kind, title, status=status, body=body,
        )
        # 顶栏进度
        pct = _STAGE_PROGRESS.get(prefix)
        if pct is not None and status == "running":
            self._set_agent_progress(pct)
        if title:
            self._set_agent_status_text(title)

    def _on_status(self, msg: str):
        if hasattr(self, "_trajectory"):
            self._trajectory.set_status_hint(msg or "")
        if msg:
            self._set_agent_status_text(msg)

    def _on_partial(self, text: str):
        if not hasattr(self, "_stream_buf"):
            self._stream_buf = ""
        if getattr(self, "_holding_prev_live_code", False):
            # 开始真正输出时再替换「上一版」，避免长时间空白
            self._holding_prev_live_code = False
            self._stream_buf = ""
        self._stream_buf += text or ""
        # 流式过程中也可打开查看（看当前缓冲）
        if self._stream_buf.strip():
            self._view_code_btn.setEnabled(True)
            if hasattr(self, "_live_code_hint"):
                n = len(self._stream_buf)
                self._live_code_hint.setText(f"流式写入中 · {n:,} 字符")
        self._schedule_live_code_refresh()

    def _schedule_live_code_refresh(self):
        if not hasattr(self, "_live_code_timer"):
            return
        if not self._live_code_timer.isActive():
            self._live_code_timer.start(90)

    def _flush_live_code(self):
        if not hasattr(self, "_live_code"):
            return
        if getattr(self, "_holding_prev_live_code", False):
            return
        buf = getattr(self, "_stream_buf", "") or ""
        if self._live_code.toPlainText() == buf:
            return
        self._live_code.setPlainText(buf)
        cursor = self._live_code.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._live_code.setTextCursor(cursor)

    def _set_live_code(self, text: str):
        if not hasattr(self, "_live_code"):
            return
        t = text or ""
        self._holding_prev_live_code = False
        self._live_code.setPlainText(t)
        if hasattr(self, "_live_code_hint"):
            if t.strip():
                self._live_code_hint.setText(f"{len(t):,} 字符")
            else:
                self._live_code_hint.setText("流式输出会显示在这里")

    def _keep_prev_live_code(self, *, phase: str) -> None:
        """修订/优化开始时保留上一版代码，避免右侧变黑像卡住。"""
        prev = (self._generated_code or "").strip()
        self._stream_buf = ""
        if not prev:
            self._holding_prev_live_code = False
            self._set_live_code("")
            self._view_code_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            self._copy_btn.setEnabled(False)
            return
        self._holding_prev_live_code = True
        # 不调用 _set_live_code，以免清掉 holding 标记
        if hasattr(self, "_live_code") and not (self._live_code.toPlainText() or "").strip():
            self._live_code.setPlainText(self._generated_code)
        if hasattr(self, "_live_code_hint"):
            label = {"修订": "修订中", "优化": "优化中"}.get(phase, "处理中")
            self._live_code_hint.setText(
                f"{label}… · 暂显上一版（{len(prev):,} 字符），写出后替换"
            )
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)

    def _format_elapsed(self, seconds: float) -> str:
        s = max(0, int(seconds))
        if s < 60:
            return f"已 {s} 秒"
        return f"已 {s // 60} 分 {s % 60:02d} 秒"

    def _tick_agent_elapsed(self):
        if not getattr(self, "_agent_busy", False):
            return
        import time
        started = float(getattr(self, "_agent_busy_started", 0) or 0)
        if started <= 0:
            return
        base = (getattr(self, "_agent_status_base", "") or "").strip() or "进行中"
        elapsed = self._format_elapsed(time.monotonic() - started)
        # 省略号循环：生成中. / .. / ...
        n_dots = (int((time.monotonic() - started) * 2) % 3) + 1
        dots = "." * n_dots
        self._render_agent_status(f"{base}{dots} · {elapsed}")
        banner_base = (getattr(self, "_busy_banner_base", "") or "").strip()
        if banner_base and hasattr(self, "_gen_result_banner"):
            self._gen_result_banner.setText(f"{banner_base}{dots}")

    def _render_agent_status(self, text: str):
        if hasattr(self, "_agent_status"):
            t = (text or "").replace("\n", " ").strip()
            if len(t) > 110:
                t = t[:109] + "…"
            self._agent_status.setText(t or "…")

    def _set_busy_banner(self, text: str):
        if not hasattr(self, "_gen_result_banner"):
            return
        base = (text or "").rstrip(".…· ")
        self._busy_banner_base = base
        self._gen_result_banner.setText(base + "…")
        self._gen_result_banner.setProperty("ok", False)
        self._gen_result_banner.setProperty("busy", True)
        self._gen_result_banner.setProperty("warn", False)
        self._gen_result_banner.style().unpolish(self._gen_result_banner)
        self._gen_result_banner.style().polish(self._gen_result_banner)
        self._gen_result_banner.show()

    def _set_warn_banner(self, text: str):
        if not hasattr(self, "_gen_result_banner"):
            return
        self._busy_banner_base = ""
        self._gen_result_banner.setText(text)
        self._gen_result_banner.setProperty("ok", False)
        self._gen_result_banner.setProperty("busy", False)
        self._gen_result_banner.setProperty("warn", True)
        self._gen_result_banner.style().unpolish(self._gen_result_banner)
        self._gen_result_banner.style().polish(self._gen_result_banner)
        self._gen_result_banner.show()

    def _set_ok_banner(self, text: str):
        if not hasattr(self, "_gen_result_banner"):
            return
        self._busy_banner_base = ""
        self._gen_result_banner.setText(text)
        self._gen_result_banner.setProperty("ok", True)
        self._gen_result_banner.setProperty("busy", False)
        self._gen_result_banner.setProperty("warn", False)
        self._gen_result_banner.style().unpolish(self._gen_result_banner)
        self._gen_result_banner.style().polish(self._gen_result_banner)
        self._gen_result_banner.show()

    def _set_agent_busy(self, phase: str, status: str, *, indeterminate: bool = True):
        import time

        self._agent_busy = True
        self._agent_busy_started = time.monotonic()
        if hasattr(self, "_agent_phase"):
            self._agent_phase.setText(phase or "Agent")
        # 状态正文不加「并非卡住」；省略号由计时器动画补上
        clean = (status or "").strip().rstrip(".…")
        self._set_agent_status_text(clean or f"{phase}中")
        if hasattr(self, "_agent_dots"):
            self._agent_dots.start()
        elif hasattr(self, "_agent_dot") and hasattr(self._agent_dot, "setProperty"):
            self._agent_dot.setProperty("busy", True)
            self._agent_dot.setProperty("success", False)
            self._agent_dot.style().unpolish(self._agent_dot)
            self._agent_dot.style().polish(self._agent_dot)
        busy_tips = {
            "生成": "生成中",
            "修订": "修订中 · 辅助工具处理中，右侧暂显上一版",
            "优化": "优化中 · 分析改写中，右侧暂显上一版",
        }
        self._set_busy_banner(busy_tips.get(phase, f"{phase}中"))
        if hasattr(self, "_to_trial_btn"):
            self._to_trial_btn.setProperty("ready", False)
            self._to_trial_btn.setText("去试运行 →")
            self._to_trial_btn.style().unpolish(self._to_trial_btn)
            self._to_trial_btn.style().polish(self._to_trial_btn)
        if hasattr(self, "_progress"):
            self._progress.show()
            if indeterminate:
                self._progress.setRange(0, 0)
            else:
                self._progress.setRange(0, 100)
                self._progress.setValue(5)
        if hasattr(self, "_agent_elapsed_timer"):
            self._agent_elapsed_timer.start()
            self._tick_agent_elapsed()
        for btn in (getattr(self, "_cancel_btn", None), getattr(self, "_strip_cancel_btn", None)):
            if btn is not None:
                btn.setEnabled(True)

    def _set_agent_idle(self, status: str = "就绪", *, success: bool = False):
        self._agent_busy = False
        self._busy_banner_base = ""
        if hasattr(self, "_agent_elapsed_timer"):
            self._agent_elapsed_timer.stop()
        if hasattr(self, "_agent_phase"):
            self._agent_phase.setText("Agent")
        self._agent_status_base = ""
        self._set_agent_status_text(status)
        if hasattr(self, "_agent_dots"):
            self._agent_dots.stop()
        elif hasattr(self, "_agent_dot") and hasattr(self._agent_dot, "setProperty"):
            self._agent_dot.setProperty("busy", False)
            self._agent_dot.setProperty("success", bool(success))
            self._agent_dot.style().unpolish(self._agent_dot)
            self._agent_dot.style().polish(self._agent_dot)
        if hasattr(self, "_progress"):
            self._progress.hide()
            self._progress.setRange(0, 0)
        # 失败/取消时收起「进行中」蓝条；成功由 _set_ok_banner 覆盖
        if not success and hasattr(self, "_gen_result_banner"):
            busy = self._gen_result_banner.property("busy")
            if busy in (True, "true"):
                self._gen_result_banner.hide()
                self._gen_result_banner.clear()
                self._gen_result_banner.setProperty("busy", False)
        for btn in (getattr(self, "_cancel_btn", None), getattr(self, "_strip_cancel_btn", None)):
            if btn is not None:
                btn.setEnabled(False)

    def _set_agent_status_text(self, text: str):
        t = (text or "").replace("\n", " ").strip()
        if getattr(self, "_agent_busy", False):
            self._agent_status_base = t
            import time
            started = float(getattr(self, "_agent_busy_started", 0) or 0)
            if started > 0:
                elapsed = self._format_elapsed(time.monotonic() - started)
                self._render_agent_status(f"{t} · {elapsed}" if t else elapsed)
                return
        self._render_agent_status(t or "…")

    def _set_agent_progress(self, pct: int):
        if not hasattr(self, "_progress"):
            return
        self._progress.show()
        if self._progress.maximum() == 0:
            self._progress.setRange(0, 100)
        self._progress.setValue(max(0, min(100, int(pct))))

    def _open_session_archive(self):
        d = None
        if getattr(self, "_archive", None) is not None:
            d = self._archive.session_dir
        if d is None or not Path(d).is_dir():
            # 打开 sessions 根目录
            try:
                from backend.script_generator.session_archive import SESSIONS_ROOT
                root = SESSIONS_ROOT
                root.mkdir(parents=True, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))
                QMessageBox.information(
                    self,
                    "会话归档",
                    "当前还没有本次会话目录。\n已打开归档根目录，生成/试跑后会写入子文件夹。",
                )
            except Exception as e:
                QMessageBox.warning(self, "无法打开归档", str(e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _refresh_trial_chip_styles(self):
        chips = getattr(self, "_trial_chips", None) or {}
        for chip in chips.values():
            chip.style().unpolish(chip)
            chip.style().polish(chip)

    def _set_trial_chip(self, key: str, stage: str):
        chips = getattr(self, "_trial_chips", None) or {}
        chip = chips.get(key)
        if chip is None:
            return
        chip.setProperty("stage", stage)
        self._refresh_trial_chip_styles()

    def _update_trial_frame_preview(self):
        lbl = getattr(self, "_trial_frame_lbl", None)
        if lbl is None:
            return
        frame = self._last_trial_frame
        if frame is None:
            lbl.setPixmap(QPixmap())
            lbl.setText("试跑结束或停止后\n显示画面缩略图")
            return
        try:
            import numpy as np
            from PySide6.QtGui import QImage

            arr = np.asarray(frame)
            if arr.ndim != 3 or arr.shape[2] < 3:
                lbl.setText("画面格式无法预览")
                return
            # BGR → RGB
            rgb = arr[:, :, :3][:, :, ::-1].copy()
            h, w, _ = rgb.shape
            qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            pix = QPixmap.fromImage(qimg.copy())
            scaled = pix.scaled(
                lbl.width() or 200,
                lbl.height() or 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            lbl.setText("")
            lbl.setPixmap(scaled)
            self._set_trial_chip("frame", "done")
        except Exception as e:
            lbl.setText(f"预览失败\n{e}")

    def _current_code_text(self) -> str:
        code = (self._generated_code or "").strip()
        if code:
            return self._generated_code
        return getattr(self, "_stream_buf", "") or ""

    def _view_full_code(self):
        code = self._current_code_text()
        if not code.strip():
            QMessageBox.information(self, "暂无代码", "还没有可查看的脚本，请先生成。")
            return
        dlg = FullCodeDialog(self, code=code)
        dlg.exec()

    def _apply_edited_code(self, code: str):
        """从完整代码弹窗写回当前生成结果，并同步试运行文件。"""
        text = code if code.endswith("\n") else (code + "\n" if code else "")
        self._generated_code = text
        self._stream_buf = text
        self._set_live_code(text)
        self._view_code_btn.setEnabled(bool(text.strip()))
        self._save_btn.setEnabled(bool(text.strip()))
        self._copy_btn.setEnabled(bool(text.strip()))
        try:
            if text.strip():
                self._sync_trial_code()
        except Exception as e:
            print(f"[ScriptGenerator] 写回试运行文件失败: {e}")

    def _flash_taskbar(self, _reason: str = ""):
        """完成后提醒：任务栏图标闪烁 / 底部高亮，直到用户点回窗口（不抢焦点）。"""
        win = self.window()
        if win is None:
            return
        app = QApplication.instance()
        if app is not None:
            try:
                app.alert(win, 0)
            except Exception:
                pass
        # Windows 任务栏底部色条（窗口不在前台时持续闪到用户点回来）
        try:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = (
                    ("cbSize", wintypes.UINT),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.DWORD),
                )

            FLASHW_TRAY = 0x00000002
            FLASHW_TIMERNOFG = 0x0000000C
            info = FLASHWINFO()
            info.cbSize = ctypes.sizeof(FLASHWINFO)
            info.hwnd = int(win.winId())
            info.dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG
            info.uCount = 0
            info.dwTimeout = 0
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _reset_trial_surface_after_new_code(self) -> None:
        """重新生成/写入新代码后，清掉上一轮试跑的红条/日志，避免误以为仍失败。"""
        try:
            self._trial_log_lines.clear()
            if hasattr(self, "_trial_log"):
                self._trial_log.clear()
        except Exception:
            pass
        if getattr(self, "_trial_hud", None) is not None:
            try:
                self._trial_hud.reset("")
            except Exception:
                pass
        for key in ("write", "run", "frame", "feas"):
            try:
                self._set_trial_chip(key, "idle")
            except Exception:
                pass
        # 上一轮自动填的「修复本地校验」在本次已通过时清掉，避免误导再修订
        if not self._trial_blocked and hasattr(self, "_feedback"):
            fb = self._feedback.toPlainText().strip()
            if fb.startswith("修复本地校验错误"):
                self._feedback.clear()
                self._set_feedback_stale(False)
        if self._trial_blocked:
            self._append_trial_log(
                f"[脚本检查] 未通过：{self._trial_block_reason or '见上方提示'}"
            )
        else:
            self._append_trial_log("[脚本检查] 通过 · 可点「试运行」")

    def _on_success(self, code: str):
        self._generated_code = code
        self._stream_buf = code or ""
        self._set_live_code(code or "")
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._revise_btn.setEnabled(True)
        self._confirm_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)

        n_chars = len(code or "")
        tok = ""
        if hasattr(self, "_token_label"):
            tok = (self._token_label.text() or "").strip()
        summary_bits = [f"脚本约 {n_chars:,} 字符"]
        if tok and "入" in tok:
            summary_bits.append(tok.replace("↑ ", "").strip())
        src = ""
        if getattr(self, "_source_dir", None):
            src = Path(self._source_dir).name
        if src:
            summary_bits.append(f"素材「{src}」")
        summary = " · ".join(summary_bits)

        self._sync_trial_code()
        self._reset_trial_surface_after_new_code()
        blocked = bool(self._trial_blocked)
        reason = (self._trial_block_reason or "脚本检查未通过").strip()
        if blocked:
            summary += f"\n本地校验未过，暂不可试运行：{reason}"
            self._set_agent_idle(f"已生成 · 校验未过不可试运行", success=False)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(
                    f"已生成 · {n_chars:,} 字符 · 校验未过，请修订或改代码"
                )
            self._set_warn_banner(
                f"生成完成但不可试运行　　{reason}　　可点「根据反馈修订」"
            )
            if hasattr(self, "_to_trial_btn"):
                self._to_trial_btn.setProperty("ready", False)
                self._to_trial_btn.style().unpolish(self._to_trial_btn)
                self._to_trial_btn.style().polish(self._to_trial_btn)
                self._to_trial_btn.setText("去试运行 →（先修校验）")
            # 自动带上校验错误，方便一键修订
            if hasattr(self, "_feedback") and not self._feedback.toPlainText().strip():
                errs = []
                # reason is first error; pull from last validate via re-run is heavy — use reason
                self._feedback.setPlainText(f"修复本地校验错误：{reason}")
            self._trajectory.succeed_run("生成完成（校验未过）", summary=summary)
        else:
            summary += "\n结构校验已通过。建议：保存到文件 → 去试运行验证匹配与流程。"
            self._set_agent_idle("✓ 脚本已就绪 · 可去试运行", success=True)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(f"已生成 · {n_chars:,} 字符 · 可保存或试运行")
            self._set_ok_banner(
                f"✓ 生成成功　　{summary_bits[0]}"
                + "　　下一步：点右下角「去试运行 →」"
            )
            if hasattr(self, "_to_trial_btn"):
                self._to_trial_btn.setProperty("ready", True)
                self._to_trial_btn.style().unpolish(self._to_trial_btn)
                self._to_trial_btn.style().polish(self._to_trial_btn)
                self._to_trial_btn.setText("去试运行 → 验证脚本")
            self._trajectory.succeed_run("生成成功", summary=summary)

        self._update_trial_availability()
        self._flash_taskbar("脚本已生成" if not blocked else "脚本已生成但校验未过")
        self._archive_generate_done(code)
        if getattr(self, "_vision_refresh_cb", None) and self._vision_refresh_cb.isChecked():
            self._vision_refresh_cb.setChecked(False)

    def _on_error(self, msg: str):
        translated = self._translate_error(msg)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        reason = (translated.split("\n", 1)[0] or "生成失败").strip()
        self._set_agent_idle(f"生成失败 · {reason}")
        self._restore_code_preview_after_agent_fail()
        self._update_trial_availability()
        self._trajectory.fail_run(reason)
        # 失败也落盘：便于复盘半截/膨胀代码（此前只归档成功结果）
        try:
            self._archive_generate_failed(translated)
        except Exception as e:
            print(f"[ScriptGenerator] 归档生成失败信息出错: {e}")
        QMessageBox.critical(self, "生成失败", translated)

    def _archive_generate_failed(self, error_msg: str):
        code = (getattr(self, "_stream_buf", "") or self._generated_code or "").strip()
        if self._archive.session_dir is None:
            self._archive.begin("generate", reuse=False)
        if code:
            self._archive.write_text("code_failed.py", code if code.endswith("\n") else code + "\n")
            self._set_live_code(code)
            self._view_code_btn.setEnabled(True)
            self._save_btn.setEnabled(True)
            self._copy_btn.setEnabled(True)
            # 允许用户拿失败稿去试运行页手工修订
            self._generated_code = code if code.endswith("\n") else code + "\n"
        self._archive.write_text("generate_error.txt", error_msg or "")
        self._archive.write_json("trajectory.json", self._archive_trajectory())
        expl = ""
        if hasattr(self, "_explanation"):
            expl = self._explanation.toPlainText()
        if expl.strip():
            self._archive.write_text("explanation.txt", expl)
        self._archive.merge_meta({
            **self._archive_meta(),
            "last_event": "generate_failed",
            "failed_code_chars": len(code),
        })
        self._archive.append_event(
            "generate_failed",
            error=(error_msg or "")[:800],
            code_chars=len(code),
        )
        d = self._archive.session_dir
        if d:
            self._trajectory.update_step(
                "archive",
                "Info",
                "已归档失败稿",
                status="done",
                body=str(d),
            )

    def _archive_meta(self) -> dict:
        return {
            "provider": self._current_provider(),
            "model": self._model.currentText().strip(),
            "source_dir": str(self._source_dir or ""),
            "expl_path": str(self._expl_path or ""),
            "trial_account": self._trial_account_name or "",
            "tokens_in": self._last_inp_tokens,
            "tokens_out": self._last_out_tokens,
        }

    def _archive_trajectory(self) -> list[dict]:
        if hasattr(self, "_trajectory"):
            return self._trajectory.export_snapshot()
        return []

    def _try_cache_trial_frame(self):
        name = self._trial_account_name
        if not name or self._facade is None:
            return
        try:
            ctrl = self._facade.controller
            ub = ctrl._browser_instances.get(name)
            if ub is not None and getattr(ub, "_browser", None) is not None:
                frame = getattr(ub._browser, "_frame", None)
                if frame is not None:
                    self._last_trial_frame = frame.copy()
                    return
        except Exception:
            pass

    def _persist_stop_frame(self, *, name: str = "screenshot_stop.png") -> str:
        """把缓存的停帧写成 PNG，供修订诊断识图。返回路径或空串。"""
        if self._last_trial_frame is None:
            return self._stop_frame_path or ""
        try:
            if self._archive.session_dir is None:
                self._archive.begin("trial", reuse=False)
            p = self._archive.save_bgr_frame(name, self._last_trial_frame)
            if p is not None:
                self._stop_frame_path = str(p)
                return self._stop_frame_path
        except Exception as e:
            print(f"[ScriptGenerator] 持久化停帧失败: {e}")
        # 无归档时落到临时文件
        try:
            import tempfile
            import cv2
            import numpy as np

            fd, tmp = tempfile.mkstemp(prefix="ms_stop_", suffix=".png")
            import os
            os.close(fd)
            arr = np.asarray(self._last_trial_frame)
            if cv2.imencode(".png", arr)[0]:
                cv2.imwrite(tmp, arr)
                self._stop_frame_path = tmp
                return tmp
        except Exception as e:
            print(f"[ScriptGenerator] 临时停帧失败: {e}")
        return self._stop_frame_path or ""

    def _cache_stop_frame_now(self, *, also_capture: bool = True) -> str:
        """停止瞬间：优先拷当前帧，可选再请求一次截图，并落盘。"""
        self._try_cache_trial_frame()
        if also_capture and self._facade and self._trial_account_name:
            try:
                acc = self._selected_account()
                if acc is None or acc.get("name") != self._trial_account_name:
                    acc = {"name": self._trial_account_name}
                self._facade.controller.capture_screenshot(acc)
            except Exception:
                pass
        path = self._persist_stop_frame()
        if path:
            self._append_trial_log(f"[试运行] 已缓存停帧: {path}")
        return path

    def _on_screenshot_ready(self, account_name: str, frame):
        if account_name != self._trial_account_name:
            return
        try:
            self._last_trial_frame = frame.copy() if hasattr(frame, "copy") else frame
        except Exception:
            self._last_trial_frame = frame
        # 试跑中也刷缩略图（弱实时）；结束时再定格
        try:
            self._update_trial_frame_preview()
        except Exception:
            pass
        # 试跑结束/停止后异步截图到达时，补写停帧文件
        if not self._trial_running:
            try:
                self._persist_stop_frame()
            except Exception:
                pass

    def _archive_generate_done(self, code: str):
        try:
            from backend.script_generator.agent import (
                apply_codegen_patches,
                validate_script_local,
            )

            expl = self._explanation.toPlainText()
            ctx = getattr(self, "_gen_ctx", {}) or {}
            plan = ctx.get("plan_struct") or None
            free = ctx.get("free_mode")
            if not isinstance(free, bool):
                free = bool(self._free_mode_cb.isChecked())
            src = str(ctx.get("source_dir") or self._source_dir or "")
            # 与管线同参数再跑一次确定性补丁：legacy/异常路径漏补的在此兜住，
            # 归档错误 = 补丁后的真实残差，不再出现「管线过、UI 拦」。
            patched, notes = apply_codegen_patches(
                code,
                source_dir=src,
                plan=plan,
                explanation=expl,
                free_mode=free,
            )
            if patched.strip() and self._code_compiles(patched):
                if patched != code:
                    self._append_trial_log(
                        f"[归档] 生成后本地补全 {len(notes)} 项（含: "
                        + "; ".join(notes[:3]) + "）"
                    )
                code = patched
                self._generated_code = patched
                self._stream_buf = patched
            errs = validate_script_local(
                code,
                plan=plan,
                explanation=expl,
                source_dir=src,
                free_mode=free,
            )
            if errs:
                self._trial_blocked = True
                self._trial_block_reason = errs[0]
                self._append_trial_log(
                    f"[归档] 仍有 {len(errs)} 项校验残差（可点「重修订」带上错误修复）"
                )
                if not self._feedback.toPlainText().strip():
                    self._feedback.setPlainText(
                        "修复本地校验错误：\n"
                        + "\n".join(f"- {e}" for e in errs[:8])
                    )
            d = self._archive.snapshot_generate(
                explanation=expl,
                code=code,
                trajectory=self._archive_trajectory(),
                meta=self._archive_meta(),
                validation_errors=errs or None,
            )
            self._append_trial_log(f"[归档] 生成材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档生成失败: {e}")

    def _archive_trial_end(self, status: str):
        try:
            if self._archive.session_dir is None:
                self._archive.begin("trial", reuse=False)
            self._try_cache_trial_frame()
            if not self._stop_frame_path:
                self._persist_stop_frame(name="screenshot_trial_end.png")
            acc = self._selected_account()
            if acc and self._facade:
                try:
                    self._facade.controller.capture_screenshot(acc)
                except Exception:
                    pass
            d = self._archive.snapshot_trial_end(
                status=status,
                trial_log="\n".join(self._trial_log_lines),
                code=self._generated_code or "",
                feedback_draft=self._feedback.toPlainText(),
                meta=self._archive_meta(),
                frame=self._last_trial_frame,
            )
            self._append_trial_log(f"[归档] 试运行材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档试跑失败: {e}")

    def _archive_revise_start(self, feedback: str):
        try:
            if self._archive.session_dir is None:
                self._archive.begin("revise")
            self._try_cache_trial_frame()
            d = self._archive.snapshot_revise_start(
                feedback=feedback,
                code_before=self._generated_code or "",
                explanation=self._explanation.toPlainText(),
                trial_log="\n".join(self._trial_log_lines[-200:]),
                trajectory=self._archive_trajectory(),
                meta=self._archive_meta(),
                frame=self._last_trial_frame,
            )
            self._append_trial_log(f"[归档] 修订前材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档修订前失败: {e}")

    def _archive_revise_done(self, code: str, summary: str, writeback: list[str] | None = None):
        try:
            from backend.script_generator.agent import validate_script_local
            expl = self._explanation.toPlainText()
            errs = validate_script_local(
                code,
                explanation=expl,
                source_dir=str(self._source_dir or ""),
                free_mode=bool(self._free_mode_cb.isChecked()),
            )
            d = self._archive.snapshot_revise_done(
                code_after=code,
                summary=summary,
                trajectory=self._archive_trajectory(),
                meta=self._archive_meta(),
                validation_errors=errs or None,
                writeback_bullets=writeback,
            )
            self._append_trial_log(f"[归档] 修订后材料: {d}")
        except Exception as e:
            print(f"[ScriptGenerator] 归档修订后失败: {e}")

    # ── 试运行 / 修订 ──

    def _update_trial_availability(self):
        has_code = bool((self._generated_code or "").strip())
        has_facade = self._facade is not None
        has_feedback = bool(self._feedback.toPlainText().strip())
        revise_busy = self._revise_worker is not None and self._revise_worker.isRunning()
        optimize_busy = self._optimize_worker is not None and self._optimize_worker.isRunning()
        gen_busy = revise_busy or optimize_busy
        if not has_facade:
            self._trial_hint.setText(
                "当前未连接主程序：可生成/保存，但无法试运行。"
                "请从主窗口状态栏打开「脚本生成」。"
            )
        elif self._trial_blocked:
            reason = self._trial_block_reason or "脚本检查未通过"
            self._trial_hint.setText(
                f"脚本检查未通过：{reason}。"
                "请在「查看完整代码」改路径/结构后保存，会自动重新检查。"
            )
        else:
            if not self._trial_hint.text().startswith("当前未连接"):
                self._trial_hint.setText("")
        has_ready_account = bool(self._selected_account())
        can_trial = (
            has_code
            and has_facade
            and has_ready_account
            and not self._trial_running
            and not self._trial_blocked
        )
        self._trial_btn.setEnabled(can_trial)
        self._stop_trial_btn.setEnabled(bool(has_facade and self._trial_running))
        self._account_combo.setEnabled(has_facade)
        self._refresh_acc_btn.setEnabled(has_facade)
        if has_code:
            self._revise_btn.setEnabled(not self._trial_running and not gen_busy)
            self._confirm_btn.setEnabled(not self._trial_running)
        else:
            self._revise_btn.setEnabled(False)
            self._confirm_btn.setEnabled(False)
        if hasattr(self, "_rerevise_btn"):
            # 修订失败后常无反馈、也未必 trial_blocked；只要还有代码就应能重修订
            can_rerevise = (
                has_code
                and not gen_busy
                and not self._trial_running
                and (
                    has_feedback
                    or self._trial_blocked
                    or bool(getattr(self, "_last_revise_error", ""))
                )
            )
            self._rerevise_btn.setEnabled(can_rerevise)
        self._update_optimize_availability()

    def _update_optimize_availability(self):
        if not hasattr(self, "_optimize_btn"):
            return
        has_code = bool((self._optimize_code or "").strip())
        gen_busy = (
            (self._revise_worker is not None and self._revise_worker.isRunning())
            or (self._optimize_worker is not None and self._optimize_worker.isRunning())
        )
        self._optimize_btn.setEnabled(has_code and not gen_busy)
        if hasattr(self, "_optimize_save_btn"):
            self._optimize_save_btn.setEnabled(
                has_code and self._optimize_script_path is not None
            )
        if hasattr(self, "_optimize_hint"):
            if not has_code:
                self._optimize_hint.setText("请从列表点选脚本（或「加载选中」）。")
            else:
                self._optimize_hint.setText("")

    def _account_ready(self, account: dict) -> bool:
        if not self._facade or not account:
            return False
        name = account.get("name") or ""
        if not name:
            return False
        ctrl = self._facade.controller
        return (
            name in getattr(ctrl, "_browser_instances", {})
            or name in getattr(ctrl, "_window_instances", {})
        )

    def _fill_account_combo(self, combo: QComboBox, current: dict | None) -> None:
        combo.clear()
        if self._facade is None:
            combo.addItem("（无主程序）", None)
            return
        accounts = list(self._facade.list_accounts() or [])
        ready_accounts = [acc for acc in accounts if self._account_ready(acc)]
        if not ready_accounts:
            combo.addItem("（暂无已启动账号）", None)
            return
        for acc in ready_accounts:
            name = acc.get("name") or ""
            ctrl = self._facade.controller
            kinds = []
            if name in getattr(ctrl, "_browser_instances", {}):
                kinds.append("浏览器")
            if name in getattr(ctrl, "_window_instances", {}):
                kinds.append("窗口")
            suffix = " + ".join(kinds) if kinds else "已启动"
            combo.addItem(f"{name}  · {suffix}", acc)
        if current:
            for i in range(combo.count()):
                data = combo.itemData(i)
                if isinstance(data, dict) and data.get("name") == current.get("name"):
                    combo.setCurrentIndex(i)
                    break

    def _refresh_accounts(self):
        current = self._selected_account()
        self._fill_account_combo(self._account_combo, current)
        if self._facade is None:
            self._update_trial_availability()
            return
        accounts = list(self._facade.list_accounts() or [])
        ready_accounts = [acc for acc in accounts if self._account_ready(acc)]
        if not ready_accounts and accounts:
            self._trial_hint.setText(
                "列表里只显示已启动浏览器或已绑定窗口的账号。"
                "请先在「开始」页启动后再点「刷新账号」。"
            )
            self._trial_btn.setEnabled(False)
        elif self._trial_hint.text().startswith("列表里只显示"):
            self._trial_hint.setText("")
        self._update_trial_availability()

    def _trial_script_name_hint(self) -> str:
        if self._optimize_script_path is not None:
            return self._optimize_script_path.stem
        name = (self._script_name.text().strip() if hasattr(self, "_script_name") else "") or ""
        if name.lower().endswith(".py"):
            name = name[:-3]
        return name or "trial"

    def _write_trial_file(self) -> Path:
        raw = (self._generated_code or "").strip()
        if not raw:
            raise RuntimeError("没有可试运行的代码")
        from backend.script_generator.pseudo_codegen import inject_pseudo_record_for_trial

        injected = inject_pseudo_record_for_trial(
            raw, script_name=self._trial_script_name_hint()
        )
        mode = "injected"
        chosen = injected
        if not self._code_compiles(injected):
            # 注入版语法损坏：回退原文，宁可没有伪录制也不能让试运行直接 SyntaxError
            mode = "raw-fallback"
            chosen = raw
            self._append_trial_log("[试运行] ⚠ 伪录制注入版编译失败，已回退未注入版本")
        if not self._code_compiles(chosen):
            raise RuntimeError(
                "试运行代码存在语法错误（原文编译失败）："
                "请回生成页查看完整代码或点「重修订」后再试运行。"
            )
        _TRIAL_DIR.mkdir(parents=True, exist_ok=True)
        init_py = _TRIAL_DIR / "__init__.py"
        if not init_py.exists():
            init_py.write_text("# trial package\n", encoding="utf-8")
        path = SCRIPTS_PATH / _TRIAL_REL
        # 原子写：避免任务在写盘中途 import 到半截文件
        import os as _os

        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(chosen, encoding="utf-8")
        _os.replace(tmp, path)
        # 双归档：raw / injected 都留，事后可复现注入问题
        if getattr(self, "_archive", None) is not None and self._archive.session_dir:
            try:
                self._archive.write_text("code_at_trial_raw.py", raw)
                self._archive.write_text("code_at_trial_injected.py", chosen)
            except Exception as e:
                print(f"[ScriptGenerator] 归档试运行文件失败: {e}")
        self._append_trial_log(f"[试运行] 写入模式: {mode}")
        return path

    @staticmethod
    def _code_compiles(code: str) -> bool:
        try:
            compile(code or "", "<trial>", "exec")
            return True
        except SyntaxError:
            return False

    def _run_local_script_validate(self, *, log: bool = False) -> list[str]:
        """本地脚本检查：结构 + 素材（写入试运行文件后自动执行）。"""
        code = (self._generated_code or "").strip()
        if not code:
            self._trial_blocked = True
            self._trial_block_reason = "无代码"
            self._update_trial_availability()
            return ["无代码"]
        from backend.script_generator.agent import (
            patch_missing_handler_return_keys,
            validate_script_local,
        )

        # 生成侧偶发漏补 return 目标键：试运行前再补一次，避免「生成成功却不能试跑」
        patched, patch_notes = patch_missing_handler_return_keys(code)
        if patch_notes and patched.strip() and patched != code:
            self._generated_code = patched if patched.endswith("\n") else patched + "\n"
            code = self._generated_code.strip()
            try:
                self._write_trial_file()
            except Exception:
                pass
            if log:
                preview = "；".join(patch_notes[:4])
                more = f" 等{len(patch_notes)}处" if len(patch_notes) > 4 else ""
                self._append_trial_log(f"[脚本检查] 已自动补状态路由：{preview}{more}")

        errs = validate_script_local(
            code,
            explanation=self._explanation.toPlainText(),
            source_dir=str(self._source_dir or ""),
            free_mode=bool(self._free_mode_cb.isChecked()),
        )
        self._trial_blocked = bool(errs)
        self._trial_block_reason = errs[0] if errs else ""
        self._update_trial_availability()
        if log:
            if errs:
                lines = "\n".join(f"- {e}" for e in errs[:8])
                if len(errs) > 8:
                    lines += f"\n…共 {len(errs)} 项"
                self._append_trial_log(f"[脚本检查] 未通过\n{lines}")
            else:
                self._append_trial_log("[脚本检查] 通过（结构 + 素材）")
        return errs

    def _sync_trial_code(self):
        """写入试运行临时文件并自动做本地脚本检查。"""
        try:
            if (self._generated_code or "").strip():
                self._write_trial_file()
                self._run_local_script_validate(log=True)
        except Exception as e:
            print(f"[ScriptGenerator] 写入试运行临时文件失败: {e}")

    def _selected_account(self) -> dict | None:
        data = self._account_combo.currentData()
        return data if isinstance(data, dict) and data.get("name") else None

    def _selected_opt_account(self) -> dict | None:
        """优化页不再选账号；仅复用试运行页已选账号作伪录制匹配提示。"""
        return self._selected_account()

    def _on_tab_changed(self, index: int):
        if index == self.TAB_TRIAL and (self._generated_code or "").strip():
            # 回到试运行页时重检：避免「生成已修好但仍沿用上次拦截」
            prev_blocked = bool(self._trial_blocked)
            errs = self._run_local_script_validate(log=False)
            if prev_blocked and not errs:
                self._append_trial_log("[脚本检查] 重新检查已通过 · 可试运行")
                if hasattr(self, "_feedback"):
                    fb = self._feedback.toPlainText().strip()
                    if fb.startswith("修复本地校验错误"):
                        self._feedback.clear()
                        self._set_feedback_stale(False)
            elif errs and not prev_blocked:
                self._append_trial_log(
                    f"[脚本检查] 未通过：{errs[0]}"
                )
        if index == self.TAB_OPTIMIZE:
            self._refresh_optimize_script_list()

    def _init_optimize_folder_combo(self):
        if not hasattr(self, "_optimize_folder_combo"):
            return
        self._optimize_folder_combo.blockSignals(True)
        self._optimize_folder_combo.clear()
        self._optimize_folder_combo.addItem("全部", "")
        if SCRIPTS_PATH.is_dir():
            for p in sorted(SCRIPTS_PATH.iterdir(), key=lambda x: x.name.lower()):
                if p.is_dir() and p.name not in ("__pycache__",):
                    self._optimize_folder_combo.addItem(p.name, p.name)
        self._optimize_folder_combo.blockSignals(False)

    def _collect_optimize_py_files(self) -> list[Path]:
        root = SCRIPTS_PATH
        if not root.is_dir():
            return []
        folder = (self._optimize_folder or "").strip()
        search = (
            self._optimize_search.text().strip().lower()
            if hasattr(self, "_optimize_search")
            else ""
        )
        base = root / folder if folder else root
        if not base.is_dir():
            return []
        skip_parts = {"__pycache__"}
        out: list[Path] = []
        for p in sorted(base.rglob("*.py"), key=lambda x: str(x).lower()):
            if any(part in skip_parts for part in p.parts):
                continue
            if p.name == "__init__.py":
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if search and search not in rel.lower():
                continue
            out.append(p)
        return out

    def _refresh_optimize_script_list(self):
        if not hasattr(self, "_optimize_script_list"):
            return
        self._optimize_script_files = self._collect_optimize_py_files()
        total = len(self._optimize_script_files)
        pages = max(1, (total + _OPTIMIZE_PAGE_SIZE - 1) // _OPTIMIZE_PAGE_SIZE)
        if self._optimize_page >= pages:
            self._optimize_page = max(0, pages - 1)
        start = self._optimize_page * _OPTIMIZE_PAGE_SIZE
        chunk = self._optimize_script_files[start : start + _OPTIMIZE_PAGE_SIZE]

        self._optimize_script_list.clear()
        root = SCRIPTS_PATH
        for p in chunk:
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = p.name
            item = QListWidgetItem(rel)
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            self._optimize_script_list.addItem(item)

        self._optimize_page_label.setText(
            f"第 {self._optimize_page + 1}/{pages} 页 · 共 {total} 个脚本"
        )
        self._optimize_prev_btn.setEnabled(self._optimize_page > 0)
        self._optimize_next_btn.setEnabled(self._optimize_page < pages - 1)

    def _optimize_change_page(self, delta: int):
        self._optimize_page = max(0, self._optimize_page + int(delta))
        self._refresh_optimize_script_list()

    def _on_optimize_folder_changed(self):
        data = self._optimize_folder_combo.currentData()
        self._optimize_folder = str(data or "")
        self._optimize_page = 0
        self._refresh_optimize_script_list()

    def _append_optimize_log(self, line: str):
        if not line:
            return
        self._optimize_log_lines.append(line)
        if hasattr(self, "_optimize_log"):
            self._optimize_log.append(line)
            self._optimize_log.moveCursor(QTextCursor.MoveOperation.End)

    def _load_optimize_script(self, path: Path) -> bool:
        path = Path(path)
        if not path.is_file():
            QMessageBox.warning(self, "文件不存在", str(path))
            return False
        # 点选同一文件不重复读盘
        if (
            self._optimize_script_path is not None
            and path.resolve() == self._optimize_script_path.resolve()
            and (self._optimize_code or "").strip()
        ):
            return True
        try:
            code = path.read_text(encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))
            return False
        self._optimize_code = code
        self._optimize_script_path = path
        self._generated_code = code
        self._stream_buf = code
        try:
            rel = path.relative_to(SCRIPTS_PATH).as_posix()
        except ValueError:
            rel = path.name
        self._optimize_path_label.setText(rel)
        self._script_name.setText(path.name)
        self._append_optimize_log(f"[加载] {rel}（{len(code)} 字符）")
        # 加载只读入内存；写入试跑临时文件 + 本地校验延后到「去试运行」
        self._trial_blocked = False
        self._trial_block_reason = ""
        self._update_optimize_availability()
        self._update_trial_availability()
        return True

    def _on_optimize_script_activated(self, item: QListWidgetItem):
        raw = item.data(Qt.ItemDataRole.UserRole)
        if raw:
            self._load_optimize_script(Path(str(raw)))

    def _on_optimize_load_selected(self):
        item = self._optimize_script_list.currentItem()
        if not item:
            QMessageBox.information(self, "未选择", "请先在列表中选中脚本。")
            return
        raw = item.data(Qt.ItemDataRole.UserRole)
        if raw:
            self._load_optimize_script(Path(str(raw)))

    def _on_optimize_pick_file(self):
        start = str(SCRIPTS_PATH if SCRIPTS_PATH.is_dir() else Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Python 脚本", start, "Python (*.py);;All (*.*)",
        )
        if path:
            self._load_optimize_script(Path(path))

    def _on_optimize_save(self):
        code = (self._optimize_code or "").strip()
        if not code:
            QMessageBox.warning(self, "无代码", "没有可保存的内容。")
            return
        path = self._optimize_script_path
        if path is None:
            path_str, _ = QFileDialog.getSaveFileName(
                self,
                "保存脚本",
                str(SCRIPTS_PATH / "my_script.py"),
                "Python (*.py)",
            )
            if not path_str:
                return
            path = Path(path_str)
        try:
            path.write_text(code, encoding="utf-8")
            self._optimize_script_path = path
            self._append_optimize_log(f"[保存] {path}")
            QMessageBox.information(self, "已保存", str(path))
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _view_optimize_code(self):
        code = (self._optimize_code or self._generated_code or "").strip()
        if not code:
            QMessageBox.information(self, "无代码", "请先加载脚本。")
            return
        dlg = FullCodeDialog(self, code=code, title="优化中的脚本")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.applied_code is not None:
            self._optimize_code = dlg.applied_code
            self._generated_code = dlg.applied_code
            self._stream_buf = dlg.applied_code
            self._trial_blocked = False
            self._trial_block_reason = ""
            self._update_optimize_availability()
            self._update_trial_availability()

    def _on_optimize_go_trial(self):
        if not (self._optimize_code or "").strip():
            QMessageBox.information(self, "无代码", "请先加载脚本。")
            return
        self._generated_code = self._optimize_code
        self._sync_trial_code()
        self._tabs.setCurrentIndex(self.TAB_TRIAL)

    def _on_trial_run(self):
        if self._facade is None:
            QMessageBox.warning(self, "无法试运行", "请从主窗口打开脚本生成器后再试运行。")
            return
        if not (self._generated_code or "").strip():
            QMessageBox.warning(self, "无代码", "请先生成脚本。")
            return
        account = self._selected_account()
        if not account:
            QMessageBox.warning(self, "未选账号", "请先选择账号。")
            return
        name = account["name"]
        ctrl = self._facade.controller
        ready = (
            name in getattr(ctrl, "_browser_instances", {})
            or name in getattr(ctrl, "_window_instances", {})
        )
        if not ready:
            QMessageBox.warning(
                self,
                "账号未就绪",
                f"账号「{name}」尚未启动浏览器或绑定窗口。\n"
                "请先在「开始」页启动后再试运行。",
            )
            self._refresh_accounts()
            return
        # 试跑：按实际就绪目标写入 _target，避免生成脚本 UserBrowser 注解锁死浏览器
        has_br = name in getattr(ctrl, "_browser_instances", {})
        has_win = name in getattr(ctrl, "_window_instances", {})
        if has_win and not has_br:
            account["_target"] = "window"
        elif has_br and not has_win:
            account["_target"] = "browser"
        elif has_win and account.get("_target") not in ("browser", "window"):
            account["_target"] = "window"
        task_ctrl = getattr(ctrl, "_task_ctrls", {}).get(name)
        if task_ctrl is not None and getattr(task_ctrl, "_future", None):
            QMessageBox.warning(
                self,
                "账号忙碌",
                f"账号「{name}」已有任务在跑。\n请先停止该账号当前任务，再试运行。",
            )
            return
        try:
            path = self._write_trial_file()
        except Exception as e:
            QMessageBox.critical(self, "写入临时文件失败", str(e))
            return

        self._trial_log_lines.clear()
        self._trial_log.clear()
        self._trial_account_name = name
        self._trial_running = True
        self._last_trial_frame = None
        self._stop_frame_path = ""
        self._tabs.setCurrentIndex(self.TAB_TRIAL)
        self._set_trial_chip("write", "done")
        self._set_trial_chip("run", "active")
        self._set_trial_chip("frame", "idle")
        self._set_trial_chip("feas", "idle")
        if getattr(self, "_trial_hud", None) is not None:
            self._trial_hud.start_run(name)
        if hasattr(self, "_trial_frame_lbl"):
            self._trial_frame_lbl.setPixmap(QPixmap())
            self._trial_frame_lbl.setText("运行中…\n结束或停止后显示停帧")
        self._set_agent_status_text(f"试运行 · {name}")
        if hasattr(self, "_trajectory") and self._trajectory._steps:
            self._trajectory.begin_phase(f"试运行 · {name}", kind="Trial")
        # 再试跑时反馈框若仍是上次内容 → 标成「陈旧」样式，提醒改写/清空
        if self._feedback.toPlainText().strip():
            self._set_feedback_stale(True)
        self._append_trial_log(f"[试运行] 临时文件: {path}")
        self._append_trial_log(f"[试运行] 账号: {name} · 模块: {_TRIAL_REL}")
        self._append_trial_log(
            "[试运行] 已自动开启伪录制（确认完成并保存时会从正式脚本去掉）"
        )
        self._set_trial_pseudo_env(True)
        self._update_trial_availability()
        try:
            self._facade.start_task(account, _TRIAL_REL)
        except Exception as e:
            self._trial_running = False
            self._set_trial_pseudo_env(False)
            hud = getattr(self, "_trial_hud", None)
            if hud is not None:
                hud.set_terminal("error")
            self._update_trial_availability()
            QMessageBox.critical(self, "试运行失败", str(e))

    def _on_stop_trial(self):
        if self._facade is None or not self._trial_account_name:
            return
        # 先缓存停帧，再 stop（避免任务结束后帧被清掉）
        self._cache_stop_frame_now(also_capture=True)
        account = self._selected_account()
        if account is None or account.get("name") != self._trial_account_name:
            account = {"name": self._trial_account_name}
        try:
            self._facade.stop_task(account)
        except Exception as e:
            QMessageBox.warning(self, "停止失败", str(e))
        self._append_trial_log("[试运行] 已请求停止")

    def _append_trial_log(self, line: str):
        self._trial_log_lines.append(line)
        self._trial_log.append(line)

    def _on_trial_log(self, account: str, event):
        if not self._trial_running:
            return
        if account != self._trial_account_name:
            return
        msg = getattr(event, "message", None) or str(event)
        level = getattr(event, "level", "")
        prefix = f"[{level}] " if level else ""
        self._append_trial_log(f"{prefix}{msg}")
        hud = getattr(self, "_trial_hud", None)
        if hud is not None:
            hud.on_log_line(f"{prefix}{msg}")

    def _on_trial_state(self, event):
        if not self._trial_running:
            return
        # UnifiedEvent: type == "task", payload 含 browser / status
        try:
            if getattr(event, "type", None) != "task":
                return
            snap = event.payload
            browser = getattr(snap, "browser", None) or getattr(snap, "account", None)
            if browser != self._trial_account_name:
                return
            status_raw = getattr(snap, "status", "")
            status = getattr(status_raw, "value", status_raw)
            status = str(status or "").lower()
            message = getattr(snap, "message", "") or ""
            if message:
                self._append_trial_log(f"[状态] {message}")
                hud = getattr(self, "_trial_hud", None)
                if hud is not None:
                    hud.on_log_line(f"[状态] {message}")
            if status in ("finished", "stopped", "error", "idle"):
                self._trial_running = False
                self._set_trial_pseudo_env(False)
                hud = getattr(self, "_trial_hud", None)
                if hud is not None:
                    hud.set_terminal(status)
                # 自然结束/停止结束时再确保有停帧
                if not self._stop_frame_path:
                    self._cache_stop_frame_now(also_capture=True)
                else:
                    self._try_cache_trial_frame()
                    self._persist_stop_frame()
                self._update_trial_availability()
                self._append_trial_log(f"[试运行] 结束 ({status})")
                self._set_trial_chip("run", "done")
                self._update_trial_frame_preview()
                self._archive_trial_end(status)
                self._set_trial_chip("feas", "active")
                self._report_trial_feasibility(status)
                self._set_trial_chip("feas", "done")
                self._set_agent_status_text(f"试运行结束 · {status}")
                if hasattr(self, "_trajectory") and self._trajectory._steps:
                    self._trajectory.succeed_run(f"试运行结束 ({status})")
        except Exception:
            pass

    def _on_revise(self):
        self._launch_revise(run_label="根据反馈修订")

    def _on_rerevise(self):
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._launch_revise(run_label="重修订")

    def _on_optimize(self):
        code = (self._optimize_code or self._generated_code or "").strip()
        if not code:
            QMessageBox.warning(self, "无代码", "请先从脚本库加载 .py 或生成脚本。")
            return
        api_key = self._api_key.text().strip()
        model = self._model.currentText().strip()
        if not api_key or not model:
            QMessageBox.warning(self, "缺少配置", "请填写 API Key 和模型。")
            return

        self._generated_code = code
        user_feedback = ""
        if hasattr(self, "_optimize_feedback"):
            user_feedback = self._optimize_feedback.toPlainText().strip()

        from backend.script_generator.pseudo_analyze import find_latest_pseudo_record

        script_hint = (
            self._optimize_script_path.stem
            if self._optimize_script_path
            else (self._script_name.text().strip() or "_gen_trial")
        )
        # 账号仅用于匹配伪录制目录名，可为空
        opt_acc = self._selected_opt_account()
        account = (opt_acc or {}).get("name") or self._trial_account_name or ""
        rec = find_latest_pseudo_record(script_hint=script_hint, account_hint=account)
        if rec is None:
            rec = find_latest_pseudo_record(script_hint=script_hint)

        force = bool(user_feedback)
        if rec is None and not user_feedback:
            ok = QMessageBox.question(
                self,
                "未找到伪录制",
                "未找到匹配的伪录制，且未填写优化方向。\n\n"
                "仍仅按代码做保守优化？\n"
                "（也可取消后先写优化方向再提交。）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ok != QMessageBox.StandardButton.Yes:
                return
        elif rec is None and user_feedback:
            self._append_optimize_log("[优化] 无伪录制，将按填写的优化方向修订")

        try:
            self._persist_explanation(self._explanation.toPlainText())
        except Exception as e:
            print(f"[ScriptGenerator] 优化前保存介绍失败: {e}")

        if self._archive.session_dir is None:
            self._archive.begin("optimize")

        params = {
            "provider": self._current_provider(),
            "api_key": api_key,
            "model": model,
            "api_endpoint": self._endpoint.text().strip() or None,
            "explanation_text": self._explanation.toPlainText(),
            "current_code": code,
            "source_dir": str(self._source_dir or ""),
            "script_name_hint": script_hint,
            "account_hint": account,
            "user_feedback": user_feedback,
            "trial_log": "\n".join(
                (self._optimize_log_lines + self._trial_log_lines)[-200:]
            ),
            "pseudo_record_dir": str(rec) if rec else "",
            "force": force,
            "max_tokens": self._max_tokens.value() if hasattr(self, "_max_tokens") else None,
        }

        self._generate_btn.setEnabled(False)
        self._revise_btn.setEnabled(False)
        if hasattr(self, "_optimize_btn"):
            self._optimize_btn.setEnabled(False)
        if hasattr(self, "_rerevise_btn"):
            self._rerevise_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._set_agent_busy("优化", "分析伪录制 / 边界评估…", indeterminate=True)
        self._keep_prev_live_code(phase="优化")
        self._token_label.setText("")
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._trajectory.begin_run("脚本优化", fresh=False)
        if self._archive.session_dir:
            self._archive.append_event(
                "optimize_begin",
                script_hint=script_hint,
                has_feedback=bool(user_feedback),
            )
        if user_feedback:
            self._append_optimize_log("[优化方向]\n" + user_feedback[:500])

        # 保存优化前试跑日志，供再试跑后做可靠性对比
        self._pre_optimize_trial_log = "\n".join(self._trial_log_lines)
        self._awaiting_reliability_retrial = False
        self._pre_optimize_record_dir = ""
        try:
            from backend.script_generator.pseudo_analyze import find_latest_pseudo_record

            hint = ""
            if self._optimize_script_path:
                hint = self._optimize_script_path.stem
            rec = find_latest_pseudo_record(script_hint=hint)
            if rec:
                self._pre_optimize_record_dir = str(rec)
        except Exception:
            pass

        self._optimize_worker = OptimizeWorker(params)
        self._optimize_worker.finished.connect(self._on_optimize_success)
        self._optimize_worker.partial.connect(self._on_partial)
        self._optimize_worker.status.connect(self._on_status)
        self._optimize_worker.artifact.connect(self._on_artifact)
        self._optimize_worker.token_info.connect(self._on_token_info)
        self._optimize_worker.error.connect(self._on_optimize_error)
        self._optimize_worker.start()

    def _on_optimize_success(self, code: str, summary: str = "", meta=None):
        meta = meta if isinstance(meta, dict) else {}
        self._optimize_code = code
        self._generated_code = code
        self._stream_buf = code or ""
        self._set_live_code(code or "")
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        skipped = bool((meta or {}).get("skipped_llm"))
        n_chars = len(code or "")
        idle = "✓ 优化完成 · 建议再试运行" if not skipped else "已接近优化边界"
        self._set_agent_idle(idle, success=not skipped)
        if hasattr(self, "_live_code_hint"):
            self._live_code_hint.setText(
                f"{'已优化' if not skipped else '未改写'} · {n_chars:,} 字符"
            )
        self._set_ok_banner(
            ("✓ 优化完成　　" if not skipped else "边界评估完成　　")
            + f"{n_chars:,} 字符　　建议再试运行对比"
        )
        # 不在此处同步试跑校验；用户点「去试运行」时再写入临时文件
        self._update_trial_availability()
        self._update_optimize_availability()

        summary = (summary or "").strip() or "（无摘要）"
        ceiling = meta.get("ceiling") or {}
        baseline = meta.get("baseline") or {}
        if baseline:
            self._append_optimize_log(
                "[优化 baseline] "
                f"total={baseline.get('total_s')}s "
                f"black={baseline.get('black_s')}s "
                f"effective={baseline.get('effective_s')}s"
            )
        if meta.get("pseudo_record_dir"):
            self._append_optimize_log(f"[优化] 伪录制: {meta['pseudo_record_dir']}")
        self._append_optimize_log("[优化摘要]\n" + summary)
        self._trajectory.succeed_run("优化完成" if not skipped else "边界评估完成")
        self._flash_taskbar("脚本优化完成" if not skipped else "已接近优化边界")

        val_errors = meta.get("validation_errors") or []
        near = bool(ceiling.get("near_ceiling"))
        if not skipped:
            self._awaiting_reliability_retrial = True
            self._append_optimize_log(
                "[可靠性] 请再试跑一次；结束后将自动对比优化前/后轨迹信号。"
            )

        if skipped:
            title = "已接近脚本优化边界"
            tip = summary
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Information)
        elif val_errors:
            title = "优化完成 — 脚本检查未通过"
            tip = (
                "代码已更新，但本地校验有警告，请核对后再试跑。\n\n"
                f"{summary}\n\n"
                + "\n".join(f"- {e}" for e in val_errors[:6])
                + "\n\n建议再试跑一次，核对可靠性是否下降。"
            )
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Warning)
        elif near:
            _show_scroll_message(
                self,
                "优化完成 — 可能已接近边界",
                "代码已更新。\n\n"
                f"{summary}\n\n"
                "请再试跑一次：对比 effective，并核对可靠性是否下降；"
                "若 effective 变化 <5%，建议停止反复优化。",
                icon=QMessageBox.Icon.Information,
            )
        else:
            _show_scroll_message(
                self,
                "优化完成",
                "代码已更新。\n\n"
                f"{summary}\n\n"
                "请点「去试运行」再跑一轮：会自动对比优化前/后可靠性；"
                "也可先「保存到文件」。",
            )

        if self._archive.session_dir:
            try:
                self._archive.write_text("code_post_optimize.py", code)
                self._archive.write_text("optimize_summary.txt", summary)
                if hasattr(self, "_optimize_feedback"):
                    fb = self._optimize_feedback.toPlainText().strip()
                    if fb:
                        self._archive.write_text("optimize_feedback.txt", fb)
                if meta.get("pseudo_record_dir"):
                    self._archive.merge_meta({"pseudo_record_dir": meta["pseudo_record_dir"]})
                self._archive.append_event("optimize_done")
            except Exception as e:
                print(f"[ScriptGenerator] 归档优化失败: {e}")

    def _on_optimize_error(self, msg: str):
        translated = self._translate_error(msg)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        reason = (translated.split("\n", 1)[0] or "优化失败").strip()
        self._set_agent_idle(f"优化失败 · {reason}")
        self._update_trial_availability()
        self._trajectory.update_step(
            "optimize",
            "Error",
            reason,
            status="error",
            body=translated,
        )
        self._trajectory.fail_run(reason)
        QMessageBox.critical(self, "优化失败", translated)

    def _launch_revise(self, *, run_label: str = "根据反馈修订"):
        api_key = self._api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key")
            return
        feedback = self._feedback.toPlainText().strip()
        if not feedback and self._trial_blocked:
            reason = self._trial_block_reason or "硬校验未通过"
            feedback = f"修复本地校验错误：{reason}"
        if not feedback and getattr(self, "_last_revise_error", ""):
            feedback = (
                "上次修订失败，请基于原代码继续修复。\n"
                f"失败原因：{self._last_revise_error}"
            )
        if not feedback:
            QMessageBox.warning(
                self,
                "缺少反馈",
                "请先在试运行页填写反馈，或保留上次反馈后再修订。\n\n"
                "若刚因硬校验失败，可直接点「重修订」（会自动带上错误说明）。",
            )
            return
        if not (self._generated_code or "").strip():
            # 界面被清空时尽量从试运行临时文件恢复
            recovered = self._try_recover_code_from_trial()
            if not recovered:
                QMessageBox.warning(self, "无代码", "请先生成脚本。")
                return
            feedback = feedback  # keep

        try:
            self._persist_explanation()
        except Exception as e:
            print(f"[ScriptGenerator] 修订前保存介绍失败: {e}")

        if self._archive.session_dir is None:
            self._archive.begin("revise")
        # 修订前再确保停帧落盘（截图异步晚到时）
        if self._last_trial_frame is not None:
            self._persist_stop_frame()
        self._archive_revise_start(feedback)

        params = {
            "provider": self._current_provider(),
            "api_key": api_key,
            "model": self._model.currentText().strip(),
            "api_endpoint": self._endpoint.text().strip() or None,
            "explanation_text": self._explanation.toPlainText().strip(),
            "current_code": self._generated_code,
            "user_feedback": feedback,
            "source_dir": str(self._source_dir) if self._source_dir else "",
            "trial_log": "\n".join(self._trial_log_lines[-200:]),
            "max_tokens": int(self._max_tokens.value()),
            "free_mode": bool(self._free_mode_cb.isChecked()),
            "stop_frame_path": self._stop_frame_path or "",
            "prior_summary": getattr(self, "_last_revise_summary", "") or "",
            "prior_diagnosis": getattr(self, "_last_diagnosis_json", "") or "",
            "chat_session": getattr(self, "_chat_session", None),
        }
        v_key = self._vision_api_key.text().strip()
        v_model = self._vision_model.currentText().strip()
        if v_key and v_model:
            params["vision_assist"] = {
                "provider": self._current_vision_provider(),
                "api_key": v_key,
                "model": v_model,
                "api_endpoint": self._vision_endpoint.text().strip() or None,
                "compress_images": self._compress_img_cb.isChecked(),
            }
        elif self._send_img_cb.isChecked():
            # 未配辅助识图时，若主模型支持看图则回退主模型
            params["vision_assist"] = {
                "provider": self._current_provider(),
                "api_key": api_key,
                "model": self._model.currentText().strip(),
                "api_endpoint": self._endpoint.text().strip() or None,
                "compress_images": self._compress_img_cb.isChecked(),
            }

        self._generate_btn.setEnabled(False)
        self._revise_btn.setEnabled(False)
        self._confirm_btn.setEnabled(False)
        self._trial_btn.setEnabled(False)
        if hasattr(self, "_rerevise_btn"):
            self._rerevise_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._set_agent_busy("修订", run_label, indeterminate=True)
        self._keep_prev_live_code(phase="修订")
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._trajectory.begin_run(run_label, fresh=False)
        if self._archive.session_dir is not None:
            self._archive.append_event("revise_begin")
        self._trajectory.update_step(
            "revise",
            "Revise",
            run_label,
            status="running",
            body=(feedback[:400] + ("…" if len(feedback) > 400 else "")),
        )

        self._revise_worker = ReviseWorker(params)
        self._revise_worker.finished.connect(self._on_revise_success)
        self._revise_worker.partial.connect(self._on_partial)
        self._revise_worker.status.connect(self._on_status)
        self._revise_worker.artifact.connect(self._on_artifact)
        self._revise_worker.token_info.connect(self._on_token_info)
        self._revise_worker.error.connect(self._on_revise_error)
        self._revise_worker.start()

    _FEEDBACK_SECTION = "试运行反馈（生成时必须遵守）"

    def _find_intro_in_folder(self, folder: Path) -> Path | None:
        for name in _INTRO_FILENAMES:
            cand = folder / name
            if cand.is_file():
                return cand
        return None

    def _update_expl_path_label(self, *, saved: bool = False):
        if not hasattr(self, "_expl_path_label"):
            return
        path = self._resolve_expl_save_path()
        if path:
            hint = " · 已自动保存" if saved else "（编辑会自动保存）"
            self._expl_path_label.setText(f"绑定文件：{path}{hint}")
        else:
            self._expl_path_label.setText(
                "未绑定文件：请先「加载介绍 txt」或「选择图片文件夹」"
            )

    def _resolve_expl_save_path(self) -> Path | None:
        if self._expl_path:
            return self._expl_path
        if self._source_dir:
            found = self._find_intro_in_folder(self._source_dir)
            if found:
                self._expl_path = found
                return found
            self._expl_path = self._source_dir / "脚本介绍.txt"
            return self._expl_path
        return None

    def _persist_explanation(self, text: str | None = None) -> Path | None:
        """把当前脚本描述写回绑定的介绍 txt。"""
        path = self._resolve_expl_save_path()
        if path is None:
            return None
        body = self._explanation.toPlainText() if text is None else text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((body or "").rstrip() + "\n", encoding="utf-8")
        self._expl_path = path
        self._explanation_text = body or ""
        self._update_expl_path_label()
        return path

    def _on_explanation_edited(self):
        if self._expl_loading:
            return
        self._expl_save_timer.start(800)

    def _autosave_explanation(self):
        if self._expl_loading:
            return
        if self._resolve_expl_save_path() is None:
            return
        try:
            path = self._persist_explanation()
            if path:
                self._update_expl_path_label(saved=True)
        except Exception as e:
            print(f"[ScriptGenerator] 介绍自动保存失败: {e}")

    def _save_explanation_clicked(self):
        path = self._resolve_expl_save_path()
        if path is None:
            from gui.widgets.ResourcePicker import ResourcePickerDialog
            dlg = ResourcePickerDialog(self, mode="folders", root_path=str(IMG_PATH))
            if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
                QMessageBox.information(
                    self, "未绑定文件",
                    "请先选择图片文件夹或加载介绍 txt，再保存。",
                )
                return
            self._source_dir = Path(dlg.selected_path)
            path = self._resolve_expl_save_path()
        try:
            saved = self._persist_explanation()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        QMessageBox.information(self, "已保存", str(saved))

    def _maybe_bind_expl_file(self, folder: Path, *, load_if_empty: bool = True):
        """素材夹里若有脚本介绍/解释，记住路径；介绍框为空时自动载入。"""
        bound = None
        if self._expl_path and self._expl_path.is_file():
            try:
                if self._expl_path.resolve().parent == folder.resolve():
                    bound = self._expl_path
            except Exception:
                bound = None
        if bound is None:
            bound = self._find_intro_in_folder(folder)
        if bound is None:
            # 素材夹已选、尚无介绍文件 → 编辑时自动创建 脚本介绍.txt
            self._expl_path = folder / "脚本介绍.txt"
            self._update_expl_path_label()
            return
        self._expl_path = bound
        self._update_expl_path_label()
        if not load_if_empty:
            return
        if self._explanation.toPlainText().strip():
            return
        try:
            self._expl_loading = True
            text = bound.read_text(encoding="utf-8")
            self._explanation.setPlainText(text)
            self._explanation_text = text
        except Exception:
            pass
        finally:
            self._expl_loading = False

    def _feedback_bullets(self, feedback: str) -> list[str]:
        from datetime import date
        today = date.today().isoformat()
        lines = [ln.strip() for ln in (feedback or "").splitlines() if ln.strip()]
        if not lines:
            return []
        bullets = []
        for ln in lines:
            ln = re.sub(r"^[-*•]\s*", "", ln)
            ln = re.sub(r"^[\d]+[\.\)、]\s*", "", ln)
            if len(ln) > 400:
                ln = ln[:400] + "…"
            bullets.append(f"- {today}：{ln}")
        return bullets

    def _confirm_and_write_feedback(self, feedback: str) -> tuple[str | None, list[str]]:
        """分类压句后弹勾选框，确认才写入脚本解释。"""
        from backend.script_generator.feedback_opt import (
            distill_feedback,
            optimize_feedback_sync,
        )

        items = distill_feedback(feedback, self._explanation.toPlainText())
        if not items:
            return None, []

        api_key = self._api_key.text().strip()
        model = self._model.currentText().strip()
        if api_key and model and any(it.kind == "constraint" for it in items):
            progress = QProgressDialog("正在整理约束条目…", None, 0, 0, self)
            progress.setWindowTitle("脚本解释")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.setCancelButton(None)
            progress.show()
            QApplication.processEvents()
            try:
                items = optimize_feedback_sync(
                    items,
                    self._explanation.toPlainText(),
                    provider=self._current_provider(),
                    api_key=api_key,
                    model=model,
                    api_endpoint=self._endpoint.text().strip() or None,
                )
            except Exception as e:
                self._append_trial_log(f"[介绍] 约束整理失败，使用本地规则: {e}")
            finally:
                progress.close()

        dlg = FeedbackWritebackDialog(items, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._append_trial_log("[介绍] 已跳过写回（未确认）")
            return None, []
        bullets = dlg.selected_bullets()
        if not bullets:
            self._append_trial_log("[介绍] 未勾选任何条目，不写回")
            return None, []
        msg = self._merge_feedback_into_explanation(feedback, bullets=bullets)
        return msg, bullets

    def _merge_feedback_into_explanation(self, feedback: str, bullets: list[str] | None = None) -> str | None:
        """把试运行反馈并进介绍（编辑框 + 原 txt），避免下次生成再踩同样的坑。"""
        new_lines = list(bullets) if bullets is not None else self._feedback_bullets(feedback)
        if not new_lines:
            return None
        current = self._explanation.toPlainText().rstrip()
        header = f"## {self._FEEDBACK_SECTION}"
        to_add = []
        for line in new_lines:
            body = line.split("：", 1)[-1].strip()
            if line in current or (body and body in current):
                continue
            to_add.append(line)
        if not to_add:
            return None
        if header in current:
            merged = current + "\n" + "\n".join(to_add)
        else:
            merged = current + ("\n\n" if current else "") + header + "\n" + "\n".join(to_add)
        self._explanation.setPlainText(merged)
        self._explanation_text = merged

        try:
            path = self._persist_explanation(merged)
        except Exception as e:
            self._append_trial_log(f"[介绍] 写回失败: {e}")
            return f"介绍已更新，但写文件失败：{e}"
        if path is None:
            return "已写入当前介绍（未绑定 txt，仅本次窗口有效；请加载介绍或选择图片文件夹后再保存）"
        msg = f"已把反馈写入 {path}"
        self._append_trial_log(f"[介绍] {msg}")
        return msg

    def _on_revise_success(self, code: str, summary: str = "", meta=None):
        meta = meta if isinstance(meta, dict) else {}

        self._generated_code = code
        self._stream_buf = code or ""
        self._set_live_code(code or "")
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        n_chars = len(code or "")
        self._last_revise_summary = (summary or "").strip()
        if isinstance(meta, dict) and meta.get("chat_session"):
            self._chat_session = meta["chat_session"]
        if isinstance(meta, dict) and meta.get("continue_chat"):
            self._append_trial_log("[修订] 已使用生成同会话续写")
        if isinstance(meta, dict) and meta.get("diagnosis"):
            try:
                import json as _json
                self._last_diagnosis_json = _json.dumps(
                    meta["diagnosis"], ensure_ascii=False, indent=2,
                )
            except Exception:
                pass
        self._sync_trial_code()
        self._reset_trial_surface_after_new_code()
        trial_blocked = self._trial_blocked
        if trial_blocked:
            reason = self._trial_block_reason or "脚本检查未通过"
            self._set_agent_idle("修订完成 · 校验未过不可试运行", success=False)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(f"已修订 · {n_chars:,} 字符 · 校验未过")
            self._set_warn_banner(f"修订完成但不可试运行　　{reason}")
            self._append_trial_log(
                "[修订] 脚本检查未通过，不可试运行。"
                + (f" ({reason})" if reason else "")
            )
        else:
            self._set_agent_idle("✓ 修订完成 · 可去试运行", success=True)
            if hasattr(self, "_live_code_hint"):
                self._live_code_hint.setText(f"已修订 · {n_chars:,} 字符 · 可保存或试运行")
            self._set_ok_banner(
                f"✓ 修订完成　　{n_chars:,} 字符　　可保存或去试运行验证"
            )
            if hasattr(self, "_to_trial_btn"):
                self._to_trial_btn.setProperty("ready", True)
                self._to_trial_btn.style().unpolish(self._to_trial_btn)
                self._to_trial_btn.style().polish(self._to_trial_btn)
                self._to_trial_btn.setText("去试运行 → 验证脚本")
        self._update_trial_availability()
        summary = (summary or "").strip() or "（无摘要）"
        self._append_trial_log("[修订摘要]\n" + summary)
        done_summary = f"约 {n_chars:,} 字符。\n{summary}"
        self._trajectory.succeed_run(
            "修订完成" if not trial_blocked else "修订完成（校验未过）",
            summary=done_summary,
        )
        self._flash_taskbar("反馈修订完成")
        self._last_revise_error = ""

        # 先展示修订结果，再弹「新增约束」确认（避免结果被写回对话框挡住）
        review_fail = not meta.get("review_ok", True) or "未完全通过" in summary
        if trial_blocked:
            title = "修订完成 — 脚本检查未通过"
            tip = (
                "代码已写入试运行临时文件，但脚本检查未通过，不可试运行。\n\n"
                f"{summary}\n\n"
                f"首项：{self._trial_block_reason or '见试运行日志'}\n\n"
                "请在「查看完整代码」改 _img 路径或结构后保存，会自动重新检查。\n"
                "关闭本窗后可选择是否把反馈写入脚本介绍。"
            )
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Critical)
        elif review_fail:
            title = "修订完成 — 审查未完全通过"
            tip = (
                "代码已更新并同步到试运行临时文件。\n\n"
                f"{summary}\n\n"
                "审查认为仍有反馈未落实（已自动补修至多 2 轮）。"
                "可改反馈后「重修订」或试运行验证。\n"
                "关闭本窗后可选择是否把反馈写入脚本介绍。"
            )
            _show_scroll_message(self, title, tip, icon=QMessageBox.Icon.Warning)
        else:
            title = "修订完成 — 请核对"
            tip = (
                "代码已更新并同步到试运行临时文件。\n\n"
                f"{summary}\n\n"
                "若仍与预期不符，请改反馈后「重修订」或再次修订。\n"
                "关闭本窗后可选择是否把反馈写入脚本介绍。"
            )
            _show_scroll_message(self, title, tip)

        feedback = self._feedback.toPlainText().strip()
        wrote = None
        writeback: list[str] = []
        if feedback:
            wrote, writeback = self._confirm_and_write_feedback(feedback)
        self._archive_revise_done(code, summary, writeback=writeback or None)
        if wrote:
            QMessageBox.information(self, "介绍已更新", wrote)

        if self._feedback.toPlainText().strip():
            self._set_feedback_stale(True)

    def _on_feedback_edited(self):
        if getattr(self, "_feedback_stale", False):
            self._set_feedback_stale(False)
        self._update_trial_availability()

    def _set_feedback_stale(self, stale: bool):
        self._feedback_stale = bool(stale)
        if not hasattr(self, "_feedback"):
            return
        self._feedback.setProperty("stale", "true" if stale else "false")
        self._feedback.style().unpolish(self._feedback)
        self._feedback.style().polish(self._feedback)
        if hasattr(self, "_feedback_stale_hint"):
            self._feedback_stale_hint.setVisible(stale)

    def _try_recover_code_from_trial(self) -> bool:
        """修订失败清空预览后，从试运行临时文件恢复内存中的代码。"""
        try:
            path = SCRIPTS_PATH / _TRIAL_REL
            if not path.is_file():
                return False
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                return False
            self._generated_code = text if text.endswith("\n") else text + "\n"
            self._stream_buf = self._generated_code
            self._set_live_code(self._generated_code)
            self._view_code_btn.setEnabled(True)
            self._save_btn.setEnabled(True)
            self._copy_btn.setEnabled(True)
            return True
        except Exception as e:
            print(f"[ScriptGenerator] 从试运行文件恢复代码失败: {e}")
            return False

    def _restore_code_preview_after_agent_fail(self) -> None:
        """生成/修订失败时恢复右侧代码预览与操作按钮（不丢 _generated_code）。"""
        code = (self._generated_code or "").strip()
        if not code:
            self._try_recover_code_from_trial()
            code = (self._generated_code or "").strip()
        if not code:
            return
        self._stream_buf = self._generated_code or code
        self._set_live_code(self._stream_buf)
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)

    def _on_revise_error(self, msg: str):
        translated = self._translate_error(msg)
        self._last_revise_error = translated
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        reason = (translated.split("\n", 1)[0] or "修订失败").strip()
        self._set_agent_idle(f"修订失败 · {reason}")
        self._restore_code_preview_after_agent_fail()
        self._update_trial_availability()
        self._trajectory.update_step(
            "revise",
            "Error",
            reason,
            status="error",
            body=translated,
        )
        self._trajectory.fail_run(reason)
        QMessageBox.critical(self, "修订失败", translated)

    def _set_trial_pseudo_env(self, enabled: bool) -> None:
        """试跑期间设置环境变量，供脚本内 getenv 分支启用伪录制。"""
        import os
        from backend.automation.run_recorder import ENV_FLAG

        if enabled:
            os.environ[ENV_FLAG] = "1"
        else:
            os.environ.pop(ENV_FLAG, None)

    def _report_trial_feasibility(self, status: str = "") -> None:
        """试跑结束后输出 L1 可行性评估；优化后再试跑时对比可靠性。"""
        try:
            from backend.script_generator.feasibility import (
                assess_trial_feasibility,
                compare_reliability,
            )
            from backend.script_generator.pseudo_analyze import find_latest_pseudo_record

            log = "\n".join(self._trial_log_lines)
            record_dir = None
            try:
                hint = ""
                if getattr(self, "_optimize_script_path", None):
                    hint = self._optimize_script_path.stem
                elif (self._optimize_code or self._generated_code or "").strip():
                    # 试跑临时脚本名不稳定时仍尽量用最近录制
                    hint = ""
                account = getattr(self, "_trial_account_name", "") or ""
                rec = find_latest_pseudo_record(
                    script_hint=hint, account_hint=account,
                )
                if rec is None and hint:
                    rec = find_latest_pseudo_record(script_hint=hint)
                if rec is None:
                    rec = find_latest_pseudo_record()
                record_dir = rec
            except Exception:
                record_dir = None

            feas = assess_trial_feasibility(
                log, status=status, record_dir=record_dir,
            )
            msg = feas.get("user_message") or ""
            if msg:
                self._append_trial_log("[可行性]\n" + msg)
            if hasattr(self, "_append_optimize_log"):
                self._append_optimize_log("[试跑可行性]\n" + msg)

            if (
                self._awaiting_reliability_retrial
                and (self._pre_optimize_trial_log or "").strip()
            ):
                cmp = compare_reliability(
                    self._pre_optimize_trial_log,
                    log,
                    before_record_dir=getattr(self, "_pre_optimize_record_dir", None) or None,
                    after_record_dir=record_dir,
                )
                self._awaiting_reliability_retrial = False
                if cmp.get("ok"):
                    note = "[可靠性对比] 相对优化前未见明显劣化（匹配/终态信号）。"
                else:
                    degraded = cmp.get("degraded") or []
                    note = (
                        "[可靠性对比] ⚠️ 相对优化前可能下降：\n"
                        + "\n".join(f"· {d}" for d in degraded)
                        + "\n建议回退或再修订，勿只追求速度。"
                    )
                self._append_trial_log(note)
                if hasattr(self, "_append_optimize_log"):
                    self._append_optimize_log(note)
                feas = dict(feas)
                feas["reliability_compare"] = {
                    "ok": cmp.get("ok"),
                    "degraded": cmp.get("degraded"),
                    "before_level": (cmp.get("before") or {}).get("level"),
                    "after_level": (cmp.get("after") or {}).get("level"),
                    "before_match_source": (
                        ((cmp.get("before") or {}).get("signals") or {}).get("match_source")
                    ),
                    "after_match_source": (
                        ((cmp.get("after") or {}).get("signals") or {}).get("match_source")
                    ),
                }

            if getattr(self, "_trial_hud", None) is not None:
                self._trial_hud.set_feasibility(feas)
            if self._archive.session_dir:
                try:
                    self._archive.write_text(
                        "feasibility.json",
                        __import__("json").dumps(feas, ensure_ascii=False, indent=2),
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[ScriptGenerator] 可行性评估失败: {e}")

    def _strip_pseudo_from_working_code(self) -> str:
        """从当前工作副本去掉伪录制，写回内存（正式保存用）。"""
        from backend.script_generator.pseudo_codegen import strip_pseudo_record_for_save

        code = strip_pseudo_record_for_save(self._generated_code or "")
        self._generated_code = code
        self._stream_buf = code
        if hasattr(self, "_optimize_code") and (self._optimize_code or "").strip():
            self._optimize_code = strip_pseudo_record_for_save(self._optimize_code)
        return code

    def _on_confirm_done(self):
        if not (self._generated_code or "").strip():
            QMessageBox.warning(self, "无代码", "没有可保存的脚本。")
            return
        if self._trial_running:
            QMessageBox.warning(self, "仍在试运行", "请先停止试运行再确认保存。")
            return
        reply = QMessageBox.question(
            self,
            "确认保存",
            "确定将当前脚本保存为正式文件吗？\n"
            "保存时会去掉试运行用的伪录制开关。\n"
            "保存后可在脚本列表中使用。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._strip_pseudo_from_working_code()
        self._save_script()

    def _cancel_generate(self):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)
        if self._revise_worker and self._revise_worker.isRunning():
            self._revise_worker.terminate()
            self._revise_worker.wait(2000)
        if self._optimize_worker and self._optimize_worker.isRunning():
            self._optimize_worker.terminate()
            self._optimize_worker.wait(2000)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._set_agent_idle("已取消")
        self._update_trial_availability()
        if hasattr(self, "_trajectory"):
            self._trajectory.mark_cancelled()

    @staticmethod
    def _translate_error(msg: str) -> str:
        """把 API / 网络异常翻成用户可读的中文说明。"""
        import re

        raw = (msg or "").strip() or "未知错误"
        low = raw.lower()

        def pack(title: str, advice: str) -> str:
            return (
                f"{title}\n\n"
                f"建议：\n{advice}\n\n"
                f"—— 原始错误（给排查用）——\n{raw}"
            )

        # 本地/结构校验失败（必须最先判：错误文本里常含 TASK_task1_TIMEOUT 等标识符，
        # 会被下面的 timeout 子串匹配误判成「网络超时」）
        if (
            "生成的脚本校验失败" in raw
            or "未通过本地检查" in raw
            or "校验失败（已重试" in raw
            or "仍校验失败" in raw
        ):
            return pack(
                "生成的脚本未通过本地检查",
                "1. 到「4. 试运行」点「重修订」（生成页也有此按钮），会自动带上错误说明\n"
                "2. 也可直接再点「生成脚本」重试一次\n"
                "3. 若同类错误反复出现，请简化脚本描述或补充素材/介绍",
            )

        # 余额 / 额度不足（须在通用 403 之前，DeepSeek 常回 402 + Insufficient Balance）
        if (
            "insufficient balance" in low
            or "insufficient_quota" in low
            or "insufficient credits" in low
            or "exceeded your current quota" in low
            or "billing" in low and ("hard limit" in low or "not active" in low)
            or "余额不足" in raw
            or "额度不足" in raw
            or "欠费" in raw
            or re.search(r"\b402\b", raw)
            or ("error code: 402" in low)
        ):
            return pack(
                "账号余额不足，无法调用该模型",
                "1. 到当前提供商官网充值（例如 DeepSeek 开放平台）\n"
                "2. 或在「API 配置」换一个仍有余额的方案 / 提供商后重试\n"
                "3. 充值到账后可直接点「重修订」或「生成脚本」，不必重新写介绍",
            )

        # API Key 无效 / 缺失（含 DeepSeek 400 + Please pass a valid API key）
        if (
            ("valid api key" in low)
            or ("incorrect api key" in low)
            or ("invalid api key" in low)
            or ("invalid_api_key" in low)
            or ("authentication" in low and ("401" in raw or "invalid" in low))
            or ("api key" in low and ("invalid" in low or "empty" in low or "missing" in low))
            or ("invalid argument" in low and "api" in low and "key" in low)
        ):
            return pack(
                "API Key 无效或未正确填写",
                "1. 检查 API Key 是否完整复制（前后不要有空格或换行）\n"
                "2. 确认 Key 属于当前选择的「提供商」（例如 DeepSeek 的 Key 不能填到 OpenAI）\n"
                "3. 到对应官网重新创建 Key，粘贴后点「保存到当前」，再点「连接测试」",
            )

        if "401" in raw or "unauthorized" in low:
            return pack(
                "身份验证失败（未授权）",
                "1. API Key 可能填错、过期或被撤销\n"
                "2. 重新填写正确的 Key 并保存后重试",
            )

        if "403" in raw or "permission" in low or "forbidden" in low:
            return pack(
                "没有权限使用该模型或接口",
                "1. 确认账号已开通该模型\n"
                "2. 换一个列表中的模型再试\n"
                "3. 检查是否需要充值或完成实名认证",
            )

        # 模型名错误
        if "400" in raw and "model" in low and "api key" not in low:
            m = re.search(r"passed (\S+)", raw)
            model_name = m.group(1) if m else "当前模型"
            return pack(
                f"模型「{model_name}」当前不可用",
                "1. 从下拉列表换一个该提供商支持的模型\n"
                "2. 或确认手动填写的模型名是否拼写正确",
            )

        if "429" in raw or "rate limit" in low or "too many requests" in low:
            return pack(
                "请求过于频繁，已被限流",
                "请稍等一会儿再试；若持续出现，可降低调用频率或升级套餐。",
            )

        if re.search(r"(?<![A-Za-z0-9_])timeout(?![A-Za-z0-9_])|\btimed out\b", low):
            return pack(
                "请求超时",
                "1. 检查本机网络 / 代理是否正常\n"
                "2. 若使用了自定义端点，确认地址可访问\n"
                "3. 稍后重试",
            )

        if any(x in low for x in ("connection", "connecterror", "namenor", "getaddrinfo", "network")):
            return pack(
                "无法连接到 AI 服务器",
                "1. 检查网络是否畅通\n"
                "2. 如需代理，请先配置系统或终端代理\n"
                "3. 自定义端点请确认填写正确（含 https://）",
            )

        if "ssl" in low or "certificate" in low:
            return pack(
                "安全连接（证书）校验失败",
                "多为网络中间设备或代理导致。可检查代理设置，或换网络后再试。",
            )

        if "token" in low and ("exceed" in low or "limit" in low or "too long" in low or "context" in low):
            return pack(
                "内容过长，超出模型限制",
                "1. 减少参考图片数量，或勾选「压缩图片」\n"
                "2. 缩短脚本描述文字后再生成",
            )

        if "500" in raw or "502" in raw or "503" in raw or "overloaded" in low:
            return pack(
                "AI 服务暂时不可用",
                "这是服务端问题，请稍后再试；也可换一个模型或提供商。",
            )

        if "空内容" in raw or "empty content" in low:
            advice = (
                "1. 请再点一次「生成脚本」重试\n"
                "2. 若使用 DeepSeek V4（如 deepseek-v4-flash）：默认会先「思考」再写代码，"
                "思考可能占满输出额度导致正文为空。程序已自动关闭思考模式，请重试\n"
                "3. 图片较多时可勾选「压缩图片」，或暂时取消「发送图片给 AI」只保留文件名\n"
                "4. 仍失败可换 deepseek-chat，或换 Claude / GPT"
            )
            if "思考模式" in raw or "had_reasoning" in raw or "finish_reason=length" in raw:
                advice = (
                    "这通常不是额度不足，而是模型把输出额度用在了「思考过程」上，正文还没写完就结束了。\n\n"
                    "1. 直接再生成一次（程序已对 DeepSeek 关闭 thinking）\n"
                    "2. 图片较多时可勾选「压缩图片」，或减少发送的参考图\n"
                    "3. 或改用 deepseek-chat / 其他提供商"
                )
            return pack("AI 返回了空结果", advice)

        if "校验失败" in raw or "语法错误" in raw or "修订后仍校验失败" in raw:
            return pack(
                "生成的脚本未通过本地检查",
                "若已有代码：优先到「4. 试运行」点「重修订」（生成页也有此按钮）；"
                "硬校验失败时会自动带上错误说明。\n"
                "也可再点「生成脚本」从零重试；若仍失败请简化脚本描述。",
            )

        # 未识别：给通用中文壳，仍附原始错误
        return pack(
            "连接或调用失败",
            "1. 用「连接测试」确认 Key / 模型 / 网络是否正常\n"
            "2. 对照下方原始错误排查（常见是 Key 填错或模型名不对）\n"
            "3. 仍无法解决时，可把原始错误发给开发者协助查看",
        )
    def _save_script(self):
        # 正式落盘前去掉试运行伪录制（确认保存 / 生成页保存共用）
        code = self._strip_pseudo_from_working_code()
        # 保存前语法闸门：剥离/手改后若语法坏了，绝不落盘（历史事故：存进去才发现 SyntaxError）
        if not self._code_compiles(code):
            try:
                compile(code, "<save>", "exec")
            except SyntaxError as e:
                QMessageBox.critical(
                    self,
                    "保存被阻止：语法错误",
                    f"当前代码第 {e.lineno} 行语法错误：{e.msg}\n\n"
                    "请在「查看完整代码」里修正，或点「重修订」后再保存。",
                )
            return
        name = self._script_name.text().strip()
        if not name:
            name = "my_script.py"
        if not name.endswith(".py"):
            name += ".py"

        out_dir = Path(self._output_dir.text().strip())
        out_dir.mkdir(parents=True, exist_ok=True)

        # 自动处理重名
        base = out_dir / name
        stem = name.rsplit(".py", 1)[0]
        counter = 1
        out_path = base
        while out_path.exists():
            out_path = out_dir / f"{stem} ({counter}).py"
            counter += 1

        try:
            out_path.write_text(code, encoding="utf-8")
            QMessageBox.information(self, "保存成功", f"已保存到:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def _copy_code(self):
        QApplication.clipboard().setText(self._generated_code)
        self._copy_btn.setText("已复制")
        QTimer.singleShot(2000, lambda: self._copy_btn.setText("复制代码"))
