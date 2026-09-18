#!/usr/bin/env python3
"""窗口点击精度探针：截图 →（可选找图）→ 后台点击 → 标注落点 → 再截对比。

用于排查 MuMu/Win32「空点击」（匹配成功但界面无变化）。

用法::

    python tools/window_click_probe.py --list MuMu
    python tools/window_click_probe.py --title MuMu安卓 --xy 400,300
    python tools/window_click_probe.py --hwnd 18879224 --image path/to/btn.png
    python tools/window_click_probe.py --title MuMu --xy 400,300 --modes raw,dpi_div

输出目录: screenshots/click_probe/{ts}/
  before.png / after_{mode}.png / annotated_{mode}.png / report.txt
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from backend.automation.win32_target import Win32Target, enum_windows  # noqa: E402


OUT_ROOT = ROOT / "screenshots" / "click_probe"


def _parse_xy(s: str) -> tuple[int, int]:
    parts = s.replace(" ", "").split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("xy 格式应为 x,y")
    return int(parts[0]), int(parts[1])


def _annotate(frame, x: int, y: int, label: str) -> np.ndarray:
    img = frame.copy()
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    h, w = img.shape[:2]
    color = (0, 0, 255)
    cv2.drawMarker(img, (int(x), int(y)), color, markerType=cv2.MARKER_CROSS, markerSize=28, thickness=2)
    cv2.circle(img, (int(x), int(y)), 12, color, 2)
    # 十字线辅助看偏移
    cv2.line(img, (0, int(y)), (w, int(y)), (0, 255, 255), 1)
    cv2.line(img, (int(x), 0), (int(x), h), (0, 255, 255), 1)
    cv2.putText(
        img,
        f"{label} ({x},{y})",
        (max(8, int(x) + 16), max(24, int(y) - 12)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2,
        cv2.LINE_AA,
    )
    return img


def _save(path: Path, frame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), frame)


def _pick_window(args) -> Win32Target:
    if args.hwnd:
        return Win32Target.from_hwnd(int(args.hwnd))
    title = (args.title or "").strip()
    if not title:
        raise SystemExit("请传 --hwnd 或 --title")
    hits = enum_windows(title)
    if not hits:
        raise SystemExit(f"未找到标题含 {title!r} 的可见窗口")
    if len(hits) > 1 and not args.pick:
        print(f"匹配到 {len(hits)} 个窗口，用 --pick N 选择（默认 0）:")
        for i, t in enumerate(hits):
            print(f"  [{i}] hwnd={t.hwnd} title={t.title!r} class={t.class_name!r}")
    idx = int(args.pick or 0)
    if idx < 0 or idx >= len(hits):
        raise SystemExit(f"--pick 越界: {idx}")
    return hits[idx]


def _resolve_point(win: Win32Target, frame, args) -> tuple[int, int, str]:
    if args.xy:
        x, y = args.xy
        return x, y, "xy"
    if args.image:
        from backend.matcher.matcher import matcher

        path = Path(args.image)
        if not path.is_file():
            path = ROOT / args.image
        if not path.is_file():
            raise SystemExit(f"模板不存在: {args.image}")
        hit = matcher.match(
            target=frame,
            template=str(path),
            threshold=float(args.threshold),
            match_type="image",
            use_color_check=False,
            match_select="best",
            use_orb=True,
        )
        if not hit or hit.x is None:
            raise SystemExit(f"未匹配到模板 {path}（threshold={args.threshold}）")
        score = getattr(hit, "score", getattr(hit, "max_val", None))
        print(f"match ok: ({hit.x},{hit.y}) score={score}")
        return int(hit.x), int(hit.y), f"img:{path.name}"
    # 默认点客户区中心
    h, w = frame.shape[:2]
    return w // 2, h // 2, "center"


def _click_modes(x: int, y: int, dpi: float, modes: list[str]) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for m in modes:
        m = m.strip().lower()
        if m == "raw":
            out.append(("raw", x, y))
        elif m in ("dpi_div", "dpi"):
            sx = int(round(x / dpi)) if dpi > 1.01 else x
            sy = int(round(y / dpi)) if dpi > 1.01 else y
            out.append((f"dpi_div/{dpi:.2f}", sx, sy))
        elif m in ("dpi_mul",):
            sx = int(round(x * dpi))
            sy = int(round(y * dpi))
            out.append((f"dpi_mul/{dpi:.2f}", sx, sy))
        else:
            raise SystemExit(f"未知 mode: {m}（支持 raw,dpi_div,dpi_mul）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="窗口点击精度探针")
    ap.add_argument("--list", metavar="TITLE", help="只列出匹配标题的窗口")
    ap.add_argument("--hwnd", type=int, help="窗口句柄")
    ap.add_argument("--title", default="", help="标题子串（如 MuMu）")
    ap.add_argument("--pick", type=int, default=0, help="多窗口时选第几个")
    ap.add_argument("--xy", type=_parse_xy, help="截图像素坐标 x,y")
    ap.add_argument("--image", help="模板图路径，匹配后点中心")
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument(
        "--modes",
        default="raw,dpi_div",
        help="点击坐标变换：raw,dpi_div,dpi_mul（逗号分隔）",
    )
    ap.add_argument("--sleep", type=float, default=0.6, help="点击后等待再截图（秒）")
    ap.add_argument("--send-mode", choices=("send", "post"), default="send")
    args = ap.parse_args()

    if args.list is not None:
        hits = enum_windows(args.list)
        if not hits:
            print(f"无可见窗口匹配 {args.list!r}")
            return 1
        for i, t in enumerate(hits):
            print(
                f"[{i}] hwnd={t.hwnd} dpi={t.dpi_scale:.2f} "
                f"client={t.client_rect} title={t.title!r} class={t.class_name!r}"
            )
        return 0

    win = _pick_window(args)
    dpi = float(win.dpi_scale)
    print(
        f"target hwnd={win.hwnd} title={win.title!r} class={win.class_name!r} "
        f"dpi={dpi:.2f} client={getattr(win, 'client_rect', '?')}"
    )

    before = win.screenshot(client_only=True, method="auto")
    if before is None:
        raise SystemExit("截图失败")
    x0, y0, src = _resolve_point(win, before, args)
    modes = [m for m in (args.modes or "raw").split(",") if m.strip()]
    clicks = _click_modes(x0, y0, dpi, modes)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_ROOT / ts
    out.mkdir(parents=True, exist_ok=True)
    _save(out / "before.png", before)

    lines = [
        f"hwnd={win.hwnd}",
        f"title={win.title}",
        f"class={win.class_name}",
        f"dpi_scale={dpi:.4f}",
        f"source_point=({x0},{y0}) via {src}",
        f"send_mode={args.send_mode}",
        "",
    ]

    for label, cx, cy in clicks:
        safe = label.replace("/", "_").replace(".", "p")
        ann = _annotate(before, x0, y0, f"src {src}")
        # 若变换后坐标不同，再标一次实际点击点
        if (cx, cy) != (x0, y0):
            ann = _annotate(ann, cx, cy, f"click {label}")
        else:
            ann = _annotate(ann, cx, cy, f"click {label}")
        _save(out / f"annotated_{safe}.png", ann)

        print(f"click [{label}] -> ({cx},{cy})")
        win.click(cx, cy, send_mode=args.send_mode)
        time.sleep(max(0.05, float(args.sleep)))
        after = win.screenshot(client_only=True, method="auto")
        if after is not None:
            _save(out / f"after_{safe}.png", after)
            # 简单差分能量，辅助判断有没有画面变化
            try:
                a = before.astype(np.float32)
                b = after.astype(np.float32)
                if a.shape != b.shape:
                    diff_score = -1.0
                else:
                    diff_score = float(np.mean(np.abs(a - b)))
            except Exception:
                diff_score = -1.0
        else:
            diff_score = -1.0
        lines.append(f"mode={label} click=({cx},{cy}) frame_diff_mean={diff_score:.3f}")
        print(f"  frame_diff_mean={diff_score:.3f}  (越大越可能点到了有效控件)")

    report = out / "report.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n输出: {out}")
    print("请肉眼对比 before.png / after_*.png；若 raw 无变化而 dpi_div 有变化，说明坐标需按 DPI 换算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
