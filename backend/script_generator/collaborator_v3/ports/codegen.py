"""Port for producing a script with the classic generate pipeline.

默认由主逻辑模型手写脚本；经典管线（plan → architecture → checklist →
few-shot → validate/fix）仅在用户明确要求传统状态机，或 LLM 多次校验失败后
作为备用，与协作 V2 的 `codegen_bridge.generate_from_logic` 同一条。
"""

from __future__ import annotations

from typing import Protocol


class ScriptCodeGenerator(Protocol):
    generator_id: str
    generator_version: str

    def generate(self, *, intro: str = "") -> tuple[str, str, list[str]]:
        """跑一整条出码管线。

        返回 ``(脚本文本, 脚本名, issues)``；``issues`` 非空表示没出码成功。
        """
        ...
