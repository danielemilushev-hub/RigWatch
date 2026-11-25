"""Start with Windows, and finding a RigWatch window that is already open.

The registry tests use a scratch key under HKEY_CURRENT_USER, never the real
start-up list, so running them cannot add anything to Windows start-up.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from rigwatch import __version__, app, startup
from rigwatch.server import RigWatchServer
from rigwatch.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
SCRATCH_PARENT = r"Software\RigWatchTests"
SCRATCH = rf"{SCRATCH_PARENT}\{os.getpid()}"


def delete_tree(key: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS) as handle:
            while True:
                try:
                    delete_tree(key + "\\" + winreg.EnumKey(handle, 0))
                except OSError:
                    break
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except OSError:
        pass


def tearDownModule():
    delete_tree(SCRATCH)
    try:                                   # remove the parent too, unless another test run is using it
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, SCRATCH_PARENT)
    except OSError:
        pass


class CommandTests(unittest.TestCase):
    def test_exe_starts_itself_minimised(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\Apps\Rig Watch\RigWatch.exe"):
            self.assertEqual(startup.launch_command(), r'"C:\Apps\Rig Watch\RigWatch.exe" --minimized')

    def test_source_checkout_starts_its_launcher_without_a_console(self):
        command = startup.launch_command()
        program, script = startup.command_files(command)
        self.assertTrue(program.is_file() and script.is_file())
        self.assertEqual(script, ROOT / "tools" / "rigwatch_entry.py")
        self.assertTrue(command.endswith(" --minimized"))
        if program.with_name("pythonw.exe").is_file():
            self.assertEqual(program.name.lower(), "pythonw.exe")

    def test_a_path_too_long_for_the_startup_list_is_refused(self):
        long_exe = "C:\\" + "x" * 260 + "\\RigWatch.exe"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", long_exe):
            self.assertIsNone(startup.launch_command())

    def test_the_launcher_works_from_any_folder(self):
        """A start-up entry has no working directory; the launcher must not need one."""
        with tempfile.TemporaryDirectory() as elsewhere:
            done = subprocess.run([sys.executable, str(ROOT / "tools" / "rigwatch_entry.py"), "--version"],
                                  cwd=elsewhere, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"RigWatch {__version__}", done.stdout)

    def test_files_named_by_a_command(self):
        self.assertEqual(startup.command_files(r'"C:\a b\x.exe" "D:\c\y.py" --minimized'),
                         [Path(r"C:\a b\x.exe"), Path(r"D:\c\y.py")])
        self.assertEqual(startup.command_files(r"C:\x.exe --minimized"), [Path(r"C:\x.exe")])
        self.assertEqual(startup.command_files(""), [])


class ExternalDriveTests(unittest.TestCase):
    """A copy on a USB drive only starts if the drive is plugged in at sign-in."""

    def probe(self, bus, drive_type=3, removable=False):
        from rigwatch.sensors import storage
        drives = [{"index": 0, "bus": "NVMe", "removable": False}, {"index": 4, "bus": bus, "removable": removable}]
        with mock.patch.object(storage, "volume_disks", return_value=[4]), \
                mock.patch.object(storage, "list_drives", return_value=drives), \
                mock.patch.object(startup.ctypes.windll.kernel32, "GetDriveTypeW", return_value=drive_type):
            return startup._probe_external("Q:\\")

    def test_usb_hard_disk_counts_although_windows_calls_it_fixed(self):
        self.assertTrue(self.probe("USB"))

    def test_internal_drive_does_not(self):
        self.assertFalse(self.probe("SATA"))
        self.assertFalse(self.probe("NVMe"))

    def test_usb_stick_and_card_reader_count(self):
        self.assertTrue(self.probe("SATA", drive_type=2))
        self.assertTrue(self.probe("SD"))
        self.assertTrue(self.probe("SATA", removable=True))

    def test_network_share_is_not_a_drive(self):
        self.assertFalse(startup.on_external_drive(Path(r"\\server\share\RigWatch.exe")))


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(delete_tree, SCRATCH)
        self.here = Path(__file__)                       # any file that exists
        self.command = f'"{self.here}" --minimized'
        self.entry = self.make(lambda: self.command)

    def make(self, command):
        return startup.StartupEntry(SCRATCH + r"\Run", SCRATCH + r"\Approved", "RigWatch", command)

    def approve(self, first_byte: int) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, SCRATCH + r"\Approved", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "RigWatch", 0, winreg.REG_BINARY, bytes([first_byte]) + bytes(11))

    def test_off_until_switched_on(self):
        st = self.entry.status()
        self.assertEqual((st["state"], st["enabled"], st["available"]), ("off", False, True))

    def test_on_and_off_again_leaves_nothing_behind(self):
        st = self.entry.enable()
        self.assertEqual((st["state"], st["enabled"], st["this_copy"]), ("on", True, True))
        self.assertEqual(self.entry.registered(), self.command)
        self.assertEqual(st["target"], str(self.here))
        self.assertFalse(st["target_missing"])
        st = self.entry.disable()
        self.assertEqual(st["state"], "off")
        self.assertIsNone(self.entry.registered())
        self.assertIsNone(self.entry._read(self.entry.approved_key))

    def test_switched_off_in_task_manager_is_not_shown_as_on(self):
        self.entry.enable()
        self.approve(0x03)
        st = self.entry.status()
        self.assertEqual((st["state"], st["enabled"]), ("blocked", False))

    def test_switched_on_in_task_manager_counts_as_on(self):
        self.entry.enable()
        self.approve(0x02)
        self.assertEqual(self.entry.status()["state"], "on")

    def test_switching_on_here_lifts_the_block(self):
        self.entry.enable()
        self.approve(0x03)
        self.assertEqual(self.entry.enable()["state"], "on")

    def test_a_copy_that_cannot_be_registered_says_so(self):
        entry = self.make(lambda: None)
        st = entry.enable()
        self.assertEqual((st["state"], st["available"]), ("off", False))
        self.assertIn("too long", st["note"])
        self.assertIsNone(entry.registered())

    def test_a_moved_exe_is_followed(self):
        gone = self.make(lambda: r'"C:\no such folder\RigWatch.exe" --minimized')
        gone.enable()
        self.assertTrue(self.entry.status()["target_missing"])
        self.assertTrue(self.entry.repair())
        self.assertEqual(self.entry.registered(), self.command)

    def test_another_copy_that_still_exists_is_left_alone(self):
        other = f'"{ROOT / "README.md"}" --minimized'
        self.make(lambda: other).enable()
        self.assertFalse(self.entry.repair())
        self.assertEqual(self.entry.registered(), other)
        self.assertFalse(self.entry.status()["this_copy"])

    def test_nothing_to_repair_when_off(self):
        self.assertFalse(self.entry.repair())
        self.assertIsNone(self.entry.registered())


class FakeWindow(BaseHTTPRequestHandler):
    """Stands in for a RigWatch that is already running."""
    info = {"app": "rigwatch", "version": "x", "clients": 0, "window": True}
    focused = 0

    def log_message(self, *args):
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(type(self).info)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).focused += 1
        self._reply({"ok": True})


class SecondStartTests(unittest.TestCase):
    """Opening RigWatch while it is already open must reuse the open window."""

    def setUp(self):
        handler = type("Handler", (FakeWindow,), {"info": dict(FakeWindow.info), "focused": 0})
        self.handler = handler
        self.running = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.running.serve_forever, daemon=True).start()
        self.addCleanup(self.running.server_close)
        self.addCleanup(self.running.shutdown)
        self.port = self.running.server_address[1]

        closed = ThreadingHTTPServer(("127.0.0.1", 0), handler)      # a port nothing listens on
        self.usual_port = closed.server_address[1]
        closed.server_close()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.note = Path(tmp.name) / "instance.json"
        patcher = mock.patch.object(app, "_instance_file", lambda: self.note)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_found_on_the_usual_port(self):
        self.assertTrue(app._hand_over_to_running_instance(self.port))
        self.assertEqual(self.handler.focused, 1)

    def test_found_on_the_port_it_noted_when_the_usual_one_was_taken(self):
        self.assertFalse(app._hand_over_to_running_instance(self.usual_port))
        self.note.write_text(json.dumps({"port": self.port, "pid": 1}), encoding="utf-8")
        self.assertTrue(app._hand_over_to_running_instance(self.usual_port))
        self.assertEqual(self.handler.focused, 1)

    def test_a_startup_launch_does_not_raise_the_open_window(self):
        self.assertTrue(app._hand_over_to_running_instance(self.port, focus=False))
        self.assertEqual(self.handler.focused, 0)

    def test_a_rigwatch_without_a_window_is_not_a_window_to_reuse(self):
        self.handler.info["window"] = False
        self.assertFalse(app._hand_over_to_running_instance(self.port))
        self.assertEqual(self.handler.focused, 0)

    def test_another_program_on_the_port_is_ignored(self):
        self.handler.info["app"] = "something else"
        self.assertFalse(app._hand_over_to_running_instance(self.port))

    def test_an_older_rigwatch_is_still_recognised(self):
        del self.handler.info["window"]                  # 2.1 and earlier do not report it
        self.assertTrue(app._hand_over_to_running_instance(self.port))

    def test_a_stale_note_is_ignored(self):
        self.note.write_text("not json", encoding="utf-8")
        self.assertFalse(app._hand_over_to_running_instance(self.usual_port))

    def test_note_is_written_and_removed_by_its_owner_only(self):
        app._note_instance(4321)
        self.assertEqual(json.loads(self.note.read_text(encoding="utf-8")), {"port": 4321, "pid": os.getpid()})
        self.note.write_text(json.dumps({"port": 4321, "pid": -1}), encoding="utf-8")
        app._forget_instance()
        self.assertTrue(self.note.exists())               # another instance's note stays
        app._note_instance(4321)
        app._forget_instance()
        self.assertFalse(self.note.exists())


class ApiTests(unittest.TestCase):
    """The Settings switch talks to /api/startup."""

    @classmethod
    def setUpClass(cls):
        cls.server = RigWatchServer(("127.0.0.1", 0), collector=None, settings=Settings())
        cls.server.startup = startup.StartupEntry(SCRATCH + r"\ApiRun", SCRATCH + r"\ApiApproved", "RigWatch",
                                                  lambda: f'"{Path(__file__)}" --minimized')
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.loads(err.read())

    def test_switch_round_trip(self):
        self.assertEqual(self.call("/api/startup")[1]["state"], "off")
        status, st = self.call("/api/startup", {"enabled": True})
        self.assertEqual((status, st["state"]), (200, "on"))
        self.assertEqual(self.call("/api/startup")[1]["state"], "on")
        self.assertEqual(self.call("/api/startup", {"enabled": False})[1]["state"], "off")

    def test_a_malformed_request_changes_nothing(self):
        status, _ = self.call("/api/startup", {"enabled": "yes"})
        self.assertEqual(status, 400)
        self.assertEqual(self.call("/api/startup")[1]["state"], "off")

    def test_web_pages_cannot_flip_the_switch(self):
        status, _ = self.call("/api/startup", {"enabled": True}, {"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(self.call("/api/startup")[1]["state"], "off")

    def test_ping_says_whether_there_is_a_window(self):
        self.assertIs(self.call("/api/ping")[1]["window"], False)


if __name__ == "__main__":
    unittest.main()
