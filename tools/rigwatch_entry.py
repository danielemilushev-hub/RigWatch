"""Entry point for the standalone RigWatch.exe build (see tools/build-exe.ps1)."""

import sys

_CONSOLE_FLAGS = {"--json", "--server", "--browser", "--version", "-h", "--help"}


def _attach_console() -> None:
    """A windowed exe has no console. When a flag that prints is used from a
    terminal, write to that terminal instead of printing into the void."""
    if sys.stdout is not None or not _CONSOLE_FLAGS.intersection(sys.argv[1:]):
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):          # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
    except Exception:
        pass


def main() -> int:
    if sys.argv[1:2] == ["--sensor-helper"]:
        # The elevated sensor helper shares this executable. It is dispatched before
        # anything else is imported, so the app itself never runs with those rights.
        from rigwatch.sensors import helper
        return helper.main(sys.argv[2:])
    _attach_console()
    try:
        from rigwatch.app import main as run
        return run()
    except SystemExit:
        raise
    except Exception:
        import traceback
        try:
            from rigwatch.settings import data_dir
            (data_dir() / "rigwatch.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
