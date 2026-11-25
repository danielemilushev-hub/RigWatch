"""Taskbar strip: live numbers parked on the taskbar while the window is minimised.

Windows 11 removed the deskband API, so third-party code cannot add an item
*inside* the taskbar any more. This is the closest thing: a borderless,
always-on-top strip the height of the taskbar, parked next to the weather
widget. Click it to bring the window back, drag it anywhere (the position is
remembered), right-click for a menu.

Everything here runs on the window's UI thread, driven by one WinForms timer,
so there is no cross-thread UI access.
"""

from __future__ import annotations

import ctypes
import re
import winreg
from ctypes import wintypes

# identity colours (same as the UI's dark palette)
CPU, MEM, GPU, PWR = (0x39, 0x87, 0xE5), (0x19, 0x9E, 0x70), (0xD9, 0x59, 0x26), (0x90, 0x85, 0xE9)
BG, INK, MUTED, EDGE = (0x14, 0x17, 0x1C), (0xF1, 0xF3, 0xF6), (0x8A, 0x93, 0xA1), (0x2E, 0x34, 0x3E)


# ------------------------------------------------------------ pure helpers
def short_gpu_label(name: str | None, index: int) -> str:
    """'AMD Radeon RX 7800 XT' -> '7800'. The model number tells cards apart."""
    tokens = [t for t in (name or "").split() if any(c.isdigit() for c in t)]
    if tokens:
        return re.sub(r"[^0-9A-Za-z]", "", tokens[-1])[:5] or f"GPU{index + 1}"
    return f"GPU{index + 1}"


def _temp(value, unit: str) -> str | None:
    if value is None:
        return None
    return f"{value * 9 / 5 + 32:.0f}°" if unit == "F" else f"{value:.0f}°"


def build_fields(snap: dict, temp_unit: str = "C") -> list[dict]:
    """What the strip shows: label, colour, value text and an optional temperature.

    A reading that is unavailable has ``value`` None and is drawn as "N/A".
    """
    cpu = snap.get("cpu") or {}
    mem = snap.get("memory") or {}
    power = snap.get("power") or {}
    fields = [
        {"label": "CPU", "color": CPU, "wide": cpu.get("temp_c") is not None,
         "value": None if cpu.get("util") is None else f"{cpu['util']:.0f}%",
         "extra": _temp(cpu.get("temp_c"), temp_unit)},
        {"label": "RAM", "color": MEM, "wide": False,
         "value": None if mem.get("pct") is None else f"{mem['pct']:.0f}%", "extra": None},
    ]
    for i, g in enumerate(snap.get("gpus") or []):
        fields.append({"label": short_gpu_label(g.get("name"), i), "color": GPU, "wide": True,
                       "value": None if g.get("util") is None else f"{g['util']:.0f}%",
                       "extra": _temp(g.get("temp_c"), temp_unit)})
    fps = snap.get("fps") or {}
    if fps.get("available"):
        # The program in use is not always drawing; that is a dash, not a missing sensor.
        value = fps.get("value")
        fields.append({"label": "FPS", "color": GPU, "wide": False, "extra": None, "placeholder": "–",
                       "value": None if value is None else f"{value:.0f}"})
    total = power.get("total_w")
    fields.append({"label": "WATT", "color": PWR, "wide": False,
                   "value": None if total is None else f"{total:.0f}W", "extra": None})
    return fields


# ------------------------------------------------------------ Win32 helpers
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", ctypes.c_void_p),
                ("uCallbackMessage", ctypes.c_uint32), ("uEdge", ctypes.c_uint32),
                ("rc", _RECT), ("lParam", ctypes.c_ssize_t)]


ABM_GETTASKBARPOS = 5
ABE_TOP, ABE_BOTTOM = 1, 3
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]


def taskbar_rect():
    """(left, top, right, bottom, edge) of the primary taskbar, or None."""
    try:
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.SHAppBarMessage.argtypes = [ctypes.c_uint32, ctypes.POINTER(_APPBARDATA)]
        shell.SHAppBarMessage.restype = ctypes.c_ssize_t
        data = _APPBARDATA()
        data.cbSize = ctypes.sizeof(_APPBARDATA)
        if not shell.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
            return None
        r = data.rc
        return (r.left, r.top, r.right, r.bottom, data.uEdge)
    except Exception:
        return None


def virtual_screen():
    """Bounds of all monitors together."""
    try:
        left, top = _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77)
        width, height = _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79)
        if width and height:
            return (left, top, left + width, top + height)
    except Exception:
        pass
    return (0, 0, 1920, 1080)


def _taskbar_setting(name: str, default: int) -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced") as key:
            return int(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return default


def default_position(width: int, height: int, scale: float) -> tuple[int, int]:
    """Where the strip parks when the user has not moved it."""
    bar = taskbar_rect()
    if not bar:
        left, top, right, bottom = virtual_screen()
        return (left + int(12 * scale), bottom - height - int(6 * scale))
    left, top, right, bottom, edge = bar
    if edge not in (ABE_TOP, ABE_BOTTOM):
        return (left + int(6 * scale), top + int(8 * scale))
    y = top + ((bottom - top) - height) // 2
    centred = _taskbar_setting("TaskbarAl", 1) == 1          # Windows 11 default: icons centred
    widgets = _taskbar_setting("TaskbarDa", 1) != 0          # weather / widgets button on the left
    if centred:
        x = left + int((172 if widgets else 12) * scale)     # clear of the weather widget
    else:
        x = right - width - int(440 * scale)                 # icons are on the left: sit before the tray
    return (x, y)


# ---------------------------------------------------------------- the strip
class TaskbarStrip:
    def __init__(self, window, collector, settings) -> None:
        self.window = window            # pywebview window
        self.collector = collector
        self.settings = settings
        self._refs: list = []           # keep .NET delegates and objects alive
        self._form = None
        self._visible = False
        self._seq = -1
        self._layout_key = None
        self._drag = None
        self._moved = False

    # -- set-up ------------------------------------------------------------
    def create(self) -> None:
        """Build the strip. Must be called on the window's UI thread."""
        import clr  # type: ignore
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        import System.Drawing as D  # type: ignore
        import System.Windows.Forms as WF  # type: ignore
        self._D, self._WF = D, WF
        self._scale = max(1.0, _user32.GetDpiForSystem() / 96.0)

        form = WF.Form()
        form.Text = "RigWatch strip"
        form.FormBorderStyle = getattr(WF.FormBorderStyle, "None")
        form.ShowInTaskbar = False
        form.TopMost = True
        form.StartPosition = WF.FormStartPosition.Manual
        form.BackColor = D.Color.FromArgb(*BG)
        form.Location = D.Point(-32000, -32000)

        picture = WF.PictureBox()
        picture.Dock = WF.DockStyle.Fill
        picture.BackColor = D.Color.FromArgb(*BG)
        form.Controls.Add(picture)

        menu = WF.ContextMenuStrip()
        for text, handler in (("Open RigWatch", self._restore_main), ("Park on taskbar", self._park),
                              (None, None), ("Hide this strip", self._disable), ("Quit RigWatch", self._quit)):
            if text is None:
                menu.Items.Add(WF.ToolStripSeparator())
                continue
            item = WF.ToolStripMenuItem(text)
            click = self._handler(handler)
            item.Click += click
            menu.Items.Add(item)
            self._refs += [item, click]
        picture.ContextMenuStrip = menu

        for event, fn in ((picture.MouseDown, self._mouse_down), (picture.MouseMove, self._mouse_move),
                          (picture.MouseUp, self._mouse_up)):
            event += fn
        self._form, self._picture, self._menu = form, picture, menu

        # Tool window (no taskbar button, hidden from Alt+Tab) that never takes focus.
        self._hwnd = int(form.Handle.ToInt64())
        style = _user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE,
                               style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        try:                            # Windows 11: rounded corners and a quiet border
            corner = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, 33, ctypes.byref(corner), 4)
            border = ctypes.c_int(EDGE[0] | (EDGE[1] << 8) | (EDGE[2] << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, 34, ctypes.byref(border), 4)
        except Exception:
            pass
        form.Show()                     # creates child handles; off-screen and non-activating
        _user32.ShowWindow(self._hwnd, SW_HIDE)

        timer = WF.Timer()
        timer.Interval = 250
        tick = self._handler(self._tick)
        timer.Tick += tick
        timer.Start()
        self._refs += [timer, tick, menu, picture, form]

    def _handler(self, fn):
        """Wrap a zero-argument method as a .NET EventHandler."""
        from System import EventHandler  # type: ignore

        def call(_sender, _args):
            try:
                fn()
            except Exception:
                pass
        return EventHandler(call)

    # -- state -------------------------------------------------------------
    def _main_minimised(self) -> bool:
        main = self.window.native
        if main is None or main.IsDisposed:
            return False
        return main.WindowState == self._WF.FormWindowState.Minimized

    def _tick(self) -> None:
        wanted = bool(self.settings.get().get("taskbar_strip", True)) and self._main_minimised()
        if wanted and not self._visible:
            self._seq = -1
            self._redraw()
            self._place()
            _user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True
        elif not wanted and self._visible:
            _user32.ShowWindow(self._hwnd, SW_HIDE)
            self._visible = False
        if self._visible:
            # The taskbar raises itself above other topmost windows when clicked; re-assert.
            _user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self._redraw()

    # -- drawing -----------------------------------------------------------
    def _redraw(self) -> None:
        snap = self.collector.snapshot()
        if not snap.get("ready") or snap.get("seq") == self._seq:
            return
        self._seq = snap.get("seq")
        D = self._D
        s = self._scale
        fields = build_fields(snap, self.settings.get().get("temp_unit", "C"))

        bar = taskbar_rect()
        height = int(40 * s)
        if bar and bar[4] in (ABE_TOP, ABE_BOTTOM):
            height = max(int(26 * s), min(int(52 * s), (bar[3] - bar[1]) - int(8 * s)))
        widths = [int((90 if f["wide"] else 64) * s) for f in fields]
        width = sum(widths) + int(18 * s)
        layout = (width, height)
        if layout != self._layout_key:
            self._layout_key = layout
            self._form.Size = D.Size(width, height)
            if self._visible:
                self._place()

        bmp = D.Bitmap(width, height)
        g = D.Graphics.FromImage(bmp)
        try:
            g.SmoothingMode = D.Drawing2D.SmoothingMode.AntiAlias
            g.TextRenderingHint = D.Text.TextRenderingHint.ClearTypeGridFit
            g.Clear(D.Color.FromArgb(*BG))
            fmt = D.StringFormat.GenericTypographic
            label_font = D.Font("Segoe UI", float(max(9, round(10.5 * s))), D.FontStyle.Regular, D.GraphicsUnit.Pixel)
            value_font = D.Font("Segoe UI", float(max(12, round(15 * s))), D.FontStyle.Bold, D.GraphicsUnit.Pixel)
            extra_font = D.Font("Segoe UI", float(max(10, round(12 * s))), D.FontStyle.Regular, D.GraphicsUnit.Pixel)
            ink, muted = D.SolidBrush(D.Color.FromArgb(*INK)), D.SolidBrush(D.Color.FromArgb(*MUTED))
            mid = height / 2.0
            x = int(14 * s)
            for field, col in zip(fields, widths):
                dot = D.SolidBrush(D.Color.FromArgb(*field["color"]))
                g.FillEllipse(dot, float(x), float(mid - 10 * s), float(6 * s), float(6 * s))
                dot.Dispose()
                g.DrawString(field["label"], label_font, muted, D.PointF(float(x + 10 * s), float(mid - 15 * s)), fmt)
                text = field["value"] or field.get("placeholder") or "N/A"
                g.DrawString(text, value_font, ink if field["value"] else muted, D.PointF(float(x), float(mid - 1 * s)), fmt)
                if field["extra"]:
                    w = g.MeasureString(text, value_font, 10000, fmt).Width
                    g.DrawString(field["extra"], extra_font, muted,
                                 D.PointF(float(x + w + 6 * s), float(mid + 2 * s)), fmt)
                x += col
            for obj in (label_font, value_font, extra_font, ink, muted):
                obj.Dispose()
        finally:
            g.Dispose()
        old = self._picture.Image
        self._picture.Image = bmp
        if old is not None:
            old.Dispose()

    # -- placement ---------------------------------------------------------
    def _place(self) -> None:
        width, height = self._form.Width, self._form.Height
        saved = self.settings.get().get("strip_pos")
        if saved:
            x, y = int(saved[0]), int(saved[1])
        else:
            x, y = default_position(width, height, self._scale)
        left, top, right, bottom = virtual_screen()
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        self._form.Location = self._D.Point(x, y)

    def _park(self) -> None:
        self.settings.update({"strip_pos": None})
        self._place()

    # -- interaction -------------------------------------------------------
    def _mouse_down(self, _sender, e) -> None:
        try:
            if e.Button == self._WF.MouseButtons.Left:
                p = self._WF.Cursor.Position
                self._drag = (p.X, p.Y, self._form.Left, self._form.Top)
                self._moved = False
            elif e.Button == self._WF.MouseButtons.Right:
                _user32.SetForegroundWindow(self._hwnd)      # so the menu closes on an outside click
        except Exception:
            pass

    def _mouse_move(self, _sender, _e) -> None:
        try:
            if not self._drag:
                return
            p = self._WF.Cursor.Position
            sx, sy, wx, wy = self._drag
            dx, dy = p.X - sx, p.Y - sy
            if abs(dx) > 2 or abs(dy) > 2:
                self._moved = True
            if self._moved:
                self._form.Location = self._D.Point(wx + dx, wy + dy)
        except Exception:
            pass

    def _mouse_up(self, _sender, e) -> None:
        try:
            if e.Button != self._WF.MouseButtons.Left or not self._drag:
                return
            self._drag = None
            if self._moved:
                self.settings.update({"strip_pos": [int(self._form.Left), int(self._form.Top)]})
            else:
                self._restore_main()
        except Exception:
            pass

    def _restore_main(self) -> None:
        main = self.window.native
        main.WindowState = self._WF.FormWindowState.Normal
        main.Activate()
        _user32.SetForegroundWindow(int(main.Handle.ToInt64()))

    def _disable(self) -> None:
        self.settings.update({"taskbar_strip": False})

    def _quit(self) -> None:
        self.window.native.Close()
