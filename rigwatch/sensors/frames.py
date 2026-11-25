"""Frame rate (FPS) of running programs, counted from Windows' own graphics event log.

Every frame a program hands to the screen passes through the Windows graphics
kernel (DxgKrnl), which can log one "present" event per frame through Event
Tracing for Windows. This module opens a private real-time trace session, asks
for that one event only, and counts the events per process.

* Nothing is injected into any program. This is passive listening.
* Direct3D 9 to 12, Vulkan and OpenGL all present through the same kernel entry
  point, so one event covers them all.
* A normal account may run a trace session when it belongs to "Performance Log
  Users", which interactive accounts do on current Windows. If Windows refuses,
  ``available`` stays False and ``reason`` says why; no number is made up.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import threading
import time
import uuid
from collections import deque
from ctypes import wintypes

PROVIDER = "802ec45a-1e99-4b83-9920-87c98277ba9d"      # Microsoft-Windows-DxgKrnl
PRESENT_EVENT = 184                                    # Present: one per frame a program presents
PRESENT_KEYWORDS = 0x8000001                           # Base | Present
SESSION_PREFIX = "RigWatch-Frames-"
WINDOW_S = 1.0                                         # the rate is averaged over this long
FLUSH_WAIT_S = 0.03                                    # time the reader gets to take in a flushed batch
TICKS = 10_000_000                                     # event timestamps are in 100 ns units
_EPOCH_TICKS = 116_444_736_000_000_000                 # 1601-01-01 to 1970-01-01

_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_INVALID_TRACE = 0xFFFFFFFFFFFFFFFF


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class _WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", ctypes.c_ulong), ("ProviderId", ctypes.c_ulong),
                ("HistoricalContext", ctypes.c_uint64), ("TimeStamp", ctypes.c_int64),
                ("Guid", _GUID), ("ClientContext", ctypes.c_ulong), ("Flags", ctypes.c_ulong)]


class _EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", _WNODE_HEADER), ("BufferSize", ctypes.c_ulong), ("MinimumBuffers", ctypes.c_ulong),
                ("MaximumBuffers", ctypes.c_ulong), ("MaximumFileSize", ctypes.c_ulong),
                ("LogFileMode", ctypes.c_ulong), ("FlushTimer", ctypes.c_ulong), ("EnableFlags", ctypes.c_ulong),
                ("AgeLimit", ctypes.c_long), ("NumberOfBuffers", ctypes.c_ulong), ("FreeBuffers", ctypes.c_ulong),
                ("EventsLost", ctypes.c_ulong), ("BuffersWritten", ctypes.c_ulong),
                ("LogBuffersLost", ctypes.c_ulong), ("RealTimeBuffersLost", ctypes.c_ulong),
                ("LoggerThreadId", ctypes.c_void_p), ("LogFileNameOffset", ctypes.c_ulong),
                ("LoggerNameOffset", ctypes.c_ulong)]


class _EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Id", ctypes.c_ushort), ("Version", ctypes.c_ubyte), ("Channel", ctypes.c_ubyte),
                ("Level", ctypes.c_ubyte), ("Opcode", ctypes.c_ubyte), ("Task", ctypes.c_ushort),
                ("Keyword", ctypes.c_uint64)]


class _EVENT_HEADER(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_ushort), ("HeaderType", ctypes.c_ushort), ("Flags", ctypes.c_ushort),
                ("EventProperty", ctypes.c_ushort), ("ThreadId", ctypes.c_ulong), ("ProcessId", ctypes.c_ulong),
                ("TimeStamp", ctypes.c_int64), ("ProviderId", _GUID), ("EventDescriptor", _EVENT_DESCRIPTOR),
                ("ProcessorTime", ctypes.c_uint64), ("ActivityId", _GUID)]


class _EVENT_RECORD(ctypes.Structure):
    _fields_ = [("EventHeader", _EVENT_HEADER), ("BufferContext", ctypes.c_ulong),
                ("ExtendedDataCount", ctypes.c_ushort), ("UserDataLength", ctypes.c_ushort),
                ("ExtendedData", ctypes.c_void_p), ("UserData", ctypes.c_void_p), ("UserContext", ctypes.c_void_p)]


_EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.POINTER(_EVENT_RECORD))


class _EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [("LogFileName", ctypes.c_wchar_p), ("LoggerName", ctypes.c_wchar_p),
                ("CurrentTime", ctypes.c_int64), ("BuffersRead", ctypes.c_ulong),
                ("ProcessTraceMode", ctypes.c_ulong), ("CurrentEvent", ctypes.c_byte * 88),
                ("LogfileHeader", ctypes.c_byte * 280), ("BufferCallback", ctypes.c_void_p),
                ("BufferSize", ctypes.c_ulong), ("Filled", ctypes.c_ulong), ("EventsLost", ctypes.c_ulong),
                ("EventRecordCallback", _EVENT_RECORD_CALLBACK), ("IsKernelTrace", ctypes.c_ulong),
                ("Context", ctypes.c_void_p)]


class _EVENT_FILTER_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Ptr", ctypes.c_uint64), ("Size", ctypes.c_ulong), ("Type", ctypes.c_ulong)]


class _EVENT_FILTER_EVENT_ID(ctypes.Structure):
    _fields_ = [("FilterIn", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte), ("Count", ctypes.c_ushort),
                ("Events", ctypes.c_ushort * 1)]


class _ENABLE_TRACE_PARAMETERS(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("EnableProperty", ctypes.c_ulong),
                ("ControlFlags", ctypes.c_ulong), ("SourceId", _GUID),
                ("EnableFilterDesc", ctypes.POINTER(_EVENT_FILTER_DESCRIPTOR)), ("FilterDescCount", ctypes.c_ulong)]


_PROPS_SIZE = ctypes.sizeof(_EVENT_TRACE_PROPERTIES) + 2 * 1024      # room for the two names


def _api():
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.StartTraceW.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_wchar_p, ctypes.c_void_p]
    adv.StartTraceW.restype = ctypes.c_ulong
    adv.ControlTraceW.argtypes = [ctypes.c_uint64, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong]
    adv.ControlTraceW.restype = ctypes.c_ulong
    adv.EnableTraceEx2.argtypes = [ctypes.c_uint64, ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_ubyte,
                                   ctypes.c_uint64, ctypes.c_uint64, ctypes.c_ulong, ctypes.c_void_p]
    adv.EnableTraceEx2.restype = ctypes.c_ulong
    adv.OpenTraceW.argtypes = [ctypes.POINTER(_EVENT_TRACE_LOGFILEW)]
    adv.OpenTraceW.restype = ctypes.c_uint64
    adv.ProcessTrace.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
    adv.ProcessTrace.restype = ctypes.c_ulong
    adv.CloseTrace.argtypes = [ctypes.c_uint64]
    adv.CloseTrace.restype = ctypes.c_ulong
    adv.QueryAllTracesW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    adv.QueryAllTracesW.restype = ctypes.c_ulong
    return adv


def _properties():
    buf = ctypes.create_string_buffer(_PROPS_SIZE)
    props = _EVENT_TRACE_PROPERTIES.from_buffer(buf)
    props.Wnode.BufferSize = _PROPS_SIZE
    props.Wnode.Flags = 0x00020000              # WNODE_FLAG_TRACED_GUID
    props.Wnode.ClientContext = 1               # high-resolution timestamps
    # Real time, and one shared buffer instead of one per processor core. With so
    # few events, per-core buffers reach the reader late and out of step with each other.
    props.LogFileMode = 0x00000100 | 0x10000000
    props.FlushTimer = 1                        # seconds; rates() also asks for a flush on every sample
    props.LoggerNameOffset = ctypes.sizeof(_EVENT_TRACE_PROPERTIES)
    return buf, props


def _now_ticks() -> int:
    return time.time_ns() // 100 + _EPOCH_TICKS


def _pid_alive(pid: int) -> bool:
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259                                           # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def session_names(adv=None) -> list[str]:
    """Names of all running trace sessions this account can see."""
    adv = adv or _api()
    count = 64
    buffers = [_properties()[0] for _ in range(count)]
    array = (ctypes.c_void_p * count)(*[ctypes.addressof(b) for b in buffers])
    got = ctypes.c_ulong(0)
    if adv.QueryAllTracesW(array, count, ctypes.byref(got)) != 0:
        return []
    names = []
    for buf in buffers[:got.value]:
        props = _EVENT_TRACE_PROPERTIES.from_buffer(buf)
        if props.LoggerNameOffset:
            names.append(ctypes.wstring_at(ctypes.addressof(buf) + props.LoggerNameOffset))
    return names


def stop_stale_sessions(adv=None) -> int:
    """Stop frame sessions left behind by a RigWatch that was killed. Returns how many."""
    adv = adv or _api()
    stopped = 0
    for name in session_names(adv):
        if not name.startswith(SESSION_PREFIX):
            continue
        try:
            owner = int(name[len(SESSION_PREFIX):])
        except ValueError:
            continue
        if owner != os.getpid() and _pid_alive(owner):
            continue
        buf, _ = _properties()
        if adv.ControlTraceW(0, name, buf, 1) == 0:                        # EVENT_TRACE_CONTROL_STOP
            stopped += 1
    return stopped


def frame_rates(times: dict[int, deque], end: int) -> dict[int, float]:
    """Frames per second per process over the ``WINDOW_S`` that ends at ``end``.

    ``times`` maps a process id to the timestamps of its present events, oldest
    first. Timestamps that fell out of the window are dropped as a side effect;
    events newer than ``end`` stay for the next call.
    """
    start = end - int(WINDOW_S * TICKS)
    rates: dict[int, float] = {}
    for pid in list(times):
        stamps = times[pid]
        while stamps and stamps[0] <= start:
            stamps.popleft()
        if not stamps:
            del times[pid]
            continue
        count = sum(1 for stamp in stamps if stamp <= end)
        if count:
            rates[pid] = count / WINDOW_S
    return rates


def foreground_pid() -> int | None:
    """Process id of the window the user is working in."""
    try:
        user32 = ctypes.windll.user32
        pid = wintypes.DWORD()
        hwnd = user32.GetForegroundWindow()
        if not hwnd or not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return None
        return pid.value or None
    except Exception:
        return None


class FrameCounter:
    """Counts presented frames per process. ``start()``, then ``rates()`` once per sample."""

    def __init__(self) -> None:
        self.available = False
        self.reason: str | None = "Frame rate reading is switched off."
        self.events = 0                      # present events seen since start (for tests and diagnostics)
        self.other_events = 0                # anything else that slipped through the filter
        self._times: dict[int, deque] = {}
        self._lock = threading.Lock()
        self._adv = None
        self._name = f"{SESSION_PREFIX}{os.getpid()}"
        self._session = ctypes.c_uint64(0)
        self._trace = _INVALID_TRACE
        self._thread: threading.Thread | None = None
        self._keep: list = []                # ctypes objects Windows keeps pointers to
        self._running = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if self._running:
            return True
        try:
            return self._start()
        except Exception as exc:             # a missing API on an old Windows, for example
            self.available, self.reason = False, f"Frame rate reading could not start ({exc!r})."
            return False

    def _start(self) -> bool:
        adv = self._adv = self._adv or _api()
        stop_stale_sessions(adv)
        buf, _ = _properties()
        rc = adv.StartTraceW(ctypes.byref(self._session), self._name, buf)
        if rc == _ERROR_ALREADY_EXISTS:
            stale, _ = _properties()
            adv.ControlTraceW(0, self._name, stale, 1)
            buf, _ = _properties()
            rc = adv.StartTraceW(ctypes.byref(self._session), self._name, buf)
        if rc != 0:
            self.available = False
            self.reason = ("Windows did not allow RigWatch to read its graphics event log on this account."
                           if rc == _ERROR_ACCESS_DENIED else
                           f"Windows could not start the graphics event log reader (error {rc}).")
            return False

        provider = _GUID.from_buffer_copy(uuid.UUID(PROVIDER).bytes_le)
        ids = _EVENT_FILTER_EVENT_ID(FilterIn=1, Reserved=0, Count=1)
        ids.Events[0] = PRESENT_EVENT
        desc = _EVENT_FILTER_DESCRIPTOR(Ptr=ctypes.addressof(ids), Size=ctypes.sizeof(ids), Type=0x80000200)
        params = _ENABLE_TRACE_PARAMETERS(Version=2, EnableFilterDesc=ctypes.pointer(desc), FilterDescCount=1)
        rc = adv.EnableTraceEx2(self._session.value, ctypes.byref(provider), 1, 4, PRESENT_KEYWORDS, 0, 0,
                                ctypes.byref(params))
        if rc != 0:                          # older Windows without event-id filters: filter in the callback
            rc = adv.EnableTraceEx2(self._session.value, ctypes.byref(provider), 1, 4, PRESENT_KEYWORDS, 0, 0, None)
        if rc != 0:
            self._stop_session()
            self.available, self.reason = False, f"Windows could not switch on frame events (error {rc})."
            return False

        callback = _EVENT_RECORD_CALLBACK(self._on_event)
        log = _EVENT_TRACE_LOGFILEW()
        log.LoggerName = self._name
        log.ProcessTraceMode = 0x00000100 | 0x10000000         # real time | event records
        log.EventRecordCallback = callback
        trace = adv.OpenTraceW(ctypes.byref(log))
        if trace == _INVALID_TRACE:
            error = ctypes.get_last_error()
            self._stop_session()
            self.available, self.reason = False, f"Windows could not open the frame event stream (error {error})."
            return False
        self._trace = trace
        self._keep = [callback, log, ids, desc, params, provider]
        handles = (ctypes.c_uint64 * 1)(trace)
        self._thread = threading.Thread(target=adv.ProcessTrace, args=(handles, 1, None, None),
                                        name="rigwatch-frames", daemon=True)
        self._thread.start()
        self._running = True
        self.available, self.reason = True, None
        atexit.register(self.stop)
        return True

    def _stop_session(self) -> None:
        buf, _ = _properties()
        self._adv.ControlTraceW(0, self._name, buf, 1)

    def stop(self) -> None:
        """Stop listening. The reader thread drains what is buffered and ends."""
        if not self._running:
            return
        self._running = False
        try:
            self._stop_session()
            if self._thread is not None:
                self._thread.join(timeout=5)
            if self._trace != _INVALID_TRACE:
                self._adv.CloseTrace(self._trace)
                self._trace = _INVALID_TRACE
        except Exception:
            pass
        if self._thread is None or not self._thread.is_alive():
            self._keep = []                  # only safe to free once Windows no longer calls back
        with self._lock:
            self._times.clear()
        self.available, self.reason = False, "Frame rate reading is switched off."
        try:
            atexit.unregister(self.stop)
        except Exception:
            pass

    # ------------------------------------------------------------- readings
    def _on_event(self, record) -> None:
        header = record.contents.EventHeader
        if header.EventDescriptor.Id != PRESENT_EVENT:
            self.other_events += 1
            return
        pid, stamp = header.ProcessId, header.TimeStamp
        with self._lock:
            stamps = self._times.get(pid)
            if stamps is None:
                stamps = self._times[pid] = deque(maxlen=8192)
            stamps.append(stamp)
            self.events += 1

    def rates(self) -> dict[int, float]:
        """Frames per second for every process that presented in the last second.

        Windows hands events over in batches. Asking for a flush first, and giving
        the reader thread a moment, makes the last second complete before it is counted.
        """
        if not self._running:
            return {}
        end = _now_ticks()
        try:
            buf, _ = _properties()
            self._adv.ControlTraceW(0, self._name, buf, 3)                 # EVENT_TRACE_CONTROL_FLUSH
        except Exception:
            pass
        time.sleep(FLUSH_WAIT_S)
        with self._lock:
            return frame_rates(self._times, end)


if __name__ == "__main__":       # python -m rigwatch.sensors.frames [seconds]
    import sys

    import psutil

    counter = FrameCounter()
    print("started:", counter.start(), "|", counter.reason or "listening")
    try:
        for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 5):
            time.sleep(1.0)
            fg = foreground_pid()
            parts = []
            for pid, fps in sorted(counter.rates().items(), key=lambda kv: -kv[1])[:6]:
                try:
                    name = psutil.Process(pid).name()
                except Exception:
                    name = "?"
                parts.append(f"{name}({pid}){'*' if pid == fg else ''} {fps:.0f}")
            print(f"events {counter.events:6d}  other {counter.other_events:5d} | " + "  ".join(parts))
    finally:
        counter.stop()
        print("sessions left:", [n for n in session_names() if n.startswith(SESSION_PREFIX)])
