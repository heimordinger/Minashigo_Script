# backend/browser/mixins/utils.py
import base64
import time
from pathlib import Path
from typing import Optional, Any

import cv2
import numpy as np

from core.path import PROJECT_ROOT

GAME_CANVAS_SELECTOR = "#Cocos2dGameContainer"
GAME_AREA_SELECTORS = (
    "#Cocos2dGameContainer",
    "#GameCanvas",
    "canvas#GameCanvas",
    "canvas.gameCanvas",
    "#GameDiv",
)

_CANVAS_BUFFER_JS = """
() => {
    const canvas = document.getElementById('GameCanvas')
        || document.querySelector('canvas#GameCanvas')
        || document.querySelector('canvas.gameCanvas');
    if (!canvas) return null;
    let dataUrl = null;
    let err = null;
    try {
        dataUrl = canvas.toDataURL('image/png');
    } catch (e) {
        err = String(e);
    }
    return {
        dataUrl,
        error: err,
        bufferW: canvas.width,
        bufferH: canvas.height,
    };
}
"""

class UtilsMixin:
    """工具方法混入类"""

    def find(self, value: str, type: str = "auto"):
        if type == "id":
            return self.page.locator(f"#{value}")
        elif type == "class":
            return self.page.locator(f".{value}")
        elif type == "text":
            return self.page.get_by_text(value)
        elif type == "button":
            return self.page.get_by_role("button", name=value)
        elif type == "name":
            return self.page.locator(f'[name="{value}"]')
        else:
            return self.page.get_by_role("button", name=value)

    async def fill_input(self, selector: str, value: str, type: str = "id"):
        elem = self.find(selector, type=type)
        await elem.fill(value)

    def device_to_css(self, x: int, y: int):
        if getattr(self, '_screenshot_css', False):
            return int(x), int(y)
        if getattr(self, "_frame_capture_mode", None) == "canvas_buffer":
            return self._apply_frame_origin_css(x, y)
        dpr = self.device_pixel_ratio or 1.0
        cx, cy = int(x / dpr), int(y / dpr)
        origin = getattr(self, '_frame_origin_css', None)
        if origin:
            cx += int(origin[0])
            cy += int(origin[1])
        return cx, cy

    def _apply_frame_origin_css(self, x: int, y: int) -> tuple[int, int]:
        mode = getattr(self, "_frame_capture_mode", None)
        origin = getattr(self, "_frame_origin_css", None)
        if mode == "canvas_buffer" and origin:
            css_size = getattr(self, "_frame_css_size", None)
            buf_size = getattr(self, "_frame_buffer_size", None)
            if css_size and buf_size and buf_size[0] > 0 and buf_size[1] > 0:
                cx = origin[0] + x * css_size[0] / buf_size[0]
                cy = origin[1] + y * css_size[1] / buf_size[1]
                return int(cx), int(cy)
        if origin:
            return int(x + origin[0]), int(y + origin[1])
        return int(x), int(y)

    def _clear_frame_capture_state(self) -> None:
        self._frame_origin_css = None
        self._frame_capture_mode = None
        self._frame_css_size = None
        self._frame_buffer_size = None

    async def _capture_canvas_buffer(self) -> Optional[np.ndarray]:
        """从 iframe 内 canvas 缓冲区读取，不受页面滚动/视口影响，也不触发整页截图。"""
        self._game_canvas_meta = None
        for frame in self.page.frames:
            try:
                loc = frame.locator("#GameCanvas, canvas.gameCanvas").first
                if await loc.count() == 0:
                    continue
                box = await loc.bounding_box(timeout=2000)
                if not box or box.get("width", 0) < 100 or box.get("height", 0) < 100:
                    continue

                payload = await frame.evaluate(_CANVAS_BUFFER_JS)
                if not payload or not payload.get("dataUrl"):
                    continue
                data_url = payload["dataUrl"]
                if not isinstance(data_url, str) or "," not in data_url:
                    continue

                png_bytes = base64.b64decode(data_url.split(",", 1)[1])
                img = cv2.imdecode(
                    np.frombuffer(png_bytes, np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if img is None or img.size == 0:
                    continue
                if float(np.mean(img)) < 3.0:
                    continue

                buf_w = int(payload.get("bufferW") or img.shape[1])
                buf_h = int(payload.get("bufferH") or img.shape[0])
                self._frame_origin_css = (box["x"], box["y"])
                self._frame_css_size = (box["width"], box["height"])
                self._frame_buffer_size = (buf_w, buf_h)
                self._frame_capture_mode = "canvas_buffer"
                self._game_canvas_meta = {
                    "selector": "#GameCanvas(buffer)",
                    "frame_url": frame.url,
                }
                return img
            except Exception:
                continue
        return None

    async def _screenshot_viewport(self, clip: Optional[dict] = None) -> bytes:
        # 不要传 animations="disabled"：会注入样式/打断合成，
        # 滚动条不在顶部时高频截图会产生明显页面抖动。
        kwargs = {"timeout": 5 * 60000}
        if clip:
            kwargs["clip"] = clip
        return await self.page.screenshot(**kwargs)

    async def _locator_page_box(self, frame, selector: str) -> Optional[dict]:
        """locator 的 bounding_box 已是相对顶层页面的 CSS 坐标（含 iframe 内元素）。"""
        try:
            loc = frame.locator(selector).first
            if await loc.count() == 0:
                return None
            box = await loc.bounding_box(timeout=2000)
            if not box:
                return None
            w, h = box.get("width", 0), box.get("height", 0)
            if w < 10 or h < 10:
                return None
            return {
                "x": max(0.0, box["x"]),
                "y": max(0.0, box["y"]),
                "width": w,
                "height": h,
            }
        except Exception:
            return None

    async def _largest_canvas_clip(self) -> Optional[dict]:
        best = None
        best_area = 0.0
        self._game_canvas_meta = None
        for frame in self.page.frames:
            try:
                count = await frame.locator("canvas").count()
            except Exception:
                continue
            for i in range(count):
                loc = frame.locator("canvas").nth(i)
                try:
                    box = await loc.bounding_box(timeout=1000)
                    if not box:
                        continue
                    w, h = box.get("width", 0), box.get("height", 0)
                    if w < 100 or h < 100:
                        continue
                    area = w * h
                    if area <= best_area:
                        continue
                    best_area = area
                    best = {
                        "x": max(0.0, box["x"]),
                        "y": max(0.0, box["y"]),
                        "width": w,
                        "height": h,
                    }
                    self._game_canvas_meta = {
                        "selector": "canvas(largest)",
                        "frame_url": frame.url,
                        "canvas_id": await loc.get_attribute("id"),
                        "canvas_class": await loc.get_attribute("class"),
                    }
                except Exception:
                    continue
        return best

    async def _find_game_canvas_clip(self) -> Optional[dict]:
        self._game_canvas_meta = None
        for frame in self.page.frames:
            for selector in GAME_AREA_SELECTORS:
                clip = await self._locator_page_box(frame, selector)
                if clip:
                    self._game_canvas_meta = {
                        "selector": selector,
                        "frame_url": frame.url,
                    }
                    return clip
        return await self._largest_canvas_clip()

    async def diagnose_game_canvas(self) -> list[dict]:
        rows: list[dict] = []
        for frame in self.page.frames:
            for selector in ("#GameDiv", "#Cocos2dGameContainer", "#GameCanvas"):
                try:
                    loc = frame.locator(selector).first
                    if await loc.count() == 0:
                        continue
                    box = await loc.bounding_box(timeout=500)
                except Exception:
                    box = None
                rows.append({
                    "kind": "game_area",
                    "selector": selector,
                    "frame_url": frame.url,
                    "box": box,
                })
            try:
                count = await frame.locator("canvas").count()
            except Exception:
                continue
            for i in range(count):
                loc = frame.locator("canvas").nth(i)
                try:
                    box = await loc.bounding_box(timeout=500)
                except Exception:
                    box = None
                rows.append({
                    "kind": "canvas",
                    "frame_url": frame.url,
                    "canvas_id": await loc.get_attribute("id"),
                    "canvas_class": await loc.get_attribute("class"),
                    "box": box,
                })
        return rows

    async def get_game_canvas_rect(self) -> Optional[dict]:
        clip = await self._find_game_canvas_clip()
        if not clip:
            return None
        meta = getattr(self, "_game_canvas_meta", None) or {}
        return {
            **clip,
            **meta,
        }

    async def _game_canvas_clip(self) -> Optional[dict]:
        return await self._find_game_canvas_clip()

    async def update_game_frame(self, save_screenshot=False) -> np.ndarray:
        """仅截取游戏区（不影响默认 update_frame 全视口）。"""
        return await self.update_frame(
            save_screenshot=save_screenshot,
            crop_game_canvas=True,
        )

    async def update_frame(
            self,
            save_screenshot=False,
            crop_game_canvas: bool = False,
    ) -> np.ndarray:
        self._screenshot_css = False
        self._clear_frame_capture_state()

        if crop_game_canvas:
            from backend.browser.game_frame_capture import capture_game_frame

            frame, clip, meta = await capture_game_frame(
                self.page,
                dpr=self.device_pixel_ratio or 1.0,
                restore_scroll=False,
            )
            if frame is not None and clip is not None:
                self._frame_origin_css = (clip["x"], clip["y"])
                self._frame_capture_mode = str(meta.get("method") or "viewport_crop")
                self._game_canvas_meta = {
                    "selector": meta.get("selector"),
                    "method": meta.get("method"),
                }
                print(
                    f"{self.account['name']}: 游戏区裁剪 "
                    f"{frame.shape[1]}x{frame.shape[0]} "
                    f"@ ({clip['x']:.0f},{clip['y']:.0f}) "
                    f"method={meta.get('method')}"
                )
            else:
                reason = meta.get("selector") or "no-canvas"
                if meta.get("viewport_crop_failed"):
                    reason = "crop+element-failed"
                print(f"{self.account['name']}: 游戏区裁剪失败({reason})，回退全视口")
                frame = None
                self._frame_capture_mode = "full"
        else:
            frame = None

        if frame is None:
            png_bytes = await self._screenshot_viewport()
            frame = cv2.imdecode(
                np.frombuffer(png_bytes, np.uint8),
                cv2.IMREAD_COLOR,
            )
            if getattr(self, "_frame_capture_mode", None) is None:
                self._frame_capture_mode = "full"

        safe_name = str(self.account['name']).encode('utf-8', errors='replace').decode('utf-8')
        if save_screenshot:
            cv2.imwrite(PROJECT_ROOT / "screenshot" / f"{safe_name}.png", frame)

        self._frame = frame
        self._frame_ts = time.time()
        return frame

    async def get_window_rect(self):
        return await self.page.evaluate("""
        () => {
            const { screenX, screenY, outerWidth, outerHeight } = window;
            return {
                left: screenX,
                top: screenY,
                width: outerWidth,
                height: outerHeight
            };
        }
        """)

    def viewport_to_image(self, vx: int, vy: int):
        if self._frame is None:
            return None, None

        origin = getattr(self, '_frame_origin_css', None)
        if origin:
            vx -= int(origin[0])
            vy -= int(origin[1])

        if getattr(self, "_frame_capture_mode", None) == "canvas_buffer":
            css_size = getattr(self, "_frame_css_size", None)
            buf_size = getattr(self, "_frame_buffer_size", None)
            if css_size and buf_size and css_size[0] > 0 and css_size[1] > 0:
                ix = int(vx * buf_size[0] / css_size[0])
                iy = int(vy * buf_size[1] / css_size[1])
            else:
                ix, iy = int(vx), int(vy)
        else:
            dpr = self.device_pixel_ratio or 1.0
            ix = int(vx * dpr)
            iy = int(vy * dpr)

        h, w = self._frame.shape[:2]

        if ix < 0 or iy < 0 or ix >= w or iy >= h:
            return None, None

        return ix, iy

    async def rect(self):
        return await self.page.evaluate("""
            () => {
                const { screenX, screenY, outerWidth, outerHeight } = window;
                return { left: screenX, top: screenY, width: outerWidth, height: outerHeight };
            }
            """)

    async def get_element_rect(self, selector: str) -> Optional[dict]:
        return await self.page.evaluate("""
        (selector) => {
            const element = document.querySelector(selector);
            if (!element) return null;

            const rect = element.getBoundingClientRect();
            return {
                left: rect.left,
                top: rect.top,
                right: rect.right,
                bottom: rect.bottom,
                width: rect.width,
                height: rect.height,
                x: rect.x,
                y: rect.y
            };
        }
        """, selector)

    async def save_screenshot(
            self,
            name: Optional[str] = None,
            path: Optional[Path] = None
    ) -> Path:
        if path is None:
            if name is None:
                name = f"screenshot_{int(time.time())}"
            path = PROJECT_ROOT / "screenshots" / f"{name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)

        await self.page.screenshot(path=str(path))
        print(f"{self.account['name']}: 截图已保存 {path}")
        return path

    async def execute_js(
            self,
            script: str,
            *args,
            wait_for_result: bool = True
    ) -> Any:
        if wait_for_result:
            result = await self.page.evaluate(script, *args)
        else:
            await self.page.evaluate(script, *args)
            result = None

        print(f"{self.account['name']}: 执行 JS ({script[:50]}...)")
        return result
