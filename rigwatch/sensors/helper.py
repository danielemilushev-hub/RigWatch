"""Sensor helper: reads the sensors Windows keeps behind administrator rights.

CPU core temperature, motherboard temperatures, case fans and rail voltages live
in processor registers and Super I/O chips that only a kernel driver can reach.
This helper runs elevated, drives LibreHardwareMonitor's sensor engine (which
talks to the PawnIO driver) and serves the readings to RigWatch on this computer.

It only ever reads, and it does not import the rest of RigWatch, so the code that
runs with administrator rights is this one file plus the sensor engine.

    serve                  run the service (what the scheduled task starts)
    probe [--out FILE]     read everything once, as JSON
    install --sid SID      register the on-demand scheduled task   (elevated)
    uninstall              remove it                               (elevated)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

APP_ID = "rigwatch-sensor-helper"
PROTOCOL = 1
PORTS = tuple(range(47116, 47121))
TASK_NAME = "RigWatch Sensor Helper"
ENGINE_DIR = Path(__file__).resolve().parents[1] / "vendor" / "lhm"
ENGINE_DLL = "LibreHardwareMonitorLib.dll"

IDLE_EXIT_S = 25.0        # nobody asked for readings for this long: RigWatch was closed
STARTUP_GRACE_S = 60.0
SAMPLE_S = 1.0

# Exit codes of ``install`` / ``uninstall``.
EXIT_OK, EXIT_NOT_ELEVATED, EXIT_OTHER_ACCOUNT, EXIT_TASK_FAILED = 0, 2, 3, 4

_PAWNIO_DEVICE = "\\\\?\\GLOBALROOT\\Device\\PawnIO"
_NO_WINDOW = 0x08000000

# LibreHardwareMonitor sensor type -> (kind, unit) as the rest of RigWatch names them.
_KINDS = {
    "Temperature": ("temp", "°C"), "Fan": ("fan", "RPM"), "Voltage": ("voltage", "V"),
    "Control": ("control", "%"), "Current": ("current", "A"), "Power": ("power", "W"),
}
# RigWatch already measures processor load, clock and power itself (and the same
# way Task Manager does), so only take what it cannot get: temperature, voltage.
_CPU_KINDS = ("temp", "voltage")


# ------------------------------------------------------------------ privileges
def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def driver_state() -> str:
    """``ok`` when the PawnIO device opens, ``denied`` without administrator
    rights, ``missing`` when the driver is not installed."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = k32.CreateFileW(_PAWNIO_DEVICE, 0xC0000000, 3, None, 3, 0, None)   # read|write, open existing
    if handle in (None, wintypes.HANDLE(-1).value):
        return "denied" if ctypes.get_last_error() == 5 else "missing"
    k32.CloseHandle(handle)
    return "ok"


def current_sid() -> str | None:
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             timeout=10, creationflags=_NO_WINDOW).stdout.strip()
        sid = out.rsplit(",", 1)[-1].strip().strip('"')
        return sid if sid.startswith("S-1-") else None
    except Exception:
        return None


# ---------------------------------------------------------------------- engine
def plausible(kind: str, value: float) -> bool:
    """A reading must look like a measurement. A failed read comes back as 0 or
    as a sentinel, and RigWatch never shows those as if they were real."""
    if value != value:                       # NaN
        return False
    if kind == "temp":
        return 0.0 < value < 150.0
    if kind == "fan":
        return 0.0 <= value < 30000.0
    if kind == "voltage":
        return 0.0 < value < 30.0
    if kind == "control":
        return 0.0 <= value <= 100.0
    return value >= 0.0


class Engine:
    """LibreHardwareMonitor's hardware layer, loaded in-process through pythonnet."""

    def __init__(self, lib_dir: Path = ENGINE_DIR) -> None:
        lib_dir = Path(lib_dir)
        dll = lib_dir / ENGINE_DLL
        if not dll.is_file():
            raise FileNotFoundError(f"sensor engine not found in {lib_dir}")
        if str(lib_dir) not in sys.path:
            sys.path.append(str(lib_dir))       # lets .NET resolve the engine's own dependencies
        import clr  # pythonnet
        clr.AddReference(str(dll))
        from LibreHardwareMonitor.Hardware import Computer  # type: ignore
        from System.Reflection import Assembly  # type: ignore
        try:
            v = Assembly.LoadFrom(str(dll)).GetName().Version
            self.version = f"{v.Major}.{v.Minor}.{v.Build}"
        except Exception:
            self.version = None
        self._fans_seen: set[str] = set()
        self._computer = Computer()
        self._computer.IsCpuEnabled = True
        self._computer.IsMotherboardEnabled = True
        self._computer.Open()

    def read(self) -> list[dict]:
        out: list[dict] = []
        for hw in self._computer.Hardware:
            try:
                self._walk(hw, str(hw.HardwareType) == "Cpu", out)
            except Exception:
                continue                         # one misbehaving chip must not hide the others
        return out

    def _walk(self, hw, is_cpu: bool, out: list[dict]) -> None:
        hw.Update()
        name = str(hw.Name)
        for sensor in hw.Sensors:
            kind_unit = _KINDS.get(str(sensor.SensorType))
            if not kind_unit:
                continue
            kind, unit = kind_unit
            if is_cpu and kind not in _CPU_KINDS:
                continue
            raw = sensor.Value
            if raw is None:
                continue
            value = float(raw)
            label = str(sensor.Name)
            if not plausible(kind, value) or "tjmax" in label.lower():   # "distance to TjMax" is not a temperature
                continue
            sid = str(sensor.Identifier)
            if kind == "fan":
                # An empty fan header reads 0 forever; a fan that has spun and then stops is news.
                if value > 0:
                    self._fans_seen.add(sid)
                elif sid not in self._fans_seen:
                    continue
            out.append({"id": sid, "hardware": name, "is_cpu": is_cpu, "kind": kind,
                        "label": label, "value": round(value, 3), "unit": unit})
        for sub in hw.SubHardware:
            self._walk(sub, is_cpu, out)

    def close(self) -> None:
        try:
            self._computer.Close()
        except Exception:
            pass


# --------------------------------------------------------------------- service
class Service:
    """Samples on one thread; the HTTP side only ever hands out the last result."""

    def __init__(self, lib_dir: Path = ENGINE_DIR) -> None:
        self._lib_dir = lib_dir
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._last_request = time.monotonic()
        self._stop = threading.Event()
        self._payload = self._make("starting", [], None, None)

    def _make(self, state: str, readings: list[dict], engine: str | None, error: str | None,
              driver: str | None = None) -> dict:
        return {"app": APP_ID, "protocol": PROTOCOL, "state": state, "elevated": is_elevated(),
                "driver": driver, "engine": engine, "error": error, "t": time.time(), "readings": readings}

    def payload(self) -> dict:
        with self._lock:
            self._last_request = time.monotonic()
            return self._payload

    def stop(self) -> None:
        self._stop.set()

    def _publish(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload

    def _idle(self) -> bool:
        now = time.monotonic()
        with self._lock:
            return now - self._last_request > IDLE_EXIT_S and now - self._started > STARTUP_GRACE_S

    def run(self) -> None:
        """Sampling loop. Returns when RigWatch has gone away or ``stop`` was called."""
        engine: Engine | None = None
        try:
            while not self._stop.is_set() and not self._idle():
                if engine is None:
                    driver = driver_state()
                    if driver != "ok":
                        # ok -> the engine can open the driver. Anything else: say so and look again,
                        # so installing PawnIO later is picked up without restarting anything.
                        self._publish(self._make("no_driver" if driver == "missing" else "not_elevated",
                                                 [], None, None, driver))
                        self._stop.wait(5.0)
                        continue
                    try:
                        engine = Engine(self._lib_dir)
                        engine.read()            # first pass primes the chips; values settle on the next
                    except Exception as exc:
                        self._publish(self._make("error", [], None, f"{type(exc).__name__}: {exc}", driver))
                        self._stop.wait(15.0)
                        continue
                else:
                    try:
                        readings = engine.read()
                        self._publish(self._make("ok", readings, engine.version, None, "ok"))
                    except Exception as exc:
                        self._publish(self._make("error", [], engine.version, f"{type(exc).__name__}: {exc}", "ok"))
                self._stop.wait(SAMPLE_S)
        finally:
            if engine is not None:
                engine.close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    service: Service

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost") or self.headers.get("Origin"):
            return self._send(403, {"error": "forbidden"})          # local programs only, never a web page
        if self.path == "/v1/readings":
            return self._send(200, self.service.payload())
        if self.path == "/v1/stop":
            self.service.stop()
            return self._send(200, {"app": APP_ID, "stopping": True})
        return self._send(404, {"error": "not found"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False       # on Windows, "reuse" would let two helpers share one port


def fetch(port: int, path: str = "/v1/readings", timeout: float = 0.5) -> dict | None:
    """Ask a helper on ``port``; ``None`` when nothing there answers as one."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) and data.get("app") == APP_ID else None
    except Exception:
        return None


def serve(lib_dir: Path = ENGINE_DIR) -> int:
    service = Service(lib_dir)
    handler = type("Handler", (_Handler,), {"service": service})
    server = None
    for port in PORTS:
        try:
            server = _Server(("127.0.0.1", port), handler)
            break
        except OSError:
            if fetch(port) is not None:
                return 0                          # a helper is already serving: one is enough
    if server is None:
        return 1
    threading.Thread(target=server.serve_forever, name="helper-http", daemon=True).start()
    try:
        service.run()
    finally:
        server.shutdown()
        server.server_close()
    return 0


def probe(lib_dir: Path = ENGINE_DIR, out: str | None = None) -> int:
    result: dict = {"elevated": is_elevated(), "driver": driver_state(), "engine": None,
                    "error": None, "readings": []}
    try:
        engine = Engine(lib_dir)
        result["engine"] = engine.version
        engine.read()
        time.sleep(1.2)
        result["readings"] = engine.read() if result["driver"] == "ok" else []
        engine.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if result["error"] is None else 1


# -------------------------------------------------------------- scheduled task
def launch_command() -> tuple[str, list[str]]:
    """How this helper is started: the frozen RigWatch.exe, or Python on this file."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--sensor-helper"]
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    # -I: ignore PYTHON* environment variables and the user's site-packages.
    return str(windowless if windowless.is_file() else exe), ["-I", str(Path(__file__).resolve())]


_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Reads CPU temperature, fan and voltage sensors for the RigWatch hardware monitor. Runs only while RigWatch is open.</Description>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_xml(sid: str, command: str, arguments: list[str]) -> str:
    return _TASK_XML.format(sid=escape(sid), command=escape(command),
                            arguments=escape(subprocess.list2cmdline(arguments)),
                            workdir=escape(str(Path(command).parent)))


def install(sid: str | None) -> int:
    """Register the task that lets RigWatch start this helper without another prompt."""
    if not is_elevated():
        return EXIT_NOT_ELEVATED
    mine = current_sid()
    if not mine or (sid and sid != mine):
        # Permission was given with a different account's password. A task for that
        # account could not be started from the account RigWatch runs in.
        return EXIT_OTHER_ACCOUNT
    command, prefix = launch_command()
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="rigwatch-task-")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as fh:
            fh.write(task_xml(mine, command, prefix + ["serve"]))
        done = subprocess.run(["schtasks", "/create", "/tn", TASK_NAME, "/xml", path, "/f"],
                              capture_output=True, timeout=30, creationflags=_NO_WINDOW)
        return EXIT_OK if done.returncode == 0 else EXIT_TASK_FAILED
    except Exception:
        return EXIT_TASK_FAILED
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def uninstall() -> int:
    if not is_elevated():
        return EXIT_NOT_ELEVATED
    try:
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
        subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], capture_output=True, timeout=30,
                       creationflags=_NO_WINDOW)
        still = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME], capture_output=True, timeout=30,
                               creationflags=_NO_WINDOW)
        return EXIT_TASK_FAILED if still.returncode == 0 else EXIT_OK
    except Exception:
        return EXIT_TASK_FAILED


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rigwatch-sensor-helper",
                                     description="Reads the sensors Windows keeps behind administrator rights.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--lib-dir", default=str(ENGINE_DIR), help="folder holding the sensor engine")
        if name == "probe":
            p.add_argument("--out", default=None, help="write the JSON here instead of printing it")
    sub.add_parser("install").add_argument("--sid", default=None)
    sub.add_parser("uninstall")
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return serve(Path(args.lib_dir))
    if args.cmd == "probe":
        return probe(Path(args.lib_dir), args.out)
    if args.cmd == "install":
        return install(args.sid)
    return uninstall()


if __name__ == "__main__":
    sys.exit(main())
