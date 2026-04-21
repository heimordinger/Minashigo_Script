from PySide6.QtWidgets import QWidget, QLabel, QApplication
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QMovie

from core.path import ICON_PATH

class LoadingSplash(QWidget):
    """
    极速启动的 Loading Splash
    只负责：显示窗口 + 播放 GIF
    """

    def __init__(self, gif_path: str):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)

        self.movie = QMovie(str(gif_path))
        self.movie.setCacheMode(QMovie.CacheAll)
        self.label.setMovie(self.movie)

        self.resize(200, 200)

        self.show()

        QTimer.singleShot(0, self._start_movie)

    def _start_movie(self):

        self.movie.start()

        self.movie.frameChanged.connect(self._adjust_size_once)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center() - self.rect().center())

    def _adjust_size_once(self):
        size = self.movie.currentPixmap().size()
        if not size.isEmpty():
            self.resize(size)
            self.label.resize(size)
            self.movie.frameChanged.disconnect(self._adjust_size_once)

if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    splash = LoadingSplash(ICON_PATH.parent / "loading.gif")

    QTimer.singleShot(5000, splash.close)

    sys.exit(app.exec())

