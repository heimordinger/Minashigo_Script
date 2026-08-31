# core/config/config.py
import json
from pathlib import Path

from core.path import CONFIG_PATH, PROJECT_ROOT

_DEFAULT_CONFIG = {
    "browser": {
        "browser_path": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "browser_data_dir": str(PROJECT_ROOT / "browser_data"),
    },
    "task": {
        "enable_daemon_task": True
    },
    "ui": {
        "theme": "light",  # light | dark
    },
    "loading": {
        "topmost": True,
    },
}


def _is_chromium_browser(exe_path: Path) -> bool:
    if not exe_path.exists() or not exe_path.is_file():
        return False
    name = exe_path.name.lower()
    keywords = ["chrome", "edge", "brave", "opera"]
    if not any(k in name for k in keywords):
        return False
    parent = exe_path.parent

    chromium_signatures = [
        "chrome.dll",
        "msedge.dll",
        "resources.pak",
        "icudtl.dat",
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
        "opera.exe",
    ]

    for sig in chromium_signatures:
        if (parent / sig).exists():
            return True

    return False


def _get_nested(data: dict, keys: list):
    cur = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set_nested(data: dict, keys: list, value):
    cur = data
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def _delete_nested(data: dict, keys: list):
    cur = data
    for k in keys[:-1]:
        if k not in cur:
            return
        cur = cur[k]
    cur.pop(keys[-1], None)


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data: dict = {}  # 只存用户数据

    def load(self):
        """
        只加载用户数据，不填默认值
        """
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def save(self):
        """
        只保存用户数据
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get(self, key_path: str):
        """
        获取值（优先用户值，否则默认值）
        key_path: "browser.browser_path"
        """
        keys = key_path.split(".")

        user_val = _get_nested(self.data, keys)
        if user_val not in (None, ""):
            return user_val

        return _get_nested(_DEFAULT_CONFIG, keys)

    def get_user(self, key_path: str):
        """
        只获取用户值（不 fallback）
        """
        return _get_nested(self.data, key_path.split("."))

    def get_default(self, key_path: str):
        """
        获取默认值
        """
        return _get_nested(_DEFAULT_CONFIG, key_path.split("."))

    def set(self, key_path: str, value):
        """
        写入用户配置：
        - 自动校验
        - 如果 value == 默认值 → 删除
        """

        self._validate(key_path, value)

        keys = key_path.split(".")
        default_val = self.get_default(key_path)

        if value == default_val or value in ("", None):
            _delete_nested(self.data, keys)
        else:
            _set_nested(self.data, keys, value)

    @property
    def browser_path(self) -> Path | None:
        val = self.get("browser.browser_path")
        return Path(val) if val else None

    @property
    def browser_data_dir(self) -> Path | None:
        val = self.get("browser.browser_data_dir")
        return Path(val) if val else None

    @property
    def project_version(self) -> str:
        return self.get("app.project_version")

    @property
    def author(self) -> str:
        return self.get("app.author")

    @property
    def about(self) -> str:
        return self.get("app.about")

    @property
    def enable_daemon_task(self) -> bool:
        return bool(self.get("task.enable_daemon_task"))

    @property
    def ui_theme(self) -> str:
        val = (self.get("ui.theme") or "light")
        val = str(val).strip().lower()
        return val if val in ("light", "dark") else "light"

    def _validate(self, key_path: str, value):
        if key_path == "ui.theme":
            if value not in ("light", "dark"):
                raise ValueError("主题必须是: light, dark")
            return

        if key_path == "browser.browser_path":
            p = Path(value)

            if not p.exists():
                raise ValueError("浏览器路径不存在")

            if not p.is_file():
                raise ValueError("浏览器路径必须是 .exe 文件")

            if p.suffix.lower() != ".exe":
                raise ValueError("请选择 .exe 文件")

            if not _is_chromium_browser(p):
                raise ValueError("该浏览器不是 Chromium 内核")

        elif key_path == "browser.browser_data_dir":
            if value in ("", None):
                return

            p = Path(value)

            if not p.exists():
                raise ValueError("用户数据目录不存在")

            if not p.is_dir():
                raise ValueError("用户数据必须是文件夹")


print("PROJECT_ROOT:", PROJECT_ROOT)
config = Config()
