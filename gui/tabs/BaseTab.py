from PySide6.QtWidgets import QWidget, QVBoxLayout


class BaseTab(QWidget):
    def __init__(self, tab_id: str | None = None):
        super().__init__()
        self.tab_id = tab_id
        self.layout = QVBoxLayout(self)

    def render(self, state):
        pass
