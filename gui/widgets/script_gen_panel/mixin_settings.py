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
from core.path import IMG_PATH, PROJECT_ROOT, SCRIPTS_PATH

from gui.widgets.script_gen_panel.constants import (
    _DEFAULT_PROFILE, _KEYRING_SERVICE,
)
from gui.widgets.script_gen_panel.workers import ConnectionTestWorker
from gui.widgets.script_gen_panel.widgets import _NoWheelComboBox, _NoWheelSpinBox


class SettingsMixin:
    @staticmethod
    def _default_max_tokens() -> int:
        import json
        path = PROJECT_ROOT / "backend" / "script_generator" / "config.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            return int(cfg.get("defaults", {}).get("max_tokens", 16384))
        except Exception:
            return 16384

    @staticmethod
    def _default_codegen_free_mode() -> bool:
        import json
        path = PROJECT_ROOT / "backend" / "script_generator" / "config.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            return bool(cfg.get("defaults", {}).get("codegen_free_mode", False))
        except Exception:
            return False

    def _on_free_mode_toggled(self, checked: bool):
        """同步到 config.json，生成/修订共用。"""
        import json
        path = PROJECT_ROOT / "backend" / "script_generator" / "config.json"
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

    @staticmethod
    def _load_providers_config() -> dict:
        """从 config.json 加载提供商配置"""
        import json
        path = PROJECT_ROOT / "backend" / "script_generator" / "config.json"
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
            # 供协作页缓存：避免随后读控件偶发空值时误走本地兜底
            try:
                cached = self._collab_llm_params() if hasattr(self, "_collab_llm_params") else None
                if cached:
                    self._collab_llm_params_cache = dict(cached)
            except Exception:
                pass
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
