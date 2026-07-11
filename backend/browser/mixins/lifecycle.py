# backend/browser/mixins/lifecycle.py
import asyncio
import json
import socket
import time
from pathlib import Path
from typing import Optional

import aiohttp

from backend.browser.launcher import BrowserLauncher
from backend.browser.utils import is_port_in_use, kill_by_port
from core.coord.viewport_context import viewport_ctx
from core.logging.events import LogLevel
from core.state.events import StateEvent, StateDomain
from core.config.config import config
import requests


def is_cdp_alive(port: int) -> bool:
    """检查端口上是否真的有 Chrome CDP 在运行（不只是 HTTP 200）"""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        if r.status_code != 200:
            return False
        data = r.json()
        # CDP 的 /json/version 一定包含 Browser 字段
        return "Browser" in data and "webSocketDebuggerUrl" in data
    except Exception:
        return False


def wait_port(port, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


class LifecycleMixin:

    def start(self, browser_path: Optional[Path] = None, time_out=30_0000):
        msg = f"{self.account['name']} 启动中"
        print(msg)
        self._log("启动浏览器中")

        if browser_path is None:
            browser_path = config.browser_path
        if not browser_path.exists():
            self._log(f"浏览器路径不存在: {browser_path}", LogLevel.ERROR)
            raise FileNotFoundError(browser_path)

        if is_cdp_alive(self.port):
            self._log(f"检测到已有CDP，直接复用 (port={self.port})")
        else:
            self._log(f"未检测到CDP，启动新浏览器 (port={self.port})")
            self.ensure_browser(browser_path)
            self._log("浏览器启动完成，等待连接中")

        print(f"{self.account['name']}({self.port}) 启动完成")
        if self.controller.get_playwright():
            self._log("浏览器实例创建完成")
            self.controller.emit_state(
                StateEvent(
                    account=self.account['name'],
                    domain=StateDomain.BROWSER,
                    key="status",
                    value="已完成启动",
                    message="浏览器实例创建完成"
                )
            )
        else:
            self._log("浏览器启动完成,等待连接中")
            self.controller.emit_state(
                StateEvent(
                    account=self.account['name'],
                    domain=StateDomain.BROWSER,
                    key="status",
                    value="已完成启动",
                    message="浏览器实例创建完成,等待连接中"
                )
            )
        start_time = time.time()
        while not is_port_in_use(self.port):
            if time.time() - start_time > time_out / 1000:
                raise TimeoutError(f"浏览器端口 {self.port} 启动超时")
            time.sleep(0.5)

    # ---------- 等 CDP ----------
    async def wait_for_cdp(self, timeout=180):
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{self.port}/json/version"

        async with aiohttp.ClientSession() as session:
            while time.time() < deadline:
                try:
                    async with session.get(url, timeout=1) as resp:
                        if resp.status == 200:
                            return
                except:
                    await asyncio.sleep(0.5)

        raise TimeoutError(f"CDP not ready on port {self.port}")

    # ---------- 连接 Playwright ----------
    async def connect(self):
        async def wait_for_playwright(timeout=120):
            start = asyncio.get_event_loop().time()
            while self.controller.get_playwright() is None:
                if getattr(self.controller, '_pw_failed', False):
                    raise RuntimeError("Playwright 初始化已失败，无法连接")
                if asyncio.get_event_loop().time() - start > timeout:
                    raise TimeoutError("Playwright init timeout")
                await asyncio.sleep(0.05)

        self._log("[CONNECT-00] enter connect()")

        # ====== 强制清理旧状态 ======
        if getattr(self, "browser", None):
            self._log("[CONNECT-01] existing browser detected, closing")
            try:
                await self.browser.close()
                self._log("[CONNECT-01A] old browser closed")
            except Exception as e:
                self._log(f"[CONNECT-01E] close old browser failed: {e}", level=LogLevel.WARNING)

            self.browser = None
            self.context = None
            self.page = None

        if getattr(self, "_page_watch_task", None):
            self._log("[CONNECT-02] cancel page watcher task")
            self._page_watch_task.cancel()
            self._page_watch_task = None

        try:
            # ====== 等待 CDP ======
            self._log("[CONNECT-10] wait_for_cdp() start")

            cdp_ok = False
            for i in range(12):  # 最多重试12次（12×5=60s=与 ensure_browser 超时一致）
                try:
                    await self.wait_for_cdp(timeout=5)
                    cdp_ok = True
                    self._log(f"[CONNECT-11] CDP ready (try={i + 1})")
                    break
                except Exception as e:
                    self._log(f"[CONNECT-10E] CDP not ready (try={i + 1}): {e}")
                    await asyncio.sleep(1)

            if not cdp_ok:
                raise RuntimeError("CDP不可用，无法连接浏览器")

            self._log("[CONNECT-15] wait_for_playwright() start")
            await wait_for_playwright()
            self._log("[CONNECT-16] wait_for_playwright() done")

            # ====== 连接 CDP ======
            self._log(f"[CONNECT-20] connect_over_cdp start (port={self.port})")

            for i in range(3):
                try:
                    self.browser = await self.controller.get_playwright().chromium.connect_over_cdp(
                        f"http://127.0.0.1:{self.port}"
                    )
                    self._log(f"[CONNECT-21] connect_over_cdp success (try={i + 1})")
                    break
                except Exception as e:
                    self._log(f"[CONNECT-20E] connect_over_cdp failed (try={i + 1}): {e}")
                    # 如果不是最后一次失败，可能是 CDP 信息不准确，杀掉进程下次循环时重启
                    if i < 2 and ("not valid JSON" in str(e) or "Connection closed" in str(e)):
                        self._log(f"[CONNECT-20E] CDP 无效，清理端口 {self.port} 后重试")
                        from backend.browser.utils import kill_by_port
                        kill_by_port(self.port)
                        await asyncio.sleep(2)
                        # 重新启动浏览器进程
                        self._closed = False
                        if not is_cdp_alive(self.port):
                            self._log(f"[CONNECT-20E] 重启浏览器 (port={self.port})")
                            self.ensure_browser(config.browser_path)
                    else:
                        await asyncio.sleep(1)
            else:
                raise RuntimeError("connect_over_cdp 多次失败")

            # ====== context ======
            self._log(f"[CONNECT-30] contexts count = {len(self.browser.contexts)}")
            self.context = self.browser.contexts[0]
            self._log("[CONNECT-31] context selected")

            # ====== page ======
            if self.context.pages:
                self._log(f"[CONNECT-40] reuse existing page (count={len(self.context.pages)})")
                # 优先选非空白页（仪表盘），避免取到空白页
                best = self.context.pages[0]
                for p in self.context.pages:
                    try:
                        u = p.url
                        if u and "dashboard" in u:
                            best = p
                            break
                        if u and u not in ("about:blank", "chrome://newtab/", ""):
                            best = p
                    except Exception:
                        pass
                self.page = best
                # 关闭其他标签页
                for p in self.context.pages:
                    if p != self.page:
                        try:
                            await p.close()
                            self._log("[CONNECT-40A] closed extra page")
                        except Exception:
                            pass
            else:
                self._log("[CONNECT-41] no page found, creating new page")
                self.page = await self.context.new_page()
                self._log("[CONNECT-42] new page created")

            # ====== 页面状态 ======
            self._log("[CONNECT-50] wait_for_load_state(domcontentloaded)")
            await self.page.wait_for_load_state("domcontentloaded")
            self._log("[CONNECT-51] domcontentloaded reached")

            # 导航到仪表盘（如果当前不是仪表盘的话）
            dash_url = getattr(self, '_dash_url', None)
            if dash_url:
                cur = self.page.url
                if "dashboard" not in cur:
                    self._log(f"[CONNECT-52] navigating to dashboard")
                    await self.page.goto(dash_url, wait_until="domcontentloaded")
                    self._log("[CONNECT-53] dashboard loaded")
                else:
                    self._log("[CONNECT-53] already on dashboard")

            # ====== 固定窗口标题（防止被网页顶掉） ======
            self._fixed_title = f"{self.account['name']}"
            safe_title = json.dumps(self._fixed_title)
            await self.context.add_init_script(f"document.title = {safe_title};")
            await self.page.evaluate(f"document.title = {safe_title};")
            self._log(f"[CONNECT-52] fixed window title = {self._fixed_title}")

            # ====== DPR ======
            self._log("[CONNECT-60] evaluate devicePixelRatio")
            self.device_pixel_ratio = await self.page.evaluate("window.devicePixelRatio")
            self._log(f"[CONNECT-61] devicePixelRatio = {self.device_pixel_ratio}")
            viewport_ctx.add_for_account(account=self.account,
                                         dpr=self.device_pixel_ratio)

            # ====== 确保窗口最大化 ======
            try:
                cdp = await self._get_cdp()
                win = await cdp.send("Browser.getWindowForTarget")
                await cdp.send("Browser.setWindowBounds", {
                    "windowId": win["windowId"],
                    "bounds": {"windowState": "maximized"}
                })
                self._log(f"[CONNECT-62] window maximized (id={win['windowId']})")
            except Exception as e:
                self._log(f"[CONNECT-62E] maximize failed: {e}", level=LogLevel.WARNING)

            # ====== watcher ======
            self._log("[CONNECT-70] start page state watcher")
            self._page_watch_task = asyncio.create_task(self._page_state_watcher())

            # ====== 完成 ======
            self._log("[CONNECT-80] browser connected successfully")

            self.controller.emit_state(
                StateEvent(
                    account=self.account['name'],
                    domain=StateDomain.BROWSER,
                    key="ready",
                    value=True
                )
            )
            self._log("[CONNECT-81] ready state emitted")
            self._log("浏览器初始化完成，可以开始使用")

        except asyncio.CancelledError:
            self._log("[CONNECT-CANCEL] connect cancelled, cleaning up", level=LogLevel.WARNING)

            try:
                if self.browser:
                    await self.browser.close()
                    self._log("[CONNECT-CANCEL-A] browser closed after cancel")
            except Exception as e:
                self._log(f"[CONNECT-CANCEL-E] cleanup failed: {e}", level=LogLevel.WARNING)

            raise

        except Exception as e:
            self._log(f"[CONNECT-EXCEPTION] connect failed: {e}", level=LogLevel.ERROR)
            raise

    # ---------- 页面状态监控 ----------
    async def _page_state_watcher(self, interval=15.0):
        last_url = None
        last_title = None
        fixed_title = getattr(self, '_fixed_title', f"{self.account['name']} - Minashigo")
        _title_fix_skip = 0  # 跳过计数器，避免频繁 evaluate

        while not self._closed:
            try:
                url = self.page.url
                title = await self.page.title()

                if url != last_url or title != last_title:
                    last_url = url
                    last_title = title

                    self.url = url
                    self.title = title

                    self.controller.emit_state(
                        StateEvent(
                            account=self.account["name"],
                            domain=StateDomain.BROWSER,
                            key="page",
                            value={"url": url, "title": title},
                            message="page updated"
                        )
                    )

                # 固定窗口标题（防止网页JS篡改），最多每5轮一次 + 5秒超时
                if title != fixed_title and _title_fix_skip <= 0:
                    _title_fix_skip = 5
                    try:
                        await asyncio.wait_for(
                            self.page.evaluate(f"document.title = {json.dumps(fixed_title)};"),
                            timeout=5
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass
                else:
                    _title_fix_skip -= 1
            except Exception:
                pass

            await asyncio.sleep(interval)

    # ---------- 关闭 ----------
    async def close(self):
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True

            try:
                if self.page:
                    await self.page.close()
                if self.context:
                    await self.context.close()
                if self.browser:
                    await self.browser.close()

            except Exception as e:
                self._log(f"关闭浏览器异常: {e}")

            finally:
                kill_by_port(self.port)
                from core.port_manager import port_manager
                port_manager.release_browser_port(self.port)
                print(f"{self.account['name']}: 浏览器已关闭")

    async def recover_from_crash(self):
        """页面崩溃后恢复：reload 当前标签页"""
        self._log("页面崩溃，执行恢复流程", level=LogLevel.WARNING)

        if not self.page:
            self._log("page 不可用，尝试从 context 获取", level=LogLevel.WARNING)
            if self.context and self.context.pages:
                self.page = self.context.pages[0]
            else:
                self._log("无可用标签页", level=LogLevel.ERROR)
                raise RuntimeError("无可用标签页")

        try:
            await self.page.reload(timeout=30000)
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            self._log(f"页面已恢复，当前 URL: {self.page.url}")
        except Exception as e:
            self._log(f"页面恢复失败: {e}", level=LogLevel.ERROR)
            raise

    def ensure_browser(self, browser_path: Path):
        from backend.browser.utils import is_port_in_use, kill_by_port
        from core.port_manager import port_manager
        import time

        # ====== ① CDP存在,直接复用 ======
        if is_cdp_alive(self.port):
            self._log(f"[BROWSER] 复用已有CDP实例 (port={self.port})")
            port_manager.release_browser_port(self.port)
            return

        # ====== 端口被占但没有CDP,强制清理 ======
        if is_port_in_use(self.port):
            self._log(f"[BROWSER] 检测到脏进程，占用端口但无CDP → 强制清理 (port={self.port})")

            kill_by_port(self.port)

            # 等待端口释放
            for _ in range(10):
                if not is_port_in_use(self.port):
                    break
                time.sleep(0.3)
            else:
                raise RuntimeError(f"端口 {self.port} 无法释放")

        # ====== 释放临时端口占用，让 Chrome 绑定 ======
        port_manager.release_browser_port(self.port)

        # ====== 启动新浏览器 ======
        self._log(f"[BROWSER] 启动新浏览器 (port={self.port})")

        # 清理用户数据目录中的会话文件，防止 Chrome 还原旧标签页
        for session_file in ["Current Session", "Current Tabs", "Last Session", "Last Tabs", "Last Active Tabs"]:
            fpath = self.user_data_dir / "Default" / session_file
            if fpath.exists():
                try:
                    fpath.unlink()
                    self._log(f"[BROWSER] 已清除会话文件: {session_file}")
                except Exception as e:
                    self._log(f"[BROWSER] 清除会话文件失败: {session_file} ({e})")

        # 保存仪表盘 URL，connect() 中导航用
        try:
            from core.taskflow_manager import taskflow_manager
            from urllib.parse import quote
            api_port = taskflow_manager.api_port
            name = self.account.get('name', '')
            email = self.account.get('email', '')
            self._dash_url = (f"http://127.0.0.1:{api_port}/dashboard"
                              f"?name={quote(name)}&email={quote(email)}&port={self.port}")
        except Exception:
            self._dash_url = None

        launcher = BrowserLauncher()
        launcher.start(
            browser_path=browser_path,
            user_data=self.user_data_dir,
            port=self.port,
            url="about:blank",
        )

        # ====== 等待CDP ======
        start = time.time()
        while time.time() - start < 60:
            if is_cdp_alive(self.port):
                self._log(f"[BROWSER] CDP已就绪 (port={self.port})")
                return
            time.sleep(0.5)

        raise RuntimeError("CDP启动失败")
