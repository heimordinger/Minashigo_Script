"""Shared scheme facade over the existing Script Generator profiles.

The legacy profile file remains the source of truth for now.  This module gives
all AI features one stable API and preserves the existing keyring slots.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal


SchemePurpose = Literal["collaboration", "visual_agent", "general"]
_DEFAULT_PATH = Path.home() / ".minashigo" / "script_gen_config.json"
_KEYRING_SERVICE = "Minashigo_ScriptGenerator"
_DEFAULT_SCHEME = "默认"


def _slot(name: str, kind: str) -> str:
    safe = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", (name or _DEFAULT_SCHEME).strip())
    return f"profile:{safe}:{kind}"


def _looks_visual(model: str) -> bool:
    value = str(model or "").lower()
    return any(token in value for token in ("vision", "-vl", "vl-", "gpt-4o", "gpt-4.1", "gemini", "claude"))


@dataclass(frozen=True)
class AIScheme:
    id: str
    name: str
    main_provider: str
    main_model: str
    main_endpoint: str
    vision_provider: str
    vision_model: str
    vision_endpoint: str
    max_tokens: int
    image_mode: str
    supports_vision: bool

    @classmethod
    def from_profile(cls, name: str, payload: dict[str, Any]) -> "AIScheme":
        main_model = str(payload.get("model") or "").strip()
        vision_model = str(payload.get("vision_model") or "").strip()
        return cls(
            id=name,
            name=name,
            main_provider=str(payload.get("provider") or "").strip(),
            main_model=main_model,
            main_endpoint=str(payload.get("endpoint") or "").strip(),
            vision_provider=str(payload.get("vision_provider") or "").strip(),
            vision_model=vision_model,
            vision_endpoint=str(payload.get("vision_endpoint") or "").strip(),
            max_tokens=int(payload.get("max_tokens") or 0),
            image_mode=str(payload.get("image_mode") or "direct"),
            supports_vision=bool(vision_model or _looks_visual(main_model)),
        )


@dataclass(frozen=True)
class ResolvedModel:
    scheme_id: str
    provider: str
    model: str
    endpoint: str
    api_key: str
    max_tokens: int
    role: Literal["main", "vision"]

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.model and self.api_key)

    def as_runtime_params(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "max_tokens": self.max_tokens,
        }


class AISchemeStore:
    """Read shared AI schemes and remember a scheme per feature."""

    def __init__(self, path: Path | str | None = None):
        self._central = None
        if path is None:
            from .config_store import AIConfigStore
            self._central = AIConfigStore()
        self.path = Path(path) if path is not None else _DEFAULT_PATH

    def _central_schemes(self) -> list[AIScheme]:
        data = self._central.read()
        result = []
        for sid, raw in data["schemes"].items():
            roles = raw.get("roles") or {}
            main = data["models"].get(roles.get("main") or "", {})
            vision = data["models"].get(roles.get("vision") or "", {})
            main_api = data["providers"].get(main.get("provider_id") or "", {})
            vision_api = data["providers"].get(vision.get("provider_id") or "", {})
            result.append(AIScheme(
                id=sid, name=str(raw.get("name") or sid),
                main_provider=str(main_api.get("provider") or ""),
                main_model=str(main.get("model") or ""),
                main_endpoint=str(main_api.get("endpoint") or ""),
                vision_provider=str(vision_api.get("provider") or ""),
                vision_model=str(vision.get("model") or ""),
                vision_endpoint=str(vision_api.get("endpoint") or ""),
                max_tokens=int(raw.get("max_tokens") or 0),
                image_mode=str(raw.get("image_mode") or "direct"),
                supports_vision=bool(vision.get("vision") or main.get("vision")),
            ))
        return result

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _profiles(self, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        profiles = data.get("profiles")
        if isinstance(profiles, dict) and profiles:
            return {str(k): v for k, v in profiles.items() if isinstance(v, dict)}
        # Compatibility with the oldest flat configuration.
        payload = {
            key: data.get(key, "")
            for key in (
                "provider", "model", "endpoint", "max_tokens",
                "vision_provider", "vision_model", "vision_endpoint", "image_mode",
            )
        }
        return {_DEFAULT_SCHEME: payload} if any(payload.values()) else {}

    def list_schemes(self, purpose: SchemePurpose = "general") -> list[AIScheme]:
        if self._central is not None:
            schemes = self._central_schemes()
            if purpose == "visual_agent":
                schemes = [item for item in schemes if item.supports_vision]
            return sorted(schemes, key=lambda item: item.name)
        schemes = [
            AIScheme.from_profile(name, payload)
            for name, payload in self._profiles(self._read()).items()
        ]
        if purpose == "visual_agent":
            schemes = [scheme for scheme in schemes if scheme.supports_vision]
        return sorted(schemes, key=lambda item: (item.name != _DEFAULT_SCHEME, item.name))

    def get(self, scheme_id: str) -> AIScheme | None:
        if self._central is not None:
            return next((item for item in self._central_schemes() if item.id == scheme_id), None)
        payload = self._profiles(self._read()).get(str(scheme_id or ""))
        return AIScheme.from_profile(str(scheme_id), payload) if payload else None

    def selected_id(self, purpose: SchemePurpose) -> str:
        if self._central is not None:
            return self._central.selected(purpose)
        data = self._read()
        selected = data.get("feature_schemes") or {}
        value = str(selected.get(purpose) or "").strip() if isinstance(selected, dict) else ""
        if self.get(value):
            return value
        if purpose == "collaboration":
            active = str(data.get("active_profile") or "").strip()
            if self.get(active):
                return active
        schemes = self.list_schemes(purpose)
        return schemes[0].id if schemes else ""

    def select(self, purpose: SchemePurpose, scheme_id: str) -> None:
        if self._central is not None:
            self._central.select(purpose, scheme_id)
            return
        if self.get(scheme_id) is None:
            raise KeyError(f"AI 方案不存在: {scheme_id}")
        data = self._read()
        selected = data.setdefault("feature_schemes", {})
        selected[purpose] = scheme_id
        if purpose == "collaboration":
            data["active_profile"] = scheme_id
        self._write(data)

    def resolve(self, scheme_id: str, purpose: SchemePurpose) -> ResolvedModel:
        if self._central is not None:
            value = self._central.resolve(scheme_id, purpose)
            return ResolvedModel(
                scheme_id=scheme_id, provider=value["provider"], model=value["model"],
                endpoint=value["endpoint"], api_key=value["api_key"],
                max_tokens=value["max_tokens"], role=value["role"],
            )
        scheme = self.get(scheme_id)
        if scheme is None:
            raise KeyError(f"AI 方案不存在: {scheme_id}")
        use_vision = purpose == "visual_agent" and bool(scheme.vision_model)
        role = "vision" if use_vision else "main"
        provider = scheme.vision_provider if use_vision else scheme.main_provider
        model = scheme.vision_model if use_vision else scheme.main_model
        endpoint = scheme.vision_endpoint if use_vision else scheme.main_endpoint
        kind = "vision_api_key" if use_vision else "api_key"
        api_key = ""
        try:
            import keyring

            api_key = keyring.get_password(_KEYRING_SERVICE, _slot(scheme.id, kind)) or ""
            if not api_key and scheme.id == _DEFAULT_SCHEME:
                api_key = keyring.get_password(_KEYRING_SERVICE, kind) or ""
        except Exception:
            api_key = ""
        return ResolvedModel(
            scheme_id=scheme.id,
            provider=provider,
            model=model,
            endpoint=endpoint,
            api_key=api_key,
            max_tokens=scheme.max_tokens,
            role=role,
        )
