# core/path.py
from pathlib import Path
import sys
import os


def get_root() -> Path:
    """项目根目录（开发时=源码，打包后=exe所在目录）。"""
    env = os.getenv("PROJECT_ROOT")
    if env:
        return Path(env)

    if getattr(sys, 'frozen', False):
        # exe 目录（脚本、账号配置等用户文件放置处）
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parents[1]


def get_bundle() -> Path:
    """打包后的数据目录（开发时=源码，打包后=sys._MEIPASS）。"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return get_root()


PROJECT_ROOT = get_root()
_BUNDLE = get_bundle()

# 用户文件（在 exe 同级目录）
json_path = PROJECT_ROOT / "json"
SCRIPTS_PATH = PROJECT_ROOT / "scripts"
USER_DATA_PATH = PROJECT_ROOT / "user_data"
PYTHON_PATH = PROJECT_ROOT / "python"

# 打包资源（开发时在源码，exe 时在 _MEIPASS）
ICON_PATH = _BUNDLE / "icon" / "icon.ico"
IMG_PATH = _BUNDLE / "assets" / "images"
CONFIG_PATH = _BUNDLE / "core" / "config" / "config.json"
MODELS_PATH = _BUNDLE / "models"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if __name__ == "__main__":
    print(PROJECT_ROOT)
