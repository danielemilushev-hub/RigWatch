"""NVIDIA telemetry through NVML (the library behind ``nvidia-smi``).

Uses the official ``nvidia-ml-py`` bindings. Every read is individually
guarded because NVML reports "not supported" per sensor on many cards.
"""

from __future__ import annotations


class Nvml:
    def __init__(self) -> None:
        self.available = False
        self.devices: list[dict] = []
        self._n = None
        try:
            import pynvml as n
            n.nvmlInit()
            self._n = n
            for i in range(n.nvmlDeviceGetCount()):
                handle = n.nvmlDeviceGetHandleByIndex(i)
                name = n.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                device_id = None
                bus = None
                try:
                    pci = n.nvmlDeviceGetPciInfo(handle)
                    device_id = (pci.pciDeviceId >> 16) & 0xFFFF
                    bus = pci.bus
                except Exception:
                    pass
                self.devices.append({"handle": handle, "name": name, "device_id": device_id, "bus": bus})
            self.available = bool(self.devices)
        except Exception:
            self.available = False

    def _try(self, fn, *args):
        try:
            return fn(*args)
        except Exception:
            return None

    def read(self, device: dict) -> dict[str, float]:
        n, h = self._n, device["handle"]
        out: dict[str, float] = {}
        if n is None:
            return out
        temp = self._try(n.nvmlDeviceGetTemperature, h, n.NVML_TEMPERATURE_GPU)
        if temp is not None:
            out["temp_edge_c"] = float(temp)
        fan = self._try(n.nvmlDeviceGetFanSpeed, h)
        if fan is not None:
            out["fan_pct"] = float(fan)
        power = self._try(n.nvmlDeviceGetPowerUsage, h)
        if power is not None:
            out["board_power_w"] = power / 1000.0
        limit = self._try(n.nvmlDeviceGetEnforcedPowerLimit, h)
        if limit is not None:
            out["power_limit_w"] = limit / 1000.0
        util = self._try(n.nvmlDeviceGetUtilizationRates, h)
        if util is not None:
            out["activity_pct"] = float(util.gpu)
            out["mem_activity_pct"] = float(util.memory)
        core = self._try(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_GRAPHICS)
        if core is not None:
            out["core_clock_mhz"] = float(core)
        mem = self._try(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_MEM)
        if mem is not None:
            out["mem_clock_mhz"] = float(mem)
        return out

    def driver_version(self) -> str | None:
        if self._n is None:
            return None
        v = self._try(self._n.nvmlSystemGetDriverVersion)
        return v.decode() if isinstance(v, bytes) else v

    def close(self) -> None:
        if self._n is not None:
            self._try(self._n.nvmlShutdown)
            self._n = None
