"""演示 / 截图模式：不读真实 accounts.json，日志脱敏。

启用：环境变量 MINASHIGO_DEMO=1，或运行 tools/run_demo.bat
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path

ENV_FLAG = "MINASHIGO_DEMO"

# 仅供界面展示，勿用于真实登录（名称与真实编队无关）
DEMO_ACCOUNTS: list[dict] = [
    {
        "name": "编队-01",
        "email": "user01@example.com",
        "password": "example-password",
    },
    {
        "name": "编队-02",
        "email": "user02@example.com",
        "password": "example-password",
    },
    {
        "name": "编队-03",
        "email": "user03@example.com",
        "password": "example-password",
    },
]

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
)
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


def is_demo_mode() -> bool:
    v = (os.getenv(ENV_FLAG) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def demo_accounts() -> list[dict]:
    return deepcopy(DEMO_ACCOUNTS)


def demo_browser_data_dir(account_name: str, *, project_root: Path) -> Path:
    """与真实 browser_data 隔离的演示配置目录。"""
    safe = str(account_name or "default").strip() or "default"
    return project_root / "browser_data_demo" / safe


def demo_settings_paths(*, project_root: Path) -> tuple[str, str]:
    """设置页展示的占位路径（不含本机用户名）。"""
    root = project_root.resolve()
    browser = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    data = str(root / "browser_data_demo")
    return browser, data


def mask_sensitive(text: str) -> str:
    """日志 / 状态栏里的邮箱、手机号打码。"""
    if not text:
        return text
    out = _EMAIL_RE.sub(lambda m: _mask_email(m.group(0)), str(text))
    out = _PHONE_RE.sub("1**********", out)
    return out


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***@***"
    if len(local) <= 2:
        masked_local = local[0] + "***" if local else "***"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def demo_window_title_suffix() -> str:
    return " [演示]" if is_demo_mode() else ""


def demo_startup_banner() -> str:
    if not is_demo_mode():
        return ""
    return (
        "[演示模式] 已加载示例账号，不会读写 json/accounts.json；"
        "浏览器配置使用 browser_data_demo/；日志邮箱会自动打码。"
    )


def demo_block_mutation(*, parent=None) -> bool:
    """演示模式下拦截增删改账号。返回 True 表示已拦截。"""
    if not is_demo_mode():
        return False
    try:
        from PySide6.QtWidgets import QMessageBox

        if parent is not None:
            QMessageBox.information(parent, "演示模式", "演示模式下不可修改账号。")
    except Exception:
        pass
    return True
