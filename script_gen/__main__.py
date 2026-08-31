"""可直接运行: python -m script_gen

独立启动时没有主程序 facade，无法试运行账号任务；
请从主窗口状态栏「脚本生成」打开以使用试运行。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main():
    from PySide6.QtWidgets import QApplication
    from script_gen.window import ScriptGenWindow

    app = QApplication.instance() or QApplication(sys.argv)
    win = ScriptGenWindow.open(facade=None, parent=None)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
