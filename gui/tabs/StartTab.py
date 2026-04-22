from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QPushButton,
)
from gui.tabs.BaseTab import BaseTab


class StartTab(BaseTab):
    account_submitted = Signal(object)

    def __init__(self, facade):
        super().__init__(tab_id="开始")
        self.facade = facade
        self.title = QLabel("选择账号")
        font = QFont()
        font.setPointSize(12)
        self.account_list = QListWidget()
        self.account_list.setFont(font)
        self.start_button = QPushButton("开始")

        self.layout.addWidget(self.title)
        self.layout.addWidget(self.account_list)
        self.layout.addWidget(self.start_button)
        self.start_button.clicked.connect(self.on_start_clicked)
        self._load_accounts()

    def _load_accounts(self):
        self.account_list.clear()
        accounts = self.facade.list_accounts()
        if not accounts:
            self.account_list.addItem("未找到账号")
            self.account_list.setEnabled(False)
            self.start_button.setEnabled(False)
            return

        for acc in accounts:
            text = acc.get("name") or acc.get("email", "未知账号")
            self.account_list.addItem(text)

        self.account_list.setEnabled(True)
        self.start_button.setEnabled(True)

    def render(self, state=None):
        self._load_accounts()

    def on_start_clicked(self):
        row = self.account_list.currentRow()
        if row < 0:
            return
        account = self.facade.list_accounts()[row]
        self.facade.select_account(account)
        added = self.facade.add_account_to_tasks(account)
        if not added:
            print("账号已存在")

        self.account_submitted.emit(account)
