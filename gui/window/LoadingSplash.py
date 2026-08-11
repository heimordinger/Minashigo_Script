from pathlib import Path

from PySide6.QtWidgets import QWidget, QLabel, QApplication
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QMovie

from core.path import ICON_PATH, CONFIG_PATH


def _load_topmost_setting() -> bool:
    """读取 loading.topmost，默认 True；不依赖完整 config 模块。"""
    try:
        import json
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("loading", {}).get("topmost", True))
    except Exception:
        pass
    return True


class LoadingSplash(QWidget):
    """
    进程内 Loading Splash：显示窗口 + 播放 GIF。
    """

    def __init__(self, gif_path=None):
        super().__init__(None)
        self._stopped = False
        topmost = _load_topmost_setting()

        flags = Qt.FramelessWindowHint | Qt.Tool
        if topmost:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)

        path = Path(gif_path) if gif_path is not None else ICON_PATH.parent / "loading.gif"
        self.movie = QMovie(str(path))
        self.movie.setCacheMode(QMovie.CacheAll)
        self.label.setMovie(self.movie)

        self.resize(200, 200)
        self.show()
        QTimer.singleShot(0, self._start_movie)

    def _start_movie(self):
        if self._stopped:
            return
        self.movie.start()
        self.movie.frameChanged.connect(self._adjust_size_once)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center() - self.rect().center())

    def _adjust_size_once(self, _frame=None):
        size = self.movie.currentPixmap().size()
        if not size.isEmpty():
            self.resize(size)
            self.label.resize(size)
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.center() - self.rect().center())
            try:
                self.movie.frameChanged.disconnect(self._adjust_size_once)
            except (TypeError, RuntimeError):
                pass

    def stop(self):
        """与 AppStartup 兼容的关闭接口。"""
        if self._stopped:
            return
        self._stopped = True
        try:
            self.movie.stop()
        except Exception:
            pass
        self.close()


if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    splash = LoadingSplash()
    QTimer.singleShot(5000, splash.stop)
    sys.exit(app.exec())
