"""协作临时探针脚本：授权后尽量放开，便于探索与复用。

默认能力偏宽（可读图、调 Matcher、import）；写盘 / 点窗等升权由 GUI 开关控制。
"""

from __future__ import annotations

import io
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT

PROBE_OUT_ROOT = PROJECT_ROOT / "screenshots" / "collab_probe"


def default_permissions() -> dict[str, bool]:
    # 基线：临时脚本开；探针产物写盘也开——探针脚本本身已能跑任意 Python，
    # 而 save_image 只能写 screenshots/collab_probe/<会话>/（文件名做了净化），
    # 卡在这里只会让模型"嘴上裁剪"却不产出。控窗仍然默认关。
    return {
        "probe_script": True,
        "write_files": True,
        "runtime_control": False,
    }


PERM_LABELS = {
    "probe_script": "临时脚本",
    "write_files": "写文件",
    "runtime_control": "控制窗口",
}

PERM_REASONS = {
    "probe_script": "执行临时 Python 探针（读画面、Matcher、探索逻辑）。",
    "write_files": "把探针/裁剪产物写入协作输出目录（只写应用自己的目录）。",
    "runtime_control": "消息点击探针或改绑账号窗口。",
}


def normalize_permissions(raw: Optional[dict]) -> dict[str, bool]:
    base = default_permissions()
    if not isinstance(raw, dict):
        return base
    for k in base:
        if k in raw:
            base[k] = bool(raw[k])
    return base


def _path_is_file(p) -> bool:
    try:
        return Path(p).is_file()
    except Exception:
        return False


def _result_to_dict(res: Any) -> dict[str, Any]:
    if res is None:
        return {"ok": False, "matched": False, "score": 0.0}
    if hasattr(res, "to_dict"):
        d = dict(res.to_dict())
        d["matched"] = bool(res)
        d["ok"] = True
        return d
    if isinstance(res, dict):
        return res
    if isinstance(res, (list, tuple)) and len(res) >= 3:
        x, y, score = res[0], res[1], res[2]
        return {
            "ok": True,
            "x": x,
            "y": y,
            "score": float(score or 0),
            "matched": x is not None and y is not None,
        }
    return {"ok": True, "value": repr(res)}


def _scale_compare(frame: Path, tmpl: Path, *, matched: bool) -> dict[str, Any]:
    """尺度对照：hint 处多少分、最佳尺度在哪、多少分。

    只测量、不下结论；命中时也带上（方便回答"这次到底用的什么尺度"），
    没命中时才附上"hint 可能过时"的解读。
    """
    try:
        from backend.script_generator.collaborator.scale_ops import (
            layout_hint,
            scale_probe,
        )

        out: dict[str, Any] = {"scale": 1.0, "scale_hint": layout_hint()}
        probe = scale_probe(frame, tmpl, steps=12)
        if probe.get("ok"):
            out["best_scale"] = probe.get("best_scale")
            out["peak_at_hint"] = probe.get("peak_at_hint")
            out["peak_at_scale"] = probe.get("peak_at_scale")
            if not matched and probe.get("hint_looks_stale"):
                out["scale_advice"] = probe.get("advice")
        return out
    except Exception:
        return {}


def match_template_on_frame(
    *,
    frame_path: Path,
    template_path: str | Path,
    threshold: float = 0.9,
    **kwargs,
) -> dict[str, Any]:
    """结构化模板匹配（不需要临时脚本权限）。

    返回里带上尺度对照：``scale_hint``（配置）、``peak_at_hint`` / ``peak_at_scale``。
    "没命中但分数贴着阈值"和"hint 处峰值远低于最佳尺度"都从这里能看出来——
    否则模型只能看到"没命中"，很容易误判成"画面里没有"。
    """
    from backend.matcher.matcher import Matcher

    frame = Path(frame_path)
    tmpl = Path(template_path)
    if not frame.is_file():
        return {"ok": False, "error": f"画面不存在：{frame}"}
    if not tmpl.is_file():
        # 允许相对 assets/
        cand = PROJECT_ROOT / "assets" / str(template_path)
        if cand.is_file():
            tmpl = cand
        else:
            return {"ok": False, "error": f"模板不存在：{template_path}"}
    try:
        matcher = Matcher()
        res = matcher.match(
            str(frame),
            template=str(tmpl),
            threshold=float(threshold),
            **kwargs,
        )
        out = _result_to_dict(res)
        out["template"] = str(tmpl)
        out["frame"] = str(frame)
        out["threshold"] = float(threshold)
        out.update(_scale_compare(frame, tmpl, matched=bool(out.get("matched"))))
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)}


def template_diagnostics(
    frame_path: str | Path,
    template_path: str | Path,
    *,
    threshold: float = 0.9,
) -> dict[str, Any]:
    """模板信息量 + 可区分性诊断（纯像素统计，不依赖视觉模型）。

    起因：实况会话里「空格子」是一块平整的米色补丁，它在整张结界截图上
    命中 42 处、分数 0.9775~0.99998。旧闸只看"命中数顶格 or 分数极差 < 0.02"，
    实测极差 0.022466 恰好擦边漏过 → 模型拿着 42 个自信的假阳性没法收场。

    这里换三个与"分数极差"无关的判据：

    1. ``gray_std`` / ``edge_density``：模板自身有没有信息量。纯色补丁的
       灰度标准差接近 0、边缘密度接近 0，是"在哪都能匹配"的根因。
    2. ``peak_margin``：响应图峰值与 P90 的差。好模板是尖峰（背景低），
       平坦模板则整张图都高分（峰值和 P90 贴在一起）。
    3. ``lattice``：命中点是否落在规则网格上（x/y 步距方差极小）。
       整屏平铺命中几乎必然呈点阵，和"真有 N 个图标"是两回事。
    """
    try:
        import cv2
        import numpy as np
    except Exception:  # pragma: no cover - 环境缺 cv2 时不拦截
        return {}
    from backend.script_generator.vision_tools import _imread

    diag: dict[str, Any] = {}
    try:
        tmpl = _imread(Path(template_path))
        frame = _imread(Path(frame_path))
    except Exception:
        return {}
    if tmpl is None or frame is None or tmpl.size == 0 or frame.size == 0:
        return {}
    th, tw = tmpl.shape[:2]
    fh, fw = frame.shape[:2]
    if th > fh or tw > fw:
        return {}
    try:
        tmpl_gray = cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        diag["gray_std"] = round(float(tmpl_gray.std()), 4)
        gx = cv2.Sobel(tmpl_gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(tmpl_gray, cv2.CV_32F, 0, 1, ksize=3)
        mag = cv2.magnitude(gx, gy)
        diag["edge_density"] = round(float((mag > 20).mean()), 4)
        diag["template_size"] = [int(tw), int(th)]

        resp = cv2.matchTemplate(frame_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED)
        if resp.size:
            top = float(resp.max())
            p90 = float(np.percentile(resp, 90))
            diag["response_top"] = round(top, 6)
            diag["response_p90"] = round(p90, 6)
            diag["peak_margin"] = round(top - p90, 6)
    except Exception:
        return diag or {}
    return diag


# 模板"缺信息量"的阈值：灰度标准差 / 峰值比 P90 高出的余量
_FLAT_STD_MAX = 6.0
_FLAT_EDGE_MIN = 0.02
_PEAK_MARGIN_MIN = 0.05
# 只有模板确实"匹配上了"（峰值够高）时，峰值不尖才说明它区分不出目标。
# 峰值本身很低 = 什么都没匹配上，那是"模板有区分度"，不能用这条判它不可信。
_PEAK_RELEVANT_TOP = 0.5


def template_low_information(diag: dict[str, Any]) -> bool:
    """诊断结论：模板信息量/可区分性不足 → 命中数不可信。"""
    if not isinstance(diag, dict) or not diag:
        return False
    std = diag.get("gray_std")
    if isinstance(std, (int, float)) and std < _FLAT_STD_MAX:
        return True
    top = diag.get("response_top")
    margin = diag.get("peak_margin")
    if (
        isinstance(top, (int, float))
        and top >= _PEAK_RELEVANT_TOP
        and isinstance(margin, (int, float))
        and margin < _PEAK_MARGIN_MIN
    ):
        return True
    return False


def _flat_template_reasons(diag: dict[str, Any]) -> list[str]:
    """由诊断结果给出"这个模板不可信"的人话理由。"""
    reasons: list[str] = []
    if not isinstance(diag, dict) or not diag:
        return reasons
    std = diag.get("gray_std")
    edge = diag.get("edge_density")
    top = diag.get("response_top")
    margin = diag.get("peak_margin")
    if isinstance(std, (int, float)) and std < _FLAT_STD_MAX:
        reasons.append(
            f"模板几乎是一块纯色（灰度标准差 {std:.1f}），缺纹理/边框，"
            "在平坦背景上也处处高分——这类命中不能当目标数。"
        )
    elif isinstance(edge, (int, float)) and edge < _FLAT_EDGE_MIN:
        reasons.append(
            f"模板边缘信息极少（边缘密度 {edge:.3f}），基本是色块，先换带轮廓的模板。"
        )
    if (
        isinstance(top, (int, float))
        and top >= _PEAK_RELEVANT_TOP
        and isinstance(margin, (int, float))
        and margin < _PEAK_MARGIN_MIN
    ):
        reasons.append(
            f"响应图峰值（{top:.3f}）只比 P90 高 {margin:.3f}：峰值不是尖峰、"
            "整张图都高分，说明这个模板区分不出目标与背景。"
        )
    return reasons


def match_template_multi_on_frame(
    *,
    frame_path: Path,
    template_path: str | Path,
    threshold: float = 0.9,
    max_count: int = 50,
    min_distance: int = 20,
    crop_top_left: Optional[tuple[int, int]] = None,
    crop_bottom_right: Optional[tuple[int, int]] = None,
    scale_hint: Optional[float] = None,
    scales: Optional[list] = None,
    use_color_check: bool = False,
    color_tol: float = 30.0,
    expected_count: int = 0,
) -> dict[str, Any]:
    """同一模板在目标图中找多个实例，返回去重后的坐标、框、分数和尺度。

    ``expected_count`` 已知时传进来：命中点阵与它不符会一并提示。
    """
    from backend.matcher.matcher import Matcher

    frame = Path(frame_path)
    tmpl = Path(template_path)
    if not frame.is_file():
        return {"ok": False, "error": f"画面不存在：{frame}"}
    if not tmpl.is_file():
        cand = PROJECT_ROOT / "assets" / str(template_path)
        if cand.is_file():
            tmpl = cand
        else:
            return {"ok": False, "error": f"模板不存在：{template_path}"}
    try:
        limit = max(1, min(200, int(max_count or 50)))
        distance = max(1, int(min_distance or 20))
        expected = max(0, int(expected_count or 0))
        matcher = Matcher()
        raw = matcher.match(
            str(frame),
            template=str(tmpl),
            threshold=float(threshold),
            match_type="image_multi",
            max_count=limit,
            min_dist=distance,
            crop_top_left=crop_top_left,
            crop_bottom_right=crop_bottom_right,
            scale_hint=scale_hint,
            scales=scales,
            use_color_check=bool(use_color_check),
            color_tol=float(color_tol),
        )
        matches = []
        for index, item in enumerate(raw or [], 1):
            x, y = int(item.get("x") or 0), int(item.get("y") or 0)
            width = max(1, int(item.get("width") or 1))
            height = max(1, int(item.get("height") or 1))
            matches.append(
                {
                    "index": index,
                    "x": x,
                    "y": y,
                    "score": round(float(item.get("score") or 0.0), 6),
                    "scale": round(float(item.get("scale") or 1.0), 6),
                    "box": {
                        "x": int(round(x - width / 2)),
                        "y": int(round(y - height / 2)),
                        "w": width,
                        "h": height,
                    },
                }
            )
        scores = [m["score"] for m in matches]
        saturated = len(matches) >= limit
        # 更本质的判据：模板信息量 / 峰值可区分性。
        diag = template_diagnostics(frame, tmpl, threshold=float(threshold))
        score_spread = (max(scores) - min(scores)) if scores else 0.0
        reasons: list[str] = []
        if saturated:
            reasons.append(
                f"命中数量达到上限（{limit}）；先确认模板信息量，再决定是不是真有这么多目标。"
            )
        reasons.extend(_flat_template_reasons(diag))
        # ⚠️ 不按"分数极差"或"命中点阵"判 ambiguous：
        # 本项目界面本来就是规则网格、图标彼此近乎相同。实况「空格子」在结界1上
        # 命中 42 个（3 行卡 × 5 槽 = 45，减去 3 枚勋章）**是正确的**，而它的
        # 分数极差只有 0.022、命中点又整整齐齐 —— 这两条都会把正确结果误报成
        # "模板太泛"。真正说明模板不可信的只有"模板本身没信息量/峰值不突出"。
        diag_bad = template_low_information(diag)
        ambiguous = bool(saturated or diag_bad)
        result: dict[str, Any] = {
            "ok": True,
            "matched": bool(matches),
            "count": len(matches),
            "matches": matches,
            "threshold": float(threshold),
            "max_count": limit,
            "min_distance": distance,
            "saturated": saturated,
            "ambiguous": ambiguous,
            "warning": (
                " ".join(reasons)
                or (
                    "命中数量达到上限，或模板自身信息量不足（近纯色 / 峰值不突出）；"
                    "请提高阈值、增大 min_distance、限定 search_region，"
                    "或更换包含边缘/纹理的模板。"
                )
            ) if ambiguous else "",
            "template": str(tmpl),
            "frame": str(frame),
        }
        if diag:
            diag["score_spread"] = round(score_spread, 6)
            result["diagnostics"] = diag
        if reasons:
            result["reasons"] = reasons
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _build_namespace(
    session,
    permissions: dict[str, bool],
) -> tuple[dict[str, Any], Path]:
    import cv2
    import numpy as np

    from backend.matcher.matcher import Matcher

    frame_path = Path(session.frame_path) if session.frame_path else None
    frame_bgr = None
    if frame_path and frame_path.is_file():
        # 中文路径：cv2.imread 在 Windows 会按 ANSI 解析，读不到（帧名常带中文，
        # 例如 window_光物-圣诞水剑_xxx.png），这里必须走 np.fromfile + imdecode。
        try:
            data = np.fromfile(str(frame_path), dtype=np.uint8)
            frame_bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception:
            frame_bgr = None

    sid = str(getattr(session, "session_id", "") or "probe")
    out_dir = PROBE_OUT_ROOT / sid
    matcher = Matcher()
    printed: list[str] = []

    def _print(*args, **kwargs):
        buf = io.StringIO()
        kwargs.setdefault("file", buf)
        print(*args, **kwargs)
        printed.append(buf.getvalue())

    def match(template, threshold: float = 0.9, **kw):
        target = frame_bgr if frame_bgr is not None else str(frame_path or "")
        if target is None or target == "":
            raise RuntimeError("当前没有可用画面")
        res = matcher.match(target, template=str(template), threshold=float(threshold), **kw)
        return _result_to_dict(res)

    def list_assets(subdir: str = "images"):
        root = PROJECT_ROOT / "assets" / (subdir or "")
        if not root.is_dir():
            return []
        out = []
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
                out.append(str(p.relative_to(PROJECT_ROOT)).replace("\\", "/"))
        return out[:400]

    def _record_binding_failure(failure) -> None:
        """把绑定失败留痕到会话（结构化，带稳定诊断码），供体检/排障读取。

        只留痕、不抛异常：``resolve_image`` 的契约是"找不到返回 None"。但**不允许**
        再回退到"按文件名猜一张"——那正是设计 §4.2 要删掉的"工具自行猜 token 路径"。
        """
        try:
            if not isinstance(getattr(session, "artifacts", None), dict):
                session.artifacts = {}
            items = session.artifacts.setdefault("resource_binding_failures", [])
            if isinstance(items, list):
                payload = failure.to_dict() if hasattr(failure, "to_dict") else dict(failure)
                if not items or items[-1] != payload:
                    items.append(payload)
                    del items[:-20]
        except Exception:  # noqa: BLE001 - 留痕失败不影响工具行为
            pass

    def resolve_image(name_or_path):
        """把"素材名/文件名/相对路径/绝对路径"解析成真实存在的图片路径。

        模型写工具时经常把 `ARGS["base"]` 传成裸文件名（例如 `"结界1带窗口.png"`），
        `cv2.imread` 拿到相对路径只会返回 None，报出来却是 AttributeError，
        完全看不出是"路径没解析"。这里按**两级**解析：

        1. **资源引用**（``asset_xxx`` / ``[[asset_xxx]]``）→ 统一走
           :class:`ResourceBinder`（设计 §4.2）。绑不上就记结构化失败并返回 None，
           **绝不**再按文件名/stem 去猜一张最像的图（猜错比报错更难查）；
        2. 普通文件名/路径 → 沿用旧的查找顺序（原样 → PROJECT_ROOT → assets →
           会话素材按名字 → 附近目录同名）。

        找不到返回 None，调用方请自行判断并给出人话错误。
        """
        raw = str(name_or_path or "").strip().strip('"').strip("'")
        if not raw:
            return None
        # ① 资源引用（token / asset_xxx 形状）：交给统一 Binder；
        #    这里不再自己判断状态/版本/内容，也**不**回退到"按文件名猜一张"。
        from backend.script_generator.resource_binding import (
            ResourceBinder,
            ResourceBindingError,
            ResourceRequest,
            SessionResourceSource,
            looks_like_resource_ref,
        )

        if looks_like_resource_ref(raw):
            try:
                ResourceBinder.parse_ref(raw, allow_name=False)
            except ResourceBindingError as exc:
                _record_binding_failure(exc.failure)
                return None
            binder = ResourceBinder(SessionResourceSource(session), origin="tool_call")
            resource, failure = binder.try_bind(ResourceRequest(ref=raw, origin="tool_call"))
            if failure is not None:
                _record_binding_failure(failure)
                return None
            path = Path(str(getattr(resource, "path", "") or ""))
            return path if path.is_file() else None
        cands: list[Path] = [Path(raw)]
        if not Path(raw).is_absolute():
            cands.append(PROJECT_ROOT / raw)
            cands.append(PROJECT_ROOT / "assets" / raw)
            cands.append(PROJECT_ROOT / "assets" / "images" / raw)
        for c in cands:
            try:
                if c.is_file():
                    return c
            except Exception:
                continue
        base = Path(raw).name
        raw_stem = Path(raw).stem.lower()
        # 会话素材：asset_id / 交流别名（可能不带扩展名）/ 文件名，先精确再按主名匹配。
        # 实况里素材别名是「结界1带窗口」而磁盘名是 paste_20260922_105428.png，
        # 模型按别名带 .png 来叫（"结界1带窗口.png"），必须能对上。
        assets = [a for a in list(getattr(session, "assets", None) or []) if isinstance(a, dict)]
        for a in assets:
            if str(a.get("id") or "") == raw:
                ap = Path(str(a.get("path") or ""))
                if ap.is_file():
                    return ap
        for a in assets:
            ap = Path(str(a.get("path") or ""))
            if not ap.is_file():
                continue
            for c in (str(a.get("name") or ""), str(a.get("file_name") or ""), ap.name):
                if c and c == raw:
                    return ap
        for a in assets:
            ap = Path(str(a.get("path") or ""))
            if not ap.is_file():
                continue
            for c in (str(a.get("name") or ""), str(a.get("file_name") or ""), ap.name):
                if c and Path(c).stem.lower() == raw_stem:
                    return ap
        for d in (
            PROJECT_ROOT / "assets" / "images",
            PROJECT_ROOT / "assets",
            PROJECT_ROOT / "screenshots" / "collab_snips",
            out_dir,
        ):
            try:
                if not d.is_dir():
                    continue
                for p in d.rglob(base):
                    if p.is_file():
                        return p
                for p in d.rglob(f"{raw_stem}.*"):
                    if p.is_file() and p.stem.lower() == raw_stem:
                        return p
            except Exception:
                continue
        return None

    def read_image(name_or_path):
        """解析路径并读成 BGR ndarray（中文路径安全）。读不到返回 None。"""
        p = resolve_image(name_or_path)
        if p is None:
            return None
        try:
            data = np.fromfile(str(p), dtype=np.uint8)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception:
            return None

    def save_image(name: str, image) -> str:
        if not permissions.get("write_files"):
            raise PermissionError(
                "需要「写文件」权限；界面将弹出授权。"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = Path(str(name or "out.png")).name
        if not Path(safe).suffix:
            safe = f"{safe}.png"
        dest = out_dir / safe
        arr = image
        if isinstance(arr, (str, Path)):
            # 中文路径：cv2.imread 在 Windows 会按 ANSI 解析，读不到
            try:
                data = np.fromfile(str(arr), dtype=np.uint8)
                arr = cv2.imdecode(data, cv2.IMREAD_COLOR)
            except Exception:
                arr = None
        if arr is None:
            raise RuntimeError("save_image 需要 ndarray 或可读路径")
        # 同上：用 imencode + write_bytes，避免中文名被写成乱码文件
        ok, buf = cv2.imencode(Path(str(dest)).suffix or ".png", arr)
        if not ok:
            raise RuntimeError(f"编码失败：{dest}")
        dest.write_bytes(buf.tobytes())
        return str(dest)

    def write_text(name: str, text: str) -> str:
        if not permissions.get("write_files"):
            raise PermissionError(
                "需要「写文件」权限；界面将弹出授权。"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = Path(str(name or "note.txt")).name
        dest = out_dir / safe
        dest.write_text(str(text or ""), encoding="utf-8")
        return str(dest)

    def mark_image(
        marks,
        *,
        asset_id: str = "",
        path: str = "",
        name: str = "标记.png",
        title: str = "",
    ):
        """在图上标记并落盘，返回新图路径。等价于内置的 mark_on_image。

        marks 里每项给 rect=[x0,y0,x1,y1] 或 x/y（+可选 radius/label/color），
        坐标按原图像素理解。
        """
        from backend.script_generator.collaborator.annotation import render_marks
        from backend.script_generator.collaborator.canvas import resolve_template_path

        if not permissions.get("write_files"):
            raise PermissionError("需要「写文件」权限；界面将弹出授权。")
        src_path = None
        if str(asset_id or "").strip() or str(path or "").strip():
            src_path, _asset, err = resolve_template_path(
                session, asset_id=str(asset_id or ""), template_path=str(path or "")
            )
            if err or src_path is None:
                raise RuntimeError(err or "找不到要标记的图")
        elif frame_path is not None:
            src_path = frame_path
        if src_path is None:
            raise RuntimeError("没有可标记的画面，请给 asset_id 或 path")
        try:
            data = np.fromfile(str(src_path), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception:
            img = None
        if img is None:
            raise RuntimeError(f"读不到图片：{src_path}")
        canvas, dropped, _items = render_marks(img, marks, title=str(title or ""))
        saved = save_image(str(name or "标记.png"), canvas)
        return {"path": saved, "dropped": dropped}

    def runtime_call(tool_name: str, **kwargs):
        if not permissions.get("runtime_control"):
            raise PermissionError(
                "需要「控制窗口」权限；界面将弹出授权。"
            )
        from backend.script_generator.collaborator.llm_turn import _run_runtime_tool

        return _run_runtime_tool(str(tool_name), dict(kwargs))

    ns: dict[str, Any] = {
        "__name__": "collab_probe",
        "__builtins__": __builtins__,
        "cv2": cv2,
        "np": np,
        "numpy": np,
        "Path": Path,
        "PROJECT_ROOT": PROJECT_ROOT,
        "ASSETS": PROJECT_ROOT / "assets",
        "OUT_DIR": out_dir,
        "FRAME_PATH": frame_path,
        "frame": frame_bgr,
        "matcher": matcher,
        "match": match,
        "mark_image": mark_image,
        "list_assets": list_assets,
        "resolve_image": resolve_image,
        "read_image": read_image,
        "save_image": save_image,
        "write_text": write_text,
        "runtime_call": runtime_call,
        "print": _print,
        "result": None,
        "ARGS": {},
        "params": {},
        "_printed": printed,
    }
    # 尺度标定能力：hint / 最佳尺度 / 两边峰值，供 Agent 判断"配置过时了没有"
    try:
        from backend.script_generator.collaborator.scale_ops import register_medal_scale

        register_medal_scale(ns)
    except Exception:
        pass
    return ns, out_dir


def run_probe_script(
    code: str,
    *,
    session,
    permissions: Optional[dict] = None,
    timeout_sec: float = 45.0,
    args: Optional[dict] = None,
    script_id: str = "",
) -> dict[str, Any]:
    """执行一段临时探针脚本。返回 ok/result/stdout/error/need_permission。"""
    perms = normalize_permissions(permissions)
    if not perms.get("probe_script"):
        return {
            "ok": False,
            "need_permission": "probe_script",
            "error": "需要「临时脚本」权限；界面将弹出授权。",
        }
    src = (code or "").strip()
    if not src:
        return {"ok": False, "error": "脚本为空"}
    if len(src) > 80_000:
        return {"ok": False, "error": "脚本过长（上限约 80KB）"}

    started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    started_clock = time.perf_counter()
    lifecycle_id = str(script_id or "session_temporary_script").strip()
    if not isinstance(getattr(session, "artifacts", None), dict):
        session.artifacts = {}
    lifecycle_store = session.artifacts.setdefault("temporary_scripts", {})
    if not isinstance(lifecycle_store, dict):
        lifecycle_store = {}
        session.artifacts["temporary_scripts"] = lifecycle_store
    previous = lifecycle_store.get(lifecycle_id, {})
    created_at = str(previous.get("created_at") or "") or started_at
    lifecycle_store[lifecycle_id] = {
        "script_id": lifecycle_id,
        "created_at": created_at,
        "updated_at": started_at,
        "code": src[:4000],
    }
    ns, out_dir = _build_namespace(session, perms)
    if isinstance(args, dict) and args:
        # 自造工具调用：参数以 ARGS/params 暴露给脚本
        ns["ARGS"] = dict(args)
        ns["params"] = dict(args)
    stdout_buf = io.StringIO()

    def _exec():
        with redirect_stdout(stdout_buf):
            exec(compile(src, "<collab_probe>", "exec"), ns, ns)

    import concurrent.futures

    err = None
    need_permission = ""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_exec)
            fut.result(timeout=float(timeout_sec))
    except concurrent.futures.TimeoutError:
        err = f"执行超时（>{timeout_sec}s）"
    except PermissionError as e:
        msg = str(e)
        need_permission = "write_files" if "写文件" in msg else "runtime_control"
        err = msg
    except Exception:
        err = traceback.format_exc(limit=12)

    printed = "".join(ns.get("_printed") or [])
    stdout = (stdout_buf.getvalue() or "") + printed
    result = ns.get("result")
    if result is not None and not isinstance(result, (dict, list, str, int, float, bool)):
        try:
            result = _result_to_dict(result)
        except Exception:
            result = repr(result)

    finished_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    duration_ms = round((time.perf_counter() - started_clock) * 1000, 3)
    # 记入会话，便于复用和溯源
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    session.artifacts["last_probe_script"] = {
        "script_id": lifecycle_id,
        "code": src[:4000],
        "ok": err is None,
        "stdout_tail": (stdout or "")[-2000:],
        "result": result,
        # 失败原因要留档：以前只存 ok=False，出事时盘上看不出为什么失败
        "error": (str(err)[-1200:] if err else ""),
        "out_dir": str(out_dir),
        "created_at": created_at,
        "updated_at": started_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
    }

    if err:
        failed = {
            "ok": False,
            "error": err,
            "stdout": (stdout or "")[-4000:],
            "out_dir": str(out_dir),
            "script_id": lifecycle_id,
            "created_at": created_at,
            "updated_at": started_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
        }
        if need_permission:
            failed["need_permission"] = need_permission
        return failed
    return {
        "ok": True,
        "result": result,
        "stdout": (stdout or "")[-4000:],
        "out_dir": str(out_dir),
        "permissions": perms,
        "has_frame": bool(_path_is_file(getattr(session, "frame_path", None))),
        "script_id": lifecycle_id,
        "created_at": created_at,
        "updated_at": started_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
    }
