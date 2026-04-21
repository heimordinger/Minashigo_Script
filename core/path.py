# core/path.py
import sys
from pathlib import Path


def get_this_path():
    this_path = Path(__file__).resolve().parent
    while True:
        if (this_path / "main.py").exists():
            return this_path
        if this_path.parent == this_path:
            print("未找到脚本根目录(main.py)，请确认是否更改了文件夹名称再重新运行")
            print("按任意按键关闭程序……")
            input()
            sys.exit()
        this_path = this_path.parent


PROJECT_ROOT = get_this_path()
json_path = PROJECT_ROOT / "json"
SCRIPTS_PATH = PROJECT_ROOT / "scripts"
ICON_PATH = PROJECT_ROOT / "icon" / "icon.png"
IMG_PATH = PROJECT_ROOT / "assets" / "images"
USER_DATA_PATH = PROJECT_ROOT / "user_data"
CONFIG_PATH = PROJECT_ROOT / "core" / "config" / "config.json"
MODELS_PATH = PROJECT_ROOT / "models"
PYTHON_PATH = PROJECT_ROOT / "python"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if __name__ == "__main__":
    print(PROJECT_ROOT)
