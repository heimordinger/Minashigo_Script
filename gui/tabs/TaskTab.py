from gui.tabs.BaseTab import BaseTab
from gui.widgets.TaskTable import TaskTable

class TaskTab(BaseTab):
    def __init__(self, tab_id: str | None = None):
        super().__init__(tab_id)

        self.table = TaskTable()
        self.layout.addWidget(self.table)
        header = self.table.horizontalHeader()
        font = self.font()
        font.setBold(True)
        header.setFont(font)

    def render(self, state):
        self.table.render(state)

    def on_start(self):
        task = self.table.get_selected_task()
        print("开始任务:", task)

