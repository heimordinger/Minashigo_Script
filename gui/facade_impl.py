import json
from pathlib import Path

from controller.ctrl import Controller
from core.logging.events import LogLevel, LogSource
from gui.state.UIState import UIState
from core.path import json_path, SCRIPTS_PATH


class FacadeImpl:
    def __init__(self, controller: Controller = None):
        self.controller = controller
        self.state = UIState()
        accounts = self._load_accounts()
        self.state.accounts = accounts

    def _load_accounts(self) -> list[dict]:
        path = Path(json_path, "accounts.json")
        if not path.exists():
            return []

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        accounts = []
        for name, info in raw.items():
            accounts.append({
                "name": name,
                "email": info.get("email", ""),
                "password": info.get("password", "")
            })

        return accounts

    def list_accounts(self):
        return self.state.accounts

    def select_account(self, account: dict):
        self.state.current_account = account
        self.state.message = f"已选择账号：{account.get('name')}"

    def get_current_account(self):
        return self.state.current_account

    def add_account(self, account: dict) -> bool:
        if any(a["name"] == account["name"] for a in self.state.accounts):
            return False

        self.state.accounts.append(account)
        self.save_accounts()

        self.controller.emit_log(
            account=account["name"],
            message="账号已添加",
            level=LogLevel.INFO,
            source=LogSource.SYSTEM
        )
        return True

    def reconnect_browser(self, account: dict):
        self.controller.reconnect_browser(account)

    def update_account(self, index: int, account: dict):
        self.state.accounts[index] = account
        self.save_accounts()

    def delete_account(self, index: int):
        self.state.accounts.pop(index)
        self.save_accounts()

    def save_accounts(self):
        path = Path(json_path, "accounts.json")
        data = {a["name"]: {"email": a["email"], "password": a["password"]}
                for a in self.state.accounts}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_targets(self):
        return []

    def list_tasks(self):
        return []

    def start_task(self, target, task):
        print("start_task:", target, task)
        self.controller.start_task(account=target, task_name=task)

    def stop_task(self, target):
        print("stop_task:", target)
        self.controller.stop_task(target)

    def stop_all(self):
        pass

    def scan_process_tasks(self) -> list[str]:
        process_dir = Path(SCRIPTS_PATH)
        if not process_dir.exists():
            return []

        return sorted([
            p.stem
            for p in process_dir.iterdir()
            if p.is_file() and p.suffix == ".py"
        ])

    def get_status_snapshot(self):
        return []

    def shutdown(self):
        browsers = self.controller._browsers
        for browser in browsers.values():
            self.controller.submit(browser.close())

    def add_account_to_tasks(self, account: dict) -> bool:
        for row in self.state.task_rows:
            if row.account_name == account["name"]:
                self.state.message = "该账号已在任务列表中"
                return False

        from gui.state.TaskRowState import TaskRowState

        self.state.task_rows.append(
            TaskRowState(
                account_name=account["name"],
                available_tasks=self.scan_process_tasks(),
                selected_task=None,
                running=False,
                status="待启动"
            )
        )

        self.state.message = f"已添加账号：{account['name']}"
        return True

    def open_page(self, account: dict):
        print(f"[Facade] 打开网页: {account['name']}")

    def start_browser(self, account: dict):
        self.controller.submit(
            self._start_browser_async(account)
        )

    async def _start_browser_async(self, account: dict):
        await self.controller.start_browser_async(account)

    def restart_browser(self, account: dict):
        print(f"[Facade] 重启浏览器: {account['name']}")

    def close_browser(self, account: dict):
        print(f"[Facade] 关闭浏览器: {account['name']}")

    def subscribe(self, fn):
        self.controller.subscribe(fn)
