# -*- coding: utf-8 -*-
"""网页 Network 检查器（主项目 GUI 工具）。

也可独立运行::
    python tools/network_inspector.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QSettings, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

BODY_CAPTURE_TYPES = frozenset({"xhr", "fetch", "document", "other"})
BODY_MAX_BYTES = 2 * 1024 * 1024
# 游戏加载诊断时默认隐藏的静态资源类型
_STATIC_TYPES = frozenset({"image", "font", "stylesheet", "media"})
_SLOW_MS_DEFAULT = 800
# 影子行：同 URL 的 other/204，且已有更完整的 xhr/fetch
_SHADOW_TYPES = frozenset({"other"})
_PRIMARY_TYPES = frozenset({"xhr", "fetch", "document", "script"})

try:
    from backend.browser.net_optimize import (
        is_ad_or_tracker_host,
        url_path_key as _url_path_key,
    )
except Exception:  # pragma: no cover
    def is_ad_or_tracker_host(host: str) -> bool:  # type: ignore
        return False

    def _url_path_key(url: str) -> str:  # type: ignore
        return url or ""

CDP_RESOURCE_TYPES = {
    "Document": "document",
    "Stylesheet": "stylesheet",
    "Image": "image",
    "Media": "media",
    "Font": "font",
    "Script": "script",
    "TextTrack": "other",
    "XHR": "xhr",
    "Fetch": "fetch",
    "EventSource": "other",
    "WebSocket": "websocket",
    "Manifest": "other",
    "SignedExchange": "other",
    "Ping": "other",
    "CSPViolationReport": "other",
    "Preflight": "other",
    "Other": "other",
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DEFAULT_PORT = 9333


def _default_browser_path() -> str:
    try:
        from core.config.config import config

        p = config.browser_path
        if p:
            return str(p)
    except Exception:
        pass
    return DEFAULT_EDGE


def _quick_cdp_alive(port: int, timeout: float = 0.25) -> bool:
    """短超时探测 CDP，避免阻塞 UI。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as r:
            data = json.load(r)
        return bool(data.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def discover_cdp_endpoints(facade=None, *, scan_extra: bool = False) -> list[dict]:
    """发现可接入的 CDP。

    默认只列账号运行中浏览器（快）。scan_extra=True 时再轻量扫端口范围。
    """
    out: list[dict] = []
    seen: set[int] = set()
    held: set[int] = set()
    try:
        from core.port_manager import port_manager

        held = set(getattr(port_manager, "_held_ports", {}) or {})
    except Exception:
        pass

    browsers = {}
    if facade is not None:
        try:
            browsers = getattr(facade.controller, "_browsers", None) or {}
        except Exception:
            browsers = {}
    for name, b in browsers.items():
        port = getattr(b, "port", None)
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        if port in seen:
            continue
        seen.add(port)
        alive = _quick_cdp_alive(port)
        tag = "" if alive else "（等待中）"
        out.append(
            {
                "label": f"运行中 · 账号「{name}」 · {port}{tag}",
                "port": port,
                "kind": "account",
                "account": name,
            }
        )

    if not scan_extra:
        return out

    from backend.browser.utils import is_port_in_use

    start, end = 9200, 9220  # 轻量范围
    try:
        from core.port_manager import port_manager

        sp = getattr(port_manager, "service_ports", None)
        if sp is not None:
            start = int(sp.browser_debug_start)
            end = min(int(sp.browser_debug_end) + 1, start + 24)
    except Exception:
        pass

    for port in range(start, end):
        if port in seen:
            continue
        # PortManager 用 socket 占坑的端口：跳过，否则会误判并拖慢探测
        if port in held:
            continue
        if not is_port_in_use(port):
            continue
        if not _quick_cdp_alive(port, timeout=0.2):
            continue
        seen.add(port)
        out.append(
            {
                "label": f"已检测到 CDP · {port}",
                "port": port,
                "kind": "scan",
            }
        )
    return out


class _SourceScanWorker(QThread):
    """后台扫描来源，避免卡死主界面。"""

    finished_ok = Signal(object)  # list[dict]
    failed = Signal(str)

    def __init__(self, facade=None, *, scan_extra: bool = True, parent=None):
        super().__init__(parent)
        self.facade = facade
        self.scan_extra = scan_extra

    def run(self) -> None:
        try:
            self.finished_ok.emit(
                discover_cdp_endpoints(self.facade, scan_extra=self.scan_extra)
            )
        except Exception as e:
            self.failed.emit(str(e))


@dataclass
class NetEntry:
    id: int
    t: float
    method: str
    url: str
    resource_type: str = ""
    status: int | None = None
    ok: bool | None = None
    mime: str = ""
    size: int | None = None
    duration_ms: float | None = None
    request_headers: dict = field(default_factory=dict)
    response_headers: dict = field(default_factory=dict)
    post_data: str = ""
    response_body: str = ""
    # True = response_body 为 CDP 返回的 base64 原文（二进制，勿当 UTF-8 文本）
    response_body_base64: bool = False
    response_json: Any = None
    failure: str = ""
    started_mono: float = 0.0
    _cdp_request_id: str = ""
    _cdp_session_id: str = ""


def entry_to_dict(e: NetEntry) -> dict:
    d = asdict(e)
    for k in ("started_mono", "_cdp_request_id", "_cdp_session_id"):
        d.pop(k, None)
    if not d.get("post_data"):
        d.pop("post_data", None)
    if not d.get("response_body"):
        d.pop("response_body", None)
        d.pop("response_body_base64", None)
    elif not d.get("response_body_base64"):
        d.pop("response_body_base64", None)
    if d.get("response_json") is None:
        d.pop("response_json", None)
    if not d.get("failure"):
        d.pop("failure", None)
    return d


class _TargetConn:
    """单个 page/iframe 目标上的 CDP 连接。"""

    def __init__(self, worker: "NetworkWorker", info: dict, ws: Any):
        self.worker = worker
        self.info = info
        self.ws = ws
        self.sid = info.get("id") or info.get("targetId") or ""
        self._cmd = 0
        self._futures: dict[int, asyncio.Future] = {}

    async def send(self, method: str, params: Optional[dict] = None) -> dict:
        self._cmd += 1
        cid = self._cmd
        fut = asyncio.get_running_loop().create_future()
        self._futures[cid] = fut
        await self.ws.send(
            json.dumps({"id": cid, "method": method, "params": params or {}})
        )
        try:
            return await asyncio.wait_for(fut, timeout=20)
        except Exception:
            self._futures.pop(cid, None)
            raise

    def _resolve(self, data: dict) -> None:
        cid = data.get("id")
        fut = self._futures.pop(cid, None) if cid is not None else None
        if fut is None or fut.done():
            return
        if "error" in data:
            fut.set_exception(RuntimeError(str(data.get("error"))))
        else:
            fut.set_result(data.get("result") or {})

    async def run(self) -> None:
        # 必须先开读循环再 send，否则 Network.enable 的响应无人收取会超时
        async def reader() -> None:
            async for raw in self.ws:
                if self.worker._stop:
                    break
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if "id" in data:
                    self._resolve(data)
                    continue
                method = data.get("method") or ""
                params = data.get("params") or {}
                if method == "Network.requestWillBeSent":
                    self.worker._on_request_will_be_sent(params, self.sid)
                elif method == "Network.responseReceived":
                    self.worker._on_response_received(params, self.sid)
                elif method == "Network.loadingFinished":
                    asyncio.create_task(
                        self.worker._on_loading_finished(params, self.sid, self)
                    )
                elif method == "Network.loadingFailed":
                    self.worker._on_loading_failed(params, self.sid)

        reader_task = asyncio.create_task(reader())
        await asyncio.sleep(0)
        try:
            await self.send("Network.enable")
        except Exception:
            reader_task.cancel()
            raise
        await reader_task


class NetworkWorker(QThread):
    """后台：轮询 /json/list，对每个目标开 CDP Network。"""

    connected = Signal(str)
    failed = Signal(str)
    entry_updated = Signal(object)
    navigated = Signal(str)
    closed = Signal()

    def __init__(
        self,
        *,
        port: int,
        start_url: str,
        browser_path: str,
        launch: bool,
        user_data_dir: str,
        parent=None,
    ):
        super().__init__(parent)
        self.port = port
        self.start_url = start_url.strip() or "about:blank"
        self.browser_path = browser_path
        self.launch = launch
        self.user_data_dir = user_data_dir
        self._stop = False
        self._goto_url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._pending: dict[int, NetEntry] = {}
        self._cdp_req: dict[tuple[str, str], int] = {}
        self._conns: dict[str, _TargetConn] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._target_infos: dict[str, dict] = {}

    def request_goto(self, url: str) -> None:
        self._goto_url = url

    def stop(self) -> None:
        self._stop = True

    def _launch_browser(self) -> None:
        path = Path(self.browser_path)
        if not path.is_file():
            raise FileNotFoundError(f"浏览器不存在: {path}")
        ud = Path(self.user_data_dir)
        ud.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(path),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={ud}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            self.start_url,
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        last_err = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=1
                ) as r:
                    if r.status == 200:
                        return
            except Exception as e:
                last_err = str(e)
            time.sleep(0.25)
        raise TimeoutError(f"CDP 未就绪 port={self.port}: {last_err}")

    @staticmethod
    def _is_noise_url(url: str) -> bool:
        u = (url or "").lower()
        return (
            not u
            or "devtools://" in u
            or u.startswith("blob:devtools")
            or u.startswith("chrome-extension://")
            or u.startswith("edge://")
            or u.startswith("chrome://")
            or u in ("about:blank", "about:srcdoc")
        )

    @staticmethod
    def _want_target(info: dict) -> bool:
        t = (info.get("type") or "").lower()
        # json/list 常见: page / iframe / worker；只要能开 Network 的都试
        if t not in ("page", "iframe"):
            return False
        url = info.get("url") or ""
        if not url or url in ("about:blank", "about:srcdoc"):
            return t == "page"
        return not NetworkWorker._is_noise_url(url)

    def _list_targets(self) -> list[dict]:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json/list", timeout=3
        ) as r:
            data = json.load(r)
        if not isinstance(data, list):
            raise RuntimeError("json/list 返回异常")
        return data

    def _wait_cdp_http(self, timeout: float = 15) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                self._list_targets()
                return
            except Exception as e:
                last = str(e)
            time.sleep(0.25)
        raise TimeoutError(
            f"无法访问 CDP http://127.0.0.1:{self.port}/json/list — {last}"
        )

    def _best_url(self) -> str:
        best = ""
        for info in self._target_infos.values():
            u = info.get("url") or ""
            if self.start_url and self.start_url.rstrip("/") in u:
                return u
            if u.startswith("http") and not best:
                best = u
        return best

    def _pick_page_conn(self) -> Optional[_TargetConn]:
        prefer = None
        fallback = None
        for sid, conn in self._conns.items():
            info = conn.info
            if (info.get("type") or "").lower() != "page":
                continue
            u = info.get("url") or ""
            if self._is_noise_url(u) and u not in ("", "about:blank"):
                continue
            if self.start_url and self.start_url.rstrip("/") in u:
                return conn
            if u.startswith("http") and prefer is None:
                prefer = conn
            if fallback is None:
                fallback = conn
        return prefer or fallback

    async def _navigate(self, url: str, *, soft: bool = False) -> None:
        conn = self._pick_page_conn()
        try:
            if conn is None:
                raise RuntimeError("没有可用的 page 目标，请确认浏览器已打开页面")
            await conn.send("Page.enable")
            await conn.send("Page.navigate", {"url": url})
            self.navigated.emit(url)
        except Exception as e:
            self.failed.emit(
                f"导航失败{'（仍可继续监听）' if soft else ''}: {e}"
            )
            if soft:
                self.navigated.emit(url)

    def _on_request_will_be_sent(self, params: dict, session_id: str) -> None:
        req = params.get("request") or {}
        url = req.get("url") or ""
        if self._is_noise_url(url):
            return
        request_id = params.get("requestId") or ""
        if not request_id:
            return
        key = (session_id, request_id)
        existing_eid = self._cdp_req.get(key)
        if existing_eid is not None:
            entry = self._pending.get(existing_eid)
            if entry is not None:
                entry.url = url
                entry.method = req.get("method") or entry.method
                entry.request_headers = dict(req.get("headers") or {})
                self.entry_updated.emit(entry)
            return

        rtype = CDP_RESOURCE_TYPES.get(params.get("type") or "Other", "other")
        eid = self._next_id
        self._next_id += 1
        entry = NetEntry(
            id=eid,
            t=time.time(),
            method=req.get("method") or "GET",
            url=url,
            resource_type=rtype,
            request_headers=dict(req.get("headers") or {}),
            post_data=req.get("postData") or "",
            started_mono=time.perf_counter(),
            _cdp_request_id=request_id,
            _cdp_session_id=session_id,
        )
        self._pending[eid] = entry
        self._cdp_req[key] = eid
        self.entry_updated.emit(entry)

    def _on_response_received(self, params: dict, session_id: str) -> None:
        request_id = params.get("requestId") or ""
        eid = self._cdp_req.get((session_id, request_id))
        if eid is None:
            return
        entry = self._pending.get(eid)
        if entry is None:
            return
        resp = params.get("response") or {}
        entry.status = resp.get("status")
        try:
            st = int(entry.status) if entry.status is not None else None
        except (TypeError, ValueError):
            st = None
        entry.ok = bool(st is not None and 200 <= st < 400)
        entry.mime = resp.get("mimeType") or ""
        headers = dict(resp.get("headers") or {})
        entry.response_headers = headers
        cl = None
        for k, v in headers.items():
            if str(k).lower() == "content-length":
                cl = v
                break
        try:
            entry.size = int(cl) if cl is not None else entry.size
        except (TypeError, ValueError):
            pass
        if params.get("type"):
            entry.resource_type = CDP_RESOURCE_TYPES.get(
                params["type"], entry.resource_type
            )
        entry.duration_ms = round((time.perf_counter() - entry.started_mono) * 1000, 1)
        self.entry_updated.emit(entry)

    async def _on_loading_finished(
        self, params: dict, session_id: str, conn: _TargetConn
    ) -> None:
        request_id = params.get("requestId") or ""
        eid = self._cdp_req.get((session_id, request_id))
        if eid is None:
            return
        entry = self._pending.get(eid)
        if entry is None:
            return
        encoded = params.get("encodedDataLength")
        if encoded is not None and entry.size is None:
            try:
                entry.size = int(encoded)
            except (TypeError, ValueError):
                pass
        entry.duration_ms = round((time.perf_counter() - entry.started_mono) * 1000, 1)
        if entry.resource_type in BODY_CAPTURE_TYPES:
            try:
                too_big = entry.size is not None and entry.size > BODY_MAX_BYTES
                if not too_big:
                    body = await conn.send(
                        "Network.getResponseBody", {"requestId": request_id}
                    )
                    text = body.get("body") or ""
                    if body.get("base64Encoded"):
                        # 二进制：保留 CDP base64 原文，禁止 utf-8 replace 损坏
                        if text and (len(text) * 3 // 4) <= BODY_MAX_BYTES:
                            entry.response_body = text
                            entry.response_body_base64 = True
                            entry.response_json = None
                    elif text and len(text.encode("utf-8", errors="ignore")) <= BODY_MAX_BYTES:
                        entry.response_body = text
                        entry.response_body_base64 = False
                        ct = (entry.mime or "").lower()
                        if "json" in ct or text[:1] in "{[":
                            try:
                                entry.response_json = json.loads(text)
                            except Exception:
                                pass
            except Exception:
                pass
        self.entry_updated.emit(entry)

    def _on_loading_failed(self, params: dict, session_id: str) -> None:
        request_id = params.get("requestId") or ""
        eid = self._cdp_req.get((session_id, request_id))
        if eid is None:
            return
        entry = self._pending.get(eid)
        if entry is None:
            return
        entry.failure = params.get("errorText") or "failed"
        entry.duration_ms = round((time.perf_counter() - entry.started_mono) * 1000, 1)
        self.entry_updated.emit(entry)

    async def _run_target(self, info: dict) -> None:
        import websockets

        tid = info.get("id") or ""
        ws_url = info.get("webSocketDebuggerUrl") or ""
        if not tid or not ws_url:
            return
        try:
            async with websockets.connect(
                ws_url,
                max_size=64 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=10,
            ) as ws:
                conn = _TargetConn(self, info, ws)
                self._conns[tid] = conn
                self._target_infos[tid] = info
                try:
                    await conn.run()
                finally:
                    self._conns.pop(tid, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._conns.pop(tid, None)
        finally:
            self._tasks.pop(tid, None)

    async def _sync_targets(self) -> int:
        """扫描 json/list，为新目标启动监听。返回当前活跃目标数。"""
        targets = self._list_targets()
        alive = 0
        for info in targets:
            tid = info.get("id") or ""
            if not tid or not self._want_target(info):
                continue
            if not info.get("webSocketDebuggerUrl"):
                continue
            self._target_infos[tid] = info
            alive += 1
            task = self._tasks.get(tid)
            if task is not None and not task.done():
                # 更新 URL 信息
                conn = self._conns.get(tid)
                if conn is not None:
                    conn.info = info
                continue
            self._tasks[tid] = asyncio.create_task(self._run_target(info))
        # 清理已结束 task
        for tid, task in list(self._tasks.items()):
            if task.done():
                self._tasks.pop(tid, None)
        return alive

    async def _cdp_main(self) -> None:
        self._wait_cdp_http(20)

        # 等至少一个可挂目标
        deadline = time.time() + 20
        n = 0
        last_err = ""
        while time.time() < deadline and not self._stop:
            try:
                n = await self._sync_targets()
                if n > 0:
                    break
            except Exception as e:
                last_err = str(e)
            await asyncio.sleep(0.4)
        if n <= 0:
            raise RuntimeError(
                f"端口 {self.port} 上没有可监听的 page/iframe"
                + (f"（{last_err}）" if last_err else "")
                + "。请确认浏览器已开页面，且 CDP 端口正确。"
            )

        # 等连接真正建立
        for _ in range(50):
            if self._conns:
                break
            await asyncio.sleep(0.1)

        cur = self._best_url()
        self.connected.emit(cur or self.start_url)

        if self.launch and self.start_url.startswith("http"):
            if not (cur and self.start_url.rstrip("/") in cur):
                # 等 page conn 就绪再跳
                for _ in range(30):
                    if self._pick_page_conn() is not None:
                        break
                    await asyncio.sleep(0.1)
                await self._navigate(self.start_url, soft=True)
            else:
                self.navigated.emit(cur)
        elif cur:
            self.navigated.emit(cur)

        while not self._stop:
            goto = self._goto_url
            if goto is not None:
                self._goto_url = None
                await self._navigate(goto, soft=False)
            try:
                await self._sync_targets()
            except Exception:
                pass
            await asyncio.sleep(0.5)

        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def run(self) -> None:
        try:
            if self.launch:
                self._launch_browser()
            asyncio.run(self._cdp_main())
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self._conns.clear()
            self._tasks.clear()
            self.closed.emit()


class NetworkInspectorWindow(QMainWindow):
    COLS = ["#", "名称", "方法", "状态", "类型", "耗时ms", "大小", "URL"]
    _instance: "NetworkInspectorWindow | None" = None

    def __init__(self, facade=None, parent=None):
        # 不挂 parent：与 ScriptGenWindow 一样用独立顶层窗，可互相遮挡/切焦点
        super().__init__(None, Qt.WindowType.Window)
        self.setObjectName("NetworkInspectorWindow")
        self.setWindowTitle("网络检查")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._force_closing = False
        try:
            from core.path import ICON_PATH

            self.setWindowIcon(QIcon(str(ICON_PATH)))
        except Exception:
            pass
        self.setMinimumSize(800, 520)
        self.resize(1100, 720)
        self.facade = facade
        self._worker: Optional[NetworkWorker] = None
        self._scan_worker: Optional[_SourceScanWorker] = None
        self._entries: dict[int, NetEntry] = {}
        self._row_by_id: dict[int, int] = {}

        # 主题：优先沿用主窗 qss，否则读当前主题
        parent_qss = (parent.styleSheet() or "").strip() if parent is not None else ""
        if parent_qss:
            self.setStyleSheet(parent_qss)
        else:
            self._apply_theme()

        self._build_ui()
        if not parent_qss:
            self._apply_theme()
        # 先塞静态选项，后台再刷运行中账号（不堵 UI）
        self._apply_source_list([])
        QTimer.singleShot(0, lambda: self._refresh_sources(scan_extra=False))

    @classmethod
    def open(cls, *, facade=None, parent=None) -> "NetworkInspectorWindow":
        win = cls._instance
        if win is None:
            win = cls(facade=facade, parent=parent)
            cls._instance = win
        else:
            if facade is not None:
                win.facade = facade
            # 复用窗口，不每次全量扫端口；仅轻量刷新账号列表
            if not (win._worker and win._worker.isRunning()):
                win._refresh_sources(scan_extra=False)
            # 主窗换主题后再次打开时同步样式
            if parent is not None:
                parent_qss = (parent.styleSheet() or "").strip()
                if parent_qss:
                    win.setStyleSheet(parent_qss)
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def _apply_theme(self) -> None:
        try:
            from gui.styles.theme import current_theme_from_config, load_theme_qss

            self.setStyleSheet(load_theme_qss(current_theme_from_config()))
        except Exception:
            pass

    @staticmethod
    def _entry_name(url: str) -> str:
        """类似 Chrome Network 的 Name：路径最后一段。"""
        try:
            from urllib.parse import unquote, urlparse

            p = urlparse(url or "")
            path = unquote(p.path or "").rstrip("/")
            name = path.rsplit("/", 1)[-1] if path else ""
            if not name:
                name = p.netloc or (url or "")[:80]
            if p.query:
                q = p.query if len(p.query) <= 48 else p.query[:48] + "…"
                name = f"{name}?{q}"
            return name
        except Exception:
            return (url or "")[:80]

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("来源"))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(280)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        bar.addWidget(self.source_combo, 2)

        self.refresh_src_btn = QPushButton("刷新")
        self.refresh_src_btn.setToolTip("重新扫描运行中的账号浏览器 / CDP 端口")
        self.refresh_src_btn.clicked.connect(
            lambda: self._refresh_sources(scan_extra=True)
        )
        bar.addWidget(self.refresh_src_btn)

        bar.addWidget(QLabel("端口"))
        self.port_edit = QLineEdit(str(DEFAULT_PORT))
        self.port_edit.setFixedWidth(72)
        self.port_edit.setPlaceholderText("端口")
        bar.addWidget(self.port_edit)

        self.connect_btn = QPushButton("连接")
        self.connect_btn.clicked.connect(self._on_connect)
        bar.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("断开")
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        bar.addWidget(self.disconnect_btn)
        layout.addLayout(bar)

        launch_bar = QHBoxLayout()
        launch_bar.addWidget(QLabel("浏览器"))
        self.browser_edit = QLineEdit(_default_browser_path())
        launch_bar.addWidget(self.browser_edit, 2)
        self.launch_hint = QLabel("启动新浏览器时使用上方路径与端口")
        self.launch_hint.setStyleSheet("color:#666;")
        launch_bar.addWidget(self.launch_hint)
        layout.addLayout(launch_bar)

        nav = QHBoxLayout()
        nav.addWidget(QLabel("URL"))
        self.url_edit = QLineEdit("https://play.games.dmm.co.jp/")
        self.url_edit.returnPressed.connect(self._on_goto)
        nav.addWidget(self.url_edit, 3)
        self.goto_btn = QPushButton("跳转")
        self.goto_btn.setEnabled(False)
        self.goto_btn.clicked.connect(self._on_goto)
        nav.addWidget(self.goto_btn)
        layout.addLayout(nav)

        tools = QHBoxLayout()
        tools.addWidget(QLabel("过滤"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("URL / 方法 / 类型 / 状态…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        tools.addWidget(self.filter_edit, 2)

        tools.addWidget(QLabel("类型"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部", "")
        for t in (
            "document",
            "xhr",
            "fetch",
            "script",
            "stylesheet",
            "image",
            "font",
            "websocket",
            "other",
        ):
            self.type_combo.addItem(t, t)
        self.type_combo.currentIndexChanged.connect(self._apply_filter)
        tools.addWidget(self.type_combo)

        self.hide_static_cb = QCheckBox("隐藏静态")
        self.hide_static_cb.setChecked(True)
        self.hide_static_cb.setToolTip(
            "隐藏 image / font / stylesheet / media，方便看加载相关 XHR/脚本"
        )
        self.hide_static_cb.toggled.connect(self._apply_filter)
        tools.addWidget(self.hide_static_cb)

        self.hide_ads_cb = QCheckBox("隐藏广告域")
        self.hide_ads_cb.setChecked(True)
        self.hide_ads_cb.setToolTip("隐藏追踪/广告域名（与自动化拦截名单一致）")
        self.hide_ads_cb.toggled.connect(self._apply_filter)
        tools.addWidget(self.hide_ads_cb)

        self.hide_shadow_cb = QCheckBox("合并影子行")
        self.hide_shadow_cb.setChecked(True)
        self.hide_shadow_cb.setToolTip(
            "隐藏同 URL 的 other/空耗时影子行（已有 xhr 主记录时）"
        )
        self.hide_shadow_cb.toggled.connect(self._apply_filter)
        tools.addWidget(self.hide_shadow_cb)

        self.only_fail_cb = QCheckBox("仅失败")
        self.only_fail_cb.setToolTip("只显示失败或 HTTP≥400")
        self.only_fail_cb.toggled.connect(self._apply_filter)
        tools.addWidget(self.only_fail_cb)

        self.only_slow_cb = QCheckBox("仅慢请求")
        self.only_slow_cb.setToolTip(f"耗时 ≥ {_SLOW_MS_DEFAULT} ms")
        self.only_slow_cb.toggled.connect(self._apply_filter)
        tools.addWidget(self.only_slow_cb)

        self.only_game_cb = QCheckBox("仅游戏域")
        self.only_game_cb.setToolTip("只显示 deepone-online.com")
        self.only_game_cb.toggled.connect(self._apply_filter)
        tools.addWidget(self.only_game_cb)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self._clear)
        tools.addWidget(self.clear_btn)
        layout.addLayout(tools)

        tools2 = QHBoxLayout()
        self.select_all_btn = QPushButton("全选可见")
        self.select_all_btn.setToolTip("选中当前过滤后的全部行（Ctrl+A）")
        self.select_all_btn.clicked.connect(self._select_all_visible)
        tools2.addWidget(self.select_all_btn)

        self.export_sel_btn = QPushButton("导出选中")
        self.export_sel_btn.setToolTip("导出勾选的多条请求为 JSON 数组（含标头/body）")
        self.export_sel_btn.clicked.connect(self._export_selected)
        tools2.addWidget(self.export_sel_btn)

        self.export_visible_btn = QPushButton("导出可见")
        self.export_visible_btn.setToolTip("导出当前过滤可见的全部请求（摘要，体积更小）")
        self.export_visible_btn.clicked.connect(self._export_visible)
        tools2.addWidget(self.export_visible_btn)

        self.export_agg_btn = QPushButton("按路径汇总")
        self.export_agg_btn.setToolTip(
            "按 URL path 聚合可见请求：次数 / abort / 最大耗时 / 最大体积"
        )
        self.export_agg_btn.clicked.connect(self._export_path_aggregate)
        tools2.addWidget(self.export_agg_btn)

        self.export_page_btn = QPushButton("导出概览")
        self.export_page_btn.setToolTip("导出下方当前标签页内容（针对当前焦点行）")
        self.export_page_btn.clicked.connect(self._export_current_page)
        tools2.addWidget(self.export_page_btn)

        self.count_label = QLabel("0 条")
        tools2.addWidget(self.count_label)
        tools2.addStretch(1)
        layout.addLayout(tools2)

        splitter = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_menu)
        self.table.doubleClicked.connect(self._focus_export_one)
        self.table.verticalHeader().setVisible(False)
        # Ctrl+A 全选可见行
        sc_all = QShortcut(QKeySequence.StandardKey.SelectAll, self.table)
        sc_all.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_all.activated.connect(self._select_all_visible)
        self.table.setStyleSheet(
            """
            QTableWidget {
                gridline-color: #d8d8d8;
                outline: none;
                color: #111111;
                background: #ffffff;
                alternate-background-color: #f7f9fb;
            }
            QHeaderView::section {
                background-color: #eceff3;
                color: #111111;
                padding: 4px 8px;
                border: none;
                border-right: 1px solid #d0d0d0;
                border-bottom: 1px solid #c8c8c8;
                font-weight: 600;
            }
            QTableWidget::item {
                padding: 2px 6px;
                color: #111111;
            }
            QTableWidget::item:selected {
                background-color: #b8dbf7;
                color: #111111;
            }
            QTableWidget::item:selected:!active {
                background-color: #d0e8f8;
                color: #111111;
            }
            """
        )
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)  # 名称优先可见
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.Interactive)
        self.table.setColumnWidth(7, 260)
        self.table.itemSelectionChanged.connect(self._on_select)
        splitter.addWidget(self.table)

        self.detail_tabs = QTabWidget()
        mono = QFont("Consolas", 10)

        def _make_page(placeholder: str) -> QTextEdit:
            edit = QTextEdit()
            edit.setReadOnly(True)
            edit.setFont(mono)
            edit.setPlaceholderText(placeholder)
            return edit

        self.tab_overview = _make_page("选中一行查看概览")
        self.tab_headers = _make_page("选中一行查看标头")
        self.tab_request = _make_page("无请求体")
        self.tab_preview = _make_page("无预览（非 JSON 或未抓到 body）")
        self.tab_response = _make_page("无响应体")

        self.detail_tabs.addTab(self.tab_overview, "概览")
        self.detail_tabs.addTab(self.tab_headers, "标头")
        self.detail_tabs.addTab(self.tab_request, "请求")
        self.detail_tabs.addTab(self.tab_preview, "预览")
        self.detail_tabs.addTab(self.tab_response, "响应")
        self.detail_tabs.currentChanged.connect(self._sync_export_page_btn)
        splitter.addWidget(self.detail_tabs)
        splitter.setSizes([480, 260])
        layout.addWidget(splitter, 1)

        self.status = QLabel("未连接")
        layout.addWidget(self.status)
        self._on_source_changed()
        self._sync_export_page_btn()

    def _apply_source_list(self, endpoints: list) -> None:
        cur = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for ep in endpoints or []:
            self.source_combo.addItem(
                ep["label"], {"mode": "attach", "port": ep["port"]}
            )
        self.source_combo.addItem("手动填写端口…", {"mode": "manual"})
        self.source_combo.addItem("启动新浏览器…", {"mode": "launch"})
        restored = False
        if isinstance(cur, dict):
            for i in range(self.source_combo.count()):
                d = self.source_combo.itemData(i)
                if not isinstance(d, dict):
                    continue
                if d.get("mode") == cur.get("mode") and d.get("port") == cur.get("port"):
                    self.source_combo.setCurrentIndex(i)
                    restored = True
                    break
                if d.get("mode") == cur.get("mode") and cur.get("mode") in (
                    "manual",
                    "launch",
                ):
                    self.source_combo.setCurrentIndex(i)
                    restored = True
                    break
        if not restored and endpoints:
            self.source_combo.setCurrentIndex(0)
        self.source_combo.blockSignals(False)
        self._on_source_changed()

    def _refresh_sources(self, *, scan_extra: bool = False) -> None:
        if self._scan_worker and self._scan_worker.isRunning():
            return
        self.refresh_src_btn.setEnabled(False)
        self.status.setText("正在扫描来源…")
        worker = _SourceScanWorker(
            self.facade, scan_extra=scan_extra, parent=self
        )
        self._scan_worker = worker

        def _ok(endpoints):
            self._apply_source_list(endpoints or [])
            self.refresh_src_btn.setEnabled(True)
            if not (self._worker and self._worker.isRunning()):
                self.status.setText(f"来源已更新（{len(endpoints or [])} 个运行中）")

        def _fail(msg: str):
            self.refresh_src_btn.setEnabled(True)
            self._apply_source_list([])
            self.status.setText(f"扫描失败: {msg}")

        worker.finished_ok.connect(_ok)
        worker.failed.connect(_fail)
        worker.start()

    @Slot()
    def _on_source_changed(self) -> None:
        data = self.source_combo.currentData()
        if not isinstance(data, dict):
            data = {"mode": "manual"}
        mode = data.get("mode") or "manual"
        if mode == "attach":
            self.port_edit.setEnabled(False)
            if data.get("port"):
                self.port_edit.setText(str(int(data["port"])))
            self.browser_edit.setEnabled(False)
            self.launch_hint.setText("将接入已运行的 CDP（账号浏览器或扫描到的端口）")
        elif mode == "launch":
            self.port_edit.setEnabled(True)
            self.browser_edit.setEnabled(True)
            self.launch_hint.setText("将启动独立浏览器；端口若被占用会自动顺延")
        else:
            self.port_edit.setEnabled(True)
            self.browser_edit.setEnabled(False)
            self.launch_hint.setText("手动指定 CDP 端口后连接")

    def _read_port(self) -> int:
        text = self.port_edit.text().strip()
        try:
            port = int(text)
        except ValueError as e:
            raise ValueError(f"端口无效: {text!r}") from e
        if not (1024 <= port <= 65535):
            raise ValueError(f"端口需在 1024–65535: {port}")
        return port

    @Slot()
    def _sync_export_page_btn(self) -> None:
        idx = self.detail_tabs.currentIndex()
        name = self.detail_tabs.tabText(idx) if idx >= 0 else "当前页"
        self.export_page_btn.setText(f"导出{name}")

    @Slot()
    def _on_connect(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        data = self.source_combo.currentData()
        if not isinstance(data, dict):
            data = {"mode": "manual"}
        mode = data.get("mode") or "manual"
        launch = mode == "launch"
        try:
            if mode == "attach" and data.get("port"):
                port = int(data["port"])
            else:
                port = self._read_port()
        except ValueError as e:
            QMessageBox.warning(self, "Network Inspector", str(e))
            return
        if launch:
            try:
                from backend.browser.utils import get_port

                port = get_port(port)
                self.port_edit.setText(str(port))
            except Exception:
                pass
        ud = str(PROJECT_ROOT / "browser_data_demo" / "_network_inspector")
        self._worker = NetworkWorker(
            port=port,
            start_url=self.url_edit.text().strip(),
            browser_path=self.browser_edit.text().strip() or _default_browser_path(),
            launch=launch,
            user_data_dir=ud,
        )
        self._worker.connected.connect(self._on_connected)
        self._worker.failed.connect(self._on_failed)
        self._worker.entry_updated.connect(self._on_entry)
        self._worker.navigated.connect(self._on_navigated)
        self._worker.closed.connect(self._on_closed)
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self.goto_btn.setEnabled(True)
        self.source_combo.setEnabled(False)
        self.refresh_src_btn.setEnabled(False)
        self.status.setText(
            f"连接中… ({'启动' if launch else '接入'} port={port})"
        )
        self._worker.start()

    @Slot()
    def _on_disconnect(self) -> None:
        if self._worker:
            self._worker.stop()
            self.status.setText("正在断开…")

    @Slot()
    def _on_goto(self) -> None:
        url = self.url_edit.text().strip()
        if not url or not self._worker:
            return
        self._worker.request_goto(url)
        self.status.setText(f"跳转: {url}")

    @Slot(str)
    def _on_connected(self, url: str) -> None:
        self.status.setText(f"已连接  {url}")
        if url and url.startswith(("http://", "https://")):
            self.url_edit.setText(url)

    @Slot(str)
    def _on_navigated(self, url: str) -> None:
        if url and not url.startswith(("devtools://", "chrome://", "edge://")):
            self.url_edit.setText(url)
        self.status.setText(f"已导航  {url}")

    @Slot(str)
    def _on_failed(self, msg: str) -> None:
        self.status.setText(f"错误: {msg}")
        if "仍可继续监听" in msg:
            return
        QMessageBox.warning(self, "Network Inspector", msg)

    @Slot()
    def _on_closed(self) -> None:
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.goto_btn.setEnabled(False)
        self.source_combo.setEnabled(True)
        self.refresh_src_btn.setEnabled(True)
        self._on_source_changed()
        self.status.setText("已断开（浏览器进程可能仍在运行）")
        self._worker = None
        # 不断开后全量扫端口；仅轻量刷账号列表
        self._refresh_sources(scan_extra=False)

    @Slot(object)
    def _on_entry(self, entry: NetEntry) -> None:
        self._entries[entry.id] = entry
        row = self._row_by_id.get(entry.id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_by_id[entry.id] = row
            for c in range(len(self.COLS)):
                self.table.setItem(row, c, QTableWidgetItem(""))

        def set_col(c: int, text: str, bg: QColor | None = None) -> None:
            item = self.table.item(row, c)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, c, item)
            item.setText(text)
            item.setForeground(QColor("#111111"))
            if bg is not None:
                item.setBackground(bg)
            else:
                item.setBackground(QColor(0, 0, 0, 0))

        status_txt = ""
        bg = None
        if entry.failure:
            status_txt = "ERR"
            bg = QColor("#f8d0d4")
        elif entry.status is not None:
            status_txt = str(entry.status)
            if entry.status >= 400:
                bg = QColor("#f8d0d4")
            elif entry.status >= 300:
                bg = QColor("#ffe6bf")
            else:
                bg = QColor("#d4efdf")

        size_txt = ""
        if entry.size is not None:
            if entry.size >= 1024 * 1024:
                size_txt = f"{entry.size / 1024 / 1024:.1f}MB"
            elif entry.size >= 1024:
                size_txt = f"{entry.size / 1024:.1f}KB"
            else:
                size_txt = f"{entry.size}B"

        name = self._entry_name(entry.url)
        set_col(0, str(entry.id))
        set_col(1, name)
        set_col(2, entry.method)
        set_col(3, status_txt, bg)
        set_col(4, entry.resource_type)
        set_col(5, "" if entry.duration_ms is None else str(entry.duration_ms))
        set_col(6, size_txt)
        set_col(7, entry.url)
        self.table.item(row, 0).setData(Qt.UserRole, entry.id)
        name_item = self.table.item(row, 1)
        if name_item is not None:
            name_item.setToolTip(entry.url)
        url_item = self.table.item(row, 7)
        if url_item is not None:
            url_item.setToolTip(entry.url)
        self._apply_filter()
        # 选中行后续更新（如抓到 body）时刷新下方分页
        cur = self._selected_entry()
        if cur is not None and cur.id == entry.id:
            self._on_select()

    def _entry_host(self, e: NetEntry) -> str:
        try:
            from urllib.parse import urlparse

            return (urlparse(e.url or "").hostname or "").lower()
        except Exception:
            return ""

    def _shadow_entry_ids(self) -> set[int]:
        """同 URL 下已有主类型记录时，把 other/空壳行标为影子。"""
        by_key: dict[str, list[NetEntry]] = {}
        for e in self._entries.values():
            by_key.setdefault(_url_path_key(e.url), []).append(e)
        shadows: set[int] = set()
        for items in by_key.values():
            if len(items) < 2:
                continue
            primaries = [
                x
                for x in items
                if x.resource_type in _PRIMARY_TYPES
                and (
                    x.status is not None
                    or x.duration_ms is not None
                    or x.failure
                    or x.response_body
                )
            ]
            if not primaries:
                # 没有明确主记录：保留耗时最长/有 status 的一条，其余 other 当影子
                ranked = sorted(
                    items,
                    key=lambda x: (
                        x.resource_type in _PRIMARY_TYPES,
                        x.status is not None,
                        x.duration_ms is not None,
                        x.duration_ms or 0,
                        x.size or 0,
                    ),
                    reverse=True,
                )
                for x in ranked[1:]:
                    if x.resource_type in _SHADOW_TYPES or (
                        x.duration_ms is None and not x.failure and x.status is None
                    ):
                        shadows.add(x.id)
                continue
            primary_ids = {p.id for p in primaries}
            for x in items:
                if x.id in primary_ids:
                    continue
                if x.resource_type in _SHADOW_TYPES:
                    shadows.add(x.id)
                elif x.duration_ms is None and not x.failure and x.status in (None, 204):
                    shadows.add(x.id)
        return shadows

    def _visible_ids(self) -> list[int]:
        q = self.filter_edit.text().strip().lower()
        typ = self.type_combo.currentData() or ""
        hide_static = self.hide_static_cb.isChecked()
        hide_ads = self.hide_ads_cb.isChecked()
        hide_shadow = self.hide_shadow_cb.isChecked()
        only_fail = self.only_fail_cb.isChecked()
        only_slow = self.only_slow_cb.isChecked()
        only_game = self.only_game_cb.isChecked()
        shadows = self._shadow_entry_ids() if hide_shadow else set()
        out = []
        for eid, e in self._entries.items():
            if typ and e.resource_type != typ:
                continue
            if hide_static and e.resource_type in _STATIC_TYPES:
                continue
            if hide_shadow and eid in shadows:
                continue
            host = self._entry_host(e)
            if hide_ads and is_ad_or_tracker_host(host):
                continue
            if only_game and "deepone-online.com" not in host:
                continue
            if only_fail:
                failed = bool(e.failure) or (
                    e.status is not None and int(e.status) >= 400
                )
                if not failed:
                    continue
            if only_slow:
                if e.duration_ms is None or e.duration_ms < _SLOW_MS_DEFAULT:
                    continue
            if q:
                blob = " ".join(
                    [
                        e.method,
                        str(e.status or ""),
                        e.resource_type,
                        e.url,
                        self._entry_name(e.url),
                        e.failure,
                        e.mime,
                    ]
                ).lower()
                if q not in blob:
                    continue
            out.append(eid)
        return out

    @Slot()
    def _apply_filter(self) -> None:
        visible = set(self._visible_ids())
        shown = 0
        for eid, row in self._row_by_id.items():
            hide = eid not in visible
            self.table.setRowHidden(row, hide)
            if not hide:
                shown += 1
        sel_n = len(self._selected_entries())
        extra = f" · 已选 {sel_n}" if sel_n else ""
        self.count_label.setText(f"{shown} / {len(self._entries)} 条{extra}")

    def _selected_entry(self) -> Optional[NetEntry]:
        entries = self._selected_entries()
        return entries[0] if entries else None

    def _selected_entries(self) -> list[NetEntry]:
        rows = self.table.selectionModel().selectedRows()
        out: list[NetEntry] = []
        seen: set[int] = set()
        for idx in rows:
            item = self.table.item(idx.row(), 0)
            if not item:
                continue
            eid = item.data(Qt.UserRole)
            if eid in seen:
                continue
            e = self._entries.get(eid)
            if e is not None:
                seen.add(eid)
                out.append(e)
        return out

    @Slot()
    def _select_all_visible(self) -> None:
        from PySide6.QtCore import QItemSelectionModel

        sm = self.table.selectionModel()
        sm.clearSelection()
        flags = (
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows
        )
        model = self.table.model()
        for eid in self._visible_ids():
            row = self._row_by_id.get(eid)
            if row is None:
                continue
            sm.select(model.index(row, 0), flags)
        self._apply_filter()
        n = len(self._selected_entries())
        self.status.setText(f"已全选可见 {n} 条")

    def _default_export_dir(self) -> Path:
        settings = QSettings("Minashigo", "NetworkInspector")
        last = str(settings.value("last_export_dir", "") or "").strip()
        if last:
            p = Path(last)
            # 旧默认在 screenshots/network，自动迁到 diagnostics/network
            try:
                if "screenshots" in p.parts and p.name == "network":
                    p = PROJECT_ROOT / "diagnostics" / "network"
                    settings.setValue("last_export_dir", str(p))
            except Exception:
                pass
            if p.is_dir():
                return p
        d = PROJECT_ROOT / "diagnostics" / "network"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _remember_export_path(self, path: str) -> None:
        try:
            parent = str(Path(path).resolve().parent)
            QSettings("Minashigo", "NetworkInspector").setValue(
                "last_export_dir", parent
            )
        except Exception:
            pass

    def _suggest_name(self, e: NetEntry) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path_part = ""
        try:
            from urllib.parse import urlparse

            path_part = urlparse(e.url).path.strip("/").replace("/", "_")
            path_part = "".join(
                c if c.isalnum() or c in "-_." else "_" for c in path_part
            )
            path_part = path_part[:60]
        except Exception:
            path_part = ""
        mid = f"_{path_part}" if path_part else ""
        return f"req_{e.id}{mid}_{ts}.json"

    def _write_json(self, path: str, payload: Any) -> None:
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _entry_summary(e: NetEntry) -> dict:
        return {
            "id": e.id,
            "method": e.method,
            "status": e.status,
            "resource_type": e.resource_type,
            "url": e.url,
            "mime": e.mime,
            "size": e.size,
            "duration_ms": e.duration_ms,
            "failure": e.failure or None,
        }

    @staticmethod
    def _format_headers(title: str, headers: dict) -> str:
        lines = [title]
        if not headers:
            lines.append("  (空)")
            return "\n".join(lines)
        for k in sorted(headers.keys(), key=lambda x: str(x).lower()):
            lines.append(f"  {k}: {headers[k]}")
        return "\n".join(lines)

    def _clear_detail_tabs(self) -> None:
        self.tab_overview.clear()
        self.tab_headers.clear()
        self.tab_request.clear()
        self.tab_preview.clear()
        self.tab_response.clear()

    @Slot()
    def _on_select(self) -> None:
        shown = sum(
            1
            for eid, row in self._row_by_id.items()
            if not self.table.isRowHidden(row)
        )
        sel_n = len(self._selected_entries())
        extra = f" · 已选 {sel_n}" if sel_n else ""
        self.count_label.setText(f"{shown} / {len(self._entries)} 条{extra}")

        e = self._selected_entry()
        if not e:
            return

        overview = [
            f"#{e.id}  {e.method}  {e.status or '-'}  {e.resource_type}",
            f"名称: {self._entry_name(e.url)}",
            f"URL: {e.url}",
            f"MIME: {e.mime or '-'}",
            f"大小: {e.size if e.size is not None else '-'}",
            f"耗时: {e.duration_ms if e.duration_ms is not None else '-'} ms",
            f"失败: {e.failure or '-'}",
        ]
        self.tab_overview.setPlainText("\n".join(overview))

        headers_txt = "\n\n".join(
            [
                f"General\n  Request URL: {e.url}\n  Request Method: {e.method}\n"
                f"  Status Code: {e.status if e.status is not None else '-'}",
                self._format_headers("Request Headers", e.request_headers),
                self._format_headers("Response Headers", e.response_headers),
            ]
        )
        self.tab_headers.setPlainText(headers_txt)

        if e.post_data:
            self.tab_request.setPlainText(e.post_data)
        else:
            self.tab_request.setPlainText("(无请求体)")

        if e.response_json is not None:
            self.tab_preview.setPlainText(
                json.dumps(e.response_json, ensure_ascii=False, indent=2)
            )
        elif e.response_body and e.response_body_base64:
            n = len(e.response_body)
            head = e.response_body[:120]
            self.tab_preview.setPlainText(
                f"(二进制 base64，长度 {n} 字符)\n"
                f"导出 JSON 字段 response_body + response_body_base64=true\n\n"
                f"{head}{'…' if n > 120 else ''}"
            )
        elif e.response_body:
            body = e.response_body
            # 尝试当 JSON 预览
            try:
                parsed = json.loads(body)
                self.tab_preview.setPlainText(
                    json.dumps(parsed, ensure_ascii=False, indent=2)
                )
            except Exception:
                preview = body if len(body) <= 20000 else body[:20000] + "\n…(截断)"
                self.tab_preview.setPlainText(preview)
        else:
            self.tab_preview.setPlainText("(无预览内容)")

        if e.response_body:
            raw = e.response_body
            if e.response_body_base64:
                prefix = f"[base64 binary, chars={len(raw)}]\n"
                shown = raw if len(raw) <= 200000 else raw[:200000] + "\n…(截断)"
                self.tab_response.setPlainText(prefix + shown)
            else:
                if len(raw) > 200000:
                    raw = raw[:200000] + "\n…(截断)"
                self.tab_response.setPlainText(raw)
        else:
            self.tab_response.setPlainText("(无响应体 / 未捕获)")

    @Slot()
    def _on_table_menu(self, pos) -> None:
        selected = self._selected_entries()
        menu = QMenu(self)
        act_all = menu.addAction("全选可见")
        act_full = menu.addAction(f"导出选中（{len(selected)}）…")
        act_vis = menu.addAction("导出可见…")
        page = self.detail_tabs.tabText(self.detail_tabs.currentIndex())
        act_page = menu.addAction(f"导出当前行「{page}」…")
        act_full.setEnabled(bool(selected))
        act_page.setEnabled(bool(selected))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == act_all:
            self._select_all_visible()
        elif chosen == act_full:
            self._export_selected()
        elif chosen == act_vis:
            self._export_visible()
        elif chosen == act_page:
            self._export_current_page()

    @Slot()
    def _clear(self) -> None:
        self._entries.clear()
        self._row_by_id.clear()
        self.table.setRowCount(0)
        self._clear_detail_tabs()
        self.count_label.setText("0 条")

    def _suggest_part_name(self, e: NetEntry, kind: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = self._entry_name(e.url)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in base)[:50]
        return f"{kind}_{e.id}_{safe}_{ts}.json"

    def _page_payload(self, e: NetEntry, tab_name: str) -> tuple[Any, str]:
        """返回 (payload, 文件名前缀)。"""
        if tab_name == "概览":
            return (
                {
                    "id": e.id,
                    "method": e.method,
                    "status": e.status,
                    "resource_type": e.resource_type,
                    "name": self._entry_name(e.url),
                    "url": e.url,
                    "mime": e.mime,
                    "size": e.size,
                    "duration_ms": e.duration_ms,
                    "failure": e.failure or None,
                },
                "overview",
            )
        if tab_name == "标头":
            return (
                {
                    "url": e.url,
                    "method": e.method,
                    "status": e.status,
                    "resource_type": e.resource_type,
                    "mime": e.mime,
                    "request_headers": e.request_headers,
                    "response_headers": e.response_headers,
                },
                "headers",
            )
        if tab_name == "请求":
            body: Any
            if e.post_data:
                try:
                    body = json.loads(e.post_data)
                except Exception:
                    body = e.post_data
            else:
                body = None
            return (
                {
                    "url": e.url,
                    "method": e.method,
                    "post_data": body,
                },
                "request",
            )
        if tab_name == "预览":
            if e.response_json is not None:
                return e.response_json, "preview"
            if e.response_body and e.response_body_base64:
                return {
                    "encoding": "base64",
                    "mime": e.mime,
                    "base64": e.response_body,
                }, "preview"
            if e.response_body:
                try:
                    return json.loads(e.response_body), "preview"
                except Exception:
                    return {"raw": e.response_body}, "preview"
            return {"raw": None}, "preview"
        # 响应
        if e.response_json is not None:
            return e.response_json, "response"
        if e.response_body and e.response_body_base64:
            return {
                "url": e.url,
                "mime": e.mime,
                "encoding": "base64",
                "base64": e.response_body,
            }, "response"
        if e.response_body:
            try:
                return json.loads(e.response_body), "response"
            except Exception:
                return {
                    "url": e.url,
                    "mime": e.mime,
                    "raw": e.response_body,
                }, "response"
        return {"raw": None}, "response"

    @Slot()
    def _focus_export_one(self) -> None:
        """双击：仅保留当前行选中，并弹出导出该条。"""
        e = self._selected_entry()
        if not e:
            return
        self.table.clearSelection()
        row = self._row_by_id.get(e.id)
        if row is not None:
            self.table.selectRow(row)
        self._export_selected()

    @Slot()
    def _export_selected(self) -> None:
        entries = self._selected_entries()
        if not entries:
            QMessageBox.information(self, "导出", "请先选中请求（可多选 / Ctrl+A 全选可见）")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if len(entries) == 1:
            default = self._suggest_name(entries[0])
        else:
            default = f"reqs_{len(entries)}_{ts}.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"导出选中（{len(entries)} 条）",
            str(self._default_export_dir() / default),
            "JSON (*.json)",
        )
        if not path:
            return
        payload = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(entries),
            "entries": [entry_to_dict(e) for e in entries],
        }
        # 单条也包在 entries 里，方便脚本统一解析；保留 entry 兼容旧格式
        if len(entries) == 1:
            payload["entry"] = payload["entries"][0]
        self._write_json(path, payload)
        self._remember_export_path(path)
        ids = ",".join(f"#{e.id}" for e in entries[:8])
        more = "…" if len(entries) > 8 else ""
        self.status.setText(f"已导出 {len(entries)} 条 ({ids}{more}) → {path}")

    @Slot()
    def _export_visible(self) -> None:
        ids = self._visible_ids()
        entries = [self._entries[i] for i in ids if i in self._entries]
        if not entries:
            QMessageBox.information(self, "导出", "当前过滤下没有可见请求")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"导出可见（{len(entries)} 条摘要）",
            str(self._default_export_dir() / f"visible_{len(entries)}_{ts}.json"),
            "JSON (*.json)",
        )
        if not path:
            return
        payload = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(entries),
            "filters": {
                "text": self.filter_edit.text().strip(),
                "type": self.type_combo.currentData() or "",
                "hide_static": self.hide_static_cb.isChecked(),
                "hide_ads": self.hide_ads_cb.isChecked(),
                "hide_shadow": self.hide_shadow_cb.isChecked(),
                "only_fail": self.only_fail_cb.isChecked(),
                "only_slow": self.only_slow_cb.isChecked(),
                "only_game": self.only_game_cb.isChecked(),
                "slow_ms": _SLOW_MS_DEFAULT,
            },
            "entries": [self._entry_summary(e) for e in entries],
        }
        self._write_json(path, payload)
        self._remember_export_path(path)
        self.status.setText(f"已导出可见摘要 {len(entries)} 条 → {path}")

    @Slot()
    def _export_path_aggregate(self) -> None:
        ids = self._visible_ids()
        entries = [self._entries[i] for i in ids if i in self._entries]
        if not entries:
            QMessageBox.information(self, "汇总", "当前过滤下没有可见请求")
            return
        from collections import defaultdict
        from urllib.parse import urlparse

        buckets: dict[str, list[NetEntry]] = defaultdict(list)
        for e in entries:
            try:
                path = urlparse(e.url or "").path or e.url
            except Exception:
                path = e.url
            buckets[path].append(e)

        rows = []
        for path, items in buckets.items():
            durs = [x.duration_ms for x in items if x.duration_ms is not None]
            sizes = [
                x.size for x in items if isinstance(x.size, (int, float)) and x.size
            ]
            aborts = sum(1 for x in items if "ABORTED" in (x.failure or ""))
            fails = sum(
                1
                for x in items
                if x.failure
                or (isinstance(x.status, int) and x.status >= 400)
            )
            ok200 = sum(1 for x in items if x.status == 200)
            rows.append(
                {
                    "path": path,
                    "count": len(items),
                    "ok_200": ok200,
                    "aborts": aborts,
                    "fails": fails,
                    "max_ms": max(durs) if durs else None,
                    "sum_ms": round(sum(durs), 1) if durs else None,
                    "max_size": max(sizes) if sizes else None,
                    "types": sorted({x.resource_type for x in items if x.resource_type}),
                }
            )
        rows.sort(key=lambda r: (r.get("max_ms") or 0), reverse=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"按路径汇总（{len(rows)}）",
            str(self._default_export_dir() / f"agg_path_{len(rows)}_{ts}.json"),
            "JSON (*.json)",
        )
        if not path:
            return
        payload = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "path_count": len(rows),
            "entry_count": len(entries),
            "paths": rows,
        }
        self._write_json(path, payload)
        self._remember_export_path(path)
        # 状态栏给一眼摘要
        top = rows[0] if rows else None
        tip = ""
        if top:
            tip = f" · 最慢 {top.get('max_ms')}ms {top.get('path', '')[-48:]}"
        self.status.setText(f"已导出路径汇总 {len(rows)} 条{tip} → {path}")

    @Slot()
    def _export_current_page(self) -> None:
        e = self._selected_entry()
        if not e:
            QMessageBox.information(self, "导出", "请先选中一条请求")
            return
        tab_name = self.detail_tabs.tabText(self.detail_tabs.currentIndex())
        payload, kind = self._page_payload(e, tab_name)

        if tab_name == "请求" and isinstance(payload, dict) and payload.get("post_data") is None:
            QMessageBox.information(self, "导出", "该请求没有请求体")
            return
        if tab_name in ("预览", "响应"):
            empty = payload == {"raw": None} or (
                isinstance(payload, dict)
                and payload.get("raw") is None
                and list(payload.keys()) == ["raw"]
            )
            if empty:
                QMessageBox.information(self, "导出", f"当前「{tab_name}」没有可导出的内容")
                return

        path, _ = QFileDialog.getSaveFileName(
            self,
            f"导出{tab_name}",
            str(self._default_export_dir() / self._suggest_part_name(e, kind)),
            "JSON (*.json)",
        )
        if not path:
            return

        # 预览/响应：业务 JSON 直接落盘；其它页带 page 包装
        if tab_name in ("预览", "响应") and isinstance(payload, (dict, list)):
            if isinstance(payload, dict) and set(payload.keys()) <= {"url", "mime", "raw"}:
                out: Any = {
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "page": tab_name,
                    "data": payload,
                }
            else:
                out = payload
        else:
            out = {
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "page": tab_name,
                "data": payload,
            }
        self._write_json(path, out)
        self._remember_export_path(path)
        self.status.setText(f"已导出{tab_name} #{e.id} → {path}")

    def closeEvent(self, event) -> None:
        # 点关闭只隐藏，保留单例与已建立的监听，避免反复重建卡顿
        if getattr(self, "_force_closing", False):
            if self._worker and self._worker.isRunning():
                self._worker.stop()
                self._worker.wait(3000)
            if NetworkInspectorWindow._instance is self:
                NetworkInspectorWindow._instance = None
            super().closeEvent(event)
            return
        self.hide()
        event.ignore()

    def force_close(self) -> None:
        """主程序退出时真正关闭。"""
        self._force_closing = True
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.wait(1500)
        if NetworkInspectorWindow._instance is self:
            NetworkInspectorWindow._instance = None
        self.close()


def main(facade=None) -> None:
    app = QApplication.instance()
    owned = False
    if app is None:
        app = QApplication(sys.argv)
        owned = True
    NetworkInspectorWindow.open(facade=facade)
    if owned:
        sys.exit(app.exec())


if __name__ == "__main__":
    main()
