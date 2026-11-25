"""Enumerate display adapters through DXGI.

DXGI is the authoritative source for the adapter LUID (which is what the GPU
performance counters are keyed by) and for the real amount of dedicated video
memory. ``Win32_VideoController.AdapterRAM`` is a 32-bit field that tops out
at 4 GB, which is why the old app had to guess VRAM from the card's name.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

DXGI_ADAPTER_FLAG_SOFTWARE = 2
VENDORS = {0x1002: "AMD", 0x1022: "AMD", 0x10DE: "NVIDIA", 0x8086: "Intel",
           0x1414: "Microsoft", 0x5143: "Qualcomm"}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    ]


@dataclass
class Adapter:
    index: int
    name: str
    vendor_id: int
    device_id: int
    subsys_id: int
    revision: int
    dedicated_bytes: int
    shared_bytes: int
    luid_low: int
    luid_high: int
    software: bool

    @property
    def vendor(self) -> str:
        return VENDORS.get(self.vendor_id, f"0x{self.vendor_id:04X}")

    @property
    def luid_key(self) -> str:
        """LUID formatted the way PDH instance names spell it."""
        return f"luid_0x{self.luid_high & 0xFFFFFFFF:08x}_0x{self.luid_low & 0xFFFFFFFF:08x}"


# IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
_IID_FACTORY1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA,
                      (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))


def _vtable_call(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p)).contents.value
    fn_ptr = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(fn_ptr)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        _vtable_call(obj, 2, wintypes.ULONG)(obj)


def enumerate_adapters(include_software: bool = False) -> list[Adapter]:
    """Return every display adapter Windows knows about."""
    try:
        dxgi = ctypes.WinDLL("dxgi.dll")
    except OSError:
        return []
    factory = ctypes.c_void_p()
    dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    dxgi.CreateDXGIFactory1.restype = ctypes.c_long
    if dxgi.CreateDXGIFactory1(ctypes.byref(_IID_FACTORY1), ctypes.byref(factory)) != 0:
        return []

    adapters: list[Adapter] = []
    seen: set[tuple[int, int]] = set()
    try:
        # IDXGIFactory1::EnumAdapters1 is vtable slot 12.
        enum_adapters1 = _vtable_call(factory, 12, ctypes.c_long, wintypes.UINT,
                                      ctypes.POINTER(ctypes.c_void_p))
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters1(factory, i, ctypes.byref(adapter)) != 0:
                break
            try:
                desc = _DXGI_ADAPTER_DESC1()
                # IDXGIAdapter1::GetDesc1 is vtable slot 10.
                get_desc1 = _vtable_call(adapter, 10, ctypes.c_long,
                                         ctypes.POINTER(_DXGI_ADAPTER_DESC1))
                if get_desc1(adapter, ctypes.byref(desc)) == 0:
                    software = bool(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
                    luid = (desc.AdapterLuid.LowPart, desc.AdapterLuid.HighPart)
                    if luid not in seen and (include_software or not software):
                        seen.add(luid)
                        adapters.append(Adapter(
                            index=len(adapters),
                            name=desc.Description.strip(),
                            vendor_id=desc.VendorId,
                            device_id=desc.DeviceId,
                            subsys_id=desc.SubSysId,
                            revision=desc.Revision,
                            dedicated_bytes=int(desc.DedicatedVideoMemory),
                            shared_bytes=int(desc.SharedSystemMemory),
                            luid_low=luid[0],
                            luid_high=luid[1],
                            software=software,
                        ))
            finally:
                _release(adapter)
            i += 1
    finally:
        _release(factory)
    return adapters


if __name__ == "__main__":
    for a in enumerate_adapters(include_software=True):
        print(f"{a.index}: {a.name} [{a.vendor}] dev=0x{a.device_id:04X} "
              f"vram={a.dedicated_bytes / 2**30:.1f} GB shared={a.shared_bytes / 2**30:.1f} GB "
              f"{a.luid_key} software={a.software}")
