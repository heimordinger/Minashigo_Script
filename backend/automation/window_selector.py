"""
窗口查找/选择工具 —— 用于交互式选取目标窗口。

提供：
  1. print_windows()  — 列出匹配的窗口信息
  2. pick_window()    — 交互式选择（控制台）
  3. QuickPicker      — PySide6 窗口选择器（如果 Qt 可用）
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

from .win32_target import Win32Target


# ========================================================================
# 窗口信息结构化
# ========================================================================

@dataclass
class WindowInfo:
    index: int
    hwnd: int
    title: str
    class_name: str
    pid: int
    rect: str
    client_size: str
    visible: bool

    @classmethod
    def from_target(cls, target: Win32Target, index: int = 0) -> "WindowInfo":
        r = target.rect
        cr = target.client_rect
        return cls(
            index=index,
            hwnd=target.hwnd,
            title=target.title[:80],
            class_name=target.class_name,
            pid=target.pid,
            rect=f"{r['left']},{r['top']} {r['width']}x{r['height']}",
            client_size=f"{cr['width']}x{cr['height']}",
            visible=target.is_visible,
        )


# ========================================================================
# 输出
# ========================================================================

def print_windows(targets: list[Win32Target],
                  title: str = "找到的窗口",
                  detailed: bool = False):
    """打印窗口列表到控制台。"""
    if not targets:
        print(f"[{title}] (无匹配)")
        return

    print(f"\n{'=' * 80}")
    print(f"[{title}] 共 {len(targets)} 个窗口")
    print(f"{'=' * 80}")

    if detailed:
        for i, t in enumerate(targets):
            info = WindowInfo.from_target(t, i)
            print(f"\n  [{info.index}] 句柄={info.hwnd}")
            print(f"       标题: {info.title}")
            print(f"       类名: {info.class_name}")
            print(f"       PID:  {info.pid}")
            print(f"       位置: {info.rect}")
            print(f"       客户区: {info.client_size}")
    else:
        print(f"  {'Idx':<4} {'HWND':<10} {'标题':<40} {'类名':<20} {'客户区':<12}")
        print(f"  {'-'*4} {'-'*10} {'-'*40} {'-'*20} {'-'*12}")
        for i, t in enumerate(targets):
            info = WindowInfo.from_target(t, i)
            title_trunc = info.title[:38] if len(info.title) > 38 else info.title
            cls_trunc = info.class_name[:18] if len(info.class_name) > 18 else info.class_name
            print(f"  {i:<4} {info.hwnd:<10} {title_trunc:<40} {cls_trunc:<20} {info.client_size:<12}")
    print()


# ========================================================================
# 控制台交互选择
# ========================================================================

def pick_window(title_pattern: str = "",
                prompt: str = "请选择窗口编号（Enter=取消）: ") -> Optional[Win32Target]:
    """交互式选择窗口。

    Args:
        title_pattern: 可选标题筛选
        prompt: 输入提示

    Returns:
        选中的 Win32Target，取消则返回 None
    """
    if title_pattern:
        targets = Win32Target.from_title(title_pattern)
    else:
        targets = Win32Target.all_visible()

    # 按标题排序（方便查找）
    targets.sort(key=lambda t: t.title.lower())

    if not targets:
        print("未找到匹配的可见窗口。")
        return None

    print_windows(targets, f"窗口列表{' (筛选: ' + title_pattern + ')' if title_pattern else ' (全部可见)'}")

    try:
        choice = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not choice:
        return None

    try:
        idx = int(choice)
        if 0 <= idx < len(targets):
            selected = targets[idx]
            print(f"已选择: [{selected.title}] (hwnd={selected.hwnd})")
            return selected
        else:
            print(f"编号 {idx} 超出范围 [0-{len(targets) - 1}]")
            return None
    except ValueError:
        print(f"无效输入: {choice}")
        return None


# ========================================================================
# PySide6 弹窗选择器
# ========================================================================

def pick_window_qt(title_pattern: str = "",
                   parent=None) -> Optional[Win32Target]:
    """PySide6 弹窗窗口选择器。

    如果 Qt 不可用则回退到控制台选择。

    Returns:
        选中的 Win32Target，取消则返回 None
    """
    try:
        from PySide6 import QtWidgets
    except ImportError:
        return pick_window(title_pattern)

    if title_pattern:
        targets = Win32Target.from_title(title_pattern)
    else:
        targets = Win32Target.all_visible()

    targets.sort(key=lambda t: t.title.lower())

    if not targets:
        print("未找到匹配的可见窗口。")
        return None

    try:
        if QtWidgets.QApplication.instance() is None:
            # 没有 Qt 应用上下文，回退控制台
            return pick_window(title_pattern)
    except Exception:
        return pick_window(title_pattern)

    # 构建选择列表
    items = []
    for i, t in enumerate(targets):
        title = t.title[:60] if t.title else "(无标题)"
        cls = t.class_name[:20]
        cr = t.client_rect
        label = f"[{i}] {title}  ({cls})  {cr['width']}x{cr['height']}"
        items.append(label)

    item, ok = QtWidgets.QInputDialog.getItem(
        parent or QtWidgets.QWidget(),
        "选择目标窗口",
        "窗口列表（按标题排序）:",
        items,
        editable=False,
    )

    if ok and item:
        idx = items.index(item)
        return targets[idx]
    return None


# ========================================================================
# 命令行入口：python -m backend.automation.window_selector
# ========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="窗口选择工具")
    parser.add_argument("pattern", nargs="?", default="",
                        help="标题筛选关键词（可选）")
    parser.add_argument("--all", "-a", action="store_true",
                        help="显示所有可见窗口（即使指定了 pattern）")
    parser.add_argument("--detailed", "-d", action="store_true",
                        help="显示详细信息")
    args = parser.parse_args()

    if args.all:
        targets = Win32Target.all_visible()
        print_windows(targets, "全部可见窗口", detailed=args.detailed)
    elif args.pattern:
        targets = Win32Target.from_title(args.pattern)
        print_windows(targets, f"匹配 '{args.pattern}'", detailed=args.detailed)
    else:
        win = pick_window()
        if win:
            print(f"\n选中的窗口信息:")
            info = WindowInfo.from_target(win)
            print(f"  hwnd:       {info.hwnd}")
            print(f"  标题:       {info.title}")
            print(f"  类名:       {info.class_name}")
            print(f"  PID:        {info.pid}")
            print(f"  窗口区域:   {info.rect}")
            print(f"  客户区:     {info.client_size}")
            print(f"  可见:       {info.visible}")

            # 测试截图
            try:
                frame = win.screenshot()
                print(f"\n  截图成功: {frame.shape[1]}x{frame.shape[0]} {frame.dtype}")
                print(f"  像素范围: [{frame.min()}, {frame.max()}]")
            except Exception as e:
                print(f"\n  截图失败: {e}")
