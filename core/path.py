# core/path.py
from pathlib import Path
import sys
import os


def get_root() -> Path:
    env = os.getenv("PROJECT_ROOT")
    if env:
        return Path(env)

    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = get_root()
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
