# backend/browser/launcher.py

import subprocess
from pathlib import Path


class BrowserLaunchError(Exception):
    pass


class BrowserLauncher:
    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def start(
        self,
        browser_path: Path,
        user_data: Path,
        port: int,
        url: str | None = None,
    ):
        self._validate_browser(browser_path)
        self._validate_user_data(user_data)
        user_data.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(browser_path),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={str(user_data)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            # 隐藏自动化提示栏，避免视口变化
            "--disable-infobars",
            "--disable-extensions",
            "--disable-sync",
            # 允许最小化/后台时继续渲染，保证截图正常
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        if url:
            cmd.append(url)

        try:
            print("launch cmd:", cmd)
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            raise BrowserLaunchError(f"浏览器启动失败: {e}")

        return self.proc

    def _validate_browser(self, path: Path):
        print("进入 _validate_browser")

        if not path:
            raise BrowserLaunchError("浏览器路径为空")

        print("路径存在检查")
        if not path.exists():
            raise BrowserLaunchError("浏览器路径不存在")

        print("文件检查")
        if not path.is_file():
            raise BrowserLaunchError("浏览器路径必须是文件")

        print("后缀检查")
        if path.suffix.lower() != ".exe":
            raise BrowserLaunchError("浏览器必须是 .exe 文件")

        print("开始 Chromium 检测")
        if not self._is_chromium(path):
            raise BrowserLaunchError("该浏览器不是 Chromium 内核")

        print("通过 Chromium 检测")

    def _validate_user_data(self, path: Path):
        if not path:
            raise BrowserLaunchError("用户数据目录为空")

        if path.exists() and not path.is_dir():
            raise BrowserLaunchError("用户数据路径必须是目录")

    def _is_chromium(self, exe_path: Path) -> bool:
        name = exe_path.name.lower()

        keywords = ["chrome", "edge", "brave", "opera"]
        if not any(k in name for k in keywords):
            return False

        parent = exe_path.parent

        signatures = [
            "chrome.dll",
            "msedge.dll",
            "resources.pak",
            "icudtl.dat",
            "chrome.exe",
            "msedge.exe"
        ]

        return any((parent / sig).exists() for sig in signatures)