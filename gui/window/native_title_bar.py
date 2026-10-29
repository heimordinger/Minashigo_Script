"""Keep the Windows-owned title bar in sync with the Qt application theme."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget


_FILTER_ATTR = "_minashigo_native_title_bar_filter"


def _colorref(hex_color: str) -> int:
    """Convert #RRGGBB to the BGR COLORREF expected by DWM."""
    value = hex_color.lstrip("#")
    red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return red | (green << 8) | (blue << 16)


def apply_native_title_bar_theme(window, theme: str) -> bool:
    """Ask DWM to use light or dark caption controls for ``window``.

    The call is deliberately best-effort: older Windows builds and non-Windows
    platforms simply keep their system default title bar.
    """
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(window.winId())
        dark = str(theme).lower() == "dark"
        enabled = wintypes.BOOL(1 if dark else 0)
        dwmapi = ctypes.windll.dwmapi

        # Windows 10 20H1+ uses 20; some earlier Windows 10 builds use 19.
        for attribute in (20, 19):
            result = dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                wintypes.DWORD(attribute),
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                applied = True
                break
        else:
            applied = False

        # Windows 11 supports exact caption, text and border colours.  Older
        # builds simply reject these attributes and retain immersive dark mode.
        colors = (
            (35, "#111317" if dark else "#f4f0e8"),  # DWMWA_CAPTION_COLOR
            (36, "#d9dde5" if dark else "#292722"),  # DWMWA_TEXT_COLOR
            (34, "#2b3038" if dark else "#d8d0c2"),  # DWMWA_BORDER_COLOR
        )
        for attribute, color in colors:
            value = wintypes.DWORD(_colorref(color))
            dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), wintypes.DWORD(attribute),
                ctypes.byref(value), ctypes.sizeof(value),
            )
        return applied
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return False


class NativeTitleBarThemeFilter(QObject):
    """Apply the current theme whenever any native top-level window appears."""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget):
            if watched.isWindow():
                from gui.styles.theme import current_theme_from_config

                apply_native_title_bar_theme(watched, current_theme_from_config())
        return False


def install_native_title_bar_theme_sync(app: QApplication | None = None) -> QObject | None:
    """Install one application-wide watcher for current and future windows."""
    app = app or QApplication.instance()
    if app is None:
        return None
    installed = getattr(app, _FILTER_ATTR, None)
    if installed is not None:
        return installed
    watcher = NativeTitleBarThemeFilter(app)
    app.installEventFilter(watcher)
    setattr(app, _FILTER_ATTR, watcher)
    sync_all_native_title_bars()
    return watcher


def sync_all_native_title_bars(theme: str | None = None) -> None:
    """Refresh every existing top-level window after a theme change."""
    app = QApplication.instance()
    if app is None:
        return
    if theme is None:
        from gui.styles.theme import current_theme_from_config

        theme = current_theme_from_config()
    for window in app.topLevelWidgets():
        apply_native_title_bar_theme(window, theme)
