"""Application entry: starts the sampler, the local server and the window."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__
from .collector import Collector
from .server import DEFAULT_PORT, UI_DIR, start_server
from .settings import Settings, data_dir

# Sizes in device-independent pixels. The window toolkit applies the display's DPI itself.
WINDOW_SIZE = (1280, 860)
MINI_SIZE = (350, 500)


def _fit_to_display() -> None:
    """Shrink the start-up size if the screen is smaller than the default window."""
    global WINDOW_SIZE
    try:
        import ctypes
        from ctypes import wintypes
        scale = max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
        work = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)   # SPI_GETWORKAREA
        max_w = int((work.right - work.left) / scale) - 48
        max_h = int((work.bottom - work.top) / scale) - 48
        if max_w > 400 and max_h > 300:
            WINDOW_SIZE = (min(WINDOW_SIZE[0], max_w), min(WINDOW_SIZE[1], max_h))
    except Exception:
        pass


def _on_ui_thread(window, fn) -> None:
    """Run ``fn`` on the window's UI thread and wait for it.

    WinForms controls must only be touched from the thread that owns them. Doing
    it from another thread can block that thread on the UI thread while the UI
    thread is itself waiting to run Python code - a deadlock that freezes the app.
    """
    try:
        from System import Func, Type  # type: ignore

        def call():
            try:
                fn()
            except Exception:
                pass
            return None
        window.native.Invoke(Func[Type](call))
    except Exception:
        pass


class WindowApi:
    """Bridges UI actions (mini mode, always-on-top, focus) to the native window.

    Called from the HTTP server's threads; every change is applied on the UI thread.
    """

    def __init__(self) -> None:
        self.window = None
        self._mini = False
        self._normal_size = None
        self._pinned = False

    def _apply(self, fn) -> None:
        if self.window is not None:
            _on_ui_thread(self.window, fn)

    def set_on_top(self, on: bool) -> None:
        self._pinned = bool(on)

        def apply():
            if not self._mini:
                self.window.native.TopMost = self._pinned
        self._apply(apply)

    def focus(self) -> None:
        """Bring the window forward (used when the app is started a second time)."""
        def apply():
            import ctypes
            form = self.window.native
            if int(form.WindowState) == 1:          # FormWindowState.Minimized
                form.WindowState = type(form.WindowState)(0)
            form.Activate()
            ctypes.windll.user32.SetForegroundWindow(int(form.Handle.ToInt64()))
        self._apply(apply)

    def set_mini(self, on: bool) -> None:
        def apply():
            w = self.window
            if on == self._mini:
                return
            if on:
                self._normal_size = (w.width, w.height)
                w.resize(*MINI_SIZE)
                w.native.TopMost = True
            else:
                w.resize(*(self._normal_size or WINDOW_SIZE))
                w.native.TopMost = self._pinned
            self._mini = on
        self._apply(apply)


def _style_native_window(window) -> None:
    """Dark title bar + our icon on the WinForms window. Call on the UI thread."""
    try:
        import ctypes
        hwnd = int(window.native.Handle.ToInt64())
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
        caption = ctypes.c_int(0x000F0C0A)   # BGR of the app background
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
    except Exception:
        pass
    try:
        from System.Drawing import Icon  # type: ignore
        icon = UI_DIR / "assets" / "icon.ico"
        if icon.is_file():
            window.native.Icon = Icon(str(icon))
    except Exception:
        pass


def _run_window(url: str, api: WindowApi, settings: Settings, collector: Collector,
                start_minimized: bool = False) -> bool:
    try:
        import webview
    except Exception:
        return False
    try:
        # Group under our own taskbar identity instead of "Python".
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RigWatch.HardwareMonitor")
    except Exception:
        pass
    _fit_to_display()
    window = webview.create_window(
        "RigWatch", url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=(340, 400),
        background_color="#0a0c0f", on_top=bool(settings.get().get("always_on_top")),
        minimized=start_minimized,
    )
    api.window = window
    api._pinned = bool(settings.get().get("always_on_top"))
    try:
        from .strip import TaskbarStrip
        strip = TaskbarStrip(window, collector, settings)
    except Exception:
        strip = None              # the taskbar strip is optional

    def ui_setup():               # one hop to the UI thread for everything that touches the window
        _style_native_window(window)
        if strip is not None:
            strip.create()

    window.events.shown += lambda: _on_ui_thread(window, ui_setup)
    webview.start(storage_path=str(data_dir() / "webview"), private_mode=False)
    return True


def _run_edge_app(url: str, server) -> bool:
    """Fallback when the native window toolkit is unavailable: Edge app window."""
    candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    edge = next((c for c in candidates if c and os.path.isfile(c)), None)
    if edge:
        subprocess.Popen([edge, f"--app={url}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}"])
    else:
        webbrowser.open(url)
    # Stay alive while a page is connected; exit shortly after the last one closes.
    connected_once = False
    try:
        while True:
            time.sleep(1.0)
            with server.clients_lock:
                clients, last = server.stream_clients, server.last_client_seen
            if clients:
                connected_once = True
            elif connected_once and time.monotonic() - last > 8:
                return True
            elif not connected_once and time.monotonic() - last > 60:
                return True
    except KeyboardInterrupt:
        return True


def _focus_running_instance(port: int) -> bool:
    """If RigWatch is already open, bring that window forward instead of starting twice."""
    import urllib.request
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/ping", timeout=0.6) as resp:
            if json.loads(resp.read()).get("app") != "rigwatch":
                return False
        req = urllib.request.Request(f"{base}/api/window", data=b'{"focus": true}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return bool(json.loads(resp.read()).get("ok"))
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rigwatch", description="RigWatch - hardware monitor")
    parser.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    parser.add_argument("--server", action="store_true", help="run only the local API/UI server (no window)")
    parser.add_argument("--json", action="store_true", help="print one snapshot as JSON and exit")
    parser.add_argument("--minimized", action="store_true",
                        help="start minimised, showing only the taskbar strip")
    parser.add_argument("--port", type=int, default=None, help="local port (default 47115)")
    parser.add_argument("--interval", type=float, default=None, help="seconds between samples")
    parser.add_argument("--version", action="version", version=f"RigWatch {__version__}")
    args = parser.parse_args(argv)

    if not (args.json or args.server or args.browser) and _focus_running_instance(args.port or DEFAULT_PORT):
        return 0

    settings = Settings()
    interval = args.interval or settings.get()["interval"]
    collector = Collector(interval)
    collector.start()

    if args.json:
        deadline = time.time() + 15
        while time.time() < deadline:
            snap = collector.snapshot()
            if snap.get("ready") and snap.get("seq", 0) >= 2:   # second sample has real rates
                break
            time.sleep(0.1)
        print(json.dumps({"static": collector.static(), "snapshot": collector.snapshot()}, indent=2, default=str))
        collector.stop()
        return 0

    api = WindowApi()
    server = start_server(collector, settings, args.port, api)
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    try:
        if args.server:
            print(f"RigWatch is running at {url}  (Ctrl+C to stop)", flush=True)
            while True:
                time.sleep(3600)
        elif args.browser:
            webbrowser.open(url)
            print(f"RigWatch is running at {url}  (Ctrl+C to stop)", flush=True)
            while True:
                time.sleep(3600)
        elif not _run_window(url, api, settings, collector, args.minimized):
            _run_edge_app(url, server)
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
