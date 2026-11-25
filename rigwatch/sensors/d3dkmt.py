"""Vendor-neutral GPU temperature / fan / power via the Windows display kernel.

``D3DKMTQueryAdapterInfo(KMTQAITYPE_ADAPTERPERFDATA)`` is the call Task Manager
uses for its GPU temperature readout. It needs no admin rights and works for
any WDDM 2.4+ driver, so it is the fallback when a vendor API is not present.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

KMTQAITYPE_ADAPTERPERFDATA = 62


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _OPENADAPTERFROMLUID(ctypes.Structure):
    _fields_ = [("AdapterLuid", _LUID), ("hAdapter", wintypes.UINT)]


class _CLOSEADAPTER(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT)]


class _QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", wintypes.UINT),
                ("pPrivateDriverData", ctypes.c_void_p),
                ("PrivateDriverDataSize", wintypes.UINT)]


class _ADAPTER_PERFDATA(ctypes.Structure):
    _fields_ = [
        ("PhysicalAdapterIndex", wintypes.UINT),
        ("MemoryFrequency", ctypes.c_ulonglong),
        ("MaxMemoryFrequency", ctypes.c_ulonglong),
        ("MaxMemoryFrequencyOC", ctypes.c_ulonglong),
        ("MemoryBandwidth", ctypes.c_ulonglong),
        ("PCIEBandwidth", ctypes.c_ulonglong),
        ("FanRPM", wintypes.ULONG),
        ("Power", wintypes.ULONG),          # tenths of a percent of max power
        ("Temperature", wintypes.ULONG),    # tenths of a degree Celsius
        ("PowerStateOverride", ctypes.c_ubyte),
    ]


_gdi32 = None


def _lib():
    global _gdi32
    if _gdi32 is None:
        g = ctypes.WinDLL("gdi32.dll")
        for name in ("D3DKMTOpenAdapterFromLuid", "D3DKMTQueryAdapterInfo", "D3DKMTCloseAdapter"):
            fn = getattr(g, name)
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_long
        _gdi32 = g
    return _gdi32


def query_perf(luid_low: int, luid_high: int) -> dict | None:
    """Return ``{temp_c, fan_rpm, power_pct}`` (each may be None) or None."""
    try:
        g = _lib()
    except OSError:
        return None
    op = _OPENADAPTERFROMLUID()
    op.AdapterLuid.LowPart = luid_low & 0xFFFFFFFF
    op.AdapterLuid.HighPart = luid_high
    if g.D3DKMTOpenAdapterFromLuid(ctypes.byref(op)) != 0:
        return None
    try:
        perf = _ADAPTER_PERFDATA()
        perf.PhysicalAdapterIndex = 0
        q = _QUERYADAPTERINFO(op.hAdapter, KMTQAITYPE_ADAPTERPERFDATA,
                              ctypes.cast(ctypes.byref(perf), ctypes.c_void_p),
                              ctypes.sizeof(perf))
        if g.D3DKMTQueryAdapterInfo(ctypes.byref(q)) != 0:
            return None
        # Drivers report 0 for sensors they do not implement.
        return {
            "temp_c": perf.Temperature / 10.0 if perf.Temperature else None,
            "fan_rpm": int(perf.FanRPM) if perf.FanRPM else None,
            "power_pct": perf.Power / 10.0 if perf.Power else None,
            "mem_freq_hz": int(perf.MemoryFrequency) or None,
        }
    finally:
        close = _CLOSEADAPTER(op.hAdapter)
        g.D3DKMTCloseAdapter(ctypes.byref(close))


if __name__ == "__main__":
    from .dxgi import enumerate_adapters
    for a in enumerate_adapters():
        print(a.name, query_perf(a.luid_low, a.luid_high))
