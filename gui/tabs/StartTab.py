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
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(2)

        self.name_label = QLabel(name)
        nf = QFont()
        nf.setPointSize(12)
        nf.setBold(True)
        self.name_label.setFont(nf)
        self.name_label.setStyleSheet("color: #e8e8e8;")

        layout.addWidget(self.name_label)

        if email:
            self.email_label = QLabel(email)
            ef = QFont()
            ef.setPointSize(10)
            self.email_label.setFont(ef)
            self.email_label.setStyleSheet("color: #888888;")
            layout.addWidget(self.email_label)


class StartTab(BaseTab):
    account_submitted = Signal(object)

    def __init__(self, facade):
        super().__init__(tab_id="开始")
        self.facade = facade

        # ── 标题 ──
        self.title = QLabel("选择账号")

        # ── 账号列表 ──
        self.account_list = QListWidget()
        font = QFont()
        font.setPointSize(12)
        self.account_list.setFont(font)
        self.account_list.setSpacing(4)
        self.account_list.setStyleSheet("""
            QListWidget {
                background-color: #1a1a1a;
                border: none;
                border-radius: 10px;
                padding: 6px;
                outline: none;
            }
            QListWidget::item {
                background-color: #252525;
                border-radius: 8px;
                border: 1px solid #2e2e2e;
            }
            QListWidget::item:hover {
                background-color: #2e2e2e;
                border-color: #3a3a3a;
            }
            QListWidget::item:selected {
                background-color: #1e3a5f;
                border-color: #4aa3ff;
            }
        """)

        # ── 开始按钮 ──
        self.start_button = QPushButton("开始")

        # ── 布局 ──
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
            name = acc.get("name", "未知账号")
            email = acc.get("email", "")

            item = QListWidgetItem()
            item.setSizeHint(QSize(0, 52))
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
