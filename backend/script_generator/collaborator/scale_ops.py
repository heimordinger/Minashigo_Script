"""尺度标定：把「模板尺度不对」从"要人去猜"变成"一次调用就有数"。

背景：``layout.json`` 里的 ``medal_scale_hint`` 是随分辨率/模拟器变的。
真实案例里 hint=0.6 但实际最佳尺度是 0.682——hint 处峰值只有 0.414（远低于
0.75 阈值），于是数章返回 0 命中、看起来像"空板"。原来的
``resolve_medal_scale`` 其实**算对了**（0.682 / 0.844），却被
``_RECALIBRATE_MIN_PEAK = 0.88`` 挡住不用。

这个模块给协作 Agent 用的不是"结论"，而是**可比较的数字**：
hint 处多少分、最佳尺度在哪、多少分。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _imread(path):
    """中文路径安全的读图（Windows 下 cv2.imread 会按 ANSI 解析）。"""
    import cv2
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def layout_hint(default: Optional[float] = None) -> Optional[float]:
    """读 assets/images/yys/结界/layout.json 的 medal_scale_hint。"""
    import json

    from core.path import PROJECT_ROOT

    p = PROJECT_ROOT / "assets" / "images" / "yys" / "结界" / "layout.json"
    if not p.is_file():
        return default
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return float(raw.get("medal_scale_hint"))
    except Exception:
        return default


def scale_probe(
    frame_path: str | Path,
    template_path: str | Path,
    *,
    scale: Optional[float] = None,
    steps: int = 14,
    s_min: float = 0.35,
    s_max: float = 1.80,
) -> dict[str, Any]:
    """比较「配置 hint 的尺度」与「实际最佳尺度」的匹配峰值。

    返回 hint / 最佳尺度 / 两边峰值，以及一句是否该重定标的判断。
    这是给 Agent 判断"配置过时了没有"用的，纯测量、不下结论。
    """
    import numpy as np
    import cv2

    frame_p = Path(str(frame_path))
    tmpl_p = Path(str(template_path))
    if not frame_p.is_file():
        return {"ok": False, "error": f"画面不存在：{frame_p}"}
    if not tmpl_p.is_file():
        from core.path import PROJECT_ROOT

        cand = PROJECT_ROOT / "assets" / str(template_path)
        if cand.is_file():
            tmpl_p = cand
        else:
            return {"ok": False, "error": f"模板不存在：{template_path}"}
    try:
        frame = _imread(frame_p)
        tmpl = _imread(tmpl_p)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读图失败：{e}"}
    if frame is None or tmpl is None:
        return {"ok": False, "error": "读图失败：不是有效图片"}

    def peak_at(s: float) -> float:
        fg = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tg = tmpl if tmpl.ndim == 2 else cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)
        th = max(4, int(round(tg.shape[0] * float(s))))
        tw = max(4, int(round(tg.shape[1] * float(s))))
        if th >= fg.shape[0] or tw >= fg.shape[1]:
            return 0.0
        t = cv2.resize(tg, (tw, th), interpolation=cv2.INTER_AREA)
        return float(cv2.minMaxLoc(cv2.matchTemplate(fg, t, cv2.TM_CCOEFF_NORMED))[1])

    best_s, best_peak = 1.0, -1.0
    for s in np.geomspace(float(s_min), float(s_max), max(6, int(steps))):
        pk = peak_at(float(s))
        if pk > best_peak:
            best_s, best_peak = float(s), pk

    hint = layout_hint()
    out: dict[str, Any] = {
        "ok": True,
        "template": str(tmpl_p),
        "frame": str(frame_p),
        "scale_hint": hint,
        "best_scale": round(best_s, 4),
        "peak_at_scale": round(best_peak, 4),
        "scale": float(scale) if scale else None,
    }
    if scale:
        out["peak_at_call_scale"] = round(peak_at(float(scale)), 4)
    if hint:
        out["peak_at_hint"] = round(peak_at(float(hint)), 4)
        delta = abs(best_s - float(hint)) / float(hint)
        out["hint_delta"] = round(delta, 4)
        out["hint_looks_stale"] = bool(
            delta > 0.15 and best_peak > float(out.get("peak_at_hint") or 0) + 0.08
        )
        if out["hint_looks_stale"]:
            out["advice"] = (
                f"hint {float(hint):.3f} 处峰值 {out['peak_at_hint']:.3f}，"
                f"最佳尺度 {best_s:.3f} 处 {best_peak:.3f}——配置 hint 很可能过时。"
                "先向用户确认是否换过分辨率/模拟器，再决定改 layout.json 还是显式传 scale。"
            )
    return out


def scale_sweep(
    frame_path: str | Path,
    template_path: str | Path,
    *,
    threshold: float = 0.75,
    steps: int = 14,
) -> dict[str, Any]:
    """扫描尺度并报出每个尺度下的命中数，用于看"哪个尺度才数得出来"。"""
    import numpy as np
    import cv2

    frame_p = Path(str(frame_path))
    tmpl_p = Path(str(template_path))
    if not frame_p.is_file():
        return {"ok": False, "error": f"画面不存在：{frame_p}"}
    if not tmpl_p.is_file():
        return {"ok": False, "error": f"模板不存在：{template_path}"}
    try:
        frame = _imread(frame_p)
        tmpl = _imread(tmpl_p)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"读图失败：{e}"}
    if frame is None or tmpl is None:
        return {"ok": False, "error": "读图失败：不是有效图片"}
    fg = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    tg = tmpl if tmpl.ndim == 2 else cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)

    rows: list[dict[str, Any]] = []
    for s in np.geomspace(0.35, 1.80, max(6, int(steps))):
        th = max(4, int(round(tg.shape[0] * float(s))))
        tw = max(4, int(round(tg.shape[1] * float(s))))
        if th >= fg.shape[0] or tw >= fg.shape[1]:
            continue
        res = cv2.matchTemplate(
            fg, cv2.resize(tg, (tw, th), interpolation=cv2.INTER_AREA), cv2.TM_CCOEFF_NORMED
        )
        n = int((res >= float(threshold)).sum())
        rows.append({"scale": round(float(s), 4), "peak": round(float(res.max()), 4), "hits": n})
    return {"ok": True, "threshold": float(threshold), "sweep": rows}


def register_medal_scale(ns: dict[str, Any]) -> None:
    """把尺度能力挂进探针命名空间（探针里可直接调）。"""
    ns["layout_hint"] = layout_hint
    ns["scale_probe"] = scale_probe
    ns["scale_sweep"] = scale_sweep
    try:
        import importlib.util
        import sys

        from core.path import PROJECT_ROOT

        helper = PROJECT_ROOT / "scripts" / "yys" / "结界勋章.py"
        if helper.is_file():
            spec = importlib.util.spec_from_file_location("collab_jiejie_medal", helper)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["collab_jiejie_medal"] = mod
                spec.loader.exec_module(mod)
                ns["jiejie"] = mod
                ns["calibrate_medal_scale"] = mod.calibrate_medal_scale
                ns["resolve_medal_scale"] = mod.resolve_medal_scale
    except Exception:
        # 玩法 helper 不在（或 import 失败）不该挡住探针
        pass
