import sys
import traceback


def _run() -> int:
    try:
        from .app import main
        return main()
    except SystemExit:
        raise
    except Exception:
        # When started without a console (pythonw) a crash would be invisible: keep a log.
        try:
            from .settings import data_dir
            (data_dir() / "rigwatch.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise


sys.exit(_run())
