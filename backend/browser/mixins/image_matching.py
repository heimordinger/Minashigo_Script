# backend/browser/mixins/image_matching.py
import asyncio
from pathlib import Path
from typing import Union

from core.match.match_result import MatchResult


class ImageMatchingMixin:
    """图像匹配相关方法混入类"""

    async def match_image(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
    ):
        print(f"[match_image] _frame is None? {self._frame is None}")
        print(f"[match_image] _frame_ts={self._frame_ts}")
        if self._frame is not None:
            print(f"[match_image] frame shape={self._frame.shape}, dtype={self._frame.dtype}")
        print(f"[match_image] img_path type={type(img_path)}, is_base64={str(img_path).startswith('data:image')}")

        if self._frame is None:
            print(f"[match_image] _frame为None，调用update_frame()")
            await self.update_frame()
            print(f"[match_image] update_frame后 frame shape={self._frame.shape}")

        # 不转换 base64 数据 URL 为 Path（Windows 上 Path 会将 / 转为 \，破坏 data:image 前缀判断）
        orig_path = img_path
        if not str(img_path).startswith("data:image"):
            img_path = Path(img_path)
            print(f"[match_image] 转换为Path: {img_path}")

        print(f"[match_image] 开始匹配: template={str(img_path)[:80]}, threshold={threshold}")
        # 在默认线程池中执行 CPU 密集的 OpenCV 匹配，避免阻塞事件循环
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.matcher.match(
                target=self._frame,
                template=img_path,
                threshold=threshold,
                match_type="image",
                use_color_check=use_color_check,
                match_select=match_select,
            )
        )

        print(f"{self.account['name']}: 图片匹配[{img_path}]:({result.x}, {result.y}), {result.score}")

        if result.x is None:
            print(f"[match_image] ❌ 匹配无结果, score={result.score}")
            return MatchResult(x=None, y=None, max_val=result.score, match_success=False)

        x, y = self.device_to_css(result.x, result.y)
        print(f"[match_image] ✅ 匹配成功: img=({result.x},{result.y}) -> css=({x},{y}), score={result.score}")

        return MatchResult(x=x, y=y, max_val=result.score, match_success=result.score >= threshold)

    async def match_image_multi(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
    ):
        if self._frame is None:
            await self.update_frame()

        # 不转换 base64 数据 URL 为 Path（Windows 上 Path 会将 / 转为 \，破坏 data:image 前缀判断）
        if not str(img_path).startswith("data:image"):
            img_path = Path(img_path)

        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None,
            lambda: self.matcher.match(
                target=self._frame,
                template=img_path,
                match_type="image_multi",
                threshold=threshold,
                use_color_check=use_color_check,
            )
        )

        if not results:
            return []

        converted = []
        for r in results:
            x, y = self.device_to_css(r["x"], r["y"])
            converted.append({
                "x": x,
                "y": y,
                "score": r["score"],
            })

        return converted

    async def click_image(
            self,
            img_path: Union[str, Path],
            pianyi=(0, 0),
            down_time=0.12,
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
    ):
        import time
        _t0 = time.time()
        print(f"[Browser.click_image] entered t=0", flush=True)

        match = await self.match_image(
            img_path=img_path,
            threshold=threshold,
            use_color_check=use_color_check,
            match_select=match_select,
        )
        print(f"[Browser.click_image] match_image done t={time.time()-_t0:.3f}s match.x={match.x} match.y={match.y}", flush=True)

        if match.x is None:
            print(f"[Browser.click_image] match失败，返回False", flush=True)
            return False

        x = match.x + pianyi[0]
        y = match.y + pianyi[1]
        print(f"[Browser.click_image] 计算坐标 ({x},{y}) t={time.time()-_t0:.3f}s", flush=True)

        if self._is_debug:
            await self.draw_click_point(x, y, color="red")

        print(f"[Browser.click_image] 开始执行click({x},{y}) t={time.time()-_t0:.3f}s", flush=True)
        await self.click(x=x, y=y, down_time=down_time)
        print(f"[Browser.click_image] click完成 t={time.time()-_t0:.3f}s", flush=True)

        print(
            f"{self.account['name']}: 点击图片:{img_path}({x},{y}), "
            f"最大匹配度:{match.max_val}"
        )

        return True