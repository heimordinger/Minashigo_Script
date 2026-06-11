# backend/browser/mixins/text_matching.py
from core.match.match_result import MatchResult
from core.logging.events import LogLevel


class TextMatchingMixin:
    """文字匹配相关方法混入类"""

    async def match_text(
            self,
            text: str,
            threshold: float = 0.6,
            match_select: str = "best",
    ):
        if threshold < 1:
            threshold = 100 * threshold
        if self._frame is None:
            await self.update_frame()

        result = self.matcher.match(
            target=self._frame,
            text=text,
            threshold=threshold,
            match_type="text",
            match_select=match_select
        )

        if not result:
            return MatchResult(x=None, y=None, max_val=None)

        x = result.get("x")
        y = result.get("y")
        max_val = result.get("max_val")

        if x is None or y is None:
            return MatchResult(x=None, y=None, max_val=max_val)

        x, y = self.device_to_css(x, y)

        return MatchResult(x=x, y=y, max_val=max_val)

    async def click_text(
            self,
            text: str,
            threshold: int = 60,
            pianyi=(0, 0),
            match_select: str = "best",
    ):
        if self._frame is None:
            await self.update_frame()

        if not text:
            self._log("文字为空", LogLevel.WARNING)
            return False

        result = await self.match_text(text=text, threshold=threshold, match_select=match_select)

        if not result:
            print("未匹配到文字")
            return False

        x = result.x + pianyi[0]
        y = result.y + pianyi[1]

        await self.click(x=x, y=y)

        return True