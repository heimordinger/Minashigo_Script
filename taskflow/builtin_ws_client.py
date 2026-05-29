#!/usr/bin/env python3
"""
内置WebSocket客户端，用于TaskFlow服务器启动时自动连接
"""
import asyncio
import websockets
import json
import logging
from typing import Dict, Any, Optional

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BuiltinWSClient:
    """内置WebSocket客户端"""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 8011):
        self.host = host
        self.port = port
        self.ws_url = f"ws://{host}:{port}"
        self.websocket: Optional[websockets.WebSocketServerProtocol] = None
        self.is_connected = False
        self.callbacks: Dict[str, Any] = {}
        
    async def connect(self):
        """连接到WebSocket服务器"""
        try:
            logger.info(f"[BuiltinWS] 正在连接到 {self.ws_url}")
            self.websocket = await websockets.connect(self.ws_url, max_size=20 * 1024 * 1024)
            self.is_connected = True
            logger.info(f"[BuiltinWS] 连接成功: {self.ws_url}")
            
            # 启动消息处理循环
            asyncio.create_task(self._message_loop())
            
            return True
        except Exception as e:
            logger.error(f"[BuiltinWS] 连接失败: {e}")
            self.is_connected = False
            return False
    
    async def disconnect(self):
        """断开连接"""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info("[BuiltinWS] 已断开连接")
    
    async def _message_loop(self):
        """消息处理循环"""
        try:
            async for message in self.websocket:
                logger.info(f"[BuiltinWS] 收到消息: {message}")
                # 这里可以处理来自服务器的消息
        except websockets.exceptions.ConnectionClosed:
            logger.info("[BuiltinWS] 连接已关闭")
            self.is_connected = False
        except Exception as e:
            logger.error(f"[BuiltinWS] 消息处理错误: {e}")
            self.is_connected = False
    
    async def send_task(self, task_name: str, properties: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
        """发送任务到服务器"""
        if not self.is_connected or not self.websocket:
            raise ConnectionError("WebSocket未连接")
        
        task_id = f"task_{len(self.callbacks)}"
        
        message = {
            "task": {
                "task_name": task_name,
                "properties": properties
            },
            "meta": {
                "id": task_id
            }
        }
        
        logger.info(f"[BuiltinWS] 发送任务: {task_name}")
        
        # 创建Future来等待响应
        future = asyncio.get_event_loop().create_future()
        self.callbacks[task_id] = future
        
        try:
            # 发送消息
            await self.websocket.send(json.dumps(message, ensure_ascii=False))
            
            # 等待响应
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
            
        except asyncio.TimeoutError:
            logger.error(f"[BuiltinWS] 任务超时: {task_name}")
            raise TimeoutError(f"任务执行超时: {task_name}")
        finally:
            # 清理回调
            self.callbacks.pop(task_id, None)
    
    async def _handle_response(self, message: str):
        """处理服务器响应"""
        try:
            data = json.loads(message)
            task_id = data.get("meta", {}).get("id")
            
            if task_id and task_id in self.callbacks:
                future = self.callbacks[task_id]
                if not future.done():
                    future.set_result(data)
        except Exception as e:
            logger.error(f"[BuiltinWS] 响应处理错误: {e}")

# 全局实例
builtin_client = BuiltinWSClient()

async def get_builtin_client() -> BuiltinWSClient:
    """获取内置客户端实例"""
    if not builtin_client.is_connected:
        await builtin_client.connect()
    return builtin_client

async def test_connection():
    """测试连接"""
    client = await get_builtin_client()
    if client.is_connected:
        logger.info("[BuiltinWS] 连接测试成功")
        return True
    else:
        logger.error("[BuiltinWS] 连接测试失败")
        return False

if __name__ == "__main__":
    # 测试连接
    asyncio.run(test_connection())
