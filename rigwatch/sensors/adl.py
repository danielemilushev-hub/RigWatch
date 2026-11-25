"""AMD Radeon telemetry through ADL (AMD Display Library).

``atiadlxx.dll`` ships with every Radeon driver. ``ADL2_New_QueryPMLogData_Get``
returns the same sensor block Adrenalin's own overlay reads: temperatures, fan,
clocks, voltage and measured power draw. No admin rights are required.
"""

from __future__ import annotations

import ctypes
import os

ADL_OK = 0
ADL_MAX_PATH = 256
ADL_PMLOG_MAX_SENSORS = 256
AMD_VENDOR_ID = 1002  # ADL reports the PCI vendor id as a *decimal* 1002

# (sensor index in ADL_PMLOG_SENSORS, key, unit) -- only the ones worth showing.
SENSORS = [
    (1, "core_clock_mhz", "MHz"),
    (2, "mem_clock_mhz", "MHz"),
    (8, "temp_edge_c", "°C"),
    (9, "temp_mem_c", "°C"),
    (10, "temp_vr_core_c", "°C"),
    (11, "temp_vr_mem_c", "°C"),
    (12, "temp_liquid_c", "°C"),
    (14, "fan_rpm", "RPM"),
    (15, "fan_pct", "%"),
    (16, "soc_voltage_mv", "mV"),
    (17, "soc_power_w", "W"),
    (19, "activity_pct", "%"),
    (20, "mem_activity_pct", "%"),
    (21, "core_voltage_mv", "mV"),
    (22, "mem_voltage_mv", "mV"),
    (23, "asic_power_w", "W"),
    (24, "temp_vr_soc_c", "°C"),
    (27, "temp_hotspot_c", "°C"),
    (28, "temp_gfx_c", "°C"),
    (29, "temp_soc_c", "°C"),
    (30, "gfx_power_w", "W"),
    (32, "temp_cpu_c", "°C"),
    (33, "cpu_power_w", "W"),
    (34, "cpu_clock_mhz", "MHz"),
    (40, "bus_speed", ""),
    (41, "bus_lanes", ""),
    (73, "board_power_w", "W"),
]


class _AdapterInfo(ctypes.Structure):
    _fields_ = [
        ("iSize", ctypes.c_int),
        ("iAdapterIndex", ctypes.c_int),
        ("strUDID", ctypes.c_char * ADL_MAX_PATH),
        ("iBusNumber", ctypes.c_int),
        ("iDeviceNumber", ctypes.c_int),
        ("iFunctionNumber", ctypes.c_int),
        ("iVendorID", ctypes.c_int),
        ("strAdapterName", ctypes.c_char * ADL_MAX_PATH),
        ("strDisplayName", ctypes.c_char * ADL_MAX_PATH),
        ("iPresent", ctypes.c_int),
        ("iExist", ctypes.c_int),
        ("strDriverPath", ctypes.c_char * ADL_MAX_PATH),
        ("strDriverPathExt", ctypes.c_char * ADL_MAX_PATH),
        ("strPNPString", ctypes.c_char * ADL_MAX_PATH),
        ("iOSDisplayIndex", ctypes.c_int),
    ]


class _SensorData(ctypes.Structure):
    _fields_ = [("supported", ctypes.c_int), ("value", ctypes.c_int)]


class _PMLogDataOutput(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int), ("sensors", _SensorData * ADL_PMLOG_MAX_SENSORS)]


_MALLOC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int)

_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def amd_driver_entries() -> list[dict]:
    """Registry entries of installed AMD display drivers (version, folder)."""
    import winreg
    entries: list[dict] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS)
    except OSError:
        return entries
    with root:
        for i in range(64):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    def val(name):
                        try:
                            return winreg.QueryValueEx(key, name)[0]
                        except OSError:
                            return None
                    match = str(val("MatchingDeviceId") or "").lower()
                    if "ven_1002" not in match:
                        continue
                    folder = None
                    for name in ("UserModeDriverName", "InstalledDisplayDrivers", "OpenGLDriverName"):
                        v = val(name)
                        first = v[0] if isinstance(v, list) and v else v
                        if isinstance(first, str) and os.path.isabs(first):
                            folder = os.path.dirname(first)
                            break
                    entries.append({
                        "desc": val("DriverDesc"),
                        "driver_version": val("DriverVersion"),
                        "driver_date": val("DriverDate"),
                        "software_version": val("RadeonSoftwareVersion"),
                        "folder": folder,
                    })
            except OSError:
                continue
    return entries


def _candidate_dlls() -> list[str]:
    paths: list[str] = []
    for entry in amd_driver_entries():
        folder = entry.get("folder")
        if folder:
            candidate = os.path.join(folder, "atiadlxx.dll")
            if os.path.isfile(candidate) and candidate not in paths:
                paths.append(candidate)
    paths += ["atiadlxx.dll", "atiadlxy.dll"]
    return paths


class Adl:
    """One ADL context. ``devices`` lists each physical Radeon exactly once."""

    def __init__(self) -> None:
        self._dll = None
        self._ctx = ctypes.c_void_p()
        self._keepalive = []
        self.devices: list[dict] = []
        self.available = False
        self.dll_path: str | None = None
        try:
            self._open()
        except Exception:
            self.available = False

    def _open(self) -> None:
        # The copy in System32 can belong to a different (staged or rolled-back)
        # driver package than the one actually running the card, in which case
        # every call fails. Try the active driver's own folder first.
        for path in _candidate_dlls():
            try:
                if os.path.isabs(path):
                    os.add_dll_directory(os.path.dirname(path))
                dll = ctypes.WinDLL(path)
            except OSError:
                continue
            try:
                self._init(dll)
            except Exception:
                pass
            if self.devices:
                self.dll_path = path
                self.available = True
                return
            self.close()

    def _init(self, dll) -> None:
        msvcrt = ctypes.CDLL("msvcrt.dll")
        msvcrt.malloc.argtypes = [ctypes.c_size_t]
        msvcrt.malloc.restype = ctypes.c_void_p
        malloc_cb = _MALLOC(lambda size: msvcrt.malloc(size))
        self._keepalive.append(malloc_cb)

        dll.ADL2_Main_Control_Create.argtypes = [_MALLOC, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        dll.ADL2_Main_Control_Create.restype = ctypes.c_int
        dll.ADL2_Main_Control_Destroy.argtypes = [ctypes.c_void_p]
        dll.ADL2_Adapter_NumberOfAdapters_Get.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        dll.ADL2_Adapter_AdapterInfo_Get.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        dll.ADL2_New_QueryPMLogData_Get.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.POINTER(_PMLogDataOutput)]
        dll.ADL2_New_QueryPMLogData_Get.restype = ctypes.c_int

        if dll.ADL2_Main_Control_Create(malloc_cb, 1, ctypes.byref(self._ctx)) != ADL_OK:
            return
        self._dll = dll

        count = ctypes.c_int(0)
        if dll.ADL2_Adapter_NumberOfAdapters_Get(self._ctx, ctypes.byref(count)) != ADL_OK:
            return
        if count.value <= 0:
            return
        infos = (_AdapterInfo * count.value)()
        if dll.ADL2_Adapter_AdapterInfo_Get(self._ctx, infos, ctypes.sizeof(infos)) != ADL_OK:
            return

        # ADL lists one entry per display output; collapse to physical cards.
        seen: set[tuple[int, int]] = set()
        for info in infos:
            # Depending on driver version the id comes back as 1002, 0x1002 or -1002.
            if abs(info.iVendorID) not in (AMD_VENDOR_ID, 0x1002) or not info.iExist:
                continue
            slot = (info.iBusNumber, info.iDeviceNumber)
            if slot in seen:
                continue
            probe = _PMLogDataOutput()
            if dll.ADL2_New_QueryPMLogData_Get(self._ctx, info.iAdapterIndex,
                                               ctypes.byref(probe)) != ADL_OK:
                continue
            seen.add(slot)
            self.devices.append({
                "adapter_index": info.iAdapterIndex,
                "name": info.strAdapterName.decode("mbcs", "replace").strip(),
                "pnp": info.strPNPString.decode("mbcs", "replace").strip().upper(),
                "bus": info.iBusNumber,
            })

    def read(self, adapter_index: int) -> dict[str, float]:
        """Return every supported sensor for one adapter."""
        if not self._dll:
            return {}
        data = _PMLogDataOutput()
        if self._dll.ADL2_New_QueryPMLogData_Get(self._ctx, adapter_index,
                                                 ctypes.byref(data)) != ADL_OK:
            return {}
        out: dict[str, float] = {}
        for idx, key, _unit in SENSORS:
            s = data.sensors[idx]
            if s.supported:
                out[key] = float(s.value)
        return out

    def read_raw(self, adapter_index: int) -> dict[int, int]:
        """Every supported sensor by raw index (diagnostics)."""
        if not self._dll:
            return {}
        data = _PMLogDataOutput()
        if self._dll.ADL2_New_QueryPMLogData_Get(self._ctx, adapter_index,
                                                 ctypes.byref(data)) != ADL_OK:
            return {}
        return {i: data.sensors[i].value for i in range(ADL_PMLOG_MAX_SENSORS)
                if data.sensors[i].supported}

    def close(self) -> None:
        if self._dll and self._ctx:
            try:
                self._dll.ADL2_Main_Control_Destroy(self._ctx)
            except Exception:
                pass
            self._ctx = ctypes.c_void_p()
            self._dll = None


if __name__ == "__main__":
    adl = Adl()
    print("available:", adl.available, "| dll:", adl.dll_path)
    print("drivers:", amd_driver_entries())
    names = {idx: key for idx, key, _ in SENSORS}
    for dev in adl.devices:
        print(dev)
        for idx, val in adl.read_raw(dev["adapter_index"]).items():
            print(f"  [{idx:3d}] {names.get(idx, '?'):18s} = {val}")
    adl.close()
