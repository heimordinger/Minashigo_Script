"""
蹇€熻剼鏈?鈥斺€?缃《鎮诞绐楁祴璇?鍚姩鍚庢樉绀轰竴涓疆椤舵偓娴獥锛屽寘鍚洃鎺с€佸綍鍒躲€侀鑹层€佽矾寰勬帶鍒躲€?"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from PySide6.QtWidgets import QApplication
from gui.widgets.QuickScriptOverlay import QuickScriptOverlay
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = QuickScriptOverlay()
    w.show()
    sys.exit(app.exec())
