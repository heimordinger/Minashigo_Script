"""设置页可清理的运行/调试缓存（不影响脚本与素材本体）。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.path import IMG_PATH, PROJECT_ROOT, USER_DATA_PATH

SCREENSHOTS = PROJECT_ROOT / "screenshots"
PSEUDO_RECORD_DIR = SCREENSHOTS / "pseudo_record"
DAILY_STOP_DIR = SCREENSHOTS / "daily_stop"
CLICK_PROBE_DIR = SCREENSHOTS / "click_probe"
NETWORK_DIR = SCREENSHOTS / "network"
HOTSPOT_PATH = USER_DATA_PATH / "match_hotspots.json"


@dataclass
class ClearItemResult:
    key: str
    label: str
    ok: bool
    detail: str
    bytes_freed: int = 0


def path_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += int(p.stat().st_size)
                except OSError:
                    continue
    except OSError:
        return total
    return total


def format_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            if u == "B":
                return f"{int(x)} {u}"
            return f"{x:.1f} {u}"
        x /= 1024.0
    return f"{n} B"


def _rm_tree(path: Path) -> tuple[int, str]:
    if not path.exists():
        return 0, "不存在"
    freed = path_size_bytes(path)
    try:
        if path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path, ignore_errors=False)
        return freed, "已删除"
    except Exception as e:
        return 0, f"失败: {e}"


def clear_match_runtime() -> ClearItemResult:
    """尺度定标 + hotspot 落盘 + Matcher 模板/ORB 内存缓存。"""
    notes: list[str] = []
    freed = 0
    try:
        from backend.matcher.scale_calibrate import get_scale_cache

        get_scale_cache().clear()
        notes.append("尺度定标")
    except Exception as e:
        notes.append(f"尺度定标失败({e})")

    try:
        from backend.matcher.hotspot_roi import get_hotspot_store

        store = get_hotspot_store()
        store.clear_all()
        notes.append("匹配热点")
        if HOTSPOT_PATH.is_file():
            freed += path_size_bytes(HOTSPOT_PATH)
    except Exception as e:
        notes.append(f"热点失败({e})")

    try:
        from backend.matcher.matcher import matcher

        n_t = len(getattr(matcher, "_template_cache", {}) or {})
        n_o = len(getattr(matcher, "_orb_cache", {}) or {})
        matcher._template_cache = {}
        matcher._orb_cache = {}
        notes.append(f"模板缓存({n_t})/ORB({n_o})")
    except Exception as e:
        notes.append(f"模板缓存失败({e})")

    try:
        from backend.matcher.match_ref import _load_ref_file

        _load_ref_file.cache_clear()
    except Exception:
        pass

    ok = not any("失败" in n for n in notes)
    return ClearItemResult(
        key="match",
        label="匹配缓存",
        ok=ok,
        detail="、".join(notes),
        bytes_freed=freed,
    )


def clear_dir_item(key: str, label: str, path: Path) -> ClearItemResult:
    freed, detail = _rm_tree(path)
    try:
        shown = path.relative_to(PROJECT_ROOT)
    except ValueError:
        shown = path
    return ClearItemResult(
        key=key,
        label=label,
        ok="失败" not in detail,
        detail=f"{shown}: {detail}",
        bytes_freed=freed,
    )


def clear_vision_caches_under_assets() -> ClearItemResult:
    """清除 assets/images 下各包的 .vision_cache（不删识图目录.txt / 脚本介绍）。"""
    root = Path(IMG_PATH)
    if not root.is_dir():
        return ClearItemResult("vision", "素材识图缓存", True, "素材目录不存在", 0)
    freed = 0
    n = 0
    errors: list[str] = []
    for cache_dir in root.rglob(".vision_cache"):
        if not cache_dir.is_dir():
            continue
        f, detail = _rm_tree(cache_dir)
        if "失败" in detail:
            errors.append(str(cache_dir))
        else:
            freed += f
            n += 1
    if errors:
        return ClearItemResult(
            "vision",
            "素材识图缓存",
            False,
            f"已清 {n} 处；失败 {len(errors)}",
            freed,
        )
    return ClearItemResult(
        "vision",
        "素材识图缓存",
        True,
        f"已清除 {n} 个 .vision_cache",
        freed,
    )


CLEAR_OPTIONS: list[tuple[str, str, str, Callable[[], ClearItemResult]]] = [
    (
        "match",
        "匹配缓存（尺度定标 / 热点 / 模板内存）",
        "下次匹配会重新定标；体积很小，建议默认勾选。",
        clear_match_runtime,
    ),
    (
        "pseudo",
        "伪录制记录",
        "screenshots/pseudo_record；仅影响性能分析/优化回放，不影响跑脚本。",
        lambda: clear_dir_item("pseudo", "伪录制记录", PSEUDO_RECORD_DIR),
    ),
    (
        "daily_stop",
        "任务停帧截图",
        "screenshots/daily_stop；失败/停止时存档，删了不影响运行。",
        lambda: clear_dir_item("daily_stop", "任务停帧截图", DAILY_STOP_DIR),
    ),
    (
        "click_probe",
        "点击探针截图",
        "screenshots/click_probe；调试点击精度用。",
        lambda: clear_dir_item("click_probe", "点击探针截图", CLICK_PROBE_DIR),
    ),
    (
        "network",
        "网络调试截图",
        "screenshots/network；网络相关调试产物。",
        lambda: clear_dir_item("network", "网络调试截图", NETWORK_DIR),
    ),
    (
        "vision",
        "素材识图缓存（.vision_cache）",
        "生成器用；不删识图目录.txt / 脚本介绍。下次生成可能重新调识图 API。",
        clear_vision_caches_under_assets,
    ),
]


def estimate_sizes() -> dict[str, int]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    jobs = {
        "match": lambda: path_size_bytes(HOTSPOT_PATH),
        "pseudo": lambda: path_size_bytes(PSEUDO_RECORD_DIR),
        "daily_stop": lambda: path_size_bytes(DAILY_STOP_DIR),
        "click_probe": lambda: path_size_bytes(CLICK_PROBE_DIR),
        "network": lambda: path_size_bytes(NETWORK_DIR),
        "vision": lambda: (
            sum(
                path_size_bytes(p)
                for p in Path(IMG_PATH).rglob(".vision_cache")
                if p.is_dir()
            )
            if Path(IMG_PATH).is_dir()
            else 0
        ),
    }
    out: dict[str, int] = {k: 0 for k in jobs}
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
        futs = {pool.submit(fn): key for key, fn in jobs.items()}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                out[key] = int(fut.result() or 0)
            except Exception:
                out[key] = 0
    return out


def run_clear(selected_keys: list[str]) -> list[ClearItemResult]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    keyset = set(selected_keys)
    jobs = [(key, fn) for key, _label, _tip, fn in CLEAR_OPTIONS if key in keyset]
    if not jobs:
        return []
    results: dict[str, ClearItemResult] = {}
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
        futs = {pool.submit(fn): key for key, fn in jobs}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = ClearItemResult(
                    key=key, label=key, ok=False, detail=f"失败: {e}", bytes_freed=0
                )
    # 保持 CLEAR_OPTIONS 顺序
    order = [key for key, _l, _t, _f in CLEAR_OPTIONS if key in results]
    return [results[k] for k in order]
