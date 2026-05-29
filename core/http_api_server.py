"""
HTTP API服务器
处理配置管理、账号管理等非实时通信
"""
import json
import asyncio
from aiohttp import web, WSMsgType
from pathlib import Path
from typing import Dict, Any, Optional
import logging


class HTTPAPIServer:
    def __init__(self, taskflow_manager):
        self.app = web.Application()
        self.taskflow_manager = taskflow_manager
        self.setup_routes()
        self.runner = None
        self.site = None
        
    def setup_routes(self):
        """设置API路由"""
        # 配置管理
        self.app.router.add_get('/api/config', self.get_config)
        self.app.router.add_post('/api/config', self.update_config)
        
        # 账号管理
        self.app.router.add_get('/api/accounts', self.get_accounts)
        self.app.router.add_post('/api/accounts', self.create_account)
        self.app.router.add_delete('/api/accounts/{account_id}', self.delete_account)
        
        # 任务模板管理
        self.app.router.add_get('/api/templates', self.get_templates)
        self.app.router.add_post('/api/templates', self.save_template)
        
        # 节点和工作流
        self.app.router.add_get('/api/nodes', self.get_nodes)
        self.app.router.add_get('/api/workflows', self.get_workflows)
        
        # 服务器状态
        self.app.router.add_get('/api/status', self.get_status)

        # 控制表盘
        self.app.router.add_get('/dashboard', self.serve_dashboard)
        self.app.router.add_get('/api/dashboard/data', self.get_dashboard_data)

        # 静态资源
        self.app.router.add_get('/favicon.ico', self.serve_favicon)

    async def get_config(self, request):
        """获取配置"""
        try:
            account_id = request.query.get('account_id', 'default')
            # 这里从taskflow_manager获取配置
            config_data = {
                'account_id': account_id,
                'settings': {
                    'theme': 'dark',
                    'auto_save': True,
                    'debug_mode': False
                }
            }
            return web.json_response({'success': True, 'data': config_data})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def update_config(self, request):
        """更新配置"""
        try:
            data = await request.json()
            account_id = data.get('account_id', 'default')
            settings = data.get('settings', {})
            
            # 这里更新taskflow_manager的配置
            print(f"[HTTP API] 更新配置: {account_id}, {settings}")
            
            return web.json_response({'success': True, 'message': '配置更新成功'})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def get_accounts(self, request):
        """获取账号列表"""
        try:
            accounts = list(self.taskflow_manager.accounts.values())
            return web.json_response({'success': True, 'data': accounts})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def create_account(self, request):
        """创建账号"""
        try:
            data = await request.json()
            account_name = data.get('name')
            account_email = data.get('email')
            
            if not account_name or not account_email:
                return web.json_response({'success': False, 'error': '账号名称和邮箱必填'}, status=400)
            
            # 这里调用taskflow_manager的账号创建逻辑
            account = {
                'name': account_name,
                'email': account_email,
                'created_at': '2024-01-01'
            }
            
            self.taskflow_manager.accounts[account_name] = account
            print(f"[HTTP API] 创建账号: {account_name}")
            
            return web.json_response({'success': True, 'data': account})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def delete_account(self, request):
        """删除账号"""
        try:
            account_id = request.match_info['account_id']
            if account_id in self.taskflow_manager.accounts:
                del self.taskflow_manager.accounts[account_id]
                print(f"[HTTP API] 删除账号: {account_id}")
                return web.json_response({'success': True, 'message': '账号删除成功'})
            else:
                return web.json_response({'success': False, 'error': '账号不存在'}, status=404)
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def get_templates(self, request):
        """获取任务模板"""
        try:
            templates = [
                {'id': 1, 'name': '网页自动化', 'description': '基础网页操作模板'},
                {'id': 2, 'name': '数据处理', 'description': '数据处理模板'},
            ]
            return web.json_response({'success': True, 'data': templates})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def save_template(self, request):
        """保存任务模板"""
        try:
            data = await request.json()
            template_name = data.get('name')
            template_data = data.get('data')
            
            print(f"[HTTP API] 保存模板: {template_name}")
            
            return web.json_response({'success': True, 'message': '模板保存成功'})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def get_nodes(self, request):
        """获取节点列表"""
        try:
            nodes = [
                {'type': 'url', 'category': 'action', 'name': 'URL节点'},
                {'type': 'click', 'category': 'action', 'name': '点击节点'},
                {'type': 'sleep', 'category': 'flow', 'name': '延时节点'},
            ]
            return web.json_response({'success': True, 'data': nodes})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def get_workflows(self, request):
        """获取工作流列表"""
        try:
            workflows = [
                {'id': 1, 'name': '登录流程', 'status': 'active'},
                {'id': 2, 'name': '数据采集', 'status': 'inactive'},
            ]
            return web.json_response({'success': True, 'data': workflows})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def get_status(self, request):
        """获取服务器状态"""
        try:
            status = self.taskflow_manager.get_server_info()
            return web.json_response({'success': True, 'data': status})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    
    async def serve_dashboard(self, request):
        """提供控制表盘页面"""
        try:
            dashboard_path = Path(__file__).parent.parent / "taskflow" / "dashboard.html"
            if not dashboard_path.exists():
                return web.Response(text="Dashboard not found", status=404)
            html = dashboard_path.read_text(encoding="utf-8")
            return web.Response(text=html, content_type="text/html", charset="utf-8")
        except Exception as e:
            print(f"[HTTP API] Dashboard error: {e}")
            return web.Response(text=str(e), status=500)

    async def serve_favicon(self, request):
        """提供网站图标"""
        icon_path = Path(__file__).parent.parent / "icon" / "icon.ico"
        if icon_path.exists():
            return web.Response(
                body=icon_path.read_bytes(),
                content_type="image/x-icon"
            )
        return web.Response(status=404)

    async def get_dashboard_data(self, request):
        """获取控制表盘数据（不含密码）"""
        try:
            accounts = []
            for name, info in self.taskflow_manager.accounts.items():
                accounts.append({
                    "name": name,
                    "email": info.get("email", ""),
                })
            status = self.taskflow_manager.get_server_info()
            return web.json_response({
                "success": True,
                "data": {
                    "accounts": accounts,
                    "status": status,
                }
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def start(self, port: int):
        """启动HTTP API服务器"""
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, '127.0.0.1', port)
            await self.site.start()
            print(f"[HTTP API] 服务器已启动，端口: {port}")
        except Exception as e:
            print(f"[HTTP API] 启动失败: {e}")
            raise
    
    async def stop(self):
        """停止HTTP API服务器"""
        try:
            if self.site:
                await self.site.stop()
            if self.runner:
                await self.runner.cleanup()
            print("[HTTP API] 服务器已停止")
        except Exception as e:
            print(f"[HTTP API] 停止失败: {e}")
