from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QHBoxLayout,
    QPushButton,
    QHeaderView
)

class TaskTable(QTableWidget):
    def __init__(self):
        super().__init__()
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["账号", "脚本", "状态", "操作"])

        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)

        self.verticalHeader().setVisible(False)

        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionsMovable(True)

    def render(self, state):
        rows = state.task_rows
        self.setRowCount(len(rows))

        for row, row_state in enumerate(state.task_rows):
            self._render_account(row, row_state)
            self._render_task_selector(row, row_state)
            self._render_status(row, row_state)
            self._render_action(row, row_state)

    def _render_account(self, row, row_state):
        item = self.item(row, 0)
        if not item:
            item = QTableWidgetItem()
            self.setItem(row, 0, item)
        item.setText(row_state.account_name)

    def _render_task_selector(self, row, row_state):
        combo = self.cellWidget(row, 1)
        if not combo:
            combo = QComboBox()
            combo.currentTextChanged.connect(
                lambda task, r=row: self.on_task_selected(r, task)
            )
            self.setCellWidget(row, 1, combo)

        combo.blockSignals(True)
        combo.clear()
        combo.addItems(row_state.available_tasks)
        if row_state.selected_task:
            combo.setCurrentText(row_state.selected_task)
        combo.blockSignals(False)

    def _render_status(self, row, row_state):
        label = self.cellWidget(row, 2)
        if not label:
            label = QLabel()
            self.setCellWidget(row, 2, label)

        label.setText(row_state.status)

    def _render_action(self, row, row_state):
        action = self.cellWidget(row, 3)
        if not action:
            action = TaskActionCell()
            action.start_clicked.connect(
                lambda _, r=row: self.on_start_clicked(r)
            )
            action.stop_clicked.connect(
                lambda _, r=row: self.on_stop_clicked(r)
            )
            self.setCellWidget(row, 3, action)

        action.update_state(row_state.running)

    def on_task_selected(self, row: int, task: str):
        print(f"[UI] row={row} selected task={task}")

    def on_start_clicked(self, row: int):
        print(f"[UI] start row={row}")

    def on_stop_clicked(self, row: int):
        print(f"[UI] stop row={row}")

    def add_empty_task(self):
        pass

    def get_selected_task(self) -> dict:
        pass

class TaskActionCell(QWidget):
    start_clicked = Signal()
    stop_clicked = Signal()

    def __init__(self):
        super().__init__()

        self.start_btn = QPushButton("开始")
        self.stop_btn = QPushButton("停止")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.start_btn)
        layout.addWidget(self.stop_btn)

        self.start_btn.clicked.connect(self.start_clicked)
        self.stop_btn.clicked.connect(self.stop_clicked)

    def update_state(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

