# -*- coding: utf-8 -*-
"""方案 G：阈值从 1 下降标定（挂修订/优化；默认不启用）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def thresholds_path_for_script(script_path: str | Path) -> Path:
    p = Path(script_path)
    return p.with_suffix(p.suffix + ".thresholds.json") if p.suffix else Path(str(p) + ".thresholds.json")


def load_thresholds(path: str | Path) -> dict[str, float]:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, float] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if k.startswith("_"):
                continue
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def save_thresholds(path: str | Path, table: dict[str, float], *, meta: Optional[dict] = None) -> None:
    p = Path(path)
    payload: dict[str, Any] = {"_meta": meta or {"version": 1}}
    for k, v in sorted(table.items()):
        payload[k] = round(float(v), 4)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def calibrate_image_threshold(
    browser,
    img_path: str | Path,
    *,
    start: float = 1.0,
    step: float = 0.02,
    floor: float = 0.75,
    stable_hits: int = 2,
) -> tuple[float | None, list[str]]:
    """从 start 下降直到稳定命中。返回 (threshold|None, notes)。

    仅应在标定/优化模式调用，不要在正常 do_work 热路径使用。
    """
    notes: list[str] = []
    th = float(start)
    floor = float(floor)
    step = abs(float(step)) or 0.02
    while th + 1e-9 >= floor:
        hits = 0
        for _ in range(max(1, int(stable_hits))):
            if callable(getattr(browser, "invalidate_frame", None)):
                try:
                    browser.invalidate_frame()
                except Exception:
                    pass
            if callable(getattr(browser, "update_frame", None)):
                try:
                    await browser.update_frame()
                except Exception:
                    pass
            m = await browser.match_image(img_path, threshold=th)
            if m:
                hits += 1
            else:
                break
        if hits >= stable_hits:
            notes.append(f"钉死 {img_path} @ {th:.4f}（连续 {hits} 次）")
            return th, notes
        notes.append(f"未命中 @ {th:.4f}")
        th = round(th - step, 4)
    notes.append(f"降至下限 {floor} 仍未稳定命中：{img_path}")
    return None, notes


def should_offer_calibration(feedback: str = "", trial_log: str = "") -> bool:
    text = f"{feedback or ''}\n{trial_log or ''}"
    return bool(
        __import__("re").search(
            r"阈值|点不到|匹配不到|误匹配|match\s*fail|匹配失败|threshold",
            text,
            __import__("re").I,
        )
    )
