"""Thin ctypes wrapper over the Windows Performance Data Helper (PDH) API.

One persistent query is opened for the lifetime of the app and collected once
per tick, which is how Task Manager reads the same numbers. This replaces
spawning PowerShell on every refresh.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_CSTATUS_VALID_DATA = 0x00000000
PDH_CSTATUS_NEW_DATA = 0x00000001


class _FmtValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _FmtValue)]


def _load():
    pdh = ctypes.WinDLL("pdh.dll")
    H = wintypes.HANDLE
    pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(H)]
    pdh.PdhOpenQueryW.restype = wintypes.DWORD
    pdh.PdhAddEnglishCounterW.argtypes = [H, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(H)]
    pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
    pdh.PdhCollectQueryData.argtypes = [H]
    pdh.PdhCollectQueryData.restype = wintypes.DWORD
    pdh.PdhGetFormattedCounterArrayW.argtypes = [
        H, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
    pdh.PdhGetFormattedCounterValue.argtypes = [
        H, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_FmtValue),
    ]
    pdh.PdhGetFormattedCounterValue.restype = wintypes.DWORD
    pdh.PdhRemoveCounter.argtypes = [H]
    pdh.PdhRemoveCounter.restype = wintypes.DWORD
    pdh.PdhCloseQuery.argtypes = [H]
    pdh.PdhCloseQuery.restype = wintypes.DWORD
    return pdh


class PdhQuery:
    """A set of counters collected together.

    ``add(key, path)`` registers a counter (wildcard instances allowed) and
    returns False when the counter set does not exist on this machine, so
    callers can report "unavailable" instead of inventing a value.
    """

    def __init__(self) -> None:
        self._pdh = _load()
        self._query = wintypes.HANDLE()
        status = self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
        if status != 0:
            raise OSError(f"PdhOpenQuery failed: 0x{status:08X}")
        self._counters: dict[str, wintypes.HANDLE] = {}
        self._lock = threading.Lock()
        self._buf = ctypes.create_string_buffer(64 * 1024)

    def add(self, key: str, path: str) -> bool:
        handle = wintypes.HANDLE()
        status = self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle))
        if status != 0:
            return False
        self._counters[key] = handle
        return True

    def has(self, key: str) -> bool:
        return key in self._counters

    def collect(self) -> bool:
        with self._lock:
            return self._pdh.PdhCollectQueryData(self._query) == 0

    def array(self, key: str, nocap: bool = True) -> dict[str, float]:
        """Return ``{instance_name: value}`` for a wildcard counter.

        Instances that share a name (PDH does that for some sets) are summed.
        """
        handle = self._counters.get(key)
        if handle is None:
            return {}
        fmt = PDH_FMT_DOUBLE | (PDH_FMT_NOCAP100 if nocap else 0)
        with self._lock:
            for _ in range(3):
                size = wintypes.DWORD(len(self._buf))
                count = wintypes.DWORD(0)
                status = self._pdh.PdhGetFormattedCounterArrayW(
                    handle, fmt, ctypes.byref(size), ctypes.byref(count), self._buf)
                if status == PDH_MORE_DATA:
                    self._buf = ctypes.create_string_buffer(size.value + 16 * 1024)
                    continue
                if status != 0:
                    return {}
                items = ctypes.cast(self._buf, ctypes.POINTER(_FmtItem))
                out: dict[str, float] = {}
                for i in range(count.value):
                    item = items[i]
                    if item.FmtValue.CStatus not in (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA):
                        continue
                    name = item.szName or ""
                    out[name] = out.get(name, 0.0) + item.FmtValue.doubleValue
                return out
        return {}

    def value(self, key: str, nocap: bool = True) -> float | None:
        """Return a single-instance counter value, or None if unavailable."""
        handle = self._counters.get(key)
        if handle is None:
            return None
        fmt = PDH_FMT_DOUBLE | (PDH_FMT_NOCAP100 if nocap else 0)
        val = _FmtValue()
        with self._lock:
            status = self._pdh.PdhGetFormattedCounterValue(handle, fmt, None, ctypes.byref(val))
        if status != 0 or val.CStatus not in (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA):
            return None
        return val.doubleValue

    def close(self) -> None:
        with self._lock:
            if self._query:
                self._pdh.PdhCloseQuery(self._query)
                self._query = wintypes.HANDLE()
