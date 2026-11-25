"""User preferences, stored as JSON under %APPDATA%\\RigWatch."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DEFAULTS = {
    "interval": 1.0,          # seconds between samples
    "temp_unit": "C",         # "C" or "F"
    "theme": "system",        # "system" | "dark" | "light"
    "always_on_top": False,
    "window": 60,             # default chart window in seconds
    "taskbar_strip": True,    # show live numbers on the taskbar while minimised
    "strip_pos": None,        # [x, y] once the user has dragged the strip; None = parked
    "privacy": False,         # hide the computer name and network addresses in the UI
}
_ALLOWED = {
    "interval": lambda v: isinstance(v, (int, float)) and 0.25 <= float(v) <= 10,
    "temp_unit": lambda v: v in ("C", "F"),
    "theme": lambda v: v in ("system", "dark", "light"),
    "always_on_top": lambda v: isinstance(v, bool),
    "window": lambda v: v in (60, 300, 900),
    "taskbar_strip": lambda v: isinstance(v, bool),
    "strip_pos": lambda v: v is None or (isinstance(v, list) and len(v) == 2
                                         and all(isinstance(n, int) for n in v)),
    "privacy": lambda v: isinstance(v, bool),
}


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "RigWatch"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = data_dir() / "settings.json"
        self._values = dict(DEFAULTS)
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
            for key, value in stored.items():
                if key in _ALLOWED and _ALLOWED[key](value):
                    self._values[key] = value
        except (OSError, ValueError):
            pass

    def get(self) -> dict:
        with self._lock:
            return dict(self._values)

    def update(self, changes: dict) -> dict:
        """Apply the valid entries of ``changes`` and persist. Returns all settings."""
        with self._lock:
            for key, value in (changes or {}).items():
                if key in _ALLOWED and _ALLOWED[key](value):
                    self._values[key] = value
            try:
                self._path.write_text(json.dumps(self._values, indent=2), encoding="utf-8")
            except OSError:
                pass
            return dict(self._values)
