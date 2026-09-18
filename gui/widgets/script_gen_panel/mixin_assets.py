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

from gui.widgets.script_gen_panel.constants import _INTRO_FILENAMES
from gui.widgets.script_gen_panel.widgets import ImagePreviewDialog
from core.path import IMG_PATH


class AssetsMixin:
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
            # 介绍一般与素材同目录：自动导入父文件夹图片
            parent = path.parent
            if parent.is_dir() and (
                self._source_dir is None
                or Path(self._source_dir).resolve() != parent.resolve()
                or not self._image_entries
            ):
                self._import_folder_images(parent, recursive=True)
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))
        finally:
            self._expl_loading = False

    def _add_images(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="pick_file", root_path=str(IMG_PATH), multi_select=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        for p in dlg.selected_paths:
            self._append_image(Path(p))

    def _import_folder_images(self, root: Path, *, recursive: bool = True) -> int:
        # 扫描并导入素材夹图片；跳过 . 开头目录
        root = Path(root)
        self._source_dir = root
        self._on_image_mode_changed()
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
        self._update_img_label()
        return count

    def _add_folder(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="folders", root_path=str(IMG_PATH))
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        root = Path(dlg.selected_path)
        # 脚本生成素材默认含全部子目录（与白名单 / 识图一致）
        recursive = True if dlg.recursive is None else bool(dlg.recursive)
        self._maybe_bind_expl_file(root)
        if self._explanation.toPlainText().strip():
            try:
                self._persist_explanation()
            except Exception as e:
                print(f"[ScriptGenerator] 绑定介绍后保存失败: {e}")

        count = self._import_folder_images(root, recursive=recursive)
        if count == 0:
            QMessageBox.information(
                self,
                "无图片",
                f"文件夹内{'（含子文件夹）' if recursive else ''}未找到图片文件",
            )

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

    def _browse_output(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="folders", root_path=str(SCRIPTS_PATH))
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_path:
            self._output_dir.setText(dlg.selected_path)

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
