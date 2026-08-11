"""
快速脚本 —— 置顶悬浮窗测试
启动后显示一个置顶悬浮窗，包含拾取、录制、颜色、路径控制。
"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from PySide6.QtWidgets import QApplication
from gui.widgets.QuickScriptOverlay import QuickScriptOverlay

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = QuickScriptOverlay()
    w.show()
    sys.exit(app.exec())
