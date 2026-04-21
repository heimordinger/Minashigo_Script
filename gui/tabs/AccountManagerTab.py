# gui/tabs/AccountManagerTab.py
import qtawesome as qta
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QPushButton,
    QHBoxLayout, QDialog, QLineEdit, QLabel, QDialogButtonBox,
    QMessageBox, QHeaderView
)

from core.path import ICON_PATH


class AccountManagerTab(QWidget):
    accounts_changed = Signal()

    def __init__(self, facade):
        super().__init__()
        self.facade = facade
        self.setWindowTitle("账号管理")
        self.resize(600, 400)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "邮箱", "密码", "操作"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.add_btn = QPushButton("添加账号")
        self.add_btn.clicked.connect(self.add_account)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Fixed)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.add_btn)

        self.load_accounts()
        self.resize_columns()

    def load_accounts(self):
        self.table.setRowCount(0)
        accounts = self.facade.list_accounts()

        for idx, acc in enumerate(accounts):
            self.table.insertRow(idx)

            self.table.setItem(idx, 0, QTableWidgetItem(acc["name"]))
            self.table.setItem(idx, 1, QTableWidgetItem(acc["email"]))

            pwd_item = QTableWidgetItem("*" * 8)
            pwd_item.setData(Qt.UserRole, acc["password"])
            self.table.setItem(idx, 2, pwd_item)

            toggle_btn = QPushButton()
            toggle_btn.setIcon(qta.icon("fa5.eye"))
            toggle_btn.setIconSize(QSize(16, 16))
            toggle_btn.setFixedSize(28, 28)
            toggle_btn.setFlat(True)

            edit_btn = QPushButton("编辑")
            del_btn = QPushButton("删除")

            toggle_btn.clicked.connect(
                lambda _, r=idx, b=toggle_btn: self.toggle_password(r, b)
            )
            edit_btn.clicked.connect(lambda _, r=idx: self.edit_account(r))
            del_btn.clicked.connect(lambda _, r=idx: self.delete_account(r))

            widget = QWidget()
            hlayout = QHBoxLayout(widget)
            hlayout.setContentsMargins(0, 0, 0, 0)
            hlayout.addWidget(toggle_btn)
            hlayout.addWidget(edit_btn)
            hlayout.addWidget(del_btn)
            hlayout.addStretch()

            self.table.setCellWidget(idx, 3, widget)


    def add_account(self):
        existing_names = {acc["name"] for acc in self.facade.list_accounts()}

        dialog = AccountDialog(parent=self, existing_names=existing_names)

        if dialog.exec() == QDialog.Accepted:
            account = dialog.get_account()
            if self.facade.add_account(account):
                self.load_accounts()
            else:
                QMessageBox.warning(self, "提示", "账号名称已存在")

        self.accounts_changed.emit()

    def edit_account(self, row):
        accounts = self.facade.list_accounts()
        acc = accounts[row]

        existing_emails = {
            a["email"] for i, a in enumerate(accounts) if i != row
        }

        dialog = AccountDialog(
            account=acc,
            parent=self,
            existing_emails=existing_emails
        )

        if dialog.exec() == QDialog.Accepted:
            new_acc = dialog.get_account()
            self.facade.update_account(row, new_acc)
            self.load_accounts()

        self.accounts_changed.emit()

    def delete_account(self, row):
        self.facade.delete_account(row)
        self.load_accounts()
        self.accounts_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_columns()

    def resize_columns(self):
        total = self.table.viewport().width()

        if total <= 0:
            return

        w0 = int(total * 8/3 / 10)
        w1 = int(total * 8/3 / 10)
        w2 = int(total * 8/3 / 10)
        w3 = int(total * 2 / 10)

        w3 = max(w3, 180)

        self.table.setColumnWidth(0, w0)
        self.table.setColumnWidth(1, w1)
        self.table.setColumnWidth(2, w2)
        self.table.setColumnWidth(3, w3)


    def toggle_password(self, row, btn):
        item = self.table.item(row, 2)
        real_pwd = item.data(Qt.UserRole)

        if item.text().startswith("*"):
            item.setText(real_pwd)
            btn.setIcon(qta.icon("fa5.eye-slash"))
        else:
            item.setText("*" * len(real_pwd))
            btn.setIcon(qta.icon("fa5.eye"))


class AccountDialog(QDialog):
    def __init__(self, account=None, parent=None, existing_names=None, existing_emails=None):
        super().__init__(parent)

        self.existing_names = existing_names or set()
        self.existing_emails = existing_emails or set()

        self.setWindowTitle("账号信息")
        self.setFixedSize(300, 200)
        self.setWindowIcon(QIcon(str(ICON_PATH)))

        self.name_input = QLineEdit()
        self.email_input = QLineEdit()
        self.pwd_input = QLineEdit()
        self.pwd_input.setEchoMode(QLineEdit.Password)

        self.toggle_pwd_btn = QPushButton()
        self.toggle_pwd_btn.setIcon(qta.icon("fa5.eye"))
        self.toggle_pwd_btn.setIconSize(QSize(16, 16))
        self.toggle_pwd_btn.setFixedSize(28, 28)
        self.toggle_pwd_btn.setCheckable(True)
        self.toggle_pwd_btn.setFlat(True)
        self.toggle_pwd_btn.clicked.connect(self.toggle_password_visibility)

        if account:
            self.name_input.setText(account["name"])
            self.email_input.setText(account["email"])
            self.pwd_input.setText(account["password"])
        else:
            self._init_suggested_name()

        self.email_input.textChanged.connect(self._on_email_changed)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("名称"))
        layout.addWidget(self.name_input)
        layout.addWidget(QLabel("邮箱"))
        layout.addWidget(self.email_input)
        layout.addWidget(QLabel("密码"))

        pwd_layout = QHBoxLayout()
        pwd_layout.addWidget(self.pwd_input)
        pwd_layout.addWidget(self.toggle_pwd_btn)
        layout.addLayout(pwd_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _init_suggested_name(self):
        text = self.email_input.text()
        if "@" in text:
            base = text.split("@")[0]
            self.name_input.setText(self._suggest_name(base))

    def _on_email_changed(self, text: str):
        if self.name_input.text():
            return
        if "@" not in text:
            return
        base = text.split("@")[0]
        self.name_input.setText(self._suggest_name(base))

    def _suggest_name(self, base: str) -> str:
        if not base:
            return ""

        if base not in self.existing_names:
            return base

        i = 2
        while f"{base}_{i}" in self.existing_names:
            i += 1
        return f"{base}_{i}"


    def accept(self):
        name = self.name_input.text().strip()
        email = self.email_input.text().strip()

        if not name:
            QMessageBox.warning(self, "提示", "名称不能为空")
            return

        if not email:
            QMessageBox.warning(self, "提示", "邮箱不能为空")
            return

        if self.existing_emails and email in self.existing_emails:
            QMessageBox.warning(self, "提示", f"邮箱「{email}」已存在")
            return

        super().accept()

    def get_account(self):
        return {
            "name": self.name_input.text().strip(),
            "email": self.email_input.text().strip(),
            "password": self.pwd_input.text().strip()
        }


    def toggle_password_visibility(self):
        if self.toggle_pwd_btn.isChecked():
            self.pwd_input.setEchoMode(QLineEdit.Normal)
            self.toggle_pwd_btn.setIcon(qta.icon("fa5.eye-slash"))
        else:
            self.pwd_input.setEchoMode(QLineEdit.Password)
            self.toggle_pwd_btn.setIcon(qta.icon("fa5.eye"))
