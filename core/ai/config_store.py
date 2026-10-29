"""Independent API service, model and AI scheme storage with legacy migration."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


AI_CONFIG_PATH = Path.home() / ".minashigo" / "ai_config.json"
LEGACY_CONFIG_PATH = Path.home() / ".minashigo" / "script_gen_config.json"
KEYRING_SERVICE = "Minashigo_AI"
LEGACY_KEYRING_SERVICE = "Minashigo_ScriptGenerator"


def _id(text: str) -> str:
    value = re.sub(r"[^\w\-.]+", "_", str(text or "").strip(), flags=re.UNICODE).strip("_")
    return value or "default"


class AIConfigStore:
    def __init__(self, path: Path | str | None = None, legacy_path: Path | str | None = None):
        self.path = Path(path) if path else AI_CONFIG_PATH
        self.legacy_path = Path(legacy_path) if legacy_path else LEGACY_CONFIG_PATH
        self.ensure_migrated()

    @staticmethod
    def empty() -> dict[str, Any]:
        return {"version": 1, "providers": {}, "models": {}, "schemes": {}, "selections": {}}

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self.empty()
        except Exception:
            return self.empty()
        for key in ("providers", "models", "schemes", "selections"):
            data.setdefault(key, {})
        data.setdefault("version", 1)
        return data

    def write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_migrated(self) -> None:
        if self.path.is_file() or not self.legacy_path.is_file():
            return
        try:
            legacy = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except Exception:
            return
        profiles = legacy.get("profiles") if isinstance(legacy, dict) else None
        if not isinstance(profiles, dict):
            profiles = {"默认": legacy} if isinstance(legacy, dict) and legacy.get("model") else {}
        data = self.empty()
        for name, payload in profiles.items():
            if not isinstance(payload, dict):
                continue
            sid = _id(name)
            roles = {}
            for role, prefix, key_kind in (("main", "", "api_key"), ("vision", "vision_", "vision_api_key")):
                provider = str(payload.get(prefix + "provider") or "").strip()
                model = str(payload.get(prefix + "model") or "").strip()
                if not provider or not model:
                    continue
                provider_id = f"{sid}_{role}_api"
                model_id = f"{sid}_{role}_model"
                data["providers"][provider_id] = {
                    "name": f"{name}·{'主模型' if role == 'main' else '视觉模型'}",
                    "provider": provider,
                    "endpoint": str(payload.get(prefix + "endpoint") or ""),
                    "protocol": "openai_compatible",
                }
                data["models"][model_id] = {
                    "name": model,
                    "provider_id": provider_id,
                    "model": model,
                    "vision": role == "vision" or any(x in model.lower() for x in ("vl", "vision", "gpt-4o", "gemini", "claude")),
                }
                roles[role] = model_id
                self._migrate_key(str(name), key_kind, provider_id)
            data["schemes"][sid] = {
                "name": str(name),
                "roles": roles,
                "max_tokens": int(payload.get("max_tokens") or 0),
                "image_mode": str(payload.get("image_mode") or "direct"),
            }
        active = _id(str(legacy.get("active_profile") or ""))
        if active in data["schemes"]:
            data["selections"]["collaboration"] = active
        visual = next((k for k, v in data["schemes"].items() if (v.get("roles") or {}).get("vision")), "")
        if visual:
            data["selections"]["visual_agent"] = visual
        self.write(data)

    def _migrate_key(self, profile: str, kind: str, provider_id: str) -> None:
        try:
            import keyring
            safe = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", profile)
            old = keyring.get_password(LEGACY_KEYRING_SERVICE, f"profile:{safe}:{kind}")
            if not old and profile == "默认":
                old = keyring.get_password(LEGACY_KEYRING_SERVICE, kind)
            if old and not keyring.get_password(KEYRING_SERVICE, f"provider:{provider_id}"):
                keyring.set_password(KEYRING_SERVICE, f"provider:{provider_id}", old)
        except Exception:
            pass

    def set_provider_key(self, provider_id: str, key: str) -> None:
        if not key:
            return
        import keyring
        keyring.set_password(KEYRING_SERVICE, f"provider:{provider_id}", key)

    def provider_key(self, provider_id: str) -> str:
        try:
            import keyring
            return keyring.get_password(KEYRING_SERVICE, f"provider:{provider_id}") or ""
        except Exception:
            return ""

    def save_item(self, section: str, item_id: str, payload: dict[str, Any]) -> None:
        if section not in ("providers", "models", "schemes"):
            raise ValueError(section)
        data = self.read()
        data[section][_id(item_id)] = payload
        self.write(data)
        self.export_legacy()

    def delete_connection(self, model_id: str) -> bool:
        """Delete a model preset and safely remove its unused dependencies."""
        data = self.read()
        model_id = _id(model_id)
        model = data["models"].pop(model_id, None)
        if not model:
            return False
        provider_id = str(model.get("provider_id") or "")
        affected = {
            scheme_id for scheme_id, scheme in data["schemes"].items()
            if model_id in (scheme.get("roles") or {}).values()
        }
        for scheme_id in affected:
            data["schemes"].pop(scheme_id, None)
        for purpose, scheme_id in list(data["selections"].items()):
            if scheme_id in affected:
                data["selections"].pop(purpose, None)
        if provider_id and not any(
            str(item.get("provider_id") or "") == provider_id
            for item in data["models"].values()
        ):
            data["providers"].pop(provider_id, None)
            try:
                import keyring
                keyring.delete_password(KEYRING_SERVICE, f"provider:{provider_id}")
            except Exception:
                pass
        self.write(data)
        self.export_legacy()
        return True

    def select(self, purpose: str, scheme_id: str) -> None:
        data = self.read()
        if scheme_id not in data["schemes"]:
            raise KeyError(scheme_id)
        data["selections"][purpose] = scheme_id
        self.write(data)
        self.export_legacy()

    def selected(self, purpose: str) -> str:
        data = self.read()
        value = str(data["selections"].get(purpose) or "")
        if value in data["schemes"]:
            return value
        return next(iter(data["schemes"]), "")

    def resolve(self, scheme_id: str, purpose: str) -> dict[str, Any]:
        data = self.read()
        scheme = data["schemes"].get(scheme_id) or {}
        roles = scheme.get("roles") or {}
        role = "vision" if purpose == "visual_agent" and roles.get("vision") else "main"
        model_id = roles.get(role) or roles.get("main") or ""
        model = data["models"].get(model_id) or {}
        provider_id = str(model.get("provider_id") or "")
        provider = data["providers"].get(provider_id) or {}
        return {
            "scheme_id": scheme_id, "role": role,
            "provider": str(provider.get("provider") or ""),
            "endpoint": str(provider.get("endpoint") or ""),
            "model": str(model.get("model") or ""),
            "api_key": self.provider_key(provider_id),
            "max_tokens": int(scheme.get("max_tokens") or 0),
        }

    def export_legacy(self) -> None:
        """Compatibility projection for the current collaboration UI/runtime."""
        data = self.read()
        try:
            legacy = json.loads(self.legacy_path.read_text(encoding="utf-8")) if self.legacy_path.is_file() else {}
        except Exception:
            legacy = {}
        profiles = {}
        for sid, scheme in data["schemes"].items():
            roles = scheme.get("roles") or {}
            main = data["models"].get(roles.get("main") or "", {})
            vision = data["models"].get(roles.get("vision") or "", {})
            main_pid = str(main.get("provider_id") or "")
            vision_pid = str(vision.get("provider_id") or "")
            main_api = data["providers"].get(main_pid, {})
            vision_api = data["providers"].get(vision_pid, {})
            name = str(scheme.get("name") or sid)
            profiles[name] = {
                "provider": str(main_api.get("provider") or ""),
                "model": str(main.get("model") or ""),
                "endpoint": str(main_api.get("endpoint") or ""),
                "max_tokens": int(scheme.get("max_tokens") or 0),
                "vision_provider": str(vision_api.get("provider") or ""),
                "vision_model": str(vision.get("model") or ""),
                "vision_endpoint": str(vision_api.get("endpoint") or ""),
                "image_mode": str(scheme.get("image_mode") or "direct"),
            }
            self._export_key(name, "api_key", main_pid)
            self._export_key(name, "vision_api_key", vision_pid)
        legacy["profiles"] = profiles
        selected = data["selections"].get("collaboration")
        selected_name = str((data["schemes"].get(selected) or {}).get("name") or "")
        if not selected_name and profiles:
            selected_name = next(iter(profiles))
        legacy["active_profile"] = selected_name
        if selected_name in profiles:
            legacy.update(profiles[selected_name])
        self.legacy_path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")

    def _export_key(self, profile_name: str, kind: str, provider_id: str) -> None:
        if not provider_id:
            return
        key = self.provider_key(provider_id)
        if not key:
            return
        try:
            import keyring
            safe = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", profile_name)
            keyring.set_password(LEGACY_KEYRING_SERVICE, f"profile:{safe}:{kind}", key)
        except Exception:
            pass
