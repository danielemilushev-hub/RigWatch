"""Optional bridge to LibreHardwareMonitor for motherboard-level sensors.

CPU core temperature, case-fan RPM and rail voltages can only be read through
a kernel driver. RigWatch deliberately does not ship one. If the user already
runs LibreHardwareMonitor with its "Remote Web Server" enabled, its sensors
are picked up automatically from ``http://127.0.0.1:8085/data.json``.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8085/data.json"
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
_GROUP_KIND = {
    "temperatures": "temp", "fans": "fan", "voltages": "voltage", "powers": "power",
    "clocks": "clock", "load": "usage", "controls": "control", "currents": "current",
}
_KIND_UNIT = {"temp": "°C", "fan": "RPM", "voltage": "V", "power": "W", "clock": "MHz",
              "usage": "%", "control": "%", "current": "A"}
_TYPE_KIND = {"temperature": "temp", "fan": "fan", "voltage": "voltage", "power": "power",
              "clock": "clock", "load": "usage", "control": "control", "current": "current"}
_CPU_TEMP_PRIORITY = ["core (tctl/tdie)", "cpu package", "package", "core max", "core average", "cpu"]


def _number(text) -> float | None:
    if not isinstance(text, str):
        return None
    m = _NUMBER.search(text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def parse(tree: dict) -> list[dict]:
    """Flatten LibreHardwareMonitor's sensor tree into a list of readings."""
    readings: list[dict] = []

    def walk(node: dict, hardware: str | None, hw_image: str, kind: str | None):
        children = node.get("Children") or []
        text = str(node.get("Text", "")).strip()
        image = str(node.get("ImageURL", "")).lower()
        if not children:
            sensor_kind = _TYPE_KIND.get(str(node.get("Type", "")).lower(), kind)
            value = _number(node.get("Value"))
            if hardware and sensor_kind and value is not None:
                readings.append({
                    "hardware": hardware,
                    "is_cpu": "cpu" in hw_image,
                    "kind": sensor_kind,
                    "label": text,
                    "value": value,
                    "unit": _KIND_UNIT.get(sensor_kind, ""),
                })
            return
        group_kind = _GROUP_KIND.get(text.lower())
        if group_kind:
            for child in children:
                walk(child, hardware, hw_image, group_kind)
        else:
            # A hardware node (or the computer / root node above it).
            for child in children:
                walk(child, text or hardware, image or hw_image, None)

    walk(tree, None, "", None)
    return readings


def cpu_temperature(readings: list[dict]) -> float | None:
    temps = [r for r in readings if r["is_cpu"] and r["kind"] == "temp"]
    if not temps:
        return None
    by_label = {r["label"].lower(): r["value"] for r in temps}
    for label in _CPU_TEMP_PRIORITY:
        if label in by_label:
            return by_label[label]
    return max(r["value"] for r in temps)


class LhmBridge:
    """Polls the LibreHardwareMonitor web server when it is reachable."""

    def __init__(self, url: str = DEFAULT_URL) -> None:
        self.url = url
        self.connected = False
        self.readings: list[dict] = []
        self._next_try = 0.0

    def poll(self) -> None:
        now = time.monotonic()
        if now < self._next_try:
            return
        try:
            req = urllib.request.Request(self.url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=0.35) as resp:
                tree = json.loads(resp.read().decode("utf-8", "replace"))
            self.readings = parse(tree)
            self.connected = bool(self.readings)
            self._next_try = now + 1.5
        except Exception:
            self.connected = False
            self.readings = []
            self._next_try = now + 20.0   # not running: look again later, cheaply
