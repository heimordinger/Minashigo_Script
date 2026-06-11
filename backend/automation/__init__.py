"""
backend/automation —— 窗口级自动化模块

底层：纯 ctypes 调 Win32 API（零外部依赖）
核心：Win32Target 控制已启动的任意 Windows 窗口

用法::

    from backend.automation.win32_target import Win32Target

    # 查找窗口
    targets = Win32Target.from_title("记事本")
    win = targets[0]

    # 截图
    frame = win.screenshot()                  # np.ndarray (BGR)
    result = matcher.match(target=frame, ...)

    # 后台点击
    win.click(result.x, result.y)              # 不抢前台

    # 键盘输入
    win.type_text("hello")
    win.key_combo("CTRL", "V")
"""

from .win32_target import Win32Target

__all__ = ["Win32Target"]

