"""Sensor helper: the elevated service, its protocol, and the app-side client.

Nothing here needs administrator rights or touches the scheduled task: the
engine and the helper process are replaced by stand-ins where a test needs them.

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from rigwatch.collector import Collector
from rigwatch.sensors import bridge, helper, helper_client


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class FakeSensor:
    def __init__(self, kind, name, value, ident):
        self.SensorType, self.Name, self.Value, self.Identifier = kind, name, value, ident


class FakeHardware:
    def __init__(self, hw_type, name, sensors, sub=()):
        self.HardwareType, self.Name, self.Sensors, self.SubHardware = hw_type, name, sensors, list(sub)
        self.updates = 0

    def Update(self):
        self.updates += 1


def walk(hardware, is_cpu, engine=None):
    engine = engine or helper.Engine.__new__(helper.Engine)     # skip loading the real .NET engine
    if not hasattr(engine, "_fans_seen"):
        engine._fans_seen = set()
    out = []
    engine._walk(hardware, is_cpu, out)
    return out, engine


READINGS = [
    {"id": "/amdcpu/0/temperature/2", "hardware": "AMD Ryzen 5 7500F", "is_cpu": True, "kind": "temp",
     "label": "Core (Tctl/Tdie)", "value": 51.25, "unit": "°C"},
    {"id": "/amdcpu/0/temperature/3", "hardware": "AMD Ryzen 5 7500F", "is_cpu": True, "kind": "temp",
     "label": "CCD1 (Tdie)", "value": 44.0, "unit": "°C"},
    {"id": "/lpc/nct6687d/0/fan/0", "hardware": "Nuvoton NCT6687D", "is_cpu": False, "kind": "fan",
     "label": "CPU Fan", "value": 812.0, "unit": "RPM"},
]


class EngineFilterTests(unittest.TestCase):
    def test_failed_reads_are_never_reported(self):
        # Without the driver every register reads as zero. That is not a measurement.
        self.assertFalse(helper.plausible("temp", 0.0))
        self.assertFalse(helper.plausible("temp", -12.0))
        self.assertFalse(helper.plausible("temp", 255.0))
        self.assertFalse(helper.plausible("voltage", 0.0))
        self.assertFalse(helper.plausible("temp", float("nan")))
        self.assertTrue(helper.plausible("temp", 51.3))
        self.assertTrue(helper.plausible("fan", 0.0))               # a stopped fan is a real reading

    def test_cpu_keeps_only_what_rigwatch_cannot_measure_itself(self):
        cpu = FakeHardware("Cpu", "AMD Ryzen 5 7500F", [
            FakeSensor("Temperature", "Core (Tctl/Tdie)", 51.25, "/amdcpu/0/temperature/2"),
            FakeSensor("Temperature", "Core (Tctl/Tdie)", 0.0, "/amdcpu/0/temperature/9"),      # failed read
            FakeSensor("Temperature", "CPU Core #1 Distance to TjMax", 48.0, "/intelcpu/0/temperature/7"),
            FakeSensor("Load", "CPU Total", 12.0, "/amdcpu/0/load/0"),
            FakeSensor("Power", "Package", 36.0, "/amdcpu/0/power/0"),
            FakeSensor("Voltage", "Core #1 VID", 1.1, "/amdcpu/0/voltage/1"),
            FakeSensor("Temperature", "Unread", None, "/amdcpu/0/temperature/8"),
        ])
        out, _ = walk(cpu, True)
        self.assertEqual([(r["kind"], r["label"], r["value"]) for r in out],
                         [("temp", "Core (Tctl/Tdie)", 51.25), ("voltage", "Core #1 VID", 1.1)])
        self.assertTrue(all(r["is_cpu"] for r in out))
        self.assertEqual(cpu.updates, 1)

    def test_empty_fan_headers_stay_out_until_a_fan_has_spun(self):
        chip = FakeHardware("SuperIO", "Nuvoton NCT6687D", [
            FakeSensor("Fan", "Pump Fan", 0.0, "/lpc/nct6687d/0/fan/1"),
            FakeSensor("Fan", "CPU Fan", 812.0, "/lpc/nct6687d/0/fan/0"),
        ])
        board = FakeHardware("Motherboard", "MSI B650", [], sub=[chip])
        out, engine = walk(board, False)
        self.assertEqual([r["label"] for r in out], ["CPU Fan"])
        self.assertEqual(out[0]["hardware"], "Nuvoton NCT6687D")
        chip.Sensors[1].Value = 0.0                                  # the fan that was spinning stops
        out, _ = walk(board, False, engine)
        self.assertEqual([(r["label"], r["value"]) for r in out], [("CPU Fan", 0.0)])


class ServiceTests(unittest.TestCase):
    class FakeEngine:
        version = "9.9.9"

        def __init__(self, lib_dir):
            pass

        def read(self):
            return READINGS

        def close(self):
            pass

    def run_service(self, driver):
        with mock.patch.object(helper, "driver_state", return_value=driver), \
                mock.patch.object(helper, "Engine", self.FakeEngine), \
                mock.patch.object(helper, "SAMPLE_S", 0.02):
            service = helper.Service()
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            try:
                wait_for(lambda: service.payload()["state"] != "starting", 5)
                return service.payload()
            finally:
                service.stop()
                thread.join(timeout=5)

    def test_reports_readings_once_the_driver_opens(self):
        payload = self.run_service("ok")
        self.assertEqual((payload["state"], payload["engine"], payload["app"]), ("ok", "9.9.9", helper.APP_ID))
        self.assertEqual(payload["readings"], READINGS)

    def test_no_driver_means_no_readings(self):
        for driver, state in (("missing", "no_driver"), ("denied", "not_elevated")):
            payload = self.run_service(driver)
            self.assertEqual(payload["state"], state)
            self.assertEqual(payload["readings"], [])

    def test_web_pages_are_refused(self):
        service = helper.Service()
        handler = type("Handler", (helper._Handler,), {"service": service})
        server = helper._Server(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertEqual(helper.fetch(port)["app"], helper.APP_ID)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/readings", headers={"Origin": "https://example.com"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(ctx.exception.code, 403)
            ctx.exception.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_only_the_app_keeps_the_helper_alive(self):
        # Another program polling the port must not keep an elevated process running.
        service = helper.Service()
        handler = type("Handler", (helper._Handler,), {"service": service})
        server = helper._Server(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            service._last_request = 0.0
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/readings", timeout=2) as resp:
                self.assertEqual(json.loads(resp.read())["app"], helper.APP_ID)     # answered...
            self.assertEqual(service._last_request, 0.0)                            # ...but not counted
            helper.fetch(port)
            self.assertGreater(service._last_request, 0.0)
        finally:
            server.shutdown()
            server.server_close()


class TaskTests(unittest.TestCase):
    def test_task_runs_elevated_on_demand_only(self):
        xml = helper.task_xml("S-1-5-21-1-2-3-1001", r"C:\Apps\My RigWatch\RigWatch.exe", ["--sensor-helper", "serve"])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ET.fromstring(xml.split("?>", 1)[1])
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", ns).text, "HighestAvailable")
        self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", ns).text, "S-1-5-21-1-2-3-1001")
        self.assertEqual(len(root.find("t:Triggers", ns)), 0)        # never starts by itself
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", ns).text, r"C:\Apps\My RigWatch\RigWatch.exe")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", ns).text, "--sensor-helper serve")

    def test_install_refuses_without_elevation(self):
        with mock.patch.object(helper, "is_elevated", return_value=False):
            self.assertEqual(helper.install("S-1-5-21-1"), helper.EXIT_NOT_ELEVATED)
            self.assertEqual(helper.uninstall(), helper.EXIT_NOT_ELEVATED)

    def test_install_refuses_another_accounts_permission(self):
        with mock.patch.object(helper, "is_elevated", return_value=True), \
                mock.patch.object(helper, "current_sid", return_value="S-1-5-21-ADMIN"), \
                mock.patch.object(helper.subprocess, "run") as run:
            self.assertEqual(helper.install("S-1-5-21-USER"), helper.EXIT_OTHER_ACCOUNT)
            run.assert_not_called()


class StateTests(unittest.TestCase):
    def test_states(self):
        d = helper_client.derive_state
        self.assertEqual(d(None, False, None, None), "off")
        self.assertEqual(d(None, True, None, None), "starting")
        self.assertEqual(d(None, True, 3.0, None), "starting")
        self.assertEqual(d(None, True, 60.0, None), "not_responding")
        self.assertEqual(d({"state": "ok"}, True, 60.0, None), "on")
        self.assertEqual(d({"state": "no_driver"}, True, 1.0, None), "no_driver")
        self.assertEqual(d({"state": "not_elevated"}, False, None, None), "not_elevated")
        self.assertEqual(d({"state": "something new"}, True, 1.0, None), "error")
        self.assertEqual(d({"state": "ok"}, True, 1.0, "disabling"), "disabling")


class FakeHelper:
    """Stands in for the elevated helper process: same protocol, canned readings."""

    def __init__(self, state="ok", readings=READINGS):
        self.payload = {"app": helper.APP_ID, "protocol": helper.PROTOCOL, "state": state, "elevated": True,
                        "driver": "ok" if state == "ok" else "missing", "engine": "0.9.6", "error": None,
                        "readings": readings if state == "ok" else []}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps(outer.payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ClientTests(unittest.TestCase):
    def patches(self, ports, installed=False):
        return (mock.patch.object(helper, "PORTS", ports),
                mock.patch.object(helper_client, "task_installed", return_value=installed),
                mock.patch.object(helper_client, "start_task", return_value=True),
                mock.patch.object(helper_client, "pawnio_installed", return_value=True))

    def test_reads_a_running_helper(self):
        fake = FakeHelper()
        a, b, c, d = self.patches((fake.port,))
        with a, b, c, d:
            client = helper_client.HelperClient()
            client.start()
            try:
                self.assertTrue(wait_for(lambda: client.status()["state"] == "on"))
                self.assertEqual(client.readings(), READINGS)
                self.assertEqual(client.status()["sensors"], len(READINGS))
            finally:
                client.stop()
                fake.close()

    def test_readings_are_withheld_unless_the_helper_vouches_for_them(self):
        fake = FakeHelper(state="no_driver")
        a, b, c, d = self.patches((fake.port,))
        with a, b, c, d:
            client = helper_client.HelperClient()
            client.start()
            try:
                self.assertTrue(wait_for(lambda: client.status()["state"] == "no_driver"))
                self.assertEqual(client.readings(), [])
            finally:
                client.stop()
                fake.close()

    def test_installed_helper_is_started_on_demand(self):
        a, b, c, d = self.patches((free_port(),), installed=True)
        with a, b, c as start, d:
            client = helper_client.HelperClient()
            client.start()
            try:
                self.assertTrue(wait_for(lambda: start.called))
                self.assertEqual(client.status()["state"], "starting")
            finally:
                client.stop()

    def test_declined_permission_changes_nothing(self):
        a, b, c, d = self.patches((free_port(),))
        with a, b, c, d, mock.patch.object(helper_client, "engine_available", return_value=True), \
                mock.patch.object(helper_client, "run_elevated", return_value=helper_client.CANCELLED):
            result = helper_client.HelperClient().enable()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "off")
        self.assertIn("nothing was changed", result["note"])


class CollectorTests(unittest.TestCase):
    """The reading has to arrive on the CPU page, labelled with where it came from."""

    def test_cpu_temperature_comes_from_the_helper(self):
        fake = FakeHelper()
        with mock.patch.object(helper, "PORTS", (fake.port,)), \
                mock.patch.object(helper_client, "task_installed", return_value=False), \
                mock.patch.object(helper_client, "start_task", return_value=True):
            collector = Collector(0.25)
            collector.start()
            try:
                self.assertTrue(wait_for(lambda: collector.snapshot().get("cpu", {}).get("temp_c") is not None, 30),
                                collector.snapshot().get("error"))
                snap = collector.snapshot()
                self.assertEqual(bridge.cpu_temperature(READINGS), 51.25)   # Tctl/Tdie, not the cooler CCD sensor
                self.assertEqual(snap["cpu"]["temp_c"], 51.2)               # shown to one decimal
                self.assertEqual(snap["cpu"]["temp_source"], "Processor thermal sensor")
                self.assertIsNone(snap["cpu"]["temp_reason"])
                self.assertEqual(snap["helper"]["state"], "on")
                self.assertEqual(snap["bridge"]["source"], "Sensor helper")
                rows = {r["id"]: r for r in collector.sensors()["rows"]}
                self.assertEqual(rows["cpu.temp"]["source"], "Processor thermal sensor")
                fan = rows["lhm./lpc/nct6687d/0/fan/0"]
                self.assertEqual((fan["group"], fan["value"], fan["unit"]), ("Nuvoton NCT6687D (sensor helper)", 812.0, "RPM"))
            finally:
                collector.stop()
                fake.close()


if __name__ == "__main__":
    unittest.main()
