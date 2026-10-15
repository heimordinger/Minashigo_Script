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
from typing import Any, Callable, Optional

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
    base_rect = rect_tuple(base.rect)
    cands: list[tuple[float, Win32Target]] = []
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
                # 多开：取与 base 重叠最多的那个外壳，别拿第一个碰到的
                cands.append((overlap_ratio(base_rect, rect_tuple(tgt.rect)), tgt))
    if cands:
        cands.sort(key=lambda x: -x[0])
        return cands[0][1]
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
            # 多开同进程：与外壳无几何关系的顶层窗属于别的实例
            if not instance_related(shell.rect, tgt.rect, is_child=False):
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


# ──────────────────────────────────────────────────────────────────────
# 实例分组：多开时把 shell / render / mirror 归到同一个实例
# ──────────────────────────────────────────────────────────────────────

Rect = tuple[int, int, int, int]
MIN_SANE_RECT = 80


def rect_tuple(raw: Any) -> Rect:
    """dict / RECT 风格 → (left, top, right, bottom)。"""
    if raw is None:
        return (0, 0, 0, 0)
    if isinstance(raw, dict):
        return (
            int(raw.get("left") or 0),
            int(raw.get("top") or 0),
            int(raw.get("right") or 0),
            int(raw.get("bottom") or 0),
        )
    try:
        return (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
    except Exception:
        return (0, 0, 0, 0)


def rect_size(rect: Rect) -> tuple[int, int]:
    return (
        max(0, int(rect[2]) - int(rect[0])),
        max(0, int(rect[3]) - int(rect[1])),
    )


def rect_usable(rect: Rect, *, min_size: int = MIN_SANE_RECT) -> bool:
    """矩形是否可用于几何比较（排除最小化后的 -32000 之类）。"""
    w, h = rect_size(rect)
    return (
        w >= min_size
        and h >= min_size
        and int(rect[0]) > -30000
        and int(rect[1]) > -30000
    )


def overlap_ratio(inner: Rect, outer: Rect) -> float:
    """inner 被 outer 覆盖的面积比例（0~1）。"""
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    if iw <= 0 or ih <= 0:
        return 0.0
    x0, y0 = max(ix0, ox0), max(iy0, oy0)
    x1, y1 = min(ix1, ox1), min(iy1, oy1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float((x1 - x0) * (y1 - y0)) / float(iw * ih)


def related_rects(a: Rect, b: Rect, *, min_ratio: float = 0.55) -> bool:
    """两个窗口矩形是否属于同一实例（互相覆盖足够多）。"""
    if not rect_usable(a) or not rect_usable(b):
        return False
    return (
        overlap_ratio(a, b) >= min_ratio
        or overlap_ratio(b, a) >= min_ratio
    )


def instance_related(
    shell_rect: Rect,
    cand_rect: Rect,
    *,
    is_child: bool = False,
    min_ratio: float = 0.55,
) -> bool:
    """候选窗是否属于该外壳所在的实例。

    子窗天然属于该外壳；顶层候选必须与外壳矩形有足够重叠，
    否则视为**另一个实例**（多开同进程时 pid 没有区分力）。
    """
    if is_child:
        return True
    if not rect_usable(shell_rect) or not rect_usable(cand_rect):
        return True  # 几何不可用（最小化等）→ 不武断排除，沿用旧行为
    return related_rects(shell_rect, cand_rect, min_ratio=min_ratio)


def classify_layer_role(
    title: str,
    class_name: str,
    *,
    emulator: str = "",
) -> tuple[str, list[str]]:
    """返回 (role, notes)。role: shell|render|mirror|unknown。"""
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
            if "NxDevice" in t or "nemuwin" in c.lower():
                return "render", [f"{emu}：设备/渲染子层"]
            return "shell", [f"按 {emu} 规则识别为外壳（可截图，点击可能无效）"]

    low_c = c.lower()
    if any(k in low_c for k in ("render", "subwin", "opengl", "d3d", "vulkan", "gl")):
        return "render", ["类名含渲染相关关键字"]
    if any(k in t.lower() for k in ("render", "game", "unity", "unreal")):
        return "render", ["标题含渲染/游戏相关关键字"]
    if any(k in low_c for k in ("qt", "chrome", "cef", "electron")):
        notes.append("可能是外壳/浏览器壳，点击需确认是否有独立渲染子窗")
        return "shell", notes
    return "unknown", notes or ["未能归类；可用 message_click_probe 验证是否吃点击"]


@dataclass(frozen=True)
class LayerInfo:
    """一个模拟器相关窗口层。"""

    hwnd: int
    title: str
    class_name: str
    pid: int
    role: str
    rect: Rect
    client: tuple[int, int]
    root_hwnd: int
    is_child: bool
    emulator: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class_name": self.class_name,
            "pid": self.pid,
            "role": self.role,
            "rect": list(self.rect),
            "client_wh": list(self.client),
            "root_hwnd": self.root_hwnd,
            "is_child_of_shell": self.is_child,
            "emulator": self.emulator,
            "notes": list(self.notes),
        }


def layer_from_target(
    tgt: Win32Target,
    *,
    is_child: bool = False,
    min_client: int = MIN_SANE_RECT,
) -> Optional[LayerInfo]:
    """Win32Target → LayerInfo；太小的辅助窗返回 None。"""
    try:
        cr = tgt.client_rect
        w = int(cr.get("width") or 0)
        h = int(cr.get("height") or 0)
        if w < min_client or h < min_client:
            return None
        title = str(tgt.title or "")
        cls = str(tgt.class_name or "")
        rule = _match_rule(title, cls)
        role, notes = classify_layer_role(title, cls, emulator=rule.name if rule else "")
        if rule is None and role == "unknown":
            return None  # 与模拟器无关的杂窗
        return LayerInfo(
            hwnd=int(tgt.hwnd),
            title=title,
            class_name=cls,
            pid=int(tgt.pid),
            role=role,
            rect=rect_tuple(tgt.rect),
            client=(w, h),
            root_hwnd=int(tgt.root_hwnd),
            is_child=bool(is_child),
            emulator=rule.name if rule else "",
            notes=tuple(notes),
        )
    except Exception:
        return None


def collect_emulator_layers(*, min_client: int = MIN_SANE_RECT) -> list[LayerInfo]:
    """扫描所有可见窗口，收集模拟器相关的层（含子窗，不做截图）。"""
    layers: list[LayerInfo] = []
    seen: set[int] = set()

    for w in Win32Target._enum_all():
        raw_title = str(w.get("title") or "")
        raw_cls = str(w.get("class_name") or "")
        if not _looks_emulator_ish(raw_title, raw_cls):
            continue  # 无关窗口不枚举子窗，避免拖慢
        try:
            root = Win32Target(int(w["hwnd"]))
        except Exception:
            continue
        kids: list[int] = []
        for h in _enum_child_hwnds(int(root.hwnd)):
            try:
                if int(Win32Target(h).pid) != int(root.pid):
                    continue
            except Exception:
                continue
            kids.append(int(h))

        info = layer_from_target(root, is_child=False, min_client=min_client)
        kid_infos: list[LayerInfo] = []
        for h in kids:
            if h in seen:
                continue
            ki = layer_from_target(Win32Target(h), is_child=True, min_client=min_client)
            if ki is not None:
                kid_infos.append(ki)
        if info is None and not kid_infos:
            continue
        if info is not None and int(info.hwnd) not in seen:
            seen.add(int(info.hwnd))
            layers.append(info)
        for ki in kid_infos:
            if int(ki.hwnd) in seen:
                continue
            seen.add(int(ki.hwnd))
            layers.append(ki)
    return layers


_EMULATOR_HINTS = (
    "mumu",
    "nemu",
    "ldplayer",
    "dnplayer",
    "雷电",
    "nox",
    "夜神",
    "bluestacks",
    "蓝叠",
    "memu",
    "逍遥",
    "安卓设备",
    "toolsavebits",
    "renderwindow",
    "subwin",
    "nxdevice",
)


def _looks_emulator_ish(title: str, cls: str) -> bool:
    """快速判断顶层窗是否值得展开（避免遍历浏览器等无关窗口的子窗）。"""
    if _match_rule(title, cls) is not None:
        return True
    blob = f"{title} {cls}".lower()
    return any(k in blob for k in _EMULATOR_HINTS)


def group_layers(
    layers: list[LayerInfo],
    *,
    min_ratio: float = 0.55,
) -> list[dict[str, Any]]:
    """把层按实例分组（纯函数，便于测试）。

    先按顶层祖先分组，再按 pid + 几何重叠合并——镜像层常是独立顶层窗，
    与渲染区重叠，因此会并回同一实例。
    """
    groups: list[dict[str, Any]] = []
    for layer in layers or []:
        hit: Optional[dict[str, Any]] = None
        for g in groups:
            if int(g["pid"]) != int(layer.pid):
                continue
            if int(g["key_hwnd"]) == int(layer.root_hwnd):
                hit = g
                break
            if any(
                related_rects(other.rect, layer.rect, min_ratio=min_ratio)
                for other in g["layers"]
            ):
                hit = g
                break
        if hit is None:
            groups.append(
                {
                    "key_hwnd": int(layer.root_hwnd),
                    "pid": int(layer.pid),
                    "layers": [layer],
                }
            )
        else:
            hit["layers"].append(layer)

    out: list[dict[str, Any]] = []
    for idx, g in enumerate(groups, 1):
        members: list[LayerInfo] = list(g["layers"])
        shells = [x for x in members if x.role == "shell"]
        renders = [x for x in members if x.role == "render"]
        mirrors = [x for x in members if x.role == "mirror"]
        shell = max(shells, key=lambda x: x.client[0] * x.client[1], default=None)
        render = max(renders, key=lambda x: x.client[0] * x.client[1], default=None)
        name = (
            (shell.title if shell else "")
            or next((x.title for x in members if x.title), "")
            or f"实例{idx}"
        )
        notes: list[str] = []
        pids = {int(x.pid) for x in members}
        if len(pids) > 1:
            notes.append("同组内 pid 不一致，几何合并可能有误")
        if render is None and shell is not None:
            notes.append("未找到渲染子窗，点击目标暂用外壳")
        if not mirrors:
            notes.append("未发现镜像层（ToolSaveBits 之类）")
        click = render or shell
        out.append(
            {
                "index": idx,
                "name": name,
                "key_hwnd": int(g["key_hwnd"]),
                "pid": int(g["pid"]),
                "shell": shell.to_dict() if shell else None,
                "render": render.to_dict() if render else None,
                "mirrors": [x.to_dict() for x in mirrors],
                "layers": [x.to_dict() for x in members],
                "click_hwnd": int(click.hwnd) if click else 0,
                "emulator": next((x.emulator for x in members if x.emulator), ""),
                "notes": notes,
            }
        )
    out.sort(key=lambda g: (str(g["name"]).lower(), int(g["key_hwnd"])))
    for i, g in enumerate(out, 1):
        g["index"] = i
    return out


def group_emulator_instances(*, min_client: int = MIN_SANE_RECT) -> list[dict[str, Any]]:
    """多开时把所有模拟器窗口按实例分好组（shell / render / mirror）。"""
    return group_layers(collect_emulator_layers(min_client=min_client))
