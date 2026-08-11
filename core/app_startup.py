"""
应用启动模块 — 支持异步链式初始化
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
        self._model_check_thread = None
        self._loading = None

    def _ts(self, msg):
        print(f"[{time.time()-self._t0:7.3f}] {msg}")

    # ==================== 同步接口（保持向后兼容） ====================

    def load_resources(self):
        """加载模型资源（后台线程运行，不阻塞后续步骤）"""
        self._model_check_thread = threading.Thread(
            target=self._check_models, daemon=True
        )
        self._model_check_thread.start()
        self._ts("  模型检查已在后台启动")

    def _check_models(self):
        from models.resource_manager import ensure_all_models
        ensure_all_models()

    def _preload_scripts(self):
        """后台预扫描 scripts/ 目录，预热文件系统缓存"""
        from pathlib import Path
        from core.path import SCRIPTS_PATH
        p = Path(SCRIPTS_PATH)
        if p.exists():
            list(p.rglob("*.py"))  # 触发文件系统遍历，后续调用走缓存

    def _ensure_models_ready(self):
        if self._model_check_thread and self._model_check_thread.is_alive():
            print("[Startup] 等待模型检查完成...")
            self._model_check_thread.join()
        self._ts("  模型资源准备完成")

    def load_config(self):
        from core.config.config import config
        config.load()
        self._ts("  配置加载完成")

    def setup_ports(self):
        from core.port_manager import port_manager
        service_ports = port_manager.get_service_ports()
        print(f"[Main] 服务端口分配完成 - Taskflow HTTP: {service_ports.taskflow_http}, WS: {service_ports.taskflow_ws}")
        self._ts("  端口分配完成")
        return service_ports

    async def startup_taskflow(self):
        from core.taskflow_manager import taskflow_manager
        await taskflow_manager.start_async()
        await self.startup_websocket_client()
        self._ts("  Taskflow管理器启动完成")

    async def startup_websocket_client(self):
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
        def run_taskflow_startup():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.startup_taskflow())
            loop.close()
        taskflow_thread = threading.Thread(target=run_taskflow_startup, daemon=True)
        taskflow_thread.start()

    def setup_controller(self):
        from controller.ctrl import Controller
        self.controller = Controller()
        self._ts("  控制器初始化完成")

    def setup_facade(self):
        from gui.facade_impl import FacadeImpl
        self.facade = FacadeImpl(self.controller)
        self._ts("  外观层初始化完成")

    def setup_gui(self):
        """用已有 QApp 创建主窗口"""
        self._ensure_models_ready()
        from gui.window.MainWindow import MainWindow
        self.window = MainWindow(facadeImpl=self.facade, loop=None)
        self._ts("  GUI初始化完成")

    def setup_quit_handler(self, loading_animation):
        self._loading = loading_animation

        def on_about_to_quit():
            print("Qt退出，关闭后端")
            async def shutdown_taskflow():
                from core.taskflow_manager import taskflow_manager
                await taskflow_manager.shutdown()
            def run_taskflow_shutdown():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(shutdown_taskflow())
                loop.close()
            shutdown_thread = threading.Thread(target=run_taskflow_shutdown)
            shutdown_thread.start()
            shutdown_thread.join(timeout=5)
            self.facade.shutdown()

        # 必须在 QApp 创建后才能连接
        from PySide6.QtCore import QCoreApplication
        try:
            QCoreApplication.instance().aboutToQuit.connect(on_about_to_quit)
        except Exception:
            pass

    def run(self):
        self._ts("进入事件循环")
        from PySide6.QtWidgets import QApplication
        QApplication.instance().exec()
        print("程序结束")

    # ==================== 异步链式接口 ====================

    def schedule_init(self, loading):
        """在事件循环中安排异步初始化链（由 main.py 调用）"""
        self._loading = loading
        from PySide6.QtCore import QTimer

        # Phase 1: 模型检查(后台) + 配置 + 端口 + 脚本列表预加载
        self._ts("开始加载资源")
        self.load_resources()
        self.load_config()
        self.setup_ports()
        # 后台预扫描 scripts/ 目录，加速 account-panel 首次打开
        threading.Thread(target=self._preload_scripts, daemon=True).start()

        # Phase 2: 等模型就绪 → 控制器
        QTimer.singleShot(0, self._phase2)

    def _phase2(self):
        """等模型检查完成 + 初始化控制器 + 外观层"""
        from PySide6.QtCore import QTimer
        if self._model_check_thread and self._model_check_thread.is_alive():
            QTimer.singleShot(50, self._phase2)
            return
        self._ensure_models_ready()
        self.setup_controller()
        self.setup_facade()
        # Phase 3: GUI
        QTimer.singleShot(0, self._phase3)

    def _phase3(self):
        """创建主窗口并显示"""
        from gui.window.MainWindow import MainWindow
        self.window = MainWindow(facadeImpl=self.facade, loop=None)
        self._ts("  GUI初始化完成")

        # 退出处理
        self.setup_quit_handler(self._loading)

        # 停止加载动画并显示主窗口
        self._loading.stop()
        self.window.show()
        self._ts("  显示主窗口")

        # Phase 4: 后台启动 TaskFlow
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._phase4)

    def _phase4(self):
        """后台启动 TaskFlow 服务器"""
        self._ts("启动TaskFlow后台")
        self.start_taskflow_background()
