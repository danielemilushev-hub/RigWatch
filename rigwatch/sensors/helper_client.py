"""App side of the sensor helper: turning it on and off, starting it, reading it.

RigWatch itself never runs elevated. Turning the helper on shows one Windows
permission prompt, which registers an on-demand scheduled task; from then on
RigWatch starts the helper through that task without prompting, and the helper
exits by itself shortly after RigWatch is closed.
"""

from __future__ import annotations

import ctypes
import subprocess
import threading
import time
import winreg
from ctypes import wintypes

from . import helper

PAWNIO_URL = "https://pawnio.eu"
CANCELLED = -1223           # the permission prompt was declined
FAILED = -1

_NO_WINDOW = 0x08000000
_START_WINDOW_S = 25.0      # how long a freshly started helper gets before it counts as not responding

MESSAGES = {
    "off": "",
    "enabling": "Waiting for permission from Windows…",
    "disabling": "Turning the sensor helper off…",
    "starting": "Starting the sensor helper…",
    "on": "Reading the processor’s and the motherboard’s own sensors.",
    "no_driver": "The PawnIO driver is not installed. Install it from pawnio.eu and the readings appear by themselves.",
    "not_elevated": "The helper started without administrator rights, so the sensors stay out of reach. "
                    "Turn it off and on again from an administrator account.",
    "error": "The sensor engine reported a problem.",
    "not_responding": "The sensor helper did not start. Turn it off and on again.",
}
_EXIT_MESSAGES = {
    CANCELLED: "Permission was not given, so nothing was changed.",
    FAILED: "Windows could not start the request for permission.",
    helper.EXIT_NOT_ELEVATED: "Windows did not grant administrator rights.",
    helper.EXIT_OTHER_ACCOUNT: "Permission was given with a different account. The helper has to be turned on "
                               "from an account that is an administrator itself.",
    helper.EXIT_TASK_FAILED: "Windows refused to register the helper.",
}


# ------------------------------------------------------------------- Windows
def _schtasks(*args: str) -> int:
    try:
        return subprocess.run(["schtasks", *args], capture_output=True, timeout=20,
                              creationflags=_NO_WINDOW).returncode
    except Exception:
        return FAILED


def task_installed() -> bool:
    return _schtasks("/query", "/tn", helper.TASK_NAME) == 0


def start_task() -> bool:
    return _schtasks("/run", "/tn", helper.TASK_NAME) == 0


def pawnio_installed() -> bool:
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\PawnIO").Close()
        return True
    except OSError:
        return False


def engine_available() -> bool:
    return (helper.ENGINE_DIR / helper.ENGINE_DLL).is_file()


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def run_elevated(args: list[str], timeout_s: float = 90.0) -> int:
    """Run the helper with ``args`` elevated (one Windows permission prompt) and
    return its exit code, ``CANCELLED`` when the prompt was declined."""
    command, prefix = helper.launch_command()
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)       # apartment-threaded, as ShellExecute expects
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        info = _ShellExecuteInfo()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = 0x40 | 0x100                                 # keep the process handle; no async
        info.lpVerb = "runas"
        info.lpFile = command
        info.lpParameters = subprocess.list2cmdline(prefix + args)
        info.nShow = 0
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            return CANCELLED if ctypes.get_last_error() == 1223 else FAILED
        if not info.hProcess:
            return FAILED
        try:
            if k32.WaitForSingleObject(info.hProcess, int(timeout_s * 1000)) != 0:
                return FAILED
            code = wintypes.DWORD(0)
            k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
            return int(code.value)
        finally:
            k32.CloseHandle(info.hProcess)
    except Exception:
        return FAILED


# -------------------------------------------------------------------- client
def derive_state(payload: dict | None, installed: bool, since_start: float | None, busy: str | None) -> str:
    """One word for where the helper stands, from what was observed."""
    if busy:
        return busy
    if payload is not None:
        return {"ok": "on", "starting": "starting", "no_driver": "no_driver",
                "not_elevated": "not_elevated"}.get(payload.get("state"), "error")
    if installed:
        return "starting" if since_start is None or since_start < _START_WINDOW_S else "not_responding"
    return "off"


class HelperClient:
    """Keeps the helper running while RigWatch is open and holds its latest readings.

    All waiting happens on its own thread, so the sampler never blocks on it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._port: int | None = None
        self._scan = 0
        self._payload: dict | None = None
        self._installed = False
        self._driver_installed = False
        self._started_at: float | None = None
        self._attempts = 0
        self._busy: str | None = None
        self._checked_at = 0.0

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rigwatch-helper-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _refresh_install_state(self) -> None:
        installed, driver = task_installed(), pawnio_installed()
        with self._lock:
            self._installed, self._driver_installed = installed, driver
            self._checked_at = time.monotonic()

    def _poll(self) -> dict | None:
        if self._port is not None:
            data = helper.fetch(self._port, timeout=1.0)
            if data is not None:
                return data
            self._port = None
        self._scan += 1
        # The helper takes the first free port, so the others only matter now and then.
        for port in (helper.PORTS if self._scan % 5 == 1 else helper.PORTS[:1]):
            data = helper.fetch(port, timeout=0.4)
            if data is not None:
                self._port = port
                return data
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._checked_at > 30.0:
                self._refresh_install_state()
            payload = self._poll()
            with self._lock:
                self._payload = payload
                installed, busy, started = self._installed, self._busy, self._started_at
                if payload is not None:
                    self._attempts = 0
            if payload is not None:
                delay = 1.0
            elif installed and not busy:
                # Start it; if it keeps failing (the app was moved, say), stop hammering Windows.
                retry_after = 20.0 if self._attempts < 3 else 300.0
                if started is None or now - started > retry_after:
                    start_task()
                    with self._lock:
                        self._started_at = time.monotonic()
                        self._attempts += 1
                delay = 1.0 if self._attempts <= 3 else 5.0
            else:
                delay = 10.0
            self._wake.wait(delay)
            self._wake.clear()

    # ----------------------------------------------------------------- reads
    def readings(self) -> list[dict]:
        """Sensor readings, only while the helper says they are real measurements."""
        with self._lock:
            payload = self._payload
        if not payload or payload.get("state") != "ok":
            return []
        rows = payload.get("readings")
        return rows if isinstance(rows, list) else []

    def status(self) -> dict:
        with self._lock:
            payload, installed, busy = self._payload, self._installed, self._busy
            driver_installed = self._driver_installed
            since = None if self._started_at is None else time.monotonic() - self._started_at
        state = derive_state(payload, installed, since, busy)
        message = MESSAGES.get(state, "")
        if state == "error" and payload and payload.get("error"):
            message = f"{message} ({payload['error']})"
        return {
            "state": state,
            "message": message,
            "installed": installed,
            "driver_installed": driver_installed or bool(payload and payload.get("driver") == "ok"),
            "engine_available": engine_available(),
            "engine_version": payload.get("engine") if payload else None,
            "sensors": len(payload.get("readings") or []) if payload and payload.get("state") == "ok" else 0,
            "driver_url": PAWNIO_URL,
        }

    # --------------------------------------------------------------- actions
    def _begin(self, what: str) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = what
            return True

    def _end(self) -> None:
        with self._lock:
            self._busy = None
        self._refresh_install_state()
        self._wake.set()

    def enable(self) -> dict:
        if not engine_available():
            return {"ok": False, "note": "This copy of RigWatch was built without the sensor engine.", **self.status()}
        if not self._begin("enabling"):
            return {"ok": False, "note": "A permission request is already open.", **self.status()}
        try:
            sid = helper.current_sid()
            code = run_elevated(["install"] + (["--sid", sid] if sid else []))
        finally:
            with self._lock:
                self._started_at, self._attempts = None, 0
            self._end()
        with self._lock:
            installed = self._installed
        ok = code == helper.EXIT_OK and installed
        note = None if ok else _EXIT_MESSAGES.get(code, "Turning the helper on did not work.")
        return {"ok": ok, "note": note, **self.status()}

    def disable(self) -> dict:
        if not self._begin("disabling"):
            return {"ok": False, "note": "A permission request is already open.", **self.status()}
        code = helper.EXIT_OK
        try:
            if task_installed():
                # Removing the task without a prompt works on some setups; ask only when it does not.
                _schtasks("/end", "/tn", helper.TASK_NAME)
                if _schtasks("/delete", "/tn", helper.TASK_NAME, "/f") != 0 or task_installed():
                    code = run_elevated(["uninstall"])
            if not task_installed():
                with self._lock:
                    port = self._port
                for candidate in ([port] if port else list(helper.PORTS[:1])):
                    helper.fetch(candidate, "/v1/stop", timeout=1.0)
                with self._lock:
                    self._payload, self._port = None, None
        finally:
            self._end()
        with self._lock:
            installed = self._installed
        ok = not installed
        note = None if ok else _EXIT_MESSAGES.get(code, "Turning the helper off did not work.")
        return {"ok": ok, "note": note, **self.status()}
