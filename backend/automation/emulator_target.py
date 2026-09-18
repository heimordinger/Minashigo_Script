"""模拟器外壳 hwnd → 可接收消息点击的渲染 hwnd。

常见模拟器是「Qt/外壳窗 + 独立渲染窗」：截图点外壳往往能出图，
但对外壳发 WM_LBUTTON* 游戏无反应。绑定后自动改用渲染窗。

注意 MuMu：同进程可能有多个 MuMuNxDevice；顶层 ToolSaveBits 能截图但
消息点击无效，应优先外壳子窗 nemuwin / 子级 NxDevice。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

from backend.automation.win32_target import Win32Target

_user32 = ctypes.windll.user32
_EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


@dataclass(frozen=True)
class EmulatorRule:
    name: str
    is_shell: Callable[[str, str], bool]
    is_render: Callable[[str, str], bool]
    prefer_title: tuple[str, ...] = ()
    prefer_class: tuple[str, ...] = ()


def _has_any(text: str, keys: tuple[str, ...]) -> bool:
    t = text or ""
    return any(k and k in t for k in keys)


EMULATOR_RULES: tuple[EmulatorRule, ...] = (
    EmulatorRule(
        name="mumu",
        is_shell=lambda t, c: _has_any(t, ("MuMu",))
        or _has_any(c, ("MuMu", "nemu", "Nemu")),
        is_render=lambda t, c: (
            ("NxDevice" in t and "ToolSaveBits" not in c)
            or _has_any(c, ("nemuwin", "MuMuNative"))
        ),
        prefer_title=("nemudisplay", "NxDevice"),
        prefer_class=("nemuwin", "MuMuNativeWindow"),
    ),
    EmulatorRule(
        name="ldplayer",
        is_shell=lambda t, c: _has_any(t, ("雷电", "LDPlayer", "dnplayer", "ChangZhi"))
        or _has_any(c, ("LDPlayer", "dnplayer")),
        is_render=lambda t, c: _has_any(t, ("TheRender", "RenderWindow", "subWin"))
        or _has_any(c, ("RenderWindow", "subWin", "LDPlayerRender")),
        prefer_title=("TheRender", "RenderWindow", "subWin"),
        prefer_class=("RenderWindow", "subWin", "LDPlayerRender"),
    ),
    EmulatorRule(
        name="nox",
        is_shell=lambda t, c: _has_any(t, ("Nox", "夜神", "NoxPlayer"))
        or _has_any(c, ("Nox",)),
        is_render=lambda t, c: _has_any(c, ("subWin",))
        or (_has_any(t, ("Nox",)) and _has_any(c, ("Render", "sub"))),
        prefer_title=("Nox",),
        prefer_class=("subWin",),
    ),
    EmulatorRule(
        name="bluestacks",
        is_shell=lambda t, c: _has_any(t, ("BlueStacks", "蓝叠", "HD-Player"))
        or _has_any(c, ("BlueStacks",)),
        is_render=lambda t, c: _has_any(t, ("_ctl_", "Guest", "Render"))
        or _has_any(c, ("_ctl_", "BlueStacksApp")),
        prefer_title=("_ctl_", "BlueStacks", "Guest"),
        prefer_class=("_ctl_", "BlueStacksApp"),
    ),
    EmulatorRule(
        name="memu",
        is_shell=lambda t, c: _has_any(t, ("MEmu", "逍遥", "Microvirt"))
        or _has_any(c, ("MEmu",)),
        is_render=lambda t, c: _has_any(t, ("Render",))
        or _has_any(c, ("RenderWindow", "subWin")),
        prefer_title=("Render",),
        prefer_class=("RenderWindow", "subWin"),
    ),
)


def resolve_automation_target(hwnd: int) -> Win32Target:
    """绑定窗口时解析真正用于截图/点击的目标。"""
    base = Win32Target.from_hwnd(int(hwnd))
    title = str(base.title or "")
    cls = str(base.class_name or "")

    rule = _match_rule(title, cls)
    if rule is None:
        return base

    # 已是可靠渲染窗（排除 MuMu 顶层 ToolSaveBits 镜像）
    if rule.is_render(title, cls) and "ToolSaveBits" not in cls:
        return base

    shell = _resolve_shell_for(base, rule)
    prefer = _find_render_candidate(shell, rule)
    if prefer is not None and int(prefer.hwnd) != int(base.hwnd):
        print(
            f"[emulator:{rule.name}] 自动化目标 {hwnd}({title!r}/{cls}) → "
            f"{prefer.hwnd}({prefer.title!r}/{prefer.class_name})"
        )
        return prefer
    return base


def _match_rule(title: str, cls: str) -> Optional[EmulatorRule]:
    for rule in EMULATOR_RULES:
        if rule.is_shell(title, cls) or rule.is_render(title, cls):
            return rule
    return None


def _resolve_shell_for(base: Win32Target, rule: EmulatorRule) -> Win32Target:
    """若当前已是渲染/镜像窗，尽量回到同进程外壳再找子渲染窗。"""
    title = str(base.title or "")
    cls = str(base.class_name or "")
    if rule.is_shell(title, cls) and "NxDevice" not in title and "nemuwin" not in cls:
        return base
    try:
        pid = int(base.pid)
    except Exception:
        return base
    for w in Win32Target._enum_all():
        try:
            tgt = Win32Target(int(w["hwnd"]))
            if int(tgt.pid) != pid:
                continue
        except Exception:
            continue
        t = str(tgt.title or "")
        c = str(tgt.class_name or "")
        if rule.is_shell(t, c) and "NxDevice" not in t and "ToolSaveBits" not in c:
            if "安卓设备" in t or "LDPlayer" in t or "Nox" in t or "BlueStacks" in t or "MEmu" in t or "MuMu" in t:
                return tgt
    return base


def _enum_child_hwnds(parent: int) -> list[int]:
    out: list[int] = []

    def cb(h, _lparam):
        out.append(int(h))
        return True

    try:
        _user32.EnumChildWindows(int(parent), _EnumChildProc(cb), 0)
    except Exception:
        return out
    return out


def _candidate_targets(shell: Win32Target) -> list[tuple[Win32Target, bool]]:
    """返回 (target, is_child_of_shell)。优先枚举外壳子窗。"""
    try:
        pid = int(shell.pid)
    except Exception:
        return []

    seen: set[int] = set()
    out: list[tuple[Win32Target, bool]] = []

    def _add(hwnd: int, is_child: bool) -> None:
        hwnd = int(hwnd)
        if hwnd in seen or hwnd <= 0:
            return
        seen.add(hwnd)
        try:
            out.append((Win32Target(hwnd), is_child))
        except Exception:
            return

    for h in _enum_child_hwnds(int(shell.hwnd)):
        try:
            if int(Win32Target(h).pid) != pid:
                continue
        except Exception:
            continue
        _add(h, True)

    for w in Win32Target._enum_all():
        try:
            h = int(w["hwnd"])
            tgt = Win32Target(h)
            if int(tgt.pid) != pid:
                continue
            _add(h, False)
        except Exception:
            continue

    return out


def _shot_size(tgt: Win32Target) -> tuple[int, int]:
    try:
        img = tgt.screenshot(client_only=True, method="auto")
    except Exception:
        return 0, 0
    if img is None or getattr(img, "size", 0) == 0:
        return 0, 0
    h, w = img.shape[:2]
    return int(w), int(h)


def _find_render_candidate(
    shell: Win32Target, rule: EmulatorRule
) -> Optional[Win32Target]:
    shell_w, shell_h = _shot_size(shell)
    cands: list[tuple[tuple, Win32Target]] = []

    for tgt, is_child in _candidate_targets(shell):
        if int(tgt.hwnd) == int(shell.hwnd):
            continue
        title = str(tgt.title or "")
        cls = str(tgt.class_name or "")
        wdt, h = _shot_size(tgt)
        if wdt < 80 or h < 80:
            continue

        # MuMu 顶层 ToolSaveBits 镜像：能截图、点不动 → 强烈降权
        tool_mirror = "ToolSaveBits" in cls
        nemu = "nemuwin" in cls.lower() or "nemudisplay" in title.lower()

        named = (
            rule.is_render(title, cls)
            or _has_any(title, rule.prefer_title)
            or _has_any(cls, rule.prefer_class)
        )
        geometry_ok = False
        if shell_w > 0 and shell_h > 0:
            same_w = abs(wdt - shell_w) <= max(8, shell_w // 50)
            shorter = h <= shell_h and (shell_h - h) <= max(120, shell_h // 3)
            geometry_ok = same_w and shorter and (h * wdt) >= (shell_w * shell_h) * 0.40

        if tool_mirror and not is_child:
            # 仍可作最后兜底，但不与子渲染窗竞争
            if not named and not geometry_ok:
                continue
        elif not named and not geometry_ok:
            continue

        width_pen = abs(wdt - shell_w) if shell_w else 0
        area = -(wdt * h)
        # 优先级：nemuwin > 外壳子窗 > 具名渲染 > 非 ToolSaveBits > 几何
        key = (
            0 if nemu else 1,
            0 if is_child else 1,
            0 if named else 1,
            1 if tool_mirror else 0,
            width_pen,
            area,
        )
        cands.append((key, tgt))

    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]
