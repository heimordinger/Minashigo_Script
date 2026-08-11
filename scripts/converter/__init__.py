# scripts/converter/__init__.py
"""Python 脚本 -> TaskFlow 节点图转换器。

用法:
    from scripts.converter import convert_script
    graph = convert_script("scripts/DeepOne/DO日常.py")
"""

from .ir import ActionNode, GuardRef, Loop, ScriptIR
from .parser import ScriptParser
from .renderer import LiteGraphRenderer


def convert_script(source_path: str) -> dict:
    """读取一个 Python 脚本，返回 LiteGraph JSON dict。

    Args:
        source_path: 脚本文件路径

    Returns:
        LiteGraph JSON（可直接写入 workflow.json）
    """
    parser = ScriptParser(source_path)
    ir = parser.parse()
    renderer = LiteGraphRenderer(ir)
    return renderer.render()
