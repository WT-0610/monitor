"""Launch the current local UI in a separate Windows preview window."""

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import webview


def round_window(window):
    hwnd = wintypes.HWND(window.native.Handle.ToInt64())
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return
    if user32.IsZoomed(hwnd):
        user32.SetWindowRgn(hwnd, None, True)
        return
    diameter = round(24 * (user32.GetDpiForWindow(hwnd) or 96) / 96)
    gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
    region = gdi32.CreateRoundRectRgn(
        0, 0, rect.right - rect.left, rect.bottom - rect.top,
        diameter, diameter,
    )
    if region and not user32.SetWindowRgn(hwnd, wintypes.HRGN(region), True):
        gdi32.DeleteObject(wintypes.HRGN(region))


def open_window(url: str) -> None:
    target = urlsplit(url)
    if target.scheme != "http" or target.netloc != f"127.0.0.1:{target.port}" or target.path not in ("", "/") or target.query or target.fragment:
        raise ValueError("Expected a local application URL")
    url = url.rstrip("/") + "/"
    with build_opener(ProxyHandler({})).open(url + "api/health", timeout=3):
        pass

    window = webview.create_window(
        "桔子监控台",
        url,
        width=1440,
        height=900,
        min_size=(900, 580),
        frameless=True,
        easy_drag=False,
        shadow=False,
        background_color="#f5f5f6",
    )
    window.expose(window.minimize, window.maximize, window.restore, window.destroy)
    window.events.shown += lambda: round_window(window)
    window.events.resized += lambda width, height: round_window(window)
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    script = (assets / "desktop_titlebar.js").read_text(encoding="utf-8")
    window.events.loaded += lambda: window.evaluate_js(script)
    webview.start(gui="edgechromium")
