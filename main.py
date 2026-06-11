# main.py

def main():
    print("正在加载……")

    from models.resource_manager import ensure_all_models
    ensure_all_models()

    print("开始运行程序,请稍等")

    import sys
    from core.path import ICON_PATH
    from PySide6.QtWidgets import QApplication
    from gui.window.LoadingSplash import LoadingSplash
    app = QApplication(sys.argv)
    splash = LoadingSplash(ICON_PATH.parent / "loading.gif")

    from core.config.config import config
    config.load()

    from controller.ctrl import Controller
    controller = Controller()

    from gui.facade_impl import FacadeImpl
    facade = FacadeImpl(controller)

    from gui.window.MainWindow import MainWindow
    window = MainWindow(facadeImpl=facade, loop=None)

    from PySide6.QtCore import QTimer
    QTimer.singleShot(3000, lambda: (splash.close(), window.show()))

    def on_about_to_quit():
        print("Qt退出，关闭后端")
        facade.shutdown()

    app.aboutToQuit.connect(on_about_to_quit)

    app.exec()

    print("程序结束")


if __name__ == '__main__':
    main()
