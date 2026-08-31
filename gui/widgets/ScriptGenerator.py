"""
ScriptGenerator 面板
====================
用户配置 API、上传脚本解释和图片、生成自动化脚本。
"""

import asyncio
import re
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QCheckBox, QTextEdit,
    QLabel, QFileDialog, QSpinBox, QTabWidget, QSplitter,
    QMessageBox, QGroupBox, QProgressBar, QStyle,
    QApplication, QDialog, QScrollArea, QDialogButtonBox, QProgressDialog,
    QInputDialog,
)
from PySide6.QtGui import QFont, QPixmap, QKeySequence, QShortcut, QTextCursor, QTextDocument

from gui.widgets.GenTrajectory import GenTrajectory
from backend.script_generator.agent import generate_script, test_connection
from core.path import IMG_PATH, SCRIPTS_PATH

# 试运行写入 scripts/_trial/，由 TaskController 按 scripts._trial.* 加载
_TRIAL_REL = "_trial/_gen_trial.py"
_TRIAL_DIR = SCRIPTS_PATH / "_trial"
_INTRO_FILENAMES = ("脚本介绍.txt", "脚本解释.txt")
_KEYRING_SERVICE = "Minashigo_ScriptGenerator"
_DEFAULT_PROFILE = "默认"


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
        self._explanation_text: str = ""
        self._worker: GenerateWorker | None = None
        self._revise_worker: ReviseWorker | None = None
        self._facade = None
        self._trial_running = False
        self._trial_account_name: str = ""
        self._trial_log_lines: list[str] = []
        self._last_trial_frame = None
        self._stop_frame_path: str = ""
        self._last_revise_summary: str = ""
        self._last_diagnosis_json: str = ""
        self._chat_session: dict | None = None
        self._last_inp_tokens = 0
        self._last_out_tokens = 0
        self._trial_blocked = False
        self._trial_block_reason = ""
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
        outer.setSpacing(8)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("ScriptGenTabs")
        outer.addWidget(self._tabs)

        self._tabs.addTab(self._wrap_scroll(self._build_page_api()), "1. API 配置")
        self._tabs.addTab(self._wrap_scroll(self._build_page_input()), "2. 描述与素材")
        self._tabs.addTab(self._build_page_generate(), "3. 生成")
        self._tabs.addTab(self._build_page_trial(), "4. 试运行")

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
        self._profile_combo = QComboBox()
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

        self._provider = QComboBox()
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

        self._model = QComboBox()
        self._model.setEditable(True)
        self._model.setToolTip(
            "具体使用哪一个 AI 模型。可从列表选择，也可手动输入模型名。\n"
            "不同提供商的模型名不能混用。"
        )
        f.addRow("模型:", self._model)

        self._max_tokens = QSpinBox()
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

        self._vision_provider = QComboBox()
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

        self._vision_model = QComboBox()
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
            "仍跳过本地 codegen patch。勾选会写入 config.json defaults.codegen_free_mode。"
        )
        self._free_mode_cb.setChecked(self._default_codegen_free_mode())
        self._free_mode_cb.toggled.connect(self._on_free_mode_toggled)
        opt_row.addWidget(self._free_mode_cb)
        opt_row.addStretch()
        il.addLayout(opt_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("识图方式:"))
        self._image_mode = QComboBox()
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
        layout.setSpacing(10)

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

        self._progress = QProgressBar()
        self._progress.setObjectName("ScriptGenProgress")
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.hide()
        gen_row.addWidget(self._progress, 1)

        self._token_label = QLabel("")
        self._token_label.setObjectName("MutedLabel")
        gen_row.addWidget(self._token_label)
        layout.addLayout(gen_row)

        self._trajectory = GenTrajectory()
        layout.addWidget(self._trajectory, 1)

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
        to_trial.clicked.connect(lambda: self._tabs.setCurrentIndex(self.TAB_TRIAL))
        act_row.addWidget(to_trial)
        layout.addLayout(act_row)
        return page

    def _build_page_trial(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 0)
        layout.setSpacing(10)

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

        acc_row = QHBoxLayout()
        acc_row.addWidget(QLabel("账号:"))
        self._account_combo = QComboBox()
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
        layout.addWidget(splitter, 1)

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
        layout.addLayout(rev_row)
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
        recursive = dlg.recursive
        self._source_dir = root
        self._maybe_bind_expl_file(root)
        self._on_image_mode_changed()
        if self._explanation.toPlainText().strip():
            try:
                self._persist_explanation()
            except Exception as e:
                print(f"[ScriptGenerator] 绑定介绍后保存失败: {e}")

        it = root.rglob("*") if recursive else root.iterdir()
        count = 0
        for f in sorted(it):
            if f.is_file() and f.suffix.lower() in self.IMG_EXTENSIONS:
                self._append_image(f)
                count += 1
        if count == 0:
            QMessageBox.information(self, "无图片", f"文件夹内{'（含子文件夹）' if recursive else ''}未找到图片文件")

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
        self._progress.show()
        self._token_label.setText("")
        self._stream_buf = ""
        self._view_code_btn.setEnabled(False)
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._archive.begin("generate")
        self._trajectory.begin_run("开始生成")
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

    def _on_artifact(self, kind: str, payload: str):
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

    def _on_status(self, msg: str):
        if hasattr(self, "_trajectory"):
            self._trajectory.set_status_hint(msg or "")

    def _on_partial(self, text: str):
        if not hasattr(self, "_stream_buf"):
            self._stream_buf = ""
        self._stream_buf += text or ""
        # 流式过程中也可打开查看（看当前缓冲）
        if self._stream_buf.strip():
            self._view_code_btn.setEnabled(True)

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

    def _on_success(self, code: str):
        self._generated_code = code
        self._stream_buf = code or ""
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._revise_btn.setEnabled(True)
        self._confirm_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()
        self._sync_trial_code()
        self._update_trial_availability()
        self._trajectory.succeed_run("生成完成")
        self._flash_taskbar("脚本已生成")
        self._archive_generate_done(code)
        if getattr(self, "_vision_refresh_cb", None) and self._vision_refresh_cb.isChecked():
            self._vision_refresh_cb.setChecked(False)

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
        # 试跑结束/停止后异步截图到达时，补写停帧文件
        if not self._trial_running:
            try:
                self._persist_stop_frame()
            except Exception:
                pass

    def _archive_generate_done(self, code: str):
        try:
            from backend.script_generator.agent import validate_script_local
            expl = self._explanation.toPlainText()
            errs = validate_script_local(
                code,
                explanation=expl,
                source_dir=str(self._source_dir or ""),
                free_mode=bool(self._free_mode_cb.isChecked()),
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

    def _on_error(self, msg: str):
        translated = self._translate_error(msg)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()
        self._trajectory.fail_run(translated)
        QMessageBox.critical(self, "生成失败", translated)

    # ── 试运行 / 修订 ──

    def _update_trial_availability(self):
        has_code = bool((self._generated_code or "").strip())
        has_facade = self._facade is not None
        has_feedback = bool(self._feedback.toPlainText().strip())
        revise_busy = self._revise_worker is not None and self._revise_worker.isRunning()
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
            self._revise_btn.setEnabled(not self._trial_running and not revise_busy)
            self._confirm_btn.setEnabled(not self._trial_running)
        if hasattr(self, "_rerevise_btn"):
            self._rerevise_btn.setEnabled(
                has_code
                and (has_feedback or self._trial_blocked)
                and not revise_busy
                and not self._trial_running
            )

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

    def _refresh_accounts(self):
        current = self._account_combo.currentData()
        self._account_combo.clear()
        if self._facade is None:
            self._account_combo.addItem("（无主程序）", None)
            return
        accounts = list(self._facade.list_accounts() or [])
        ready_accounts = [acc for acc in accounts if self._account_ready(acc)]
        if not ready_accounts:
            self._account_combo.addItem("（暂无已启动账号）", None)
            if accounts:
                self._trial_hint.setText(
                    "列表里只显示已启动浏览器或已绑定窗口的账号。"
                    "请先在「开始」页启动后再点「刷新账号」。"
                )
            self._trial_btn.setEnabled(False)
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
            self._account_combo.addItem(f"{name}  · {suffix}", acc)
        if current:
            for i in range(self._account_combo.count()):
                data = self._account_combo.itemData(i)
                if isinstance(data, dict) and data.get("name") == current.get("name"):
                    self._account_combo.setCurrentIndex(i)
                    break
        # 有就绪账号时清掉「暂无」提示（无主程序提示由 _update_trial_availability 处理）
        if self._trial_hint.text().startswith("列表里只显示"):
            self._trial_hint.setText("")
        self._update_trial_availability()

    def _write_trial_file(self) -> Path:
        code = (self._generated_code or "").strip()
        if not code:
            raise RuntimeError("没有可试运行的代码")
        _TRIAL_DIR.mkdir(parents=True, exist_ok=True)
        init_py = _TRIAL_DIR / "__init__.py"
        if not init_py.exists():
            init_py.write_text("# trial package\n", encoding="utf-8")
        path = SCRIPTS_PATH / _TRIAL_REL
        path.write_text(code, encoding="utf-8")
        return path

    def _run_local_script_validate(self, *, log: bool = False) -> list[str]:
        """本地脚本检查：结构 + 素材（写入试运行文件后自动执行）。"""
        code = (self._generated_code or "").strip()
        if not code:
            self._trial_blocked = True
            self._trial_block_reason = "无代码"
            self._update_trial_availability()
            return ["无代码"]
        from backend.script_generator.agent import validate_script_local

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
        # 再试跑时反馈框若仍是上次内容 → 标成「陈旧」样式，提醒改写/清空
        if self._feedback.toPlainText().strip():
            self._set_feedback_stale(True)
        self._append_trial_log(f"[试运行] 临时文件: {path}")
        self._append_trial_log(f"[试运行] 账号: {name} · 模块: {_TRIAL_REL}")
        self._update_trial_availability()
        try:
            self._facade.start_task(account, _TRIAL_REL)
        except Exception as e:
            self._trial_running = False
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
            if status in ("finished", "stopped", "error", "idle"):
                self._trial_running = False
                # 自然结束/停止结束时再确保有停帧
                if not self._stop_frame_path:
                    self._cache_stop_frame_now(also_capture=True)
                else:
                    self._try_cache_trial_frame()
                    self._persist_stop_frame()
                self._update_trial_availability()
                self._append_trial_log(f"[试运行] 结束 ({status})")
                self._archive_trial_end(status)
        except Exception:
            pass

    def _on_revise(self):
        self._launch_revise(run_label="根据反馈修订")

    def _on_rerevise(self):
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._launch_revise(run_label="重修订")

    def _launch_revise(self, *, run_label: str = "根据反馈修订"):
        api_key = self._api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key")
            return
        feedback = self._feedback.toPlainText().strip()
        if not feedback and self._trial_blocked:
            reason = self._trial_block_reason or "硬校验未通过"
            feedback = f"修复本地校验错误：{reason}"
        if not feedback:
            QMessageBox.warning(
                self,
                "缺少反馈",
                "请先在试运行页填写反馈，或保留上次反馈后再修订。\n\n"
                "若刚因硬校验失败，可直接点「重修订」（会自动带上错误说明）。",
            )
            return
        if not (self._generated_code or "").strip():
            QMessageBox.warning(self, "无代码", "请先生成脚本。")
            return

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
        self._progress.show()
        self._stream_buf = ""
        self._view_code_btn.setEnabled(False)
        self._tabs.setCurrentIndex(self.TAB_GEN)
        self._trajectory.begin_run(run_label)
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
        self._view_code_btn.setEnabled(True)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()
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
        trial_blocked = self._trial_blocked
        val_errors = [self._trial_block_reason] if trial_blocked else []
        if trial_blocked:
            self._append_trial_log(
                "[修订] 脚本检查未通过，不可试运行。"
                + (f" ({self._trial_block_reason})" if self._trial_block_reason else "")
            )
        self._update_trial_availability()
        summary = (summary or "").strip() or "（无摘要）"
        self._append_trial_log("[修订摘要]\n" + summary)
        self._trajectory.succeed_run("修订完成")
        self._flash_taskbar("反馈修订完成")

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

    def _on_revise_error(self, msg: str):
        translated = self._translate_error(msg)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()
        self._update_trial_availability()
        self._trajectory.update_step(
            "revise",
            "Revise",
            "修订失败",
            status="error",
            body=translated,
        )
        self._trajectory.fail_run(translated)
        QMessageBox.critical(self, "修订失败", translated)

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
            "确定将当前脚本保存为正式文件吗？\n保存后可在脚本列表中使用。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._save_script()

    def _cancel_generate(self):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)
        if self._revise_worker and self._revise_worker.isRunning():
            self._revise_worker.terminate()
            self._revise_worker.wait(2000)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()
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

        if "timeout" in low or "timed out" in low:
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
            out_path.write_text(self._generated_code, encoding="utf-8")
            QMessageBox.information(self, "保存成功", f"已保存到:\n{out_path}")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def _copy_code(self):
        QApplication.clipboard().setText(self._generated_code)
        self._copy_btn.setText("已复制")
        QTimer.singleShot(2000, lambda: self._copy_btn.setText("复制代码"))
