# backend/browser/mixins/image_matching.py
import asyncio
from pathlib import Path
from typing import Union

from core.match.match_result import MatchResult


class ImageMatchingMixin:
    """图像匹配相关方法混入类"""

    # 子类可覆盖；默认开启历史热点 ROI
    use_hotspot_roi: bool = True

    async def match_image(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_select: str = "best",
            quiet: bool = False,
            match_mode: str = "image",
            pixel_tol: float = 8.0,
            use_hotspot_roi: bool | None = None,
    ):
        if not quiet:
            print(f"[match_image] _frame is None? {self._frame is None}")
            print(f"[match_image] _frame_ts={self._frame_ts}")
            if self._frame is not None:
                print(f"[match_image] frame shape={self._frame.shape}, dtype={self._frame.dtype}")
            print(f"[match_image] img_path type={type(img_path)}, is_base64={str(img_path).startswith('data:image')}")

        if self._frame is None:
            if not quiet:
                print(f"[match_image] _frame为None，调用update_frame()")
            await self.update_frame()
            if not quiet:
                print(f"[match_image] update_frame后 frame shape={self._frame.shape}")

        # 不转换 base64 数据 URL 为 Path（Windows 上 Path 会将 / 转为 \，破坏 data:image 前缀判断）
        orig_path = img_path
        if not str(img_path).startswith("data:image"):
            img_path = Path(img_path)
            if not quiet:
                print(f"[match_image] 转换为Path: {img_path}")

        mode = (match_mode or "image").lower()
        if mode not in ("image", "pixel"):
            mode = "image"
        mtype = "pixel" if mode == "pixel" else "image"

        if not quiet:
            print(
                f"[match_image] 开始匹配: template={str(img_path)[:80]}, "
                f"threshold={threshold}, mode={mode}"
            )
        if not quiet:
            self._emit_match_hud(str(orig_path), "matching")

        from backend.matcher.hotspot_roi import (
            adaptive_match,
            normalize_template_key,
            resolve_capture_mode,
        )

        hotspot_on = (
            self.use_hotspot_roi if use_hotspot_roi is None else bool(use_hotspot_roi)
        )
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: adaptive_match(
                self.matcher,
                self._frame,
                img_path,
                threshold=threshold,
                match_type=mtype,
                use_color_check=use_color_check if mode == "image" else False,
                match_select=match_select,
                use_orb=(mode == "image"),
                pixel_tol=pixel_tol,
                template_key=normalize_template_key(orig_path),
                capture_mode=resolve_capture_mode(self),
                enabled=hotspot_on,
                multi=False,
            ),
        )

        if not quiet:
            print(f"{self.account['name']}: 图片匹配[{img_path}]:({result.x}, {result.y}), {result.score}")

        if result.x is None:
            if not quiet:
                print(f"[match_image] ❌ 匹配无结果, score={result.score}")
            if not quiet:
                self._emit_match_hud(str(orig_path), "fail", result.score)
            return MatchResult(x=None, y=None, max_val=result.score, match_success=False)

        x, y = self.device_to_css(result.x, result.y)
        ok = result.score >= threshold
        if not quiet:
            if ok:
                print(
                    f"[match_image] ✅ 匹配成功: img=({result.x},{result.y}) -> "
                    f"css=({x},{y}), score={result.score} (>= {threshold})"
                )
            else:
                print(
                    f"[match_image] ⚠️ 有候选但低于阈值: img=({result.x},{result.y}) -> "
                    f"css=({x},{y}), score={result.score} < {threshold} → 视为未命中"
                )
        if not quiet:
            self._emit_match_hud(
                str(orig_path), "ok" if ok else "fail", result.score,
                x=x, y=y,
            )

        return MatchResult(x=x, y=y, max_val=result.score, match_success=ok)

    async def match_images_parallel(
            self,
            specs: list[tuple],
            *,
            quiet: bool = True,
    ) -> list[MatchResult]:
        """对当前帧并行匹配多张模板。spec: (img_path, threshold) 或 (img_path, threshold, kwargs)。"""
        if self._frame is None:
            await self.update_frame()

        async def _one(spec: tuple) -> MatchResult:
            path, threshold, *rest = spec
            kw = rest[0] if rest else {}
            return await self.match_image(
                path,
                threshold=threshold,
                quiet=quiet,
                **kw,
            )

        return list(await asyncio.gather(*[_one(s) for s in specs]))

    def _emit_match_hud(self, img_path: str, status: str, score=None,
                        action: str = "match", x=None, y=None):
        ctrl = getattr(self, "controller", None)
        if not ctrl or not hasattr(ctrl, "emit_match_event"):
            return
        try:
            account = self.account["name"]
        except Exception:
            return
        ctrl.emit_match_event(
            account=account,
            img_path=img_path,
            status=status,
            score=score,
            action=action,
            x=x,
            y=y,
        )

    async def match_image_multi(
            self,
            img_path: Union[str, Path],
            threshold: float = 0.9,
            use_color_check: bool = False,
            match_mode: str = "image",
            pixel_tol: float = 8.0,
            use_hotspot_roi: bool | None = None,
    ):
        if self._frame is None:
            await self.update_frame()

        orig_path = img_path
        if not str(img_path).startswith("data:image"):
            img_path = Path(img_path)

        mode = (match_mode or "image").lower()
        if mode not in ("image", "pixel"):
            mode = "image"
        mtype = "pixel_multi" if mode == "pixel" else "image_multi"

        from backend.matcher.hotspot_roi import (
            adaptive_match,
            normalize_template_key,
            resolve_capture_mode,
        )

        hotspot_on = (
            self.use_hotspot_roi if use_hotspot_roi is None else bool(use_hotspot_roi)
        )
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None,
            lambda: adaptive_match(
                self.matcher,
                self._frame,
                img_path,
                threshold=threshold,
                match_type=mtype,
                use_color_check=use_color_check if mode == "image" else False,
                use_orb=(mode == "image"),
                pixel_tol=pixel_tol,
                template_key=normalize_template_key(orig_path),
                capture_mode=resolve_capture_mode(self),
                enabled=hotspot_on,
                multi=True,
            ),
        )

        if not results:
            return []

        converted = []
        for r in results:
            if isinstance(r, dict):
                rx, ry, sc = r["x"], r["y"], r["score"]
            else:
                rx, ry, sc = r.x, r.y, r.max_val
            x, y = self.device_to_css(rx, ry)
            converted.append({
                "x": x,
                "y": y,
                "score": sc,
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
            match_mode: str = "image",
            pixel_tol: float = 8.0,
    ):
        import time
        _t0 = time.time()
        print(f"[Browser.click_image] entered t=0", flush=True)

        match = await self.match_image(
            img_path=img_path,
            threshold=threshold,
            use_color_check=use_color_check,
            match_select=match_select,
            match_mode=match_mode,
            pixel_tol=pixel_tol,
        )
        print(f"[Browser.click_image] match_image done t={time.time()-_t0:.3f}s match.x={match.x} match.y={match.y}", flush=True)

        # 必须看 match_success / __bool__：低于阈值时仍可能带 x,y，不得点击
        if not match or match.x is None:
            print(f"[Browser.click_image] match失败，返回False", flush=True)
            self._emit_match_hud(
                str(img_path), "fail",
                getattr(match, "max_val", None) or getattr(match, "score", None),
                action="click",
            )
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
        self._emit_match_hud(
            str(img_path), "ok", match.max_val,
            action="click", x=x, y=y,
        )

        return True
