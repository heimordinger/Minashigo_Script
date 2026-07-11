"""
ScriptGenerator 面板
====================
用户配置 API、上传脚本解释和图片、生成自动化脚本。
"""

import asyncio
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QCheckBox, QTextEdit,
    QLabel, QFileDialog,
    QMessageBox, QGroupBox, QProgressBar,
    QApplication, QDialog, QScrollArea, QDialogButtonBox,
)
from PySide6.QtGui import QFont, QPixmap

from backend.script_generator.agent import generate_script
from core.path import IMG_PATH, SCRIPTS_PATH


# ═══════════════════════════════════════════════════════════════
# 图片预览弹窗
# ═══════════════════════════════════════════════════════════════

CELL_STYLE = "QGroupBox{border:1px solid #bbb;border-radius:4px;padding:6px;margin:2px}"
MAX_W, MAX_H = 280, 200


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

class GenerateWorker(QThread):
    finished = Signal(str)
    partial = Signal(str)  # 流式输出的片段
    token_info = Signal(int, int)  # 输入tokens, 输出tokens
    error = Signal(str)

    def __init__(self, params: dict):
        super().__init__()
        self.params = params

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.params["on_partial"] = lambda text: self.partial.emit(text)
            code, inp, out = loop.run_until_complete(generate_script(**self.params))
            self.token_info.emit(inp, out)
            self.finished.emit(code)
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
        self._image_entries: list[dict] = []
        self._source_dir: Path | None = None
        self._generated_code: str = ""
        self._explanation_text: str = ""
        self._worker: GenerateWorker | None = None
        self._settings_path = Path.home() / ".minashigo" / "script_gen_config.json"
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._build_ui()

    # ── UI 构建 ──

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(6)

        # ===== API 设置 =====
        api_group = QGroupBox("API 设置")
        g_api = QVBoxLayout(api_group)
        _al = QLabel("配置 AI 提供商，用于生成脚本代码")
        _al.setStyleSheet("color:#888;font-size:11px;")
        g_api.addWidget(_al)
        f = QFormLayout()
        g_api.addLayout(f)

        self._endpoint = QLineEdit()
        self._endpoint.setPlaceholderText("可选，默认使用官方 API")
        f.addRow("自定义端点:", self._endpoint)

        self._provider = QComboBox()
        f.addRow("提供商:", self._provider)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("sk-...")
        f.addRow("API Key:", self._api_key)

        self._model = QComboBox()
        self._model.setEditable(True)
        self._model.setEditable(True)
        f.addRow("模型:", self._model)

        self._provider_hint = QLabel()
        self._provider_hint.setStyleSheet("color:#e8a000;font-size:11px;")
        self._provider_hint.setVisible(False)
        self._provider_hint.setWordWrap(True)
        f.addRow(self._provider_hint)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("保存配置")
        save_btn.clicked.connect(self._save_settings)
        load_btn = QPushButton("加载配置")
        load_btn.clicked.connect(self._load_settings)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(load_btn)
        btn_row.addStretch()
        g_api.addLayout(btn_row)

        # 所有控件建好后再初始化数据+连信号
        self._providers_config = self._load_providers_config()
        self._provider.addItems(list(self._providers_config.keys()))
        self._provider.currentTextChanged.connect(self._on_provider_changed)
        self._refresh_models()

        layout.addWidget(api_group)

        # ===== 脚本描述 =====
        desc_group = QGroupBox("脚本描述")
        dl = QVBoxLayout(desc_group)
        _dl = QLabel("描述脚本的功能和流程逻辑，AI 将据此生成代码")
        _dl.setStyleSheet("color:#888;font-size:11px;")
        dl.addWidget(_dl)

        h = QHBoxLayout()
        self._load_expl_btn = QPushButton("加载 脚本解释.txt")
        self._load_expl_btn.clicked.connect(self._load_explanation)
        h.addWidget(self._load_expl_btn)
        h.addStretch()
        dl.addLayout(h)

        self._explanation = QTextEdit()
        self._explanation.setPlaceholderText("粘贴脚本解释内容，或点击上方加载 .txt 文件")
        self._explanation.setMaximumHeight(100)
        dl.addWidget(self._explanation)

        layout.addWidget(desc_group)

        # ===== 图片管理 =====
        img_group = QGroupBox("参考图片")
        il = QVBoxLayout(img_group)
        _il = QLabel("选择图片所在文件夹，生成脚本时自动引用此路径")
        _il.setStyleSheet("color:#888;font-size:11px;")
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
        self._img_label.setStyleSheet("color: #888; padding: 4px 0;")
        il.addWidget(self._img_label)

        opt_row = QHBoxLayout()
        self._send_img_cb = QCheckBox("发送图片给 AI")
        self._send_img_cb.setChecked(True)
        self._send_img_cb.toggled.connect(lambda on: self._compress_img_cb.setEnabled(on))
        opt_row.addWidget(self._send_img_cb)
        self._compress_img_cb = QCheckBox("压缩图片（省 token）")
        self._compress_img_cb.setChecked(False)
        self._compress_img_cb.setEnabled(True)
        opt_row.addWidget(self._compress_img_cb)
        opt_row.addStretch()
        il.addLayout(opt_row)

        layout.addWidget(img_group)

        # ===== 输出设置 =====
        out_group = QGroupBox("输出")
        og = QVBoxLayout(out_group)
        _ol = QLabel("生成的脚本保存位置和文件名")
        _ol.setStyleSheet("color:#888;font-size:11px;")
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

        # ===== 生成按钮 =====
        gen_row = QHBoxLayout()
        self._generate_btn = QPushButton("生成脚本")
        self._generate_btn.setStyleSheet("font-weight:bold; padding:6px 20px;")
        self._generate_btn.clicked.connect(self._on_generate)
        gen_row.addWidget(self._generate_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_generate)
        gen_row.addWidget(self._cancel_btn)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.hide()
        gen_row.addWidget(self._progress)

        self._token_label = QLabel("")
        self._token_label.setStyleSheet("color:#888;font-size:11px;")
        gen_row.addWidget(self._token_label)

        gen_row.addStretch()
        layout.addLayout(gen_row)

        # ===== 代码预览 =====
        layout.addWidget(QLabel("生成的脚本:"))
        self._code_preview = QTextEdit()
        self._code_preview.setReadOnly(True)
        self._code_preview.setFont(QFont("Consolas", 10))
        layout.addWidget(self._code_preview, 1)

        # ===== 操作按钮 =====
        act_row = QHBoxLayout()
        self._save_btn = QPushButton("保存到文件")
        self._save_btn.clicked.connect(self._save_script)
        self._save_btn.setEnabled(False)
        act_row.addWidget(self._save_btn)

        self._copy_btn = QPushButton("复制代码")
        self._copy_btn.clicked.connect(self._copy_code)
        self._copy_btn.setEnabled(False)
        act_row.addWidget(self._copy_btn)
        act_row.addStretch()
        layout.addLayout(act_row)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ── 持久化存储 ──

    def _save_settings(self):
        import json
        data = {
            "provider": self._provider.currentText(),
            "model": self._model.currentText().strip(),
            "endpoint": self._endpoint.text().strip(),
        }
        self._settings_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # API key 存入系统凭据管理器
        api_key = self._api_key.text().strip()
        if api_key:
            try:
                import keyring
                keyring.set_password("Minashigo_ScriptGenerator", "api_key", api_key)
            except ImportError:
                print("[ScriptGenerator] 未安装 keyring，API key 不会持久化。安装: pip install keyring")
            except Exception as e:
                print(f"[ScriptGenerator] keyring 存储失败: {e}")
        print(f"[ScriptGenerator] 配置已保存")

    def _load_settings(self):
        import json
        if not self._settings_path.exists():
            return
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception:
            return

        # 加载期间屏蔽信号，防止 provider 切换触发保存/重置
        self._provider.blockSignals(True)
        self._model.blockSignals(True)

        idx = self._provider.findText(data.get("provider", "claude"))
        if idx >= 0:
            self._provider.setCurrentIndex(idx)

        # provider 变了 → 手动刷新模型列表
        self._refresh_models()

        model = data.get("model", "")
        if model:
            self._model.setCurrentText(model)

        endpoint = data.get("endpoint", "")
        if endpoint:
            self._endpoint.setText(endpoint)

        self._provider.blockSignals(False)
        self._model.blockSignals(False)

        # API key 从系统凭据管理器读取
        try:
            import keyring
            api_key = keyring.get_password("Minashigo_ScriptGenerator", "api_key")
            if api_key:
                self._api_key.setText(api_key)
        except ImportError:
            pass
        except Exception as e:
            print(f"[ScriptGenerator] keyring 读取失败: {e}")

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

    def _refresh_models(self):
        print(f"[ScriptGenerator] 切换提供商: {self._provider.currentText()}")
        self._model.clear()
        self._model.setEditText("")
        p = self._provider.currentText()
        info = self._providers_config.get(p, {})
        models = info.get("models", [])
        if models:
            self._model.addItems(models)
        endpoint = info.get("default_endpoint", "")
        if endpoint and not self._endpoint.text().strip():
            self._endpoint.setText(endpoint)
        self._model.setCurrentIndex(0)

    def _on_provider_changed(self, provider: str):
        print(f"[ScriptGenerator] 提供商变更为: {provider}")
        try:
            self._save_settings()
        except Exception as e:
            print(f"[ScriptGenerator] 保存配置失败: {e}")
        info = self._providers_config.get(provider, {})
        hint = info.get("hint", "")
        if hint:
            self._provider_hint.setText(hint)
            self._provider_hint.setVisible(True)
        else:
            self._provider_hint.setVisible(False)
        try:
            self._refresh_models()
        except Exception as e:
            print(f"[ScriptGenerator] 刷新模型列表失败: {e}")
            import traceback
            traceback.print_exc()

    # ── 脚本描述 ──

    def _load_explanation(self):
        from gui.widgets.ResourcePicker import ResourcePickerDialog
        dlg = ResourcePickerDialog(self, mode="pick_file", root_path=str(IMG_PATH))
        if dlg.exec() != QDialog.Accepted or not dlg.selected_path:
            return
        try:
            text = Path(dlg.selected_path).read_text(encoding="utf-8")
            self._explanation.setText(text)
            self._explanation_text = text
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))

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

        params = {
            "provider": self._provider.currentText(),
            "api_key": api_key,
            "model": self._model.currentText().strip(),
            "api_endpoint": self._endpoint.text().strip() or None,
            "explanation_text": expl_text,
            "image_paths": [e["path"] for e in self._image_entries],
            "source_dir": str(self._source_dir) if self._source_dir else "",
            "send_images": self._send_img_cb.isChecked(),
            "compress_images": self._compress_img_cb.isChecked(),
        }

        self._generate_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._progress.show()
        self._token_label.setText("")
        self._code_preview.setText("⏳ thinking...")

        self._worker = GenerateWorker(params)
        self._worker.finished.connect(self._on_success)
        self._worker.partial.connect(self._on_partial)
        self._worker.token_info.connect(self._on_token_info)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_token_info(self, inp: int, out: int):
        if inp or out:
            self._token_label.setText(f"↕ {inp} 入 / {out} 出")

    def _on_partial(self, text: str):
        if self._code_preview.toPlainText().startswith("⏳"):
            self._code_preview.clear()
        self._code_preview.insertPlainText(text)
        self._code_preview.verticalScrollBar().setValue(
            self._code_preview.verticalScrollBar().maximum()
        )

    def _cancel_generate(self):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(2000)
        self._code_preview.setText("已取消")
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()

    def _on_success(self, code: str):
        self._generated_code = code
        self._code_preview.setText(code)
        self._save_btn.setEnabled(True)
        self._copy_btn.setEnabled(True)
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()

    def _on_error(self, msg: str):
        translated = self._translate_error(msg)
        self._code_preview.setText(f"生成失败:\n{translated}")
        self._generate_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress.hide()
        QMessageBox.critical(self, "生成失败", translated)

    @staticmethod
    def _translate_error(msg: str) -> str:
        import re
        # API key 无效
        if "401" in msg and "authentication" in msg.lower():
            return "API Key 无效或已过期，请检查后重新填写并保存"
        # 模型名错误
        if "400" in msg and "model" in msg.lower():
            m = re.search(r"passed (\S+)", msg)
            model_name = m.group(1) if m else "当前模型"
            return f"模型「{model_name}」不被当前提供商支持，请切换提供商或更换模型"
        # 速率限制
        if "429" in msg or "rate limit" in msg.lower():
            return "请求过于频繁，请稍后再试"
        # 超时
        if "timeout" in msg.lower() or "timed out" in msg.lower():
            return "请求超时，可能是网络问题或服务端响应慢，请重试"
        # token 超限
        if "token" in msg.lower() and ("exceed" in msg.lower() or "limit" in msg.lower()):
            return "内容过长，超过了模型的最大 token 限制，请减少图片或描述文本"
        # 服务器错误
        if "500" in msg or "502" in msg or "503" in msg:
            return "AI 服务暂时不可用，请稍后重试"
        # 无内容
        if "空内容" in msg:
            return "AI 返回了空结果，请重试一次"
        return msg

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
