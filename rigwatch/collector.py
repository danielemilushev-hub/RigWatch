"""Background sampler: turns the raw sensor sources into one coherent snapshot.

Design rules
------------
* Nothing is estimated. A value that cannot be measured is ``None`` and carries
  a human-readable reason; the UI shows "not available", never a guess.
* Sampling runs on its own thread so the UI can never stall on a slow sensor.
* One snapshot per tick, shared by every view, so numbers always agree.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

import psutil

from . import __version__
from .sensors import adl as adl_mod
from .sensors import bridge, d3dkmt, dxgi, nvidia, smbios, storage, winsys
from .sensors.pdh import PdhQuery

HISTORY_POINTS = 900          # 15 minutes at the default 1 s interval
_ENGINE_RE = re.compile(
    r"pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_engtype_(.+)", re.I)
_PMEM_RE = re.compile(r"pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_(\d+)", re.I)
_CORE_RE = re.compile(r"core(\d+)", re.I)
_ENGINE_GROUPS = [
    ("3D", "3D"), ("Compute", "Compute"), ("Cuda", "Compute"),
    ("VideoDecode", "Video decode"), ("Video Decode", "Video decode"),
    ("VideoEncode", "Video encode"), ("Video Encode", "Video encode"),
    ("Video Codec", "Video codec"), ("Video JPEG", "Video codec"),
    ("VideoProcessing", "Video processing"), ("Video Processing", "Video processing"),
    ("Copy", "Copy"),
]


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _round(v, digits=1):
    return None if v is None else round(v, digits)


class SensorStats:
    """Running min / max / average for every sensor since the last reset."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.since = time.time()

    def reset(self) -> None:
        self.rows.clear()
        self.since = time.time()

    def update(self, readings: list[dict]) -> None:
        live = set()
        for r in readings:
            sid = r["id"]
            live.add(sid)
            value = r["value"]
            row = self.rows.get(sid)
            if row is None:
                row = self.rows[sid] = {"id": sid, "group": r["group"], "label": r["label"],
                                        "unit": r["unit"], "kind": r["kind"], "source": r["source"],
                                        "value": None, "min": None, "max": None, "sum": 0.0, "n": 0}
            row["value"] = value
            row["source"] = r["source"]
            if value is not None:
                row["min"] = value if row["min"] is None else min(row["min"], value)
                row["max"] = value if row["max"] is None else max(row["max"], value)
                row["sum"] += value
                row["n"] += 1
        for sid, row in self.rows.items():
            if sid not in live:
                row["value"] = None

    def table(self) -> list[dict]:
        out = []
        for row in self.rows.values():
            item = {k: row[k] for k in ("id", "group", "label", "unit", "kind", "source", "value", "min", "max")}
            item["avg"] = round(row["sum"] / row["n"], 2) if row["n"] else None
            out.append(item)
        return out


class Collector:
    def __init__(self, interval: float = 1.0) -> None:
        self.interval = max(0.25, float(interval))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq = 0
        self._snapshot: dict = {"ready": False}
        self._static: dict = {"ready": False, "version": __version__}
        self._history_t: deque = deque(maxlen=HISTORY_POINTS)
        self._history: dict[str, deque] = {}
        self.stats = SensorStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick = 0

        # sources (created on the sampler thread)
        self._pdh: PdhQuery | None = None
        self._adapters: list[dxgi.Adapter] = []
        self._adl: adl_mod.Adl | None = None
        self._nvml: nvidia.Nvml | None = None
        self._gpu_vendor_dev: dict[int, tuple[str, object]] = {}
        self._bridge = bridge.LhmBridge()
        self._drives: list[dict] = []
        self._drive_health: dict[int, dict] = {}
        self._disk_prev: dict[int, dict] = {}
        self._volumes: list[dict] = []
        self._net_prev: dict[str, tuple] = {}
        self._net_meta: dict = {}
        self._net_primary: str | None = None
        self._net_session: dict[str, list[int]] = {}
        self._proc_rows: list[dict] = []
        self._proc_prev: dict[int, tuple[int, int]] = {}
        self._proc_prev_at = 0.0
        self._gpu_pid_util: dict[int, float] = {}
        self._gpu_pid_mem: dict[int, float] = {}
        self._last_mono = time.monotonic()
        self._ncpu = psutil.cpu_count(logical=True) or 1
        self._sources: list[dict] = []

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rigwatch-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def set_interval(self, seconds: float) -> None:
        self.interval = max(0.25, min(10.0, float(seconds)))

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot

    def static(self) -> dict:
        with self._lock:
            return self._static

    def wait_next(self, last_seq: int, timeout: float = 15.0) -> tuple[int, dict]:
        """Block until a snapshot newer than ``last_seq`` exists (for streaming)."""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            return self._seq, self._snapshot

    def history(self) -> dict:
        with self._lock:
            return {"t": list(self._history_t),
                    "series": {k: list(v) for k, v in self._history.items()}}

    def sensors(self) -> dict:
        with self._lock:
            return {"since": self.stats.since, "rows": self.stats.table()}

    def reset_stats(self) -> None:
        with self._lock:
            self.stats.reset()

    # ----------------------------------------------------------------- loop
    def _run(self) -> None:
        try:
            self._init_sources()
        except Exception as exc:  # never let the sampler die silently
            self._sources.append({"name": "Startup", "ok": False, "detail": repr(exc)})
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception as exc:
                with self._lock:
                    self._snapshot = dict(self._snapshot, error=repr(exc))
            next_at += self.interval
            delay = next_at - time.monotonic()
            if delay < -self.interval:        # fell behind (sleep/resume): resync
                next_at = time.monotonic()
                delay = 0
            self._stop.wait(max(0.0, delay))
        self._close_sources()

    def _close_sources(self) -> None:
        for src in (self._adl, self._nvml, self._pdh):
            try:
                if src is not None:
                    src.close()
            except Exception:
                pass

    # ----------------------------------------------------------- initialise
    def _init_sources(self) -> None:
        sources: list[dict] = []

        # Windows performance counters (the same ones Task Manager reads)
        q = PdhQuery()
        added = {
            "cpu_util": q.add("cpu_util", r"\Processor Information(*)\% Processor Utility"),
            "cpu_freq": q.add("cpu_freq", r"\Processor Information(*)\Actual Frequency"),
            "cpu_perf": q.add("cpu_perf", r"\Processor Information(*)\% Processor Performance"),
            "energy": q.add("energy", r"\Energy Meter(*)\Power"),
            "gpu_eng": q.add("gpu_eng", r"\GPU Engine(*)\Utilization Percentage"),
            "gpu_ded": q.add("gpu_ded", r"\GPU Adapter Memory(*)\Dedicated Usage"),
            "gpu_shr": q.add("gpu_shr", r"\GPU Adapter Memory(*)\Shared Usage"),
            "gpu_pmem": q.add("gpu_pmem", r"\GPU Process Memory(*)\Dedicated Usage"),
            "tz": q.add("tz", r"\Thermal Zone Information(*)\High Precision Temperature"),
        }
        q.collect()
        self._pdh = q
        sources.append({"name": "Windows performance counters",
                        "ok": added["cpu_util"] or added["gpu_eng"],
                        "detail": "CPU utilisation and clock per thread, GPU engine load, video memory"
                                  if added["cpu_util"] else "Counter sets not available on this system",
                        "provides": "CPU, GPU load, VRAM"})
        sources.append({"name": "CPU energy meter (RAPL)", "ok": added["energy"],
                        "detail": "Measured package and per-core power from the processor's own energy counters"
                                  if added["energy"] else "This processor/driver does not expose an energy meter",
                        "provides": "CPU power"})

        # Display adapters
        self._adapters = dxgi.enumerate_adapters()
        sources.append({"name": "DXGI adapter enumeration", "ok": bool(self._adapters),
                        "detail": f"{len(self._adapters)} hardware adapter(s) with true video-memory size",
                        "provides": "GPU identity, VRAM size"})

        # Vendor telemetry
        amd = [a for a in self._adapters if a.vendor == "AMD"]
        nv = [a for a in self._adapters if a.vendor == "NVIDIA"]
        if amd:
            self._adl = adl_mod.Adl()
            pool = list(self._adl.devices)
            for a in amd:
                needle = f"DEV_{a.device_id:04X}"
                match = next((d for d in pool if needle in d["pnp"]), None)
                if match:
                    pool.remove(match)
                    self._gpu_vendor_dev[a.index] = ("adl", match)
            sources.append({"name": "AMD ADL (Radeon driver telemetry)", "ok": self._adl.available,
                            "detail": "Temperatures, fan, clocks, voltage and measured board power"
                                      if self._adl.available else
                                      "Radeon driver library did not respond (driver install may be inconsistent)",
                            "provides": "GPU temperature, fan, power"})
        if nv:
            self._nvml = nvidia.Nvml()
            pool = list(self._nvml.devices)
            for a in nv:
                match = next((d for d in pool if d["device_id"] == a.device_id), pool[0] if pool else None)
                if match:
                    pool.remove(match)
                    self._gpu_vendor_dev[a.index] = ("nvml", match)
            sources.append({"name": "NVIDIA NVML", "ok": self._nvml.available,
                            "detail": "Temperature, fan, clocks and measured power"
                                      if self._nvml.available else "NVML did not initialise",
                            "provides": "GPU temperature, fan, power"})
        sources.append({"name": "Windows display kernel (D3DKMT)", "ok": bool(self._adapters),
                        "detail": "Vendor-neutral GPU temperature, the same reading Task Manager shows",
                        "provides": "GPU temperature (fallback)"})

        # Storage
        self._drives = storage.list_drives()
        sources.append({"name": "Storage device queries", "ok": bool(self._drives),
                        "detail": "Drive model, temperature and NVMe wear data read directly from each drive",
                        "provides": "Drive temperature, health"})
        sources.append({"name": "SMBIOS firmware table", "ok": True,
                        "detail": "Motherboard, BIOS and memory-module details",
                        "provides": "System inventory"})
        self._sources = sources

        # prime rate-based readers
        psutil.cpu_percent(percpu=True)
        self._net_prev = {k: (v.bytes_recv, v.bytes_sent) for k, v in psutil.net_io_counters(pernic=True).items()}
        self._net_session = {k: [0, 0] for k in self._net_prev}
        self._processes()       # first pass only records CPU-time baselines
        self._build_static()

    def _build_static(self) -> None:
        bios = smbios.read()
        cpu_id = winsys.cpu_identity()
        physical = psutil.cpu_count(logical=False) or self._ncpu
        gpus = []
        amd_drivers = adl_mod.amd_driver_entries() if any(a.vendor == "AMD" for a in self._adapters) else []
        for a in self._adapters:
            driver = {}
            if a.vendor == "AMD" and amd_drivers:
                d = amd_drivers[0]
                driver = {"version": d.get("software_version") or d.get("driver_version"),
                          "build": d.get("driver_version"), "date": d.get("driver_date")}
            elif a.vendor == "NVIDIA" and self._nvml:
                driver = {"version": self._nvml.driver_version()}
            kind, _dev = self._gpu_vendor_dev.get(a.index, (None, None))
            gpus.append({
                "index": a.index, "name": a.name, "vendor": a.vendor,
                "vram_bytes": a.dedicated_bytes, "shared_bytes": a.shared_bytes,
                "device_id": f"{a.vendor_id:04X}:{a.device_id:04X}",
                "driver": driver,
                "telemetry": {"adl": "AMD ADL", "nvml": "NVIDIA NVML"}.get(kind, "Windows display kernel"),
            })
        static = {
            "ready": True,
            "version": __version__,
            "os": winsys.os_info(),
            "cpu": {**cpu_id, "cores": physical, "threads": self._ncpu,
                    "socket": bios["cpu"].get("socket"),
                    "smt": self._ncpu > physical},
            "board": bios["board"], "bios": bios["bios"], "system": bios["system"],
            "memory": {"total_bytes": psutil.virtual_memory().total, **bios["memory"]},
            "gpus": gpus,
            "drives": self._drives,
            "sources": self._sources,
            "boot_time": psutil.boot_time(),
        }
        with self._lock:
            self._static = static

    # --------------------------------------------------------------- sample
    def _sample(self) -> None:
        mono = time.monotonic()
        dt = max(1e-3, mono - self._last_mono)
        self._last_mono = mono
        self._tick += 1
        slow = self._tick == 1 or (self._tick % max(1, round(5 / self.interval)) == 0)
        proc_due = self._tick == 1 or (self._tick % max(1, round(2 / self.interval)) == 0)

        if self._pdh:
            self._pdh.collect()
        self._bridge.poll()

        cpu = self._cpu()
        gpus = self._gpus()
        mem = self._memory()
        disks = self._storage(dt, slow)
        net = self._network(dt, slow)
        if proc_due:
            self._proc_rows = self._processes()

        cpu_w = cpu.get("power_w")
        gpu_w = [g.get("power_w") for g in gpus if g.get("power_w") is not None]
        power = {
            "cpu_w": cpu_w,
            "gpu_w": round(sum(gpu_w), 1) if gpu_w else None,
            "total_w": round((cpu_w or 0) + sum(gpu_w), 1) if (cpu_w is not None or gpu_w) else None,
            "scope": "CPU package + graphics card" if (cpu_w is not None and gpu_w) else
                     "CPU package" if cpu_w is not None else "Graphics card" if gpu_w else None,
        }
        battery = self._battery()

        snap = {
            "ready": True,
            "t": time.time(),
            "interval": self.interval,
            "cpu": cpu, "gpus": gpus, "memory": mem, "drives": disks["drives"],
            "volumes": disks["volumes"], "network": net, "power": power,
            "battery": battery,
            "processes": self._proc_rows,
            "bridge": {"connected": self._bridge.connected,
                       "sensors": self._bridge.readings if self._bridge.connected else []},
        }
        snap["alerts"] = self._alerts(snap)
        snap["status"] = ("critical" if any(a["level"] == "critical" for a in snap["alerts"]) else
                          "warning" if snap["alerts"] else "ok")
        readings = self._flatten(snap)

        with self._cond:
            self.stats.update(readings)
            self._append_history(snap)
            self._seq += 1
            snap["seq"] = self._seq
            self._snapshot = snap
            self._cond.notify_all()

    # ------------------------------------------------------------------ CPU
    def _cpu(self) -> dict:
        q = self._pdh
        per_thread: list[float] = []
        total = None
        if q and q.has("cpu_util"):
            util = q.array("cpu_util")
            total = util.get("_Total")
            cores = []
            for name, v in util.items():
                if "_Total" in name or "," not in name:
                    continue
                grp, num = name.split(",", 1)
                if grp.isdigit() and num.isdigit():
                    cores.append((int(grp), int(num), v))
            per_thread = [_clamp(v) for _g, _n, v in sorted(cores)]
        if total is None or not per_thread:
            per_thread = [float(v) for v in psutil.cpu_percent(percpu=True)]
            total = sum(per_thread) / len(per_thread) if per_thread else 0.0
            util_source = "psutil"
        else:
            util_source = "Windows performance counters"
        total = _clamp(total)

        freqs: list[float] = []
        if q and q.has("cpu_freq"):
            fmap = q.array("cpu_freq")
            items = []
            for name, v in fmap.items():
                if "_Total" in name or "," not in name:
                    continue
                grp, num = name.split(",", 1)
                if grp.isdigit() and num.isdigit():
                    items.append((int(grp), int(num), v))
            freqs = [v for _g, _n, v in sorted(items) if v > 0]
        freq_avg = sum(freqs) / len(freqs) if freqs else None
        freq_max = max(freqs) if freqs else None

        power_w = None
        core_power: list[float] = []
        if q and q.has("energy"):
            energy = q.array("energy")
            pkg = [v for name, v in energy.items() if name.upper().endswith("_PKG")]
            if pkg:
                power_w = sum(pkg) / 1000.0
            cores = []
            for name, v in energy.items():
                if name.upper().endswith("_CORE"):
                    m = _CORE_RE.search(name)
                    if m:
                        cores.append((int(m.group(1)), v / 1000.0))
            core_power = [round(v, 2) for _i, v in sorted(cores)]

        temp_c, temp_source, temp_reason = None, None, None
        if q and q.has("tz"):
            zones = [v / 10.0 - 273.15 for v in q.array("tz").values() if v > 0]
            zones = [z for z in zones if 0 < z < 130]
            if zones:
                temp_c, temp_source = max(zones), "ACPI thermal zone"
        if temp_c is None and self._bridge.connected:
            t = bridge.cpu_temperature(self._bridge.readings)
            if t is not None:
                temp_c, temp_source = t, "LibreHardwareMonitor"
        if temp_c is None:
            temp_reason = ("Windows keeps the processor's temperature sensor behind administrator "
                           "rights, and this version of RigWatch does not read it yet. "
                           "Built-in support is in progress.")

        perf = winsys.performance_info() or {}
        return {
            "util": round(total, 1),
            "util_source": util_source,
            "per_thread": [round(v, 1) for v in per_thread],
            "freq_mhz": _round(freq_avg, 0),
            "freq_max_mhz": _round(freq_max, 0),
            "per_thread_freq": [round(v) for v in freqs],
            "power_w": _round(power_w, 1),
            "per_core_power_w": core_power,
            "temp_c": _round(temp_c, 1),
            "temp_source": temp_source,
            "temp_reason": temp_reason,
            "processes": perf.get("processes"),
            "threads": perf.get("threads"),
            "handles": perf.get("handles"),
            "uptime_s": int(time.time() - psutil.boot_time()),
        }

    # ------------------------------------------------------------------ GPU
    def _gpus(self) -> list[dict]:
        q = self._pdh
        engine_util: dict[str, dict[tuple[str, str], float]] = {}
        pid_util: dict[int, dict[str, float]] = {}
        if q and q.has("gpu_eng"):
            for name, v in q.array("gpu_eng").items():
                m = _ENGINE_RE.match(name)
                if not m:
                    continue
                pid, luid, _phys, eng, etype = int(m.group(1)), m.group(2).lower(), m.group(3), m.group(4), m.group(5)
                per = engine_util.setdefault(luid, {})
                per[(eng, etype)] = per.get((eng, etype), 0.0) + v
                if v > 0:
                    pe = pid_util.setdefault(pid, {})
                    pe[eng] = pe.get(eng, 0.0) + v
        self._gpu_pid_util = {pid: _clamp(max(e.values())) for pid, e in pid_util.items() if e}

        pid_mem: dict[int, float] = {}
        if q and q.has("gpu_pmem"):
            for name, v in q.array("gpu_pmem").items():
                m = _PMEM_RE.match(name)
                if m and v > 0:
                    pid = int(m.group(1))
                    pid_mem[pid] = pid_mem.get(pid, 0.0) + v
        self._gpu_pid_mem = pid_mem

        ded = {k.lower(): v for k, v in q.array("gpu_ded").items()} if q and q.has("gpu_ded") else {}
        shr = {k.lower(): v for k, v in q.array("gpu_shr").items()} if q and q.has("gpu_shr") else {}

        out = []
        for a in self._adapters:
            luid = a.luid_key.replace("luid_", "").lower()
            engines = engine_util.get(luid, {})
            util = _clamp(max(engines.values())) if engines else None
            by_group: dict[str, float] = {}
            for (_eng, etype), v in engines.items():
                for prefix, group in _ENGINE_GROUPS:
                    if etype.lower().startswith(prefix.lower()):
                        by_group[group] = max(by_group.get(group, 0.0), _clamp(v))
                        break
            vram_used = next((v for k, v in ded.items() if luid in k), None)
            shared_used = next((v for k, v in shr.items() if luid in k), None)
            vram_total = a.dedicated_bytes or None

            g: dict = {
                "index": a.index, "name": a.name, "vendor": a.vendor,
                "util": _round(util, 1),
                "engines": [{"name": k, "util": round(v, 1)} for k, v in by_group.items()],
                "vram_used": int(vram_used) if vram_used is not None else None,
                "vram_total": vram_total,
                "vram_pct": round(vram_used / vram_total * 100, 1) if vram_used is not None and vram_total else None,
                "shared_used": int(shared_used) if shared_used is not None else None,
                "temp_c": None, "temp_hotspot_c": None, "temp_mem_c": None,
                "fan_rpm": None, "fan_pct": None, "power_w": None, "power_kind": None,
                "core_clock_mhz": None, "mem_clock_mhz": None, "voltage_mv": None,
                "activity_pct": None, "pcie_gen": None, "pcie_lanes": None, "power_limit_w": None,
                "temp_source": None,
            }
            kind, dev = self._gpu_vendor_dev.get(a.index, (None, None))
            s: dict = {}
            if kind == "adl" and self._adl:
                s = self._adl.read(dev["adapter_index"])
                g["temp_source"] = "AMD ADL"
            elif kind == "nvml" and self._nvml:
                s = self._nvml.read(dev)
                g["temp_source"] = "NVIDIA NVML"
            if s:
                g["temp_c"] = s.get("temp_edge_c") or s.get("temp_gfx_c")
                g["temp_hotspot_c"] = s.get("temp_hotspot_c")
                g["temp_mem_c"] = s.get("temp_mem_c")
                g["fan_rpm"] = s.get("fan_rpm")
                g["fan_pct"] = s.get("fan_pct")
                if s.get("board_power_w") is not None:
                    g["power_w"], g["power_kind"] = s["board_power_w"], "board"
                elif s.get("asic_power_w") is not None:
                    g["power_w"], g["power_kind"] = s["asic_power_w"], "chip"
                elif s.get("gfx_power_w") is not None:
                    g["power_w"], g["power_kind"] = s["gfx_power_w"], "chip"
                g["core_clock_mhz"] = s.get("core_clock_mhz")
                g["mem_clock_mhz"] = s.get("mem_clock_mhz")
                g["voltage_mv"] = s.get("core_voltage_mv")
                g["activity_pct"] = s.get("activity_pct")
                g["power_limit_w"] = s.get("power_limit_w")
                if s.get("bus_lanes"):
                    g["pcie_lanes"] = int(s["bus_lanes"])
                    g["pcie_gen"] = int(s["bus_speed"]) if s.get("bus_speed") else None
            if g["temp_c"] is None:
                k = d3dkmt.query_perf(a.luid_low, a.luid_high)
                if k:
                    g["temp_c"] = k.get("temp_c")
                    g["temp_source"] = "Windows display kernel" if g["temp_c"] is not None else None
                    if g["fan_rpm"] is None:
                        g["fan_rpm"] = k.get("fan_rpm")
            out.append(g)
        return out

    # --------------------------------------------------------------- memory
    def _memory(self) -> dict:
        vm = psutil.virtual_memory()
        perf = winsys.performance_info() or {}
        used = vm.total - vm.available
        return {
            "total": vm.total, "used": used, "available": vm.available,
            "pct": round(used / vm.total * 100, 1) if vm.total else None,
            "commit_used": perf.get("commit_used"), "commit_limit": perf.get("commit_limit"),
            "commit_pct": round(perf["commit_used"] / perf["commit_limit"] * 100, 1)
                          if perf.get("commit_limit") else None,
            "cached": perf.get("system_cache"),
            "kernel_paged": perf.get("kernel_paged"),
            "kernel_nonpaged": perf.get("kernel_nonpaged"),
        }

    # -------------------------------------------------------------- storage
    def _storage(self, dt: float, slow: bool) -> dict:
        if slow and self._tick % max(1, round(15 / self.interval)) == 0:
            fresh = storage.list_drives()
            if [d["index"] for d in fresh] != [d["index"] for d in self._drives]:
                self._drives = fresh
                self._build_static()
        if slow:
            for d in self._drives:
                try:
                    self._drive_health[d["index"]] = storage.read_health(d["index"])
                except Exception:
                    self._drive_health[d["index"]] = {}
            self._volumes = self._read_volumes()

        drives = []
        for d in self._drives:
            idx = d["index"]
            perf = winsys.disk_performance(idx)
            prev = self._disk_prev.get(idx)
            read_bps = write_bps = active = None
            if perf and prev:
                span = (perf["query_100ns"] - prev["query_100ns"]) / 1e7
                if span > 0:
                    read_bps = max(0.0, (perf["read_bytes"] - prev["read_bytes"]) / span)
                    write_bps = max(0.0, (perf["write_bytes"] - prev["write_bytes"]) / span)
                    idle = (perf["idle_100ns"] - prev["idle_100ns"]) / 1e7
                    active = _clamp(100.0 * (1.0 - idle / span))
            if perf:
                self._disk_prev[idx] = perf
            h = self._drive_health.get(idx, {})
            drives.append({
                "index": idx, "model": d.get("model"), "kind": d.get("kind"), "bus": d.get("bus"),
                "size_bytes": d.get("size_bytes"),
                "read_bps": _round(read_bps, 0), "write_bps": _round(write_bps, 0),
                "active_pct": _round(active, 1),
                "temp_c": h.get("temp_c"), "temp_warning_c": h.get("warning_c"),
                "temp_critical_c": h.get("critical_c"),
                "health_pct": h.get("health_pct"),
                "spare_pct": h.get("available_spare_pct"),
                "power_on_hours": h.get("power_on_hours"),
                "power_cycles": h.get("power_cycles"),
                "data_written_bytes": h.get("data_written_bytes"),
                "data_read_bytes": h.get("data_read_bytes"),
                "unsafe_shutdowns": h.get("unsafe_shutdowns"),
                "media_errors": h.get("media_errors"),
                "critical_warning": h.get("critical_warning"),
                "volumes": [v["mount"] for v in self._volumes if idx in v["disks"]],
            })
        return {"drives": drives, "volumes": self._volumes}

    def _read_volumes(self) -> list[dict]:
        vols = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            vols.append({
                "mount": part.device.rstrip("\\"), "fs": part.fstype,
                "total": usage.total, "used": usage.used, "free": usage.free,
                "pct": round(usage.used / usage.total * 100, 1) if usage.total else None,
                "disks": storage.volume_disks(part.device[:2]),
            })
        return vols

    # -------------------------------------------------------------- network
    def _network(self, dt: float, slow: bool) -> dict:
        if slow or not self._net_meta:
            stats = psutil.net_if_stats()
            addrs = psutil.net_if_addrs()
            meta = {}
            for name, st in stats.items():
                ipv4 = [a.address for a in addrs.get(name, []) if a.family == 2]
                ipv6 = [a.address.split("%")[0] for a in addrs.get(name, []) if a.family == 23]
                mac = next((a.address for a in addrs.get(name, []) if a.family == psutil.AF_LINK), None)
                meta[name] = {"up": st.isup, "speed_mbps": st.speed or None, "ipv4": ipv4,
                              "ipv6": ipv6[:2], "mac": mac}
            self._net_meta = meta
            primary_ip = winsys.primary_ipv4()
            self._net_primary = next((n for n, m in meta.items() if primary_ip and primary_ip in m["ipv4"]), None)

        counters = psutil.net_io_counters(pernic=True)
        adapters = []
        for name, c in counters.items():
            meta = self._net_meta.get(name, {})
            if "loopback" in name.lower() or any(ip.startswith("127.") for ip in meta.get("ipv4", [])):
                continue
            prev = self._net_prev.get(name)
            rx = tx = 0.0
            if prev:
                d_rx, d_tx = max(0, c.bytes_recv - prev[0]), max(0, c.bytes_sent - prev[1])
                rx, tx = d_rx / dt, d_tx / dt
                sess = self._net_session.setdefault(name, [0, 0])
                sess[0] += d_rx
                sess[1] += d_tx
            self._net_prev[name] = (c.bytes_recv, c.bytes_sent)
            if not meta.get("up") and not (rx or tx):
                continue
            sess = self._net_session.get(name, [0, 0])
            adapters.append({
                "name": name, "primary": name == self._net_primary,
                "rx_bps": round(rx), "tx_bps": round(tx),
                "rx_total": c.bytes_recv, "tx_total": c.bytes_sent,
                "rx_session": sess[0], "tx_session": sess[1],
                **{k: meta.get(k) for k in ("up", "speed_mbps", "ipv4", "ipv6", "mac")},
            })
        adapters.sort(key=lambda a: (not a["primary"], -(a["rx_total"] + a["tx_total"])))
        primary = next((a for a in adapters if a["primary"]), adapters[0] if adapters else None)
        return {
            "adapters": adapters,
            "primary": primary["name"] if primary else None,
            "rx_bps": primary["rx_bps"] if primary else None,
            "tx_bps": primary["tx_bps"] if primary else None,
        }

    # ------------------------------------------------------------ processes
    def _processes(self) -> list[dict]:
        """Apps grouped by executable name, from a single kernel query."""
        now = time.monotonic()
        procs = winsys.process_snapshot()
        prev, prev_at = self._proc_prev, self._proc_prev_at
        span = (now - prev_at) if prev_at else 0.0
        current: dict[int, tuple[int, int]] = {}
        groups: dict[str, dict] = {}
        for p in procs:
            pid = p["pid"]
            current[pid] = (p["create_time"], p["cpu_100ns"])
            if pid == 0:
                continue                      # the idle "process" is not an app
            name = p["name"] or "System process"
            g = groups.get(name)
            if g is None:
                g = groups[name] = {"name": name, "count": 0, "cpu": 0.0, "mem": 0,
                                    "gpu": 0.0, "vram": 0.0, "threads": 0}
            g["count"] += 1
            before = prev.get(pid)
            if before and before[0] == p["create_time"] and span > 0:   # same process, not a reused pid
                g["cpu"] += max(0, p["cpu_100ns"] - before[1]) / 1e7 / span / self._ncpu * 100.0
            g["mem"] += p["private_ws"]
            g["threads"] += p["threads"]
            g["gpu"] += self._gpu_pid_util.get(pid, 0.0)
            g["vram"] += self._gpu_pid_mem.get(pid, 0.0)
        self._proc_prev, self._proc_prev_at = current, now

        total_mem = psutil.virtual_memory().total or 1
        rows = []
        for g in groups.values():
            g["cpu"] = round(min(100.0, g["cpu"]), 1)
            g["gpu"] = round(min(100.0, g["gpu"]), 1)
            g["vram"] = int(g["vram"])
            score = g["cpu"] * 4 + g["mem"] / total_mem * 100 + g["gpu"] * 2 + g["vram"] / 2 ** 30
            rows.append((score, g))
        rows.sort(key=lambda r: -r[0])
        return [g for _s, g in rows[:80]]

    def _battery(self) -> dict | None:
        try:
            b = psutil.sensors_battery()
        except Exception:
            b = None
        if b is None:
            return None
        secs = b.secsleft if b.secsleft and b.secsleft > 0 else None
        return {"pct": round(b.percent, 0), "plugged": bool(b.power_plugged), "secs_left": secs}

    # --------------------------------------------------------------- alerts
    def _alerts(self, s: dict) -> list[dict]:
        alerts: list[dict] = []

        def add(level, component, message):
            alerts.append({"level": level, "component": component, "message": message})

        t = s["cpu"].get("temp_c")
        if t is not None:
            if t >= 95:
                add("critical", "cpu", f"CPU is at {t:.0f} °C, at its thermal limit")
            elif t >= 85:
                add("warning", "cpu", f"CPU is running hot at {t:.0f} °C")
        for g in s["gpus"]:
            t, hot = g.get("temp_c"), g.get("temp_hotspot_c")
            if (t is not None and t >= 95) or (hot is not None and hot >= 108):
                add("critical", "gpu", f"{g['name']} is at its thermal limit ({(hot or t):.0f} °C)")
            elif (t is not None and t >= 85) or (hot is not None and hot >= 98):
                add("warning", "gpu", f"{g['name']} is running hot ({(t or hot):.0f} °C)")
            if g.get("vram_pct") is not None and g["vram_pct"] >= 97:
                add("warning", "gpu", f"Video memory is almost full ({g['vram_pct']:.0f}%)")
        m = s["memory"]
        if m.get("pct") is not None:
            if m["pct"] >= 95:
                add("critical", "memory", f"Memory is almost exhausted ({m['pct']:.0f}% in use)")
            elif m["pct"] >= 88:
                add("warning", "memory", f"Memory use is high ({m['pct']:.0f}%)")
        for d in s["drives"]:
            name = d.get("model") or f"Drive {d['index']}"
            t = d.get("temp_c")
            crit = d.get("temp_critical_c") or 80
            warn = d.get("temp_warning_c") or 70
            if t is not None and t >= crit:
                add("critical", "storage", f"{name} is overheating ({t:.0f} °C)")
            elif t is not None and t >= warn:
                add("warning", "storage", f"{name} is running hot ({t:.0f} °C)")
            if d.get("critical_warning"):
                add("critical", "storage", f"{name} reports a drive health warning")
            elif d.get("health_pct") is not None and d["health_pct"] <= 10:
                add("critical", "storage", f"{name} has {d['health_pct']}% of its rated life left")
            elif d.get("health_pct") is not None and d["health_pct"] <= 30:
                add("warning", "storage", f"{name} has {d['health_pct']}% of its rated life left")
            if d.get("media_errors"):
                add("warning", "storage", f"{name} has logged {d['media_errors']} media error(s)")
        for v in s["volumes"]:
            if v["total"] and v["free"] / v["total"] < 0.05:
                add("warning", "storage", f"{v['mount']} is almost full ({v['pct']:.0f}% used)")
        b = s.get("battery")
        if b and not b["plugged"] and b["pct"] <= 10:
            add("warning", "power", f"Battery is low ({b['pct']:.0f}%)")
        alerts.sort(key=lambda a: a["level"] != "critical")
        return alerts

    # ---------------------------------------------------- flat sensor table
    def _flatten(self, s: dict) -> list[dict]:
        rows: list[dict] = []

        def add(sid, group, label, value, unit, kind, source):
            rows.append({"id": sid, "group": group, "label": label,
                         "value": None if value is None else round(float(value), 2),
                         "unit": unit, "kind": kind, "source": source})

        c = s["cpu"]
        add("cpu.util", "Processor", "Utilisation", c["util"], "%", "usage", c["util_source"])
        add("cpu.freq", "Processor", "Clock (average)", c["freq_mhz"], "MHz", "clock", "Windows performance counters")
        add("cpu.freq_max", "Processor", "Clock (fastest thread)", c["freq_max_mhz"], "MHz", "clock",
            "Windows performance counters")
        add("cpu.power", "Processor", "Package power", c["power_w"], "W", "power", "CPU energy meter (RAPL)")
        add("cpu.temp", "Processor", "Temperature", c["temp_c"], "°C", "temp", c["temp_source"] or "Not available")
        for i, v in enumerate(c["per_core_power_w"]):
            add(f"cpu.core{i}.power", "Processor", f"Core {i} power", v, "W", "power", "CPU energy meter (RAPL)")
        for i, v in enumerate(c["per_thread"]):
            add(f"cpu.t{i}.util", "Processor threads", f"Thread {i} utilisation", v, "%", "usage", c["util_source"])
        for g in s["gpus"]:
            p = f"gpu{g['index']}"
            grp = g["name"]
            src = g.get("temp_source") or "Windows performance counters"
            add(f"{p}.util", grp, "Utilisation", g["util"], "%", "usage", "Windows performance counters")
            add(f"{p}.activity", grp, "Driver-reported activity", g["activity_pct"], "%", "usage", src)
            add(f"{p}.temp", grp, "Temperature", g["temp_c"], "°C", "temp", src)
            add(f"{p}.hotspot", grp, "Hotspot temperature", g["temp_hotspot_c"], "°C", "temp", src)
            add(f"{p}.temp_mem", grp, "Memory temperature", g["temp_mem_c"], "°C", "temp", src)
            add(f"{p}.fan", grp, "Fan speed", g["fan_rpm"], "RPM", "fan", src)
            add(f"{p}.fan_pct", grp, "Fan duty", g["fan_pct"], "%", "fan", src)
            add(f"{p}.power", grp, "Board power" if g["power_kind"] == "board" else "Chip power",
                g["power_w"], "W", "power", src)
            add(f"{p}.core_clock", grp, "Core clock", g["core_clock_mhz"], "MHz", "clock", src)
            add(f"{p}.mem_clock", grp, "Memory clock", g["mem_clock_mhz"], "MHz", "clock", src)
            add(f"{p}.voltage", grp, "Core voltage", g["voltage_mv"], "mV", "voltage", src)
            add(f"{p}.vram", grp, "Video memory in use", (g["vram_used"] or 0) / 2 ** 20 if g["vram_used"] is not None else None,
                "MB", "data", "Windows performance counters")
            add(f"{p}.vram_pct", grp, "Video memory in use (share)", g["vram_pct"], "%", "usage",
                "Windows performance counters")
            add(f"{p}.pcie_lanes", grp, "PCIe link width", g["pcie_lanes"], "lanes", "info", src)
            for e in g["engines"]:
                add(f"{p}.eng.{e['name']}", grp, f"{e['name']} engine", e["util"], "%", "usage",
                    "Windows performance counters")
        m = s["memory"]
        add("mem.pct", "Memory", "In use", m["pct"], "%", "usage", "Windows")
        add("mem.used", "Memory", "In use (size)", m["used"] / 2 ** 30, "GB", "data", "Windows")
        add("mem.available", "Memory", "Available", m["available"] / 2 ** 30, "GB", "data", "Windows")
        add("mem.commit", "Memory", "Committed", (m["commit_used"] or 0) / 2 ** 30 if m["commit_used"] else None,
            "GB", "data", "Windows")
        for d in s["drives"]:
            p = f"disk{d['index']}"
            grp = d.get("model") or f"Drive {d['index']}"
            add(f"{p}.temp", grp, "Temperature", d["temp_c"], "°C", "temp", "Storage device query")
            add(f"{p}.health", grp, "Remaining life", d["health_pct"], "%", "usage", "NVMe health log")
            add(f"{p}.active", grp, "Active time", d["active_pct"], "%", "usage", "Windows disk statistics")
            add(f"{p}.read", grp, "Read rate", (d["read_bps"] or 0) / 2 ** 20 if d["read_bps"] is not None else None,
                "MB/s", "rate", "Windows disk statistics")
            add(f"{p}.write", grp, "Write rate", (d["write_bps"] or 0) / 2 ** 20 if d["write_bps"] is not None else None,
                "MB/s", "rate", "Windows disk statistics")
        for a in s["network"]["adapters"]:
            p = f"net.{a['name']}"
            grp = f"Network · {a['name']}"
            add(f"{p}.rx", grp, "Download rate", a["rx_bps"] * 8 / 1e6, "Mbit/s", "rate", "Windows network statistics")
            add(f"{p}.tx", grp, "Upload rate", a["tx_bps"] * 8 / 1e6, "Mbit/s", "rate", "Windows network statistics")
        pw = s["power"]
        add("power.total", "Power", f"Measured draw ({pw['scope']})" if pw["scope"] else "Measured draw",
            pw["total_w"], "W", "power", "CPU energy meter + GPU telemetry")
        for i, r in enumerate(s["bridge"]["sensors"]):
            add(f"lhm.{r['hardware']}.{r['kind']}.{r['label']}", f"{r['hardware']} (LibreHardwareMonitor)",
                r["label"], r["value"], r["unit"], r["kind"], "LibreHardwareMonitor")
        return rows

    # -------------------------------------------------------------- history
    def _append_history(self, s: dict) -> None:
        values: dict[str, float | None] = {
            "cpu.util": s["cpu"]["util"], "cpu.power": s["cpu"]["power_w"],
            "cpu.freq": s["cpu"]["freq_mhz"], "cpu.temp": s["cpu"]["temp_c"],
            "mem.pct": s["memory"]["pct"],
            "net.rx": s["network"]["rx_bps"], "net.tx": s["network"]["tx_bps"],
            "power.total": s["power"]["total_w"],
        }
        for g in s["gpus"]:
            p = f"gpu{g['index']}"
            values.update({f"{p}.util": g["util"], f"{p}.temp": g["temp_c"],
                           f"{p}.hotspot": g["temp_hotspot_c"], f"{p}.temp_mem": g["temp_mem_c"],
                           f"{p}.power": g["power_w"], f"{p}.vram_pct": g["vram_pct"],
                           f"{p}.fan": g["fan_rpm"], f"{p}.core_clock": g["core_clock_mhz"]})
        for d in s["drives"]:
            p = f"disk{d['index']}"
            values.update({f"{p}.read": d["read_bps"], f"{p}.write": d["write_bps"],
                           f"{p}.active": d["active_pct"], f"{p}.temp": d["temp_c"]})
        n = len(self._history_t)
        self._history_t.append(round(s["t"], 2))
        for key, value in values.items():
            series = self._history.get(key)
            if series is None:
                series = self._history[key] = deque([None] * n, maxlen=HISTORY_POINTS)
            series.append(value)
        for key, series in self._history.items():
            if key not in values:
                series.append(None)
        s["hist"] = values      # lets clients extend their own history without re-deriving keys
