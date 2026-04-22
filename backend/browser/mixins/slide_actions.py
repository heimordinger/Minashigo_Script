# backend/browser/mixins/slide_actions.py
import asyncio
import math
import random
from typing import Optional, Callable, Awaitable


class SlideActionsMixin:
    """滑动操作相关方法混入类"""

    async def slide(
            self,
            coordinate1: tuple,  # 起始坐标 (x, y)
            coordinate2: tuple,  # 结束坐标 (x, y)
            steps: int = 20,  # 滑动步数，越大越平滑
            slide_time: float = 0.6,  # 滑动总时长（秒）
            ease: str = "linear",  # 缓动类型: linear/in/out/in_out
            jitter: float = 0.0,  # 随机抖动像素，模拟手抖
            hold_before: float = 0.3,  # 按下后停留时间（秒）
            hold_after: float = 0.3,  # 松开前停留时间（秒）
            button: str = "left",  # 鼠标按键: left/middle/right
            abort_check: Optional[Callable[[], Awaitable[bool]]] = None,  # 中止检查函数（必须是无参异步函数）
    ) -> bool:
        """
        执行带缓动效果的鼠标滑动操作

        Args:
            ...（其他参数说明略）
            abort_check: 中止检查函数，必须是无参数的异步函数，返回True时中止滑动
                        ⚠️ 如果函数需要参数，请使用 lambda 包装

        Examples:
            >>> # 1. 无参数的情况
            >>> async def check_condition():
            ...     return some_value > 10
            >>> await browser.slide((100,100), (500,500), abort_check=check_condition)

            >>> # 2. 有参数的情况 - 使用 lambda 包装
            >>> async def check_with_param(threshold: int):
            ...     return some_value > threshold
            >>> await browser.slide(
            ...     (100,100), (500,500),
            ...     abort_check=lambda: check_with_param(20)  # lambda 包装传参
            ... )

            >>> # 3. 需要访问实例变量的情况
            >>> await browser.slide(
            ...     (100,100), (500,500),
            ...     abort_check=lambda: self.check_timeout(30)  # 使用 self
            ... )
        """
        x1, y1 = coordinate1
        x2, y2 = coordinate2

        # 确保至少有一步
        if steps <= 0:
            steps = 1

        # 缓动函数
        def ease_fn(t: float) -> float:
            if ease == "in":
                return t * t
            elif ease == "out":
                return 1 - (1 - t) * (1 - t)
            elif ease == "in_out":
                return 0.5 * (1 - math.cos(math.pi * t))
            return t

        # 计算每步间隔
        delay = slide_time / steps

        # ---------- 开始滑动 ----------
        # 1. 移动到起点
        await self.page.mouse.move(x1, y1)
        # 2. 按下鼠标
        await self.page.mouse.down(button=button)

        # 3. 按下后停留
        if hold_before > 0:
            await asyncio.sleep(hold_before)

        # 4. 执行滑动
        for i in range(steps):
            # 检查是否需要中止
            if abort_check:
                should_abort = await abort_check()  # 直接 await 异步函数
                if should_abort:
                    await self.page.mouse.up(button=button)
                    print(f"{self.account['name']}: 滑动被中止")
                    return False

            # 计算当前位置
            t = (i + 1) / steps
            et = ease_fn(t)
            nx = x1 + (x2 - x1) * et
            ny = y1 + (y2 - y1) * et

            # 添加随机抖动
            if jitter > 0:
                nx += random.uniform(-jitter, jitter)
                ny += random.uniform(-jitter, jitter)

            await self.page.mouse.move(nx, ny)
            await asyncio.sleep(delay)

        # 5. 松开前停留
        if hold_after > 0:
            await asyncio.sleep(hold_after)

        # 6. 松开鼠标
        await self.page.mouse.up(button=button)

        print(
            f"{self.account['name']}: 滑动 ({x1:.0f},{y1:.0f}) -> ({x2:.0f},{y2:.0f}), "
            f"time={slide_time}s, steps={steps}, ease={ease}"
        )
        return True

    async def drag_and_drop(
            self,
            start: tuple,  # 起始坐标
            end: tuple,  # 结束坐标
            steps: int = 20,  # 拖拽步数
            drag_time: float = 0.8,  # 拖拽总时长
            hold_at_end: float = 0.2,  # 拖拽后停留时间
            button: str = "left",  # 鼠标按键
            abort_check: Optional[Callable[[], Awaitable[bool]]] = None,  # 中止检查
    ) -> bool:
        """
        拖拽操作（按下 -> 滑动 -> 释放）

        Args:
            start: 起始坐标 (x, y)
            end: 结束坐标 (x, y)
            steps: 拖拽步数
            drag_time: 拖拽总时长
            hold_at_end: 拖拽后停留时间（释放前）
            button: 鼠标按键
            abort_check: 中止检查函数，用法同 slide()
        """
        x1, y1 = start
        x2, y2 = end

        # 移动到起点并按下
        await self.page.mouse.move(x1, y1)
        await self.page.mouse.down(button=button)
        await asyncio.sleep(0.1)  # 短暂停顿，更像真人

        # 执行滑动
        result = await self.slide(
            coordinate1=start,
            coordinate2=end,
            steps=steps,
            slide_time=drag_time,
            ease="linear",  # 拖拽通常用线性
            button=button,
            abort_check=abort_check  # 传递中止检查
        )

        # 如果滑动成功，停留后释放
        if result and hold_at_end > 0:
            await asyncio.sleep(hold_at_end)
            await self.page.mouse.up(button=button)

        print(f"{self.account['name']}: 拖拽 ({x1:.0f},{y1:.0f}) -> ({x2:.0f},{y2:.0f})")
        return result

    async def drag_by_offset(
            self,
            start: tuple,  # 起始坐标
            offset_x: int,  # X轴偏移量
            offset_y: int,  # Y轴偏移量
            **kwargs  # 其他参数传给 drag_and_drop
    ) -> bool:
        """
        按偏移量拖拽

        Args:
            start: 起始坐标 (x, y)
            offset_x: X轴偏移量（正数向右，负数向左）
            offset_y: Y轴偏移量（正数向下，负数向上）
            **kwargs: 其他参数，如 steps, drag_time, hold_at_end, button, abort_check

        Examples:
            >>> # 向右拖拽100像素
            >>> await browser.drag_by_offset((500, 500), 100, 0)
            >>> # 向左上方拖拽
            >>> await browser.drag_by_offset((500, 500), -50, -50, drag_time=0.5)
        """
        x1, y1 = start
        x2, y2 = x1 + offset_x, y1 + offset_y

        return await self.drag_and_drop(
            start=(x1, y1),
            end=(x2, y2),
            **kwargs
        )