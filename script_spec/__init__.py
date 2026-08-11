"""脚本说明编辑器（与主工程隔离的独立包）。

运行:
  python/python311/Scripts/python.exe -m script_spec
"""

from script_spec.model import (
    ScriptSpec,
    ImageEntry,
    HelperSpec,
    TaskSpec,
    SceneRow,
    SpecIssue,
    list_images,
)

__all__ = [
    "ScriptSpec",
    "ImageEntry",
    "HelperSpec",
    "TaskSpec",
    "SceneRow",
    "SpecIssue",
    "list_images",
    "SpecEditor",
    "main",
]


def __getattr__(name: str):
    if name in ("SpecEditor", "main"):
        from script_spec.editor import SpecEditor, main
        return SpecEditor if name == "SpecEditor" else main
    raise AttributeError(name)
