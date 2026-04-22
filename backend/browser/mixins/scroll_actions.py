# backend/browser/mixins/scroll_actions.py
import asyncio
from typing import Optional


class ScrollActionsMixin:
    """滚动操作相关方法混入类"""

    async def scroll(
            self,
            delta_x: int = 0,
            delta_y: int = 0,
            x: Optional[int] = None,
            y: Optional[int] = None,
            steps: int = 10,
            scroll_time: float = 0.3,
    ) -> None:
        if x is not None and y is not None:
            await self.page.mouse.move(x, y)

        delay = scroll_time / steps if steps > 0 else 0

        for i in range(steps):
            step_delta_x = int(delta_x * ((i + 1) / steps)) - int(delta_x * (i / steps))
            step_delta_y = int(delta_y * ((i + 1) / steps)) - int(delta_y * (i / steps))

            await self.page.mouse.wheel(delta_x=step_delta_x, delta_y=step_delta_y)

            if delay > 0 and i < steps - 1:
                await asyncio.sleep(delay)

        print(f"{self.account['name']}: 滚动 ({delta_x}, {delta_y})")

    async def scroll_to_bottom(
            self,
            smooth: bool = True,
            step_size: int = 300,
            interval: float = 0.1,
    ) -> None:
        if smooth:
            last_height = await self.page.evaluate("document.body.scrollHeight")

            while True:
                await self.page.evaluate(f"window.scrollBy(0, {step_size})")
                await asyncio.sleep(interval)

                new_height = await self.page.evaluate("document.body.scrollHeight")
                scroll_y = await self.page.evaluate("window.scrollY")
                client_height = await self.page.evaluate("window.innerHeight")

                if scroll_y + client_height >= new_height - 10:
                    break

                if new_height > last_height:
                    last_height = new_height
        else:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        print(f"{self.account['name']}: 滚动到底部")

    async def scroll_to_top(self, smooth: bool = True) -> None:
        if smooth:
            await self.page.evaluate("""
            () => {
                window.scrollTo({
                    top: 0,
                    behavior: 'smooth'
                });
            }
            """)
            await asyncio.sleep(0.5)
        else:
            await self.page.evaluate("window.scrollTo(0, 0)")

        print(f"{self.account['name']}: 滚动到顶部")