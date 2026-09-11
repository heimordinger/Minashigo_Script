# -*- coding: utf-8 -*-
"""广告拦截 + 游戏加载在途请求跟踪（挂在 Browser / Lifecycle）。"""
from __future__ import annotations

import time
from typing import Any

from backend.browser.net_optimize import is_game_network_url, should_block_url


class NetOptimizeMixin:
    """在 connect() 后由 Lifecycle 调用 install_net_optimize()。"""

    def _net_opt_init_fields(self) -> None:
        if getattr(self, "_net_opt_ready", False):
            return
        self._net_opt_ready = False
        self._ad_block_enabled = True
        self._net_blocked_count = 0
        self._net_inflight: dict[int, tuple[str, float]] = {}
        self._net_route_installed = False

    async def install_net_optimize(self) -> None:
        """对当前 Playwright context 安装 route 拦截与在途跟踪。"""
        self._net_opt_init_fields()
        ctx = getattr(self, "context", None)
        if ctx is None:
            return
        if self._net_route_installed:
            return

        async def _on_route(route: Any) -> None:
            try:
                url = route.request.url
                if getattr(self, "_ad_block_enabled", True) and should_block_url(url):
                    self._net_blocked_count = int(
                        getattr(self, "_net_blocked_count", 0)
                    ) + 1
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        def _on_request(req: Any) -> None:
            try:
                url = req.url
                if is_game_network_url(url):
                    self._net_inflight[id(req)] = (url, time.monotonic())
            except Exception:
                pass

        def _on_request_done(req: Any) -> None:
            try:
                self._net_inflight.pop(id(req), None)
            except Exception:
                pass

        try:
            await ctx.route("**/*", _on_route)
            ctx.on("request", _on_request)
            ctx.on("requestfinished", _on_request_done)
            ctx.on("requestfailed", _on_request_done)
            self._net_route_installed = True
            self._net_opt_ready = True
            log = getattr(self, "_log", None)
            if callable(log):
                log("[NET] 广告拦截 + 游戏在途跟踪已启用")
        except Exception as e:
            log = getattr(self, "_log", None)
            if callable(log):
                try:
                    from core.logging.events import LogLevel

                    log(f"[NET] 安装失败: {e}", level=LogLevel.WARNING)
                except Exception:
                    log(f"[NET] 安装失败: {e}")

    def set_ad_block_enabled(self, enabled: bool) -> None:
        self._ad_block_enabled = bool(enabled)

    def game_net_inflight(self) -> list[str]:
        """当前未完成的游戏相关 URL（新→旧）。"""
        items = list(getattr(self, "_net_inflight", {}).values())
        items.sort(key=lambda x: x[1], reverse=True)
        return [u for u, _ in items]

    def game_net_inflight_count(self) -> int:
        return len(getattr(self, "_net_inflight", {}) or {})

    def game_net_busy(self, *, min_count: int = 1) -> bool:
        return self.game_net_inflight_count() >= min_count

    def ad_block_stats(self) -> dict:
        return {
            "enabled": bool(getattr(self, "_ad_block_enabled", False)),
            "blocked": int(getattr(self, "_net_blocked_count", 0)),
            "inflight": self.game_net_inflight_count(),
        }
