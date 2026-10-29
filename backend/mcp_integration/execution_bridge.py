"""Bridge MCP execution tools to the live Controller runtime objects."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from pathlib import Path
import tempfile
from typing import Any, Literal
import uuid

import cv2
import numpy as np


TargetKind = Literal["auto", "browser", "window"]


class ExecutionToolError(RuntimeError):
    """Stable, user-facing failure raised by execution tools."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, Any]:
        return {"success": False, "error": {"code": self.code, "message": self.message}}


class ExecutionBridge:
    """Resolve an account target and execute actions on Controller's asyncio loop."""

    def __init__(self, controller):
        self._controller = controller

    def list_targets(self) -> dict[str, Any]:
        browser_names = set(getattr(self._controller, "_browser_instances", {}))
        window_names = set(getattr(self._controller, "_window_instances", {}))
        names = sorted(browser_names | window_names)
        targets = []
        for name in names:
            task_ctrl = getattr(self._controller, "_task_ctrls", {}).get(name)
            account = getattr(task_ctrl, "account", {}) if task_ctrl else {}
            targets.append(
                {
                    "account": name,
                    "preferred": str((account or {}).get("_target") or "auto"),
                    "browser_ready": name in browser_names,
                    "window_ready": name in window_names,
                }
            )
        return {"success": True, "targets": targets}

    def _resolve_target(self, account: str, target: TargetKind):
        account = str(account or "").strip()
        if not account:
            raise ExecutionToolError("INVALID_ARGUMENT", "account 不能为空")
        if target not in ("auto", "browser", "window"):
            raise ExecutionToolError("INVALID_ARGUMENT", f"不支持的 target: {target}")

        browsers = getattr(self._controller, "_browser_instances", {})
        windows = getattr(self._controller, "_window_instances", {})
        if target == "browser":
            resolved = browsers.get(account)
            resolved_kind = "browser"
        elif target == "window":
            resolved = windows.get(account)
            resolved_kind = "window"
        else:
            task_ctrl = getattr(self._controller, "_task_ctrls", {}).get(account)
            account_data = getattr(task_ctrl, "account", {}) if task_ctrl else {}
            prefer_window = (account_data or {}).get("_target") == "window"
            if prefer_window:
                resolved = windows.get(account) or browsers.get(account)
                resolved_kind = "window" if account in windows else "browser"
            else:
                resolved = browsers.get(account) or windows.get(account)
                resolved_kind = "browser" if account in browsers else "window"
        if resolved is None:
            raise ExecutionToolError(
                "TARGET_NOT_READY", f"账号 {account!r} 没有已就绪的 {target} 执行目标"
            )
        return resolved, resolved_kind

    @staticmethod
    def _unwrap(runtime):
        return getattr(runtime, "_obj", runtime)

    def _execution_state(self, account: str) -> dict[str, Any]:
        task_ctrl = getattr(self._controller, "_task_ctrls", {}).get(account)
        future = getattr(task_ctrl, "_future", None) if task_ctrl else None
        running = bool(future is not None and not future.done())
        paused = bool(getattr(task_ctrl, "_paused", False)) if task_ctrl else False
        stopped = bool(getattr(task_ctrl, "_stopped", False)) if task_ctrl else False
        return {
            "ready": True,
            "task_running": running,
            "paused": paused,
            "stopped": stopped,
            "input_allowed": not paused and not stopped,
        }

    async def invoke_local(self, account: str, target: TargetKind, method: str, **kwargs):
        """Execute on the controller loop without scheduling onto it again.

        Internal agents already run on that loop.  External MCP callers use
        ``_invoke`` below, which submits this same core operation thread-safely.
        """
        runtime, resolved_kind = self._resolve_target(account, target)
        fn = getattr(runtime, method, None)
        if not callable(fn):
            raise ExecutionToolError(
                "ACTION_NOT_SUPPORTED",
                f"{type(runtime).__name__} 不支持动作 {method}",
            )
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return {
            "success": True,
            "account": account,
            "target": resolved_kind,
            "action": method,
            "result": result,
        }

    async def _invoke(self, account: str, target: TargetKind, method: str, **kwargs):
        async def call():
            return await self.invoke_local(account, target, method, **kwargs)

        try:
            future = self._controller.submit(call())
            return await asyncio.wrap_future(future)
        except ExecutionToolError:
            raise
        except Exception as exc:
            raise ExecutionToolError("ACTION_FAILED", str(exc)) from exc

    async def click(self, account: str, x: int, y: int, target: TargetKind = "auto"):
        return await self._invoke(account, target, "click", x=int(x), y=int(y), pianyi=(0, 0))

    async def click_bbox(
        self,
        account: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        screenshot_width: int | None = None,
        screenshot_height: int | None = None,
        padding: int = 12,
        threshold: float = 0.88,
        target: TargetKind = "auto",
    ) -> dict[str, Any]:
        """Crop a target from a fresh frame and click it through image matching.

        The bbox is expressed in the screenshot coordinate space seen by the
        caller.  Supplying that screenshot's dimensions lets the bridge scale
        the box onto the target's current captured frame without trusting raw
        desktop coordinates.
        """
        async def call():
            return await self.click_bbox_local(
                account, x1, y1, x2, y2,
                screenshot_width=screenshot_width,
                screenshot_height=screenshot_height,
                padding=padding, threshold=threshold, target=target,
            )

        try:
            future = self._controller.submit(call())
            return await asyncio.wrap_future(future)
        except ExecutionToolError:
            raise
        except Exception as exc:
            raise ExecutionToolError("ACTION_FAILED", str(exc)) from exc

    async def click_bbox_local(
        self,
        account: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        screenshot_width: int | None = None,
        screenshot_height: int | None = None,
        padding: int = 12,
        threshold: float = 0.88,
        target: TargetKind = "auto",
    ) -> dict[str, Any]:
        """Local-loop implementation shared by the agent and MCP wrapper."""
        runtime, resolved_kind = self._resolve_target(account, target)
        padding = max(0, min(int(padding), 64))
        threshold = float(threshold)
        if not 0.5 <= threshold <= 1.0:
            raise ExecutionToolError("INVALID_ARGUMENT", "threshold 必须位于 0.5 到 1.0")
        if int(x2) <= int(x1) or int(y2) <= int(y1):
            raise ExecutionToolError("INVALID_ARGUMENT", "bbox 必须满足 x2>x1 且 y2>y1")
        if (screenshot_width is None) != (screenshot_height is None):
            raise ExecutionToolError(
                "INVALID_ARGUMENT", "screenshot_width 和 screenshot_height 必须同时提供"
            )
        if screenshot_width is not None and (
            int(screenshot_width) <= 0 or int(screenshot_height or 0) <= 0
        ):
            raise ExecutionToolError("INVALID_ARGUMENT", "截图尺寸必须为正数")

        async def crop_and_click():
            capture_name = (
                "update_game_frame"
                if resolved_kind == "browser" and callable(getattr(runtime, "update_game_frame", None))
                else "update_frame"
            )
            capture = getattr(runtime, capture_name, None)
            click_image = getattr(runtime, "click_image", None)
            if not callable(capture) or not callable(click_image):
                raise ExecutionToolError(
                    "ACTION_NOT_SUPPORTED",
                    f"{type(runtime).__name__} 不支持 bbox 图像点击",
                )
            frame = capture()
            if inspect.isawaitable(frame):
                frame = await frame
            if not isinstance(frame, np.ndarray) or frame.ndim < 2:
                raise ExecutionToolError("CAPTURE_FAILED", "无法取得目标截图")

            height, width = frame.shape[:2]
            source_w = int(screenshot_width or width)
            source_h = int(screenshot_height or height)
            scale_x = width / source_w
            scale_y = height / source_h
            bx1 = int(round(int(x1) * scale_x))
            by1 = int(round(int(y1) * scale_y))
            bx2 = int(round(int(x2) * scale_x))
            by2 = int(round(int(y2) * scale_y))
            if bx1 < 0 or by1 < 0 or bx2 > width or by2 > height:
                raise ExecutionToolError("INVALID_ARGUMENT", "bbox 超出截图范围")
            if bx2 - bx1 < 8 or by2 - by1 < 8:
                raise ExecutionToolError("INVALID_ARGUMENT", "bbox 太小，至少需要 8x8 像素")

            cx1 = max(0, bx1 - padding)
            cy1 = max(0, by1 - padding)
            cx2 = min(width, bx2 + padding)
            cy2 = min(height, by2 + padding)
            crop = frame[cy1:cy2, cx1:cx2].copy()
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
            if float(np.std(gray)) < 3.0:
                raise ExecutionToolError(
                    "LOW_INFORMATION_TEMPLATE", "目标裁剪信息量过低，拒绝图像点击"
                )

            temp_dir = Path(tempfile.gettempdir()) / "minashigo_mcp_click"
            temp_dir.mkdir(parents=True, exist_ok=True)
            template_path = temp_dir / f"target_{uuid.uuid4().hex}.png"
            if not cv2.imwrite(str(template_path), crop):
                raise ExecutionToolError("CAPTURE_FAILED", "临时目标图保存失败")
            try:
                result = click_image(
                    img_path=template_path,
                    threshold=threshold,
                    match_select="best",
                )
                if inspect.isawaitable(result):
                    result = await result
            finally:
                with contextlib.suppress(OSError):
                    template_path.unlink()
            return {
                "clicked": bool(result),
                "bbox_on_current_frame": [bx1, by1, bx2, by2],
                "template_bbox": [cx1, cy1, cx2, cy2],
                "captured_size": [width, height],
                "threshold": threshold,
            }

        result = await crop_and_click()
        return {
            "success": bool(result.get("clicked")),
            "account": account,
            "target": resolved_kind,
            "action": "click_bbox",
            "result": result,
        }

    async def double_click(self, account: str, x: int, y: int, target: TargetKind = "auto"):
        return await self._invoke(account, target, "double_click", x=int(x), y=int(y))

    async def right_click(self, account: str, x: int, y: int, target: TargetKind = "auto"):
        return await self._invoke(account, target, "right_click", x=int(x), y=int(y))

    async def scroll(
        self,
        account: str,
        delta_y: int,
        delta_x: int = 0,
        x: int | None = None,
        y: int | None = None,
        target: TargetKind = "auto",
    ):
        _, resolved_kind = self._resolve_target(account, target)
        if resolved_kind == "window":
            # Win32 wheel uses one vertical delta and optional client coordinates.
            kwargs = {"delta": int(delta_y), "x": int(x or 0), "y": int(y or 0)}
        else:
            kwargs = {"delta_y": int(delta_y), "delta_x": int(delta_x)}
            if x is not None and y is not None:
                kwargs.update(x=int(x), y=int(y))
        return await self._invoke(account, target, "scroll", **kwargs)

    async def type_text(self, account: str, text: str, target: TargetKind = "auto"):
        if len(text) > 4000:
            raise ExecutionToolError("INVALID_ARGUMENT", "单次输入文本不能超过 4000 个字符")
        return await self._invoke(account, target, "type_text", text=text)

    async def press_key(self, account: str, key: str, target: TargetKind = "auto"):
        _, resolved_kind = self._resolve_target(account, target)
        method = "key_click" if resolved_kind == "window" else "press_key"
        return await self._invoke(account, target, method, key=key)

    async def get_target_info(
        self, account: str, target: TargetKind = "auto"
    ) -> dict[str, Any]:
        runtime, resolved_kind = self._resolve_target(account, target)
        actual = self._unwrap(runtime)

        async def inspect_target():
            common = {
                "success": True,
                "account": account,
                "target": resolved_kind,
                "runtime_type": type(actual).__name__,
                "execution": self._execution_state(account),
            }
            if resolved_kind == "browser":
                browser = getattr(actual, "_browser", actual)
                page = getattr(browser, "page", None)
                if page is None or page.is_closed():
                    common.update(
                        page={"closed": True, "loading": False},
                        execution={**common["execution"], "ready": False, "input_allowed": False},
                    )
                    return common
                from backend.browser.game_frame_capture import inspect_game_viewport

                geometry = await inspect_game_viewport(page)
                raw_url = str(page.url or "")
                safe_url = raw_url.split("?", 1)[0].split("#", 1)[0]
                common.update(
                    page={
                        "title": await page.title(),
                        "url": safe_url,
                        "closed": False,
                        "loading": geometry.get("ready_state") != "complete",
                    },
                    viewport=geometry.get("viewport"),
                    document=geometry.get("document"),
                    scroll=geometry.get("scroll"),
                    game_frame=geometry.get("game_frame"),
                    capture={
                        "mode": str(getattr(browser, "_frame_capture_mode", None) or "full"),
                        "device_scale_factor": float(
                            getattr(browser, "device_pixel_ratio", 1.0) or 1.0
                        ),
                    },
                    coordinate_space={
                        "input": "game_frame_when_cropped_otherwise_viewport",
                        "transform_managed_by_server": False,
                    },
                )
                return common

            win = getattr(actual, "_target", actual)
            client = dict(getattr(win, "client_rect", {}) or {})
            minimized = bool(getattr(win, "is_effectively_minimized", False))
            visible = bool(getattr(win, "is_visible", False))
            common.update(
                window={
                    "hwnd": int(getattr(win, "hwnd", 0) or 0),
                    "title": str(getattr(win, "title", "") or ""),
                    "client_width": int(client.get("width", 0)),
                    "client_height": int(client.get("height", 0)),
                    "visible": visible,
                    "minimized": minimized,
                    "dpi_scale": float(getattr(win, "dpi_scale", 1.0) or 1.0),
                },
                coordinate_space={"input": "client", "transform_managed_by_server": True},
            )
            common["execution"].update(
                ready=visible and not minimized,
                input_allowed=visible and not minimized and common["execution"]["input_allowed"],
            )
            return common

        try:
            return await asyncio.wrap_future(self._controller.submit(inspect_target()))
        except Exception as exc:
            raise ExecutionToolError("TARGET_INFO_FAILED", str(exc)) from exc

    async def center_game(
        self, account: str, target: TargetKind = "browser"
    ) -> dict[str, Any]:
        runtime, resolved_kind = self._resolve_target(account, target)
        if resolved_kind != "browser":
            raise ExecutionToolError("ACTION_NOT_SUPPORTED", "游戏区域居中仅支持浏览器目标")
        actual = self._unwrap(runtime)

        async def center():
            browser = getattr(actual, "_browser", actual)
            page = getattr(browser, "page", None)
            if page is None or page.is_closed():
                raise ExecutionToolError("TARGET_NOT_READY", "浏览器页面未就绪")
            from backend.browser.game_frame_capture import center_game_viewport

            result = await center_game_viewport(page)
            if hasattr(actual, "invalidate_frame"):
                actual.invalidate_frame()
            return {"success": bool(result.get("found")), "account": account, **result}

        try:
            return await asyncio.wrap_future(self._controller.submit(center()))
        except ExecutionToolError:
            raise
        except Exception as exc:
            raise ExecutionToolError("CENTER_GAME_FAILED", str(exc)) from exc

    async def screenshot(
        self,
        account: str,
        target: TargetKind = "auto",
        *,
        fmt: str = "jpeg",
        quality: int = 80,
        max_side: int | None = None,
    ) -> dict[str, Any]:
        """Capture a frame for an external caller (submits onto the loop)."""

        async def call():
            return await self.screenshot_local(
                account, target, fmt=fmt, quality=quality, max_side=max_side
            )

        try:
            future = self._controller.submit(call())
            return await asyncio.wrap_future(future)
        except ExecutionToolError:
            raise
        except Exception as exc:
            raise ExecutionToolError("CAPTURE_FAILED", str(exc)) from exc

    async def screenshot_local(
        self,
        account: str,
        target: TargetKind = "auto",
        *,
        fmt: str = "jpeg",
        quality: int = 80,
        max_side: int | None = None,
    ) -> dict[str, Any]:
        """Grab the current frame and describe how its pixels map back to clicks.

        The image is taken from the same capture call that ``click_bbox_local``
        uses, so an image-space bbox can be returned verbatim with
        ``screenshot_width/height`` from ``encoded_size``.  ``execution_click``
        instead takes the runtime's own input space; the ``coordinate_space``
        block spells out that conversion for browser targets.
        """
        runtime, resolved_kind = self._resolve_target(account, target)
        fmt = str(fmt or "jpeg").strip().lower()
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in ("jpeg", "png"):
            raise ExecutionToolError("INVALID_ARGUMENT", "format 只支持 jpeg 或 png")
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            raise ExecutionToolError("INVALID_ARGUMENT", "quality 必须是整数") from None
        if not 30 <= quality <= 95:
            raise ExecutionToolError("INVALID_ARGUMENT", "quality 必须位于 30 到 95")
        if max_side is not None:
            try:
                max_side = int(max_side)
            except (TypeError, ValueError):
                raise ExecutionToolError("INVALID_ARGUMENT", "max_side 必须是整数") from None
            if max_side < 64:
                raise ExecutionToolError("INVALID_ARGUMENT", "max_side 不能小于 64")

        capture_name = (
            "update_game_frame"
            if resolved_kind == "browser" and callable(getattr(runtime, "update_game_frame", None))
            else "update_frame"
        )
        capture = getattr(runtime, capture_name, None)
        if not callable(capture):
            raise ExecutionToolError(
                "ACTION_NOT_SUPPORTED", f"{type(runtime).__name__} 不支持截图"
            )
        frame = capture()
        if inspect.isawaitable(frame):
            frame = await frame
        if not isinstance(frame, np.ndarray) or frame.ndim < 2:
            raise ExecutionToolError("CAPTURE_FAILED", "无法取得目标截图")

        width, height = int(frame.shape[1]), int(frame.shape[0])
        if width <= 0 or height <= 0:
            raise ExecutionToolError("CAPTURE_FAILED", "截图尺寸无效")
        encoded = frame
        if max_side is not None and max(width, height) > max_side:
            ratio = max_side / float(max(width, height))
            encoded = cv2.resize(
                frame,
                (max(1, int(round(width * ratio))), max(1, int(round(height * ratio)))),
                interpolation=cv2.INTER_AREA,
            )
        enc_h, enc_w = int(encoded.shape[0]), int(encoded.shape[1])

        if fmt == "png":
            ok, buffer = cv2.imencode(".png", encoded, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        else:
            ok, buffer = cv2.imencode(".jpg", encoded, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ExecutionToolError("CAPTURE_FAILED", "截图编码失败")
        data = buffer.tobytes()
        down_x = width / float(enc_w)
        down_y = height / float(enc_h)

        payload: dict[str, Any] = {
            "success": True,
            "account": account,
            "target": resolved_kind,
            "action": "screenshot",
            "captured_size": [width, height],
            "encoded_size": [enc_w, enc_h],
            "downscale": [round(down_x, 6), round(down_y, 6)],
            "format": fmt,
            "mime_type": "image/png" if fmt == "png" else "image/jpeg",
            "bytes": len(data),
            "image_bytes": data,
        }
        if resolved_kind == "browser":
            browser = getattr(runtime, "_browser", runtime)
            mode = str(getattr(browser, "_frame_capture_mode", None) or "full")
            origin = getattr(browser, "_frame_origin_css", None) or (0.0, 0.0)
            dpr = float(getattr(browser, "device_pixel_ratio", 1.0) or 1.0)
            css_w, css_h = await self._browser_frame_scale(browser, mode, width, height)
            payload.update(
                capture_mode=mode,
                game_canvas=dict(getattr(browser, "_game_canvas_meta", None) or {}),
                coordinate_space={
                    "image": "browser_capture_frame",
                    "image_pixels": [enc_w, enc_h],
                    "origin_css": [round(float(origin[0]), 3), round(float(origin[1]), 3)],
                    "css_per_pixel": [
                        round(css_w * down_x, 6),
                        round(css_h * down_y, 6),
                    ],
                    "device_scale_factor": dpr,
                    "execution_click": "css = origin_css + image_xy * css_per_pixel",
                    "execution_click_bbox": (
                        "bbox in image pixels; pass screenshot_width/height from encoded_size"
                    ),
                    "transform_managed_by_server": False,
                },
            )
        else:
            payload.update(
                capture_mode="window_client",
                coordinate_space={
                    "image": "window_frame",
                    "image_pixels": [enc_w, enc_h],
                    "frame_per_pixel": [round(down_x, 6), round(down_y, 6)],
                    "execution_click": (
                        "frame = image_xy * frame_per_pixel; the server maps frame to client"
                    ),
                    "execution_click_bbox": (
                        "bbox in image pixels; pass screenshot_width/height from encoded_size"
                    ),
                    "transform_managed_by_server": True,
                },
            )
        return payload

    @staticmethod
    async def _browser_frame_scale(browser, mode: str, width: int, height: int):
        """CSS pixels per captured pixel for the frame currently stored on the browser."""
        dpr = float(getattr(browser, "device_pixel_ratio", 1.0) or 1.0)
        fallback = 1.0 / dpr if dpr > 0 else 1.0
        if mode == "canvas_buffer":
            css_size = getattr(browser, "_frame_css_size", None)
            buffer_size = getattr(browser, "_frame_buffer_size", None)
            if css_size and buffer_size and buffer_size[0] > 0 and buffer_size[1] > 0:
                return (
                    float(css_size[0]) / float(buffer_size[0]),
                    float(css_size[1]) / float(buffer_size[1]),
                )
            return fallback, fallback
        if mode == "element" and width > 0 and height > 0:
            # Element screenshots do not expose their pixel ratio; measure the
            # live element box instead of assuming the device scale factor.
            try:
                from backend.browser.game_frame_capture import inspect_game_viewport

                page = getattr(browser, "page", None)
                if page is not None:
                    geometry = await inspect_game_viewport(page, box_timeout_ms=1200)
                    box = (geometry.get("game_frame") or {}).get("bounds") or {}
                    box_w = float(box.get("width") or 0)
                    box_h = float(box.get("height") or 0)
                    if box_w > 0 and box_h > 0:
                        return box_w / float(width), box_h / float(height)
            except Exception:
                pass
        return fallback, fallback
