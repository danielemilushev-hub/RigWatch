"""Frame rate (FPS) and monitor details."""

import os
import subprocess
import sys
import unittest
from collections import deque
from unittest import mock

from rigwatch import strip
from rigwatch.collector import Collector, _app_name
from rigwatch.sensors import displays, frames, winsys
from rigwatch.settings import DEFAULTS, _ALLOWED

T = frames.TICKS


class FrameRateMathTests(unittest.TestCase):
    def test_counts_the_frames_of_the_last_second(self):
        end = 100 * T
        times = {7: deque(end - T + i * T // 165 + 1 for i in range(165))}
        self.assertEqual(frames.frame_rates(times, end), {7: 165.0})

    def test_older_frames_are_dropped_and_newer_ones_wait(self):
        end = 100 * T
        times = {7: deque([end - 3 * T, end - T, end - T // 2, end, end + 5])}
        self.assertEqual(frames.frame_rates(times, end), {7: 2.0})          # the half-second one and "end" itself
        self.assertEqual(list(times[7]), [end - T // 2, end, end + 5])      # the future one is kept for next time

    def test_a_program_that_stopped_drawing_disappears(self):
        end = 100 * T
        times = {7: deque([end - 5 * T]), 8: deque([end - 10])}
        self.assertEqual(frames.frame_rates(times, end), {8: 1.0})
        self.assertNotIn(7, times)

    def test_nothing_drawn_means_no_rates(self):
        self.assertEqual(frames.frame_rates({}, 100 * T), {})


class FakeCounter:
    available, reason = True, None

    def __init__(self, rates):
        self._rates = rates

    def rates(self):
        return dict(self._rates)

    def stop(self):
        pass


class ForegroundTests(unittest.TestCase):
    """Whose frame rate is shown: the program in use, including the processes it started."""

    def make(self, rates, front):
        c = Collector(0.5, frames_on=True)
        c.frames = FakeCounter(rates)
        c._own_pid = 10
        # pid -> (parent, create time, name)
        c._proc_info = {
            1: (0, 1, "explorer.exe"),
            10: (1, 10, "pythonw.exe"), 11: (10, 11, "msedgewebview2.exe"),
            20: (1, 20, "brave.exe"), 21: (20, 21, "brave.exe"),
            30: (1, 30, "Game.exe"),
            40: (1, 40, "old.exe"), 41: (40, 5, "reused.exe"),      # 41 is older than its "parent": a reused id
        }
        patcher = mock.patch.object(frames, "foreground_pid", lambda: front)
        patcher.start()
        self.addCleanup(patcher.stop)
        return c

    def test_a_game_in_front(self):
        fps = self.make({30: 144.0, 21: 60.0}, front=30)._fps()
        self.assertEqual((fps["value"], fps["app"]), (144.0, "Game"))

    def test_a_browser_draws_from_a_child_process(self):
        fps = self.make({21: 60.0}, front=20)._fps()
        self.assertEqual((fps["value"], fps["app"]), (60.0, "brave"))

    def test_program_in_front_that_is_not_drawing(self):
        fps = self.make({21: 60.0}, front=30)._fps()
        self.assertEqual((fps["value"], fps["app"]), (None, "Game"))

    def test_looking_at_rigwatch_keeps_the_program_used_before(self):
        c = self.make({30: 90.0, 11: 30.0}, front=30)
        c._fps()
        with mock.patch.object(frames, "foreground_pid", lambda: 10):
            fps = c._fps()
        self.assertEqual((fps["value"], fps["app"]), (90.0, "Game"))

    def test_rigwatch_itself_is_never_the_program(self):
        fps = self.make({11: 30.0}, front=10)._fps()
        self.assertEqual((fps["value"], fps["app"]), (None, None))
        self.assertEqual(fps["apps"], [])

    def test_a_reused_process_id_is_not_family(self):
        fps = self.make({41: 75.0}, front=40)._fps()
        self.assertIsNone(fps["value"])

    def test_a_closed_program_is_forgotten(self):
        c = self.make({30: 90.0}, front=30)
        c._fps()
        del c._proc_info[30]
        c.frames = FakeCounter({})
        with mock.patch.object(frames, "foreground_pid", lambda: 10):
            fps = c._fps()
        self.assertEqual((fps["value"], fps["app"]), (None, None))

    def test_everything_drawing_is_listed_fastest_first(self):
        fps = self.make({30: 144.0, 21: 60.0, 20: 2.0, 11: 30.0}, front=30)._fps()
        self.assertEqual(fps["apps"], [{"name": "Game", "fps": 144.0}, {"name": "brave", "fps": 60.0}])

    def test_switched_off_says_so(self):
        c = Collector(0.5)
        fps = c._fps()
        self.assertEqual((fps["enabled"], fps["value"]), (False, None))
        self.assertIn("switched off", fps["reason"])

    def test_refused_by_windows_says_why(self):
        c = self.make({}, front=30)
        c.frames.available, c.frames.reason = False, "Windows did not allow it."
        fps = c._fps()
        self.assertEqual((fps["available"], fps["value"], fps["reason"]), (False, None, "Windows did not allow it."))

    def test_program_names_lose_the_extension(self):
        self.assertEqual(_app_name("Cyberpunk2077.exe"), "Cyberpunk2077")
        self.assertEqual(_app_name("System"), "System")
        self.assertIsNone(_app_name(""))


class StripTests(unittest.TestCase):
    SNAP = {"cpu": {"util": 10.0, "temp_c": None}, "memory": {"pct": 30.0}, "gpus": [], "power": {"total_w": 90.0}}

    def labels(self, fps):
        return {f["label"]: f for f in strip.build_fields(dict(self.SNAP, fps=fps))}

    def test_frame_rate_column(self):
        field = self.labels({"available": True, "value": 164.6})["FPS"]
        self.assertEqual(field["value"], "165")

    def test_a_program_that_is_not_drawing_is_a_dash_not_a_missing_sensor(self):
        field = self.labels({"available": True, "value": None})["FPS"]
        self.assertEqual((field["value"], field["placeholder"]), (None, "–"))

    def test_no_column_when_frame_rate_is_off_or_unavailable(self):
        self.assertNotIn("FPS", self.labels({"available": False, "value": None}))
        self.assertNotIn("FPS", {f["label"] for f in strip.build_fields(self.SNAP)})

    def test_column_sits_before_the_watts(self):
        names = [f["label"] for f in strip.build_fields(dict(self.SNAP, fps={"available": True, "value": 60}))]
        self.assertEqual(names[-2:], ["FPS", "WATT"])


class SettingsTests(unittest.TestCase):
    def test_frame_rate_is_on_by_default_and_a_plain_switch(self):
        self.assertIs(DEFAULTS["fps"], True)
        self.assertTrue(_ALLOWED["fps"](False))
        self.assertFalse(_ALLOWED["fps"]("no"))


def edid(maker=b"\x05\xe3", name=b"Q32G3WG3R3", year=31, size_cm=(70, 39), native=(2560, 1440), mm=(698, 393)):
    """A minimal monitor identification block."""
    block = bytearray(128)
    block[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    block[8:10] = maker
    block[17] = year
    block[21], block[22] = size_cm
    dtd = bytearray(18)
    dtd[0:2] = b"\x01\x01"                                  # a pixel clock, so this is a timing
    dtd[2], dtd[4] = native[0] & 0xFF, (native[0] >> 8) << 4
    dtd[5], dtd[7] = native[1] & 0xFF, (native[1] >> 8) << 4
    dtd[12], dtd[13] = mm[0] & 0xFF, mm[1] & 0xFF
    dtd[14] = ((mm[0] >> 8) << 4) | (mm[1] >> 8)
    block[54:72] = dtd
    block[72:90] = b"\x00\x00\x00\xfc\x00" + name.ljust(13, b"\x0a")[:13]
    return bytes(block)


class EdidTests(unittest.TestCase):
    def test_what_a_monitor_says_about_itself(self):
        info = displays.parse_edid(edid())
        self.assertEqual(info["maker"], "AOC")
        self.assertEqual(info["name"], "Q32G3WG3R3")
        self.assertEqual(info["year"], 2021)
        self.assertEqual(info["native"], (2560, 1440))
        self.assertEqual(info["size_mm"], (698, 393))        # millimetres from the timing beat the rounded centimetres
        self.assertEqual(info["size_in"], 31.5)

    def test_unknown_maker_keeps_its_code(self):
        self.assertEqual(displays.parse_edid(edid(maker=b"\x69\x59"))["maker"], "ZJY")

    def test_garbage_is_not_trusted(self):
        self.assertEqual(displays.parse_edid(b"\x00" * 128), {})
        self.assertEqual(displays.parse_edid(b"short"), {})


class ColourTests(unittest.TestCase):
    """HDR must not be confused with Windows' automatic wide-colour mode."""

    @staticmethod
    def newer(value, mode, bits=10):
        return mock.Mock(value=value, activeColorMode=mode, bitsPerColorChannel=bits)

    @staticmethod
    def older(value, bits=8):
        return mock.Mock(value=value, bitsPerColorChannel=bits)

    def test_hdr_on(self):
        self.assertEqual(displays.colour_state(self.newer(0b110011, 2), None),
                         {"hdr": "on", "wide_colour": False, "bits_per_channel": 10})

    def test_wide_colour_is_not_hdr(self):
        state = displays.colour_state(self.newer(0b11010111, 1, bits=8), None)       # as read on the test PC
        self.assertEqual(state, {"hdr": "off", "wide_colour": True, "bits_per_channel": 8})

    def test_hdr_not_supported(self):
        self.assertEqual(displays.colour_state(self.newer(0, 0, bits=8), None)["hdr"], "unsupported")

    def test_older_windows_answer(self):
        self.assertEqual(displays.colour_state(None, self.older(0b011))["hdr"], "on")
        self.assertEqual(displays.colour_state(None, self.older(0b111)),
                         {"hdr": "off", "wide_colour": True, "bits_per_channel": 8})
        self.assertEqual(displays.colour_state(None, self.older(0b001))["hdr"], "off")
        self.assertEqual(displays.colour_state(None, self.older(0))["hdr"], "unsupported")

    def test_no_answer_claims_nothing(self):
        self.assertEqual(displays.colour_state(None, None), {"hdr": None, "wide_colour": None, "bits_per_channel": None})


class LiveTests(unittest.TestCase):
    """Against this PC. Values must be plausible; nothing is assumed about the hardware."""

    def test_monitors_are_plausible(self):
        for d in displays.list_displays():
            self.assertTrue(d["name"])
            if d["width"]:
                self.assertTrue(320 <= d["width"] <= 16384 and 200 <= d["height"] <= 16384, d)
            if d["refresh_hz"]:
                self.assertTrue(20 <= d["refresh_hz"] <= 1000, d)
            if d["max_refresh_hz"] and d["refresh_hz"]:
                self.assertGreaterEqual(d["max_refresh_hz"] + 1, d["refresh_hz"])
            self.assertIn(d["hdr"], ("on", "off", "unsupported", None))

    def test_process_list_knows_parents(self):
        me = next(p for p in winsys.process_snapshot() if p["pid"] == os.getpid())
        self.assertEqual(me["parent"], os.getppid())

    def test_frame_counter_starts_and_leaves_nothing_behind(self):
        counter = frames.FrameCounter()
        if not counter.start():
            self.skipTest(f"Windows does not allow it here: {counter.reason}")
        try:
            self.assertIn(counter._name, frames.session_names())
            self.assertIsInstance(counter.rates(), dict)
        finally:
            counter.stop()
        self.assertNotIn(counter._name, frames.session_names())
        self.assertFalse(counter.available)

    def test_a_session_left_by_a_killed_rigwatch_is_cleaned_up(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        name = f"{frames.SESSION_PREFIX}{gone.pid}"
        adv = frames._api()
        handle = frames.ctypes.c_uint64(0)
        buf, _ = frames._properties()
        if adv.StartTraceW(frames.ctypes.byref(handle), name, buf) != 0:
            self.skipTest("Windows does not allow trace sessions here")
        try:
            self.assertIn(name, frames.session_names())
            self.assertGreaterEqual(frames.stop_stale_sessions(), 1)
            self.assertNotIn(name, frames.session_names())
        finally:
            buf, _ = frames._properties()
            adv.ControlTraceW(0, name, buf, 1)


if __name__ == "__main__":
    unittest.main()
