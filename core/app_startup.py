"""
应用启动模块
处理TaskFlow服务器、控制器、GUI等组件的初始化
"""
import asyncio
import threading
import sys
import time


class AppStartup:
    def __init__(self, t0=None):
        self.controller = None
        self.facade = None
        self.window = None
        self.app = None
        self._t0 = t0 or time.time()

    def _ts(self, msg):
        print(f"[{time.time()-self._t0:7.3f}] {msg}")

    def load_resources(self):
        """加载模型资源"""
        from models.resource_manager import ensure_all_models
        ensure_all_models()
        self._ts("  模型资源准备完成")

    def load_config(self):
        """加载配置"""
        from core.config.config import config
        config.load()
        self._ts("  配置加载完成")

    def setup_ports(self):
        """设置端口分配"""
        from core.port_manager import port_manager
        service_ports = port_manager.get_service_ports()
        print(f"[Main] 服务端口分配完成 - Taskflow HTTP: {service_ports.taskflow_http}, WS: {service_ports.taskflow_ws}")
        self._ts("  端口分配完成")
        return service_ports

    async def startup_taskflow(self):
        """启动TaskFlow管理器"""
        from core.taskflow_manager import taskflow_manager
        await taskflow_manager.start_async()

        # 启动WebSocket客户端连接
        await self.startup_websocket_client()

        self._ts("  Taskflow管理器启动完成")

    async def startup_websocket_client(self):
        """启动WebSocket客户端连接"""
        try:
            from taskflow.builtin_ws_client import get_builtin_client
            print("[Main] 正在启动WebSocket客户端连接...")

            client = await get_builtin_client()
            if client.is_connected:
                print("[Main] WebSocket客户端连接成功")
                self._ts("  WebSocket客户端连接成功")
            else:
                print("[Main] WebSocket客户端连接失败")

        except Exception as e:
            print(f"[Main] WebSocket客户端启动失败: {e}")

    def start_taskflow_background(self):
        """在后台线程中启动Taskflow"""
        def run_taskflow_startup():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.startup_taskflow())
            loop.close()

        taskflow_thread = threading.Thread(target=run_taskflow_startup, daemon=True)
        taskflow_thread.start()

    def setup_controller(self):
        """设置控制器"""
        _t0 = __import__('time').time()
        print(f"[Ctrl] 开始导入controller.ctrl...")
        from controller.ctrl import Controller
        print(f"[Ctrl] 导入完成: {__import__('time').time()-_t0:.3f}s")
        self.controller = Controller()
        self._ts("  控制器初始化完成")

    def setup_facade(self):
        """设置外观"""
        from gui.facade_impl import FacadeImpl
        self.facade = FacadeImpl(self.controller)
        self._ts("  外观层初始化完成")

    def setup_gui(self):
        """设置GUI"""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QIcon
        from gui.window.MainWindow import MainWindow
        from core.path import ICON_PATH

        # Windows 任务栏图标修正：必须在 QApp 创建前设置
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MinashigoScript.1.0")
        except Exception:
            pass

        # 创建Qt应用
        self.app = QApplication(sys.argv)
        self.app.setWindowIcon(QIcon(str(ICON_PATH)))
        self.window = MainWindow(facadeImpl=self.facade, loop=None)
        self._ts("  GUI初始化完成")

    def setup_quit_handler(self, loading_animation):
        """设置退出处理"""
        def on_about_to_quit():
            print("Qt退出，关闭后端")

            # 关闭Taskflow管理器
            async def shutdown_taskflow():
                from core.taskflow_manager import taskflow_manager
                await taskflow_manager.shutdown()

            # 同步关闭Taskflow
            def run_taskflow_shutdown():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(shutdown_taskflow())
                loop.close()

            shutdown_thread = threading.Thread(target=run_taskflow_shutdown)
            shutdown_thread.start()
            shutdown_thread.join(timeout=5)

            self.facade.shutdown()

        self.app.aboutToQuit.connect(on_about_to_quit)

    def show_main_window(self, loading_animation):
        """显示主窗口"""
        from PySide6.QtCore import QTimer

        def close_loading_and_show_main():
            # 停止加载动画
            loading_animation.stop()
            self._ts("  显示主窗口")
            self.window.show()

        # 在主窗口即将显示前关闭加载动画
        QTimer.singleShot(2500, close_loading_and_show_main)

    def run(self):
        """运行应用"""
        self._ts("进入事件循环")
        self.app.exec()
        print("程序结束")
