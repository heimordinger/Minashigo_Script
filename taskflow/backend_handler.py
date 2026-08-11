# taskflow/backend_handler.py
"""TaskFlow 后端执行层 — 每个任务一个简单函数，直接调用 UserBrowser 方法"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys

import websockets

# ==============================================================
# 跨事件循环代理（WS 线程 → Controller 线程）
# ==============================================================
_main_loop = None


def set_main_loop(loop):
    global _main_loop
    _main_loop = loop


def get_main_loop():
    return _main_loop


class MainLoopProxy:
    """将目标对象的所有 async 方法调用投递到主事件循环执行"""

    def __init__(self, obj):
        self._obj = obj

    def __getattr__(self, name):
        attr = getattr(self._obj, name)
        if not callable(attr):
            return attr
        if asyncio.iscoroutinefunction(attr):
            loop = _main_loop
            if loop is None or loop is asyncio.get_running_loop():
                return attr
            async def wrapper(*args, **kwargs):
                future = asyncio.run_coroutine_threadsafe(
                    attr(*args, **kwargs), loop
                )
                return await asyncio.wrap_future(future)
            return wrapper
        return attr


# ==============================================================
# 全局状态
# ==============================================================
connected_clients = set()
current_account: dict = {}
browsers: dict[str, 'UserBrowser'] = {}  # type: ignore[name-defined]  # 避免顶层导入 browser 包（循环导入问题）


# ==============================================================
# 浏览器实例同步（从 Controller 拉取）
# ==============================================================
def sync_browser_from_controller(account_email: str):
    try:
        print(f"[SYNC] ========== 开始同步浏览器实例: {account_email} ==========")
        print(f"[SYNC] 当前TaskFlow browsers: {list(browsers.keys())}")

        if account_email in browsers:
            print(f"[SYNC] ✅ 浏览器实例已存在: {account_email}")
            return True

        print(f"[SYNC] 尝试方式1: 从controller.ctrl模块导入")
        try:
            import os
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            import controller.ctrl as ctrl_module

            if hasattr(ctrl_module, 'browsers'):
                module_browsers = getattr(ctrl_module, 'browsers', {})
                print(f"[SYNC] controller.ctrl模块browsers: {list(module_browsers.keys())}")
                if account_email in module_browsers:
                    browsers[account_email] = module_browsers[account_email]
                    print(f"[SYNC] ✅ 从controller.ctrl模块同步成功: {account_email}")
                    return True
                else:
                    print(f"[SYNC] controller.ctrl模块中没有找到: {account_email}")
            else:
                print(f"[SYNC] controller.ctrl模块没有browsers属性")

        except ImportError as e:
            print(f"[SYNC] 导入controller.ctrl失败: {e}")
        except Exception as e:
            print(f"[SYNC] 访问controller.ctrl失败: {e}")

        print(f"[SYNC] 尝试方式2: 从全局作用域查找")
        try:
            global_vars = globals()
            for var_name, var_value in global_vars.items():
                if var_name == 'browsers' and isinstance(var_value, dict):
                    print(f"[SYNC] 找到全局browsers: {list(var_value.keys())}")
                    if account_email in var_value:
                        browsers[account_email] = var_value[account_email]
                        print(f"[SYNC] ✅ 从全局browsers同步成功: {account_email}")
                        return True

        except Exception as e:
            print(f"[SYNC] 全局作用域查找失败: {e}")

        print(f"[SYNC] ❌ 所有同步方式都失败了")
        print(f"[SYNC] 最终状态:")
        print(f"[SYNC]   - 目标账号: {account_email}")
        print(f"[SYNC]   - 可用实例: {list(browsers.keys())}")
        print(f"[SYNC]   - 同步结果: 失败")
        return False

    except Exception as e:
        print(f"[SYNC] ❌ 同步浏览器实例异常: {e}")
        import traceback
        traceback.print_exc()
        return False


# ==============================================================
# 工具函数
# ==============================================================
def _get_browser(account=None):
    """根据 account 或 current_account 获取浏览器实例"""
    email = ''
    if isinstance(account, dict):
        email = account.get('email', '')
    if not email:
        email = current_account.get('email', '')
    if not email:
        return None
    return browsers.get(email)


async def _get_healthy_browser(account=None):
    """获取浏览器实例并检测 Playwright 连接是否存活，断开则自动清理"""
    browser = _get_browser(account)
    if not browser:
        return None
    try:
        alive = await browser.check_connection()
        if not alive:
            raise ConnectionError("connection dead")
        return browser
    except Exception as e:
        email = ''
        if isinstance(account, dict):
            email = account.get('email', '')
        if not email:
            email = current_account.get('email', '')
        if email:
            print(f"[HEALTH] ❌ 浏览器连接已断开，移除实例: {email}")
            browsers.pop(email, None)
        return None


# ==============================================================
# 任务函数 — 每个都是独立函数，直接调 UserBrowser 方法
# ==============================================================

async def update_frame(save_screenshot=False, account=None):
    """刷新帧"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    await browser.update_frame(save_screenshot=save_screenshot)
    return {"success": True}


async def click(x, y, account=None):
    """点击坐标"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    await browser.click(x=x, y=y)
    return {"success": True, "clicked": True, "x": x, "y": y}


async def match_image(image, threshold=0.9, use_color_check=False,
                      match_select="best", account=None):
    """匹配图片"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    result = await browser.match_image(
        image, threshold=threshold,
        use_color_check=use_color_check, match_select=match_select,
    )
    return {
        "success": result.match_success,
        "x": result.x, "y": result.y,
        "max_val": result.max_val,
        "image": image,
    }


async def click_image(image, pianyi_x=0, pianyi_y=0, down_time=0.12,
                      threshold=0.9,
                      use_color_check=False, match_select="best", account=None):
    """点击图片"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}

    # 每次点击前刷新帧，确保匹配基于最新画面
    # await browser.update_frame()

    # 匹配获取坐标
    match = await browser.match_image(
        image, threshold=threshold,
        use_color_check=use_color_check, match_select=match_select,
    )
    if not match or match.x is None:
        return {"success": False, "clicked": False,
                "clicked_x": None, "clicked_y": None,
                "match_value": match.max_val if match else 0}

    # 再点击已匹配到的坐标
    await browser.click(x=match.x, y=match.y,
                        pianyi=(pianyi_x, pianyi_y),
                        down_time=down_time)

    return {"success": True, "clicked": True,
            "clicked_x": match.x, "clicked_y": match.y,
            "match_value": match.max_val}


async def click_text(text, account=None):
    """点击文字"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    result = await browser.click_text(text=text)
    return {"success": True, "clicked": True, "text": text,
            "clicked_x": result.x if result else 0,
            "clicked_y": result.y if result else 0}


async def click_until_gone(image, timeout=10, account=None):
    """点击直到图片消失"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    result = await browser.click_until_gone(
        image, timeout=timeout,
    )
    return {"success": bool(result), "click_count": 1 if result else 0, "timeout": timeout}


async def wait_image(image, timeout=60000, account=None):
    """等待图片出现"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    result = await browser.wait_image(image, timeout=timeout)
    return {"success": True, "found": result}


async def dmm_login(game_name, account=None):
    """DMM 登录"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    result = await browser.dmm_login(game_name=game_name)
    return {"success": True, "logged_in": True, "game_name": game_name, "result": result}


async def b_sleep(seconds, upper_limit=None):
    """休眠（不依赖浏览器）"""
    s = float(seconds or 0)
    if upper_limit is not None:
        s = min(s, float(upper_limit))
    await asyncio.sleep(max(0.0, s))
    return {"slept": s}


async def goto(url, account=None, websocket=None):
    """页面跳转，websocket 可选，用于推送中途 URL 变化"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    if not url:
        return {"success": False, "error": "URL不能为空"}

    original_url = ""
    try:
        original_url = browser.page.url
    except:
        pass

    goto_task = asyncio.create_task(browser.goto(url))
    url_changed = False
    while not goto_task.done():
        await asyncio.sleep(0.3)
        try:
            current_url = browser.page.url
            if current_url and current_url != original_url and not url_changed:
                url_changed = True
                if websocket:
                    try:
                        await websocket.send(json.dumps({
                            "type": "ping", "url_changed": True, "new_url": current_url
                        }))
                    except:
                        pass
        except:
            pass

    try:
        await goto_task
    except Exception as e:
        if not url_changed:
            return {"success": False, "error": str(e)}

    import random
    if url_changed:
        await asyncio.sleep(random.uniform(0.5, 2.0))

    return {"success": True, "url": url, "navigated": True,
            "final_url": browser.page.url if hasattr(browser, 'page') else url}


# ==============================================================
# 滚动操作
# ==============================================================

async def scroll(delta_x=0, delta_y=0, x=None, y=None, steps=10,
                 scroll_time=0.3, account=None):
    """增量滚动"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    await browser.scroll(
        delta_x=delta_x, delta_y=delta_y,
        x=x, y=y, steps=steps, scroll_time=scroll_time,
    )
    return {"success": True, "delta_x": delta_x, "delta_y": delta_y}


async def scroll_to_bottom(smooth=True, step_size=300, interval=0.1, account=None):
    """滚动到底部"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    await browser.scroll_to_bottom(smooth=smooth, step_size=step_size, interval=interval)
    return {"success": True}


async def scroll_to_top(smooth=True, account=None):
    """滚动到顶部"""
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    await browser.scroll_to_top(smooth=smooth)
    return {"success": True}


# ==============================================================
# 账号操作（含子操作分发）
# ==============================================================

async def account_start_browser(account, params):
    return {"success": False, "error": f"请先通过主程序界面启动账号 {account.get('name', '')} 的浏览器"}

async def account_stop_browser(account, params):
    email = account.get('email', '')
    name = account.get('name', '')
    browser = browsers.pop(email, None)
    if browser:
        await browser.stop()
    print(f"[BROWSER] 停止: {name}({email})")
    return {"success": True, "status": "stopped", "account": name}

async def account_restart_browser(account, params):
    r1 = await account_stop_browser(account, params)
    if not r1.get("success"):
        return r1
    return await account_start_browser(account, params)

async def account_get_status(account, params):
    email = account.get('email', '')
    name = account.get('name', '')
    return {"account": name, "has_browser": email in browsers,
            "browser_status": "running" if email in browsers else "stopped"}

async def account_execute_script(account, params):
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    script = params.get('script')
    result = await browser.execute_script(script)
    return {"success": True, "script": script, "account": account.get('name', ''), "result": result}

async def account_take_screenshot(account, params):
    browser = await _get_healthy_browser(account)
    if not browser:
        return {"success": False, "error": "浏览器未启动或连接已断开"}
    result = await browser.take_screenshot()
    return {"success": True, "account": account.get('name', ''), "result": result}

_ACCOUNT_HANDLERS = {
    "start_browser": account_start_browser,
    "stop_browser": account_stop_browser,
    "restart_browser": account_restart_browser,
    "get_status": account_get_status,
    "execute_script": account_execute_script,
    "take_screenshot": account_take_screenshot,
}

async def account_operation(operation, parameters=None, account=None):
    """账号操作总入口"""
    parameters = parameters or {}
    acct = account or current_account
    print(f"[ACCOUNT_OPERATION] 账号: {acct.get('name')}({acct.get('email')}), 操作: {operation}")
    handler = _ACCOUNT_HANDLERS.get(operation)
    if not handler:
        return {"success": False, "error": f"不支持的操作: {operation}"}
    try:
        result = await handler(acct, parameters)
        return {"success": True, "account": acct.get('name', ''), "operation": operation, "result": result}
    except Exception as e:
        return {"success": False, "account": acct.get('name', ''), "operation": operation, "error": str(e)}


# ==============================================================
# 任务函数映射表（ws_handler 用它做路由）
# ==============================================================
TASK_FUNCS = {
    "click": click,
    "update_frame": update_frame,
    "match_image": match_image,
    "click_image": click_image,
    "click_text": click_text,
    "click_until_gone": click_until_gone,
    "wait_image": wait_image,
    "dmm_login": dmm_login,
    "b_sleep": b_sleep,
    "goto": goto,
    "scroll": scroll,
    "scroll_to_bottom": scroll_to_bottom,
    "scroll_to_top": scroll_to_top,
    "account_operation": account_operation,
}


# ==============================================================
# 通用分发入口（供 HTTP API 等外部调用）
# ==============================================================

async def dispatch(task_name: str, props: dict, websocket=None):
    """按 task_name 路由到对应的任务函数"""
    if task_name == "node_event":
        return {"accepted": True, "kind": "node_event"}
    func = TASK_FUNCS.get(task_name)
    if not func:
        return {"accepted": True, "kind": "unknown_task", "task_name": task_name}
    kwargs = dict(props)
    if task_name == "goto":
        kwargs["websocket"] = websocket
    # 只传函数接受的参数（过滤前端多余的属性）
    sig = inspect.signature(func)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    try:
        return await func(**filtered)
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==============================================================
# WebSocket 消息处理
# ==============================================================

async def ws_handler(websocket):
    print("[WS] Client connected:", websocket.remote_address)
    connected_clients.add(websocket)

    try:
        await asyncio.sleep(0.5)
        await websocket.send(json.dumps({
            "success": True, "message": "WebSocket connection established", "type": "welcome"
        }, ensure_ascii=False))

        async for message in websocket:
            try:
                data = json.loads(message)
            except Exception as e:
                await websocket.send(json.dumps({
                    "success": False, "error": f"无效 JSON: {e}"
                }, ensure_ascii=False))
                continue

            msg_type = data.get("type")

            # ---- 命令类型（create_tab 等） ----
            if msg_type == "command":
                payload = data.get("payload", {})
                cmd = payload.get("type")
                if cmd == "create_tab":
                    print(f"[WS] 收到创建tab命令: {payload.get('account_info')}")
                    for client in connected_clients:
                        if client != websocket:
                            try:
                                await client.send(json.dumps({"type": "command", "payload": payload},
                                                             ensure_ascii=False))
                            except Exception:
                                pass
                    await websocket.send(json.dumps({
                        "success": True, "type": "command_response",
                        "message": f"创建tab命令已处理: {payload.get('account_info', {}).get('name')}"
                    }, ensure_ascii=False))
                else:
                    await websocket.send(json.dumps({
                        "success": False, "type": "command_response",
                        "error": f"未知命令类型: {cmd}"
                    }, ensure_ascii=False))
                continue

            # ---- 任务类型 ----
            task = data.get("task") or {}
            meta = data.get("meta") or {}
            task_name = task.get("task_name")
            props = task.get("properties") or {}

            # 补充 account 信息
            if 'account' not in props and meta.get('account'):
                props['account'] = {'email': meta['account'], 'name': meta.get('account', 'unknown')}

            # node_event 纯日志，静默跳过
            if task_name == "node_event":
                continue

            # stop/pause/resume：控制后端正在执行的任务
            if task_name in ("stop_task", "pause_task", "resume_task"):
                account = props.get('account', {})
                email = account.get('email', '') if isinstance(account, dict) else ''
                targets = [browsers[email]] if email and email in browsers else list(browsers.values())
                action_map = {"stop_task": "stop", "pause_task": "pause", "resume_task": "resume"}
                method_name = action_map[task_name]
                for b in targets:
                    ctrl = getattr(b, '_task_ctrl', None)
                    if ctrl:
                        getattr(ctrl, method_name)()
                await websocket.send(json.dumps({"success": True, "task_name": task_name, "data": {method_name + "ed": True}}, ensure_ascii=False))
                continue

            if not task_name:
                await websocket.send(json.dumps({"success": False, "error": "task_name 为空"}, ensure_ascii=False))
                continue

            result = await dispatch(task_name, props, websocket=websocket)
            await websocket.send(json.dumps({
                "success": True, "meta": {"id": meta.get("id")},
                "task_name": task_name, "data": result,
            }, ensure_ascii=False))

    finally:
        connected_clients.discard(websocket)
        print("[WS] Client disconnected:", websocket.remote_address)


async def start_ws_server(host="127.0.0.1", port=8080):
    try:
        print(f"[WS] Starting WebSocket server on {host}:{port}")
        server = await websockets.serve(ws_handler, host, port, max_size=20 * 1024 * 1024)
        print(f"[WS] Server started successfully at ws://{host}:{port}")
        await server.wait_closed()
    except Exception as e:
        print(f"[WS] Failed to start WebSocket server: {e}")
        raise


def start_websocket_server(port=8011):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_ws_server(port=port))
