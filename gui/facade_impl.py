import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from controller.ctrl import Controller
from core.logging.events import LogLevel, LogSource
from gui.state.UIState import UIState
from core.path import json_path, SCRIPTS_PATH, PROJECT_ROOT


class FacadeImpl:
    def __init__(self, controller: Controller = None):
        self.controller = controller
        self.state = UIState()
        accounts = self._load_accounts()
        self.state.accounts = accounts
        self.taskflow_processes: dict[str, subprocess.Popen] = {}

    def _load_accounts(self) -> list[dict]:
        path = Path(json_path, "accounts.json")

        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            path.write_text("{}", encoding="utf-8")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            path.write_text("{}", encoding="utf-8")
            raw = {}

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

    def register_window_target(self, account: dict):
        self.controller.register_window_target(account)

    def sync_taskflow_target(self, account: dict):
        self.controller.sync_taskflow_target(account)

    def update_account(self, index: int, account: dict):
        self.state.accounts[index] = account
        self.save_accounts()

    def delete_account(self, index: int):
        self.state.accounts.pop(index)
        self.save_accounts()

    def save_accounts(self):
        path = Path(json_path, "accounts.json")

        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            a["name"]: {
                "email": a["email"],
                "password": a["password"]
            }
            for a in self.state.accounts
        }

        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

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
            str(p.relative_to(process_dir)).replace("\\", "/")
            for p in process_dir.rglob("*.py")
            if p.is_file()
        ])

    def get_status_snapshot(self):
        return []

    def shutdown(self):
        # 停止所有taskflow服务器
        for account_name, process in self.taskflow_processes.items():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                print(f"[Facade] Stopped taskflow server for {account_name} during shutdown")
        
        self.taskflow_processes.clear()
        
        # 关闭浏览器
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
        name = account['name']

        # 1. 停止正在运行的任务
        self.controller.stop_task(account)

        # 2. 关闭 Playwright 浏览器（释放端口、杀 Chrome 进程）
        browser = self.controller._browsers.get(name)
        if browser:
            future = self.controller.submit(browser.close())
            try:
                future.result(timeout=10)
            except Exception as e:
                print(f"[Facade] 关闭浏览器超时或异常: {e}")

        # 3. 清理 Controller 内部状态
        self.controller._browsers.pop(name, None)
        self.controller._browser_instances.pop(name, None)
        self.controller._window_instances.pop(name, None)
        self.controller._task_ctrls.pop(name, None)
        self.controller._tasks.pop(name, None)
        self.controller._running.pop(name, None)

        # 4. 清理 TaskFlow 的浏览器引用（run_taskflow.browsers）
        try:
            taskflow_path = Path(__file__).parent.parent / "taskflow"
            if str(taskflow_path) not in sys.path:
                sys.path.insert(0, str(taskflow_path))
            from run_taskflow import browsers as tf_browsers
            account_email = account.get('email', '')
            tf_browsers.pop(account_email, None)
            print(f"[Facade] 已清理TaskFlow浏览器引用: {account_email}")
        except Exception as e:
            print(f"[Facade] 清理TaskFlow浏览器引用失败: {e}")

        # 5. 停止 taskflow 服务器子进程
        self.stop_taskflow_server(account)

        print(f"[Facade] 账号 {name} 已完全关闭")

    def register_account_to_taskflow(self, account: dict):
        """将账号注册到Taskflow管理器"""
        from core.taskflow_manager import taskflow_manager
        
        account_name = account.get('name', 'default')
        
        # 注册账号到Taskflow管理器
        taskflow_manager.register_account(account)
        print(f"[Facade] 已将账号 {account_name} 注册到Taskflow管理器")

    def open_taskflow_browser(self, account: dict):
        """为指定账号打开taskflow网页"""
        from core.taskflow_manager import taskflow_manager
        
        account_name = account.get('name', 'default')
        
        # 使用Taskflow管理器打开网页
        success = taskflow_manager.open_taskflow_for_account(account_name)
        
        if success:
            print(f"[Facade] 已为账号 {account_name} 打开Taskflow网页")
        else:
            print(f"[Facade] 为账号 {account_name} 打开Taskflow网页失败")

    def get_taskflow_process(self, account: dict):
        """获取taskflow进程"""
        account_name = account.get('name')
        return self.taskflow_processes.get(account_name)

    def stop_taskflow_server(self, account: dict):
        """停止taskflow服务器"""
        account_name = account.get('name')
        process = self.taskflow_processes.pop(account_name, None)
        
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            print(f"[Facade] Stopped taskflow server for {account_name}")

    def subscribe(self, fn):
        self.controller.subscribe(fn)
