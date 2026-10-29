"""MCP server exposing the simple execution-only Minashigo tools."""

from __future__ import annotations

import threading
from typing import Literal

from .execution_bridge import ExecutionBridge, ExecutionToolError


def build_execution_server(controller):
    # Import lazily so the main application can still start with MCP disabled.
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Image

    bridge = ExecutionBridge(controller)
    server = MCPServer(
        "minashigo-execution",
        instructions=(
            "Execute actions only on an explicitly selected Minashigo account target. "
            "Call execution_screenshot to obtain the target's current frame; it returns the "
            "image plus the coordinate mapping. Send a bbox measured on that image to "
            "execution_click_bbox (with screenshot_width/height from encoded_size) instead of "
            "guessing absolute coordinates with execution_click. These tools do not interpret "
            "the screen for you."
        ),
    )

    async def guarded(awaitable):
        try:
            return await awaitable
        except ExecutionToolError as exc:
            return exc.as_dict()

    @server.tool()
    def execution_list_targets() -> dict:
        """List account targets currently ready for execution."""
        return bridge.list_targets()

    @server.tool()
    async def execution_get_target_info(
        account: str, target: Literal["auto", "browser", "window"] = "auto"
    ) -> dict:
        """Inspect target identity, page/window geometry, scrolling, and execution readiness."""
        return await guarded(bridge.get_target_info(account, target))

    @server.tool()
    async def execution_center_game(
        account: str, target: Literal["auto", "browser"] = "browser"
    ) -> dict:
        """Center the browser GameCanvas vertically and return before/after scroll progress."""
        return await guarded(bridge.center_game(account, target))

    @server.tool()
    async def execution_screenshot(
        account: str,
        target: Literal["auto", "browser", "window"] = "auto",
        format: Literal["jpeg", "png"] = "jpeg",
        quality: int = 80,
        max_side: int | None = None,
    ) -> list:
        """Capture the target's current frame as an image plus its coordinate mapping.

        The image is taken from the same capture call as execution_click_bbox, so a
        bbox measured on this image can be sent straight back with
        screenshot_width/screenshot_height from `encoded_size`. `execution_click`
        expects the runtime's own input space instead: for browser targets use
        `origin_css + image_xy * css_per_pixel`, for window targets scale image
        pixels by `frame_per_pixel`. Pass `max_side` to downscale large frames; the
        metadata already folds that scale into the mapping.
        """
        payload = await guarded(
            bridge.screenshot(
                account, target, fmt=format, quality=quality, max_side=max_side
            )
        )
        data = payload.pop("image_bytes", None)
        if not payload.get("success") or data is None:
            return payload
        return [payload, Image(data=data, format=payload.get("format") or "jpeg")]

    @server.tool()
    async def execution_click(
        account: str, x: int, y: int, target: Literal["auto", "browser", "window"] = "auto"
    ) -> dict:
        """Click raw client coordinates; prefer execution_click_bbox for visual targets."""
        return await guarded(bridge.click(account, x, y, target))

    @server.tool()
    async def execution_click_bbox(
        account: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        screenshot_width: int | None = None,
        screenshot_height: int | None = None,
        padding: int = 12,
        threshold: float = 0.88,
        target: Literal["auto", "browser", "window"] = "auto",
    ) -> dict:
        """Crop the described control locally, then find and click it by image.

        Coordinates refer to the screenshot seen by the caller. Pass that
        screenshot's dimensions when it differs from the target capture size.
        This avoids a second model call and never keeps the temporary crop.
        """
        return await guarded(bridge.click_bbox(
            account, x1, y1, x2, y2,
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
            padding=padding,
            threshold=threshold,
            target=target,
        ))

    @server.tool()
    async def execution_double_click(
        account: str, x: int, y: int, target: Literal["auto", "browser", "window"] = "auto"
    ) -> dict:
        """Double-click client coordinates on an account's selected target."""
        return await guarded(bridge.double_click(account, x, y, target))

    @server.tool()
    async def execution_right_click(
        account: str, x: int, y: int, target: Literal["auto", "browser", "window"] = "auto"
    ) -> dict:
        """Right-click client coordinates on an account's selected target."""
        return await guarded(bridge.right_click(account, x, y, target))

    @server.tool()
    async def execution_scroll(
        account: str,
        delta_y: int,
        delta_x: int = 0,
        x: int | None = None,
        y: int | None = None,
        target: Literal["auto", "browser", "window"] = "auto",
    ) -> dict:
        """Scroll the selected target, optionally after moving to client coordinates."""
        return await guarded(bridge.scroll(account, delta_y, delta_x, x, y, target))

    @server.tool()
    async def execution_type_text(
        account: str, text: str, target: Literal["auto", "browser", "window"] = "auto"
    ) -> dict:
        """Type text into the currently focused control on the selected target."""
        return await guarded(bridge.type_text(account, text, target))

    @server.tool()
    async def execution_press_key(
        account: str, key: str, target: Literal["auto", "browser", "window"] = "auto"
    ) -> dict:
        """Press one named key on the selected target."""
        return await guarded(bridge.press_key(account, key, target))

    return server


def start_execution_server(
    controller,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> threading.Thread:
    """Start the local Streamable HTTP server in a daemon thread."""
    server = build_execution_server(controller)

    def run() -> None:
        server.run(transport="streamable-http", host=host, port=port)

    thread = threading.Thread(target=run, name="minashigo-mcp", daemon=True)
    thread.start()
    return thread
