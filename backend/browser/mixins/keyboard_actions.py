# backend/browser/mixins/keyboard_actions.py
import asyncio
import random
import sys


class KeyboardActionsMixin:
    """键盘操作相关方法混入类"""

    async def press_key(
            self,
            key: str,
            delay: float = 0.1,
            modifiers: list = None,
    ) -> None:
        if modifiers:
            for mod in modifiers:
                await self.page.keyboard.down(mod)

        await self.page.keyboard.press(key)
        await asyncio.sleep(delay)

        if modifiers:
            for mod in reversed(modifiers):
                await self.page.keyboard.up(mod)

        print(f"{self.account['name']}: 按键 {key}" + (f" (修饰: {modifiers})" if modifiers else ""))

    async def type_text(
            self,
            text: str,
            delay_between: float = 0.05,
            human_like: bool = True,
    ) -> None:
        for char in text:
            await self.page.keyboard.type(char)

            if human_like:
                delay = delay_between * random.uniform(0.8, 1.5)
            else:
                delay = delay_between

            await asyncio.sleep(delay)

        print(f"{self.account['name']}: 输入文本 '{text}'")

    async def select_all_and_delete(self) -> None:
        if sys.platform == "darwin":
            await self.page.keyboard.press("Meta+A")
        else:
            await self.page.keyboard.press("Control+A")

        await asyncio.sleep(0.1)
        await self.page.keyboard.press("Backspace")

        print(f"{self.account['name']}: 全选并删除")