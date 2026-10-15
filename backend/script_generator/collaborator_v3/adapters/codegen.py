"""Adapter: run the classic generate pipeline (协作 V2 的出码桥) for a V3 turn.

默认仍由主逻辑模型手写脚本；本适配器只在用户点名传统状态机，或 LLM 多次
validate_script 失败后由 Runtime 调用。复用经典 `generate_from_logic`：素材落盘、
介绍兜底（由 LogicGraph 摊平）、plan/架构/清单/few-shot/自校验。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


class V2CodeGenerator:
    generator_id = "local.v2_codegen_pipeline"
    generator_version = "1"

    def __init__(
        self,
        session: Any,
        *,
        provider: str,
        api_key: str,
        model: str,
        api_endpoint: Optional[str] = None,
        max_tokens: Optional[int] = None,
        vision_assist: Optional[dict] = None,
        on_status=None,
    ) -> None:
        self.session = session
        self.provider = str(provider or "")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.api_endpoint = api_endpoint
        self.max_tokens = max_tokens
        self.vision_assist = vision_assist
        self.on_status = on_status

    def generate(self, *, intro: str = "") -> tuple[str, str, list[str]]:
        text = str(intro or "").strip()
        if text:
            # 模型给了「生成用介绍」就用它；没给则由管线按 LogicGraph 兜底。
            try:
                self.session.intro_text = text
            except Exception:
                pass
        try:
            from backend.script_generator.collaborator.codegen_bridge import (
                generate_from_logic,
                script_name_for,
            )
        except Exception as exc:  # noqa: BLE001 - 桥接不可用要原样告诉上层
            return "", "", [f"经典生成器不可用：{exc}"]
        if not (self.provider and self.api_key and self.model):
            return "", "", ["经典生成器缺少模型参数（provider / api_key / model）"]
        try:
            result = asyncio.run(
                generate_from_logic(
                    session=self.session,
                    provider=self.provider,
                    api_key=self.api_key,
                    model=self.model,
                    api_endpoint=self.api_endpoint,
                    max_tokens=self.max_tokens,
                    vision_assist=self.vision_assist,
                    on_status=self.on_status,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 出码失败不该炸掉整个主轮
            return "", "", [f"经典生成器没跑通：{exc}"]
        data = result if isinstance(result, dict) else {}
        code = str(data.get("code") or "").strip()
        if not data.get("ok") or not code:
            return "", "", [f"经典生成器没跑通：{data.get('error') or '没有产出脚本'}"]
        name = str(data.get("script_name") or "").strip()
        if not name:
            try:
                name = script_name_for(self.session)
            except Exception:
                name = "collab_script.py"
        return code, name, []
