"""Read memory-module, board and BIOS facts straight from the SMBIOS table.

``GetSystemFirmwareTable`` needs no admin rights and no WMI/PowerShell, so
this is instant and works even when WMI is slow or broken.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

_RSMB = 0x52534D42  # 'RSMB'

MEMORY_TYPES = {0x12: "DDR", 0x13: "DDR2", 0x18: "DDR3", 0x1A: "DDR4", 0x1B: "LPDDR",
                0x1C: "LPDDR2", 0x1D: "LPDDR3", 0x1E: "LPDDR4", 0x22: "DDR5", 0x23: "LPDDR5"}
FORM_FACTORS = {0x09: "DIMM", 0x0D: "SO-DIMM", 0x0C: "SIMM", 0x0F: "FB-DIMM"}
_JUNK = {"", "unknown", "none", "not specified", "to be filled by o.e.m.", "default string",
         "n/a", "undefined", "empty", "no dimm", "not installed"}


def _raw_table() -> bytes | None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetSystemFirmwareTable.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    k32.GetSystemFirmwareTable.restype = wintypes.UINT
    size = k32.GetSystemFirmwareTable(_RSMB, 0, None, 0)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if k32.GetSystemFirmwareTable(_RSMB, 0, buf, size) != size:
        return None
    return buf.raw


def _structures(data: bytes):
    """Yield ``(type, formatted_bytes, strings)`` for each SMBIOS structure."""
    if len(data) < 8:
        return
    table = data[8:8 + struct.unpack_from("<I", data, 4)[0]]
    pos = 0
    while pos + 4 <= len(table):
        stype, length = table[pos], table[pos + 1]
        if length < 4:
            break
        formatted = table[pos:pos + length]
        # the string-set follows the formatted area and ends with a double NUL
        end = pos + length
        strings = []
        while end < len(table):
            nul = table.find(b"\0", end)
            if nul == -1:
                end = len(table)
                break
            if nul == end:           # empty string -> terminator
                end += 1
                break
            strings.append(table[end:nul].decode("latin-1", "replace").strip())
            end = nul + 1
        if not strings and end < len(table) and table[end:end + 1] == b"\0":
            end += 1                 # structure without strings ends with two NULs
        yield stype, formatted, strings
        if stype == 127:
            break
        pos = end


def _s(strings: list[str], index: int) -> str | None:
    if index <= 0 or index > len(strings):
        return None
    value = strings[index - 1].strip()
    return None if value.lower() in _JUNK else value


def _byte(b: bytes, off: int) -> int:
    return b[off] if off < len(b) else 0


def _word(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0] if off + 2 <= len(b) else 0


def read() -> dict:
    """Return ``{bios, board, system, cpu, memory: {slots, modules}}``."""
    out: dict = {"bios": {}, "board": {}, "system": {}, "cpu": {}, "memory": {"slots": 0, "modules": []}}
    data = _raw_table()
    if not data:
        return out
    try:
        for stype, b, strings in _structures(data):
            if stype == 0:
                out["bios"] = {"vendor": _s(strings, _byte(b, 4)), "version": _s(strings, _byte(b, 5)),
                               "date": _s(strings, _byte(b, 8))}
            elif stype == 1:
                out["system"] = {"manufacturer": _s(strings, _byte(b, 4)), "product": _s(strings, _byte(b, 5))}
            elif stype == 2 and not out["board"]:
                out["board"] = {"manufacturer": _s(strings, _byte(b, 4)), "product": _s(strings, _byte(b, 5)),
                                "version": _s(strings, _byte(b, 6))}
            elif stype == 4 and not out["cpu"]:
                out["cpu"] = {"socket": _s(strings, _byte(b, 4)),
                              "max_speed_mhz": _word(b, 0x14) or None,
                              "current_speed_mhz": _word(b, 0x16) or None}
            elif stype == 17:
                out["memory"]["slots"] += 1
                size = _word(b, 0x0C)
                if size in (0, 0xFFFF):
                    continue
                if size == 0x7FFF and len(b) >= 0x20:
                    size_mb = struct.unpack_from("<I", b, 0x1C)[0] & 0x7FFFFFFF
                elif size & 0x8000:
                    size_mb = (size & 0x7FFF) / 1024
                else:
                    size_mb = size
                speed = _word(b, 0x15)
                configured = _word(b, 0x20)
                voltage = _word(b, 0x26)
                out["memory"]["modules"].append({
                    "slot": _s(strings, _byte(b, 0x10)) or f"Slot {out['memory']['slots']}",
                    "bank": _s(strings, _byte(b, 0x11)),
                    "size_bytes": int(size_mb * 1024 * 1024),
                    "type": MEMORY_TYPES.get(_byte(b, 0x12)),
                    "form_factor": FORM_FACTORS.get(_byte(b, 0x0E)),
                    "rated_speed": speed if 0 < speed < 0xFFFF else None,
                    "configured_speed": configured if 0 < configured < 0xFFFF else None,
                    "voltage": round(voltage / 1000, 2) if 0 < voltage < 5000 else None,
                    "manufacturer": _s(strings, _byte(b, 0x17)),
                    "part_number": _s(strings, _byte(b, 0x1A)),
                })
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(read(), indent=2))
