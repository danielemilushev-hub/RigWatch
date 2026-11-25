"""Start with Windows.

One value under the signed-in user's own ``Run`` key, which is the list Task
Manager shows under "Startup apps". It needs no administrator rights and adds
no service or scheduled task. Turning the switch off deletes the value again.

Windows keeps its own on/off choice for every start-up entry (the switch in
Task Manager). That choice is read too, so the app never claims to start with
Windows while Windows itself has the entry switched off.
"""

from __future__ import annotations

import ctypes
import re
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "RigWatch"
FLAG = "--minimized"
MAX_COMMAND = 260          # Windows ignores Run entries longer than this
DRIVE_REMOVABLE = 2
EXTERNAL_BUSES = ("USB", "SD", "MMC")


def launch_command() -> str | None:
    """The command Windows should run at sign-in for this copy of RigWatch.

    ``None`` when this copy cannot be registered (its path is too long for a
    Run entry, or a source checkout is missing its launcher).
    """
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}" {FLAG}'
    else:
        python = Path(sys.executable)
        windowless = python.with_name("pythonw.exe")
        entry = Path(__file__).resolve().parent.parent / "tools" / "rigwatch_entry.py"
        if not entry.is_file():
            return None
        command = f'"{windowless if windowless.is_file() else python}" "{entry}" {FLAG}'
    return command if len(command) <= MAX_COMMAND else None


def command_files(command: str) -> list[Path]:
    """The files a Run entry needs: the program, plus a script when there is one."""
    quoted = re.findall(r'"([^"]+)"', command or "")
    if quoted:
        return [Path(q) for q in quoted]
    first = (command or "").strip().split(" ")[0]
    return [Path(first)] if first else []


_external: dict[str, bool] = {}       # drive root -> answer; the bus a drive sits on does not change


def _probe_external(root: str) -> bool:
    try:
        if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_REMOVABLE:
            return True
        # A USB hard disk or SSD reports itself as a fixed disk; its bus tells them apart.
        from .sensors import storage
        disks = set(storage.volume_disks(root[:2]))
        return any(d.get("index") in disks and (d.get("bus") in EXTERNAL_BUSES or d.get("removable"))
                   for d in storage.list_drives())
    except Exception:
        return False


def on_external_drive(path: Path) -> bool:
    """True when the file is on a drive that may be unplugged at sign-in (USB, card reader)."""
    root = path.anchor
    if len(root) < 2 or root[1] != ":":
        return False                   # a network share, or no drive at all
    root = root.upper()
    if root not in _external:
        _external[root] = _probe_external(root)
    return _external[root]


class StartupEntry:
    """RigWatch's line in the user's start-up list."""

    def __init__(self, run_key: str = RUN_KEY, approved_key: str = APPROVED_KEY,
                 name: str = VALUE_NAME, command=launch_command) -> None:
        self.run_key, self.approved_key, self.name = run_key, approved_key, name
        self._command = command

    # ------------------------------------------------------------ registry
    def _read(self, key: str):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                return winreg.QueryValueEx(handle, self.name)[0]
        except (OSError, AttributeError):
            return None

    def _delete(self, key: str) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, self.name)
        except (OSError, AttributeError):
            pass

    def _write(self, command: str) -> bool:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.run_key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.SetValueEx(handle, self.name, 0, winreg.REG_SZ, command)
            return True
        except (OSError, AttributeError):
            return False

    # --------------------------------------------------------------- state
    def registered(self) -> str | None:
        """The command in the start-up list, or None when RigWatch is not in it."""
        value = self._read(self.run_key)
        return value if isinstance(value, str) and value.strip() else None

    def blocked(self) -> bool:
        """True when the entry is switched off in Windows' own Startup apps list."""
        data = self._read(self.approved_key)
        return isinstance(data, bytes) and len(data) > 0 and data[0] % 2 == 1

    def status(self, note: str | None = None) -> dict:
        own = self._command()
        registered = self.registered()
        state = "off" if registered is None else "blocked" if self.blocked() else "on"
        files = command_files(registered) if registered else []
        target = files[0] if files else None
        result = {
            "available": own is not None,
            "state": state,
            "enabled": state == "on",
            "this_copy": bool(registered and own and registered.lower() == own.lower()),
            "target": str(target) if target else None,
            "target_missing": bool(files) and not all(f.is_file() for f in files),
            "external": bool(target) and on_external_drive(target),
        }
        if note:
            result["note"] = note
        return result

    # ------------------------------------------------------------- actions
    def enable(self) -> dict:
        command = self._command()
        if command is None:
            return self.status("RigWatch cannot be added to Windows’ start-up list from this folder: its path is too long.")
        if not self._write(command):
            return self.status("Windows did not accept the start-up entry.")
        self._delete(self.approved_key)        # the user switched it on here: lift an earlier "off" in Task Manager
        return self.status()

    def disable(self) -> dict:
        self._delete(self.run_key)
        self._delete(self.approved_key)
        if self.registered() is not None:
            return self.status("Windows did not let RigWatch remove the start-up entry.")
        return self.status()

    def repair(self) -> bool:
        """Point the entry at this copy when the copy it names no longer exists.

        Covers a RigWatch.exe that was moved or renamed. An entry whose files are
        still there is left alone, even if it names a different copy.
        """
        registered = self.registered()
        if registered is None:
            return False
        if all(f.is_file() for f in command_files(registered)):
            return False
        own = self._command()
        if own is None or own.lower() == registered.lower():
            return False
        return self._write(own)
