"""Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v"""

import json
import time
import unittest

from rigwatch import strip
from rigwatch.collector import Collector, SensorStats
from rigwatch.sensors import bridge, smbios


# Shape of LibreHardwareMonitor's /data.json (trimmed).
LHM_SAMPLE = {
    "id": 0, "Text": "Sensor", "Min": "Min", "Value": "Value", "Max": "Max", "ImageURL": "", "Children": [{
        "id": 1, "Text": "DESKTOP", "Min": "", "Value": "", "Max": "", "ImageURL": "images_icon/computer.png", "Children": [
            {"id": 2, "Text": "AMD Ryzen 5 7500F", "ImageURL": "images_icon/cpu.png", "Children": [
                {"id": 3, "Text": "Temperatures", "ImageURL": "images_icon/temperature.png", "Children": [
                    {"id": 4, "Text": "Core (Tctl/Tdie)", "Min": "41,0 °C", "Value": "52,5 °C", "Max": "78,1 °C",
                     "Type": "Temperature", "ImageURL": "images/transparent.png", "Children": []},
                    {"id": 5, "Text": "CCD1 (Tdie)", "Min": "39.0 °C", "Value": "47.0 °C", "Max": "70.0 °C",
                     "Type": "Temperature", "ImageURL": "images/transparent.png", "Children": []}]},
                {"id": 6, "Text": "Powers", "ImageURL": "images_icon/power.png", "Children": [
                    {"id": 7, "Text": "Package", "Min": "20.1 W", "Value": "36.4 W", "Max": "88.0 W",
                     "ImageURL": "images/transparent.png", "Children": []}]}]},
            {"id": 8, "Text": "MSI B650 GAMING PLUS WIFI", "ImageURL": "images_icon/mainboard.png", "Children": [
                {"id": 9, "Text": "Nuvoton NCT6687D", "ImageURL": "images_icon/chip.png", "Children": [
                    {"id": 10, "Text": "Fans", "ImageURL": "images_icon/fan.png", "Children": [
                        {"id": 11, "Text": "CPU Fan", "Min": "600 RPM", "Value": "812 RPM", "Max": "1500 RPM",
                         "Type": "Fan", "ImageURL": "images/transparent.png", "Children": []}]}]}]},
        ]}],
}


class BridgeTests(unittest.TestCase):
    def test_parse_flattens_sensor_tree(self):
        readings = bridge.parse(LHM_SAMPLE)
        by_label = {r["label"]: r for r in readings}
        self.assertEqual(len(readings), 4)
        self.assertEqual(by_label["Core (Tctl/Tdie)"]["value"], 52.5)      # comma decimal handled
        self.assertEqual(by_label["Core (Tctl/Tdie)"]["kind"], "temp")
        self.assertEqual(by_label["Package"]["kind"], "power")             # kind from the group name
        self.assertEqual(by_label["CPU Fan"]["hardware"], "Nuvoton NCT6687D")
        self.assertEqual(by_label["CPU Fan"]["unit"], "RPM")

    def test_cpu_temperature_prefers_package_sensor(self):
        self.assertEqual(bridge.cpu_temperature(bridge.parse(LHM_SAMPLE)), 52.5)

    def test_no_cpu_means_none(self):
        self.assertIsNone(bridge.cpu_temperature([]))


class StatsTests(unittest.TestCase):
    def test_min_max_average(self):
        stats = SensorStats()
        for v in (10.0, 30.0, 20.0, None):
            stats.update([{"id": "a", "group": "g", "label": "A", "unit": "W", "kind": "power", "source": "s", "value": v}])
        row = stats.table()[0]
        self.assertEqual((row["min"], row["max"], row["avg"]), (10.0, 30.0, 20.0))
        self.assertIsNone(row["value"])           # last sample was unavailable, and says so


class StripTests(unittest.TestCase):
    """The taskbar strip's contents are plain data, so they can be checked without a window."""

    SNAP = {"cpu": {"util": 8.4, "temp_c": None}, "memory": {"pct": 49.2},
            "gpus": [{"name": "AMD Radeon RX 7800 XT", "util": 32.0, "temp_c": 61.4},
                     {"name": "AMD Radeon RX 6650 XT", "util": None, "temp_c": None}],
            "power": {"total_w": 262.3}}

    def test_one_column_per_reading(self):
        fields = strip.build_fields(self.SNAP)
        self.assertEqual([f["label"] for f in fields], ["CPU", "RAM", "7800", "6650", "WATT"])
        self.assertEqual([f["value"] for f in fields], ["8%", "49%", "32%", None, "262W"])
        self.assertEqual(fields[2]["extra"], "61°")          # GPU temperature rides along
        self.assertIsNone(fields[0]["extra"])                # no CPU temperature -> nothing invented

    def test_fahrenheit(self):
        self.assertEqual(strip.build_fields(self.SNAP, "F")[2]["extra"], "143°")

    def test_missing_power_is_not_available(self):
        snap = dict(self.SNAP, power={"total_w": None})
        self.assertIsNone(strip.build_fields(snap)[-1]["value"])

    def test_gpu_labels(self):
        self.assertEqual(strip.short_gpu_label("NVIDIA GeForce RTX 4070 Ti", 0), "4070")
        self.assertEqual(strip.short_gpu_label("Intel(R) UHD Graphics", 1), "GPU2")


class LiveSmokeTests(unittest.TestCase):
    """Reads the real machine: values must be present, plausible, and never invented."""

    @classmethod
    def setUpClass(cls):
        cls.c = Collector(0.5)
        cls.c.start()
        deadline = time.time() + 20
        while time.time() < deadline and cls.c.snapshot().get("seq", 0) < 3:
            time.sleep(0.1)
        cls.snap, cls.static = cls.c.snapshot(), cls.c.static()

    @classmethod
    def tearDownClass(cls):
        cls.c.stop()

    def test_snapshot_is_ready_and_serialisable(self):
        self.assertTrue(self.snap.get("ready"), self.snap.get("error"))
        self.assertNotIn("error", self.snap)
        json.dumps(self.snap)

    def test_cpu_values_are_plausible(self):
        cpu = self.snap["cpu"]
        self.assertTrue(0 <= cpu["util"] <= 100)
        self.assertEqual(len(cpu["per_thread"]), self.static["cpu"]["threads"])
        if cpu["power_w"] is not None:
            self.assertTrue(0 < cpu["power_w"] < 1000)
        if cpu["temp_c"] is None:                 # unavailable must come with an explanation
            self.assertTrue(cpu["temp_reason"])

    def test_memory_adds_up(self):
        m = self.snap["memory"]
        self.assertEqual(m["used"] + m["available"], m["total"])

    def test_gpu_memory_within_capacity(self):
        for g in self.snap["gpus"]:
            if g["vram_used"] is not None and g["vram_total"]:
                self.assertLessEqual(g["vram_used"], g["vram_total"] * 1.05)
            if g["temp_c"] is not None:
                self.assertTrue(0 < g["temp_c"] < 130)

    def test_smbios_reports_memory_consistently(self):
        info = smbios.read()
        total = sum(m["size_bytes"] for m in info["memory"]["modules"])
        if total:                                  # firmware-reported size is within 10% of what Windows sees
            self.assertAlmostEqual(total / self.static["memory"]["total_bytes"], 1.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
