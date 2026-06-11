import os
import zipfile
from pathlib import Path
from datetime import datetime

# ---------- 配置 ----------

ZIP_LIST = [
    "assets/",
    "backend/",
    "controller/",
    "core/",
    "icon/",
    "gui/",
    "scripts/",
    "taskflow/",
    "main.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "click.html",
    "models/resource_config.py",
    "models/resource_manager.py",
]

# 打包时排除的目录（整个跳过，不遍历）
IGNORE_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    "build",
    "dist",
    ".history",
    ".vscode",
    ".pytest_cache",
}

IGNORE_EXT = {".pyc", ".pyo"}

VERSION = "1.1.0"


# ---------- 核心逻辑 ----------

def should_ignore(path: Path) -> bool:
    if path.suffix in IGNORE_EXT:
        return True
    return False


def collect_files(root: Path, items: list[str]) -> list[Path]:
    """收集所有需要打包的文件，自动跳过 IGNORE_DIRS 中的目录。"""
    files = []

    for item in items:
        path = root / item

        if not path.exists():
            print(f"  [跳过] 不存在: {item}")
            continue

        if path.is_file():
            if not should_ignore(path):
                files.append(path)
            continue

        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]

            dp = Path(dirpath)
            for fn in filenames:
                fp = dp / fn
                if not should_ignore(fp):
                    files.append(fp)

    return files


def main():
    print("=" * 50)
    print("  Minashigo Script 打包工具")
    print("=" * 50)

    root = Path(__file__).resolve().parent
    timestamp = datetime.now().strftime("%Y%m%d")
    output_name = f"Minashigo_Script_v{VERSION}_{timestamp}.zip"
    output_path = root / output_name

    print(f"\n  扫描文件...")
    files = collect_files(root, ZIP_LIST)
    print(f"  共找到 {len(files)} 个文件")

    print(f"\n  正在打包 -> {output_name}")
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, fp in enumerate(files, 1):
            arcname = fp.relative_to(root)
            zf.write(fp, arcname)
            if i % 50 == 0 or i == len(files):
                print(f"    [{i}/{len(files)}] {arcname}")

    size = output_path.stat().st_size
    print(f"\n  ✅ 打包完成")
    print(f"  文件: {output_path}")
    print(f"  大小: {size / 1024 / 1024:.1f} MB")
    print("=" * 50)


if __name__ == "__main__":
    main()
