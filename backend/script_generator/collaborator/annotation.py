"""在图上直接标记：把模型给的坐标画成圈/框/编号，产出一张新图。

这是「在图上标记」的一等公民。此前只有 run_probe_script（手写 cv2）和
match_template 自动画框两条路，模型面对"帮我在图上标出来"时没有正经工具，
只能反问用户。本模块负责纯粹的绘制；产物如何登记、是否影响场景素材由调用方定。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

# 颜色名 → BGR。默认橙红，和界面里的手动标记一致。
COLORS: dict[str, tuple[int, int, int]] = {
    "red": (60, 60, 230),
    "orange": (59, 163, 240),
    "yellow": (60, 210, 245),
    "green": (90, 190, 70),
    "blue": (240, 130, 90),
    "purple": (210, 90, 190),
    "white": (255, 255, 255),
    "black": (20, 20, 20),
}
DEFAULT_COLOR = "orange"

MAX_POINTS = 200
# 只画一个点时给个可视尺寸，否则圈小到看不见
DEFAULT_RADIUS = 18
MAX_RADIUS = 200


@lru_cache(maxsize=16)
def _unicode_font(size: int):
    """返回能画中文的 Pillow 字体；Windows 优先使用微软雅黑。"""
    from PIL import ImageFont

    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    )
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), max(10, int(size)))
            except Exception:
                continue
    # 最后的回退不会把文本主动替换成问号；缺字时由字体显示方框。
    return ImageFont.load_default()


def _pil_text_size(text: str, size: int) -> tuple[int, int]:
    from PIL import Image, ImageDraw

    font = _unicode_font(size)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    return max(1, box[2] - box[0]), max(1, box[3] - box[1])


def _draw_unicode_text(canvas: Any, text: str, xy: tuple[int, int], color, size: int) -> None:
    """OpenCV Hershey 字体不支持中文；通过 Pillow 在 BGR 图上绘制 Unicode。"""
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    draw = ImageDraw.Draw(image)
    b, g, r = color
    draw.text(xy, text, font=_unicode_font(size), fill=(r, g, b))
    canvas[:] = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def resolve_color(raw: Any) -> tuple[int, int, int]:
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in COLORS:
            return COLORS[key]
        if key.startswith("#") and len(key) == 7:
            try:
                r = int(key[1:3], 16)
                g = int(key[3:5], 16)
                b = int(key[5:7], 16)
                return (b, g, r)  # 转 BGR
            except ValueError:
                pass
    return COLORS[DEFAULT_COLOR]


def _clamp_rect(vals: list[float], w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = vals
    x0, x1 = sorted((float(x0), float(x1)))
    y0, y1 = sorted((float(y0), float(y1)))
    x0 = max(0, min(int(round(x0)), w - 1))
    x1 = max(0, min(int(round(x1)), w - 1))
    y0 = max(0, min(int(round(y0)), h - 1))
    y1 = max(0, min(int(round(y1)), h - 1))
    return x0, y0, x1, y1


def normalize_marks(
    marks: Any, *, width: int, height: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型给的 marks 收干净。返回 (可画项, 丢弃说明)。

    坐标一律按**原图像素**理解（和 describe_current_frame 返回的 bbox 同一套），
    界面上看到的缩放不影响这里。
    """
    out: list[dict[str, Any]] = []
    dropped: list[str] = []
    if not isinstance(marks, list):
        return out, ["marks 必须是列表"]
    for i, raw in enumerate(marks[:MAX_POINTS], 1):
        if not isinstance(raw, dict):
            dropped.append(f"第{i}项不是对象")
            continue
        label = str(raw.get("label") or "").strip()[:40]
        color = resolve_color(raw.get("color"))
        radius = raw.get("radius")
        try:
            radius = int(radius) if radius is not None else 0
        except Exception:
            radius = 0
        radius = max(0, min(radius or DEFAULT_RADIUS, MAX_RADIUS))

        rect = raw.get("rect") or raw.get("box")
        if isinstance(rect, (list, tuple)) and len(rect) >= 4:
            try:
                x0, y0, x1, y1 = _clamp_rect([float(v) for v in rect[:4]], width, height)
            except Exception:
                dropped.append(f"第{i}项 rect 不是四个数字")
                continue
            if x1 - x0 < 1 or y1 - y0 < 1:
                dropped.append(f"第{i}项 rect 退化成一个点")
                continue
            out.append(
                {"kind": "rect", "rect": (x0, y0, x1, y1), "label": label, "color": color}
            )
            continue

        x, y = raw.get("x"), raw.get("y")
        if x is None or y is None:
            dropped.append(f"第{i}项既没有 rect 也没有 x/y")
            continue
        try:
            px = int(round(float(x)))
            py = int(round(float(y)))
        except Exception:
            dropped.append(f"第{i}项 x/y 不是数字")
            continue
        if not (0 <= px < width and 0 <= py < height):
            dropped.append(f"第{i}项 ({px},{py}) 超出图 {width}x{height}")
            continue
        out.append(
            {"kind": "circle", "xy": (px, py), "label": label, "color": color, "radius": radius}
        )
    if len(marks) > MAX_POINTS:
        dropped.append(f"只画了前 {MAX_POINTS} 个")
    return out, dropped


def _label_color(bgr: tuple[int, int, int]) -> tuple[int, int, int]:
    b, g, r = bgr
    # 亮度高的底色写黑字，反过来写白字
    lum = 0.114 * b + 0.587 * g + 0.299 * r
    return (20, 20, 20) if lum > 150 else (255, 255, 255)


def render_marks(
    image: Any,
    marks: Any,
    *,
    title: str = "",
    draw_index: bool = True,
) -> tuple[Any, list[str], list[dict[str, Any]]]:
    """在图像副本上绘制标记。返回 (新图, 丢弃说明, 实际画出的项)。"""
    import cv2

    canvas = image.copy()
    h, w = canvas.shape[:2]
    items, dropped = normalize_marks(marks, width=w, height=h)
    thickness = max(2, min(w, h) // 320)

    for idx, item in enumerate(items, 1):
        color = item["color"]
        label = item["label"]
        if draw_index:
            label = f"{idx} {label}".strip()
        if item["kind"] == "rect":
            x0, y0, x1, y1 = item["rect"]
            cv2.rectangle(canvas, (x0, y0), (x1, y1), color, thickness)
            anchor_y = y0
        else:
            px, py = item["xy"]
            cv2.circle(canvas, (px, py), int(item["radius"]), color, thickness)
            anchor_y = py - int(item["radius"])
        if label:
            scale = max(0.5, min(w, h) / 1400.0)
            font_size = max(14, int(round(28 * scale)))
            tw, th = _pil_text_size(label, font_size)
            base = max(2, font_size // 8)
            ty = max(th + base + 4, anchor_y - 6)
            tx = 0
            if item["kind"] == "rect":
                tx = item["rect"][0]
            else:
                tx = max(0, item["xy"][0] - tw // 2)
            tx = max(0, min(tx, w - tw - 2))
            cv2.rectangle(
                canvas,
                (tx - 3, ty - th - base - 2),
                (tx + tw + 3, ty + base),
                color,
                -1,
            )
            _draw_unicode_text(
                canvas, label, (tx, max(0, ty - base - th)), _label_color(color), font_size
            )

    if title:
        _draw_banner(canvas, title)

    return canvas, dropped, items


def _draw_banner(canvas: Any, title: str) -> None:
    import cv2

    h, w = canvas.shape[:2]
    text = str(title)[:60]
    scale = max(0.6, min(w, h) / 1200.0)
    font_size = max(16, int(round(30 * scale)))
    tw, th = _pil_text_size(text, font_size)
    base = max(2, font_size // 8)
    pad = 10
    box_h = th + base + pad * 2
    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, 0), (min(w, tw + pad * 2), box_h), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.65, canvas, 0.35, 0, canvas)
    _draw_unicode_text(canvas, text, (pad, pad), (255, 255, 255), font_size)
