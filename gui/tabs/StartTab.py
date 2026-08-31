from PySide6.QtCore import Signal, QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QWidget,
    QVBoxLayout,
)
from gui.tabs.BaseTab import BaseTab


class _AccountCard(QWidget):
    """账号列表项卡片：两行显示 name / email"""

    def __init__(self, name: str, email: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)

        self.name_label = QLabel(name)
        self.name_label.setObjectName("AccountCardName")
        nf = QFont()
        nf.setPointSize(12)
        nf.setBold(True)
        self.name_label.setFont(nf)
        layout.addWidget(self.name_label)

        if email:
            self.email_label = QLabel(email)
            self.email_label.setObjectName("AccountCardEmail")
            ef = QFont()
            ef.setPointSize(10)
            self.email_label.setFont(ef)
            layout.addWidget(self.email_label)


class StartTab(BaseTab):
    account_submitted = Signal(object)

    def __init__(self, facade):
        super().__init__(tab_id="开始")
        self.facade = facade

        self.layout.setContentsMargins(20, 18, 20, 18)
        self.layout.setSpacing(12)

        self.title = QLabel("选择账号")
        self.title.setObjectName("TitleLabel")

        hint = QLabel("选择要打开的工作台账号，开始后可在对应面板里跑脚本与调试。")
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)

        self.account_list = QListWidget()
        self.account_list.setObjectName("AccountList")
        font = QFont()
        font.setPointSize(12)
        self.account_list.setFont(font)
        self.account_list.setSpacing(4)

        self.start_button = QPushButton("开始")
        self.start_button.setObjectName("PrimaryButton")

        self.layout.addWidget(self.title)
        self.layout.addWidget(hint)
        self.layout.addWidget(self.account_list, 1)
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
            name = acc.get("name", "未知账号")
            email = acc.get("email", "")

            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 56))
            self.account_list.addItem(item)
            self.account_list.setItemWidget(item, _AccountCard(name, email))

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
