# backend/browser/mixins/debug.JS&PyMessage
class DebugMixin:
    """调试工具相关方法混入类"""

    async def ensure_debug_overlay(self):
        await self.page.evaluate("""
        (() => {
            if (window.__debugOverlay) return;

            const overlay = document.createElement('div');
            overlay.id = '__debugOverlay';
            overlay.style.position = 'fixed';
            overlay.style.left = '0';
            overlay.style.top = '0';
            overlay.style.width = '100vw';
            overlay.style.height = '100vh';
            overlay.style.pointerEvents = 'none';
            overlay.style.zIndex = '2147483647';
            document.body.appendChild(overlay);

            window.__debugOverlay = overlay;
        })();
        """)

    async def draw_click_point(self, x, y, color='red'):
        await self.ensure_debug_overlay()

        await self.page.evaluate("""
        ({ x, y, color }) => {
            const dot = document.createElement('div');
            dot.style.position = 'absolute';
            dot.style.left = (x - 5) + 'px';
            dot.style.top = (y - 5) + 'px';
            dot.style.width = '10px';
            dot.style.height = '10px';
            dot.style.borderRadius = '50%';
            dot.style.background = color;
            dot.style.boxShadow = '0 0 10px ' + color;
            dot.style.pointerEvents = 'none';
            window.__debugOverlay.appendChild(dot);
            setTimeout(() => dot.remove(), 1500);
        }
        """, {"x": x, "y": y, "color": color})

    async def draw_rect(self, x, y, w=50, h=50, color='lime'):
        await self.ensure_debug_overlay()

        await self.page.evaluate("""
        ({ x, y, w, h, color }) => {
            const box = document.createElement('div');
            box.style.position = 'absolute';
            box.style.left = x + 'px';
            box.style.top = y + 'px';
            box.style.width = w + 'px';
            box.style.height = h + 'px';
            box.style.border = '2px solid ' + color;
            box.style.pointerEvents = 'none';
            window.__debugOverlay.appendChild(box);
            setTimeout(() => box.remove(), 2000);
        }
        """, {"x": x, "y": y, "w": w, "h": h, "color": color})

    async def highlight_element(
            self,
            selector: str,
            duration: float = 2.0,
            color: str = "red"
    ) -> None:
        await self.page.evaluate("""
        ({ selector, duration, color }) => {
            const element = document.querySelector(selector);
            if (!element) return;

            const originalOutline = element.style.outline;
            const originalBoxShadow = element.style.boxShadow;

            element.style.outline = `3px solid ${color}`;
            element.style.boxShadow = `0 0 10px ${color}`;

            setTimeout(() => {
                element.style.outline = originalOutline;
                element.style.boxShadow = originalBoxShadow;
            }, duration * 1000);
        }
        """, {"selector": selector, "duration": duration, "color": color})