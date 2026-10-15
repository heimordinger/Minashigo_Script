"""内置工具：annotate_cn

在图片上画中文标注（自动加载系统中文字体，避免问号乱码）。ARGS：{"image": 底图路径, "labels": [{"text":"...","box":[x,y,w,h],"color":"#RRGGBB"}], "out": 输出路径可选}

由 `tools/_scratch/_export_bundled_tools.py` 从实况自造工具导出，**勿手改**：
要改就先在探针里跑通，再重新导出。参数从 `ARGS` 取，结论赋给 `result`。

注意：本文件不是被 import 的可运行模块，执行时由探针执行器注入
`ARGS` / `read_image` / `resolve_image` / `OUT_DIR` / `Path` / `cv2` / `np` 等名字。
"""

import os

import cv2
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
    r"C:/Windows/Fonts/simkai.ttf",
    r"C:/Windows/Fonts/Deng.ttf",
    r"C:/Windows/Fonts/NotoSansSC-VF.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def pick_font(size):
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


A = ARGS if isinstance(ARGS, dict) else {}
src = resolve_image(A.get('image'))
if src is None:
    raise RuntimeError('读不到底图：%r。用素材名或完整路径。' % (A.get('image'),))
labels = A.get('labels') or []

out_name = str(A.get('out') or 'annotate_cn_out.png')
out_path = Path(out_name)
if not out_path.is_absolute():
    out_path = OUT_DIR / out_path.name
out_path.parent.mkdir(parents=True, exist_ok=True)

img = Image.open(str(src)).convert('RGB')
W, H = img.size
d = ImageDraw.Draw(img)
font = pick_font(max(18, int(H * 0.026)))


def hex2rgb(h):
    h = str(h or '#FF3B30').lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


drawn = 0
for lb in labels:
    x, y, w, h = [int(v) for v in lb['box']]
    color = hex2rgb(lb.get('color', '#FF3B30'))
    for t in range(3):
        d.rectangle([x - t, y - t, x + w + t, y + h + t], outline=color)
    txt = lb.get('text', '')
    if txt:
        bbox = d.textbbox((0, 0), txt, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ty = y - th - 10
        if ty < 0:
            ty = y + h + 6
        d.rectangle([x, ty, x + tw + 10, ty + th + 10], fill=color)
        d.text((x + 5, ty + 5), txt, font=font, fill=(255, 255, 255))
    drawn += 1

img.save(str(out_path))
result = {
    'out': str(out_path),
    'drawn': drawn,
    'font_size': font.size if hasattr(font, 'size') else None,
    'image_size': [W, H],
}
