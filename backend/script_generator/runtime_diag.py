"""通用运行时诊断：点击目标窗、日志裁剪、消息点击探针。

面向「识图成功但界面无反应」等同类问题（不限于 MuMu）：
外壳窗能截图、渲染/子窗才吃消息点击。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.automation.emulator_target import (
    EMULATOR_RULES,
    _candidate_targets,
    _match_rule,
    _resolve_shell_for,
    _shot_size,
    resolve_automation_target,
)
from backend.automation.win32_target import Win32Target

# 注入协作 prompt / 工具结果时优先保留的日志关键词（宽泛）
LOG_KEEP_KEYS = (
    "match",
    "click",
    "点击",
    "hwnd",
    "渲染",
    "窗口",
    "window",
    "emulator",
    "改用",
    "重定向",
    "失败",
    "超时",
    "error",
    "fail",
    "score",
    "空点",
    "frame",
    "截图",
    "vision",
    "绑定",
)


@dataclass
class WindowRoleInfo:
    hwnd: int
    title: str
    class_name: str
    role: str  # shell | render | mirror | unknown
    emulator: str = ""
    client_wh: tuple[int, int] = (0, 0)
    is_child_of_shell: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class_name": self.class_name,
            "role": self.role,
            "emulator": self.emulator,
            "client_wh": list(self.client_wh),
            "is_child_of_shell": self.is_child_of_shell,
            "notes": list(self.notes),
        }


def classify_window_role(
    title: str,
    class_name: str,
    *,
    emulator: str = "",
) -> tuple[str, list[str]]:
    """返回 (role, notes)。role: shell|render|mirror|unknown"""
    t = title or ""
    c = class_name or ""
    notes: list[str] = []
    if "ToolSaveBits" in c:
        return "mirror", ["能截图、消息点击常无效的镜像/工具层，勿作点击目标"]

    rule = _match_rule(t, c)
    emu = emulator or (rule.name if rule else "")
    if rule:
        if rule.is_render(t, c) and "ToolSaveBits" not in c:
            return "render", [f"按 {emu} 规则识别为渲染/操作层"]
        if rule.is_shell(t, c):
            # 部分标题既像壳又像设备
            if "NxDevice" in t or "nemuwin" in c.lower():
                return "render", [f"{emu}：设备/渲染子层"]
            return "shell", [f"按 {emu} 规则识别为外壳（可截图，点击可能无效）"]

    # 通用启发式（非已知模拟器也尽量分类）
    low_c = c.lower()
    if any(k in low_c for k in ("render", "subwin", "opengl", "d3d", "vulkan", "gl")):
        return "render", ["类名含渲染相关关键字"]
    if any(k in t.lower() for k in ("render", "game", "unity", "unreal")):
        return "render", ["标题含渲染/游戏相关关键字"]
    if any(k in low_c for k in ("qt", "chrome", "cef", "electron")):
        notes.append("可能是外壳/浏览器壳，点击需确认是否有独立渲染子窗")
        return "shell", notes
    return "unknown", notes or ["未能归类；可用 message_click_probe 验证是否吃点击"]


def inspect_click_target(hwnd: int) -> dict[str, Any]:
    """检查绑定 hwnd：角色、推荐目标、同进程候选。"""
    try:
        base = Win32Target.from_hwnd(int(hwnd))
    except Exception as e:
        return {"ok": False, "error": f"无效 hwnd: {e}"}

    title = str(base.title or "")
    cls = str(base.class_name or "")
    rule = _match_rule(title, cls)
    emu = rule.name if rule else ""
    role, notes = classify_window_role(title, cls, emulator=emu)
    cw, ch = _shot_size(base)

    resolved = resolve_automation_target(int(hwnd))
    resolved_changed = int(resolved.hwnd) != int(base.hwnd)
    r_role, r_notes = classify_window_role(
        str(resolved.title or ""),
        str(resolved.class_name or ""),
        emulator=emu,
    )

    shell = _resolve_shell_for(base, rule) if rule else base
    candidates: list[dict[str, Any]] = []
    if rule:
        for tgt, is_child in _candidate_targets(shell)[:24]:
            tw, th = _shot_size(tgt)
            if tw < 40 or th < 40:
                continue
            rr, rn = classify_window_role(
                str(tgt.title or ""), str(tgt.class_name or ""), emulator=emu
            )
            candidates.append(
                {
                    "hwnd": int(tgt.hwnd),
                    "title": str(tgt.title or ""),
                    "class_name": str(tgt.class_name or ""),
                    "role": rr,
                    "client_wh": [tw, th],
                    "is_child_of_shell": bool(is_child),
                    "notes": rn[:2],
                    "recommended": int(tgt.hwnd) == int(resolved.hwnd),
                }
            )
    else:
        # 未知应用：同进程大客户区窗
        try:
            pid = int(base.pid)
        except Exception:
            pid = 0
        if pid:
            for w in Win32Target._enum_all():
                try:
                    tgt = Win32Target(int(w["hwnd"]))
                    if int(tgt.pid) != pid:
                        continue
                    tw, th = _shot_size(tgt)
                    if tw < 120 or th < 120:
                        continue
                    rr, rn = classify_window_role(
                        str(tgt.title or ""), str(tgt.class_name or "")
                    )
                    candidates.append(
                        {
                            "hwnd": int(tgt.hwnd),
                            "title": str(tgt.title or ""),
                            "class_name": str(tgt.class_name or ""),
                            "role": rr,
                            "client_wh": [tw, th],
                            "is_child_of_shell": False,
                            "notes": rn[:2],
                            "recommended": int(tgt.hwnd) == int(resolved.hwnd),
                        }
                    )
                except Exception:
                    continue
            candidates = candidates[:20]

    symptom = ""
    if role in ("shell", "mirror") and resolved_changed:
        symptom = (
            "当前绑定更像外壳/镜像层；系统已能解析到另一渲染窗。"
            "若出现「识图成功但点不动」，应对账号改绑到推荐 hwnd。"
        )
    elif role in ("shell", "mirror") and not resolved_changed:
        symptom = (
            "当前绑定像外壳/镜像，且未能自动解析到更好的渲染窗。"
            "可对候选做 message_click_probe，或换绑同进程其它大客户区窗。"
        )
    elif role == "render":
        symptom = "当前目标已像渲染/操作层；若仍点不动，查坐标/DPI/是否点到遮罩层。"

    return {
        "ok": True,
        "tool": "inspect_click_target",
        "bound": WindowRoleInfo(
            hwnd=int(base.hwnd),
            title=title,
            class_name=cls,
            role=role,
            emulator=emu,
            client_wh=(cw, ch),
            notes=notes,
        ).to_dict(),
        "resolved": WindowRoleInfo(
            hwnd=int(resolved.hwnd),
            title=str(resolved.title or ""),
            class_name=str(resolved.class_name or ""),
            role=r_role,
            emulator=emu,
            client_wh=_shot_size(resolved),
            notes=r_notes,
        ).to_dict(),
        "resolved_differs": resolved_changed,
        "recommend_hwnd": int(resolved.hwnd),
        "candidates": candidates,
        "symptom_hint": symptom,
        "human_summary": _human_inspect_summary(
            role, emu, int(base.hwnd), int(resolved.hwnd), resolved_changed, symptom
        ),
    }


def _human_inspect_summary(
    role: str,
    emu: str,
    bound: int,
    resolved: int,
    differs: bool,
    symptom: str,
) -> str:
    bits = [f"绑定 hwnd={bound}（角色 {role}" + (f" / {emu}" if emu else "") + "）"]
    if differs:
        bits.append(f"推荐点击目标 hwnd={resolved}")
    else:
        bits.append(f"解析后仍为 hwnd={resolved}")
    if symptom:
        bits.append(symptom)
    return "；".join(bits)


def trim_runtime_logs(
    lines: list[str],
    *,
    limit: int = 40,
    prefer_keys: tuple[str, ...] = LOG_KEEP_KEYS,
) -> list[str]:
    """裁剪日志：优先保留命中关键字的行，再补最近若干行。"""
    raw = [str(x).rstrip() for x in (lines or []) if str(x).strip()]
    if not raw:
        return []
    keyed = [ln for ln in raw if any(k.lower() in ln.lower() for k in prefer_keys)]
    if len(keyed) >= limit:
        return keyed[-limit:]
    # 用最近行补足
    need = limit - len(keyed)
    recent = raw[-max(need, limit // 2) :]
    seen = set()
    out: list[str] = []
    for ln in keyed + recent:
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return out[-limit:]


def message_click_probe(
    hwnd: int,
    *,
    xy: Optional[tuple[int, int]] = None,
    settle_s: float = 0.45,
) -> dict[str, Any]:
    """对目标窗发一次客户区消息点击，用前后截图像素差判断是否「吃点击」。

    宽泛适用于任何 Win32 目标，不假设 MuMu。
    """
    try:
        tgt = Win32Target.from_hwnd(int(hwnd))
    except Exception as e:
        return {"ok": False, "error": f"无效 hwnd: {e}"}

    before = tgt.screenshot(client_only=True, method="auto")
    if before is None or getattr(before, "size", 0) == 0:
        return {"ok": False, "error": "无法截取客户区（窗口可能最小化或不可见）"}

    h, w = before.shape[:2]
    if xy is None:
        x, y = w // 2, h // 2
    else:
        x, y = int(xy[0]), int(xy[1])
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))

    try:
        # 后台消息点击（与脚本路径一致）
        tgt.click(x, y)
    except Exception as e:
        return {"ok": False, "error": f"点击失败: {e}"}

    time.sleep(max(0.15, float(settle_s)))
    after = tgt.screenshot(client_only=True, method="auto")
    if after is None or getattr(after, "size", 0) == 0:
        return {
            "ok": True,
            "hwnd": int(hwnd),
            "xy": [x, y],
            "changed": None,
            "diff_ratio": None,
            "human_summary": "已点击但无法再次截图对比",
        }

    diff_ratio = _frame_diff_ratio(before, after)
    changed = diff_ratio is not None and diff_ratio >= 0.004
    role, _ = classify_window_role(str(tgt.title or ""), str(tgt.class_name or ""))
    summary = (
        f"hwnd={hwnd} @({x},{y}) 像素变化约 {diff_ratio:.3%}"
        if diff_ratio is not None
        else f"hwnd={hwnd} @({x},{y}) 无法算差分"
    )
    if changed:
        summary += " → 该窗很可能吃消息点击"
    else:
        summary += " → 几乎无画面变化（可能点到外壳/镜像，或点在无反馈区域）"
    return {
        "ok": True,
        "tool": "message_click_probe",
        "hwnd": int(hwnd),
        "title": str(tgt.title or ""),
        "class_name": str(tgt.class_name or ""),
        "role": role,
        "xy": [x, y],
        "diff_ratio": diff_ratio,
        "changed": changed,
        "human_summary": summary,
    }


def _frame_diff_ratio(a, b) -> Optional[float]:
    import cv2
    import numpy as np

    try:
        if a.shape != b.shape:
            b = cv2.resize(b, (a.shape[1], a.shape[0]))
        if a.ndim == 3:
            ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
            gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        else:
            ga, gb = a, b
        diff = cv2.absdiff(ga, gb)
        return float(np.count_nonzero(diff > 12)) / float(diff.size)
    except Exception:
        return None
