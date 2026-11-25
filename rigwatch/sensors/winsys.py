"""Small Win32 helpers: kernel memory counters, per-disk activity, OS identity."""

from __future__ import annotations

import ctypes
import socket
import struct
import winreg
from ctypes import wintypes

from . import storage


class _PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(_PERFORMANCE_INFORMATION), wintypes.DWORD]
_psapi.GetPerformanceInfo.restype = wintypes.BOOL


def performance_info() -> dict | None:
    """Commit charge, kernel pools and object counts (what Task Manager shows)."""
    info = _PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(info)
    if not _psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return None
    page = info.PageSize
    return {
        "commit_used": info.CommitTotal * page,
        "commit_limit": info.CommitLimit * page,
        "commit_peak": info.CommitPeak * page,
        "system_cache": info.SystemCache * page,
        "kernel_paged": info.KernelPaged * page,
        "kernel_nonpaged": info.KernelNonpaged * page,
        "handles": info.HandleCount,
        "processes": info.ProcessCount,
        "threads": info.ThreadCount,
    }


IOCTL_DISK_PERFORMANCE = 0x00070020


def disk_performance(index: int) -> dict | None:
    """Cumulative I/O counters for one physical drive, including idle time."""
    handle = storage._open(rf"\\.\PhysicalDrive{index}")
    if handle is None:
        return None
    try:
        data = storage._ioctl(handle, IOCTL_DISK_PERFORMANCE, b"", 128)
    finally:
        storage._k32.CloseHandle(handle)
    if not data or len(data) < 64:
        return None
    read_bytes, write_bytes, _rt, _wt, idle = struct.unpack_from("<5q", data, 0)
    read_count, write_count, queue, _split = struct.unpack_from("<4I", data, 40)
    query_time = struct.unpack_from("<q", data, 56)[0]
    return {"read_bytes": read_bytes, "write_bytes": write_bytes, "idle_100ns": idle,
            "read_count": read_count, "write_count": write_count, "queue": queue,
            "query_100ns": query_time}


def _reg(root, path: str, name: str):
    try:
        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def os_info() -> dict:
    cv = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    hklm = winreg.HKEY_LOCAL_MACHINE
    product = _reg(hklm, cv, "ProductName") or "Windows"
    build = str(_reg(hklm, cv, "CurrentBuildNumber") or "")
    ubr = _reg(hklm, cv, "UBR")
    # The registry still says "Windows 10" on Windows 11; the build number is the truth.
    if build.isdigit() and int(build) >= 22000:
        product = product.replace("Windows 10", "Windows 11")
    return {
        "name": product,
        "version": _reg(hklm, cv, "DisplayVersion"),
        "build": f"{build}.{ubr}" if ubr is not None else build,
        "hostname": socket.gethostname(),
    }


def cpu_identity() -> dict:
    path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    hklm = winreg.HKEY_LOCAL_MACHINE
    name = _reg(hklm, path, "ProcessorNameString")
    return {
        "name": " ".join(str(name).split()) if name else "Processor",
        "vendor": _reg(hklm, path, "VendorIdentifier"),
        "base_mhz": _reg(hklm, path, "~MHz"),
    }


def primary_ipv4() -> str | None:
    """Local address of the interface that owns the default route.

    A UDP ``connect`` only selects a route; no packet is sent.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# --------------------------------------------------------------- processes
_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.POINTER(ctypes.c_ulong)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
_proc_buf_size = 1 << 20


def process_snapshot() -> list[dict]:
    """Every process in one kernel call (no handles opened), as Task Manager does it.

    Returns name, pid, thread count, cumulative CPU time (100 ns units) and the
    private working set - the "Memory" figure Task Manager shows.
    """
    global _proc_buf_size
    for _ in range(6):
        buf = ctypes.create_string_buffer(_proc_buf_size)
        needed = ctypes.c_ulong(0)
        status = _ntdll.NtQuerySystemInformation(_SYSTEM_PROCESS_INFORMATION, buf, _proc_buf_size,
                                                 ctypes.byref(needed))
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            _proc_buf_size = max(_proc_buf_size * 2, needed.value + (64 << 10))
            continue
        if status != 0:
            return []
        break
    else:
        return []

    raw = buf.raw
    base = ctypes.addressof(buf)
    out = []
    offset = 0
    while True:
        (next_off, threads, private_ws) = struct.unpack_from("<IIq", raw, offset)
        create_time, user_time, kernel_time = struct.unpack_from("<qqq", raw, offset + 32)
        name_len = struct.unpack_from("<H", raw, offset + 56)[0]
        name_ptr = struct.unpack_from("<Q", raw, offset + 64)[0]
        pid = struct.unpack_from("<Q", raw, offset + 80)[0]
        working_set = struct.unpack_from("<Q", raw, offset + 144)[0]
        if name_ptr and name_len:
            start = name_ptr - base
            name = raw[start:start + name_len].decode("utf-16-le", "replace") if 0 <= start < len(raw) else ""
        else:
            name = "System Idle Process" if pid == 0 else ""
        out.append({"pid": pid, "name": name, "threads": threads, "create_time": create_time,
                    "cpu_100ns": user_time + kernel_time, "private_ws": private_ws, "working_set": working_set})
        if not next_off:
            break
        offset += next_off
    return out
