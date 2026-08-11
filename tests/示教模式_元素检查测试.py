"""
UI 元素检查器 —— 加载截图后鼠标悬停，实时检测光标处的 UI 元素。
"""
import sys
from pathlib import Path

# 添加项目根到路径
root = Path(__file__).resolve().parents[1]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
from PySide6.QtWidgets import QApplication
from gui.widgets.ElementInspector import ElementInspector


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = ElementInspector()
    w.show()
    sys.exit(app.exec())
