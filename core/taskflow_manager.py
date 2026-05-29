# core/taskflow_manager.py
import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Dict, Optional, List
import webbrowser

from core.path import PROJECT_ROOT


class TaskflowManager:
    """单例模式的Taskflow管理器 - 混合通信架构"""
    
    _instance: Optional['TaskflowManager'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        self.accounts = {}
        self.server_ports = {}
        self.http_port = None
        self.ws_port = None
        self.api_port = None  # HTTP API端口
        self.realtime_port = None  # 实时WebSocket端口
        self.server_running = False
        self._page_opened = False
        self._sent_create_tab = set()  # 已发送过create_tab的账号，再次点击只聚焦
        self._event_loop = None
        self.builtin_client = None
        self.server_thread: Optional[threading.Thread] = None
        
        # 混合通信组件
        self.http_api_server = None
        self.realtime_websocket = None
        
        print("[TaskflowManager] 单例实例已创建 (混合通信架构)")
    
    async def start_async(self):
        """异步启动Taskflow服务器 - 混合通信架构"""
        if self.server_running:
            print("[TaskflowManager] 服务器已在运行")
            return

        self._event_loop = asyncio.get_running_loop()

        # 获取端口
        from core.port_manager import PortManager
        port_manager = PortManager()
        service_ports = port_manager.get_service_ports()
        self.http_port = service_ports.taskflow_http
        self.ws_port = service_ports.taskflow_ws
        # 为混合架构分配新端口
        self.api_port = self.http_port + 100  # HTTP API端口
        self.realtime_port = self.ws_port + 100  # 实时WebSocket端口
        
        print(f"[TaskflowManager] 混合通信架构 - HTTP: {self.http_port}, API: {self.api_port}, Realtime: {self.realtime_port}")
        
        # 导入taskflow模块
        import sys
        taskflow_path = PROJECT_ROOT / "taskflow"
        if str(taskflow_path) not in sys.path:
            sys.path.insert(0, str(taskflow_path))
        
        from run_taskflow import get_free_port, start_http_server, generate_loader
        from taskflow.backend_handler import start_websocket_server
        
        # 1. 启动原始HTTP服务器（保持兼容性）
        import threading
        import time
        
        def run_http_server():
            """运行原始HTTP服务器"""
            try:
                start_http_server(port=self.http_port)
                print(f"[TaskflowManager] 原始HTTP服务器启动成功，端口: {self.http_port}")
            except Exception as e:
                print(f"[TaskflowManager] 原始HTTP服务器启动失败: {e}")
        
        def run_websocket_server():
            """运行原始WebSocket服务器（保持兼容性）"""
            try:
                start_websocket_server(port=self.ws_port)
                print(f"[TaskflowManager] 原始WebSocket服务器启动成功，端口: {self.ws_port}")
            except Exception as e:
                print(f"[TaskflowManager] 原始WebSocket服务器启动失败: {e}")
        
        # 启动HTTP服务器
        self.server_thread = threading.Thread(
            target=run_http_server,
            daemon=True
        )
        self.server_thread.start()
        
        # 启动WebSocket服务器
        self.ws_server_thread = threading.Thread(
            target=run_websocket_server,
            daemon=True
        )
        self.ws_server_thread.start()
        
        # 等待服务器完全启动
        time.sleep(1.0)
        print("[TaskflowManager] 原始HTTP和WebSocket服务器已完全启动")
        
        # 2. 生成loader和设置账号信息
        self._generate_loader()
        self._setup_account_info()
        
        # 3. 启动HTTP API服务器
        await self._start_http_api_server()
        
        # 4. 启动实时WebSocket服务器
        await self._start_realtime_websocket()
        
        # 5. 更新端口配置文件
        self._update_port_files()
        
        # 设置服务器运行标志
        self.server_running = True
        print(f"[TaskflowManager] 混合通信架构已启动")
        print(f"[TaskflowManager] HTTP: {self.http_port}, API: {self.api_port}, Realtime: {self.realtime_port}")
        print("[TaskflowManager] 资源加载完成，等待用户打开网页")
        
        # 保持服务器运行
        await self._maintain_servers()
    
    async def _start_http_api_server(self):
        """启动HTTP API服务器"""
        try:
            from core.http_api_server import HTTPAPIServer
            self.http_api_server = HTTPAPIServer(self)
            await self.http_api_server.start(self.api_port)
            print(f"[TaskflowManager] HTTP API服务器已启动，端口: {self.api_port}")
        except Exception as e:
            print(f"[TaskflowManager] HTTP API服务器启动失败: {e}")
            raise
    
    async def _start_realtime_websocket(self):
        """启动实时WebSocket服务器"""
        try:
            from core.realtime_websocket import RealtimeWebSocketServer
            self.realtime_websocket = RealtimeWebSocketServer(self)
            await self.realtime_websocket.start(self.realtime_port)
            print(f"[TaskflowManager] 实时WebSocket服务器已启动，端口: {self.realtime_port}")
        except Exception as e:
            print(f"[TaskflowManager] 实时WebSocket服务器启动失败: {e}")
            raise
    
    def _update_port_files(self):
        """更新端口配置文件"""
        try:
            # 更新ws_port.js文件（保持兼容性）
            self._update_ws_port_file()
            
            # 创建新的端口配置文件
            port_config = {
                'http_port': self.http_port,
                'api_port': self.api_port,
                'realtime_port': self.realtime_port,
                'ws_port': self.ws_port  # 保持兼容性
            }
            
            config_file = PROJECT_ROOT / "taskflow" / "hybrid_ports.json"
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(port_config, f, indent=2, ensure_ascii=False)
            
            print(f"[TaskflowManager] 已更新混合通信端口配置: {config_file}")
            
        except Exception as e:
            print(f"[TaskflowManager] 更新端口配置失败: {e}")
    
    async def _maintain_servers(self):
        """保持服务器运行"""
        last_client_count = -1
        try:
            while self.server_running:
                await asyncio.sleep(1.0)

                if self.realtime_websocket:
                    client_count = self.realtime_websocket.get_client_count()
                    if client_count != last_client_count:
                        print(f"[TaskflowManager] 实时连接数: {client_count}")
                        last_client_count = client_count
                        
        except asyncio.CancelledError:
            print("[TaskflowManager] 服务器维护任务被取消")
        except Exception as e:
            print(f"[TaskflowManager] 服务器维护错误: {e}")
    
    async def broadcast_task_event(self, event_type: str, data: Dict, account: str = None):
        """广播任务事件到实时客户端"""
        if self.realtime_websocket:
            await self.realtime_websocket.broadcast({
                'type': event_type,
                'data': data,
                'timestamp': asyncio.get_event_loop().time()
            }, account_filter=account)
    
    async def send_log_to_client(self, level: str, message: str, account: str = None):
        """发送日志到客户端"""
        if self.realtime_websocket:
            await self.realtime_websocket.handle_log_output(level, message, account)
    
    async def update_browser_status(self, account: str, status: Dict):
        """更新浏览器状态"""
        if self.realtime_websocket:
            await self.realtime_websocket.handle_browser_status(account, status)
    
    def _generate_loader(self):
        """生成loader文件"""
        try:
            from run_taskflow import generate_loader
            generate_loader()
            print("[TaskflowManager] Loader生成完成")
        except Exception as e:
            print(f"[TaskflowManager] Loader生成失败: {e}")
    
    def _setup_account_info(self):
        """设置账号信息"""
        # 不再覆盖全局current_account，由各account-panel按需传递自己的账号
        print("[TaskflowManager] 账号信息设置完成（跳过全局current_account覆盖）")
    
    def register_account(self, account: dict):
        """注册账号（不自动创建tab）"""
        account_name = account.get('name', 'default')
        account_email = account.get('email', '')
        
        self.accounts[account_name] = account
        
        # 存储账号端口信息
        self.server_ports[account_name] = {
            'http_port': self.http_port,
            'ws_port': self.ws_port
        }
        
        print(f"[TaskflowManager] 已注册账号: {account_name} ({account_email})")
        # 注意：不再自动创建tab，由account-panel按需创建
    
    def get_taskflow_url_for_account(self, account_name: str):
        """获取指定账号的Taskflow URL（由account-panel调用）"""
        if not self.server_running:
            print(f"[TaskflowManager] 服务器未运行，无法获取 {account_name} 的URL")
            return None
        
        account = self.accounts.get(account_name)
        if not account:
            print(f"[TaskflowManager] 账号 {account_name} 未注册")
            return None
        
        url = f"http://127.0.0.1:{self.http_port}/taskflow/index.html?name={account_name}&email={account.get('email', '')}"
        return url
    
    def create_taskflow_tab_for_account(self, account_name: str):
        """为指定账号创建TaskFlow tab（由MainWindow调用）"""
        url = self.get_taskflow_url_for_account(account_name)
        if url:
            print(f"[TaskflowManager] 为账号 {account_name} 创建TaskFlow tab: {url}")
            return url
        return None
    
    def open_taskflow_for_account(self, account_name: str):
        """为指定账号打开Taskflow网页并发送创建tab任务"""
        # 等待服务器就绪（首次启动可能还没完成）
        for _ in range(25):  # 最多等 5 秒
            if self.server_running:
                break
            time.sleep(0.2)
        if not self.server_running:
            print(f"[TaskflowManager] 服务器未运行，无法为 {account_name} 打开网页")
            return False

        account = self.accounts.get(account_name)
        if not account:
            print(f"[TaskflowManager] 账号 {account_name} 未注册")
            return False

        account_info = {
            'name': account_name,
            'email': account.get('email', '')
        }

        # 只在第一次打开浏览器，后续只发WebSocket命令
        # 但如果页面被关闭了（无WS客户端），重新打开
        if not self._page_opened:
            should_open = True
        else:
            ws = getattr(self, 'realtime_websocket', None)
            should_open = ws is None or ws.get_client_count() == 0

        if should_open:
            import time
            # 使用时间戳做缓存破坏，确保浏览器每次都加载最新页面
            cache_buster = int(time.time() * 1000)
            url = f"http://127.0.0.1:{self.http_port}/taskflow/index.html?_t={cache_buster}"
            try:
                print(f"[TaskflowManager] 首次打开TaskFlow页面: {url}")
                webbrowser.open(url)
                self._page_opened = True
                time.sleep(5)  # 等待页面加载和WebSocket连接建立
            except Exception as e:
                print(f"[TaskflowManager] 打开页面失败: {e}")
                return False

        # 检查是否已为该账号发送过create_tab，是则发送focus_tab
        if account_name in self._sent_create_tab:
            print(f"[TaskflowManager] 账号 {account_name} 已有tab，发送聚焦命令")
            if self._event_loop and self._event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._send_focus_tab_task(account_info),
                    self._event_loop
                )
            return True

        # 首次为该账号发送create_tab
        self._sent_create_tab.add(account_name)
        print(f"[TaskflowManager] 发送创建tab任务: {account_info}")
        if self._event_loop and self._event_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_create_tab_task(account_info, retry=5),
                self._event_loop
            )
            print(f"[TaskflowManager] 创建tab任务已发送")
            return True
        else:
            print(f"[TaskflowManager] 事件循环未运行，无法发送任务")
            return False
    
    async def _send_focus_tab_task(self, account_info: dict):
        """通过WebSocket发送聚焦tab的命令到TaskFlow"""
        for i in range(3):
            try:
                ws = getattr(self, 'realtime_websocket', None)
                if ws:
                    client_count = ws.get_client_count()
                    print(f"[TaskflowManager] 发送聚焦tab任务 第{i+1}次尝试, WS客户端数: {client_count}")

                    await ws.broadcast({
                        'type': 'command',
                        'payload': {
                            'type': 'focus_tab',
                            'account_info': account_info
                        }
                    })
                    print(f"[TaskflowManager] 已广播聚焦tab任务: {account_info}")

                    if client_count > 0:
                        return
                else:
                    print(f"[TaskflowManager] 实时WebSocket未连接，等待重试...")
            except Exception as e:
                print(f"[TaskflowManager] 发送聚焦tab任务失败: {e}")

            await asyncio.sleep(1)

        print(f"[TaskflowManager] 聚焦tab任务发送完毕")

    async def _send_create_tab_task(self, account_info: dict, retry=5):
        """通过WebSocket发送创建tab的任务到TaskFlow（带重试，确保前端连接就绪）"""
        for i in range(retry):
            try:
                ws = getattr(self, 'realtime_websocket', None)
                if ws:
                    client_count = ws.get_client_count()
                    print(f"[TaskflowManager] 发送创建tab任务 第{i+1}次尝试, 当前WS客户端数: {client_count}")

                    await ws.broadcast({
                        'type': 'command',
                        'payload': {
                            'type': 'create_tab',
                            'account_info': account_info
                        }
                    })
                    print(f"[TaskflowManager] 已广播创建tab任务: {account_info}")

                    if client_count > 0:
                        return  # 有客户端已连接并收到消息
                else:
                    print(f"[TaskflowManager] 实时WebSocket未连接，等待重试...")
            except Exception as e:
                print(f"[TaskflowManager] 发送创建tab任务失败: {e}")

            await asyncio.sleep(2)

        print(f"[TaskflowManager] 创建tab任务发送完毕（已重试{retry}次）")
    
    def get_registered_accounts(self) -> List[str]:
        """获取已注册的账号列表"""
        return list(self.accounts.keys())
    
    def is_account_registered(self, account_name: str) -> bool:
        """检查账号是否已注册"""
        return account_name in self.accounts
    
    def get_server_info(self) -> dict:
        """获取服务器信息"""
        return {
            'http_port': self.http_port,
            'ws_port': self.ws_port,
            'running': self.server_running,
            'accounts': list(self.accounts.keys())
        }
    
    def unregister_account(self, account_name: str):
        """注销账号并关闭对应的TaskFlow tab"""
        if account_name in self.accounts:
            del self.accounts[account_name]
            if account_name in self.server_ports:
                del self.server_ports[account_name]
            print(f"[TaskflowManager] 已注销账号: {account_name}")
            
            # 通知前端关闭tab
            self._notify_close_tab(account_name)
    
    def _update_ws_port_file(self):
        """更新ws_port.js文件以使用动态端口"""
        try:
            ws_port_file = PROJECT_ROOT / "taskflow" / "ws_port.js"
            ws_port_content = f"export const WS_PORT = {self.ws_port};"
            ws_port_file.write_text(ws_port_content, encoding="utf-8")
            print(f"[TaskflowManager] 已更新WebSocket端口为: {self.ws_port}")
            
            # 验证文件更新
            updated_content = ws_port_file.read_text(encoding="utf-8")
            print(f"[TaskflowManager] ws_port.js内容: {updated_content}")
            
        except Exception as e:
            print(f"[TaskflowManager] 更新ws_port.js失败: {e}")
            
            # 强制设置默认端口
            try:
                self.ws_port = 8012
                ws_port_file = PROJECT_ROOT / "taskflow" / "ws_port.js"
                ws_port_content = f"export const WS_PORT = {self.ws_port};"
                ws_port_file.write_text(ws_port_content, encoding="utf-8")
                print(f"[TaskflowManager] 强制设置WebSocket端口为: {self.ws_port}")
            except Exception as e2:
                print(f"[TaskflowManager] 强制设置端口失败: {e2}")
    
    def _notify_create_tab(self, account: dict):
        """通知前端创建新的tab"""
        # 这里可以通过WebSocket或其他方式通知前端
        # 目前前端会在加载时自动创建tab
        account_name = account.get('name', 'default')
        account_email = account.get('email', '')
        print(f"[TaskflowManager] 准备为账号 {account_name} ({account_email}) 创建TaskFlow tab")
    
    def _notify_close_tab(self, account_name: str):
        """通知前端关闭tab"""
        # 这里可以通过WebSocket或其他方式通知前端
        print(f"[TaskflowManager] 通知前端关闭账号 {account_name} 的TaskFlow tab")
    
    async def shutdown(self):
        """关闭Taskflow服务器 - 混合通信架构"""
        if not self.server_running:
            return
        
        print("[TaskflowManager] 正在关闭混合通信架构服务器...")
        self.server_running = False
        
        # 关闭HTTP API服务器
        if self.http_api_server:
            await self.http_api_server.stop()
            print("[TaskflowManager] HTTP API服务器已关闭")
        
        # 关闭实时WebSocket服务器
        if self.realtime_websocket:
            await self.realtime_websocket.stop()
            print("[TaskflowManager] 实时WebSocket服务器已关闭")
        
        # 清理账号信息
        self.accounts.clear()
        self.server_ports.clear()
        
        print("[TaskflowManager] 混合通信架构服务器已关闭")


# 全局单例实例
taskflow_manager = TaskflowManager()
