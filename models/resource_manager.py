import requests
import zipfile
from pathlib import Path

from models.resource_config import MODELS


def _download_file(url, save_path: Path):
    print(f"[下载] {url}")

    r = requests.get(url, stream=True)
    total = int(r.headers.get("content-length", 0))

    downloaded = 0
    with open(save_path, "wb") as f:
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)

                if total:
                    percent = downloaded / total * 100
                    print(f"\r进度: {percent:.1f}%", end="")

    print("\n下载完成")


def _extract_zip(zip_path: Path, target_dir: Path):
    print(f"[解压] {zip_path}")
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(target_dir)
    zip_path.unlink()
    print("解压完成")


def _ensure_dir(resource):
    path = Path(resource["path"])

    if path.exists():
        print(f"[已存在] {resource['name']}")
        return

    print(f"[缺失] {resource['name']}，开始下载")

    tmp_zip = path.parent / f"{resource['name']}.zip"

    _download_file(resource["url"], tmp_zip)
    _extract_zip(tmp_zip, path.parent)


def ensure_all_models():
    print("=== 检查模型资源 ===")

    for res in MODELS:
        if res["type"] == "dir":
            _ensure_dir(res)

    print("=== 所有资源准备完成 ===")