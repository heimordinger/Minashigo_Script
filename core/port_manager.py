# core/port_manager.py
import socket
from typing import Dict, Set
from dataclasses import dataclass
from pathlib import Path
import json

@dataclass
class ServicePorts:
    """服务端口配置"""
    taskflow_http: int
    taskflow_ws: int
    browser_debug_start: int  # 浏览器调试起始端口
    browser_debug_end: int    # 浏览器调试结束端口
    reserved_ports: Set[int]  # 保留端口

class PortManager:
    """端口管理器 - 统一管理项目中的所有端口分配"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self._initialized = True
        self.allocated_ports: Set[int] = set()
        self.service_ports: ServicePorts = None
        self.config_file = Path(__file__).parent.parent / "config" / "ports.json"

        # socket占用字典，用于 reserve_browser_port / release_browser_port
        self._held_ports: dict[int, socket.socket] = {}
        
    def get_free_port(self, start_port: int = 8000, max_attempts: int = 100) -> int:
        """获取可用端口"""
        for i in range(max_attempts):
            port = start_port + i
            if self._is_port_available(port) and port not in self.allocated_ports:
                self.allocated_ports.add(port)
                return port
        
        raise RuntimeError(f"在端口范围 {start_port}-{start_port + max_attempts} 中找不到可用端口")
    
    def _is_port_available(self, port: int) -> bool:
        """检查端口是否可用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return True
        except OSError:
            return False
    
    def allocate_service_ports(self) -> ServicePorts:
        """为所有服务分配端口"""
        print("[PortManager] 开始分配服务端口...")
        
        # 分配Taskflow端口
        taskflow_http = self.get_free_port(8010)
        taskflow_ws = self.get_free_port(taskflow_http + 1)
        
        # 分配浏览器调试端口范围（每个账号可能需要2个端口：调试和DevTools）
        browser_debug_start = self.get_free_port(9200)
        browser_debug_count = 20  # 预留20个账号的调试端口
        browser_debug_end = browser_debug_start + browser_debug_count * 2 - 1
        
        # 预留调试端口范围
        for port in range(browser_debug_start, browser_debug_end + 1):
            self.allocated_ports.add(port)
        
        # 创建服务端口配置
        self.service_ports = ServicePorts(
            taskflow_http=taskflow_http,
            taskflow_ws=taskflow_ws,
            browser_debug_start=browser_debug_start,
            browser_debug_end=browser_debug_end,
            reserved_ports=self.allocated_ports.copy()
        )
        
        # 保存配置到文件
        self._save_port_config()
        
        # 混合通信架构端口
        taskflow_api = taskflow_http + 100  # HTTP API端口
        taskflow_realtime = taskflow_ws + 100  # 实时WebSocket端口
        
        print(f"[PortManager] 混合通信架构端口分配完成:")
        print(f"  Taskflow HTTP: {taskflow_http}")
        print(f"  Taskflow API: {taskflow_api}")
        print(f"  Taskflow WebSocket: {taskflow_ws}")
        print(f"  Taskflow Realtime: {taskflow_realtime}")
        print(f"  浏览器调试范围: {browser_debug_start}-{browser_debug_end}")
        print(f"  总计保留端口: {len(self.allocated_ports)}")
        
        # 存储混合架构端口
        self.taskflow_api = taskflow_api
        self.taskflow_realtime = taskflow_realtime
        
        return self.service_ports
    
    def get_browser_debug_port(self, account_index: int = 0) -> int:
        """获取浏览器调试端口"""
        if not self.service_ports:
            raise RuntimeError("服务端口尚未分配，请先调用 allocate_service_ports()")

        start_port = self.service_ports.browser_debug_start
        port = start_port + (account_index * 2)

        if port > self.service_ports.browser_debug_end:
            raise RuntimeError(f"浏览器调试端口超出范围: {port}")

        return port

    def reserve_browser_port(self, account_index: int = 0) -> int:
        """分配并临时占用一个空闲的浏览器调试端口（socket.bind + listen 真正 hold 住）

        返回的端口确保当前是空闲的，调用方应在启动 Chrome 前调用 release_browser_port()
        """
        if not self.service_ports:
            raise RuntimeError("服务端口尚未分配，请先调用 allocate_service_ports()")

        start = self.service_ports.browser_debug_start + (account_index * 2)
        end = self.service_ports.browser_debug_end

        for port in range(start, end + 1):
            if port in self._held_ports:
                continue
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(('127.0.0.1', port))
                s.listen(1)
                self._held_ports[port] = s
                self.allocated_ports.add(port)
                print(f"[PortManager] 已占用端口: {port}")
                return port
            except OSError:
                s.close()
                continue

        raise RuntimeError(f"没有可用的浏览器调试端口 (范围 {start}-{end})")

    def release_browser_port(self, port: int):
        """释放之前占用的端口（关闭 socket），幂等"""
        s = self._held_ports.pop(port, None)
        if s:
            try:
                s.close()
            except OSError:
                pass
            print(f"[PortManager] 已释放端口: {port}")
    
    def get_devtools_port(self, account_index: int = 0) -> int:
        """获取DevTools端口"""
        debug_port = self.get_browser_debug_port(account_index)
        return debug_port + 1
    
    def _save_port_config(self):
        """保存端口配置到文件"""
        try:
            config_data = {
                'taskflow_http': self.service_ports.taskflow_http,
                'taskflow_ws': self.service_ports.taskflow_ws,
                'browser_debug_start': self.service_ports.browser_debug_start,
                'browser_debug_end': self.service_ports.browser_debug_end,
                'reserved_ports': list(self.service_ports.reserved_ports)
            }
            
            self.config_file.parent.mkdir(exist_ok=True)
            self.config_file.write_text(json.dumps(config_data, indent=2), encoding='utf-8')
            print(f"[PortManager] 端口配置已保存到: {self.config_file}")
            
        except Exception as e:
            print(f"[PortManager] 保存端口配置失败: {e}")
    
    def load_port_config(self) -> bool:
        """从文件加载端口配置"""
        try:
            if not self.config_file.exists():
                return False
            
            config_data = json.loads(self.config_file.read_text(encoding='utf-8'))
            
            self.service_ports = ServicePorts(
                taskflow_http=config_data['taskflow_http'],
                taskflow_ws=config_data['taskflow_ws'],
                browser_debug_start=config_data['browser_debug_start'],
                browser_debug_end=config_data['browser_debug_end'],
                reserved_ports=set(config_data['reserved_ports'])
            )
            
            self.allocated_ports = self.service_ports.reserved_ports.copy()
            
            print(f"[PortManager] 从文件加载端口配置: {self.config_file}")
            return True
            
        except Exception as e:
            print(f"[PortManager] 加载端口配置失败: {e}")
            return False
    
    def get_service_ports(self) -> ServicePorts:
        """获取服务端口配置"""
        if not self.service_ports:
            if not self.load_port_config():
                return self.allocate_service_ports()
        
        return self.service_ports
    
    def is_port_allocated(self, port: int) -> bool:
        """检查端口是否已被分配"""
        return port in self.allocated_ports
    
    def release_port(self, port: int):
        """释放端口"""
        self.allocated_ports.discard(port)
        print(f"[PortManager] 已释放端口: {port}")

# 全局端口管理器实例
port_manager = PortManager()
