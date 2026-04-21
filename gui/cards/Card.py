from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel, QSizePolicy

class Card(QFrame):
    def __init__(self, title=None, content=None, stretch=0, parent=None):
        super().__init__(parent)

        self.setFrameShape(QFrame.StyledPanel)

        layout = QVBoxLayout(self)

        if title:
            layout.addWidget(QLabel(title))

        if content:
            layout.addWidget(content)
        if stretch:
            self.setSizePolicy(
                QSizePolicy.Expanding,
                QSizePolicy.Expanding
            )
        else:
            self.setSizePolicy(
                QSizePolicy.Preferred,
                QSizePolicy.Maximum
            )

