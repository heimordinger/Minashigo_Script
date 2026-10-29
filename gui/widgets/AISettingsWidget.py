"""SillyTavern-style AI connection settings."""
from __future__ import annotations

import asyncio
import re

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from core.ai.config_store import AIConfigStore


def _id(text: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", text.strip(), flags=re.UNICODE).strip("_") or "connection"


class ModelListWorker(QThread):
    """Load the model catalogue before a model has been selected."""
    finished = Signal(dict)

    def __init__(self, api_key: str, endpoint: str):
        super().__init__()
        self.api_key, self.endpoint = api_key, endpoint

    def run(self):
        async def _load() -> list[str]:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.endpoint.rstrip("/"))
            try:
                page = await client.models.list()
                return sorted(
                    {str(x.id).strip() for x in page.data if getattr(x, "id", None)},
                    key=str.casefold,
                )
            finally:
                result = client.close()
                if asyncio.iscoroutine(result):
                    await result

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            models = loop.run_until_complete(_load())
            self.finished.emit({"ok": True, "models": models, "error": ""})
        except Exception as exc:
            self.finished.emit({"ok": False, "models": [], "error": str(exc)})
        finally:
            loop.close()


class AISettingsWidget(QWidget):
    PROVIDERS = (
        ("OpenAI", "openai", "https://api.openai.com/v1"),
        ("DeepSeek", "deepseek", "https://api.deepseek.com"),
        ("Qwen / DashScope", "qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1"),
        ("Google（OpenAI 兼容）", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
        ("自定义 OpenAI 兼容", "custom", ""),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = AIConfigStore()
        self._loading = False
        self._connected = False
        self._model_worker: ModelListWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 12, 20, 20)
        root.setSpacing(10)
        title = QLabel("AI 连接")
        title.setStyleSheet("font-size:20px;font-weight:600;")
        root.addWidget(title)
        tip = QLabel("先连接 API 获取可用模型，再选择模型并保存。一个连接预设对应一个可直接使用的模型。")
        tip.setObjectName("MutedLabel")
        tip.setWordWrap(True)
        root.addWidget(tip)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("连接预设"))
        self.preset = QComboBox()
        self.preset.setMinimumWidth(320)
        self.preset.currentIndexChanged.connect(self._load_preset)
        preset_row.addWidget(self.preset, 1)
        self.edit_btn = QPushButton("编辑")
        self.edit_btn.clicked.connect(self._edit_preset)
        preset_row.addWidget(self.edit_btn)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.clicked.connect(self._delete_preset)
        preset_row.addWidget(self.delete_btn)
        self.new_btn = QPushButton("新建连接")
        self.new_btn.clicked.connect(self._new_preset)
        preset_row.addWidget(self.new_btn)
        root.addLayout(preset_row)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        root.addWidget(line)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.name = QLineEdit()
        self.name.setPlaceholderText("例如：DeepSeek 日常对话")
        form.addRow("预设名称", self.name)
        self.provider = QComboBox()
        for label, value, endpoint in self.PROVIDERS:
            self.provider.addItem(label, (value, endpoint))
        self.provider.currentIndexChanged.connect(self._provider_changed)
        form.addRow("API 来源", self.provider)
        self.endpoint = QLineEdit()
        self.endpoint.setPlaceholderText("OpenAI 兼容接口的根地址，通常以 /v1 结尾")
        self.endpoint.textEdited.connect(self._connection_edited)
        form.addRow("服务器地址", self.endpoint)

        key_row = QHBoxLayout()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("输入 API Key；留空时使用已保存的 Key")
        self.api_key.textEdited.connect(self._connection_edited)
        key_row.addWidget(self.api_key, 1)
        self.show_key = QCheckBox("显示")
        self.show_key.toggled.connect(lambda on: self.api_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        key_row.addWidget(self.show_key)
        form.addRow("API Key", key_row)

        connect_row = QHBoxLayout()
        self.connect_btn = QPushButton("连接并获取模型")
        self.connect_btn.setObjectName("PrimaryButton")
        self.connect_btn.clicked.connect(self._fetch_models)
        connect_row.addWidget(self.connect_btn)
        self.status = QLabel("● 未连接")
        self.status.setStyleSheet("color:#888;")
        connect_row.addWidget(self.status)
        connect_row.addStretch()
        form.addRow("", connect_row)

        model_row = QHBoxLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setEnabled(False)
        self.model.setInsertPolicy(QComboBox.NoInsert)
        self.model.lineEdit().setPlaceholderText("连接后从服务端选择模型")
        model_row.addWidget(self.model, 1)
        self.manual = QCheckBox("手动填写")
        self.manual.toggled.connect(self._manual_toggled)
        model_row.addWidget(self.manual)
        form.addRow("模型", model_row)
        self.vision = QCheckBox("该模型支持图片输入（视觉模型）")
        form.addRow("模型能力", self.vision)
        root.addLayout(form)

        hint = QLabel("服务端不提供模型列表时，可启用“手动填写”。是否支持图片由服务商决定，请按实际能力勾选。")
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self._cancel_edit)
        actions.addWidget(self.cancel_btn)
        self.save_btn = QPushButton("保存连接预设")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.clicked.connect(self._save_preset)
        actions.addWidget(self.save_btn)
        root.addLayout(actions)
        root.addStretch(1)
        self.reload()

    def reload(self, selected_model_id: str | None = None):
        data = self.store.read()
        old = selected_model_id or self.preset.currentData()
        self._loading = True
        self.preset.clear()
        for model_id, model in data["models"].items():
            api = data["providers"].get(str(model.get("provider_id") or ""), {})
            label = str(model.get("name") or model.get("model") or model_id)
            source = str(api.get("name") or api.get("provider") or "")
            self.preset.addItem(f"{label}  ·  {source}" if source else label, model_id)
        index = self.preset.findData(old)
        self.preset.setCurrentIndex(index if index >= 0 else (0 if self.preset.count() else -1))
        self._loading = False
        self._load_preset()

    def _load_preset(self):
        if self._loading:
            return
        data = self.store.read()
        model = data["models"].get(str(self.preset.currentData() or ""), {})
        if not model:
            self._new_preset()
            return
        provider_id = str(model.get("provider_id") or "")
        api = data["providers"].get(provider_id, {})
        self._loading = True
        self.name.setText(str(model.get("name") or model.get("model") or ""))
        value = str(api.get("provider") or "custom")
        idx = next((i for i in range(self.provider.count()) if self.provider.itemData(i)[0] == value), -1)
        self.provider.setCurrentIndex(idx if idx >= 0 else self.provider.count() - 1)
        self.endpoint.setText(str(api.get("endpoint") or ""))
        self.api_key.clear()
        self.api_key.setPlaceholderText("已保存 API Key；留空保持不变" if self.store.provider_key(provider_id) else "输入 API Key")
        current = str(model.get("model") or "")
        self.model.clear()
        self.model.addItem(current)
        self.model.setCurrentText(current)
        self.model.setEnabled(True)
        self.manual.setChecked(False)
        self.vision.setChecked(bool(model.get("vision")))
        self._loading = False
        self._connected = False
        self._set_status("saved", "已保存；点击“编辑”可修改")
        self._set_editing(False)

    def _new_preset(self):
        self._loading = True
        self.preset.setCurrentIndex(-1)
        self.name.clear()
        self.provider.setCurrentIndex(0)
        self.endpoint.setText(self.PROVIDERS[0][2])
        self.api_key.clear()
        self.api_key.setPlaceholderText("输入 API Key")
        self.model.clear()
        self.model.setEnabled(False)
        self.manual.setChecked(False)
        self.vision.setChecked(False)
        self._loading = False
        self._connected = False
        self._set_status("idle", "新建连接：请填写信息并获取模型")
        self._set_editing(True)

    def _edit_preset(self):
        if not self.preset.currentData():
            self._new_preset()
            return
        self._set_editing(True)
        self.name.setFocus()
        self._set_status("loading", "正在编辑；连接信息改变后请重新获取模型")

    def _cancel_edit(self):
        if self.preset.currentData():
            self._load_preset()
        else:
            self.reload()

    def _delete_preset(self):
        model_id = str(self.preset.currentData() or "")
        if not model_id:
            return
        name = self.name.text().strip() or self.preset.currentText()
        answer = QMessageBox.question(
            self,
            "删除连接预设",
            f"确定删除“{name}”吗？\n\n使用它的旧方案也会一并移除；API Key 仅在没有其他预设使用时删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if self.store.delete_connection(model_id):
            self.reload()
            self._set_status("idle", "连接预设已删除")

    def _set_editing(self, editing: bool):
        for widget in (
            self.name, self.provider, self.endpoint, self.api_key, self.show_key,
            self.connect_btn, self.manual, self.vision,
        ):
            widget.setEnabled(editing)
        self.model.setEnabled(editing and (
            self._connected or self.manual.isChecked() or bool(self.model.currentText().strip())
        ))
        self.save_btn.setEnabled(editing)
        self.cancel_btn.setVisible(editing)
        has_preset = bool(self.preset.currentData())
        self.edit_btn.setEnabled(has_preset and not editing)
        self.delete_btn.setEnabled(has_preset and not editing)
        self.preset.setEnabled(not editing)
        self.new_btn.setEnabled(not editing)

    def _provider_changed(self):
        if self._loading:
            return
        _, default_endpoint = self.provider.currentData()
        self.endpoint.setText(default_endpoint)
        self._connection_edited()

    def _connection_edited(self, *_):
        if self._loading:
            return
        self._connected = False
        if not self.manual.isChecked():
            self.model.setEnabled(False)
        self._set_status("idle", "连接信息已改变，请重新连接")

    def _manual_toggled(self, checked: bool):
        self.model.setEnabled(checked or self._connected or bool(self.preset.currentData()))
        if checked:
            self.model.setFocus()

    def _provider_id(self) -> str:
        model = self.store.read()["models"].get(str(self.preset.currentData() or ""), {})
        return str(model.get("provider_id") or "")

    def _effective_key(self) -> str:
        return self.api_key.text().strip() or self.store.provider_key(self._provider_id())

    def _fetch_models(self):
        if self._model_worker and self._model_worker.isRunning():
            return
        key, endpoint = self._effective_key(), self.endpoint.text().strip()
        if not key:
            QMessageBox.information(self, "AI 连接", "请先填写 API Key。")
            return
        if not endpoint:
            QMessageBox.information(self, "AI 连接", "请填写服务器地址。")
            return
        self.connect_btn.setEnabled(False)
        self._set_status("loading", "正在连接并读取模型列表…")
        self._model_worker = ModelListWorker(key, endpoint)
        self._model_worker.finished.connect(self._models_loaded)
        self._model_worker.start()

    def _models_loaded(self, result: dict):
        self.connect_btn.setEnabled(True)
        if not result.get("ok"):
            self._connected = False
            self._set_status("error", "连接失败")
            QMessageBox.warning(self, "无法获取模型列表", f"{result.get('error') or '未知错误'}\n\n若服务不支持模型列表，可勾选“手动填写”。")
            return
        models = list(result.get("models") or [])
        previous = self.model.currentText().strip()
        self.model.clear()
        self.model.addItems(models)
        if previous in models:
            self.model.setCurrentText(previous)
        self.model.setEnabled(True)
        self._connected = True
        self._set_status("ok", f"连接成功，已获取 {len(models)} 个模型")
        if not models:
            self.manual.setChecked(True)
            QMessageBox.information(self, "AI 连接", "连接成功，但服务端未返回模型；请手动填写模型标识。")

    def _save_preset(self):
        name, model_name = self.name.text().strip(), self.model.currentText().strip()
        endpoint = self.endpoint.text().strip()
        provider_name, _ = self.provider.currentData()
        if not name or not model_name or not endpoint:
            QMessageBox.information(self, "AI 连接", "请填写预设名称、服务器地址并选择模型。")
            return
        existing_model_id = str(self.preset.currentData() or "")
        existing = self.store.read()["models"].get(existing_model_id, {})
        provider_id = str(existing.get("provider_id") or f"{_id(name)}_api")
        model_id = existing_model_id or f"{_id(name)}_model"
        self.store.save_item("providers", provider_id, {
            "name": name, "provider": provider_name, "endpoint": endpoint,
            "protocol": "openai_compatible",
        })
        if self.api_key.text().strip():
            self.store.set_provider_key(provider_id, self.api_key.text().strip())
        self.store.save_item("models", model_id, {
            "name": name, "provider_id": provider_id, "model": model_name,
            "vision": self.vision.isChecked(),
        })
        self.reload(model_id)
        self._set_status("ok", "连接预设已保存")

    def _set_status(self, kind: str, text: str):
        color = {"idle": "#888", "saved": "#777", "loading": "#c58a00", "ok": "#159447", "error": "#d64545"}.get(kind, "#888")
        self.status.setStyleSheet(f"color:{color};")
        self.status.setText(f"● {text}")
