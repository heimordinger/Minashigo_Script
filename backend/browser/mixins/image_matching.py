# backend/browser/mixins/image_matching.JS&PyMessage
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
        if self._frame is None:
            await self.update_frame()

        img_path = Path(img_path)

        template = self.controller.template_cache.get(img_path)
        if template is None:
            template = self.matcher._load_and_normalize_template(img_path)
            self.controller.template_cache[img_path] = template

        x, y, max_val = self.matcher.match(
            target=self._frame,
            template=template,
            threshold=threshold,
            match_type="image",
            use_color_check=use_color_check,
            match_select=match_select
        )

        print(f"{self.account['name']}: 图片匹配[{img_path}]:{(x, y), max_val}")

        if x is None:
            return MatchResult(x=None, y=None, max_val=max_val)

        x, y = self.device_to_css(x, y)

        return MatchResult(x=x, y=y, max_val=max_val)

    async def match_image_multi(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
    ):
        if self._frame is None:
            await self.update_frame()

        img_path = Path(img_path)

        template = self.controller.template_cache.get(img_path)
        if template is None:
            template = self.matcher._load_and_normalize_template(img_path)
            self.controller.template_cache[img_path] = template

        results = self.matcher.match(
            target=self._frame,
            template=template,
            match_type="image_multi",
            threshold=threshold
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
        match = await self.match_image(
            img_path=img_path,
            threshold=threshold,
            use_color_check=use_color_check,
            match_select=match_select,
        )

        if match.x is None:
            return False

        x = match.x + pianyi[0]
        y = match.y + pianyi[1]

        if self._is_debug:
            await self.draw_click_point(x, y, color="red")

        await self.click(x=x, y=y, down_time=down_time)

        print(
            f"{self.account['name']}: 点击图片:{img_path}({x},{y}), "
            f"最大匹配度:{match.max_val}"
        )

        return True