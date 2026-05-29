import zipfile
import requests
from tqdm import tqdm
import os
from pathlib import Path
from urllib.parse import urlparse

from core.path import PROJECT_ROOT
from models.resource_config import MODELS


def _download_file(url: str, save_path: Path, chunk_size: int = 8192):
    print(f"[下载] {url}")

    tmp_path = save_path.with_suffix(".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    filename = os.path.basename(urlparse(url).path) or save_path.name

    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))

            with open(tmp_path, "wb") as f, tqdm(
                    desc=filename,
                    total=total_size,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024
            ) as bar:

                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        size = f.write(chunk)
                        bar.update(size)

        tmp_path.replace(save_path)
        print(f"下载完成: {save_path}")

    except Exception as e:
        print(f"下载失败: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _extract_zip(zip_path: Path, target_dir: Path):
    print(f"[解压] {zip_path}")

    target_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(target_dir)

    zip_path.unlink()
    print(f"解压完成: {target_dir}")


def ensure_all_models():
    print("=== 检查模型资源 ===")

    models_dir = PROJECT_ROOT / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    for res in MODELS:
        model_name = res["name"]

        model_path = models_dir / model_name

        if model_path.exists() and any(model_path.iterdir()):
            print(f"[已存在] {model_name}")
            continue

        print(f"[缺失] {model_name} -> 下载")

        tmp_zip = models_dir / f"{model_name}.zip"

        _download_file(res["url"], tmp_zip)

        _extract_zip(tmp_zip, model_path)

    print("=== 模型资源准备完成 ===")


if __name__ == "__main__":
    ensure_all_models()
