"""Connected monitors: name, resolution, refresh rate, connection, colour and scaling.

Read from Windows' display configuration and from each monitor's own
identification block (EDID), which Windows keeps in the registry. Nothing here
needs administrator rights, and nothing is estimated: a detail Windows does not
report is left out.
"""

from __future__ import annotations

import ctypes
import math
import winreg
from ctypes import wintypes

QDC_ONLY_ACTIVE_PATHS = 2
_GET_SOURCE_NAME, _GET_TARGET_NAME, _GET_ADVANCED_COLOR, _GET_ADVANCED_COLOR_2 = 1, 2, 9, 15
_MODE_SOURCE, _MODE_TARGET = 1, 2
_NO_MODE = 0xFFFFFFFF

CONNECTIONS = {
    0: "VGA", 1: "S-Video", 2: "Composite", 3: "Component", 4: "DVI", 5: "HDMI", 6: "Built-in (LVDS)",
    8: "D-terminal", 9: "SDI", 10: "DisplayPort", 11: "Built-in (eDP)", 12: "UDI", 13: "Built-in (UDI)",
    14: "TV dongle", 15: "Miracast (wireless)", 16: "Indirect (wired)", 17: "Virtual", 18: "USB-C (DisplayPort)",
    0x80000000: "Built-in",
}
BUILT_IN = {6, 11, 13, 0x80000000}
ROTATIONS = {1: 0, 2: 90, 3: 180, 4: 270}
# Three-letter maker codes from the EDID, for the brands people know by another name.
MAKERS = {
    "ACI": "ASUS", "AUS": "ASUS", "ACR": "Acer", "AOC": "AOC", "APP": "Apple", "AUO": "AU Optronics",
    "BNQ": "BenQ", "BOE": "BOE", "CMN": "Innolux", "DEL": "Dell", "EIZ": "EIZO", "GBT": "Gigabyte",
    "GSM": "LG", "HKC": "HKC", "HPN": "HP", "HWP": "HP", "IVM": "iiyama", "LEN": "Lenovo", "LGD": "LG Display",
    "MEI": "Panasonic", "MSI": "MSI", "NEC": "NEC", "PHL": "Philips", "SAM": "Samsung", "SDC": "Samsung Display",
    "SHP": "Sharp", "SNY": "Sony", "VSC": "ViewSonic", "XMI": "Xiaomi",
}


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class _PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class _PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32), ("refreshRate", _RATIONAL),
                ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wintypes.BOOL),
                ("statusFlags", ctypes.c_uint32)]


class _PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _PATH_SOURCE), ("targetInfo", _PATH_TARGET), ("flags", ctypes.c_uint32)]


class _2D(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_uint32), ("cy", ctypes.c_uint32)]


class _VIDEO_SIGNAL(ctypes.Structure):
    _fields_ = [("pixelRate", ctypes.c_uint64), ("hSyncFreq", _RATIONAL), ("vSyncFreq", _RATIONAL),
                ("activeSize", _2D), ("totalSize", _2D), ("videoStandard", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32)]


class _SOURCE_MODE(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32), ("pixelFormat", ctypes.c_uint32),
                ("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MODE_UNION(ctypes.Union):
    _fields_ = [("target", _VIDEO_SIGNAL), ("source", _SOURCE_MODE), ("raw", ctypes.c_byte * 48)]


class _MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", _LUID),
                ("mode", _MODE_UNION)]


class _HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", _LUID),
                ("id", ctypes.c_uint32)]


class _SOURCE_NAME(ctypes.Structure):
    _fields_ = [("header", _HEADER), ("viewGdiDeviceName", ctypes.c_wchar * 32)]


class _TARGET_NAME(ctypes.Structure):
    _fields_ = [("header", _HEADER), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
                ("monitorDevicePath", ctypes.c_wchar * 128)]


class _ADVANCED_COLOR(ctypes.Structure):
    _fields_ = [("header", _HEADER), ("value", ctypes.c_uint32), ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32)]


class _ADVANCED_COLOR_2(ctypes.Structure):
    _fields_ = [("header", _HEADER), ("value", ctypes.c_uint32), ("colorEncoding", ctypes.c_uint32),
                ("bitsPerColorChannel", ctypes.c_uint32), ("activeColorMode", ctypes.c_uint32)]


class _DEVMODE(ctypes.Structure):
    _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmUnion", ctypes.c_byte * 16), ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmIcm", wintypes.DWORD * 8)]


assert ctypes.sizeof(_PATH_INFO) == 72 and ctypes.sizeof(_MODE_INFO) == 64
assert ctypes.sizeof(_TARGET_NAME) == 420 and ctypes.sizeof(_DEVMODE) == 220


# ------------------------------------------------------------------- EDID
def parse_edid(data: bytes) -> dict:
    """The facts a monitor states about itself. Empty when the block is not a valid EDID."""
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}
    code = (data[8] << 8) | data[9]
    letters = "".join(chr(64 + ((code >> shift) & 0x1F)) for shift in (10, 5, 0))
    out: dict = {"maker_code": letters, "maker": MAKERS.get(letters, letters)}
    if data[17]:
        out["year"] = 1990 + data[17]
    if data[21] and data[22]:
        out["size_mm"] = (data[21] * 10, data[22] * 10)
    for offset in (54, 72, 90, 108):
        block = data[offset:offset + 18]
        if block[0] or block[1]:                      # a detailed timing: the first one is the native mode
            if "native" not in out:
                out["native"] = (block[2] | ((block[4] & 0xF0) << 4), block[5] | ((block[7] & 0xF0) << 4))
                width_mm = block[12] | ((block[14] & 0xF0) << 4)
                height_mm = block[13] | ((block[14] & 0x0F) << 8)
                if width_mm and height_mm:
                    out["size_mm"] = (width_mm, height_mm)      # millimetres beat the rounded centimetres
        elif block[3] == 0xFC:                        # monitor name
            name = block[5:18].split(b"\x0a")[0].decode("cp437", "replace").strip()
            if name:
                out["name"] = name
    if "size_mm" in out:
        w, h = out["size_mm"]
        out["size_in"] = round(math.hypot(w, h) / 25.4, 1)
    return out


_edid_cache: dict[str, dict] = {}


def _edid_for(device_path: str) -> dict:
    r"""EDID of the monitor behind an interface path such as \\?\DISPLAY#AOC3207#5&..&UID4353#{guid}."""
    if device_path in _edid_cache:
        return _edid_cache[device_path]
    info: dict = {}
    parts = device_path.split("#")
    if len(parts) >= 3:
        key = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{parts[1]}\{parts[2]}\Device Parameters"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                data = winreg.QueryValueEx(handle, "EDID")[0]
            info = parse_edid(bytes(data))
        except OSError:
            info = {}
    _edid_cache[device_path] = info
    return info


# ----------------------------------------------------------------- Windows
def _device_info(user32, struct_type, info_type: int, adapter: _LUID, target_id: int):
    packet = struct_type()
    packet.header.type = info_type
    packet.header.size = ctypes.sizeof(struct_type)
    packet.header.adapterId = adapter
    packet.header.id = target_id
    return packet if user32.DisplayConfigGetDeviceInfo(ctypes.byref(packet)) == 0 else None


def _max_refresh(user32, gdi_name: str, width: int, height: int) -> int | None:
    best = 0
    mode = _DEVMODE()
    mode.dmSize = ctypes.sizeof(_DEVMODE)
    index = 0
    while user32.EnumDisplaySettingsW(gdi_name, index, ctypes.byref(mode)):
        if mode.dmPelsWidth == width and mode.dmPelsHeight == height:
            best = max(best, int(mode.dmDisplayFrequency))
        index += 1
        if index > 5000:
            break
    return best or None


def _scale_percent(user32, x: int, y: int) -> int | None:
    """Windows' scaling for the monitor at a desktop position (100, 125, 150 ...)."""
    try:
        shcore = ctypes.WinDLL("shcore")
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))     # per-monitor aware (v2)
        try:
            monitor = user32.MonitorFromPoint(wintypes.POINT(x + 1, y + 1), 2)  # nearest
            dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
            if shcore.GetDpiForMonitor(ctypes.c_void_p(monitor), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0:
                return None
            return round(dpi_x.value / 96 * 100)
        finally:
            if previous:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
    except Exception:
        return None


def colour_state(newer, older) -> dict:
    """HDR on / off / unsupported, wide colour, and bits per colour channel.

    ``newer`` is Windows 11 24H2's answer, which names the active colour mode.
    ``older`` is the earlier answer, where "advanced colour" covers both HDR and
    Windows' automatic wide-colour management; the two are told apart by a flag.
    """
    if newer is not None:
        hdr_supported = bool(newer.value & 0x10)
        mode = newer.activeColorMode                       # 0 standard, 1 wide colour, 2 HDR
        return {"hdr": "on" if mode == 2 else "off" if hdr_supported else "unsupported",
                "wide_colour": mode == 1, "bits_per_channel": int(newer.bitsPerColorChannel) or None}
    if older is not None:
        supported, enabled, wide_enforced = bool(older.value & 1), bool(older.value & 2), bool(older.value & 4)
        return {"hdr": "on" if enabled and not wide_enforced else "off" if supported else "unsupported",
                "wide_colour": enabled and wide_enforced, "bits_per_channel": int(older.bitsPerColorChannel) or None}
    return {"hdr": None, "wide_colour": None, "bits_per_channel": None}


def luid_key(adapter: _LUID) -> str:
    """Same spelling as ``dxgi.Adapter.luid_key``, to match a monitor to its graphics card."""
    return f"luid_0x{adapter.HighPart & 0xFFFFFFFF:08x}_0x{adapter.LowPart & 0xFFFFFFFF:08x}"


def list_displays(adapter_names: dict[str, str] | None = None) -> list[dict]:
    """One entry per active monitor, in Windows' order. ``adapter_names`` maps luid_key -> card name."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)) != 0:
        return []
    paths = (_PATH_INFO * max(1, n_paths.value))()
    modes = (_MODE_INFO * max(1, n_modes.value))()
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                 ctypes.byref(n_modes), modes, None) != 0:
        return []

    out = []
    for path in paths[:n_paths.value]:
        src, tgt = path.sourceInfo, path.targetInfo
        entry: dict = {"index": len(out)}

        source = _device_info(user32, _SOURCE_NAME, _GET_SOURCE_NAME, src.adapterId, src.id)
        gdi_name = source.viewGdiDeviceName if source else None
        target = _device_info(user32, _TARGET_NAME, _GET_TARGET_NAME, tgt.adapterId, tgt.id)
        edid = _edid_for(target.monitorDevicePath) if target and target.monitorDevicePath else {}

        technology = tgt.outputTechnology
        entry["connection"] = CONNECTIONS.get(technology)
        entry["built_in"] = technology in BUILT_IN
        name = (target.monitorFriendlyDeviceName if target else "") or edid.get("name") or ""
        maker = edid.get("maker")
        if not name:
            name = "Built-in display" if entry["built_in"] else "Monitor"
        elif maker and not name.lower().startswith(maker.lower()):
            name = f"{maker} {name}"
        entry["name"] = name
        entry["maker"] = maker
        entry["year"] = edid.get("year")
        entry["size_in"] = edid.get("size_in")
        entry["native"] = list(edid["native"]) if edid.get("native") else None

        if src.modeInfoIdx != _NO_MODE and src.modeInfoIdx < n_modes.value \
                and modes[src.modeInfoIdx].infoType == _MODE_SOURCE:
            mode = modes[src.modeInfoIdx].mode.source
            entry.update(width=int(mode.width), height=int(mode.height), x=int(mode.x), y=int(mode.y),
                         primary=(mode.x == 0 and mode.y == 0))
        else:
            entry.update(width=None, height=None, x=None, y=None, primary=False)

        refresh = None
        if tgt.refreshRate.Denominator:
            refresh = tgt.refreshRate.Numerator / tgt.refreshRate.Denominator
        if tgt.modeInfoIdx != _NO_MODE and tgt.modeInfoIdx < n_modes.value \
                and modes[tgt.modeInfoIdx].infoType == _MODE_TARGET:
            signal = modes[tgt.modeInfoIdx].mode.target
            if signal.vSyncFreq.Denominator:
                refresh = signal.vSyncFreq.Numerator / signal.vSyncFreq.Denominator
            entry["signal"] = [int(signal.activeSize.cx), int(signal.activeSize.cy)]
        else:
            entry["signal"] = None
        entry["refresh_hz"] = round(refresh, 2) if refresh else None
        entry["interlaced"] = tgt.scanLineOrdering in (2, 3)
        entry["rotation"] = ROTATIONS.get(tgt.rotation)

        entry["max_refresh_hz"] = (_max_refresh(user32, gdi_name, entry["width"], entry["height"])
                                   if gdi_name and entry["width"] else None)
        entry["scale_pct"] = _scale_percent(user32, entry["x"], entry["y"]) if entry["x"] is not None else None

        entry.update(colour_state(
            _device_info(user32, _ADVANCED_COLOR_2, _GET_ADVANCED_COLOR_2, tgt.adapterId, tgt.id),
            _device_info(user32, _ADVANCED_COLOR, _GET_ADVANCED_COLOR, tgt.adapterId, tgt.id)))

        entry["adapter"] = (adapter_names or {}).get(luid_key(tgt.adapterId))
        out.append(entry)
    return out


if __name__ == "__main__":       # python -m rigwatch.sensors.displays
    import json

    from . import dxgi
    print(json.dumps(list_displays({a.luid_key: a.name for a in dxgi.enumerate_adapters()}), indent=2))
