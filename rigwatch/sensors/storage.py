"""Physical drive inventory, temperature and NVMe health.

Everything here goes through ``IOCTL_STORAGE_QUERY_PROPERTY`` on a handle
opened with *no* access rights, which Windows allows for standard users - so
drive temperature and wear are available without running as administrator.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

PROP_DEVICE = 0
PROP_SEEK_PENALTY = 7
PROP_PROTOCOL_SPECIFIC = 50
PROP_TEMPERATURE = 52

BUS_TYPES = {1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "IEEE 1394", 6: "Fibre Channel",
             7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD",
             13: "MMC", 14: "Virtual", 15: "Virtual", 16: "Storage Spaces",
             17: "NVMe", 18: "SCM", 19: "UFS"}

INVALID_HANDLE = wintypes.HANDLE(-1).value

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                 ctypes.c_void_p]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _open(path: str):
    handle = _k32.CreateFileW(path, 0, 0x1 | 0x2, None, 3, 0, None)
    if handle in (None, INVALID_HANDLE):
        return None
    return handle


def _ioctl(handle, code: int, in_bytes: bytes, out_size: int) -> bytes | None:
    in_buf = ctypes.create_string_buffer(in_bytes, len(in_bytes)) if in_bytes else None
    out_buf = ctypes.create_string_buffer(out_size)
    returned = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(handle, code, in_buf, len(in_bytes) if in_bytes else 0,
                              out_buf, out_size, ctypes.byref(returned), None)
    if not ok:
        return None
    return out_buf.raw[:returned.value]


def _query(handle, prop: int, out_size: int = 1024, extra: bytes = b"") -> bytes | None:
    # STORAGE_PROPERTY_QUERY: PropertyId, QueryType (0 = standard), AdditionalParameters
    header = struct.pack("<II", prop, 0)
    payload = header + (extra if extra else b"\0\0\0\0")
    return _ioctl(handle, IOCTL_STORAGE_QUERY_PROPERTY, payload, out_size)


def _cstr(buf: bytes, offset: int) -> str:
    if not offset or offset >= len(buf):
        return ""
    end = buf.find(b"\0", offset)
    return buf[offset:end if end != -1 else None].decode("ascii", "replace").strip()


def _device_info(handle) -> dict:
    info: dict = {}
    data = _query(handle, PROP_DEVICE, 2048)
    if data and len(data) >= 40:
        (_ver, _size, _dtype, _dmod, removable, _cq, vendor_off, product_off,
         rev_off, serial_off, bus) = struct.unpack_from("<IIBBBBIIIII", data, 0)
        vendor = _cstr(data, vendor_off)
        product = _cstr(data, product_off)
        info["model"] = " ".join(p for p in (vendor, product) if p) or "Unknown drive"
        info["firmware"] = _cstr(data, rev_off) or None
        info["bus"] = BUS_TYPES.get(bus, "Unknown")
        info["removable"] = bool(removable)
    seek = _query(handle, PROP_SEEK_PENALTY, 64)
    if seek and len(seek) >= 9:
        info["rotational"] = bool(seek[8])
    geo = _ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, b"", 256)
    if geo and len(geo) >= 32:
        info["size_bytes"] = struct.unpack_from("<q", geo, 24)[0]
    return info


def _temperature(handle) -> dict | None:
    data = _query(handle, PROP_TEMPERATURE, 512)
    if not data or len(data) < 40:
        return None
    critical, warning, count = struct.unpack_from("<hhH", data, 8)
    temps = []
    for i in range(count):
        off = 24 + i * 16
        if off + 4 > len(data):
            break
        temp = struct.unpack_from("<h", data, off + 2)[0]
        if -40 < temp < 150 and temp != 0:
            temps.append(temp)
    if not temps:
        return None
    return {"temp_c": float(max(temps)),
            "warning_c": float(warning) if 0 < warning < 150 else None,
            "critical_c": float(critical) if 0 < critical < 150 else None}


def _nvme_health(handle) -> dict | None:
    # STORAGE_PROTOCOL_SPECIFIC_DATA requesting NVMe log page 0x02 (SMART / health)
    proto = struct.pack("<10I", 3, 2, 0x02, 0, 40, 512, 0, 0, 0, 0)
    data = _query(handle, PROP_PROTOCOL_SPECIFIC, 8 + 40 + 512, proto + b"\0" * 512)
    if not data or len(data) < 48 + 512:
        return None
    data_offset = struct.unpack_from("<I", data, 8 + 16)[0]
    log = data[8 + data_offset: 8 + data_offset + 512]
    if len(log) < 200:
        return None

    def u128(off: int) -> int:
        return int.from_bytes(log[off:off + 16], "little")

    kelvin = struct.unpack_from("<H", log, 1)[0]
    if kelvin == 0:
        return None
    return {
        "temp_c": round(kelvin - 273.15, 1),
        "critical_warning": log[0],
        "available_spare_pct": log[3],
        "spare_threshold_pct": log[4],
        "percentage_used": log[5],
        "data_read_bytes": u128(32) * 512000,
        "data_written_bytes": u128(48) * 512000,
        "power_cycles": u128(112),
        "power_on_hours": u128(128),
        "unsafe_shutdowns": u128(144),
        "media_errors": u128(160),
    }


def list_drives(max_drives: int = 32) -> list[dict]:
    """Static description of each physical drive."""
    drives = []
    for n in range(max_drives):
        handle = _open(rf"\\.\PhysicalDrive{n}")
        if handle is None:
            continue
        try:
            info = _device_info(handle)
            info["index"] = n
            info["key"] = f"PhysicalDrive{n}"
            if info.get("bus") == "NVMe":
                info["kind"] = "NVMe SSD"
            elif info.get("rotational") is True:
                info["kind"] = "HDD"
            elif info.get("rotational") is False:
                info["kind"] = "SSD"
            else:
                info["kind"] = "Drive"
            if info.get("bus") == "USB":
                info["kind"] = "USB " + info["kind"]
            drives.append(info)
        finally:
            _k32.CloseHandle(handle)
    return drives


def read_health(index: int) -> dict:
    """Live temperature and (for NVMe) wear data for one drive."""
    handle = _open(rf"\\.\PhysicalDrive{index}")
    if handle is None:
        return {}
    try:
        out: dict = {}
        temp = _temperature(handle)
        if temp:
            out.update(temp)
        nvme = _nvme_health(handle)
        if nvme:
            if "temp_c" in out:
                nvme.pop("temp_c")
            out.update(nvme)
            out["health_pct"] = max(0, 100 - min(100, nvme["percentage_used"]))
        return out
    finally:
        _k32.CloseHandle(handle)


def volume_disks(letter: str) -> list[int]:
    """Physical drive numbers backing a volume such as ``C:``."""
    handle = _open(rf"\\.\{letter.rstrip(chr(92))}")
    if handle is None:
        return []
    try:
        data = _ioctl(handle, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, b"", 1024)
        if not data or len(data) < 8:
            return []
        count = struct.unpack_from("<I", data, 0)[0]
        disks = []
        for i in range(count):
            off = 8 + i * 24
            if off + 4 > len(data):
                break
            disks.append(struct.unpack_from("<I", data, off)[0])
        return disks
    finally:
        _k32.CloseHandle(handle)


if __name__ == "__main__":
    import psutil
    for d in list_drives():
        print(d)
        print("   health:", read_health(d["index"]))
    for part in psutil.disk_partitions(all=False):
        print(part.device, part.fstype, "->", volume_disks(part.device[:2]))
