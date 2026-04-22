from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PySide6.QtCore import Signal


class TaskOpBar(QWidget):
    add_clicked = Signal()
    start_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QHBoxLayout(self)

        self.add_btn = QPushButton("新增任务")
        self.start_btn = QPushButton("运行任务")

        layout.addWidget(self.add_btn)
        layout.addWidget(self.start_btn)
        layout.addStretch()

        self.add_btn.clicked.connect(self.add_clicked.emit)
        self.start_btn.clicked.connect(self.start_clicked.emit)
