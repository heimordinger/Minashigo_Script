"""协作临时探针脚本：授权后尽量放开，便于探索与复用。

默认能力偏宽（可读图、调 Matcher、import）；写盘 / 点窗等升权由 GUI 开关控制。
"""

from __future__ import annotations

import io
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Optional

from core.path import PROJECT_ROOT

PROBE_OUT_ROOT = PROJECT_ROOT / "screenshots" / "collab_probe"


def default_permissions() -> dict[str, bool]:
    # 基线：临时脚本默认开；写盘 / 控窗默认关，由 UI 按需授权（本会话）
    return {
        "probe_script": True,
        "write_files": False,
        "runtime_control": False,
    }


PERM_LABELS = {
    "probe_script": "临时脚本",
    "write_files": "写文件",
    "runtime_control": "控制窗口",
}

PERM_REASONS = {
    "probe_script": "执行临时 Python 探针（读画面、Matcher、探索逻辑）。",
    "write_files": "把探针结果写入 screenshots/collab_probe/。",
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


def match_template_on_frame(
    *,
    frame_path: Path,
    template_path: str | Path,
    threshold: float = 0.9,
    **kwargs,
) -> dict[str, Any]:
    """结构化模板匹配（不需要临时脚本权限）。"""
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
        return out
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
        frame_bgr = cv2.imread(str(frame_path))

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
            arr = cv2.imread(str(arr))
        if arr is None:
            raise RuntimeError("save_image 需要 ndarray 或可读路径")
        if not cv2.imwrite(str(dest), arr):
            raise RuntimeError(f"写入失败：{dest}")
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
        "list_assets": list_assets,
        "save_image": save_image,
        "write_text": write_text,
        "runtime_call": runtime_call,
        "print": _print,
        "result": None,
        "_printed": printed,
    }
    return ns, out_dir


def run_probe_script(
    code: str,
    *,
    session,
    permissions: Optional[dict] = None,
    timeout_sec: float = 45.0,
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

    ns, out_dir = _build_namespace(session, perms)
    stdout_buf = io.StringIO()

    def _exec():
        with redirect_stdout(stdout_buf):
            exec(compile(src, "<collab_probe>", "exec"), ns, ns)

    import concurrent.futures

    err = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_exec)
            fut.result(timeout=float(timeout_sec))
    except concurrent.futures.TimeoutError:
        err = f"执行超时（>{timeout_sec}s）"
    except PermissionError as e:
        msg = str(e)
        need = "write_files" if "写文件" in msg else "runtime_control"
        return {"ok": False, "need_permission": need, "error": msg}
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

    # 记入会话，便于复用
    if not isinstance(session.artifacts, dict):
        session.artifacts = {}
    session.artifacts["last_probe_script"] = {
        "code": src[:4000],
        "ok": err is None,
        "stdout_tail": (stdout or "")[-2000:],
        "result": result,
        "out_dir": str(out_dir),
    }

    if err:
        return {
            "ok": False,
            "error": err,
            "stdout": (stdout or "")[-4000:],
            "out_dir": str(out_dir),
        }
    return {
        "ok": True,
        "result": result,
        "stdout": (stdout or "")[-4000:],
        "out_dir": str(out_dir),
        "permissions": perms,
        "has_frame": bool(_path_is_file(getattr(session, "frame_path", None))),
    }
