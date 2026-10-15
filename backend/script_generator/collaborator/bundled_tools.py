"""仓库内置的"自造工具"：从实况验证过的探针脚本固化而来。

这些工具原本只活在 ``user_data/collab_tools.json``（本机、不进版本库、换机器就没了）。
实测验证过的高价值工具（如 ``combined_mark`` 用过 31 次）应该进版本库，和内置工具
一起对所有人可用。

和模型当场自造的工具的区别：
* **内置**（本模块）：随仓库分发，不可删除，模型每轮都能看到；
* **自造**（``user_data/collab_tools.json``）：本机沉淀，可删。

两者的执行都走 ``probe_script`` 权限，行为一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUNDLED_ROOT = Path(__file__).resolve().parent / "bundled_tools"
_MANIFEST = BUNDLED_ROOT / "manifest.json"


@dataclass(frozen=True)
class BundledTool:
    name: str
    description: str
    parameters: dict[str, Any]
    code: str
    source_path: str
    uses_at_promotion: int = 0

    def as_tool_def(self) -> dict[str, Any]:
        """装成和自造工具同形的字典，便于统一消费。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
            "code": self.code,
            "bundled": True,
            "source_path": self.source_path,
            "uses_at_promotion": self.uses_at_promotion,
        }


def load_bundled_tools() -> list[BundledTool]:
    """读 manifest + 各自源码。文件缺失时跳过该条，不抛异常。"""
    if not _MANIFEST.is_file():
        return []
    try:
        data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[BundledTool] = []
    for item in data.get("tools") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        file_name = str(item.get("file") or "").strip()
        if not name or not file_name:
            continue
        path = BUNDLED_ROOT / file_name
        try:
            code = path.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append(
            BundledTool(
                name=name,
                description=str(item.get("description") or ""),
                parameters=item.get("parameters")
                if isinstance(item.get("parameters"), dict)
                else {"type": "object", "properties": {}},
                code=code,
                source_path=str(path),
                uses_at_promotion=int(item.get("uses_at_promotion") or 0),
            )
        )
    return out


def bundled_tool(name: str) -> BundledTool | None:
    target = str(name or "").strip()
    for tool in load_bundled_tools():
        if tool.name == target:
            return tool
    return None


def bundled_tool_names() -> set[str]:
    return {tool.name for tool in load_bundled_tools()}
