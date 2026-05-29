# backend/browser/mixins/utils.py
import time
from pathlib import Path
from typing import Optional, Any

import cv2
import numpy as np

from core.path import PROJECT_ROOT


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
        dpr = self.device_pixel_ratio or 1.0
        return int(x / dpr), int(y / dpr)

    async def update_frame(self, save_screenshot=False) -> np.ndarray:
        png_bytes = await self.page.screenshot(timeout=5 * 60000)

        frame = cv2.imdecode(
            np.frombuffer(png_bytes, np.uint8),
            cv2.IMREAD_COLOR
        )
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
