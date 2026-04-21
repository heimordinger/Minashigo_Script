# backend/browser/mixins/waiting.py
import time
import asyncio
from pathlib import Path
from typing import Union, Optional

import cv2
import numpy as np

from core.match.match_result import MatchResult


class WaitingMixin:
    """等待操作相关方法混入类"""

    async def wait_for_image(
            self,
            img_path: Union[str, Path],
            timeout: float = 10,
            threshold: float = 0.9,
            interval: float = 0.5,
            use_color_check: bool = False,
    ) -> Optional[MatchResult]:
        start_time = time.time()

        while time.time() - start_time < timeout:
            await self.update_frame()

            result = await self.match_image(
                img_path=img_path,
                threshold=threshold,
                use_color_check=use_color_check
            )

            if result.x is not None:
                print(f"{self.account['name']}: 图片出现 {img_path}")
                return result

            await asyncio.sleep(interval)

        print(f"{self.account['name']}: 等待图片超时 {img_path}")
        return None

    async def wait_for_image_disappear(
            self,
            img_path: Union[str, Path],
            timeout: float = 10,
            threshold: float = 0.9,
            interval: float = 0.5,
    ) -> bool:
        start_time = time.time()

        while time.time() - start_time < timeout:
            await self.update_frame()

            result = await self.match_image(
                img_path=img_path,
                threshold=threshold
            )

            if result.x is None:
                print(f"{self.account['name']}: 图片已消失 {img_path}")
                return True

            await asyncio.sleep(interval)

        print(f"{self.account['name']}: 等待图片消失超时 {img_path}")
        return False

    async def wait_for_text(
            self,
            text: str,
            timeout: float = 10,
            threshold: int = 60,
            interval: float = 0.5,
    ) -> Optional[MatchResult]:
        start_time = time.time()

        while time.time() - start_time < timeout:
            await self.update_frame()

            result = await self.match_text(
                text=text,
                threshold=threshold
            )

            if result.x is not None:
                print(f"{self.account['name']}: 文字出现 '{text}'")
                return result

            await asyncio.sleep(interval)

        print(f"{self.account['name']}: 等待文字超时 '{text}'")
        return None

    async def wait_for_stable(
            self,
            timeout: float = 10,
            stable_time: float = 1.0,
            check_interval: float = 0.3,
    ) -> bool:
        start_time = time.time()
        stable_start = None
        last_frame = None

        while time.time() - start_time < timeout:
            await self.update_frame()

            if last_frame is not None:
                diff = cv2.absdiff(self._frame, last_frame)
                non_zero_count = np.count_nonzero(diff)

                if non_zero_count < 1000:
                    if stable_start is None:
                        stable_start = time.time()
                    elif time.time() - stable_start >= stable_time:
                        print(f"{self.account['name']}: 页面已稳定")
                        return True
                else:
                    stable_start = None

            last_frame = self._frame.copy()
            await asyncio.sleep(check_interval)

        print(f"{self.account['name']}: 等待页面稳定超时")
        return False