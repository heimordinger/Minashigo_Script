# run_taskflow.py
import asyncio
import json
import os
import socket
import http.server
import socketserver
import sys
import time
from pathlib import Path

import websockets

# 设置项目根目录路径
TEMP_ROOT = Path(__file__).resolve().parents[1]
if str(TEMP_ROOT) not in sys.path:
    sys.path.insert(0, str(TEMP_ROOT))

# 添加backend路径
backend_path = TEMP_ROOT / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

# ========== 导入后端操作层 ==========
from taskflow.backend_handler import (
    browsers,
    current_account,
    dispatch,
    ws_handler,
)

PROJECT_ROOT = TEMP_ROOT
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TASKFLOW_ROOT = PROJECT_ROOT / "taskflow"
NODES_DIR = TASKFLOW_ROOT / "nodes"
LOADER_FILE = TASKFLOW_ROOT / "core" / "loader.js"


def get_free_port(start=8010):
    with socket.socket() as s:
        for p in range(start, start + 100):
            try:
                s.bind(('127.0.0.1', p));return p
            except:
                pass


def start_http_server(port=get_free_port()):
    class CustomHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            """重写：所有响应添加反缓存头"""
            self.send_header('Cache-Control', 'no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            super().end_headers()

        def do_GET(self):
            """处理GET请求"""
            print(f"[HTTP] GET request for: {self.path}")

            # 如果是API请求，可以在这里处理
            if self.path.startswith('/api/'):
                self.handle_api_request()
                return

            # 默认处理静态文件
            super().do_GET()

        def do_POST(self):
            """处理POST请求"""
            print(f"[HTTP] POST request for: {self.path}")

            # 如果是API请求，可以在这里处理
            if self.path.startswith('/api/'):
                self.handle_api_request()
                return

            # 其他POST请求
            self.send_response(405)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Method not allowed"}).encode())

        def handle_api_request(self):
            """处理API请求"""
            try:
                # 仅 POST 请求需要读 body，GET 请求从 query 取参数
                if self.command == 'POST':
                    content_length = int(self.headers.get('Content-Length', 0))
                    post_data = self.rfile.read(content_length) if content_length else b''
                    data = json.loads(post_data.decode('utf-8')) if post_data else {}
                else:
                    from urllib.parse import urlparse, parse_qs
                    parsed_url = urlparse(self.path)
                    params = parse_qs(parsed_url.query)
                    data = {k: v[0] if len(v) == 1 else v for k, v in params.items()}

                # 各API路由
                if '/api/taskflow' in self.path:
                    self.handle_taskflow_api(data)
                elif self.path.startswith('/api/save_workflow') and self.command == 'POST':
                    self.handle_save_workflow(data)
                elif self.path.startswith('/api/load_workflow') and self.command == 'GET':
                    self.handle_load_workflow(data)
                elif self.path.startswith('/api/list_workflows') and self.command == 'GET':
                    self.handle_list_workflows()
                elif self.path.startswith('/api/list_images') and self.command == 'GET':
                    self.handle_list_images()
                elif self.path.startswith('/api/list_scripts') and self.command == 'GET':
                    self.handle_list_scripts()
                elif self.path.startswith('/api/get_image') and self.command == 'GET':
                    self.handle_get_image(data)
                elif self.path.startswith('/api/delete_workflow') and self.command == 'POST':
                    self.handle_delete_workflow(data)
                elif self.path.startswith('/api/get_thumbnail') and self.command == 'GET':
                    self.handle_get_thumbnail(data)
                else:
                    self.send_response(404)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "API endpoint not found"}).encode())

            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        def handle_taskflow_api(self, data):
            """处理TaskFlow API请求"""
            try:
                task_name = data.get('task_name')
                props = data.get('properties', {})

                # 异步执行任务
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(dispatch(task_name=task_name, props=props))
                loop.close()

                response = {
                    "success": True,
                    "data": result
                }

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode())

            except Exception as e:
                response = {
                    "success": False,
                    "error": str(e)
                }

                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode())

        def _send_json_response(self, resp: dict, status=200):
            self.send_response(status)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())

        def handle_save_workflow(self, data):
            """保存工作流到 scripts/ 目录（支持子文件夹路径）"""
            try:
                name = data.get("name", "").strip().replace("\\", "/")
                content = data.get("content", "")
                if not name:
                    self._send_json_response({"success": False, "error": "文件名不能为空"})
                    return
                if not name.endswith(".json"):
                    name += ".json"
                script_dir = PROJECT_ROOT / "scripts"
                script_dir.mkdir(parents=True, exist_ok=True)
                save_path = (script_dir / name).resolve()
                # 安全校验：防止路径逃逸
                if not str(save_path).startswith(str(script_dir.resolve())):
                    self._send_json_response({"success": False, "error": "路径非法"})
                    return
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_text(content, encoding="utf-8")
                print(f"[SaveWorkflow] 已保存: {save_path}")
                self._send_json_response({"success": True, "path": str(save_path)})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

        def handle_delete_workflow(self, data):
            """删除 scripts/ 下的文件或目录"""
            import shutil
            try:
                name = data.get("name", "").strip().replace("\\", "/")
                is_dir = data.get("is_dir", False)
                if not name:
                    self._send_json_response({"success": False, "error": "名称不能为空"})
                    return
                script_dir = PROJECT_ROOT / "scripts"
                if is_dir:
                    del_path = (script_dir / name).resolve()
                else:
                    if not name.endswith(".json"):
                        name += ".json"
                    del_path = (script_dir / name).resolve()
                # 安全校验：防止路径逃逸
                if not str(del_path).startswith(str(script_dir.resolve())):
                    self._send_json_response({"success": False, "error": "路径非法"})
                    return
                if not del_path.exists():
                    self._send_json_response({"success": False, "error": f"不存在: {name}"})
                    return
                if del_path.is_dir():
                    shutil.rmtree(del_path)
                    print(f"[DeleteWorkflow] 已删除目录: {del_path}")
                else:
                    del_path.unlink()
                    print(f"[DeleteWorkflow] 已删除文件: {del_path}")
                self._send_json_response({"success": True})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

        def handle_load_workflow(self, data):
            """从 scripts/ 加载工作流（支持子文件夹路径）"""
            try:
                name = data.get("name", "").strip().replace("\\", "/")
                if not name:
                    self._send_json_response({"success": False, "error": "文件名不能为空"})
                    return
                if not name.endswith(".json"):
                    name += ".json"
                script_dir = PROJECT_ROOT / "scripts"
                load_path = (script_dir / name).resolve()
                if not str(load_path).startswith(str(script_dir.resolve())):
                    self._send_json_response({"success": False, "error": "路径非法"})
                    return
                if not load_path.exists():
                    self._send_json_response({"success": False, "error": f"文件不存在: {name}"})
                    return
                content = load_path.read_text(encoding="utf-8")
                # 返回相对路径（不含 scripts/ 前缀）
                rel = str(load_path.relative_to(script_dir)).replace("\\", "/")
                self._send_json_response({"success": True, "content": content, "name": rel})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

        def handle_list_workflows(self):
            """递归列出 scripts/ 目录下的所有 .json 文件"""
            try:
                script_dir = PROJECT_ROOT / "scripts"
                if not script_dir.exists():
                    self._send_json_response({"success": True, "files": []})
                    return
                files = sorted(
                    str(f.relative_to(script_dir)).replace("\\", "/")
                    for f in script_dir.rglob("*.json")
                    if f.is_file()
                )
                self._send_json_response({"success": True, "files": files})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

        def handle_list_images(self):
            """递归列出 assets/images/ 下所有子目录的图片文件"""
            try:
                img_dir = PROJECT_ROOT / "assets" / "images"
                if not img_dir.exists():
                    self._send_json_response({"success": True, "files": []})
                    return
                exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
                files = sorted(
                    str(f.relative_to(img_dir))
                    for f in img_dir.rglob("*")
                    if f.is_file() and f.suffix.lower() in exts
                )
                self._send_json_response({"success": True, "files": files})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

        def handle_list_scripts(self):
            """递归列出 scripts/ 目录下的 .json 脚本文件"""
            try:
                script_dir = PROJECT_ROOT / "scripts"
                if not script_dir.exists():
                    self._send_json_response({"success": True, "files": []})
                    return
                files = sorted(
                    str(f.relative_to(script_dir)).replace("\\", "/")
                    for f in script_dir.rglob("*.json")
                    if f.is_file()
                )
                self._send_json_response({"success": True, "files": files})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

        def handle_get_image(self, data):
            """读取 assets/images/ 下的图片并返回 base64"""
            try:
                name = data.get("name", "")
                if not name:
                    self._send_json_response({"success": False, "error": "文件名不能为空"})
                    return
                img_path = PROJECT_ROOT / "assets" / "images" / name
                if not img_path.exists():
                    self._send_json_response({"success": False, "error": f"图片不存在: {name}"})
                    return
                import base64
                img_bytes = img_path.read_bytes()
                ext = img_path.suffix.lower()
                mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".bmp": "image/bmp", ".gif": "image/gif", ".webp": "image/webp"}
                mime = mime_map.get(ext, "image/png")
                b64 = base64.b64encode(img_bytes).decode("ascii")
                data_url = f"data:{mime};base64,{b64}"
                self._send_json_response({"success": True, "data_url": data_url, "name": name})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

        def handle_get_thumbnail(self, data):
            """读取 assets/images/ 下的图片，缩小后返回缩略图 base64"""
            try:
                name = data.get("name", "")
                size = int(data.get("size", 120))
                if not name:
                    self._send_json_response({"success": False, "error": "文件名不能为空"})
                    return
                img_path = PROJECT_ROOT / "assets" / "images" / name
                if not img_path.exists():
                    self._send_json_response({"success": False, "error": f"图片不存在: {name}"})
                    return

                import cv2, numpy as np, base64
                arr = np.fromfile(img_path, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    self._send_json_response({"success": False, "error": "图片解码失败"})
                    return

                h, w = img.shape[:2]
                if h > w:
                    new_h, new_w = size, max(1, int(w * size / h))
                else:
                    new_w, new_h = size, max(1, int(h * size / w))
                thumb = cv2.resize(img, (new_w, new_h))

                _, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 65])
                b64 = base64.b64encode(buf).decode("ascii")

                ext = img_path.suffix.lower()
                mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".bmp": "image/bmp", ".gif": "image/gif", ".webp": "image/webp"}
                mime = mime_map.get(ext, "image/jpeg")
                self._send_json_response({"success": True, "data_url": f"data:{mime};base64,{b64}"})
            except Exception as e:
                self._send_json_response({"success": False, "error": str(e)}, 500)

    httpd = socketserver.TCPServer(
        ("127.0.0.1", port),
        CustomHTTPRequestHandler
    )

    print(f"[HTTP] Server started at http://127.0.0.1:{port}")
    httpd.serve_forever()


def generate_loader(nodes_dir=NODES_DIR, loader_file=LOADER_FILE):
    loader_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "// taskflow/core/loader.js",
        "export async function loadAllNodes(){"
    ]

    for p in nodes_dir.rglob("*.js"):
        rel = p.relative_to(TASKFLOW_ROOT).as_posix()
        lines.append(f'    await import("../{rel}?t=" + Date.now());')

    lines.append("}")
    loader_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"[Loader] Generated {loader_file}")


def start_taskflow_server(account: dict, open_browser: bool = False):
    """
    启动Taskflow服务器
    :param account: 账号字典，包含 name 和 email
    :param open_browser: 是否自动打开浏览器
    """
    import webbrowser
    import threading
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        SELENIUM_AVAILABLE = True
    except ImportError:
        SELENIUM_AVAILABLE = False
        print("[Taskflow] 警告: Selenium未安装，将使用默认浏览器")

    account_name = account.get('name', 'default')
    account_email = account.get('email', '')

    os.chdir(PROJECT_ROOT)
    http_port = get_free_port()
    ws_port = get_free_port(http_port + 1)
    realtime_ws_port = ws_port + 100  # 实时WebSocket端口

    # 先启动HTTP服务器
    http_thread = threading.Thread(
        target=start_http_server,
        args=(http_port,),
        daemon=True
    )
    http_thread.start()

    # 等待HTTP服务器启动
    time.sleep(1.0)

    # 生成loader
    generate_loader()

    # 设置 current_account（通过原地修改以同步 backend_handler 的引用）
    current_account.clear()
    current_account.update({
        'name': account_name,
        'email': account_email
    })

    # 存储端口信息供后续使用
    global server_ports
    server_ports = {
        'http_port': http_port,
        'ws_port': ws_port,
        'realtime_ws_port': realtime_ws_port,
        'account': account
    }

    # 新架构：启动全局TaskFlow（无tab）
    if open_browser:
        # 后台加载全局TaskFlow页面，不传递账号参数
        url = f"http://127.0.0.1:{http_port}/taskflow/index.html"
        print(f"[Taskflow] 正在后台加载全局TaskFlow页面: {url}")

        # 使用无头模式在后台加载页面
        if SELENIUM_AVAILABLE:
            options = ChromeOptions()
            options.add_argument('--headless')  # 无头模式
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-web-security')
            options.add_argument('--allow-running-insecure-content')

            # 创建无头浏览器实例
            browser = webdriver.Chrome(options=options)
            browser.get(url)
            print(f"[Taskflow] 已在后台打开全局TaskFlow，等待页面完全加载后创建账号tab")
        else:
            # 回退到默认浏览器
            webbrowser.open(url)
            print(f"[Taskflow] 已使用默认浏览器打开TaskFlow，等待页面完全加载后创建账号tab")

        # 新架构：等待TaskFlow完全加载后再发送任务
        def send_create_tab_task():
            print(f"[Taskflow] 等待TaskFlow页面完全加载...")
            # 等待8秒，确保页面完全加载和WebSocket连接建立
            time.sleep(8)

            # 再等待2秒，确保WebSocket连接稳定
            print(f"[Taskflow] 等待WebSocket连接稳定...")
            time.sleep(2)

            try:
                print(f"[Taskflow] 发送创建tab任务: {account_name} ({account_email})")

                # 创建实时WebSocket连接发送任务
                task_message = {
                    "type": "command",
                    "payload": {
                        "type": "create_tab",
                        "account_info": account
                    }
                }

                async def send_task():
                    try:
                        async with websockets.connect(f"ws://127.0.0.1:{realtime_ws_port}", max_size=20 * 1024 * 1024) as websocket:
                            await websocket.send(json.dumps(task_message))
                            print(f"[Taskflow] 创建tab任务已发送")
                    except Exception as e:
                        print(f"[Taskflow] 发送创建tab任务失败: {e}")
                        # 如果连接失败，再等待3秒后重试
                        print(f"[Taskflow] 3秒后重试...")
                        time.sleep(3)
                        try:
                            async with websockets.connect(f"ws://127.0.0.1:{realtime_ws_port}", max_size=20 * 1024 * 1024) as websocket:
                                await websocket.send(json.dumps(task_message))
                                print(f"[Taskflow] 重试发送成功")
                        except Exception as retry_e:
                            print(f"[Taskflow] 重试也失败: {retry_e}")

                # 在新的事件循环中运行
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(send_task())
                loop.close()

            except Exception as e:
                print(f"[Taskflow] 创建任务线程失败: {e}")

        # 启动任务发送线程
        task_thread = threading.Thread(target=send_create_tab_task, daemon=True)
        task_thread.start()

    else:
        print(f"[Taskflow] 已为账号 {account_name} 启动服务器，端口: {http_port}")
        print(f"[Taskflow] 实时WebSocket端口: {realtime_ws_port}")

    # 启动文件监控，监听打开浏览器信号
    def monitor_signals():
        signal_file = PROJECT_ROOT / f"taskflow_{account_name}_open_browser.signal"
        while True:
            try:
                if signal_file.exists():
                    # 读取信号文件
                    signal_data = json.loads(signal_file.read_text(encoding="utf-8"))
                    signal_file.unlink()  # 删除信号文件

                    if signal_data.get("action") == "open_browser":
                        url = f"http://127.0.0.1:{http_port}/taskflow/index.html?name={account_name}&email={account_email}"
                        print(f"[Taskflow] 收到打开浏览器信号，正在打开: {url}")
                        webbrowser.open(url)

            except Exception as e:
                print(f"[Taskflow] 信号监控错误: {e}")

            time.sleep(0.5)

    # 启动信号监控线程
    signal_thread = threading.Thread(target=monitor_signals, daemon=True)
    signal_thread.start()

    # 启动WebSocket服务器（包括普通和实时）
    async def start_all_ws_servers():
        # 启动普通WebSocket服务器
        print(f"[Taskflow] 启动普通WebSocket服务器，端口: {ws_port}")
        ws_server = await websockets.serve(ws_handler, "127.0.0.1", ws_port, max_size=20 * 1024 * 1024)
        print(f"[Taskflow] 普通WebSocket服务器已启动: ws://127.0.0.1:{ws_port}")

        # 启动实时WebSocket服务器
        print(f"[Taskflow] 启动实时WebSocket服务器，端口: {realtime_ws_port}")
        realtime_server = await websockets.serve(ws_handler, "127.0.0.1", realtime_ws_port, max_size=20 * 1024 * 1024)
        print(f"[Taskflow] 实时WebSocket服务器已启动: ws://127.0.0.1:{realtime_ws_port}")

        # 更新ws_port.js文件
        port_file = TASKFLOW_ROOT / "ws_port.js"
        with open(port_file, 'w', encoding='utf-8') as f:
            f.write(f"export const WS_PORT = {ws_port};")
        print(f"[Taskflow] 更新端口文件: {port_file}")

        # 保持服务器运行，不等待关闭
        print(f"[Taskflow] WebSocket服务器正在运行...")

        # 无限等待，保持服务器运行
        await asyncio.Future()  # 永远不会完成的Future

    asyncio.run(start_all_ws_servers())


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        import json
        account = json.loads(sys.argv[1])
    else:
        # 从环境变量获取账号信息
        account_name = os.environ.get('TASKFLOW_ACCOUNT_NAME', 'test_account')
        account_email = os.environ.get('TASKFLOW_ACCOUNT_EMAIL', 'test@example.com')

        account = {
            'name': account_name,
            'email': account_email
        }

    # 新架构测试：启动TaskFlow并自动创建tab
    print(f"[Taskflow] ========== 启动新架构测试 ==========")
    print(f"[Taskflow] 测试账号: {account_name} ({account_email})")
    print(f"[Taskflow] 将启动全局TaskFlow，3秒后创建账号绑定tab")
    print(f"[Taskflow] ==========================================")

    start_taskflow_server(account, open_browser=True)
