#!/usr/bin/env python3
"""
RigWatch -- live CPU / RAM / per-GPU / storage / power dashboard for Windows.

A single-file tkinter app. Refreshes once a second and draws everything on
canvases: ring gauges, 60-second sparkline history, per-core bars and embedded
PNG icons (base64 at the bottom of this file, so there is nothing to install
alongside it).

Where the numbers come from
---------------------------
CPU / RAM / disk throughput      psutil
GPU utilisation and VRAM         PDH performance counters (pdh.dll), the same
                                 WDDM counters Task Manager reads:
    \\GPU Engine(*)\\Utilization Percentage        -> per-adapter utilisation
    \\GPU Adapter Memory(*)\\Dedicated Usage       -> VRAM in use
    \\GPU Adapter Memory(*)\\Shared Usage          -> shared system memory

Instance names look like
    pid_1234_luid_0x00000000_0x0054E10A_phys_0_eng_0_engtype_3D
so utilisation is summed across every process per engine, and the busiest
engine on an adapter becomes that adapter's number -- the same aggregation Task
Manager shows.

Those counters identify adapters only by LUID, so adapter names and total VRAM
come from D3DKMT (gdi32: D3DKMTEnumAdapters2 + D3DKMTQueryAdapterInfo). That is
the WDDM kernel thunk, it shares the LUID namespace with the counters, and it
works where DXGI does not -- CreateDXGIFactory1 returns E_NOINTERFACE on some
machines. DXGI is kept as a fallback for the rare case D3DKMT is unavailable.

Storage: psutil supplies throughput and volume capacity; disk models, bus type
(NVMe / USB / SATA) and the volume-to-physical-disk mapping come from the
storage IOCTLs (IOCTL_STORAGE_QUERY_PROPERTY and IOCTL_STORAGE_GET_DEVICE_NUMBER),
which need no elevation and no PowerShell.

No WMI, no pywin32, no vendor SDK. psutil is the only external dependency.

Run:    python hw_monitor.py
Demo:   python hw_monitor.py --demo     (synthetic data, runs on any platform)
Debug:  python hw_monitor.py --debug    (dump adapters and raw counters)
"""

import argparse
import ctypes
import json
import math
import traceback
import os
import queue
import random
import re
import sys
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import font as tkfont

POLL_SECONDS = 1.0
UI_TICK_MS = 150
HISTORY = 60

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ==========================================================================
# Shared types
# ==========================================================================

class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


def luid_key(high, low):
    """Canonical, comparable LUID string; both halves as unsigned 32-bit hex."""
    return "%08X:%08X" % (high & 0xFFFFFFFF, low & 0xFFFFFFFF)


def _wchars(buf):
    """Decode a fixed-size UTF-16 buffer up to its first NUL."""
    raw = ctypes.string_at(ctypes.byref(buf), ctypes.sizeof(buf))
    return raw.decode("utf-16-le", "replace").split("\x00", 1)[0].strip()


# ==========================================================================
# Adapter discovery -- D3DKMT first, DXGI as fallback
# ==========================================================================

KMTQAITYPE_GETSEGMENTSIZE = 3
KMTQAITYPE_ADAPTERREGISTRYINFO = 8


class D3DKMT_ADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint32),
                ("AdapterLuid", LUID),
                ("NumOfSources", ctypes.c_uint32),
                ("bPrecisePresentRegionsPreferred", ctypes.c_int32)]


class D3DKMT_ENUMADAPTERS2(ctypes.Structure):
    _fields_ = [("NumAdapters", ctypes.c_uint32),
                ("pAdapters", ctypes.POINTER(D3DKMT_ADAPTERINFO))]


class D3DKMT_ADAPTERREGISTRYINFO(ctypes.Structure):
    _fields_ = [("AdapterString", ctypes.c_uint16 * 260),
                ("BiosString", ctypes.c_uint16 * 260),
                ("DllName", ctypes.c_uint16 * 260),
                ("RegistryPath", ctypes.c_uint16 * 260)]


class D3DKMT_SEGMENTSIZEINFO(ctypes.Structure):
    _fields_ = [("DedicatedVideoMemorySize", ctypes.c_uint64),
                ("DedicatedSystemMemorySize", ctypes.c_uint64),
                ("SharedSystemMemorySize", ctypes.c_uint64)]


class D3DKMT_QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint32),
                ("Type", ctypes.c_int32),
                ("pPrivateDriverData", ctypes.c_void_p),
                ("PrivateDriverDataSize", ctypes.c_uint32)]


class D3DKMT_CLOSEADAPTER(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint32)]


class D3DKMT_ADAPTERADDRESS(ctypes.Structure):
    _fields_ = [("BusNumber", ctypes.c_uint32),
                ("DeviceNumber", ctypes.c_uint32),
                ("FunctionNumber", ctypes.c_uint32)]


KMTQAITYPE_ADAPTERADDRESS = 6
# D3DKMT_ADAPTER_PERFDATA. Windows exposes it to user mode, which makes GPU
# temperature readable without any vendor library or kernel driver. The layout
# was confirmed empirically: at offset 8 the memory clock matches what Adrenalin
# reports exactly, and offset 56 is temperature in deci-Celsius.
KMTQAITYPE_ADAPTERPERFDATA = 62
PERFDATA_SIZE = 64
PERFDATA_MEMCLOCK_OFFSET = 8
PERFDATA_TEMPERATURE_OFFSET = 56


def _adapters_via_d3dkmt(diag):
    """Names + total VRAM + LUIDs straight from the WDDM kernel thunk."""
    out = []
    try:
        gdi = ctypes.WinDLL("gdi32.dll")
    except (OSError, AttributeError) as exc:
        diag.append("d3dkmt: gdi32 unavailable (%s)" % exc)
        return out

    try:
        enum = D3DKMT_ENUMADAPTERS2()
        status = gdi.D3DKMTEnumAdapters2(ctypes.byref(enum))
        if status != 0 or enum.NumAdapters == 0:
            diag.append("d3dkmt: count probe status=0x%08X n=%d"
                        % (status & 0xFFFFFFFF, enum.NumAdapters))
            return out

        buf = (D3DKMT_ADAPTERINFO * enum.NumAdapters)()
        enum.pAdapters = ctypes.cast(buf, ctypes.POINTER(D3DKMT_ADAPTERINFO))
        status = gdi.D3DKMTEnumAdapters2(ctypes.byref(enum))
        if status != 0:
            diag.append("d3dkmt: fill status=0x%08X" % (status & 0xFFFFFFFF))
            return out

        for i in range(min(enum.NumAdapters, len(buf))):
            info = buf[i]
            name, vram = "", 0
            reg = D3DKMT_ADAPTERREGISTRYINFO()
            q = D3DKMT_QUERYADAPTERINFO(
                info.hAdapter, KMTQAITYPE_ADAPTERREGISTRYINFO,
                ctypes.cast(ctypes.byref(reg), ctypes.c_void_p),
                ctypes.sizeof(reg))
            if gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q)) == 0:
                name = _wchars(reg.AdapterString)

            seg = D3DKMT_SEGMENTSIZEINFO()
            q2 = D3DKMT_QUERYADAPTERINFO(
                info.hAdapter, KMTQAITYPE_GETSEGMENTSIZE,
                ctypes.cast(ctypes.byref(seg), ctypes.c_void_p),
                ctypes.sizeof(seg))
            if gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q2)) == 0:
                vram = int(seg.DedicatedVideoMemorySize)

            try:
                gdi.D3DKMTCloseAdapter(
                    ctypes.byref(D3DKMT_CLOSEADAPTER(info.hAdapter)))
            except Exception:
                pass

            pci = None
            addr = D3DKMT_ADAPTERADDRESS()
            q3 = D3DKMT_QUERYADAPTERINFO(
                info.hAdapter, KMTQAITYPE_ADAPTERADDRESS,
                ctypes.cast(ctypes.byref(addr), ctypes.c_void_p),
                ctypes.sizeof(addr))
            if gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q3)) == 0:
                if addr.BusNumber != 0xFFFFFFFF:
                    pci = (int(addr.BusNumber), int(addr.DeviceNumber),
                           int(addr.FunctionNumber))

            key = luid_key(info.AdapterLuid.HighPart, info.AdapterLuid.LowPart)
            if not name:
                # Render-only / virtual adapters have no registry entry.
                diag.append("d3dkmt: skipped unnamed adapter luid=%s" % key)
                continue
            out.append({"name": name, "vendor": _vendor_of(name),
                        "total_vram": vram, "keys": {key}, "pci": pci})
        diag.append("d3dkmt: %d adapter(s)" % len(out))
    except Exception as exc:
        diag.append("d3dkmt: %r" % (exc,))
    return out


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        # UTF-16 units rather than c_wchar, so the layout is exactly the
        # Windows one (c_wchar is 4 bytes on some platforms).
        ("Description", ctypes.c_uint16 * 128),
        ("VendorId", ctypes.c_uint32), ("DeviceId", ctypes.c_uint32),
        ("SubSysId", ctypes.c_uint32), ("Revision", ctypes.c_uint32),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", LUID), ("Flags", ctypes.c_uint32),
    ]


IID_IDXGIFactory1 = GUID(0x770AAE78, 0xF26F, 0x4DBB,
                         (0xA9, 0x1D, 0x59, 0xD9, 0xE3, 0x00, 0xCE, 0x8C))
IID_IDXGIFactory = GUID(0x7B7166EC, 0x21C7, 0x44AE,
                        (0xB2, 0x1A, 0xC9, 0xAE, 0x32, 0x1A, 0xE3, 0x69))
DXGI_ADAPTER_FLAG_SOFTWARE = 2


def _com_call(ptr, index, restype, argtypes, *args):
    """Invoke method #index on the vtable of a COM interface pointer."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(ptr, *args)


def _adapters_via_dxgi(diag):
    """Fallback path. EnumAdapters1 is vtable slot 12, GetDesc1 slot 10."""
    out = []
    try:
        dxgi = ctypes.WinDLL("dxgi.dll")
    except (OSError, AttributeError) as exc:
        diag.append("dxgi: unavailable (%s)" % exc)
        return out

    # Both entry points are asked for IDXGIFactory1: EnumAdapters1 (slot 12)
    # and GetDesc1 (slot 10) only exist on that interface, so requesting the
    # older IID and then calling them would read past the vtable.
    for entry, iid in (("CreateDXGIFactory1", IID_IDXGIFactory1),
                       ("CreateDXGIFactory", IID_IDXGIFactory1)):
        try:
            create = getattr(dxgi, entry)
        except AttributeError:
            continue
        create.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        create.restype = ctypes.c_long
        factory = ctypes.c_void_p()
        hr = create(ctypes.byref(iid), ctypes.byref(factory))
        if hr != 0 or not factory.value:
            diag.append("dxgi: %s hr=0x%08X" % (entry, hr & 0xFFFFFFFF))
            continue
        try:
            i = 0
            while True:
                adapter = ctypes.c_void_p()
                hr = _com_call(factory, 12, ctypes.c_long,
                               [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                               ctypes.c_uint(i), ctypes.byref(adapter))
                if hr != 0 or not adapter.value:
                    break
                try:
                    desc = DXGI_ADAPTER_DESC1()
                    if _com_call(adapter, 10, ctypes.c_long,
                                 [ctypes.POINTER(DXGI_ADAPTER_DESC1)],
                                 ctypes.byref(desc)) == 0:
                        if not (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE):
                            name = _wchars(desc.Description) or "Unknown adapter"
                            hi = desc.AdapterLuid.HighPart
                            lo = desc.AdapterLuid.LowPart
                            out.append({
                                "name": name, "vendor": _vendor_of(name),
                                "total_vram": int(desc.DedicatedVideoMemory),
                                "keys": {luid_key(hi, lo)}, "pci": None,
                            })
                finally:
                    _com_call(adapter, 2, ctypes.c_ulong, [])
                i += 1
        finally:
            _com_call(factory, 2, ctypes.c_ulong, [])
        if out:
            diag.append("dxgi: %d adapter(s) via %s" % (len(out), entry))
            return out
    return out


def sample_gpu_perfdata(diag=None):
    """
    {luid_key: {"temp": celsius, "mem_clock": hz}} for every adapter.

    Cheap enough to call once a second: it is two kernel calls per adapter, and
    the handles are closed again immediately so nothing is held across samples.
    """
    out = {}
    if sys.platform != "win32":
        return out
    try:
        gdi = ctypes.WinDLL("gdi32.dll")
        enum = D3DKMT_ENUMADAPTERS2()
        if gdi.D3DKMTEnumAdapters2(ctypes.byref(enum)) != 0 or not enum.NumAdapters:
            return out
        buf = (D3DKMT_ADAPTERINFO * enum.NumAdapters)()
        enum.pAdapters = ctypes.cast(buf, ctypes.POINTER(D3DKMT_ADAPTERINFO))
        if gdi.D3DKMTEnumAdapters2(ctypes.byref(enum)) != 0:
            return out

        for i in range(min(enum.NumAdapters, len(buf))):
            info = buf[i]
            try:
                data = ctypes.create_string_buffer(PERFDATA_SIZE)
                ctypes.memset(data, 0, PERFDATA_SIZE)   # PhysicalAdapterIndex 0
                q = D3DKMT_QUERYADAPTERINFO(
                    info.hAdapter, KMTQAITYPE_ADAPTERPERFDATA,
                    ctypes.cast(data, ctypes.c_void_p), PERFDATA_SIZE)
                if gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q)) != 0:
                    continue
                raw = data.raw
                deci = int.from_bytes(
                    raw[PERFDATA_TEMPERATURE_OFFSET:
                        PERFDATA_TEMPERATURE_OFFSET + 4], "little")
                clock = int.from_bytes(
                    raw[PERFDATA_MEMCLOCK_OFFSET:
                        PERFDATA_MEMCLOCK_OFFSET + 8], "little")
                entry = {}
                # Sanity-gate the decode: a plausible die temperature only.
                # If another Windows build lays the struct out differently this
                # simply yields nothing instead of a wrong number.
                if 0 < deci <= 1500:
                    entry["temp"] = deci / 10.0
                if 0 < clock < 100_000_000_000:
                    entry["mem_clock"] = clock
                if entry:
                    key = luid_key(info.AdapterLuid.HighPart,
                                   info.AdapterLuid.LowPart)
                    out[key] = entry
            finally:
                try:
                    gdi.D3DKMTCloseAdapter(
                        ctypes.byref(D3DKMT_CLOSEADAPTER(info.hAdapter)))
                except Exception:
                    pass
    except Exception as exc:
        if diag is not None:
            diag.append("perfdata: %r" % (exc,))
    return out


def _vendor_of(name):
    low = name.lower()
    for needle, label in (("radeon", "AMD"), ("amd", "AMD"), ("geforce", "NVIDIA"),
                          ("nvidia", "NVIDIA"), ("quadro", "NVIDIA"),
                          ("intel", "Intel"), ("arc", "Intel")):
        if needle in low:
            return label
    return "GPU"


def enumerate_adapters(diag=None):
    """Hardware display adapters, newest-working API first."""
    diag = diag if diag is not None else []
    if sys.platform != "win32":
        return []
    adapters = _adapters_via_d3dkmt(diag)
    if not adapters:
        adapters = _adapters_via_dxgi(diag)
    return adapters


# ==========================================================================
# PDH GPU counters
# ==========================================================================

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_CSTATUS_VALID_DATA = 0x00000000
PDH_CSTATUS_NEW_DATA = 0x00000001

ENGINE_RE = re.compile(
    r"luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)_phys_(\d+)_eng_(\d+)_engtype_(.+?)"
    r"(?:#\d+)?$")
MEM_RE = re.compile(r"luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)_phys_(\d+)")


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_uint32), ("doubleValue", ctypes.c_double)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]


def pretty_engine(engtype):
    """'Video Codec Engine' stays, 'Compute 0' -> 'Compute'."""
    return re.sub(r"\s+\d+$", "", engtype or "").strip()


def aggregate_gpu_counters(engine_items, dedicated_items, shared_items):
    """
    Pure aggregation step, kept free of ctypes so it is testable off-Windows.
    Returns {luid_key: {'util', 'engine', 'vram_used', 'shared_used'}}
    """
    per_engine = {}
    for name, value in engine_items:
        m = ENGINE_RE.search(name)
        if not m:
            continue
        key = luid_key(int(m.group(1), 16), int(m.group(2), 16))
        slot = (m.group(3), m.group(4), m.group(5))
        per_engine.setdefault(key, {})
        per_engine[key][slot] = per_engine[key].get(slot, 0.0) + float(value)

    def sum_mem(items):
        out = {}
        for name, value in items:
            m = MEM_RE.search(name)
            if not m:
                continue
            key = luid_key(int(m.group(1), 16), int(m.group(2), 16))
            out[key] = out.get(key, 0.0) + float(value)
        return out

    dedicated, shared = sum_mem(dedicated_items), sum_mem(shared_items)

    result = {}
    for key in set(per_engine) | set(dedicated) | set(shared):
        entry = {"util": None, "engine": None, "vram_used": None,
                 "shared_used": None}
        engines = per_engine.get(key)
        if engines:
            slot, value = max(engines.items(), key=lambda kv: kv[1])
            entry["util"] = max(0.0, min(100.0, value))
            entry["engine"] = pretty_engine(slot[2])
        if key in dedicated:
            entry["vram_used"] = int(dedicated[key])
        if key in shared:
            entry["shared_used"] = int(shared[key])
        result[key] = entry
    return result


def parse_energy_meter(items):
    """
    Watts from the Energy Meter counter set (values are milliwatts).

    Instances look like RAPL_Package0_PKG / RAPL_Package0_Core3_CORE. The
    package rail is what matters; sum it across sockets. _Total often reads 0,
    so it is only a fallback.
    """
    package = core = 0.0
    have_package = have_core = False
    total = None
    for name, value in items:
        upper = (name or "").upper()
        if upper.endswith("_PKG"):
            package += value
            have_package = True
        elif upper.endswith("_CORE"):
            core += value
            have_core = True
        elif upper.strip("_") == "TOTAL":
            total = value
    if have_package and package > 0:
        return package / 1000.0
    if total:
        return total / 1000.0
    if have_core and core > 0:
        return core / 1000.0
    return None


def parse_thermal_zones(items):
    """
    Hottest ACPI thermal zone in Celsius, or None.

    The counter is in Kelvin. Zones are firmware-defined: on a laptop one of
    them normally tracks the CPU, on most desktops none exist at all. Values
    outside a sane range are discarded rather than displayed.
    """
    best = None
    for name, value in items:
        if (name or "").strip("_").lower() == "total":
            continue
        celsius = float(value) - 273.15
        if 5.0 < celsius < 150.0:
            best = celsius if best is None else max(best, celsius)
    return best


# Board power (TBP) and rough idle draw per card, matched by name substring.
# Longest match wins. Anything unknown falls back to DEFAULT_GPU_POWER.
GPU_POWER_TABLE = {
    # AMD
    "9070 XT": (304, 18), "9070": (220, 15),
    "7900 XTX": (355, 22), "7900 XT": (315, 20), "7900 GRE": (260, 17),
    "7800 XT": (263, 16), "7700 XT": (245, 15), "7600 XT": (190, 12),
    "7600": (165, 11),
    "6950 XT": (335, 20), "6900 XT": (300, 18), "6800 XT": (300, 18),
    "6800": (250, 16), "6750 XT": (250, 15), "6700 XT": (230, 13),
    "6700": (175, 12), "6650 XT": (176, 11), "6600 XT": (160, 10),
    "6600": (132, 9), "6500 XT": (107, 7),
    "5700 XT": (225, 13), "5700": (180, 11), "5600 XT": (150, 10),
    # NVIDIA
    "5090": (575, 26), "5080": (360, 20), "5070 Ti": (300, 18),
    "5070": (250, 16), "5060 Ti": (180, 12), "5060": (145, 10),
    "4090": (450, 22), "4080 SUPER": (320, 20), "4080": (320, 20),
    "4070 Ti SUPER": (285, 16), "4070 Ti": (285, 16),
    "4070 SUPER": (220, 14), "4070": (200, 14),
    "4060 Ti": (160, 12), "4060": (115, 10),
    "3090 Ti": (450, 26), "3090": (350, 25), "3080 Ti": (350, 22),
    "3080": (320, 22), "3070 Ti": (290, 18), "3070": (220, 16),
    "3060 Ti": (200, 14), "3060": (170, 12), "3050": (130, 10),
    "2080 Ti": (250, 18), "2080": (225, 16), "2070": (175, 12),
    "2060": (160, 11), "1660": (120, 8), "1650": (75, 7),
    # Intel Arc
    "B580": (190, 11), "B570": (150, 10),
    "A770": (225, 12), "A750": (190, 11), "A580": (175, 10),
    "A380": (75, 8), "A310": (75, 7),
}
DEFAULT_GPU_POWER = (200, 15)
INTEGRATED_GPU_POWER = (25, 4)
# Integrated parts share the CPU package power budget, so a discrete-card
# default would wildly overstate them.
INTEGRATED_HINTS = ("uhd graphics", "hd graphics", "iris", "radeon graphics",
                    "radeon(tm) graphics", "vega 3", "vega 6", "vega 7",
                    "vega 8", "vega 11", "610m", "660m", "680m", "760m",
                    "780m", "880m", "890m", "microsoft basic")
DEFAULT_CPU_PACKAGE_W = 88
DEFAULT_BASELINE_W = 60
DEFAULT_PSU_W = 750
CONFIG_NAME = "monitor_config.json"


def gpu_power_profile(name, total_vram=None):
    low = (name or "").lower()
    best = None
    for key, value in GPU_POWER_TABLE.items():
        if key.lower() in low:
            if best is None or len(key) > len(best[0]):
                best = (key, value)
    if best:
        return best[1]
    if any(hint in low for hint in INTEGRATED_HINTS):
        return INTEGRATED_GPU_POWER
    # An adapter with a sliver of dedicated memory is an iGPU, not a 200 W card.
    if total_vram is not None and 0 < total_vram < 1 * 2**30:
        return INTEGRATED_GPU_POWER
    return DEFAULT_GPU_POWER


def as_number(value, fallback, minimum=0.0, maximum=1e6):
    """Config files get hand-edited. Never let a bad value reach the sampler."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return max(minimum, min(maximum, number))


CONFIG_PATH = None


def config_file_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


def save_config(config):
    """Best effort - losing a remembered window position is not worth a crash."""
    try:
        with open(config_file_path(), "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
        return True
    except Exception:
        return False


def load_power_config(adapters, diag):
    """
    Read (or create) monitor_config.json next to the script. Everything in it
    is a nameplate figure the user can correct - nothing here is measured.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)
    config = {"psu_watts": DEFAULT_PSU_W,
              "cpu_package_watts": DEFAULT_CPU_PACKAGE_W,
              # Where the minimised strip parks itself, as an offset from the
              # left edge of the taskbar - clear of the weather widget.
              "mini_offset_x": 172,
              "mini_position": None,
              "baseline_items": dict(DEFAULT_BASELINE_ITEMS),
              "librehardwaremonitor_url": DEFAULT_LHM_URL,
              "gpu_power": {}}
    for adapter in adapters:
        tbp, idle = gpu_power_profile(adapter["name"], adapter.get("total_vram"))
        config["gpu_power"][adapter["name"]] = {"tbp": tbp, "idle": idle}
    sticks = count_memory_sticks()
    if sticks:
        config["baseline_items"]["ram_sticks"] = sticks
        diag.append("power: %d memory stick(s) detected" % sticks)

    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            for key, low, high in (("psu_watts", 50, 5000),
                                   ("cpu_package_watts", 5, 1000),
                                   ("mini_offset_x", 0, 10000)):
                if key in saved:
                    config[key] = as_number(saved[key], config[key], low, high)
            if isinstance(saved.get("baseline_items"), dict):
                for key, value in saved["baseline_items"].items():
                    if key in config["baseline_items"]:
                        config["baseline_items"][key] = as_number(
                            value, config["baseline_items"][key], 0, 2000)
            if isinstance(saved.get("librehardwaremonitor_url"), str):
                config["librehardwaremonitor_url"] = saved["librehardwaremonitor_url"]
            if saved.get("system_baseline_watts") is not None:
                config["system_baseline_watts"] = as_number(
                    saved["system_baseline_watts"], 0, 0, 2000)
            position = saved.get("mini_position")
            if (isinstance(position, (list, tuple)) and len(position) == 2
                    and all(isinstance(v, (int, float)) for v in position)):
                config["mini_position"] = [int(position[0]), int(position[1])]
            for name, entry in (saved.get("gpu_power") or {}).items():
                if isinstance(entry, dict):
                    known = config["gpu_power"].get(name) or {}
                    config["gpu_power"][name] = {
                        "tbp": as_number(entry.get("tbp"),
                                         known.get("tbp", DEFAULT_GPU_POWER[0]),
                                         1, 2000),
                        "idle": as_number(entry.get("idle"),
                                          known.get("idle", DEFAULT_GPU_POWER[1]),
                                          0, 500)}
            diag.append("power: config loaded from %s" % CONFIG_NAME)
        else:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2)
            diag.append("power: wrote default %s" % CONFIG_NAME)
    except Exception as exc:
        diag.append("power: config error (%r)" % (exc,))
    return config


# Everything the sensors cannot see, itemised so each figure is visible and
# editable rather than hidden inside one lump number.
DEFAULT_BASELINE_ITEMS = {
    "motherboard": 35,      # chipset, VRM losses, audio, LAN, RGB
    "ram_per_stick": 3,
    "ram_sticks": 2,
    "fans": 8,              # case + CPU cooler
    "per_drive": 4,
    "usb_peripherals": 10,
}


def count_memory_sticks():
    """
    Populated DIMM slots, straight from the firmware's SMBIOS table
    (GetSystemFirmwareTable('RSMB'), type 17 records). No WMI, no elevation -
    it just means the RAM figure matches the machine it is running on.
    """
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetSystemFirmwareTable.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                               ctypes.c_void_p, ctypes.c_uint32]
        k32.GetSystemFirmwareTable.restype = ctypes.c_uint32
        signature = 0x52534D42                      # 'RSMB', little endian
        size = k32.GetSystemFirmwareTable(signature, 0, None, 0)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not k32.GetSystemFirmwareTable(signature, 0, buf, size):
            return None
        data = buf.raw[8:]                          # skip RawSMBIOSData header
        offset, sticks = 0, 0
        while offset + 4 <= len(data):
            record_type = data[offset]
            length = data[offset + 1]
            if length < 4:
                break
            if record_type == 127:                  # end-of-table
                break
            if record_type == 17 and offset + 0x0E <= len(data):
                capacity = int.from_bytes(data[offset + 0x0C:offset + 0x0E],
                                          "little")
                if capacity not in (0, 0xFFFF):     # 0 = slot empty
                    sticks += 1
            cursor = offset + length                # then the string table,
            while cursor + 1 < len(data) and data[cursor:cursor + 2] != b"\x00\x00":
                cursor += 1                         # terminated by a double NUL
            offset = cursor + 2
        return sticks or None
    except Exception:
        return None


def baseline_breakdown(config, disk_count):
    """(total_watts, {label: watts}) for the parts nothing reports."""
    items = dict(DEFAULT_BASELINE_ITEMS)
    items.update(config.get("baseline_items") or {})
    get = lambda key: as_number(items.get(key), DEFAULT_BASELINE_ITEMS[key],
                                0, 2000)
    parts = {
        "board": get("motherboard"),
        "RAM": get("ram_per_stick") * get("ram_sticks"),
        "fans": get("fans"),
        "drives": get("per_drive") * max(1, disk_count),
        "USB": get("usb_peripherals"),
    }
    override = config.get("system_baseline_watts")
    total = sum(parts.values())
    if isinstance(override, (int, float)) and override > 0:
        # An explicit override wins, but keep the shape for the readout.
        factor = override / total if total else 1.0
        parts = dict((k, v * factor) for k, v in parts.items())
        total = float(override)
    return total, parts


# ---------------------------------------------------------------------------
# Optional bridge to LibreHardwareMonitor
#
# Windows exposes CPU package power (RAPL) but nothing for GPU board power, and
# AMD's ADL PMLog refuses on this driver. LibreHardwareMonitor is free, open
# source, and publishes every sensor it reads over a local HTTP endpoint, so if
# it happens to be running we take real watts from it instead of modelling them.
# MSI Center and the free build of CPUID HWMonitor have no such interface.
# ---------------------------------------------------------------------------

DEFAULT_LHM_URL = "http://127.0.0.1:8085/data.json"


def _lhm_number(text):
    """LHM formats values for the local culture: '48.2 W' or '48,2 W'."""
    if not text:
        return None
    cleaned = str(text).replace("\u00a0", " ").strip()
    cleaned = cleaned.split(" ")[0].replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_lhm_tree(node, path=None, out=None):
    """Flatten the LHM JSON tree into [(path_tuple, label, value, unit), ...]."""
    path = path or ()
    out = out if out is not None else []
    if not isinstance(node, dict):
        return out
    label = (node.get("Text") or "").strip()
    value = node.get("Value")
    if isinstance(value, str) and value.strip():
        number = _lhm_number(value)
        if number is not None:
            unit = value.replace("\u00a0", " ").strip().split(" ")[-1]
            out.append((path, label, number, unit))
    for child in (node.get("Children") or []):
        parse_lhm_tree(child, path + ((label,) if label else ()), out)
    return out


def lhm_pick(readings, hardware_match, sensor_names, unit):
    """Best-ranked sensor of `unit` under a hardware node matching the key."""
    hardware_match = (hardware_match or "").lower()
    unit = unit.lower()
    best = None
    for path, label, value, sensor_unit in readings:
        if sensor_unit.lower() != unit:
            continue
        branch = " ".join(path).lower()
        if hardware_match and hardware_match not in branch:
            continue
        for rank, wanted in enumerate(sensor_names):
            if label.lower() == wanted.lower():
                if best is None or rank < best[0]:
                    best = (rank, value)
    return best[1] if best else None


class LhmBridge:
    """Polls LibreHardwareMonitor's web server; silent no-op when it is absent."""

    def __init__(self, url=None, enabled=True):
        self.url = url or DEFAULT_LHM_URL
        self.enabled = enabled
        self.readings = []
        self.available = False
        self.last_error = None
        self._next_try = 0.0

    def poll(self):
        if not self.enabled:
            return False
        now = time.monotonic()
        if now < self._next_try:
            return self.available
        try:
            import urllib.request
            with urllib.request.urlopen(self.url, timeout=0.6) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            self.readings = parse_lhm_tree(payload)
            self.available = bool(self.readings)
            self.last_error = None
            self._next_try = 0.0
        except Exception as exc:
            self.readings = []
            self.available = False
            self.last_error = "%s" % (exc,)
            self._next_try = now + 20.0     # do not hammer a closed port
        return self.available

    @staticmethod
    def _gpu_key(name):
        # LHM names the node after the card, e.g. "AMD Radeon RX 7800 XT",
        # so the model number is the safest thing to match on.
        tokens = [t for t in (name or "").split() if any(c.isdigit() for c in t)]
        return tokens[-1] if tokens else (name or "")

    def cpu_watts(self):
        return lhm_pick(self.readings, "",
                        ["CPU Package", "Package", "CPU PPT", "CPU Total",
                         "Total Output Power"], "W")

    def gpu_watts(self, name):
        return lhm_pick(self.readings, self._gpu_key(name),
                        ["GPU Package", "Total Graphics (TGP)",
                         "Total Graphics", "GPU Total", "GPU Power", "GPU PPT",
                         "Board Power", "GPU Core"], "W")

    def cpu_temp(self):
        return lhm_pick(self.readings, "",
                        ["Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)",
                         "Cores (Max)", "CPU Cores", "Core Average", "CPU Core",
                         "Package"], "\u00b0C")

    def gpu_temp(self, name):
        return lhm_pick(self.readings, self._gpu_key(name),
                        ["GPU Core", "GPU", "GPU Temperature", "GPU Hot Spot",
                         "GCD", "GPU Package", "Core"], "\u00b0C")



# ---------------------------------------------------------------------------
# NVIDIA: NVML
#
# nvml.dll ships with every NVIDIA driver - it is the library behind
# nvidia-smi - and hands over power draw and temperature directly, with no
# extra software and no kernel driver of our own. This is the path AMD's ADL
# refused to provide, so NVIDIA users get measured watts out of the box.
# ---------------------------------------------------------------------------

NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0


class NvmlPciInfo(ctypes.Structure):
    # busId is deliberately over-sized: the struct grew between NVML versions
    # and writing into a buffer larger than the library expects is harmless.
    _fields_ = [("busIdLegacy", ctypes.c_char * 16),
                ("domain", ctypes.c_uint32), ("bus", ctypes.c_uint32),
                ("device", ctypes.c_uint32), ("pciDeviceId", ctypes.c_uint32),
                ("pciSubSystemId", ctypes.c_uint32),
                ("busId", ctypes.c_char * 64)]


class NvmlBridge:
    """Live NVIDIA metrics keyed by PCI address. A no-op without the driver."""

    def __init__(self):
        self.available = False
        self.last_error = None
        self._lib = None
        self._devices = {}          # (bus, device) -> handle
        self._by_name = {}          # lowercase name -> handle
        self.readings = {}          # (bus, device) -> {"watts", "temp", "fan"}
        self._open()

    def _open(self):
        if sys.platform != "win32":
            return
        for candidate in ("nvml.dll",
                          r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"):
            try:
                lib = ctypes.CDLL(candidate)
            except OSError:
                continue
            try:
                if lib.nvmlInit_v2() != NVML_SUCCESS:
                    continue
            except AttributeError:
                continue
            self._lib = lib
            break
        if self._lib is None:
            self.last_error = "nvml.dll not present (no NVIDIA driver)"
            return

        try:
            count = ctypes.c_uint32(0)
            if self._lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != NVML_SUCCESS:
                return
            for index in range(count.value):
                handle = ctypes.c_void_p()
                if self._lib.nvmlDeviceGetHandleByIndex_v2(
                        ctypes.c_uint32(index), ctypes.byref(handle)) != NVML_SUCCESS:
                    continue
                name = ctypes.create_string_buffer(96)
                if self._lib.nvmlDeviceGetName(handle, name, 96) == NVML_SUCCESS:
                    self._by_name[name.value.decode("latin-1").strip().lower()] = handle
                pci = NvmlPciInfo()
                if self._lib.nvmlDeviceGetPciInfo_v3(
                        handle, ctypes.byref(pci)) == NVML_SUCCESS:
                    self._devices[(int(pci.bus), int(pci.device))] = handle
            self.available = bool(self._devices or self._by_name)
        except Exception as exc:
            self.last_error = "%r" % (exc,)

    def close(self):
        if self._lib is not None:
            try:
                self._lib.nvmlShutdown()
            except Exception:
                pass
            self._lib = None

    def _handle_for(self, adapter):
        pci = adapter.get("pci")
        if pci:
            handle = self._devices.get((pci[0], pci[1]))
            if handle is not None:
                return handle
        name = (adapter.get("name") or "").strip().lower()
        if name in self._by_name:
            return self._by_name[name]
        for known, handle in self._by_name.items():
            if known and (known in name or name in known):
                return handle
        return None

    def poll(self, adapters):
        """Refresh readings for the adapters we were given. Never raises."""
        if not self.available or self._lib is None:
            return False
        readings = {}
        for adapter in adapters:
            handle = self._handle_for(adapter)
            if handle is None:
                continue
            entry = {}
            try:
                milliwatts = ctypes.c_uint32(0)
                if self._lib.nvmlDeviceGetPowerUsage(
                        handle, ctypes.byref(milliwatts)) == NVML_SUCCESS:
                    entry["watts"] = milliwatts.value / 1000.0
            except Exception:
                pass
            try:
                celsius = ctypes.c_uint32(0)
                if self._lib.nvmlDeviceGetTemperature(
                        handle, NVML_TEMPERATURE_GPU,
                        ctypes.byref(celsius)) == NVML_SUCCESS:
                    entry["temp"] = float(celsius.value)
            except Exception:
                pass
            if entry:
                readings[adapter["name"]] = entry
        self.readings = readings
        return bool(readings)

    def get(self, adapter_name, field):
        return (self.readings.get(adapter_name) or {}).get(field)


def estimate_gpu_watts(util, tbp, idle):
    """Load-curve estimate. Not a measurement - the UI labels it as such."""
    if util is None:
        return None
    fraction = max(0.0, min(1.0, util / 100.0)) ** 0.85
    return idle + (tbp - idle) * fraction


def estimate_cpu_watts(util, package_limit):
    if util is None:
        return None
    idle = max(8.0, package_limit * 0.22)
    fraction = max(0.0, min(1.0, util / 100.0)) ** 0.9
    return idle + (package_limit - idle) * fraction


class GpuCounters:
    """Thin PDH wrapper. Never raises: failures degrade to empty results."""

    ENGINE_PATH = r"\GPU Engine(*)\Utilization Percentage"
    DEDICATED_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"
    SHARED_PATH = r"\GPU Adapter Memory(*)\Shared Usage"
    # AMD/Intel RAPL rails surface here as milliwatts; present on most desktops
    # and readable without elevation. This is a measured value, not a model.
    ENERGY_PATH = r"\Energy Meter(*)\Power"
    # ACPI thermal zones. Desktop firmware usually implements none, but most
    # laptops do, and this is the only route to a CPU-side temperature that
    # does not need a kernel driver. Reported in Kelvin.
    THERMAL_PATH = r"\Thermal Zone Information(*)\Temperature"

    def __init__(self):
        self._pdh = None
        self._query = None
        self._counters = {}
        self.last_error = None
        self.raw_instances = {}
        self.cpu_watts = None
        self.cpu_temp = None
        self.energy_available = False
        self._open()

    def _open(self):
        try:
            pdh = ctypes.WinDLL("pdh.dll")
        except (OSError, AttributeError) as exc:
            self.last_error = "pdh.dll unavailable: %s" % exc
            return

        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhOpenQueryW.restype = ctypes.c_long
        pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                              ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhCollectQueryData.restype = ctypes.c_long
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p]
        pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
        pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        pdh.PdhCloseQuery.restype = ctypes.c_long

        query = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
            self.last_error = "PdhOpenQuery failed"
            return

        counters = {}
        for label, path in (("engine", self.ENGINE_PATH),
                            ("dedicated", self.DEDICATED_PATH),
                            ("shared", self.SHARED_PATH),
                            ("energy", self.ENERGY_PATH),
                            ("thermal", self.THERMAL_PATH)):
            handle = ctypes.c_void_p()
            status = pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(handle))
            if status == 0:
                counters[label] = handle
                if label == "energy":
                    self.energy_available = True
            elif label not in ("energy", "thermal"):
                self.last_error = "counter unavailable: %s (0x%08X)" % (
                    path, status & 0xFFFFFFFF)

        if not counters:
            pdh.PdhCloseQuery(query)
            return

        self._pdh, self._query, self._counters = pdh, query, counters
        pdh.PdhCollectQueryData(query)  # rate counters need a baseline sample

    def close(self):
        if self._pdh and self._query:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except Exception:
                pass
        self._pdh = self._query = None
        self._counters = {}

    def reopen(self):
        self.close()
        self._open()

    @property
    def available(self):
        return self._query is not None

    def _read_array(self, handle):
        pdh = self._pdh
        fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        size = ctypes.c_uint32(0)
        count = ctypes.c_uint32(0)
        status = pdh.PdhGetFormattedCounterArrayW(
            handle, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if (status & 0xFFFFFFFF) != PDH_MORE_DATA or size.value == 0:
            return []

        buf = ctypes.create_string_buffer(size.value)
        status = pdh.PdhGetFormattedCounterArrayW(
            handle, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if status != 0 or count.value == 0:
            return []

        items = ctypes.cast(
            buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W * count.value)).contents
        out = []
        for item in items:
            if item.FmtValue.CStatus not in (PDH_CSTATUS_VALID_DATA,
                                             PDH_CSTATUS_NEW_DATA):
                continue
            if item.szName:
                out.append((item.szName, item.FmtValue.doubleValue))
        return out

    def sample(self):
        if not self.available:
            return {}
        try:
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return {}
            raw = {}
            for label in ("engine", "dedicated", "shared", "energy", "thermal"):
                handle = self._counters.get(label)
                raw[label] = self._read_array(handle) if handle else []
            self.raw_instances = raw
            self.cpu_watts = parse_energy_meter(raw["energy"])
            self.cpu_temp = parse_thermal_zones(raw["thermal"])
            return aggregate_gpu_counters(raw["engine"], raw["dedicated"],
                                          raw["shared"])
        except Exception as exc:
            self.last_error = "sample failed: %r" % (exc,)
            return {}


# ==========================================================================
# Storage inventory + throughput
# ==========================================================================

GENERIC_NONE = 0
FILE_SHARE_RW = 3
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400

BUS_TYPES = {1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre",
             7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD",
             13: "MMC", 14: "Virtual", 15: "Virtual", 16: "Storage Spaces",
             17: "NVMe", 18: "SCM", 19: "UFS"}


class STORAGE_DEVICE_NUMBER(ctypes.Structure):
    _fields_ = [("DeviceType", ctypes.c_uint32),
                ("DeviceNumber", ctypes.c_uint32),
                ("PartitionNumber", ctypes.c_uint32)]


class STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [("PropertyId", ctypes.c_uint32),
                ("QueryType", ctypes.c_uint32),
                ("AdditionalParameters", ctypes.c_ubyte * 1)]


class STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_uint32), ("Size", ctypes.c_uint32),
                ("DeviceType", ctypes.c_ubyte),
                ("DeviceTypeModifier", ctypes.c_ubyte),
                ("RemovableMedia", ctypes.c_ubyte),
                ("CommandQueueing", ctypes.c_ubyte),
                ("VendorIdOffset", ctypes.c_uint32),
                ("ProductIdOffset", ctypes.c_uint32),
                ("ProductRevisionOffset", ctypes.c_uint32),
                ("SerialNumberOffset", ctypes.c_uint32),
                ("BusType", ctypes.c_uint32),
                ("RawPropertiesLength", ctypes.c_uint32)]


_K32 = None


def _kernel32():
    """kernel32 with the storage-IOCTL prototypes set up once."""
    global _K32
    if _K32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p]
        k.CreateFileW.restype = ctypes.c_void_p
        k.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.POINTER(ctypes.c_uint32),
                                      ctypes.c_void_p]
        k.DeviceIoControl.restype = ctypes.c_int
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        _K32 = k
    return _K32


def _open_device(path):
    """Open with zero access rights - enough for these IOCTLs, no elevation."""
    k = _kernel32()
    handle = k.CreateFileW(path, GENERIC_NONE, FILE_SHARE_RW, None,
                           OPEN_EXISTING, 0, None)
    if handle is None or handle == INVALID_HANDLE:
        return None
    return handle


def volume_disk_index(letter):
    """'C:' -> 0 for PhysicalDrive0, or None."""
    handle = _open_device(r"\\.\%s" % letter)
    if handle is None:
        return None
    k = _kernel32()
    try:
        sdn = STORAGE_DEVICE_NUMBER()
        written = ctypes.c_uint32()
        ok = k.DeviceIoControl(handle, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
                               ctypes.byref(sdn), ctypes.sizeof(sdn),
                               ctypes.byref(written), None)
        return int(sdn.DeviceNumber) if ok else None
    except Exception:
        return None
    finally:
        k.CloseHandle(handle)


def disk_identity(index):
    """Model / bus type for PhysicalDrive<index>, or None."""
    handle = _open_device(r"\\.\PhysicalDrive%d" % index)
    if handle is None:
        return None
    k = _kernel32()
    try:
        query = STORAGE_PROPERTY_QUERY(0, 0, (ctypes.c_ubyte * 1)())
        buf = ctypes.create_string_buffer(2048)
        written = ctypes.c_uint32()
        ok = k.DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY,
                               ctypes.byref(query), ctypes.sizeof(query),
                               buf, ctypes.sizeof(buf), ctypes.byref(written),
                               None)
        if not ok:
            return None
        desc = ctypes.cast(buf,
                           ctypes.POINTER(STORAGE_DEVICE_DESCRIPTOR)).contents

        def text_at(offset):
            if not offset or offset >= len(buf.raw):
                return ""
            return buf.raw[offset:].split(b"\x00", 1)[0].decode("latin-1").strip()

        vendor, product = text_at(desc.VendorIdOffset), text_at(desc.ProductIdOffset)
        model = (vendor + " " + product).strip() or product or vendor
        return {"model": model or None,
                "bus": BUS_TYPES.get(desc.BusType, "disk")}
    except Exception:
        return None
    finally:
        k.CloseHandle(handle)


def disk_inventory(diag):
    """[{'index', 'model', 'bus', 'letters'}] for each physical disk."""
    if sys.platform != "win32":
        return []
    try:
        import psutil
    except Exception as exc:
        diag.append("storage: psutil unavailable (%r)" % (exc,))
        return []

    disks = {}
    try:
        for name in psutil.disk_io_counters(perdisk=True) or {}:
            digits = "".join(c for c in name if c.isdigit())
            if digits:
                disks[int(digits)] = {"index": int(digits), "model": None,
                                      "bus": None, "letters": []}
    except Exception as exc:
        diag.append("storage: io counters failed (%r)" % (exc,))

    try:
        for part in psutil.disk_partitions(all=False):
            letter = part.mountpoint.rstrip("\\")
            if not letter.endswith(":"):
                continue
            index = volume_disk_index(letter)
            if index is None:
                continue
            entry = disks.setdefault(index, {"index": index, "model": None,
                                             "bus": None, "letters": []})
            entry["letters"].append(letter)
    except Exception as exc:
        diag.append("storage: partition mapping failed (%r)" % (exc,))

    for index, entry in disks.items():
        ident = disk_identity(index)
        if ident:
            entry["model"] = ident["model"]
            entry["bus"] = ident["bus"]
        if not entry["model"]:
            entry["model"] = "Disk %d" % index
        entry["letters"].sort()
    diag.append("storage: %d disk(s)" % len(disks))
    return [disks[k] for k in sorted(disks)]


class DiskRates:
    """Per-physical-disk read/write throughput from psutil counters."""

    def __init__(self):
        self._prev = {}
        self._prev_t = None

    def sample(self, inventory):
        try:
            import psutil
            counters = psutil.disk_io_counters(perdisk=True)
        except Exception:
            return {}
        now = time.monotonic()
        dt = (now - self._prev_t) if self._prev_t else None
        self._prev_t = now
        out = {}
        for disk in inventory:
            key = "PhysicalDrive%d" % disk["index"]
            cur = counters.get(key)
            if cur is None:
                out[disk["index"]] = {"read": None, "write": None}
                continue
            prev = self._prev.get(key)
            self._prev[key] = cur
            if prev is None or not dt or dt <= 0:
                out[disk["index"]] = {"read": None, "write": None}
                continue
            out[disk["index"]] = {
                "read": max(0.0, (cur.read_bytes - prev.read_bytes) / dt),
                "write": max(0.0, (cur.write_bytes - prev.write_bytes) / dt),
            }
        return out


def disk_capacity(letters):
    """(used, total) summed over the volumes living on one physical disk."""
    try:
        import psutil
    except Exception:
        return None
    used = total = 0
    seen = False
    for letter in letters:
        try:
            usage = psutil.disk_usage(letter + "\\")
        except Exception:
            continue
        used += usage.used
        total += usage.total
        seen = True
    return (used, total) if seen else None


# ==========================================================================
# Sampling threads
# ==========================================================================

class Sampler(threading.Thread):
    def __init__(self, adapters, disks, out_queue, debug=False, power=None):
        super().__init__(daemon=True)
        self.adapters = adapters
        self.disks = disks
        self.power = power or {}
        self.queue = out_queue
        self.debug = debug
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def emit(self, sample):
        try:
            self.queue.put_nowait(sample)
        except queue.Full:
            pass

    def run(self):
        try:
            self._run()
        except Exception:
            traceback.print_exc()
            self.emit({"cpu": None, "cores": None, "freq": None,
                       "counts": (None, None), "ram": None, "swap": None,
                       "uptime": None, "cpu_temp": None, "gpus": [], "disks": [],
                       "power": None,
                       "note": "sampler stopped: see console for details"})

    def _run(self):
        import psutil

        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            pass
        counters = GpuCounters()
        rates = DiskRates()
        bridge = LhmBridge(self.power.get("librehardwaremonitor_url"),
                           enabled=bool(self.power.get("librehardwaremonitor_url")))
        nvml = NvmlBridge()
        if self.debug:
            print("  nvml: %s" % ("ready" if nvml.available else nvml.last_error))
        try:
            counts = (psutil.cpu_count(logical=True),
                      psutil.cpu_count(logical=False))
        except Exception:
            counts = (None, None)
        empty_streak = 0
        tick = 0

        while not self._stop.is_set():
            tick += 1
            sample = {"cpu": None, "cores": None, "freq": None, "counts": counts,
                      "ram": None, "swap": None, "uptime": None,
                      "cpu_temp": None, "gpus": [], "disks": [],
                      "power": None, "note": None}
            try:
                sample["cpu"] = psutil.cpu_percent(interval=None)
                sample["cores"] = psutil.cpu_percent(interval=None, percpu=True)
            except Exception:
                pass
            try:
                freq = psutil.cpu_freq()
                sample["freq"] = freq.current if freq else None
            except Exception:
                pass
            try:
                vm = psutil.virtual_memory()
                sample["ram"] = (vm.total - vm.available, vm.total, vm.percent)
            except Exception:
                pass
            try:
                sw = psutil.swap_memory()
                sample["swap"] = (sw.used, sw.total, sw.percent)
            except Exception:
                pass
            try:
                sample["uptime"] = time.time() - psutil.boot_time()
            except Exception:
                pass

            try:
                gpu_data = counters.sample()
            except Exception:
                gpu_data = {}

            if gpu_data:
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak in (10, 60):   # driver restart etc: rebuild query
                    try:
                        counters.reopen()
                    except Exception:
                        pass
            if not counters.available:
                sample["note"] = counters.last_error or "GPU counters unavailable"

            try:
                perfdata = sample_gpu_perfdata()
            except Exception:
                perfdata = {}

            for adapter in self.adapters:
                entry = next((gpu_data[k] for k in adapter["keys"] if k in gpu_data),
                             None)
                perf = next((perfdata[k] for k in adapter["keys"]
                             if k in perfdata), None) or {}
                sample["gpus"].append({
                    "name": adapter["name"],
                    "vendor": adapter.get("vendor", "GPU"),
                    "total_vram": adapter["total_vram"],
                    "util": entry["util"] if entry else None,
                    "temp": perf.get("temp"),
                    "mem_clock": perf.get("mem_clock"),
                    "engine": entry["engine"] if entry else None,
                    "vram_used": entry["vram_used"] if entry else None,
                    "shared_used": entry["shared_used"] if entry else None,
                })

            try:
                io = rates.sample(self.disks)
            except Exception:
                io = {}
            for disk in self.disks:
                rate = io.get(disk["index"]) or {"read": None, "write": None}
                cap = None
                # Capacity moves slowly; re-read it every 5th tick.
                if tick % 5 == 1 or disk.get("_cap") is None:
                    cap = disk_capacity(disk["letters"])
                    disk["_cap"] = cap
                else:
                    cap = disk.get("_cap")
                sample["disks"].append({
                    "index": disk["index"], "model": disk["model"],
                    "letters": disk["letters"], "bus": disk.get("bus"),
                    "read": rate["read"], "write": rate["write"],
                    "used": cap[0] if cap else None,
                    "total": cap[1] if cap else None,
                })

            try:
                sample["power"] = self._power_block(sample, counters, bridge, nvml)
            except Exception as exc:
                # A bad config value must cost the power readout, not the app.
                sample["power"] = None
                sample["note"] = "power calculation failed: %r" % (exc,)

            if self.debug:
                self._dump(counters, gpu_data, sample)
            self.emit(sample)
            self._stop.wait(POLL_SECONDS)

        counters.close()

    def _power_block(self, sample, counters, bridge, nvml=None):
        cfg = self.power
        package_limit = cfg.get("cpu_package_watts", DEFAULT_CPU_PACKAGE_W)
        bridge_up = False
        try:
            bridge_up = bridge.poll()
        except Exception:
            bridge_up = False
        nvml_up = False
        if nvml is not None:
            try:
                nvml_up = nvml.poll(self.adapters)
            except Exception:
                nvml_up = False

        # Prefer a real CPU sensor from the bridge; fall back to the ACPI zone.
        if bridge_up:
            sample["cpu_temp"] = bridge.cpu_temp()
        if sample.get("cpu_temp") is None:
            sample["cpu_temp"] = counters.cpu_temp
            sample["cpu_temp_is_zone"] = counters.cpu_temp is not None

        cpu_watts = counters.cpu_watts          # RAPL, already a measurement
        if cpu_watts is None and bridge_up:
            cpu_watts = bridge.cpu_watts()
        measured = cpu_watts is not None
        if not measured:
            cpu_watts = estimate_cpu_watts(sample.get("cpu"), package_limit)

        gpus = []
        for gpu in sample["gpus"]:
            profile = (cfg.get("gpu_power") or {}).get(gpu["name"]) or {}
            tbp, idle = gpu_power_profile(gpu["name"], gpu.get("total_vram"))
            tbp = profile.get("tbp", tbp)
            idle = profile.get("idle", idle)
            watts, is_measured = None, False
            if nvml_up:                       # vendor library, no extra software
                watts = nvml.get(gpu["name"], "watts")
                is_measured = watts is not None
                temp = nvml.get(gpu["name"], "temp")
                if temp is not None and gpu.get("temp") is None:
                    gpu["temp"] = temp
            if bridge_up:
                if watts is None:
                    watts = bridge.gpu_watts(gpu["name"])
                    is_measured = watts is not None
                if gpu.get("temp") is None:
                    gpu["temp"] = bridge.gpu_temp(gpu["name"])
            if watts is None:
                watts = estimate_gpu_watts(gpu.get("util"), tbp, idle)
            gpus.append({"watts": watts, "measured": is_measured, "tbp": tbp})

        baseline, items = baseline_breakdown(cfg, len(self.disks))
        parts = [w for w in [cpu_watts] + [g["watts"] for g in gpus]
                 if w is not None]
        total = sum(parts) + baseline if parts else None
        return {"cpu_watts": cpu_watts, "cpu_measured": measured,
                "cpu_limit": package_limit, "gpus": gpus, "baseline": baseline,
                "baseline_items": items, "bridge": bridge_up,
                "psu": cfg.get("psu_watts", DEFAULT_PSU_W), "total": total,
                "nvml": nvml_up}

    def _dump(self, counters, gpu_data, sample):
        print("-" * 78)
        for a in self.adapters:
            print("  adapter %-28s vram=%.2f GiB keys=%s"
                  % (a["name"], a["total_vram"] / 2**30, sorted(a["keys"])))
        print("  counter luids: %s" % sorted(gpu_data))
        for label, items in (counters.raw_instances or {}).items():
            print("  [%s] %d instances" % (label, len(items)))
            for name, value in items[:8]:
                print("      %-62s %.2f" % (name, value))
        for d in sample["disks"]:
            print("  disk %d %-32s letters=%s read=%s write=%s used=%s total=%s"
                  % (d["index"], d["model"], d["letters"], d["read"], d["write"],
                     d["used"], d["total"]))
        power = sample.get("power") or {}
        print("  power: cpu=%s (measured=%s) gpus=%s total=%s psu=%s"
              % (power.get("cpu_watts"), power.get("cpu_measured"),
                 [g["watts"] for g in power.get("gpus", [])],
                 power.get("total"), power.get("psu")))
        if counters.last_error:
            print("  last error: %s" % counters.last_error)
        sys.stdout.flush()


DEMO_ADAPTERS = [
    {"name": "AMD Radeon RX 7800 XT", "vendor": "AMD",
     "total_vram": 16 * 2**30, "keys": {"demo0"}},
    {"name": "AMD Radeon RX 6650 XT", "vendor": "AMD",
     "total_vram": 8 * 2**30, "keys": {"demo1"}},
]
DEMO_DISKS = [
    {"index": 0, "model": "CT2000P3PSSD8", "bus": "NVMe", "letters": ["C:"],
     "size": 2000396321280},
    {"index": 1, "model": "Verbatim Portable USB 3.0", "bus": "USB",
     "letters": ["D:"], "size": 1000202273280},
]


class DemoSampler(Sampler):
    """Synthetic data so the UI can be exercised anywhere (no Windows needed)."""

    def run(self):
        t = 0.0
        cores = 12
        engines = ["3D", "3D", "Compute", "Copy", "VideoDecode"]
        while not self._stop.is_set():
            t += 0.35
            cpu = max(1.0, min(99.0, 42 + 34 * math.sin(t / 3) + random.uniform(-6, 6)))
            ram_total = 64 * 2**30
            ram_used = int(ram_total * (0.45 + 0.08 * math.sin(t / 5)))
            gpus = []
            for i, ad in enumerate(DEMO_ADAPTERS):
                util = max(0.0, min(100.0, 55 + 40 * math.sin(t / (2.2 + i))
                                    + random.uniform(-8, 8)))
                used = int(ad["total_vram"] * (0.55 + 0.30 * math.sin(t / 4 + i)))
                broken = (i == 1 and int(t) % 37 in (0, 1))
                gpus.append({
                    "name": ad["name"], "vendor": "AMD",
                    "total_vram": ad["total_vram"],
                    "util": None if broken else util,
                    "temp": None if broken else 48 + util * 0.32,
                    "mem_clock": None if broken else 2425000000,
                    "engine": None if broken else random.choice(engines),
                    "vram_used": None if broken else used,
                    "shared_used": None if broken else int(1.4 * 2**30),
                })
            disks = []
            for i, dk in enumerate(DEMO_DISKS):
                disks.append({
                    "index": dk["index"], "model": dk["model"],
                    "letters": dk["letters"], "bus": dk["bus"],
                    "read": max(0.0, (60 + 90 * math.sin(t / 2 + i)) * 2**20),
                    "write": max(0.0, (20 + 40 * math.sin(t / 3 + i)) * 2**20),
                    "used": int(dk["size"] * (0.62 - 0.2 * i)),
                    "total": dk["size"],
                })
            self.emit({
                "cpu": cpu,
                "cores": [max(0.0, min(100.0, cpu + random.uniform(-35, 35)))
                          for _ in range(cores)],
                "freq": 3800 + random.uniform(-500, 700),
                "counts": (cores * 2, cores),
                "ram": (ram_used, ram_total, 100.0 * ram_used / ram_total),
                "swap": (2 * 2**30, 8 * 2**30, 25.0),
                "uptime": 3600 * 27 + t,
                "cpu_temp": 42 + cpu * 0.35,
                "gpus": gpus, "disks": disks, "note": None,
                "power": {
                    "cpu_watts": 22 + 0.62 * cpu, "cpu_measured": True,
                    "cpu_limit": 88,
                    "gpus": [{"watts": estimate_gpu_watts(g["util"], tbp, idle),
                              "measured": False, "tbp": tbp}
                             for g, (tbp, idle) in zip(gpus, ((263, 16),
                                                              (176, 11)))],
                    "baseline": 61,
                    "baseline_items": {"board": 35, "RAM": 6, "fans": 8,
                                       "drives": 8, "USB": 10},
                    "bridge": False, "psu": 750, "total": None,
                },
            })
            self._stop.wait(0.35)


# ==========================================================================
# Theme + drawing helpers
# ==========================================================================

class T:
    BG = "#0c0e13"
    CARD = "#161923"
    EDGE = "#242938"
    FG = "#e9ecf4"
    DIM = "#aab4c8"
    MUTED = "#8892a8"
    TRACK = "#222738"
    GRID = "#1c2130"
    CPU = "#4da3ff"
    RAM = "#35d0a5"
    SSD = "#4dd6ff"
    PWR = "#ffd166"
    GPU = ["#ff8a4c", "#b07dff", "#ffd166", "#5ad1ff"]
    HOT = "#ffb454"
    WARN = "#ff6b6b"


SCALE = 1.0          # effective scale: DPI scale x how big the window is
BASE_SCALE = 1.0     # from screen DPI alone
COMPACT = False      # set when the window is small enough to drop detail

# Natural size of the layout at scale 1.0, used to derive the fit factor.
DESIGN_W = 1020
MIN_FIT, MAX_FIT = 0.60, 2.60


def S(v):
    return int(round(v * SCALE))


def hexrgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def blend(c1, c2, t):
    a, b = hexrgb(c1), hexrgb(c2)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t))
                                   for i in range(3))


def level_colour(pct, base):
    """Amber past 90%, red past 97% - so a merely-full GPU isn't alarming."""
    if pct is None:
        return base
    if pct >= 97:
        return T.WARN
    if pct >= 90:
        return T.HOT
    return base


def round_rect(cv, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, splinesteps=18, **kw)


FONTS = {}


def pick_family(root, candidates):
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


def init_fonts(root):
    FONTS["ui"] = pick_family(root, ["Segoe UI", "DejaVu Sans", "Helvetica"])
    FONTS["ui_bold"] = pick_family(root, ["Segoe UI Semibold", "Segoe UI",
                                          "DejaVu Sans", "Helvetica"])
    FONTS["mono"] = pick_family(root, ["Consolas", "Cascadia Mono",
                                       "DejaVu Sans Mono", "Courier"])


def F(kind, px, weight="normal"):
    return (FONTS.get(kind, "Helvetica"), -max(8, S(px)), weight)


def fmt_gib(n):
    if n is None:
        return "N/A"
    if n >= 2**30:
        return "%.1f GiB" % (n / 2**30)
    return "%.0f MiB" % (n / 2**20)


def fmt_size(n):
    if n is None:
        return "N/A"
    if n >= 2**40:
        return "%.2f TiB" % (n / 2**40)
    if n >= 2**30:
        return "%.0f GiB" % (n / 2**30)
    return "%.0f MiB" % (n / 2**20)


def fmt_rate(bps):
    if bps is None:
        return "N/A"
    if bps >= 2**20:
        return "%.1f MB/s" % (bps / 2**20)
    if bps >= 2**10:
        return "%.0f KB/s" % (bps / 2**10)
    return "%.0f B/s" % bps


def fmt_watts(w):
    return "N/A" if w is None else "%.0f W" % w


def fmt_uptime(seconds):
    if not seconds:
        return "--"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return ("%dd %dh %dm" % (d, h, m)) if d else ("%dh %dm" % (h, m))


# ==========================================================================
# Reusable canvas widgets
# ==========================================================================

class Ring:
    """Circular gauge with a big value in the middle."""

    def __init__(self, cv, cx, cy, r, width, colour, caption, value_px=20,
                 caption_px=8):
        self.cv, self.colour = cv, colour
        box = (cx - r, cy - r, cx + r, cy + r)
        cv.create_oval(*box, outline=blend(T.CARD, colour, 0.10), width=width,
                       fill="")
        self.arc = cv.create_arc(*box, start=90, extent=-0.1, style="arc",
                                 outline=colour, width=width)
        self.value = cv.create_text(cx, cy - S(4), text="--", fill=T.FG,
                                    font=F("mono", value_px, "bold"))
        self.sub = cv.create_text(cx, cy + S(value_px * 0.62), text=caption,
                                  fill=T.MUTED, font=F("ui", caption_px))

    def set(self, pct, text=None, sub=None):
        if pct is None:
            self.cv.itemconfig(self.arc, extent=-0.1)
            self.cv.itemconfig(self.value, text=text or "N/A", fill=T.MUTED)
        else:
            pct = max(0.0, min(100.0, pct))
            self.cv.itemconfig(self.arc, extent=-max(0.1, 359.9 * pct / 100.0),
                               outline=level_colour(pct, self.colour))
            self.cv.itemconfig(self.value, text=text or "%.0f%%" % pct, fill=T.FG)
        if sub is not None:
            self.cv.itemconfig(self.sub, text=sub)


class Spark:
    """Filled history plot fed from an external series (survives rebuilds)."""

    def __init__(self, cv, x0, y0, x1, y1, colour, title, autoscale=False,
                 fmt=None, floor=1.0, label_px=9):
        self.cv, self.colour = cv, colour
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.autoscale, self.fmt, self.floor = autoscale, fmt, floor

        if title:
            cv.create_text(x0, y0 - S(11), text=title, anchor="w", fill=T.MUTED,
                           font=F("ui", label_px))
        self.peak = cv.create_text(x1, y0 - S(11), text="", anchor="e",
                                   fill=T.MUTED, font=F("ui", label_px))
        round_rect(cv, x0, y0, x1, y1, S(6), fill=T.GRID, outline="")
        for frac in (0.25, 0.5, 0.75):
            y = y0 + (y1 - y0) * frac
            cv.create_line(x0 + S(4), y, x1 - S(4), y,
                           fill=blend(T.GRID, "#ffffff", 0.06))
        self.area = cv.create_polygon(x0, y1, x1, y1,
                                      fill=blend(T.GRID, colour, 0.22), outline="")
        self.line = cv.create_line(x0, y1, x1, y1, fill=colour,
                                   width=max(1, S(1.6)), joinstyle="round")
        self._dot_r = S(3)
        self.dot = cv.create_oval(0, 0, 0, 0, fill=colour, outline=T.CARD,
                                  width=max(1, S(1)))

    def draw(self, series):
        data = list(series)
        if not data:
            return
        n = len(data)
        w, h = self.x1 - self.x0, self.y1 - self.y0
        step = w / float(max(1, n - 1))
        vals = [v for v in data if v is not None]
        top = 100.0
        if self.autoscale:
            top = max(self.floor, max(vals) * 1.15 if vals else self.floor)

        pts = []
        last = 0.0
        for i, v in enumerate(data):
            if v is None:
                v = last
            else:
                last = v
            v = max(0.0, min(top, v))
            pts.append(self.x0 + i * step)
            pts.append(self.y1 - (h - S(3)) * (v / top))
        self.cv.coords(self.line, *pts)
        self.cv.coords(self.area, *(pts + [self.x1, self.y1, self.x0, self.y1]))
        r = self._dot_r
        cx, cy = pts[-2], pts[-1]
        self.cv.coords(self.dot, cx - r, cy - r, cx + r, cy + r)
        if vals:
            peak = max(vals)
            text = self.fmt(peak) if self.fmt else "%.0f%%" % peak
            self.cv.itemconfig(self.peak, text="peak " + text)
        else:
            self.cv.itemconfig(self.peak, text="")


class CoreBars:
    """One slim bar per logical CPU."""

    def __init__(self, cv, x0, y0, x1, y1, colour, title):
        self.cv, self.colour = cv, colour
        self.box = (x0, y0, x1, y1)
        cv.create_text(x0, y0 - S(11), text=title, anchor="w", fill=T.MUTED,
                       font=F("ui", 9))
        self.bars = []

    def _build(self, n):
        for item in self.bars:
            self.cv.delete(item[0])
            self.cv.delete(item[1])
        self.bars = []
        x0, y0, x1, y1 = self.box
        gap = max(1, S(2))
        bw = max(S(2), (x1 - x0 - gap * (n - 1)) / float(n))
        for i in range(n):
            bx = x0 + i * (bw + gap)
            track = round_rect(self.cv, bx, y0, bx + bw, y1, min(S(2), bw / 2),
                               fill=T.TRACK, outline="")
            fill = self.cv.create_rectangle(bx, y1, bx + bw, y1,
                                            fill=self.colour, outline="")
            self.bars.append((track, fill, bx, bw))

    def set(self, values):
        if not values:
            return
        if len(values) != len(self.bars):
            self._build(len(values))
        x0, y0, x1, y1 = self.box
        h = y1 - y0
        for (track, fill, bx, bw), v in zip(self.bars, values):
            v = max(0.0, min(100.0, v or 0.0))
            self.cv.coords(fill, bx, y1 - h * v / 100.0, bx + bw, y1)
            self.cv.itemconfig(fill, fill=level_colour(v, self.colour))


class LevelBar:
    """Horizontal rounded bar."""

    def __init__(self, cv, x0, y0, x1, y1, colour):
        self.cv, self.colour = cv, colour
        self.box = (x0, y0, x1, y1)
        round_rect(cv, x0, y0, x1, y1, (y1 - y0) / 2.0, fill=T.TRACK, outline="")
        self.fill = round_rect(cv, x0, y0, x0 + 1, y1, (y1 - y0) / 2.0,
                               fill=colour, outline="")

    def set(self, pct):
        x0, y0, x1, y1 = self.box
        if pct is None:
            self.cv.itemconfig(self.fill, state="hidden")
            return
        self.cv.itemconfig(self.fill, state="normal",
                           fill=level_colour(pct, self.colour))
        pct = max(0.0, min(100.0, pct))
        r = (y1 - y0) / 2.0
        w = max((x1 - x0) * pct / 100.0, r * 2 + 1)
        pts = [x0 + r, y0, x0 + w - r, y0, x0 + w, y0, x0 + w, y0 + r,
               x0 + w, y1 - r, x0 + w, y1, x0 + w - r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        self.cv.coords(self.fill, *pts)


class StackedBar:
    """Segmented meter: each contributor gets its own rounded block."""

    def __init__(self, cv, x0, y0, x1, y1, colours):
        self.cv = cv
        self.box = (x0, y0, x1, y1)
        self.colours = colours
        round_rect(cv, x0, y0, x1, y1, (y1 - y0) / 2.0, fill=T.TRACK, outline="")
        self.ticks = []
        for frac in (0.25, 0.5, 0.75):
            x = x0 + (x1 - x0) * frac
            self.ticks.append(cv.create_line(x, y0 + S(2), x, y1 - S(2),
                                             fill=blend(T.TRACK, "#ffffff", 0.16)))
        self.segments = [round_rect(cv, x0, y0, x0 + 1, y1, 0, fill=c,
                                    outline="", state="hidden")
                         for c in colours]

    def set(self, values, capacity):
        x0, y0, x1, y1 = self.box
        width = x1 - x0
        radius = (y1 - y0) / 2.0
        gap = S(2)
        cursor = x0
        for item, value in zip(self.segments, values):
            if not value or not capacity:
                self.cv.itemconfig(item, state="hidden")
                continue
            w = max(radius * 2 + 1, width * min(1.0, value / capacity))
            end = min(x1, cursor + w)
            if end - cursor < radius * 2 + 1:
                self.cv.itemconfig(item, state="hidden")
                continue
            r = min(radius, (end - cursor) / 2.0)
            pts = [cursor + r, y0, end - r, y0, end, y0, end, y0 + r,
                   end, y1 - r, end, y1, end - r, y1, cursor + r, y1,
                   cursor, y1, cursor, y1 - r, cursor, y0 + r, cursor, y0]
            self.cv.coords(item, *pts)
            self.cv.itemconfig(item, state="normal")
            cursor = end + gap
            if cursor >= x1:
                cursor = x1


# ==========================================================================
# Icons
# ==========================================================================

class IconStore:
    """Picks the embedded PNG closest to the current scale, so the icons stay
    crisp as the window grows instead of being stretched."""

    def __init__(self, root):
        wanted = max(12, int(round(28 * SCALE)))
        self.images = {}
        for name, sizes in ICONS.items():
            available = sorted(sizes)
            best = min(available, key=lambda s_: abs(s_ - wanted))
            self.size = best
            try:
                # Tk's -data option wants the base64 text itself, not bytes
                self.images[name] = tk.PhotoImage(master=root,
                                                  data=sizes[best], format="png")
            except Exception:
                self.images[name] = None

    def get(self, name):
        return self.images.get(name)


# ==========================================================================
# Cards
# ==========================================================================

HEADER_H = 58


class Card(tk.Canvas):
    def __init__(self, master, w, h):
        super().__init__(master, width=w, height=h, bg=T.BG,
                         highlightthickness=0, bd=0)
        self.w, self.h = w, h
        round_rect(self, S(1), S(1), w - S(1), h - S(1), S(14),
                   fill=T.CARD, outline=T.EDGE)

    def header(self, icon, title, subtitle, accent, title_px=14):
        pad = S(16)
        if icon:
            self.create_image(pad, pad, image=icon, anchor="nw")
        tx = pad + S(38)
        title_px = title_px if not COMPACT else title_px - 1
        self.create_text(tx, pad + S(1), text=title, anchor="nw", fill=T.FG,
                         font=F("ui_bold", title_px, "bold"))
        self._sub = self.create_text(tx, pad + S(20), text=subtitle, anchor="nw",
                                     fill=T.MUTED, font=F("ui", 9))
        self.create_line(pad, S(HEADER_H), self.w - pad, S(HEADER_H), fill=T.EDGE)
        round_rect(self, S(1), S(20), S(5), S(50), S(2), fill=accent, outline="")

    def set_subtitle(self, text):
        self.itemconfig(self._sub, text=text)


class CpuCard(Card):
    def __init__(self, master, w, h, icons):
        super().__init__(master, w, h)
        self.header(icons.get("cpu"), "CPU", "detecting...", T.CPU)

        top, bottom = S(HEADER_H) + S(20), h - S(30)
        span = bottom - top
        left = S(150)
        ring_r = max(S(30), min(S(46), span * 0.30))
        ring_cy = top + span * 0.32
        self.ring = Ring(self, left / 2 + S(4), ring_cy, ring_r,
                         max(S(7), int(ring_r * 0.22)), T.CPU, "LOAD")
        self.power = self.create_text(left / 2 + S(4), bottom - S(28),
                                      text="-- W", fill=T.FG,
                                      font=F("mono", 15, "bold"))
        self.power_sub = self.create_text(left / 2 + S(4), bottom - S(10),
                                          text="package", fill=T.MUTED,
                                          font=F("ui", 8))

        if COMPACT:
            self.spark = Spark(self, left, top, w - S(18), bottom,
                               T.CPU, "Utilisation · last 60 s")
            self.cores = None
        else:
            self.spark = Spark(self, left, top, w - S(18), top + span * 0.55,
                               T.CPU, "Utilisation · last 60 s")
            self.cores = CoreBars(self, left, top + span * 0.72, w - S(18),
                                  bottom, T.CPU, "Per-core load")
        self.foot = self.create_text(S(20), h - S(15), anchor="w", text="",
                                     fill=T.DIM, font=F("ui", 9))

    def update_card(self, s, series):
        cpu = s.get("cpu")
        self.ring.set(cpu)
        self.spark.draw(series)
        if self.cores is not None:
            self.cores.set(s.get("cores"))
        logical, physical = s.get("counts") or (None, None)
        bits = []
        if physical:
            bits.append("%d cores" % physical)
        if logical:
            bits.append("%d threads" % logical)
        self.set_subtitle(" / ".join(bits) if bits else "processor")

        power = s.get("power") or {}
        watts = power.get("cpu_watts")
        temp = s.get("cpu_temp")
        text = fmt_watts(watts)
        if temp:
            text += "   %.0f°C" % temp
        self.itemconfig(self.power, text=text,
                        fill=T.FG if watts is not None else T.MUTED)
        label = "package · measured" if power.get("cpu_measured") \
            else "package · estimated"
        if temp and s.get("cpu_temp_is_zone"):
            label += " · ACPI zone"
        self.itemconfig(self.power_sub, text=label)
        freq = s.get("freq")
        self.itemconfig(self.foot, text="%s     up %s" % (
            ("%.2f GHz" % (freq / 1000.0)) if freq else "-- GHz",
            fmt_uptime(s.get("uptime"))))


class RamCard(Card):
    def __init__(self, master, w, h, icons):
        super().__init__(master, w, h)
        self.header(icons.get("ram"), "Memory", "system RAM", T.RAM)

        top, bottom = S(HEADER_H) + S(20), h - S(30)
        span = bottom - top
        left = S(150)
        ring_r = max(S(30), min(S(46), span * 0.30))
        self.ring = Ring(self, left / 2 + S(4), top + span * 0.42, ring_r,
                         max(S(7), int(ring_r * 0.22)), T.RAM, "USED")
        self.spark = Spark(self, left, top, w - S(18), top + span * 0.55,
                           T.RAM, "Usage · last 60 s")
        self.big = self.create_text(left, top + span * 0.66, anchor="nw",
                                    text="N/A", fill=T.FG,
                                    font=F("mono", 15, "bold"))
        self.small = self.create_text(left, top + span * 0.85, anchor="nw",
                                      text="", fill=T.MUTED, font=F("ui", 9))
        self.foot = self.create_text(S(20), h - S(15), anchor="w", text="",
                                     fill=T.DIM, font=F("ui", 9))

    def update_card(self, s, series):
        ram = s.get("ram")
        self.spark.draw(series)
        if ram is None:
            self.ring.set(None)
            self.itemconfig(self.big, text="N/A", fill=T.MUTED)
            self.itemconfig(self.small, text="")
        else:
            used, total, pct = ram
            self.ring.set(pct)
            self.itemconfig(self.big,
                            text="%s / %s" % (fmt_gib(used), fmt_gib(total)),
                            fill=T.FG)
            self.itemconfig(self.small, text="%s free" % fmt_gib(total - used))
        swap = s.get("swap")
        self.itemconfig(self.foot, text=(
            "swap %s / %s" % (fmt_gib(swap[0]), fmt_gib(swap[1]))
            if swap and swap[1] else "swap --"))


class GpuCard(Card):
    def __init__(self, master, w, h, icons, accent, icon_name, adapter):
        super().__init__(master, w, h)
        total = adapter.get("total_vram") or 0
        tbp, _idle = gpu_power_profile(adapter["name"])
        self.header(icons.get(icon_name), adapter["name"],
                    "%s · %s VRAM · %d W board"
                    % (adapter.get("vendor", "GPU"),
                       fmt_gib(total) if total else "size unknown", tbp),
                    accent, title_px=13)
        self.total = total

        top, bottom = S(HEADER_H) + S(22), h - S(18)
        span = bottom - top
        left = S(158)
        ring_r = max(S(32), min(S(48), span * 0.30))
        cx = left / 2 + S(4)
        self.ring = Ring(self, cx, top + span * 0.30, ring_r,
                         max(S(8), int(ring_r * 0.23)), accent, "UTIL")
        self.power = self.create_text(cx, top + span * 0.66, text="-- W",
                                      fill=T.FG, font=F("mono", 15, "bold"))
        self.power_sub = self.create_text(cx, top + span * 0.82, text="estimated",
                                          fill=T.MUTED, font=F("ui", 8))

        self.status_dot = self.create_oval(w - S(78), S(23), w - S(70), S(31),
                                           fill=accent, outline="")
        self.status_text = self.create_text(w - S(62), S(19), anchor="nw",
                                            text="live", fill=T.DIM,
                                            font=F("ui", 9))

        rx0, rx1 = left, w - S(18)
        self.spark = Spark(self, rx0, top, rx1, top + span * 0.48, accent,
                           "Utilisation · last 60 s")
        self.create_text(rx0, top + span * 0.60, text="VRAM", anchor="w",
                         fill=T.MUTED, font=F("ui", 9))
        self.vram_pct = self.create_text(rx1, top + span * 0.60, text="",
                                         anchor="e", fill=T.MUTED, font=F("ui", 9))
        self.bar = LevelBar(self, rx0, top + span * 0.66, rx1,
                            top + span * 0.66 + S(16), accent)
        self.vram_text = self.create_text(rx0, top + span * 0.80, anchor="nw",
                                          text="N/A", fill=T.FG,
                                          font=F("mono", 13, "bold"))
        self.detail = self.create_text(rx1, bottom - S(6), anchor="e",
                                       text="", fill=T.MUTED, font=F("ui", 9))

    def update_card(self, g, power, series):
        util = g.get("util")
        self.ring.set(util)
        self.spark.draw(series)
        engine = ("engine %s" % g["engine"]) if g.get("engine") else "engine N/A"
        shared = g.get("shared_used")
        self.itemconfig(self.detail, text="%s  ·  shared %s"
                        % (engine, fmt_gib(shared)))

        watts = (power or {}).get("watts")
        temp = g.get("temp")
        text = fmt_watts(watts)
        if temp:
            text += "   %.0f°C" % temp
        self.itemconfig(self.power, text=text,
                        fill=T.FG if watts is not None else T.MUTED)
        self.itemconfig(self.power_sub,
                        text="measured" if (power or {}).get("measured")
                        else "estimated")

        used = g.get("vram_used")
        total = g.get("total_vram") or self.total
        if used is None:
            self.bar.set(None)
            self.itemconfig(self.vram_text, text="N/A", fill=T.MUTED)
            self.itemconfig(self.vram_pct, text="")
        elif total:
            pct = 100.0 * used / total
            self.bar.set(pct)
            self.itemconfig(self.vram_text,
                            text="%s / %s" % (fmt_gib(used), fmt_gib(total)),
                            fill=T.FG)
            self.itemconfig(self.vram_pct, text="%.0f%%" % pct)
        else:
            self.bar.set(None)
            self.itemconfig(self.vram_text, text="%s used" % fmt_gib(used),
                            fill=T.FG)
            self.itemconfig(self.vram_pct, text="")

        live = util is not None or used is not None
        self.itemconfig(self.status_dot, fill=T.RAM if live else T.WARN)
        self.itemconfig(self.status_text, text="live" if live else "no data",
                        fill=T.DIM if live else T.WARN)


class StorageCard(Card):
    ROW_H = 44

    @staticmethod
    def height_for(disks):
        return S(HEADER_H + 14) + S(StorageCard.ROW_H) * max(1, len(disks)) + S(12)

    def __init__(self, master, w, disks, icons):
        super().__init__(master, w, StorageCard.height_for(disks))
        self.header(icons.get("ssd"), "Storage",
                    "%d physical %s · live read / write"
                    % (len(disks), "disk" if len(disks) == 1 else "disks"),
                    T.SSD)
        self.rows = {}
        for i, disk in enumerate(disks):
            y = S(HEADER_H + 14) + i * S(self.ROW_H)
            self.rows[disk["index"]] = self._row(w, y, disk)
        if not disks:
            self.create_text(S(20), S(HEADER_H + 28), anchor="w", fill=T.DIM,
                             font=F("ui", 10),
                             text="No physical disks reported.")

    def _row(self, w, y, disk):
        self.create_text(S(20), y + S(6), anchor="nw",
                         text="Disk %d · %s" % (disk["index"], disk["model"]),
                         fill=T.FG, font=F("ui", 10))
        detail = "  ".join(disk["letters"]) or "no mounted volume"
        if disk.get("bus"):
            detail = "%s  ·  %s" % (disk["bus"], detail)
        self.create_text(S(20), y + S(24), anchor="nw", text=detail,
                         fill=T.MUTED, font=F("ui", 9))

        wide = w >= S(720)
        name_col = S(288) if wide else S(210)
        unit = (w - name_col - S(20)) / (3.0 if wide else 2.0)
        bar_x0 = name_col
        bar_x1 = bar_x0 + unit * (0.72 if wide else 0.95)
        bar = LevelBar(self, bar_x0, y + S(8), bar_x1, y + S(22), T.SSD)
        cap = self.create_text(bar_x0, y + S(26), anchor="nw", text="",
                               fill=T.MUTED, font=F("ui", 9))

        spark = None
        if wide:
            sp_x0 = bar_x1 + S(24)
            sp_x1 = sp_x0 + unit * 0.78
            spark = Spark(self, sp_x0, y + S(14), sp_x1, y + S(40), T.SSD, "I/O",
                          autoscale=True, fmt=fmt_rate, floor=2 * 2**20)
            tx = sp_x1 + S(24)
        else:
            tx = bar_x1 + S(20)
        read = self.create_text(tx, y + S(6), anchor="nw", text="R  --",
                                fill=T.DIM, font=F("mono", 10))
        write = self.create_text(tx, y + S(24), anchor="nw", text="W  --",
                                 fill=T.DIM, font=F("mono", 10))
        return {"bar": bar, "cap": cap, "spark": spark,
                "read": read, "write": write}

    def update_card(self, disks, series):
        for d in disks:
            row = self.rows.get(d["index"])
            if not row:
                continue
            used, total = d.get("used"), d.get("total")
            if used is not None and total:
                pct = 100.0 * used / total
                row["bar"].set(pct)
                self.itemconfig(row["cap"], text="%s / %s  (%.0f%%)"
                                % (fmt_size(used), fmt_size(total), pct))
            else:
                row["bar"].set(None)
                self.itemconfig(row["cap"], text="capacity N/A")
            r, wr = d.get("read"), d.get("write")
            self.itemconfig(row["read"], text="R  %s" % fmt_rate(r),
                            fill=T.FG if r else T.DIM)
            self.itemconfig(row["write"], text="W  %s" % fmt_rate(wr),
                            fill=T.FG if wr else T.DIM)
            if row["spark"] is not None:
                row["spark"].draw(series.get(d["index"], []))


class PowerCard(Card):
    H = 142

    def __init__(self, master, w, adapters, icons):
        super().__init__(master, w, S(self.H))
        self.header(icons.get("bolt"), "System power", "waiting for readings",
                    T.PWR, title_px=13)

        colours = [T.CPU] + [T.GPU[i % len(T.GPU)] for i in range(len(adapters))]
        colours.append(blend(T.CARD, "#ffffff", 0.28))
        self.labels = ["CPU"] + [a["name"].split()[-2] + " " + a["name"].split()[-1]
                                 if len(a["name"].split()) >= 2 else a["name"]
                                 for a in adapters] + ["rest"]
        self.colours = colours

        y = S(HEADER_H + 18)
        self.total = self.create_text(S(20), y - S(4), anchor="nw", text="-- W",
                                      fill=T.FG, font=F("mono", 22, "bold"))
        self.of_psu = self.create_text(S(20), y + S(30), anchor="nw", text="",
                                       fill=T.MUTED, font=F("ui", 9))

        bx0, bx1 = S(240), w - S(20)
        self.bar = StackedBar(self, bx0, y, bx1, y + S(22), colours)
        self.scale_hi = self.create_text(bx1, y + S(30), anchor="ne", text="",
                                         fill=T.MUTED, font=F("ui", 8))
        self.breakdown = self.create_text(S(20), y + S(52), anchor="nw", text="",
                                          fill=T.MUTED, font=F("ui", 8))
        self.legend = []
        lx = bx0
        for label, colour in zip(self.labels, colours):
            dot = self.create_oval(lx, y + S(33), lx + S(7), y + S(40),
                                   fill=colour, outline="")
            text = self.create_text(lx + S(11), y + S(30), anchor="nw", text=label,
                                    fill=T.MUTED, font=F("ui", 8))
            self.legend.append((dot, text, colour))
            lx += S(11) + S(6) * len(label) + S(18)

    def update_card(self, power):
        if not power:
            return
        cpu = power.get("cpu_watts")
        gpus = [g.get("watts") for g in power.get("gpus", [])]
        baseline = power.get("baseline") or 0
        psu = power.get("psu") or 1
        parts = [cpu] + gpus
        total = power.get("total")
        if total is None:
            known = [p for p in parts if p is not None]
            total = (sum(known) + baseline) if known else None

        values = [(p or 0) for p in parts] + [baseline]
        self.bar.set(values, psu)
        if total is None:
            self.itemconfig(self.total, text="N/A", fill=T.MUTED)
            self.itemconfig(self.of_psu, text="")
            return
        pct = 100.0 * total / psu
        self.itemconfig(self.total, text="%.0f W" % total,
                        fill=level_colour(pct, T.FG))
        self.itemconfig(self.of_psu,
                        text="%.0f%% of %d W  ·  %.0f W free"
                        % (pct, psu, max(0.0, psu - total)))
        self.itemconfig(self.scale_hi, text="%d W" % psu)
        gpu_measured = all(g.get("measured") for g in power.get("gpus", [])) \
            and power.get("gpus")
        self.set_subtitle(
            "CPU %s · GPUs %s · board, RAM, fans and drives estimated"
            % ("measured" if power.get("cpu_measured") else "estimated",
               "measured" if gpu_measured else "estimated"))
        items = power.get("baseline_items") or {}
        if items:
            self.itemconfig(self.breakdown, text="rest %.0f W  =  %s   ·   %s"
                            % (baseline,
                               "  +  ".join("%s %.0f" % (k, v)
                                            for k, v in items.items()),
                               "click here or press S to change"))


class Header(tk.Canvas):
    H = 72

    def __init__(self, master, w, h, icons, subtitle):
        super().__init__(master, width=w, height=h, bg=T.BG,
                         highlightthickness=0, bd=0)
        round_rect(self, S(1), S(1), w - S(1), h - S(1), S(14),
                   fill=T.CARD, outline=T.EDGE)
        icon = icons.get("app")
        if icon:
            self.create_image(S(18), S(18), image=icon, anchor="nw")
        self.create_text(S(56), S(17), text="RIGWATCH", anchor="nw",
                         fill=T.FG, font=F("ui_bold", 15, "bold"))
        self.create_text(S(57), S(39), text=subtitle, anchor="nw", fill=T.MUTED,
                         font=F("ui", 9))
        self.clock = self.create_text(w - S(20), S(20), anchor="ne",
                                      text="--:--:--", fill=T.FG,
                                      font=F("mono", 17))
        self.dot = self.create_oval(w - S(112), S(45), w - S(104), S(53),
                                    fill=T.RAM, outline="")
        self.state = self.create_text(w - S(96), S(41), anchor="nw", text="live",
                                      fill=T.DIM, font=F("ui", 9))

    def tick(self, live):
        self.itemconfig(self.clock, text=time.strftime("%H:%M:%S"))
        self.itemconfig(self.dot, fill=T.RAM if live else T.WARN)
        self.itemconfig(self.state, text="live" if live else "stalled")



# ==========================================================================
# Minimised strip - a topmost sliver that parks on the taskbar
# ==========================================================================

def short_gpu_label(name, index):
    """'AMD Radeon RX 7800 XT' -> '7800'. Model numbers distinguish cards; the
    trailing XT/Ti does not."""
    tokens = [t for t in (name or "").split() if any(c.isdigit() for c in t)]
    if tokens:
        return tokens[-1][:5]
    return "GPU%d" % (index + 1)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", ctypes.c_void_p),
                ("uCallbackMessage", ctypes.c_uint32), ("uEdge", ctypes.c_uint32),
                ("rc", RECT), ("lParam", ctypes.c_ssize_t)]


SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
ABM_GETTASKBARPOS = 0x00000005
ABE_LEFT, ABE_TOP, ABE_RIGHT, ABE_BOTTOM = 0, 1, 2, 3


def virtual_screen():
    """Bounds of all monitors together - the primary screen is not the limit."""
    if sys.platform == "win32":
        try:
            user32 = ctypes.WinDLL("user32")
            left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
            if width and height:
                return (left, top, left + width, top + height)
        except Exception:
            pass
    return (0, 0, 1920, 1080)


def taskbar_rect():
    """(left, top, right, bottom, edge) of the taskbar, or None."""
    if sys.platform != "win32":
        return None
    try:
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.SHAppBarMessage.argtypes = [ctypes.c_uint32,
                                          ctypes.POINTER(APPBARDATA)]
        shell.SHAppBarMessage.restype = ctypes.c_ssize_t
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        if not shell.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
            return None
        r = data.rc
        return (r.left, r.top, r.right, r.bottom, data.uEdge)
    except Exception:
        return None


class MiniBar(tk.Toplevel):
    """
    Windows 11 dropped the deskband API, so nothing can genuinely live inside
    the taskbar any more. This is the next best thing: a borderless always-on-top
    strip sized to the taskbar and parked just right of the weather widget.
    Drag it anywhere; click it to bring the full window back.
    """

    H = 40

    def __init__(self, app, adapters, config, persist=True):
        super().__init__(app)
        # NB: not self.config - that name belongs to the Tk widget API.
        self.app, self.cfg, self.persist = app, config, persist
        self.withdraw()
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
            self.attributes("-alpha", 0.96)
        except Exception:
            pass

        bar = taskbar_rect()
        height = S(self.H)
        if bar:
            left, top, right, bottom, _edge = bar
            height = max(S(26), min(S(52), (bottom - top) - S(8)))
        self.height = height

        self.fields = [("CPU", T.CPU), ("RAM", T.RAM)]
        for i, adapter in enumerate(adapters):
            self.fields.append((short_gpu_label(adapter["name"], i),
                                T.GPU[i % len(T.GPU)]))
        self.fields.append(("WATT", T.PWR))

        col = S(64)
        width = col * len(self.fields) + S(18)
        self.width = width

        self.canvas = tk.Canvas(self, width=width, height=height, bg=T.BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        round_rect(self.canvas, 1, 1, width - 1, height - 1, S(9),
                   fill=T.CARD, outline=T.EDGE)

        self.values = []
        for i, (label, colour) in enumerate(self.fields):
            x = S(14) + i * col
            self.canvas.create_oval(x, height / 2 - S(9), x + S(6),
                                    height / 2 - S(3), fill=colour, outline="")
            self.canvas.create_text(x + S(10), height / 2 - S(11), anchor="nw",
                                    text=label, fill=T.MUTED, font=F("ui", 8))
            self.values.append(
                self.canvas.create_text(x, height / 2 + S(1), anchor="nw",
                                        text="--", fill=T.FG,
                                        font=F("mono", 12, "bold")))

        # Bound on the canvas only: the canvas fills the window, and binding
        # both it and the toplevel would run every handler twice.
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._menu)
        self._drag_from = None
        self._moved = False

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Restore window", command=self.app.restore)
        self.menu.add_command(label="Park on taskbar",
                              command=self.park_on_taskbar)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.app.on_close)

        self.place_window()

    # -- placement -------------------------------------------------------
    def park_on_taskbar(self):
        self.cfg["mini_position"] = None
        self.place_window()
        self._save()

    def _save(self):
        # --demo runs on a throwaway config; writing it would wipe the real one.
        if self.persist:
            save_config(self.cfg)

    def place_window(self):
        saved = self.cfg.get("mini_position")
        if saved:
            x, y = int(saved[0]), int(saved[1])
        else:
            bar = taskbar_rect()
            offset = int(self.cfg.get("mini_offset_x", 172))
            if bar:
                left, top, right, bottom, edge = bar
                if edge in (ABE_TOP, ABE_BOTTOM):
                    x = left + S(offset)
                    y = top + ((bottom - top) - self.height) // 2
                else:
                    x = left + S(6)
                    y = top + S(8)
            else:
                x = S(offset)
                y = self.winfo_screenheight() - self.height - S(6)
        left, top, right, bottom = virtual_screen()
        x = max(left, min(x, right - self.width))
        y = max(top, min(y, bottom - self.height))
        self.geometry("%dx%d+%d+%d" % (self.width, self.height, x, y))

    def _press(self, event):
        self._drag_from = (event.x_root, event.y_root,
                           self.winfo_x(), self.winfo_y())
        self._moved = False

    def _drag(self, event):
        if not self._drag_from:
            return
        sx, sy, wx, wy = self._drag_from
        dx, dy = event.x_root - sx, event.y_root - sy
        if abs(dx) > 2 or abs(dy) > 2:
            self._moved = True
        self.geometry("+%d+%d" % (wx + dx, wy + dy))

    def _release(self, _event):
        if self._moved:
            self.cfg["mini_position"] = [self.winfo_x(), self.winfo_y()]
            self._save()
        else:
            self.app.restore()
        self._drag_from = None

    def _menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # -- data ------------------------------------------------------------
    def update_values(self, s):
        power = s.get("power") or {}
        readings = [None if s.get("cpu") is None else "%.0f%%" % s["cpu"]]
        ram = s.get("ram")
        readings.append(None if ram is None else "%.0f%%" % ram[2])
        for gpu in s.get("gpus", []):
            util = gpu.get("util")
            readings.append(None if util is None else "%.0f%%" % util)
        total = power.get("total")
        if total is None:
            parts = [power.get("cpu_watts")] + [g.get("watts")
                                                for g in power.get("gpus", [])]
            known = [p for p in parts if p is not None]
            total = (sum(known) + (power.get("baseline") or 0)) if known else None
        readings.append(None if total is None else "%.0fW" % total)

        for item, text in zip(self.values, readings):
            self.canvas.itemconfig(item, text=text or "N/A",
                                   fill=T.FG if text else T.MUTED)



class SettingsDialog(tk.Toplevel):
    """
    The PSU rating and the unmeasurable part of the system draw have to come
    from the person running this - no interface reports them. This exists so
    that means typing a number, not editing JSON.
    """

    FIELDS = [
        ("psu_watts", "Power supply rating", "W", 50, 5000),
        (("baseline_items", "motherboard"), "Motherboard + chipset", "W", 0, 500),
        (("baseline_items", "ram_sticks"), "Memory sticks", "", 0, 32),
        (("baseline_items", "ram_per_stick"), "Watts per stick", "W", 0, 50),
        (("baseline_items", "fans"), "Case + CPU fans", "W", 0, 200),
        (("baseline_items", "per_drive"), "Watts per drive", "W", 0, 50),
        (("baseline_items", "usb_peripherals"), "USB peripherals", "W", 0, 300),
    ]

    def __init__(self, app, config, on_apply):
        super().__init__(app)
        self.cfg, self.on_apply = config, on_apply
        self.title("Settings")
        self.configure(bg=T.BG)
        self.resizable(False, False)
        self.transient(app)

        pad = S(16)
        tk.Label(self, text="System power", bg=T.BG, fg=T.FG,
                 font=F("ui_bold", 13, "bold")).grid(row=0, column=0, columnspan=3,
                                                     sticky="w", padx=pad,
                                                     pady=(pad, S(2)))
        tk.Label(self, text="Nothing reports these - they are your figures.",
                 bg=T.BG, fg=T.MUTED, font=F("ui", 9)).grid(
                     row=1, column=0, columnspan=3, sticky="w", padx=pad,
                     pady=(0, S(10)))

        self.entries = {}
        for row, (key, label, unit, low, high) in enumerate(self.FIELDS, start=2):
            tk.Label(self, text=label, bg=T.BG, fg=T.DIM,
                     font=F("ui", 10)).grid(row=row, column=0, sticky="w",
                                            padx=(pad, S(10)), pady=S(3))
            var = tk.StringVar(value="%g" % self._current(key))
            entry = tk.Entry(self, textvariable=var, width=8, justify="right",
                             bg=T.CARD, fg=T.FG, insertbackground=T.FG,
                             relief="flat", font=F("mono", 10))
            entry.grid(row=row, column=1, sticky="e", pady=S(3))
            tk.Label(self, text=unit, bg=T.BG, fg=T.MUTED,
                     font=F("ui", 9)).grid(row=row, column=2, sticky="w",
                                           padx=(S(6), pad))
            self.entries[key] = (var, low, high)

        row = len(self.FIELDS) + 2
        tk.Label(self, text="LibreHardwareMonitor endpoint", bg=T.BG, fg=T.DIM,
                 font=F("ui", 10)).grid(row=row, column=0, sticky="w",
                                        padx=(pad, S(10)), pady=(S(12), S(3)))
        self.url_var = tk.StringVar(
            value=self.cfg.get("librehardwaremonitor_url") or "")
        tk.Entry(self, textvariable=self.url_var, width=30, bg=T.CARD, fg=T.FG,
                 insertbackground=T.FG, relief="flat", font=F("mono", 9)).grid(
                     row=row, column=1, columnspan=2, sticky="we",
                     padx=(0, pad), pady=(S(12), S(3)))

        self.note = tk.Label(self, text="", bg=T.BG, fg=T.MUTED,
                             font=F("ui", 9))
        self.note.grid(row=row + 1, column=0, columnspan=3, sticky="w",
                       padx=pad, pady=(S(8), 0))

        buttons = tk.Frame(self, bg=T.BG)
        buttons.grid(row=row + 2, column=0, columnspan=3, sticky="e",
                     padx=pad, pady=pad)
        tk.Button(buttons, text="Close", command=self.destroy, relief="flat",
                  bg=T.CARD, fg=T.DIM, font=F("ui", 10),
                  activebackground=T.EDGE).pack(side="right", padx=(S(8), 0))
        tk.Button(buttons, text="Save", command=self.apply, relief="flat",
                  bg=T.PWR, fg="#1a1400", font=F("ui_bold", 10, "bold"),
                  activebackground=T.HOT).pack(side="right")

    def _current(self, key):
        # The config may have been hand-edited into nonsense; coerce here too
        # so opening this dialog can never be what fails.
        if isinstance(key, tuple):
            section, name = key
            fallback = DEFAULT_BASELINE_ITEMS.get(name, 0)
            raw = (self.cfg.get(section) or {}).get(name, fallback)
        else:
            fallback = {"psu_watts": DEFAULT_PSU_W}.get(key, 0)
            raw = self.cfg.get(key, fallback)
        return as_number(raw, fallback, 0, 100000)

    def apply(self):
        for key, (var, low, high) in self.entries.items():
            value = as_number(var.get().replace(",", "."),
                              self._current(key), low, high)
            if isinstance(key, tuple):
                self.cfg.setdefault(key[0], {})[key[1]] = value
            else:
                self.cfg[key] = value
            var.set("%g" % value)
        url = self.url_var.get().strip()
        self.cfg["librehardwaremonitor_url"] = url or None
        saved = save_config(self.cfg)
        self.note.config(text="Saved to %s" % CONFIG_NAME if saved
                         else "Could not write %s" % CONFIG_NAME,
                         fg=T.MUTED if saved else T.WARN)
        try:
            self.on_apply()
        except Exception:
            pass


# ==========================================================================
# Application
# ==========================================================================

class App(tk.Tk):
    PAD = 14
    TOP_MIN = 214
    GPU_MIN = 250
    STATUS_H = 26

    def __init__(self, adapters, disks, demo=False, debug=False, diag=None,
                 power=None):
        super().__init__()
        init_fonts(self)
        self.title("RigWatch")
        self.configure(bg=T.BG)
        self.diag = diag or []
        self.adapters, self.disks, self.demo = adapters, disks, demo

        # keep references: Tk discards PhotoImages that nothing holds. The
        # store is rebuilt on every resize, so the window icon keeps its own.
        self.icons = IconStore(self)
        try:
            self._app_icon = tk.PhotoImage(master=self, format="png",
                                           data=ICONS["app"][max(ICONS["app"])])
            self.iconphoto(True, self._app_icon)
        except Exception:
            self._app_icon = None

        # History lives here, not in the widgets, so a resize never loses it.
        self.series = {"cpu": deque([None] * HISTORY, maxlen=HISTORY),
                       "ram": deque([None] * HISTORY, maxlen=HISTORY),
                       "gpu": [deque([None] * HISTORY, maxlen=HISTORY)
                               for _ in adapters],
                       "disk": dict((d["index"],
                                     deque([None] * HISTORY, maxlen=HISTORY))
                                    for d in disks)}
        self.last_sample = None

        self.design_h = self.natural_height(len(adapters), len(disks))
        natural_w = int(DESIGN_W * BASE_SCALE)
        natural_h = int(self.design_h * BASE_SCALE)

        # A tall machine (three GPUs, several disks, 150% DPI) can want more
        # height than the display has. Clamp both the minimum and the opening
        # size to what actually fits, or the user can never see the bottom.
        avail_w = max(640, self.winfo_screenwidth() - S(40))
        avail_h = max(520, self.winfo_screenheight() - S(90))
        self.minsize(min(int(natural_w * MIN_FIT), avail_w),
                     min(int(natural_h * MIN_FIT), avail_h))
        self.geometry("%dx%d" % (min(natural_w, avail_w),
                                 min(natural_h, avail_h)))
        self.resizable(True, True)

        self.cards = []
        self._resize_job = None
        self._built = None
        self.pinned = False
        self.bind("<Escape>", lambda e: self.on_close())
        self.bind("<KeyPress-t>", self.toggle_pin)
        self.bind("<KeyPress-T>", self.toggle_pin)
        self.bind("<Configure>", self._on_configure)

        self.status = tk.Label(self, text="starting...", bg=T.BG, fg=T.MUTED,
                               anchor="w", font=F("ui", 9))

        self.update_idletasks()
        self.build()

        self.power_cfg = power or {}
        self.mini = MiniBar(self, adapters, self.power_cfg, persist=not demo)
        self.mini_active = False
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Map>", self._on_map)
        self.bind("<KeyPress-m>", lambda e: self.show_mini())
        self.bind("<KeyPress-M>", lambda e: self.show_mini())
        self.bind("<KeyPress-s>", lambda e: self.open_settings())
        self.bind("<KeyPress-S>", lambda e: self.open_settings())

        self.queue = queue.Queue(maxsize=4)
        cls = DemoSampler if demo else Sampler
        self.sampler = cls(adapters, disks, self.queue, debug=debug, power=power)
        self.sampler.start()
        self.last_sample_at = 0.0
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(UI_TICK_MS, self.drain)

    @staticmethod
    def natural_height(n_adapters, n_disks):
        """Height the layout wants at scale 1.0 - the basis for the fit factor."""
        pad = App.PAD
        gpu_rows = max(1, (n_adapters + 1) // 2)
        storage = HEADER_H + 14 + StorageCard.ROW_H * max(1, n_disks) + 12
        return (pad + Header.H + pad + App.TOP_MIN
                + gpu_rows * (pad + App.GPU_MIN) + pad + storage
                + pad + PowerCard.H + pad + App.STATUS_H)

    # -- layout ----------------------------------------------------------
    def _on_configure(self, event):
        if event.widget is not self:
            return
        size = (self.winfo_width(), self.winfo_height())
        if size == self._built:
            return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self.rebuild)

    def rebuild(self):
        self._resize_job = None
        for card in self.cards:
            try:
                card.destroy()
            except Exception:
                pass
        self.cards = []
        try:
            self.build()
        except Exception as exc:
            print("layout rebuild failed: %r" % (exc,))
            return
        if self.last_sample:
            try:
                self.render(self.last_sample)
            except Exception as exc:
                self.status.config(text="render error: %r" % (exc,))

    def build(self):
        global SCALE, COMPACT
        w = max(self.winfo_width(), self.minsize()[0])
        h = max(self.winfo_height(), self.minsize()[1])
        self._built = (self.winfo_width(), self.winfo_height())

        # Everything - fonts, padding, icons, gauges - is derived from this,
        # so the whole dashboard shrinks and grows with the window.
        fit = min(w / float(DESIGN_W * BASE_SCALE),
                  h / float(self.design_h * BASE_SCALE))
        fit = max(MIN_FIT, min(MAX_FIT, fit))
        SCALE = BASE_SCALE * fit
        COMPACT = fit < 0.85
        self.icons = IconStore(self)

        pad = S(self.PAD)
        inner = w - 2 * pad
        card_w = (inner - pad) // 2
        gpu_rows = max(1, (len(self.adapters) + 1) // 2)
        storage_h = StorageCard.height_for(self.disks)

        fixed = (pad + S(Header.H) + pad + pad + storage_h + pad
                 + S(PowerCard.H) + pad + S(self.STATUS_H)
                 + gpu_rows * pad)
        base_flex = S(self.TOP_MIN) + gpu_rows * S(self.GPU_MIN)
        share = max(0.70, min(1.25, (h - fixed) / float(base_flex)))
        top_h = int(S(self.TOP_MIN) * share)
        gpu_h = int(S(self.GPU_MIN) * share)

        y = pad
        self.head = Header(self, inner, S(Header.H), self.icons,
                           "live system telemetry · refresh %.1f s%s"
                           % (POLL_SECONDS,
                              "   ·   DEMO DATA" if self.demo else ""))
        self.head.place(x=pad, y=y)
        self.cards.append(self.head)
        y += S(Header.H) + pad

        self.cpu_card = CpuCard(self, card_w, top_h, self.icons)
        self.cpu_card.place(x=pad, y=y)
        self.ram_card = RamCard(self, inner - card_w - pad, top_h, self.icons)
        self.ram_card.place(x=pad * 2 + card_w, y=y)
        self.cards += [self.cpu_card, self.ram_card]
        y += top_h + pad

        self.gpu_cards = []
        for i, adapter in enumerate(self.adapters):
            accent = T.GPU[i % len(T.GPU)]
            icon_name = "gpu_a" if i % 2 == 0 else "gpu_b"
            cw = card_w if (i % 2 == 0 and i + 1 < len(self.adapters)) else (
                inner - card_w - pad if i % 2 else inner)
            card = GpuCard(self, cw, gpu_h, self.icons, accent, icon_name,
                           adapter)
            card.place(x=pad + (i % 2) * (card_w + pad),
                       y=y + (i // 2) * (gpu_h + pad))
            self.gpu_cards.append(card)
            self.cards.append(card)
        if not self.adapters:
            empty = Card(self, inner, gpu_h)
            empty.create_text(S(24), S(28), anchor="nw", fill=T.DIM,
                              font=F("ui", 11),
                              text="No display adapters reported.")
            empty.create_text(S(24), S(56), anchor="nw", fill=T.MUTED,
                              font=F("ui", 9), text="\n".join(self.diag[:6]))
            empty.place(x=pad, y=y)
            self.cards.append(empty)
        y += gpu_rows * (gpu_h + pad)

        self.storage_card = StorageCard(self, inner, self.disks, self.icons)
        self.storage_card.place(x=pad, y=y)
        self.cards.append(self.storage_card)
        y += storage_h + pad

        self.power_card = PowerCard(self, inner, self.adapters, self.icons)
        self.power_card.bind("<Button-1>", self.open_settings)
        self.power_card.place(x=pad, y=y)
        self.cards.append(self.power_card)
        y += S(PowerCard.H) + pad

        self.status.place(x=pad + S(4), y=y, width=inner)

    # -- lifecycle -------------------------------------------------------
    def open_settings(self, _event=None):
        if getattr(self, "_settings", None) and self._settings.winfo_exists():
            self._settings.lift()
            return
        self._settings = SettingsDialog(self, self.power_cfg, self.rebuild)

    # -- minimised strip -------------------------------------------------
    def _on_unmap(self, event):
        if event.widget is self and self.state() == "iconic":
            self.show_mini()

    def _on_map(self, event):
        if event.widget is self and self.mini_active:
            self.mini_active = False
            self.mini.withdraw()

    def show_mini(self):
        if self.mini_active:
            return
        self.mini_active = True
        self.mini.place_window()
        self.mini.deiconify()
        self.mini.lift()
        try:
            self.mini.attributes("-topmost", True)
        except Exception:
            pass
        if self.state() != "iconic":
            self.iconify()

    def restore(self):
        self.mini_active = False
        self.mini.withdraw()
        self.deiconify()
        self.lift()
        self.focus_force()

    def toggle_pin(self, _event=None):
        self.pinned = not self.pinned
        try:
            self.attributes("-topmost", self.pinned)
        except Exception:
            self.pinned = False
        self.title("RigWatch" + ("  (pinned)" if self.pinned else ""))

    def on_close(self):
        self.sampler.stop()
        try:
            self.mini.destroy()
        except Exception:
            pass
        self.destroy()

    def drain(self):
        # Whatever happens in here, the loop has to re-arm itself: if this
        # method ever returns without scheduling the next tick the window stays
        # open but stops updating, which looks like a freeze.
        try:
            latest = None
            try:
                while True:
                    latest = self.queue.get_nowait()
            except queue.Empty:
                pass
            if latest is not None:
                self.last_sample_at = time.time()
                self.last_sample = latest
                self.record(latest)
                if self.mini_active:
                    self.mini.update_values(latest)
                else:
                    self.render(latest)
            self.head.tick((time.time() - self.last_sample_at) < 5.0)
        except Exception as exc:
            try:
                self.status.config(text="update error: %r" % (exc,))
            except Exception:
                pass
        finally:
            try:
                self.after(UI_TICK_MS, self.drain)
            except Exception:
                pass

    def record(self, s):
        self.series["cpu"].append(s.get("cpu"))
        ram = s.get("ram")
        self.series["ram"].append(ram[2] if ram else None)
        for i, gpu in enumerate(s.get("gpus", [])):
            if i < len(self.series["gpu"]):
                self.series["gpu"][i].append(gpu.get("util"))
        for disk in s.get("disks", []):
            series = self.series["disk"].get(disk["index"])
            if series is None:
                continue
            r, w = disk.get("read"), disk.get("write")
            series.append(None if r is None or w is None else r + w)

    def render(self, s):
        power = s.get("power") or {}
        self.cpu_card.update_card(s, self.series["cpu"])
        self.ram_card.update_card(s, self.series["ram"])
        gpu_power = power.get("gpus", [])
        for i, (card, gpu) in enumerate(zip(self.gpu_cards, s.get("gpus", []))):
            card.update_card(gpu, gpu_power[i] if i < len(gpu_power) else None,
                             self.series["gpu"][i])
        self.storage_card.update_card(s.get("disks", []), self.series["disk"])
        self.power_card.update_card(power)
        if self.mini_active:
            self.mini.update_values(s)

        note = s.get("note")
        if note:
            self.status.config(text=note, fg=T.WARN)
        else:
            self.status.config(
                text="GPU via PDH GPU Engine / GPU Adapter Memory counters   "
                     "·   adapters from D3DKMT   ·   disks via storage "
                     "IOCTLs   ·   CPU watts from Energy Meter (RAPL)   "
                     "·   %s   ·   T = keep on top, Esc = quit"
                     % time.strftime("%H:%M:%S"), fg=T.MUTED)


def enable_dpi_awareness():
    """Ask Windows for crisp, unscaled pixels. Best effort, newest API first."""
    if sys.platform != "win32":
        return
    try:                                   # Windows 10 1703+
        fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_ssize_t]
        fn.restype = ctypes.c_int
        if fn(-4):                         # PER_MONITOR_AWARE_V2
            return
    except Exception:
        pass
    try:                                   # Windows 8.1+
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:                                   # Vista+
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def detect_scale(probe):
    try:
        return max(1.0, min(2.0, probe.winfo_fpixels("1i") / 96.0))
    except Exception:
        return 1.0


def main():
    global SCALE, BASE_SCALE
    parser = argparse.ArgumentParser(
        description="RigWatch - live CPU / RAM / GPU / disk / power monitor")
    parser.add_argument("--debug", action="store_true",
                        help="print adapters, disks and raw counter instances")
    parser.add_argument("--demo", action="store_true",
                        help="run with synthetic data (works off Windows)")
    parser.add_argument("--scale", type=float, default=None,
                        help="force UI scale factor (default: from screen DPI)")
    parser.add_argument("--psu", type=int, default=None,
                        help="power supply rating in watts (overrides config)")
    parser.add_argument("--mini", action="store_true",
                        help="start minimised to the taskbar strip")
    args = parser.parse_args()

    if not args.demo:
        if sys.platform != "win32":
            print("This monitor reads Windows performance counters and needs "
                  "Windows. Try --demo to preview the interface.")
            return 1
        try:
            import psutil  # noqa: F401
        except ImportError:
            print("psutil is required:  pip install psutil")
            return 1

    enable_dpi_awareness()

    diag = []
    if args.demo:
        adapters, disks = list(DEMO_ADAPTERS), [dict(d) for d in DEMO_DISKS]
        power_cfg = {"psu_watts": args.psu or 850, "system_baseline_watts": 60,
                     "cpu_package_watts": 88, "gpu_power": {}}
    else:
        adapters = enumerate_adapters(diag)
        disks = disk_inventory(diag)
        power_cfg = load_power_config(adapters, diag)
        if args.psu:
            power_cfg["psu_watts"] = args.psu

    probe = tk.Tk()
    probe.withdraw()
    BASE_SCALE = args.scale if args.scale else detect_scale(probe)
    SCALE = BASE_SCALE
    probe.destroy()

    if args.debug:
        print("UI base scale %.2f" % BASE_SCALE)
        for line in diag:
            print("  " + line)
        for a in adapters:
            print("  GPU  %-28s %6.2f GiB  %s"
                  % (a["name"], a["total_vram"] / 2**30, sorted(a["keys"])))
        for d in disks:
            print("  DISK %d %-34s %s" % (d["index"], d["model"], d["letters"]))
        print("  POWER config: %s" % power_cfg)

    app = App(adapters, disks, demo=args.demo, debug=args.debug, diag=diag,
              power=power_cfg)
    if args.mini:
        app.after(400, app.show_mini)
    app.mainloop()
    return 0


ICONS = {
    'app': {
        18: (
            'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADHElEQVR42p2UTWhcZRSGn/PdOzOJk59WW8FWJItE'
            'CgFxIQgudEakWkxsdTGIFjRSESJacKOicGdQitaNglimCdZGJ8EZ60IRkg5lpnbnohQXtWDBi0KzyDC0mTRz5+f7'
            'jotJmra79izO7rzwvg/nhY1RVeHuRjaXqCoiot/+eOYNEXnZWTvsnJPbxUVEjTEqIk2FXxdP1b8slTJWVUUCVZMT'
            'cTPzp39KJBJPtqJW3jlbwwB4GFXdFHLiBMATb1ssHn/NWntF1v99JgzDNgAzhcWp2YXy2mf54vCdWJqdL/9z/Iel'
            'TwF8AIXX1dovPngrc+3EiUpfGNLNZtP2pWONnT9PD65kKxWPKpACqjAygj81lY7U2Y9QssDHpheUJEFqqirnV3dr'
            'LpuyE3O6Vx4YOP/8Sd2TS6e7kHK5dLqby6W7yeRKB1QwXh2RGNBLAnBGxBcRHe2lqiJM99/Lbs/wIsBF8INATRDo'
            'xo2okZ6jLWs9Ogpw+PDDrYmT+pBAam2Zy6rsB46UctLePCoWi17vDr1FCEDMFh2EQ8D1bpM3/T4qEwXdIY7R+ABH'
            'WleZzmTkEoAxqs7dJGSwUc3tHNv7vV5M4O5LDHF/tMrnv01JdbKgTddmv/GZVHgCnz8OLOh6s8Pqf50L3zwoyxFs'
            'ehTj97uopspRP2GS7SZXTYtfUBWd41Ssn09chx3RNZ4FiCUZX+9wfdA024r4oD0hp+IPmvV6+VX5jq2fEUSUOS3G'
            'BzkY1fkreQ+/lzJigQrAKwtL+9rW+LeEbcXjqYr6I2Hoh+FIN5XF5UA9y7l2A4CFUkbsc19pYoy/+fORMdu5UpYN'
            'Rj38ItJWGDqblm6KkLfHSzo+XpIgqPiPhtlVjbpPD5jGsWJRvcfrVTs6dplqCiuQRHA3XBwvLL03O396+U7ffna+'
            'fGGmsPg1gARBYHbtmvT8ofrZWKxvWxQ1j4LWeng9BbBWPZEeaCcinjAcj8Xf6Vq3fa3Teuzdg/saoqoiIprP52Ox'
            '7aOBIC9Y202qc6Iqt9WIqohRY7w2cGZlrfHh+4cONG6um7sttRuF+D/2rHv3RGkXHQAAAABJRU5ErkJggg=='
        ),
        22: (
            'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAEQUlEQVR42qWVX2wUVRTGv3PvzOyf7pYWWiLFPy9G'
            'EzExxjcfAEMCRTCQGDYBAraALvLiCwZiTG4njTGhMfiAEQrSdNtivItRQ3UhQNqqj5YHI0Y0atAEVFjYtnR3pjNz'
            'jw/dXVfQSPAkN7mT3PndM+d88x2gIbTWEvcYt79r1TZKKZHJZKK+XK6pPdGx3BhaEswFkP9yVRQBtmNDCirOlea+'
            'yGQ6bzAzEREDAM1DWbgumaO50912IvaOFDLh+95dZWo7DowxHAXBvl1b1/TVWJbWWmYyFPXnCl2t7e3Hp6emjpa9'
            '2bdkzPot6Qd8owpoSjdzI3B2ZpriMZuCmUqrE7dfalnYduDI0Kfx7Dbq1VpLAoBDh3Qq2baoGEXR8ItbV++8lxr3'
            'jxRea2pqfsOvzDy4Y3PnrwIAYq0ty23bcUIR9Wqt5djYmKWUEkopAQCbNEtmJqWUYGZqXFprqbWWLfaCg3O+F4Qh'
            'NtabR2SWel6ZIzt+PZNZGzEzjY+vJNcl89wQn/Q8pHuAtS564Fab0xAGAAOo9OcKU0LS/QAgAIAEQiKiyE8IAOjp'
            'GZeuS2Zdjp+QDp6XMay+MIjH4ZJRigWYCcx02wUEQUSMsA5mM6+OpnTIANPVjpVUzWW7sACSAEusAYDRDkgQMYhY'
            'KRbzTKZq1mCaZ1l3toG4P4vghQGOlxx0BWVMMNAGxmYAfZNZCjrf4/ZrIUpuloJ5yd5RHojGBycxa2r7okRnYhEW'
            'MtBLjBEnjSc3DvKi9SPc5iTwzdI0vtxwnB+oZXp7WAAQRJadFF5p9PdV+zbkORtVTGjF0FEuYvpqGZ/fl8KMHQdm'
            'Z/A0RVjitGBxMIvFHMcv63N8xU7B9m/hTQejN31KWHWwJGMMyJLAzyQwRhARSRj28MFkloIVA/y1uI4SCHtAWOKV'
            'cAkhtog49kAgCUCAcRkgi9hwHSyEiSJyUie2bM8R5QcaP2mFYmuim7z1g/yhk8JOAAhuYe+pHXQBwK7GsxuGzxyV'
            'MNFfOhaC2TDe/nhPesWYvvXo96BLj4AXXwP/cbHaUolTIHQHsyiTjfeVYvHtMlg/nZvk1OaneOU4DOEM10zIqjqR'
            'NMxsBwgnnqFwvMGlwExwARFgIvIhmHH+1Ba6EtMs8xmaA0DoB08AODJ02hhANuiYi7FYnGzfS2utZT6fF3XRE0Ep'
            'JT7pppIJsMoReAXM9NjFeTUopahmA5Yl08wo1m1zZGS0NbCTRd/3D2S3rd1/LyZ0eLCwu7llwbszN0sPZ7ue/bHu'
            '/IeHCq+eLEzygD6/f2Dgo5a7Bfb15ZqOnTi3O//ZV3xkuHCwNk3+ZvTHTpx9PZFM9nqVsjGRucHE9M/yr/1vxCSo'
            'OZFMOX6lfPDyd6v2LluWp02ZTYYaR5PruubQ8OhDSSe5jgwvDcMQRHeYTa2nbFkWSIrrlblg9OWtq3+olpb/cyD+'
            'n2H6J1alBO7dzqKzAAAAAElFTkSuQmCC'
        ),
        28: (
            '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'
            'dOPxI0pcEwAAAABJRU5ErkJggg=='
        ),
        36: (
            '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'
        ),
        44: (
            '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'
        ),
        56: (
            'iVBORw0KGgoAAAANSUhEUgAAADgAAAA4CAYAAACohjseAAAMXUlEQVR42t1afYxc1XX/nXvfe/NmP22Mo+I2JSmp'
            '03iJlKYWOE1gxyXEsbENjbpT7DUxX/ZKlYqaBtRWVH3zmjZVVTdVm+ZjDaX4G96SEIJjm1iwY+o4sQsKTbtBDZCo'
            'dkAtJrB4Z+fjvXfvr3/MjLNrr+1d70dxrzRaaXf2nvO759xzfufcA/w/XzKd/yU5N0qKAMDsCwuCQAWDg04URXqu'
            'LREEVIODg04QUM24BYMgUF1dXZLP503zdyTV7t2HO9/G27MKrLOzE6VXs6W+vqXJaX1IhUIBYRjaaQOMokg3gW3d'
            'c3Cxq/UtxtrrYOxigJdbUGRann7uRRAKihQOK6VehqjvqiT9xh0bVrxwpm4XBbC5wVf++fH3+C2df2Zp12Wzrb5J'
            'EyRJDGPMnLinUhqu68JxXVQrZauUfjwpV/580+2rfnAhkHJutxx0wnB5+sDO/Z9yXH9rxvcXjJbetiRSx3E9z/Og'
            '1NxcRUuLJI6RJHECQrW2t+skrlXTNP3M3es/8dXzgZTzgevfvu8zre0dX4hrVaRJUvOzLRmIIK5VXrLEv4Lyight'
            '407OaJQjKUo144lcCWCp67pXa8dBebQUK6XdtvZ2GRk5tWVz74r7mjpfEGDzNPq3f+uW1vbOxyvlsiEt29o7nGql'
            '8l2l+bkW/sLT+fzV8VxH0fd+4JmPWeCPM76/sjxasgBNe8d899Twz+7ZfNuqL05kSTkzWhYKBT6wc/8vatf7dxHp'
            'MGlqfD/rxrXaluP/efiPmpEriiK9cOFCmQtwxSIw1joP7vn2va7j/XWSxFaJULSj4lr1mr7bVn4/iiI1FqQzdqOu'
            'ri4REbt154G/yGZb55VODdfaOjozo6WRLZt6V9wXBFRR1KXz+by9UPSajRVFkR4aWih3r1u+5YGd++OW1o6/Hy2N'
            'xC1+Vie16hcA5IaGejihBYMgUGEY2v6H913lZJwXLSme5+u4Vn1uU++Ka4PBQY1i0U4m98w2senvf87p61ua9O84'
            '8I3Wtraby6OjcSaT8apx+bq+3lWHx7rqaVaQy+UUAIirfifb0u7CwihRYq35UwDsOnmS7wBwAMD5839sSYpy1J/E'
            'tZoBqBzXo4LuBYChoZ9fndMAv/zlk2xk1+tNmkA7OlOpjL6C8olBkpLP5+1UAgJIASlTpVaTWfl83hQKkE3rPvFi'
            'kiZHfD/rJHFVSP4mAIRhzpwFcGAgb6Io0hBZnCQxvIwPETnS19eXFApFPXmySwlDsRAhRBiGYkHOeDDK5YoKgAhQ'
            'dBwXSZJQRK784raDC+p1QF2mauYcAHgjaekQYIExBkoUAPy4vtkULAfhLXv4gZv3sHjzIzy6ehc/BhHOtCWLzdMU'
            '/IQgSFgAnVmVXg4AhUJBxlnwHMl2CsmbEgLoieilMR5zsujWHq6BQdQTsS0sgMDMW/JCOs7YqfZEUAjFVqv4La8N'
            'S2qnkMQlJG4LrqjV0A0R9kRQcx2RZlwggbsariMiEKmHm1UA8PoQZK4BOjPDo6gG8mJ+ewevSAUr0yoEgCZAE0No'
            'saInojeQl7jupsJLyoLdjX0MkPda0UqDtBHhlIlB7eGqcoIPnXblS82ChwowAJUlNpoEoECEIAQQgXEycMwIVgI4'
            '9vrCuXVTNf3gQg0R3rQYS7WHX0+roBBaHIgoCAll6jR5NQAcysFcUgCbgUMZ3KkzgAhi7YPW4Flr8RPtQZkqqBQ+'
            'tGY73zdRThzLfHoi6ncOQFIOhZL2ROwE8Km0CoDQyoHA4n4hnnYyAICa0woHwI0ApThO7njmM5AXE5BqpnLmFABS'
            'xn+A7iI0SKlVsdptw0KbIFEenLiE1/ZulMMC/AubPVQDULAWEOYAOxHzWbuHR9fsYD4UsYCwDnQi2RQgN3NBJpc7'
            'yYlC+6EiLJYL7Q7eSQsIYJ0MwAQ7ASBVOMQSKiLwTQ0A8NHVu3h52CtvBAFVk/lUKnjMn48laQVQLXh07W52xyfx'
            'h6FIrXk+Z8ruykWcMQsWi2dX7j0RNUKxa3dwiVK4Pq2AEHhJGYYa2wFg3wb5LwDHtA+xBrHbgnYYdAOUo5fBHct8'
            'qsNI0hpMPArjZvF73rvw7E3beXUQUE3EY4eKk+smOOfpl0OBBtD4u1evP7pmF1thQDaK5EoVWL2dsMQ8Vd8ndXzo'
            'pIwjezfKUE9AbyCUGMQ+pdEtAisCiuBmQL7WfkWdQ45lPiAUARuPIHWyuMamOPb8VTgO1GU11CIJeeE1oH0bP+rK'
            'E1URdXEuKlLHQ+KDjg+XydltKmsBpiABioaI4KG6DzX+DuxLKvgrAJ6pQWhxw43b2TqQl9G1u7mIts582AhOjged'
            'lGGTClKlkFUu3j9xrxSwZThKzwDZFqJiYliTjPnEoIlBmnqd6LbATUo4ZTWeAChLhpAClKWv4Ic2xYvag7IJUsfH'
            'ooxgGUixBrd6rWi1BrGTgViDHySjeDjTDqU9OLRAU8442Q35yFy4RlWTjJ/S+G7zp4Kg/lsCULAkjhO481u98lYQ'
            'QMJQbHcAXU8BOKA9EIJEe6BY3NTgOrebxouDzgAK2LZ3o9wRl/H7AF6CgoGc9ho1TodJvhc458lxsFAaMGixybWV'
            'TMaKOUfr2APSN3Fib5+UQUo9zAPv6mqcsMWTJsZnQbg2hUBh2eod/LB28cG0BisKblxChRaPBQFV2Cv/+Bv97H93'
            'B95r04mBOBI7o773s7iW8R3ai7uDJAQgHt3ovzi5ooKqCQ4ABvL1fJfN4lilgte0h0VpDQTRBeBLjfMxbhZOUsZT'
            'ezfK8WxE3RNRD+QleR740QW9a+d+mXYejKL/8IaGutLzfScsgGPBNW9vQ9nKmh18WmewwZZgoNChNZbZGCCgSQiJ'
            'hwDK60OQQ6GkACUIzuOGXUNOOInu+qQALlx40oahnL+rFp6Pq1JIPEHiNgIiBEwKKwJoFyop48SIwUEAOBQ2ybgw'
            'DM8dRILBQTtjiX76pZQQGoeSMkaUhgZBARRYZz6isOvQHVLtDqBnuhie/eKzUT3s7ZU3AHxHZwDWuSgh0EkZBmmd'
            '2jU56qUFsN7iUwBFiG+KOs1GrOMDNsWRJ2+XoSCguuA1eKcCrFtGCOBgUkbCulw7lvkUZ0mXcZtWyo5wFrrQYSg2'
            'CKie/LS8TIMfZtqgHH8886nf1ekvrUTOAihSv9gffl/mFCDDSmlYWoiod9eriRlrKooi7klr+BGIE1C4o8l8IFML'
            'LrnTdaL6pQalUiBKqdTeanS2Oc6CQUC1fPnyVAQvu56LJI5B2o/UH0VzZiasCAi/ebs8+9O3cfV//xTvf3KDfB1s'
            'VPRTvdfFXMPteZ0xKVzXFYIn7l639vWxRlNnPGaAtEccx0OSxHEm4y+5YvGya+vJfmaGf4KA6vk+Sb73WakEpJqq'
            '5ep7BKpQALdFB39Za6e7UqkYL+NTiTomIgyCQeesO1gsFi0AKEcNVMqjFBFRSkNRhSLCt976FQVMv+VXt1a9wXQ2'
            '85nk6upyRIS1OA38bDYD0lhrJTX2kfqfT3IsVR5Dyeovo1t3Hniita19bWnkVNzW3uGVSyP3btrwyb8dHBx0irmc'
            'vWjFpm39QC1atEb39S1Ntu7Yn/dbWh+tVctJxs+61Url+6++tGIpMH4CSs4kywWADz5y8Ne06BdIq0jCcVxt0vQP'
            '7lp/4z80BSGXUyhisr2faSVR5ACMOditu7+9znPcB42xGVpjMr7vVWqVGzavX/HMmZMW5xwj+er2fZs7513WXxoZ'
            'TgDR2ZY2FceVr5HyubtuveHf/i8s+NBjxV9lkt7nuu6mOI5hTBrPm7/AG37zjc/3fXrV/RccI/m5K9SHarbueupv'
            'Ojrm3VsaGbbWMG1pa/Nq1Yq15GHSfgeUV+pPEo3mxCykabEUaLkSkI8oJd1+tiUzWjqVigg6Oi9zTg2/uWfThk+u'
            'b4CzOOMlWs5dItVP44GdT93jet4Wx3HdcrmUgFCZrK8dx51T6xljUKuULYnUz2Y9AEji+PN3rb/x/gbNIyZ4Zp/c'
            'MN7DB67xs+5fKqU/7rge4riKNElAztU7A5XruuJ5Pqw1MCb9XpzUCpt7Vz7VGH+ZEBwwxXHKf4qe6RZrf5fEMtK+'
            'h+S8qex1McgaSXtERB0XkecADNx56w37ztTtnMXMZJNzoQDKmKS87etHFySjpcuBWR5Zy3hgyuFN6z/+P+c6+Blb'
            'URTpsSxhrlcUUU+VUV20W5GU5qjGbK9CocA5G0y/1Nb/AgIHkUDMgQa5AAAAAElFTkSuQmCC'
        ),
    },
    'cpu': {
        18: (
            'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADnElEQVR42n2UTWhcVRTH/+fe+95MZpJJZowmFT8W'
            'LQaNkC4EFcQ0VAgpHXWTARubFlFDpUUkSmlFQjYigjsXFiJtZxqEFzd+tDHUkIqElpRKXWRTuklom6S26ThvPt67'
            '8+49LvJhisE/HM7i8j/nnh+cQ9hGY4XJDpZO8b0Dr614nif9MNMRimDpg4H9D/L5qWS8tfnphXt/L3wy2FvZ8Igt'
            'fgKAfo+lJXHJsvloZITF4oNH00aY646I9Y+MsNCO85IxdD3F5gUA8DxPblOIaSJHxoVGkmt6dJTs8J2eosORaEa5'
            'NjpKtl0tl2MUOY7DcqM5AKh/6zADxNlxPj6b0O1+JfowW+DX32TjtCWlLJaDr7IFM/xtYJufc4rYZa5WAfDE1nHW'
            'EiOb5+Myhi+qGl9KgCUjAcAGBN9lNEhAGUIEBweojh/OH6Qjm+OMFaY7rDAzDtfFbHJf24KPzy8O0qf4H/We4aG2'
            'FnzzauWXuwxh6lb1KJZcZOY8CAjqOCwBdM+waryDLkfiqKlDAoCMg+tVFMpP4ZJYhA01ygCdYYbVkou0tdO+PF9V'
            'hCs/HqRj+wt8rrENA2Fp7c1tBCp3MfvzIL2SPcvHLHDy/CHasQnb8zy5Uku3ZFRoPUbcWJh1bKx9RFGACAwCQxIQ'
            'AQALGAmo8fHf0wBw40bkCz/MdMSUXdJMf7Ul8XxA8NfxWyYQGMQMYgIxwACggaAlidYaKveqqKy07zLPqLiILVUR'
            'HCVilEJ85gINAMBA3E1Ach0SBLgJIKoiAQDSwq2G8An0MYi5GkTLDzHqO2vmXIHLxSfEcNMtdMsYTloNAQuoOKAD'
            'nCo/ie9Ti+aIgTxx4RA9vskon59K1qR42bU1/kmJtI5Q+a2HIgDT6/EfvVHgmlCIjRUu7LVSsl5151Rozc5kquli'
            'Qjbg2WgV18qZod4835aESDIcJlgNBNLCVYDDErZk8X5XvJJJq9SvNSNAutilbCa6GZT87kBJ+Vh4zTq09610Snxd'
            '8UMTUuw+wcr2pHjEL2sdkSoSc0NXU9C0s3x5uKSiP7Qh2pGJbtK2Z+Tc5DKA0+++3XfCO+U1lxpTqwI89M5A39jp'
            '7yb3pBqbZ/zS/RcPD2TnHlpaz/Pk/Pw8dXZ28gT6YfUUb6xh3SQEA8YSEQAYckhD1kN2kusnROZyOa0AIJfLma0/'
            '6ilM7nGkKgJMTitKUTi9m0VsCWBy03/OyVqwu2RpYWjNZwDgH0ajuHISfNTOAAAAAElFTkSuQmCC'
        ),
        22: (
            'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAEXklEQVR42rWVW2xUVRSGv7XPmc5M71NawEK0gAG0'
            'hotVQmKMXAImgoZIOl5o5cEIITExPpkm6DAvxkcf9AFFo5SWZBo1XBWhUBKFiBpIpESrSEGutil0nGnncs5ePnSm'
            'lIuJL6zk5CT/zv7Xv/79n7PhHpXcDsRiauJxsVs7Dnagtv7y7yuXA/Dg1+X14hxTkX1X+la0AdTPObh6cu3UdwcH'
            'rrS9tu7pPYlEwolGoz6Ae5dmBrCO5uYapCEeFwugieZMh9nUmMuM/lLEduzcXR4OhxrVOJMAeuvqxoXeRqwSj4sH'
            'kDTVWdSOrNmpswBaM9fLHzOnczmNlBSxs96pyeF0xivXdBZUoOdWK2KxmGHJFkMP9seZzCkJ0mmCLMDezayiBrAW'
            'asNQPnT+9fdfafgwljhdQm+XF4/H7R3bVm/XXifENC9Dm0BGBXEKa74FxwAWfEAMRl3eFKjem6SBjZK/RfFHHQc6'
            'wkF3xkAmKN+XPbE4k2TDvvXy8f85/TWf5VdS5R5oSv18akZ4eHQka89tWLdinQugSr0gD4ioqkUdIftUTN3UFWTa'
            'IkKBOu7PJ1GAQAjxUlxa0E+yB4zFy2seNaL1gKeq2QlxUwHRVR06MxjmbDbNq/ta5dNVHRoxloMmQJOfxwIYB1Hl'
            'T19Zvr9Fzj+7XZcFq+hO3WDJN+vlKDE1xMWagiMKMKL4AEVP3QDTTYAmmwcBI2CsjwZKmWXg4YIkKeTLB6AQRRdg'
            '27bvKoLBpBy2f1cPM3niwfs2jxUQBEXHSPw81vjkJ3pdSbqyvX1/5RA1vNG6OGkA/FD6uIRDw/PNbyeAQuvxWQQQ'
            'iu+bWGHdM9ksLNQzu0w4OByS5LFxxaKyT1VP5Y0bAtaOb8rh4CLWwyJooYEVF9cqAQBxXDUOZAh2qyYHReTSOPGG'
            '1pVvATzSqVNmw1rRsQSIz2Vf6S+poMHmCs1cjD/KoBh+BVAf67pw2Jv3TrxFTtz1k7ZuYVSDNifUAW5ksjwZsDyU'
            'z4w1KylHPOjb38qF5pA6I9mxtIzpv1kuwNb2b58rDZnKc7mTk066Cz0FvysqRasvFp5bqwW6wF/brr5nsYv0j9Wb'
            'ug7N/icvyY0vL9/tAhix22unPlAVSqW44MFl2Lzm8/x1NYxadbUYKVHPiOOq+lgxqGNx08rmiGAejaTfrqmcTvp8'
            '/zBQPUaM03L12rWIydyg1IvU4M5oo8LdYz0IBgvRU8jlXMRASWFsz4OIA/cNn/3EBi72XM1UGYMzxH/9u5o/0PIF'
            'NSd7UDvtOE0vqkXKbLp0oXNmV0aD3T8x7z3wmCv9yx6vzbbpYN8LL7U8/+UdHicSCae3rk7oARqXmHhUUs/s2KtA'
            'yd4WOVpQLJ1fdLu5zNWheMv8HoCWnV9NiZQ1uv1DFdWxI0fcBnDLBgby0WjUdwGK10nxakJVcp2H+lCbKqSDLTt+'
            'KKuXZK9K+K8ilvQOj4ZGM71G7UB86VIvkUjoRK57Uv8Cw5P7PjjakJwAAAAASUVORK5CYII='
        ),
        28: (
            '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'
            'AskF/DOVsm6zAAAAAElFTkSuQmCC'
        ),
        36: (
            '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'
        ),
        44: (
            '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'
        ),
        56: (
            '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'
            '/g9Lmcdh7Mk4awAAAABJRU5ErkJggg=='
        ),
    },
    'ram': {
        18: (
            'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC90lEQVR42s1Uz2tcZRQ9937fe+97b345yfhjp4gg'
            'GJXSWhfdNAERl3Uxgz9aXAgFt1IXrmoQxD9AFy5airVgMqsi7qQGlCIqjRFHDVZI1KrTJNPM75f33nevi7SlW6GL'
            'Hrhw4HLh3HMuF7jXQAAAPc1HMc9Y+Z/T88AD7S1tt1r+7io6tHrhWVuOX/LjSUAgUlUhAgHMgIgqlAgGIFWCJ4UB'
            'VClxgkl28dsDr1yip3/4uBSC19lFVyXNNlSEmbgEaKbeZ7DGkZJVkREZZoWWSWmoUOEwmNW8mGfNnmIWlwBktD85'
            'SYpfglqla6bBcVU6kzz+cBew74ofnYgemukCuPz9geMvKslVclGXBuM3AezAhLN2fz9NNYoOsqX3owfrSMfX1uD1'
            'VVuuvMB6raIm+TJo1N/Kru+OD6990uEkXgxnatgrsj4KnQIEe9MqAnyuuW5nvUEZxFNAe1l/NxWVAZin2U4/BeEf'
            'gmYySfsZKILIBGACAL7tOpMANBNUSw5QJcAFtaojcADvEVRLToG6MDxZW7PVktsXsA8b5FPR0DhV2STi1t7W7kxa'
            'pJdijjey3cHnarGSCbpBt/c6gC1bi34qevnL+Xa/JAi/BmWntJDCirFEAAdbw+43z71x5Y7TuHKzbuHsHfzTW+SZ'
            '1QvWsGU7HQZpXM+L0SP100/+eP4GmEggWVSphINsePn3x05cBBEeXT9/rOZKR/aGwzTwJvFGCwoDp9MiEuEb9ueF'
            '1ujYV0tvV6fJh5M87RslB6hKZseNncqhhdX2Zy3AP79ZOrnTCJ6ww6Q6MdJz3lRsFLpiL31v6WhrwwLAa2v3rU0T'
            '6li474S0oSATFuiMI2mVG94C8Ef+Lc/GG3ouD9xh5/FFznTQQ+4PJfl1CdiP/4+a3w4MfwTCdYjGQiCO0SPlP91o'
            'VADAOJQPRqH+DdV1oeIv9nZTSWIF/Xb334iqUrsNBtq3G51mU/HOCi8uLhQAsLy8bDrNps6129TpNHVurk0A0Gw2'
            'hYj0P/lxcOW9fYJwAAAAAElFTkSuQmCC'
        ),
        22: (
            'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAADTklEQVR42u2Uy2ucZRTGn+e9fDPJXHqZJqW0FF0o'
            'CJVMMIqLSC/YhQrqShAiliru/BM0FkQRRHAjQhdWacCFe1MopEUX2qgtYqULI1ostdSknXYmmW++7z2PiyS9Qd1I'
            'F4Jn94PD8573ORfg/1gLAgAk7j5xwv9rtT3AybdOGA4dsntb8eSPM5sGMb6K0loqSgeSIgwwUM6tZZrMQOcJibcx'
            'DPTOmGVd9VeOnBp76Y8AALnhE59lz5V5908SBSAQqK6+qz4ACMhIekArgCChQtJJ6hOQSgVm2Cq553fPzT3uVi3W'
            'ZLrWe/e79tS2WM32y3PPfHtqdL49NaqCe2JteG9tbPuO+fGpkTLpibh5895ae/v2+fGpkWSajCOt/b0Y77Pl/isA'
            'HunuvFwjADx6euYSgA8EuxCb9c8EKHVXDprZ+VgfPg6S5fXe26Q/yor/yVeyUHS6N9hlWSiu9w4zYY5ORy31WmHV'
            'ajnCSUIL3pMAodTy3nUYvdNqj7cgqelCiHAOpGvdYAmERunMA44AEG7vpBvIlECJcgOAhcxKiRCR01lplkonDznl'
            'lEqzVNIFypQTTmsDDHdTVjKqGhpDPtRrwWBDArJQr4VQHw4U6jAX1hl2CzeGPMkG4bSutlYxTUQVKo+V13pzSmY0'
            '97WT/iqWOscZgwPxZSVqob/YmQ2NWgZo1leWfx0scjZuqFUAfiEYAcA3h7XucQLZ/L594BSAfXfM+v47+Km78cTp'
            'mdcIlbdaEX1i/o9bpGkHgbzrXQBCshxgTL6ymjdxZuZk2ag+iO7yggEksLp5ZBmatbC43Hnj94cOzAHAA2c/fbLe'
            'aLxpV1cgJHi5rHDKQ0JUvToar+dpOWYPB0j0Zz5/uX0hfrsYhrZkBTYUznrRWINoLg/ceXHT0+8BcwDwzPmNjy21'
            '/FgvLy8Npeq2K1n6ZaTvJ5aq6ezWa5UdP2wctH/e9cIgTAM8NP7ib68fOXalpuGPc2pXMLdQwu6X5B1QrFQwsf7t'
            '8ctDO+NFfBOs8lUZsa+R86NOJb3TLML7nVB+ePjAs+duTsX0tOtmWuig6JNc9LKewRYlOk+WTji3Lrzi0/l+cFmC'
            'dSlcuJLJArBwNQ7KCJ6Dph14D8/mfy/+Bi1El6y3MwPVAAAAAElFTkSuQmCC'
        ),
        28: (
            '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'
        ),
        36: (
            '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'
        ),
        44: (
            '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'
            'fJt9wDG6f/wAAAAASUVORK5CYII='
        ),
        56: (
            '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'
        ),
    },
    'gpu_a': {
        18: (
            'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC60lEQVR42q2TTWhcdRTFf/f/viaTaRIIpboorYgQ'
            'YqEIElAEEZFK01oVEj/qSrEgSCg1EQvikJWlDUSI7gTBhdJEdJFiRUEoVruQYNESMZsUNJoWbWecyUvem/f+x0VC'
            'q1vJgctZnMvhcDkXtgkGoJGRgMFB/S+HexfNRudKtjfRmYPDRNGb5GWEFN1WzRAlJiEzkMNMCDA8cQAb5Yy9ce4D'
            '05nHuvHxVeLwLEW5RGlN8OCc4ZXjfD/eGU4dRAbmMAJESRz00yneInP7QizpAjyN7JRNfvGbpoYPEARPI3rxXLDx'
            'z2cAVD/SR614HO+vYa6NqZssL4liiKiF5KEIOwEVhTp1aAzsVSrRPbSyRcwKnR4+zN7qYZbX7kPuGOJnSvsTUz/O'
            'DRK6KpkyR1wYWEoQD2B6iVZlH82Ns3h/3ibmX8ATsZweoNQKXk1MTcz/jamJ1ECCwJtDkUNaw/xDGAtMznUI3B1E'
            '4Ws6PfwySXAd3IOYK3CEyByYu8VbcOTrAipgv4A9zDtPLmL0IU0jm6a363nQT7ggAvMY2hzb5FtGVvFADewi4jI9'
            'yQB5MWrj8ydwNkNz/QYuO4fKXpADQjwhKATC20abfSmxIiJtHeWv9COS6H1NPXERZ0Ok+SM28dUagPfcBLVAbaCF'
            'aAAFhbeQJBJlUaWkxyYvbABHNXVogNBid3z+RwECY6G64IYar9BOxN6W5+oORy3bRe6eIggiU73uqC58QhIMkRdt'
            'IAHdQFbZan4VsYHbOqx8jrkupDZJ2E9eLLGzfdCQTHOj7rP0yK9xmbW6y9YPWd+eZ+66/t34yu5Hpyqrl9+7Ft35'
            'YrfS7+NifSXt3fPcrsbi2+3dD5zs/f3bk/vTS9M2dj63uuQmzfy7H3/9qVewmoXJpWqRTlzp2j98v1/6crVMxno6'
            'jRNlGH+TES7v8Onrf8Q7j90drn+4vF4bmXx28Mrs7GywmRZMYBKmOs4A1etbjPu3Jv67o63H3zb8AyxpX7XWEZ3M'
            'AAAAAElFTkSuQmCC'
        ),
        22: (
            'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAET0lEQVR42rWTa4hVVRTHf2ufc+6d90MdH2llapOO'
            'Ulhq9iEwSoJ8peAUiQlmJUmUZpr0ON5Ec0Q/GPbQvlQySrfsMc0YJHEqrTSTCM1SxIoYGxud13Xu++zVh7lj4peK'
            'an3Y7MVe+7/++7//C/6nkP6NKsJaX/4V2tqYiqAXc/V9818x7cf6k/GG2wdSUjmAMKfk7T9j7hrF8YTOdEJie9sU'
            'RAB004y5RNydQClaaGcuwbZ6mYACWrjeX9+XK5ncMlnV8ko/8E845gT5sAEhipoUkECsJRTFdSoJc1EcNyy8L4HV'
            'IpQMxnjk1eKoi2PuxepiahLVboFDMVl7UNbsDS5Ks/XuOaDXEGo7K5p2Fbj1nTXMuBXLMSLmOlLZ07jeaJIXvqWi'
            '6h4IQ7qqigrAYnG0osD+DhxnFxGnBoBk9iSbZ21Qax9gVcsnxOcbfk7uIOKuIx+uwPEaERZSUvIcZZFldCd/w6aM'
            'KXjNoCS1YXY5yDsoy+lOH6I3m+HEmQkoy0H20DB7mNS/HYJ0EGoK9DxiLqDaAU6KTM4Chv4FcIBOsEtA98vKpkZU'
            'X0Wkg/E1Rp788F0s70O4vFAfQVQQHLAGEQdRAbloW1P4ZUVEEJ2I1X3q10VwzDQGlQ4j7bylG2csImI8VGoLKv+1'
            'AwtSCEYE+IGS6DKcUUtBRtKWeA+RsVQVvY4CudTmv2vtfuoWq6VY5w2EWsqi4wjt/bK6eZ6saq6jO3OYdB7Uvlkg'
            'nENFUUIwFtUQFQW1l0sRAuWyuukMyezjpPOKZyboxrvm6eaZT+NKDZncSnnqo6OqCEIlRosQqtGwBKgGjRL1TF+D'
            'fmBFgBSArG7ZSj68GZGxRLzHEHM91i6U1S1bCkOnIDsRexyV3YgcwchuHHuKZPYzhCjFnvZNXsPMsxiau/MVKytt'
            'ziVb1ymxWF5930gsZr/Zvt27qaOprKtokEo6J1VrdnWd9eeXHqcuPW3AIYeKIdJ2LnSGhl0P48gWinvK+0f6WYq9'
            '57mQPIe45QgGm0thvFLQEA1TiFcK+SQQIl41musGccApRXM9GMdQUVJOT2abrGp+VOLxuFNfXx8ef3nNso6hk7eV'
            'tX3XCJbeKyYtqGk98GKHM2BcZuCY6Ve1Hdh0quja+/ImMmRK4qtnDpdPXefa/LkJiSMvHR182/qydPuXU7v2vSBP'
            '7G1RkT67KUjDkIVHbhhezcH8iM+tqk4aWbugMTv6gyrtydWOuHr6dndifHiqda7nGm/pjQ/uWHNyf0M+tJnNwx95'
            'bXH0xPpj7e2nb1myovmhkzs8gZyoqoiI7tm5Z5g7eNSdPb//8jXAoBFjppxv/THIWHfooJoR43o7T36cthWTBSpK'
            'e1ubkiVXzgoJe4sSv35qho2fk+ntPL0o0vjF2u/rNBaLWf73UFUJgsD1fd/4vm+CQF1VFd9XEwSBq6oSj8cdPwhc'
            'AD8I3HhcHUCCIHDj8bhzKd4fxfL4rMA7ZakAAAAASUVORK5CYII='
        ),
        28: (
            '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'
        ),
        36: (
            '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'
        ),
        44: (
            'iVBORw0KGgoAAAANSUhEUgAAACwAAAAsCAYAAAAehFoBAAAJXklEQVR42u2ZbYyU1RXHf+fe55m3nQUELS+mH2i1'
            'VoQqAV+i1t0SUsSVRNsMao3F+kHSpBULKFCbDpOaIKANlb4E00RFjXSnUQKrkmribtqgsVDTKKjRiEgDLQgV5v15'
            'uacfZnZZcHlLBduE82nuuc9z5sy5//M/59yBs3JWzsoZFRlKqXkM5L945woFd9wHVBHN583/SjSH8kcGb4qgAPpI'
            '1/kYmyX4gjxNAL5Xlx89t7P/xKWAG3C4X6EPzhhLOvk70Gk4TeIQZGjYnM7AIigiEdZsphHeI4tffEfzeSOFghPV'
            'lkOPXt9O4P2FTGISjQh8yxl3dbCEDnwD9WgPsUzl/g17WIp4LO2wUuiLdIU/hzZ/EtUgRNlBGG8GEVAdOiNEWljS'
            'zzfTRFBVhEuJzCQy/lgqwXwRFmq+w3qDED4V5xwilkhulSUb3vxCE+7BGWNJ+u8RuyzKlKa203mDnnGIGGKNMK6s'
            '+Q4PvmRgr2NcWdj9FZFCMWhivsNjZPocQk9Z0LN/IFnzuQTsdUNTVF90mDY7vP615nIWQIrFGEDXTPFl7taQcNd+'
            '/PE1jLQjGve/6w3JzKE0YZLPGygic3GwFV05axpGbkH1aho6DkF5ZNZH+kvpJYi7ZXHxjc8wTi5npViMdUXXdEZm'
            'l7Cv/KQseWGtrrhxFUlvEmHtFgC99qY/0Ijekrk99+qKWXeS8e6iEmRxCog5tsMAnmnhstdIgUgfmj4cP/0brLkd'
            'ayByQCuQKX8UKW8KB+vzdcWs1VQOLRDpiwacnrC3iXVxXybtT8PK661vuZa0PwXqKQDS6Wk0ouEteF5M0v8mlSDg'
            'qAw5QZHodLr6plHYdB9tyduJHFQDRxg5YhfjG0elsYn91bVYIwxL3kOm/SX91fVJZufMAAMBOBNQC2PU1FrAKFML'
            'YyKrRFaphTFouXXMNephDPKZhD6+w0sLSjV8imHJSzlU34VjJiLbSHgCOIwxGPmtLOqZQ+hylOufMiI1nZp9VIrF'
            'mNm5w/ZFBcGCa+nEIGIHsYM9fPR65N4JHVZtQmXFrFvJJmdSCg6h3CT3b9yE8nOMEUQ8akEV62/RXM7KfRv+SMT3'
            'qIUBKe9uXTHzGikWY0am7efJHseIcOxaUfkhCatE8SpZ1PM3fbwjJYt61lNrPM/wlOD0FZn//J5+hpDFPS/RCJ8l'
            'm1Qwc08H3ZkhoitYW9dlXeeAXMHBusNzz2g+bziU1haTPEfswLcX6MquiQCMLInmOzyMrqUeCtCh+Y4UB64MT3OE'
            'xdBwAUbGk/GTxO5jJLsLeo3M29TQn+TSqKykEkLCm4DjGSkWY5m3qSGFvgjn/5VKWEHkfFLZcSdsEU9RhqY13yri'
            'khhRhKosKNaapH63z6F/TUfMGJxG1ALBmm/o8q4+oFkYJLaAD8YiLvl5Q2KIwqGKEYvz9tKIBJWxurxrHsZcTmnP'
            'VaDjQSDleYQOIneAbPI6whiMNOk5iiF2ZWL3yRnAMEocpaj8eydBtAffjCSdWEXKux34Kknf4Nx6guhmVL+N08up'
            'hf/AKdTCkEYY4hlFeFce2LRP10zxT6/DIoqaRLPWy3oSHtSCOrUwQjUmjAIic58s7FkvCze+LEte+JAougHVPXjW'
            'BxTfCso6AHZnzyCtGVlJPaxiTQJVJelbArdMfrrxA83fmNF8LqH5Dk8Wv/QWlWgGQoWE9akEu9DM71URxpXj0590'
            '4jXHkfs27tBlN8ylPfkUtVAIowrWvNzsvnqqAyh6+IarEfszoK2pcHNkcfGgbs1ZJuzVIwB3NACPvXcKDvdbWHO3'
            'L3Mfe1ofurGNhPk1vm2jFr6qy298E9FtzfdlImImMywJpcZBAnenLHrx1YEuLd/hHWdKlxNN8KfWS+weG2t3zsri'
            'njUE8TUE8Z8wAiNSVzAq+wNGtd3BsNRkVMscanRTblwpi3rWa3fT2dNR6bwjkm0gtKbZaRW3C7mia+L0hTeAGbrq'
            'OxdSCa4gZjyiDpH30fgNWfjizoHmPleMBzq1pX1OFWF5sBFjLiaK9qsiPOzuAEmTPX8vAI1PLkZcTfMYomg1sf8i'
            '8AJWkoPHsEHHpYOiHVWaDXh/lA5PC3Lvc+8D7x/78qMvonAUTgsArxwEDgLwAEDzBw6Sdw9/3LQP2KfLu+IWWOSw'
            'w3vKrWFS6qg6PDGEXK/Luor4Ygn1yKNV36B1Q7a1LgOSckh4/BKcCIVERgiqSuAr6jcD1P9e/7odCKpKmOzE6nBU'
            'HUqzT75ku3hM/4rjsa2AFkHmNmc6WROLfQgHWLAaxwBOjFGcQAKpqxqcc76I4gzYVjCcM6rqMEb7J2vARsREIU6S'
            'Rq0IOERVDTgn/TZAagAJZ4w7BxWHtQaJuwHYtldaFynNSwpd3rWSbHIh9TpEQXPKF4FUWzOH61VwrUB6PiRSEEfQ'
            'qB2+EUikmntBHaKwPz9O0kaL3JLpZi/vGSg1nqY6dU7/XZsMvgCUAk5XzPzuR9mLlhxMjJ0cRQFW42hi+e+veRpE'
            'b7dNvqphUxkxlmGNff+8oLJt+4HE6JEftV10GS5y2IQZV/3w3TH1nbs/aJs44VDyvDEah1hceCIbO9ouukxcrFiP'
            'Cyvvvdnu9u+IMRu9BRvWaovyBPQI7tuyZYs/derU8OF1r99/bltyebl8CFUO/HjqdeP4mjRWP9v3jme9ryeSKcrV'
            '2uPzbrv2rl+s23rdmETcV6/Xgrb29sSn5eD782+76qlV6/78+LB0253VaumkbIxOxH2Nei1KplLeAclcs/jmSZtb'
            '1URkUFE5godLpZJqd86OjD5JJ6NKlIorUTquuidfW5vVfN5k4oqm4qY+60pG83kzuvFxOhWXo7SrRqmoEo2I9ic1'
            'nzdZVzL9z56sjZRr6s8t70hrd86+3Z1PyFEV8DOFQ2YX41BsOtM+3EtnR3gqZlSQHq1SKLhYzchMdoSXaR/uKaZd'
            'CgWnxvcy7SM8P5XJZNqHe5GapBQKTtW0n6qNZDqbyrSP8JzxPZldjLdzSXzc0tzZ2dnsIZTXKuXSE9VKGdDqp/W9'
            'jVbn+USlUhodhA0Q2QwQq+4sl8pPNMJ6VCmXPGPMW62fvqlSKh2s1ionZaNSKj/RaNScOowY+zHAtm3blP93GbLh'
            'yOfzhs5OQ29zXSh8KwLo7u622847T1rH4QoiTlVlaW+vpRfohKWdnbGIaF7V0Nv7X9k4+4/OWTkJ+Q9ppAbaUlCS'
            'ugAAAABJRU5ErkJggg=='
        ),
        56: (
            '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'
            '/B9DkVN1wEIVrgAAAABJRU5ErkJggg=='
        ),
    },
    'gpu_b': {
        18: (
            'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAC8klEQVR42q2RS2icdRTFf/d+z5lOXh1Ei6jQKsZQ'
            'XUkXIkpXlUirVTqirYKvhSIurAtLjVMNPhAliyC4EAQXSidiogbEpbRQQUqlhUi7MWCDj9q85pGZ7/v+/+vCSXHp'
            'IgcOd3HPPRzOhS2CADQOWXBoDDsBnPifh5vamQWkNiOOLU00N2EPphGv9woi74kQwEAEwXAmmBhioCKYAQo+iaHb'
            'Zfrht+VT+eyobRsosZhEnMwKLnlhDQeBIB4yjKooIkJunp6BogR4XBJT7WW8kRu7w+EKJe/wV9u89+T7cnl2wval'
            'CY84Y0gdP+x/S6YBZus2LPCACH+q0XLGtqyHi2MgpyJfvWZVDbloAXd7z4FIeWmwzG0rLRYCOOeN7dld7I8vcJ8Z'
            'EwK/GPwNVBXG0pS97Ra3a5ggInS0YFSNZ7uwe7nJSfN8d2BSjniIovPs8zlLIqwhrCmsC6x5WDUDDRHNQT20vXIv'
            'cLb2Jnmg3JBEHJ07bs+XIv5S4Z4YCoMQUOtTQDe/pqUMEyNFuCjC/d9PsiDCcOGYMmGqOsgTXrhQhESAF8FUMOnz'
            'mlHX8EBFHacNfh6pMFr0qD00Ka+EyvSVdZabbea9Z0hAzRN6T2iesJ/wXyMAE5wLiVaVw7+v8HmS8sm3dTstyp5O'
            'l71PfSjtKAJvrCA0RWkhNIFVg0IdEpYTbMNTdjmDT78jXeDwN3UbDWPi8eNyHsAwmVng7NDNvNCrYAPgm6DdguvJ'
            'OGgBkVjddM7xZSlmz0ZOS4zEYBlIMS+iWjZPF71WbCZGyaCVxlQ3Mi6NBIwLZtKYQTutM7/hoqbmyblkhMfs8vZX'
            '01s7HzSX3Ed5uvpM6JKfKKKlaNg97q9U3h3YKceyRY6VbefU+MuShXWQWk3cx1/M/yjm/3ChO9N10dj6naca13VG'
            'n+vsuPq1ZqVdmRanTO1XLZI7ipuWZ01uObh+4+L8i4/u6jV2NIJ+WhPrs143BcH6s143NUzMNncm9h+NYcJW4h9n'
            'h2cMtueNwgAAAABJRU5ErkJggg=='
        ),
        22: (
            'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAET0lEQVR42rWTbWiVZRzGf//7ec7rdnbc5rThVkqk'
            'rix6ofoUvRAElrgNtAgsqMjA3gyll03PTptBpEFQlPYlGyqdbK4YFEU8rih6YYaphau2QvNl021nZzvPzsvz/Ptw'
            '9uI3KfL69L9vbi6u/3XdF1wiyMygqtLWNnf+L2hrQ0VEZy8SCTX/l9IZrlmFu1/Q6tooVa6HGu/fKfctNGIhY+Nk'
            '1m6XM6AiAN0vaVMoTCdQBiAC5gJqX0HnFsSY0lkAnVbnK4ig2Sk2NG+TtwWgq1UHQzbHCx6vik/IM7jqkTHg+wa1'
            'LOKeT8j28bBBfDIehNUjZ1kEPPAFbCM8oMojcUOlPa0wki/wXdM2cWZUfZrU1WKxxC8yvLKNvTAXyoEtelugwFEv'
            'xLJigYGAcOWUzU9Rw/3FIh4Qtme2BSoAulv0bjvA3kiYGoBMkf6eNl4pFvTRxm18mVqDAXb5Nu2+z3OWYY8K62yP'
            'rfNibDiX5nSu5FbJNgzZ7s0aw7DfwMaRcb7PZMmdOsUKT9mI8FH3ZmrXfiiewIjv4wLnUSZUGRHBzebwFUyJEECx'
            'UEYJ8Rjw9co22SPCO0YYqb8GszopXb7STYiNpecExSCqWNNElvilzGcDnvZYEQS4wfP4IrFGg5ZwR20VtflhPuhq'
            '0YfDIQIqLJ0WclHYpdYhYhBRfo1F2XBjA0+IsPivIQ6IsHx+Be8B5PNsn+3rRchnWuKrUqYWu0VYGo/SkPd4qKlD'
            'mhvb5erRCX7M5kDhfQBRCuqjInhSCt5TH9XSPEeM4Bkl1piUUxmXZ908GrRY0dWqzT1btSVgUePm2NTUIUdUVVSI'
            'ixBGqUSIApVYhKJBDFraxcxYgeACNHfIG4UCt4pheTjAM2K4ruizrqlDdpTyEFXoNMIvPuzzlT4j7NMiv09M0YsQ'
            'KubQUqVb9awx9JgIm4yPHckzemdSippQI0nxd+7UwKJBygshNBBOy30vxsdSiaGyGhZMuVVYkQrkxDms8jHWWxY7'
            'ckJMAA606pZYhJczk5zDEBMw6uGKRRmKp4qLoUyULOCJoVJ90gKWL5SJz7gxmHkxYiMZ3mzskKdMKpWymjqk/Xzk'
            'xJNy/cB8d/7g/mz1YMrc9GfF1LzTb00sPN7rXftbPB8/+3q67vB4+opD5YXw5PPpy/si6frD48XQaKu77Gh8YlH/'
            'kUyWVY3tPA0q0x9aJX3Ft31li2G87tBX6bq+3sqrggwtOfjxZG3/z7ElwpmGz1Ju/KRbrBwOHHmwZVexcjjoxk/m'
            '+m9/7d1wfYF05fGBe5PS8/j6PhtERVVFRLSzs7e2fEH1PSNDIz9AnoV19bec+eOk4wXlsgU11Q3n/8587ofdmw1a'
            '4U1WfWJHx1Z5eJPFTPRgRa29emoyO1ARvOubY8fQZFJ8LjlUVRzHsROJhEkkEsZx1FZVSSTUOI5jq6qkUikr4Tg2'
            'QMJx7FRKLUAcx7FTqZR1Id8/l8T3VTVIqAIAAAAASUVORK5CYII='
        ),
        28: (
            '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'
        ),
        36: (
            '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'
        ),
        44: (
            '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'
        ),
        56: (
            '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'
            'sAAAAABJRU5ErkJggg=='
        ),
    },
    'ssd': {
        18: (
            'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAACb0lEQVR42qWUO2hUURCGvzn3bnbXJGQDMRJ8YFBB'
            'IoKifQzY6aoRBAtFsRMfiL0ItlZa+kI0CKbxLVqZiDaC4IOsD9RC8BUUQ9xkN3vvPb/FjSauBhT/5vBNcWb+mTPH'
            'mNQWKRgGY4C/0xpgAAZ7SMCUBiXjP2VIhpmKJW22HPOoYDLMC5lDJhzAdDYhsggYvrqIi5illRRLOhTkOJhUeYWI'
            'BDLIktZcTTNOsYFhhMEsOpMx+q8tsz0AFIf0dN2QdgP0vtS83mF1/Ci5t6SOet7yWXMBNgxp7fohfQSwvjOXL75Z'
            '0V28VShsbaskbWFLcBqgNsphEt43tHLyNxZEoxxxjtteXLjWZQudYGmTs3yUo2bG7LARgjwIOgho+yPnwIu5ACLt'
            'YRibuu9lmgfbR2j3Xo+jb7zGIXM8VML7qMwrDH5hMOCBGQ6fWg4zZitb44nGd9l8042uzDlgcd1kb87ExZLWAwlA'
            'KM/ZFdQWPJmV/7DpWW2VNWb2YSgapU+OkYZG9mJQxxaNct4SxggmrRnuyie5nXkRy2d6cq3skIe4zJg8b7Ot7Kxn'
            'H0M0woQLOBunNnHbdm3Y/6Ch+UVLmZbEGJcHCTxMGFT/xJNPuTLdb9h35urx59WRJfcLBStUuTTxhdhCvBd3M45y'
            '9TPjLgPT2XKY0q3sRMRpj1CxI5dtGXNU7nTZB+BEXXNPzcTFR5pPhjxAGBi7X1TjbXOaOFosabugwUASHpAZQbrX'
            'Uywwg1qQZ3k8zr3JFUqPjc+jAwRBp4/QVHxGyYWYBXyMv3Ls+moq9Pf3B//9h/zMLFn3AEG5mX+6s2kVGoQEM30H'
            'ePY14/ym7roAAAAASUVORK5CYII='
        ),
        22: (
            'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAADsElEQVR42qWVTWxUVRiGn+/cuXc6/ZlOB2htALsw'
            'NWEajOLKsNAuXBg7QIxTSUzUGBfGhchCNxhLWeDCKO40CiSSGmObmIAlMboibjTSkID9ISGIFaW2tJ3Sv5l77zmf'
            'i5lCaYka+iZn894v537ve74fYR1UuC+I3pMuqHpsEPe4436z/Jf8e3rU9PaKy4/oW6aGg65MjTo8BEFRAVUQhMrP'
            '13KKisF6SeK4xGeDOemlR00CDvPcJd0tGY5Fc/SpMi4QKzgREgoBjhghBBDBV/AFQoVYQFTxbZlMsonDe4d17HSH'
            'fA3A01f05fyYuhUZ++Z0U6Go2UL/Hc8KRc0WippdbV9BNbvvum5aYbqGdTI/oh8AyMm+wXeXN2/f//32XFuQS2RK'
            'o+7DoMkciBfBlrmRmuOh5TRfBRn2agRxiaG/FnmitY6f/RSPiQfhHKfnp3g+3cKkwCdncnLIKPLUA4HfYayGA8ZY'
            'dTS4GGtDLFBLGotQ62KsjbCq1A89TixKyoZYF2OBxnOdElderfIWRp3+OFEOx9STAFWAIMjgBY14QLoSSyrI4AUZ'
            'PIG6qvL6FQ6oWVsVidde6urdNa4Xt5X55knVhFzmy3AWbAjAbwMdRPlRjkdzXHMWRDiPiDKqH0eLPGoMKHy3rt6O'
            '9w129py7enLPsF1+9qI2baR2u4b15p5RPQqQQHl7R7jwzCVrp0d2mqX8pagr0Zh4wZZBy/y+a4D3znfzSlBPp7Ng'
            'S1wYzMlH+WF9x6TYaQTCZX44m5NTIpjbVgjm1lK57BwkroiUdwzb/UETL0a3IIpgpMARgVf9Rna7EFyJTlSPMcqb'
            'fj1bjQ/RIu3Aqbs8dl504JfW9gsu8I5WytOUwyI2mgeUhWrcUljEuhCU29x8WMQaH4DltbYYN791ZiZZW0SMVKfG'
            'lIBnPDyE4uQUDmVWBE8MnsB0Na4oBk8ED5hcNytO9J09QVNL4dsHH4kmGv325kUiz6fN+GipRLH2YSaWh2hKbqEF'
            'wM0yNTPLTE0LW+rryLoIcR7Xi9dYaGjmbyN8eiYnhxIC29J1DQ2iTPzU5s1Q6exf1yRws3pW40b1rK6KWA0+QEJV'
            'Pv9zqXTVtPmvd43YI2r5oyrbYUENzoBotaNEUQe6wmllCDkD2SBLczjN5WpTVZAf1fe9JAdtCR9dNSb/H5xJ4jTk'
            'i6TljYEBYunv7/cAuru7bWFcU+UUiXWi/wubIR3j+lplcd23HlWz0a1RuaOyje6WqxtcUXJnof4Db6Wyu4SRQjUA'
            'AAAASUVORK5CYII='
        ),
        28: (
            'iVBORw0KGgoAAAANSUhEUgAAABwAAAAcCAYAAAByDd+UAAAEgUlEQVR42r2WXWhcRRTHf2fu7G42zaaJbWqSarVC'
            'bGusSCsqVShVKWoavyDSFESkVCmKgg+++LDNoyj4Iq3xVfAhC4rSFEVpK2KhtY0t5qtQbMD6kbQ2H5tss3fnzvFh'
            'N+km3YDQ6Dxdzsyd/8w5/99w4H8eckNEVZZXQbTyhKqkVc1y30gX7XvDbTpGtSY7ybIJf323TJWkBUSFtBoOoM8P'
            '0RjF6BblcR8RQ7nZ1KoERGI5Ec3w+uH7ZDitaqSjR4OeDvwzQ/QmG3kq9wenRZjFEOAXVNsVt8EucoErfQXzhzSA'
            'EqknVtXAQ7OXGUjClkymtPaOi1rVNqiufUBPLXcNdw1o5oXfVduHdT2A7ek5kTz36+Ca/ls3iJMgh6o8189dWsVL'
            'PkTEIgqTmuUQgKTYL7BSPSqG0Cvd2T+ZqG1iXxCjyeWJbJKgkOXL3s30Mcg0oIVC0Rf2qpt+Z/Pl3FsSqzGnmtYZ'
            'RDTq11dq1vJu/koxPXYFhJ7jAPE1fOBmAIX4LTA9woU1KzkapTgoBoyF+GpwOVoQ6dQBjZeb04pqKgiC+phGxQoV'
            '6xKF47jCFE4Fox7jSz8VsrhCFi/gxWAF1BkkmsapAxSHwWIISxgs4NB6cFEUeUXMdXawCEYFU7KANWYelWDO5AiB'
            'KtYZRASr4BGMgsETr1RTYyX48EpN3baB+mYfL5QcKIybBCaIE7fVWPXglKnIMAOIrcbaBHExiMBVD6FG+Fhq/h8j'
            'wlglQbtvzxOjwOjOISXhiyAklY/zVzjrQ4xdAWGOySOtMgzw9JA+Ek9QQwTh38xWtfJjRiRqG9IHVVnlC2h+As17'
            '+koa0QLB7k+/2VmdiD/2Q3ZcfqutdwCZe2Ua+K7SCY9skhOV4r2b5MxSD8ACQYy0rUwk3rw9N87FunoFaB/W9SbG'
            'y3NYeMdU9i8+Akg18oax1OIAQz5R4NA9GSb6OnnNxGj2s0RBNcbn+OKrVvlZdOEzaVVUvZbMKUVD4NhbvW4hFhaO'
            'AcQbeX8xFoPPcjRWwsJbSKyG6RFagM6S8coEkRMzYb5hrCrVGfjS5E1iIWVY3GCa/Xt29iCm58mBaHfclSz/H2Jh'
            '02k1lzom68bUM5/uMizEwhwWEhDYEhZ4EANzWJgSFj5P3FaDyy6BRWPLt29v7Devnrtto+lrWJsHaK7j4OgYpxXE'
            'xJDwGpO9m0pYnNWHgxpW4MBd5drWVk52ifi287oFzyp1aDiFmjxnK2IBNMWsbUm6vKrgOno0aGom/GStHC9fuP2Y'
            'WoAj98vJ8nhSNdh+TG3vBjlXHt96WmMdqkFukAi9joY1BowJkMAKSlXmRYkqpeL7HeIqxTNSef2ZB6RwBtjVr0lM'
            '2eMdinTPurD3fFXde6laHm0f0IxSfMIWQ/uvhwE8oCRiKXaHE1wgwSVUZV552yXd0JDnsyDOFpajb9OisA/5xU2z'
            '9/Bm+SmtakRV5cCB40FX1w4HKh0j3FkIMeSBxM1pmlr080OM0CUeVVnQMqbTy98izu+9dJuoklaWtRHuAl26Gf4f'
            'xj/6sClVIGqQeQAAAABJRU5ErkJggg=='
        ),
        36: (
            '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'
        ),
        44: (
            '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'
        ),
        56: (
            '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'
        ),
    },
    'bolt': {
        18: (
            'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAADEUlEQVR42p2UT2hcVRTGv+/e9+bNlBjSQpRGKFgw'
            'SLLwL4Kb0tCVAaEoMxZUqBZrwUUWQlE3LyOCf6CLbNR0iG4yIG8oiGDaiGBahG7iH7AjNgsFqZU21UzGOPHNe/d+'
            'LqaxaUIQPHC5h3vP+XEu53wXuGmSiP9lvbwAACWBpD78+IuXYHjYO/V5n5MiN4IleBjAGCtjTErx87kzN041GnSS'
            'yFgyVdLX6vOfRFFxX5qn7ynT74CDZGQt0FWUl9iJMk8PWNBoICwUX3BZ17aC1qF2s5kCAGr1+eO12XOXdyxe6NPl'
            'p5/cel6rz1+Yrp99GwAMAMjjCKE3AWBqbi5KksTGcRwszU1FgOi/P/FZ1tm9L0kSuzQ3FSVJUpBEA/M6Pcd7PgAC'
            'RRisAuDetbW8Uqm4yYPA8PhEqq9fPGkGiwdCds+XyxV/7+Nf5c1mMycpz6xN9hg9kKEnjAUgAFBSthyr5vr22EH0'
            'R29hef0P5MFPJEQ23MbTMoR2wzcbrZckANi/smJYaTh9d+JOhEEdloDXb+g6q58nBnTx2F0byYH3ug202f4cviot'
            'Hg9BfwZ90RDaKUAMY5f5EXn+K0r2+Wq16rfmbQONjVVzBHgFu8KH0FpfBplDCjBQGkQn+wY5TilJ7H+CkiSxWHcf'
            'IQjuRsffB2EJ/RGwll6FzFN85HS2MNjkjiDv+e8lH5u5xnumWihYB8O9cAKy/Bk+8MF1JWW7sAAPALlz3AYylu7W'
            'AJatBCLI92OwtAft9DU+OLOgL+OAlVtdI43fCnJe/o44jg0wYpuNETs5GdP58ABurJ/nwzPvXLoUFxrLo4rj2AwN'
            'PWGTJLG2YErCJrFPz549WavPL26Txg/PHtEvI3t2ks7p2XOf1urz7wMA4zg2o6PloNW9ciEqFlezrPuuy92KtYFC'
            '/GXbfiAtmU7gvZUxhs7lNEHQXwjCl7tZen+Qdh49evTwKiWRpKanF8Nwd/sNAoecc0XvHSGAhIHYGzxKpJUxxhG8'
            '2M7+fnXiufH25r+Mt6tdBMQ4js3Nqe8t9PbtscA/UIuLUHB4EdgAAAAASUVORK5CYII='
        ),
        22: (
            'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAEBElEQVR42qVVXYhUZRh+nu+cMzs7ziz4RyGrlBBB'
            'e+NK7LVjVmh1oTKHRMvWn1UEBSO6EOJwuiwo2jZQx5/aVqtvsBBhS8VGuoiQcLswS7qpCMzWZd3f+Tnn+94uxp3d'
            '2Z1+qO/qwPe+z/e+z/uc5wXmHK21g/945ue6Mx9BIMr3afr6dDrzwPJ1IrIiiiIA4NwE0lJECQDxPA9iMTJZuXfV'
            '97eMiAhJSj0pCESFIe2Js5d3uZ7XG8eRI1bGRaQRFEILFQGiCCiSQiLtuF6LtebI7uc3vDGD5WqtHd+nyZ/5ojud'
            'aTtZKk29MlGePHm4e/O9Zi3Lj9uWIRVPcVWhBAB9fTq9aPnSba3J1PH8mUvJvdv5utbaYY2fYno8jkatyJGe7U+/'
            'uQAMIAIQuQMpmKgXUjrINQNTc2OOfvD5ltSi1LlqeeKhPTue/UUBwHgcrQdgvBWJd7XWjoiome4FIL7tcRnCIo77'
            'Aa7gmoEpKQZujcZABUHR3b9z46flcvmOpbMVAGoAIu3W2rHubLbs+76ZGQAAQTFw+PjxSK7veRGr2jZDcHlupWEY'
            'WmCdFRFS5DbA9jowFWKAXECBzjnMhrFc29WJpHcMo2UB5SfROQfLoUQCNasWipCKgrguN2uxEFRA4DGRH3ZlYLyP'
            '4KokJioVuN41+gUDwDQdLmsFqqZqFwBXA4cMLaruaaQSj6IUCYhJmKhThnavk58PPSHf7e8EgI6OwoLC3Oa4WjHr'
            'xzK09zAyLVsxXq6CTABciqQ7WA+08gyAodWrR9X8DlQTChTpG7m+bxMWt76FyACtXqIeUIqq8BxgtPQy1x4bFMk5'
            'Fy70mH9TsQAgFG8D2IDpioFSrQA+BLgEbckEJirn2Jl/uya50Mz/7ZsCkzVgrjk6VH/pxoEHEUdppDxiunoLibi7'
            'pojQkJAg+AtgpSDGzKckUPgeLm7eNDBRFzItCVRMGdPRNnadmhCdc+jDzksiFaTOsVi4QGMQGVoMw9IvGAgfweIk'
            'UY72sevUkBQD977k5mAKQcYWypkdnsNhpZjR+kZCa+0EQdDYGvEkfh37jGtP9IsELrNh3HCLq/edjssoZqROhSh7'
            'xaWXulf9bWfPDj+vtXZ0TjuFYcjdbw62WVu5M2aWHCoWi26hMCxBUPcSdHQU6PvZ+L33B59KJpMry2ZaNzh/fuDi'
            'q2fPfy0nPv5ya8MQCIgg+XfbI3/m0qb+c19FxwYu9s5gNhj96U+uvKYcJ4yj6Hdr5Q8RoZAWAqtoPZGag84OSlmS'
            'SxItLSuNid55Kbf+cKFQUDk/Zzm7mgIVhqHt7T//cKY185wY227iuOYusASUNNAuIo7rgkrdLVUq5w+8sPEWIKw5'
            '7T8sxP+zTP8EiUr4d1aVmnYAAAAASUVORK5CYII='
        ),
        28: (
            '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'
        ),
        36: (
            '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'
            'jHA0CrUUKv8FKNWg/lBTGDUAAAAASUVORK5CYII='
        ),
        44: (
            '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'
            '3Z//Aflvf5BqgOmuAAAAAElFTkSuQmCC'
        ),
        56: (
            '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'
        ),
    },
}


if __name__ == "__main__":
    sys.exit(main())
