# backend/browser/mixins/mouse_actions.JS&PyMessage
import asyncio


class MouseActionsMixin:
    """鼠标操作相关方法混入类"""

    async def click(self, x, y, pianyi=(0, 0), down_time=0.12):
        await self.page.bring_to_front()
        await self.page.mouse.move(x + pianyi[0], y + pianyi[1])
        await self.page.mouse.down()
        await asyncio.sleep(down_time)
        await self.page.mouse.up()
        print(f"{self.account['name']}: 点击 ({x}, {y})")

    async def double_click(
            self,
            x: int,
            y: int,
            pianyi: tuple = (0, 0),
            delay_between: float = 0.1,
            down_time: float = 0.08,
    ) -> None:
        click_x = x + pianyi[0]
        click_y = y + pianyi[1]

        await self.page.mouse.move(click_x, click_y)

        # 第一次点击
        await self.page.mouse.down()
        await asyncio.sleep(down_time)
        await self.page.mouse.up()

        await asyncio.sleep(delay_between)

        # 第二次点击
        await self.page.mouse.down()
        await asyncio.sleep(down_time)
        await self.page.mouse.up()

        print(f"{self.account['name']}: 双击 ({click_x}, {click_y})")

    async def right_click(
            self,
            x: int,
            y: int,
            pianyi: tuple = (0, 0),
            down_time: float = 0.12,
    ) -> None:
        click_x = x + pianyi[0]
        click_y = y + pianyi[1]

        await self.page.mouse.move(click_x, click_y)
        await self.page.mouse.down(button="right")
        await asyncio.sleep(down_time)
        await self.page.mouse.up(button="right")

        print(f"{self.account['name']}: 右键点击 ({click_x}, {click_y})")

    async def hover(
            self,
            x: int,
            y: int,
            pianyi: tuple = (0, 0),
            duration: float = 0.5,
    ) -> None:
        hover_x = x + pianyi[0]
        hover_y = y + pianyi[1]

        await self.page.mouse.move(hover_x, hover_y)
        await asyncio.sleep(duration)

        print(f"{self.account['name']}: 悬停 ({hover_x}, {hover_y}) {duration}s")