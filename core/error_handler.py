"""
统一错误处理
=============
GUI 弹窗 + 控制台输出双通道，避免静默崩溃。
"""

import traceback
import sys


def safe_call(parent, label: str, fn, *args, **kwargs):
    """执行 fn，捕获异常并弹窗+打印"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        _report(parent, label, e)
        return None


async def safe_call_async(parent, label: str, fn, *args, **kwargs):
    """异步版"""
    try:
        return await fn(*args, **kwargs)
    except Exception as e:
        _report(parent, label, e)
        return None


def _report(parent, label: str, e: Exception):
    err = traceback.format_exc()
    # 控制台
    print(f"\n[ERROR] {label}: {e}")
    print(err[:1000])
    # GUI 弹窗
    try:
        from PySide6.QtWidgets import QMessageBox
        msg = f"{label} 出错:\n{e}\n\n{err[:500]}"
        QMessageBox.critical(parent, f"错误 - {label}", msg)
    except Exception:
        pass  # 弹窗也失败就算了
