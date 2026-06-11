"""
Win32Target —— 控制已启动的任意 Windows 窗口
==============================================

核心能力（均不抢前台）：
  - PrintWindow 后台截图 → np.ndarray（兼容现有 Matcher/OCR）
  - PostMessage 后台鼠标键盘（WM_ 消息级别，不激活窗口）

坐标系统：客户端坐标（client coordinates）—— 截图矩阵的像素 (x, y) 可直接用于 click。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional, ClassVar
import numpy as np
import cv2


# ========================================================================
# Win32 类型定义
# ========================================================================

LONG = wintypes.LONG
DWORD = wintypes.DWORD
WORD = wintypes.WORD
BOOL = wintypes.BOOL
UINT = wintypes.UINT
HWND = wintypes.HWND
HDC = wintypes.HDC
HGDIOBJ = wintypes.HANDLE
HBITMAP = wintypes.HANDLE
WPARAM = wintypes.WPARAM
LPARAM = wintypes.LPARAM
LPCWSTR = wintypes.LPCWSTR
INT = wintypes.INT


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", LONG),
        ("top", LONG),
        ("right", LONG),
        ("bottom", LONG),
    ]


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", LONG),
        ("y", LONG),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", DWORD),
        ("biWidth", LONG),
        ("biHeight", LONG),
        ("biPlanes", WORD),
        ("biBitCount", WORD),
        ("biCompression", DWORD),
        ("biSizeImage", DWORD),
        ("biXPelsPerMeter", LONG),
        ("biYPelsPerMeter", LONG),
        ("biClrUsed", DWORD),
        ("biClrImportant", DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", DWORD * 1),  # 占位
    ]


# ========================================================================
# Win32 API 函数加载
# ========================================================================

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# --- user32 ---
# EnumWindows 回调类型：BOOL CALLBACK(HWND, LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)

_EnumWindows = _user32.EnumWindows
_EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_EnumWindows.restype = BOOL

_GetWindowText = _user32.GetWindowTextW
_GetWindowText.argtypes = [HWND, wintypes.LPWSTR, INT]
_GetWindowText.restype = INT

_GetWindowTextLength = _user32.GetWindowTextLengthW
_GetWindowTextLength.argtypes = [HWND]
_GetWindowTextLength.restype = INT

_IsWindowVisible = _user32.IsWindowVisible
_IsWindowVisible.argtypes = [HWND]
_IsWindowVisible.restype = BOOL

_GetWindowRect = _user32.GetWindowRect
_GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
_GetWindowRect.restype = BOOL

_GetClientRect = _user32.GetClientRect
_GetClientRect.argtypes = [HWND, ctypes.POINTER(RECT)]
_GetClientRect.restype = BOOL

_ClientToScreen = _user32.ClientToScreen
_ClientToScreen.argtypes = [HWND, ctypes.POINTER(POINT)]
_ClientToScreen.restype = BOOL

_ScreenToClient = _user32.ScreenToClient
_ScreenToClient.argtypes = [HWND, ctypes.POINTER(POINT)]
_ScreenToClient.restype = BOOL

_PrintWindow = _user32.PrintWindow
_PrintWindow.argtypes = [HWND, HDC, UINT]
_PrintWindow.restype = BOOL

_PostMessage = _user32.PostMessageW
_PostMessage.argtypes = [HWND, UINT, WPARAM, LPARAM]
_PostMessage.restype = BOOL

_SendMessage = _user32.SendMessageW
_SendMessage.argtypes = [HWND, UINT, WPARAM, LPARAM]
_SendMessage.restype = ctypes.c_ssize_t

_IsWindow = _user32.IsWindow
_IsWindow.argtypes = [HWND]
_IsWindow.restype = BOOL

_GetClassName = _user32.GetClassNameW
_GetClassName.argtypes = [HWND, wintypes.LPWSTR, INT]
_GetClassName.restype = INT

_GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
_GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
_GetWindowThreadProcessId.restype = DWORD

_IsIconic = _user32.IsIconic
_IsIconic.argtypes = [HWND]
_IsIconic.restype = BOOL

_IsZoomed = _user32.IsZoomed
_IsZoomed.argtypes = [HWND]
_IsZoomed.restype = BOOL

_GetDC = _user32.GetDC
_GetDC.argtypes = [HWND]
_GetDC.restype = HDC

_GetWindowDC = _user32.GetWindowDC
_GetWindowDC.argtypes = [HWND]
_GetWindowDC.restype = HDC

_ReleaseDC = _user32.ReleaseDC
_ReleaseDC.argtypes = [HWND, HDC]
_ReleaseDC.restype = INT

_GetForegroundWindow = _user32.GetForegroundWindow
_GetForegroundWindow.argtypes = []
_GetForegroundWindow.restype = HWND

_SetForegroundWindow = _user32.SetForegroundWindow
_SetForegroundWindow.argtypes = [HWND]
_SetForegroundWindow.restype = BOOL

_ShowWindow = _user32.ShowWindow
_ShowWindow.argtypes = [HWND, INT]
_ShowWindow.restype = BOOL

_SetWindowPos = _user32.SetWindowPos
_SetWindowPos.argtypes = [HWND, HWND, INT, INT, INT, INT, UINT]
_SetWindowPos.restype = BOOL

_GetCursorPos = _user32.GetCursorPos
_GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
_GetCursorPos.restype = BOOL

_WindowFromPoint = _user32.WindowFromPoint
_WindowFromPoint.argtypes = [POINT]
_WindowFromPoint.restype = HWND

_GetAncestor = _user32.GetAncestor
_GetAncestor.argtypes = [HWND, UINT]
_GetAncestor.restype = HWND

_SendMessageTimeout = _user32.SendMessageTimeoutW
_SendMessageTimeout.argtypes = [HWND, UINT, WPARAM, LPARAM, UINT, UINT, ctypes.POINTER(ctypes.c_ulonglong)]
_SendMessageTimeout.restype = ctypes.c_ssize_t

# --- gdi32 ---
_CreateCompatibleDC = _gdi32.CreateCompatibleDC
_CreateCompatibleDC.argtypes = [HDC]
_CreateCompatibleDC.restype = HDC

_CreateCompatibleBitmap = _gdi32.CreateCompatibleBitmap
_CreateCompatibleBitmap.argtypes = [HDC, INT, INT]
_CreateCompatibleBitmap.restype = HBITMAP

_SelectObject = _gdi32.SelectObject
_SelectObject.argtypes = [HDC, HGDIOBJ]
_SelectObject.restype = HGDIOBJ

_DeleteDC = _gdi32.DeleteDC
_DeleteDC.argtypes = [HDC]
_DeleteDC.restype = BOOL

_DeleteObject = _gdi32.DeleteObject
_DeleteObject.argtypes = [HGDIOBJ]
_DeleteObject.restype = BOOL

_GetDIBits = _gdi32.GetDIBits
_GetDIBits.argtypes = [HDC, HBITMAP, UINT, UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), UINT]
_GetDIBits.restype = INT

_BitBlt = _gdi32.BitBlt
_BitBlt.argtypes = [HDC, INT, INT, INT, INT, HDC, INT, INT, DWORD]
_BitBlt.restype = BOOL

_GetDeviceCaps = _gdi32.GetDeviceCaps
_GetDeviceCaps.argtypes = [HDC, INT]
_GetDeviceCaps.restype = INT


# ========================================================================
# 常量
# ========================================================================

# PrintWindow flags
PW_CLIENTONLY = 0x0000001
PW_RENDERFULLCONTENT = 0x0000002

# SendMessageTimeout flags
SMTO_ABORTIFHUNG = 0x0002
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
_GETTEXT_TIMEOUT_MS = 300  # 300ms 超时防止被挂起窗口卡死

# Window messages
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MBUTTONDBLCLK = 0x0209
WM_MOUSEMOVE = 0x0200
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_CLOSE = 0x0010
WM_ACTIVATE = 0x0006

# Mouse key flags
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_SHIFT = 0x0004
MK_CONTROL = 0x0008
MK_MBUTTON = 0x0010

# ShowWindow
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWDEFAULT = 10

# SetWindowPos
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
HWND_BOTTOM = 1

# BitBlt raster ops
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000

# DIBSection usage
DIB_RGB_COLORS = 0

# BI_RGB (no compression)
BI_RGB = 0

# error / corner-case sentinel
_INVALID_HWND = 0

# GDI device caps indexes
LOGPIXELSX = 88
LOGPIXELSY = 90


# ========================================================================
# Virtual Key Codes（常用子集）
# ========================================================================

VK = {
    "BACK": 0x08, "TAB": 0x09, "CLEAR": 0x0C, "ENTER": 0x0D,
    "RETURN": 0x0D, "SHIFT": 0x10, "CTRL": 0x11, "CONTROL": 0x11,
    "ALT": 0x12, "MENU": 0x12, "PAUSE": 0x13, "CAPITAL": 0x14,
    "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20, "PAGEUP": 0x21,
    "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24, "LEFT": 0x25,
    "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "PRINT": 0x2A,
    "SNAPSHOT": 0x2C, "INSERT": 0x2D, "DELETE": 0x2E, "DEL": 0x2E,
    "HELP": 0x2F,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45,
    "F": 0x46, "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A,
    "K": 0x4B, "L": 0x4C, "M": 0x4D, "N": 0x4E, "O": 0x4F,
    "P": 0x50, "Q": 0x51, "R": 0x52, "S": 0x53, "T": 0x54,
    "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58, "Y": 0x59,
    "Z": 0x5A,
    "LWIN": 0x5B, "RWIN": 0x5C, "APPS": 0x5D,
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69,
    "MULTIPLY": 0x6A, "ADD": 0x6B, "SEPARATOR": 0x6C,
    "SUBTRACT": 0x6D, "DECIMAL": 0x6E, "DIVIDE": 0x6F,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "F13": 0x7C, "F14": 0x7D, "F15": 0x7E, "F16": 0x7F,
    "NUMLOCK": 0x90, "SCROLL": 0x91,
    "LSHIFT": 0xA0, "RSHIFT": 0xA1, "LCONTROL": 0xA2, "RCONTROL": 0xA3,
    "LMENU": 0xA4, "RMENU": 0xA5,
}

# 反向映射：vk_code → 名称（调试用）
_VK_NAMES = {v: k for k, v in VK.items()}


# ========================================================================
# 内部工具函数
# ========================================================================

def _make_lparam(x: int, y: int) -> int:
    """将 (x, y) 打包为 LPARAM（低16位=x，高16位=y）"""
    return (y << 16) | (x & 0xFFFF)


def _get_window_text_safe(hwnd: int, timeout_ms: int = _GETTEXT_TIMEOUT_MS) -> str:
    """带超时获取窗口标题，防止挂起窗口阻塞枚举（300ms 超时即跳过）。"""
    buf = ctypes.create_unicode_buffer(1024)
    result = ctypes.c_ulonglong(0)
    ret = _SendMessageTimeout(
        hwnd, WM_GETTEXT, 1024,
        ctypes.addressof(buf),       # LPARAM = buf 地址
        SMTO_ABORTIFHUNG, timeout_ms,
        ctypes.byref(result),
    )
    if ret:
        return buf.value or ""
    return ""


def _get_window_text(hwnd: int) -> str:
    """获取窗口标题（等效 _get_window_text_safe，默认超时 300ms）。"""
    return _get_window_text_safe(hwnd)


def _get_class_name(hwnd: int) -> str:
    """获取窗口类名（直接从系统结构读取，不会阻塞）。"""
    buf = ctypes.create_unicode_buffer(256)
    _GetClassName(hwnd, buf, 256)
    return buf.value or ""


def _get_pid(hwnd: int) -> int:
    pid = DWORD(0)
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


# ========================================================================
# Win32Target
# ========================================================================

class Win32Target:
    """已启动的 Windows 窗口自动化目标。

    所有操作默认在后台进行，不激活窗口、不抢鼠标/键盘焦点。
    截图用 PrintWindow，输入用 PostMessage。

    使用方式：:

        # 查找窗口
        targets = Win32Target.from_title("记事本")
        if targets:
            win = targets[0]            # 取第一个匹配
            frame = win.screenshot()    # 后台截图
            # ... 用现有 Matcher 进行图像匹配 ...
            win.click(x, y)             # 后台点击
    """

    # ----------------------------------------------------------------
    # 构造 / 工厂方法
    # ----------------------------------------------------------------

    def __init__(self, hwnd: int):
        if not isinstance(hwnd, int) or hwnd <= 0:
            raise ValueError(f"无效的窗口句柄: {hwnd}")
        self._hwnd = hwnd
        # 属性缓存：惰性填充，clear_cache() 可清空重读
        self._cache: dict[str, object] = {}

    def clear_cache(self):
        """清空属性缓存。窗口信息变化时调此方法。"""
        self._cache.clear()

    @property
    def dpi_scale(self) -> float:
        """窗口的 DPI 缩放比（如 125% = 1.25）。

        PrintWindow 截图是物理像素，但 Qt/WPF 等应用的客户区坐标是逻辑像素。
        后台点击时需将物理坐标 ÷ dpi_scale 得到正确的逻辑坐标。
        """
        v = self._cache.get("dpi_scale")
        if v is None:
            dc = _GetDC(self._hwnd)
            if dc:
                dpi = _GetDeviceCaps(dc, LOGPIXELSX)
                _ReleaseDC(self._hwnd, dc)
                v = max(dpi / 96.0, 1.0) if dpi > 0 else 1.0
            else:
                v = 1.0
            self._cache["dpi_scale"] = v
        return v  # type: ignore[return-value]

    # --- 属性 ---

    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def title(self) -> str:
        v = self._cache.get("title")
        if v is None:
            v = _get_window_text(self._hwnd)
            self._cache["title"] = v
        return v  # type: ignore[return-value]

    @property
    def class_name(self) -> str:
        v = self._cache.get("class_name")
        if v is None:
            v = _get_class_name(self._hwnd)
            self._cache["class_name"] = v
        return v  # type: ignore[return-value]

    @property
    def pid(self) -> int:
        v = self._cache.get("pid")
        if v is None:
            v = _get_pid(self._hwnd)
            self._cache["pid"] = v
        return v  # type: ignore[return-value]

    @property
    def rect(self) -> dict:
        """整个窗口在屏幕上的矩形区域（包括标题栏/边框）。"""
        v = self._cache.get("rect")
        if v is None:
            r = RECT()
            _GetWindowRect(self._hwnd, ctypes.byref(r))
            v = {
                "left": r.left, "top": r.top,
                "right": r.right, "bottom": r.bottom,
                "width": r.right - r.left, "height": r.bottom - r.top,
            }
            self._cache["rect"] = v
        return v  # type: ignore[return-value]

    @property
    def client_rect(self) -> dict:
        """客户区矩形（相对于自身，left/top 总是 0）。"""
        v = self._cache.get("client_rect")
        if v is None:
            r = RECT()
            _GetClientRect(self._hwnd, ctypes.byref(r))
            v = {
                "left": r.left, "top": r.top,
                "right": r.right, "bottom": r.bottom,
                "width": r.right - r.left, "height": r.bottom - r.top,
            }
            self._cache["client_rect"] = v
        return v  # type: ignore[return-value]

    @property
    def client_rect_screen(self) -> dict:
        """客户区在屏幕上的坐标。"""
        r = RECT()
        _GetClientRect(self._hwnd, ctypes.byref(r))
        pt = POINT(r.left, r.top)
        _ClientToScreen(self._hwnd, ctypes.byref(pt))
        return {
            "left": pt.x, "top": pt.y,
            "right": pt.x + r.right, "bottom": pt.y + r.bottom,
            "width": r.right, "height": r.bottom,
        }

    @property
    def is_visible(self) -> bool:
        return bool(_IsWindowVisible(self._hwnd))

    @property
    def is_minimized(self) -> bool:
        return bool(_IsIconic(self._hwnd))

    @property
    def is_maximized(self) -> bool:
        return bool(_IsZoomed(self._hwnd))

    @property
    def is_valid(self) -> bool:
        """窗口句柄是否仍然有效（窗口未被关闭）。"""
        return bool(_IsWindow(self._hwnd))

    def __repr__(self) -> str:
        return f"<Win32Target hwnd={self._hwnd} title={self.title!r}>"

    # --- 查找窗口 ---

    @staticmethod
    def _enum_all() -> list[dict]:
        """枚举所有顶层窗口，返回 [{hwnd, title, class_name, pid}, ...]"""
        results = []

        def callback(hwnd: int, _lparam) -> bool:
            if _IsWindowVisible(hwnd):
                title = _get_window_text(hwnd)
                cls = _get_class_name(hwnd)
                pid = _get_pid(hwnd)
                results.append({
                    "hwnd": hwnd,
                    "title": title,
                    "class_name": cls,
                    "pid": pid,
                })
            return True  # 继续枚举

        enum_proc = WNDENUMPROC(callback)
        _EnumWindows(enum_proc, 0)
        return results

    @staticmethod
    def all_visible() -> list[Win32Target]:
        """返回所有可见窗口的列表。"""
        return [Win32Target(w["hwnd"]) for w in Win32Target._enum_all()]

    @staticmethod
    def from_title(pattern: str, *, exact: bool = False, case_sensitive: bool = False) -> list[Win32Target]:
        """按窗口标题查找已打开的窗口。

        Args:
            pattern: 匹配模式
            exact: True = 完全匹配, False = 子串匹配
            case_sensitive: 是否区分大小写

        Returns:
            匹配的 Win32Target 列表（按 Z 序降序）
        """
        results = []
        for w in Win32Target._enum_all():
            title = w["title"] if case_sensitive else w["title"].lower()
            pat = pattern if case_sensitive else pattern.lower()
            if (exact and title == pat) or (not exact and pat in title):
                results.append(Win32Target(w["hwnd"]))
        return results

    @staticmethod
    def from_class(class_name: str, *, exact: bool = False) -> list[Win32Target]:
        """按窗口类名查找窗口。"""
        results = []
        for w in Win32Target._enum_all():
            cls = w["class_name"]
            if (exact and cls == class_name) or (not exact and class_name.lower() in cls.lower()):
                results.append(Win32Target(w["hwnd"]))
        return results

    @staticmethod
    def from_pid(pid: int) -> list[Win32Target]:
        """按进程 ID 查找窗口。"""
        return [Win32Target(w["hwnd"]) for w in Win32Target._enum_all() if w["pid"] == pid]

    @staticmethod
    def from_hwnd(hwnd: int) -> Win32Target:
        """从已知的窗口句柄创建。"""
        return Win32Target(hwnd)

    @staticmethod
    def foreground() -> Optional[Win32Target]:
        """获取当前前台窗口（可能返回 None）。"""
        hwnd = _GetForegroundWindow()
        if hwnd and hwnd != _INVALID_HWND:
            return Win32Target(hwnd)
        return None

    @staticmethod
    def from_cursor() -> Optional[Win32Target]:
        """获取鼠标所在位置的窗口。"""
        pt = POINT()
        _GetCursorPos(ctypes.byref(pt))
        hwnd = _WindowFromPoint(pt)
        if hwnd and hwnd != _INVALID_HWND:
            # 找顶层父窗口
            hwnd = _GetAncestor(hwnd, 2)  # GA_ROOT = 2
            if hwnd:
                return Win32Target(hwnd)
        return None

    # ----------------------------------------------------------------
    # 截图
    # ----------------------------------------------------------------

    SCREENSHOT_METHOD_AUTO = "auto"
    SCREENSHOT_METHOD_PRINTWINDOW = "printwindow"
    SCREENSHOT_METHOD_BITBLT = "bitblt"

    def screenshot(self, client_only: bool = True,
                   method: str = "auto") -> np.ndarray:
        """捕获窗口内容为 OpenCV BGR 图像。

        Args:
            client_only:
                True = 仅客户区（推荐，匹配的图像坐标可直接用于 click）
                False = 包含标题栏/边框
            method:
                "auto"         — 先 PrintWindow，失败则 BitBlt 从屏幕复制
                "printwindow"  — 仅 PrintWindow（纯后台，不抢前台）
                "bitblt"       — 仅 BitBlt 从屏幕复制（窗口需在屏幕上可见）

        Returns:
            (H, W, 3) uint8 BGR numpy array
        """
        if method == self.SCREENSHOT_METHOD_AUTO:
            return self._screenshot_auto(client_only)
        elif method == self.SCREENSHOT_METHOD_PRINTWINDOW:
            return self._screenshot_printwindow(client_only)
        elif method == self.SCREENSHOT_METHOD_BITBLT:
            return self._screenshot_bitblt(client_only)
        else:
            raise ValueError(f"未知截图方法: {method}")

    # ── auto：先 PrintWindow，失败回退 BitBlt ────────────────

    @staticmethod
    def _is_valid_frame(frame: np.ndarray) -> bool:
        """检查 PrintWindow 输出的帧是否包含真实内容（非全黑/全白）。"""
        if frame is None or frame.size == 0:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return float(np.std(gray)) > 5.0  # 有效帧的像素方差远大于5

    def _screenshot_auto(self, client_only: bool) -> np.ndarray:
        was_minimized = self.is_minimized
        try:
            if was_minimized:
                # 恢复窗口但不激活，并推到最底层，避免抢焦点
                _ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                _SetWindowPos(self._hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                              SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
                import time
                time.sleep(0.15)

            try:
                frame = self._screenshot_printwindow(client_only)
                if self._is_valid_frame(frame):
                    return frame
            except RuntimeError:
                pass
            return self._screenshot_bitblt(client_only)
        finally:
            if was_minimized:
                _ShowWindow(self._hwnd, SW_MINIMIZE)

    # ── PrintWindow（纯后台，不抢前台） ──────────────────────

    def _screenshot_printwindow(self, client_only: bool) -> np.ndarray:
        flags = PW_RENDERFULLCONTENT
        if client_only:
            flags |= PW_CLIENTONLY

        w, h = self._get_client_size()
        src_dc = _GetDC(self._hwnd) if client_only else _GetWindowDC(self._hwnd)
        if not src_dc:
            raise RuntimeError("无法获取窗口 DC")

        try:
            mem_dc = _CreateCompatibleDC(src_dc)
            if not mem_dc:
                raise RuntimeError("无法创建兼容 DC")
            try:
                bitmap = _CreateCompatibleBitmap(src_dc, w, h)
                if not bitmap:
                    raise RuntimeError("无法创建兼容位图")
                try:
                    _SelectObject(mem_dc, bitmap)
                    if not _PrintWindow(self._hwnd, mem_dc, flags):
                        raise RuntimeError("PrintWindow 返回失败")
                    return self._bitmap_to_ndarray(mem_dc, bitmap, w, h)
                finally:
                    _DeleteObject(bitmap)
            finally:
                _DeleteDC(mem_dc)
        finally:
            _ReleaseDC(self._hwnd, src_dc)

    # ── BitBlt（从屏幕 DC 复制窗口区域） ────────────────────
    # 适用于 PrintWindow 失败的场景（UWP / DirectX 等）。
    # 要求窗口在屏幕上至少部分可见（不能被完全遮挡或最小化）。

    def _screenshot_bitblt(self, client_only: bool) -> np.ndarray:
        if client_only:
            cr = self.client_rect_screen
            sx, sy = cr["left"], cr["top"]
            w, h = cr["width"], cr["height"]
        else:
            r = self.rect
            sx, sy = r["left"], r["top"]
            w, h = r["width"], r["height"]

        if w <= 0 or h <= 0:
            raise RuntimeError(f"窗口尺寸无效: {w}x{h}")

        # 屏幕 DC（捕获显示器上的实际像素）
        screen_dc = _GetDC(0)
        if not screen_dc:
            raise RuntimeError("无法获取屏幕 DC")

        try:
            mem_dc = _CreateCompatibleDC(screen_dc)
            if not mem_dc:
                raise RuntimeError("无法创建兼容 DC")
            try:
                bitmap = _CreateCompatibleBitmap(screen_dc, w, h)
                if not bitmap:
                    raise RuntimeError("无法创建兼容位图")
                try:
                    _SelectObject(mem_dc, bitmap)
                    # 从屏幕坐标 (sx, sy) 复制到内存 DC
                    if not _BitBlt(mem_dc, 0, 0, w, h,
                                   screen_dc, sx, sy,
                                   SRCCOPY | CAPTUREBLT):
                        raise RuntimeError("BitBlt 从屏幕复制失败")
                    return self._bitmap_to_ndarray(mem_dc, bitmap, w, h)
                finally:
                    _DeleteObject(bitmap)
            finally:
                _DeleteDC(mem_dc)
        finally:
            _ReleaseDC(0, screen_dc)

    # ── 辅助 ────────────────────────────────────────────────

    def _get_client_size(self) -> tuple[int, int]:
        r = RECT()
        _GetClientRect(self._hwnd, ctypes.byref(r))
        w, h = r.right, r.bottom
        if w <= 0 or h <= 0:
            raise RuntimeError(
                f"窗口客户区尺寸无效: {w}x{h}（窗口可能已最小化）"
            )
        return w, h

    @staticmethod
    def _bitmap_to_ndarray(dc: int, bitmap: int, w: int, h: int) -> np.ndarray:
        """将 GDI 位图转换为 numpy array (BGR)。"""
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # 负值 = top-down，正立
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        # 分配缓冲区（BGRA 每像素 4 字节）
        buf_size = w * h * 4
        buf = ctypes.create_string_buffer(buf_size)

        # 读像素
        ret = _GetDIBits(dc, bitmap, 0, h, buf,
                         ctypes.byref(bmi), DIB_RGB_COLORS)
        if not ret:
            raise RuntimeError("GetDIBits 失败")

        # 转为 numpy: (H, W, 4) BGRA
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)

        # BGRA → BGR（去掉 Alpha 通道）
        # OpenCV 用 BGR，直接扔掉 A
        if frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        return frame

    # ----------------------------------------------------------------
    # 鼠标操作（PostMessage — 不激活窗口）
    # ----------------------------------------------------------------

    SEND_POST = "post"      # PostMessage（异步）
    SEND_SEND = "send"      # SendMessage（同步，推荐）

    def click(self, x: int, y: int, button: str = "left",
              send_mode: str = "send") -> bool:
        """在指定客户端坐标后台点击（不抢前台）。

        Args:
            x, y: 截图像素坐标（与 screenshot() 返回值坐标系一致）
            button: "left" | "right" | "middle"
            send_mode:
                "send" — SendMessage（同步，DPI 感知应用推荐）
                "post" — PostMessage（异步）
        """
        if not _IsWindow(self._hwnd):
            raise RuntimeError(f"窗口句柄已失效 (hwnd={self._hwnd})")

        # 直接发物理像素坐标，Qt / Windows 会自行处理 DPI 缩放
        msg_down, msg_up = {
            "left": (WM_LBUTTONDOWN, WM_LBUTTONUP),
            "right": (WM_RBUTTONDOWN, WM_RBUTTONUP),
            "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP),
        }[button.lower()]

        lparam = _make_lparam(x, y)
        wparam_down = {
            "left": MK_LBUTTON,
            "right": MK_RBUTTON,
            "middle": MK_MBUTTON,
        }[button.lower()]

        if send_mode == self.SEND_POST:
            r1 = _PostMessage(self._hwnd, msg_down, wparam_down, lparam)
            r2 = _PostMessage(self._hwnd, msg_up, 0, lparam)
            if not r1 or not r2:
                raise RuntimeError(f"PostMessage 失败 (r1={r1} r2={r2})")
        else:
            _SendMessage(self._hwnd, msg_down, wparam_down, lparam)
            _SendMessage(self._hwnd, msg_up, 0, lparam)
        return True

    def double_click(self, x: int, y: int, button: str = "left"):
        """在指定坐标后台双击。"""

        msg_down, msg_dbl, msg_up = {
            "left": (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK, WM_LBUTTONUP),
            "right": (WM_RBUTTONDOWN, WM_RBUTTONDBLCLK, WM_RBUTTONUP),
            "middle": (WM_MBUTTONDOWN, WM_MBUTTONDBLCLK, WM_MBUTTONUP),
        }[button.lower()]

        lparam = _make_lparam(x, y)
        wparam = {
            "left": MK_LBUTTON,
            "right": MK_RBUTTON,
            "middle": MK_MBUTTON,
        }[button.lower()]

        _PostMessage(self._hwnd, msg_down, wparam, lparam)
        _PostMessage(self._hwnd, msg_dbl, wparam, lparam)
        _PostMessage(self._hwnd, msg_up, 0, lparam)

    def mouse_down(self, x: int, y: int, button: str = "left"):
        """按下鼠标按钮。"""

        msg, wparam = {
            "left": (WM_LBUTTONDOWN, MK_LBUTTON),
            "right": (WM_RBUTTONDOWN, MK_RBUTTON),
            "middle": (WM_MBUTTONDOWN, MK_MBUTTON),
        }[button.lower()]
        _PostMessage(self._hwnd, msg, wparam, _make_lparam(x, y))

    def mouse_up(self, x: int, y: int, button: str = "left"):
        """释放鼠标按钮。"""

        msg = {
            "left": WM_LBUTTONUP,
            "right": WM_RBUTTONUP,
            "middle": WM_MBUTTONUP,
        }[button.lower()]
        _PostMessage(self._hwnd, msg, 0, _make_lparam(x, y))

    def mouse_move(self, x: int, y: int):
        """将鼠标移动到指定客户端坐标（后台）。"""

        _PostMessage(self._hwnd, WM_MOUSEMOVE, 0, _make_lparam(x, y))

    def scroll(self, delta: int, x: int = 0, y: int = 0):
        """滚动鼠标滚轮。

        Args:
            delta: 滚动量（正=向上, 负=向下, 通常 120 为一步）
            x, y: 客户端坐标（可选）
        """

        wparam = delta << 16  # 高16位=delta
        _PostMessage(self._hwnd, WM_MOUSEWHEEL, wparam, _make_lparam(x, y))

    # ----------------------------------------------------------------
    # 键盘操作（PostMessage — 不激活窗口）
    # ----------------------------------------------------------------

    def press_key(self, key: str | int):
        """按下按键（PostMessage WM_KEYDOWN）。

        Args:
            key: VK 名称（如 "ENTER"、"A"）、或 VK 码
        """
        vk = VK.get(key.upper(), key) if isinstance(key, str) else key
        _PostMessage(self._hwnd, WM_KEYDOWN, vk, 0)

    def release_key(self, key: str | int):
        """释放按键。"""
        vk = VK.get(key.upper(), key) if isinstance(key, str) else key
        _PostMessage(self._hwnd, WM_KEYUP, vk, 0)

    def key_click(self, key: str | int):
        """按下并释放一个键。"""
        vk = VK.get(key.upper(), key) if isinstance(key, str) else key
        _PostMessage(self._hwnd, WM_KEYDOWN, vk, 0)
        _PostMessage(self._hwnd, WM_KEYUP, vk, 0)

    def type_text(self, text: str):
        """输入文本（通过 WM_CHAR 发送每个字符）。

        注意：中文等 Unicode 字符同样支持，因为 WM_CHAR 使用 UTF-16。
        """
        for ch in text:
            _PostMessage(self._hwnd, WM_CHAR, ord(ch), 0)

    def key_combo(self, *keys: str | int):
        """发送组合键（如 Ctrl+C、Alt+F4）。

        用法: win.key_combo("CTRL", "C")
              win.key_combo("ALT", "F4")
        """
        # 按下所有修饰键
        for k in keys[:-1]:
            vk = VK.get(k.upper(), k) if isinstance(k, str) else k
            _PostMessage(self._hwnd, WM_KEYDOWN, vk, 0)

        # 按最后键
        last = keys[-1]
        last_vk = VK.get(last.upper(), last) if isinstance(last, str) else last
        _PostMessage(self._hwnd, WM_KEYDOWN, last_vk, 0)
        _PostMessage(self._hwnd, WM_KEYUP, last_vk, 0)

        # 释放所有修饰键
        for k in reversed(keys[:-1]):
            vk = VK.get(k.upper(), k) if isinstance(k, str) else k
            _PostMessage(self._hwnd, WM_KEYUP, vk, 0)

    # ----------------------------------------------------------------
    # 窗口控制
    # ----------------------------------------------------------------

    def activate(self):
        """将窗口带到前台（会抢焦点，谨慎使用）。"""
        _SetForegroundWindow(self._hwnd)

    def minimize(self):
        """最小化窗口。"""
        _ShowWindow(self._hwnd, SW_MINIMIZE)

    def maximize(self):
        """最大化窗口。"""
        _ShowWindow(self._hwnd, SW_SHOWMAXIMIZED)

    def restore(self):
        """恢复窗口（从最小化/最大化还原）。"""
        _ShowWindow(self._hwnd, SW_RESTORE)

    def show_no_activate(self):
        """显示窗口但不激活它（SW_SHOWNOACTIVE）。"""
        _ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)

    def move(self, x: int, y: int):
        """移动窗口到屏幕坐标 (x, y)，不改变大小。"""
        _SetWindowPos(self._hwnd, 0, x, y, 0, 0,
                      SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def resize(self, w: int, h: int):
        """调整窗口大小，不改变位置。"""
        r = RECT()
        _GetWindowRect(self._hwnd, ctypes.byref(r))
        _SetWindowPos(self._hwnd, 0, r.left, r.top, w, h,
                      SWP_NOZORDER | SWP_NOACTIVATE)

    def close(self, force: bool = False):
        """关闭窗口。PostMessage WM_CLOSE（安全），或 WM_QUIT 强制。"""
        if force:
            # 强制关闭：发 WM_DESTROY / 终止线程
            _PostMessage(self._hwnd, 0x0002, 0, 0)  # WM_DESTROY
        else:
            _PostMessage(self._hwnd, WM_CLOSE, 0, 0)

    # ----------------------------------------------------------------
    # 坐标系转换
    # ----------------------------------------------------------------

    def client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """将客户端坐标转换为屏幕坐标。"""
        pt = POINT(x, y)
        _ClientToScreen(self._hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def screen_to_client(self, x: int, y: int) -> tuple[int, int]:
        """将屏幕坐标转换为客户端坐标。"""
        pt = POINT(x, y)
        _ScreenToClient(self._hwnd, ctypes.byref(pt))
        return pt.x, pt.y


# ========================================================================
# 便捷函数（脚本中直接用）
# ========================================================================

def enum_windows(title_pattern: str = "") -> list[Win32Target]:
    """查找可见窗口（别名，更方便）。

    Args:
        title_pattern: 为空时返回所有可见窗口

    Returns:
        Win32Target 列表
    """
    if not title_pattern:
        return Win32Target.all_visible()
    return Win32Target.from_title(title_pattern)


def window_from_cursor() -> Optional[Win32Target]:
    """鼠标所在位置的窗口。"""
    return Win32Target.from_cursor()


def foreground_window() -> Optional[Win32Target]:
    """当前前台窗口。"""
    return Win32Target.foreground()
