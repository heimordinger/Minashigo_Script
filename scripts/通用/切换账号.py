"""
通用账号切换 —— 弹窗选择目标账号，清除登录态后重新登录。
"""

import json
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Signal, Qt

from backend.browser.user_browser import UserBrowser


class _DialogHelper(QObject):
    """辅助跨线程弹窗（BlockingQueuedConnection 保证在主线程执行）。"""
    _requested = Signal()

    def __init__(self, accounts_data):
        super().__init__()
        self._accounts = accounts_data
        self._result = []
        self._requested.connect(self._show, Qt.BlockingQueuedConnection)

    def pick(self) -> dict | None:
        self._requested.emit()
        return self._result[0] if self._result else None

    def _show(self):
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QListWidget, QListWidgetItem
        )
        from PySide6.QtCore import Qt

        dlg = QDialog()
        dlg.setWindowTitle("选择目标账号")
        dlg.resize(300, 400)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("选择要切换到的账号:"))
        lw = QListWidget()
        first = None
        for name, info in self._accounts:
            item = QListWidgetItem(f"{name}  ({info.get('email', '')})")
            item.setData(Qt.UserRole, info)
            lw.addItem(item)
            if first is None:
                first = item
        if first:
            lw.setCurrentItem(first)
        layout.addWidget(lw)
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        lw.itemDoubleClicked.connect(dlg.accept)
        if dlg.exec() == QDialog.Accepted:
            item = lw.currentItem()
            if item:
                info = item.data(Qt.UserRole)
                name = lw.currentItem().text().split("  (")[0]
                self._result.append({
                    "name": name,
                    "email": info.get("email", ""),
                    "password": info.get("password", ""),
                })


async def login(browser: UserBrowser, url: str, target_account: dict):
    ENTRY_URL = url
    FINAL_GAME_PATH = urlparse(url).path.strip("/")
    LOGIN_KW = "accounts.dmm.co.jp"
    name = target_account.get("name", "?")
    browser.script_log(f"切换至账号: {name}")
    browser.account = target_account
    browser.script_log("清除 Cookie / Storage...")
    await browser.clear_session()
    await browser.goto(ENTRY_URL)
    while True:
        await browser.b_sleep(0.5)
        cur_url = await browser.get_url
        cur_title = (await browser.get_title).lower()
        if FINAL_GAME_PATH not in cur_url and LOGIN_KW in cur_url:
            browser.script_log("检测到 DMM 登录页，执行登录...")
            await browser.dmm_login()
            continue
        if FINAL_GAME_PATH in cur_url:
            stable = 0
            while stable < 3:
                await browser.b_sleep(0.5)
                if FINAL_GAME_PATH in (await browser.get_url):
                    stable += 1
                else:
                    stable = 0
            browser.script_log(f"账号切换完成: {name}")
            break


async def _pick_account_async() -> dict | None:
    """跨线程弹出账号选择对话框。"""
    accounts_file = Path(__file__).resolve().parents[2] / "json" / "accounts.json"
    if not accounts_file.exists():
        return None
    raw = json.loads(accounts_file.read_text(encoding="utf-8"))
    if not raw:
        return None
    accounts = list(raw.items())
    helper = _DialogHelper(accounts)
    from PySide6.QtWidgets import QApplication
    helper.moveToThread(QApplication.instance().thread())
    return helper.pick()


async def do_work(browser: UserBrowser):
    target = await _pick_account_async()
    if target is None:
        browser.script_log("未选择账号，取消切换")
        return
    current_url = await browser.get_url
    if "play.games.dmm.co.jp" not in current_url:
        browser.script_log("当前页面不是 DMM 游戏页，请在浏览器中先打开游戏")
        return
    await login(browser, current_url, target)
