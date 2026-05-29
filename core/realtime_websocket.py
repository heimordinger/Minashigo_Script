"""
实时WebSocket服务器
专门处理任务执行、日志输出、浏览器状态同步等实时通信
"""
import json
import asyncio
import websockets
from typing import Dict, Set, Any
import logging


class RealtimeWebSocketServer:
    def __init__(self, taskflow_manager):
        self.taskflow_manager = taskflow_manager
        self.connected_clients: Set[websockets.WebSocketServerProtocol] = set()
        self.client_accounts: Dict[websockets.WebSocketServerProtocol, Dict] = {}
        self.server = None
        self.port = None
        
    async def register_client(self, websocket, account_info: Dict = None):
        """注册客户端连接"""
        self.connected_clients.add(websocket)
        if account_info:
            self.client_accounts[websocket] = account_info
        print(f"[Realtime WS] 客户端连接: {websocket.remote_address}")

        # 发送连接确认
        await self.send_to_client(websocket, {
            'type': 'connection_established',
            'message': '实时连接已建立'
        })

        # 下发当前已注册的账号列表（刷新页面后池子重建用）
        registered_accounts = []
        for name, acct in self.taskflow_manager.accounts.items():
            registered_accounts.append({
                'name': name,
                'email': acct.get('email', ''),
            })
        if registered_accounts:
            print(f"[Realtime WS] 下发已注册账号列表: {registered_accounts}")
            await self.send_to_client(websocket, {
                'type': 'init_accounts',
                'accounts': registered_accounts,
            })
    
    async def unregister_client(self, websocket):
        """注销客户端连接"""
        self.connected_clients.discard(websocket)
        self.client_accounts.pop(websocket, None)
        print(f"[Realtime WS] 客户端断开: {websocket.remote_address}")
    
    async def send_to_client(self, websocket, data: Dict):
        """发送消息到指定客户端"""
        try:
            message = json.dumps(data, ensure_ascii=False)
            await websocket.send(message)
        except Exception as e:
            print(f"[Realtime WS] 发送消息失败: {e}")
    
    async def broadcast(self, data: Dict, account_filter: str = None):
        """广播消息到所有客户端"""
        message = json.dumps(data, ensure_ascii=False)
        disconnected_clients = set()
        
        for websocket in self.connected_clients:
            try:
                # 账号过滤
                if account_filter:
                    account_info = self.client_accounts.get(websocket)
                    if not account_info or account_info.get('name') != account_filter:
                        continue
                
                await websocket.send(message)
            except Exception as e:
                print(f"[Realtime WS] 广播失败: {e}")
                disconnected_clients.add(websocket)
        
        # 清理断开的客户端
        for client in disconnected_clients:
            await self.unregister_client(client)
    
    async def handle_task_execution(self, task_data: Dict):
        """处理任务执行"""
        await self.broadcast({
            'type': 'task_start',
            'data': task_data
        })
    
    async def handle_task_progress(self, task_id: str, progress: int, message: str = ""):
        """处理任务进度"""
        await self.broadcast({
            'type': 'task_progress',
            'task_id': task_id,
            'progress': progress,
            'message': message
        })
    
    async def handle_task_complete(self, task_id: str, result: Any):
        """处理任务完成"""
        await self.broadcast({
            'type': 'task_complete',
            'task_id': task_id,
            'result': result
        })
    
    async def handle_log_output(self, log_level: str, message: str, account: str = None):
        """处理日志输出"""
        await self.broadcast({
            'type': 'log_output',
            'level': log_level,
            'message': message,
            'timestamp': asyncio.get_event_loop().time()
        }, account_filter=account)
    
    async def handle_browser_status(self, account: str, status: Dict):
        """处理浏览器状态更新"""
        await self.broadcast({
            'type': 'browser_status',
            'account': account,
            'status': status
        }, account_filter=account)
    
    async def handle_client_message(self, websocket, message: str):
        """处理客户端消息"""
        try:
            data = json.loads(message)
            message_type = data.get('type')
            
            if message_type == 'execute_task':
                await self.handle_task_execution(data.get('data', {}))
            
            elif message_type == 'set_account':
                account_info = data.get('account', {})
                self.client_accounts[websocket] = account_info
                await self.send_to_client(websocket, {
                    'type': 'account_set',
                    'account': account_info
                })
            
            elif message_type == 'ping':
                await self.send_to_client(websocket, {'type': 'pong'})
            
            elif message_type == 'command':
                # 处理来自account-panel的命令
                command_data = data.get('data', {})
                print(f"[Realtime WS] 收到命令: {command_data}")
                
                # 广播命令给所有客户端
                await self.broadcast({
                    'type': 'command',
                    'payload': command_data
                })
            
            else:
                await self.send_to_client(websocket, {
                    'type': 'error',
                    'message': f'未知消息类型: {message_type}'
                })
                
        except json.JSONDecodeError:
            await self.send_to_client(websocket, {
                'type': 'error',
                'message': 'JSON解析失败'
            })
        except Exception as e:
            await self.send_to_client(websocket, {
                'type': 'error',
                'message': f'处理消息失败: {str(e)}'
            })
    
    async def handler(self, websocket):
        """WebSocket连接处理器"""
        await self.register_client(websocket)
        
        try:
            async for message in websocket:
                if isinstance(message, str):
                    await self.handle_client_message(websocket, message)
                else:
                    await self.send_to_client(websocket, {
                        'type': 'error',
                        'message': '只支持文本消息'
                    })
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            print(f"[Realtime WS] 连接处理错误: {e}")
        finally:
            await self.unregister_client(websocket)
    
    async def start(self, port: int):
        """启动实时WebSocket服务器"""
        try:
            self.port = port
            self.server = await websockets.serve(
                self.handler,
                '127.0.0.1',
                port,
                ping_interval=20,
                ping_timeout=10,
                max_size=20 * 1024 * 1024
            )
            print(f"[Realtime WS] 实时服务器已启动，端口: {port}")
        except Exception as e:
            print(f"[Realtime WS] 启动失败: {e}")
            raise
    
    async def stop(self):
        """停止实时WebSocket服务器"""
        try:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
            print("[Realtime WS] 实时服务器已停止")
        except Exception as e:
            print(f"[Realtime WS] 停止失败: {e}")
    
    def get_client_count(self):
        """获取连接的客户端数量"""
        return len(self.connected_clients)
    
    def get_account_clients(self, account_name: str):
        """获取指定账号的客户端"""
        return [ws for ws, account in self.client_accounts.items() 
                if account.get('name') == account_name]
