import zipfile
from pathlib import Path
from datetime import datetime

zip_list = [
    "assets/",
    "backend/",
    "controller/",
    "core/",
    "icon/",
    "gui/",
    "scripts/",
    "taskflow/",
    "main.py",
    "README.md",
    "LICENSE",
    "click.html",
    "models/resource_config.py",
    "models/resource_manager.py",
    "Minashigo_Script.exe",
]

version = "1.0.0"

IGNORE_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    "build",
    "dist",
}

IGNORE_EXT = {
    ".pyc",
    ".pyo",
}


def should_ignore(path: Path) -> bool:
    """判断是否应该忽略文件"""
    if path.suffix in IGNORE_EXT:
        return True

    for part in path.parts:
        if part in IGNORE_DIRS:
            return True

    return False


def add_to_zip(zipf: zipfile.ZipFile, path: Path, root: Path):
    """递归添加文件（带过滤）"""

    if path.is_dir():
        for file in path.rglob("*"):
            if file.is_file() and not should_ignore(file):
                zipf.write(file, file.relative_to(root))
                print(f"[文件] {file}")
    else:
        if not should_ignore(path):
            zipf.write(path, path.relative_to(root))
            print(f"[文件] {path}")


def main():
    print("=== 开始打包 ===")

    root = Path(__file__).resolve().parent

    timestamp = datetime.now().strftime("%Y%m%d")
    output_name = f"Minashigo_Script_v{version}_{timestamp}.zip"

    print(f"输出文件: {output_name}")

    with zipfile.ZipFile(output_name, "w", zipfile.ZIP_DEFLATED) as zipf:

        for item in zip_list:
            path = root / item

            if not path.exists():
                print(f"[跳过不存在] {path}")
                continue

            add_to_zip(zipf, path, root)

    print("=== 打包完成 ===")
    print(f"输出路径: {root / output_name}")


if __name__ == "__main__":
    main()
