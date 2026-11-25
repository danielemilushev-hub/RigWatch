"""Local HTTP server: serves the UI and a small JSON API on 127.0.0.1 only."""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .collector import Collector
from .settings import Settings

UI_DIR = Path(__file__).parent / "ui"
DEFAULT_PORT = 47115
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("text/javascript", ".js")


class RigWatchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, collector: Collector, settings: Settings, window_api=None):
        super().__init__(addr, Handler)
        self.collector = collector
        self.settings = settings
        self.window_api = window_api
        self.stream_clients = 0
        self.last_client_seen = time.monotonic()
        self.clients_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: RigWatchServer

    def log_message(self, fmt, *args):  # quiet by default
        pass

    # ------------------------------------------------------------- helpers
    def _local_request(self) -> bool:
        """Reject DNS-rebinding and cross-site requests; this API is local only."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlsplit(origin)
            if (o.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    # ----------------------------------------------------------------- GET
    def do_GET(self):
        if not self._local_request():
            return self._json({"error": "forbidden"}, 403)
        path = urlsplit(self.path).path
        c = self.server.collector
        try:
            if path == "/api/snapshot":
                return self._json(c.snapshot())
            if path == "/api/static":
                return self._json(c.static())
            if path == "/api/history":
                return self._json(c.history())
            if path == "/api/sensors":
                return self._json(c.sensors())
            if path == "/api/settings":
                return self._json(self.server.settings.get())
            if path == "/api/ping":
                return self._json({"app": "rigwatch", "version": __version__,
                                   "clients": self.server.stream_clients})
            if path == "/api/sensors.csv":
                return self._sensors_csv()
            if path == "/api/history.csv":
                return self._history_csv()
            if path == "/api/stream":
                return self._stream()
            return self._static_file(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    do_HEAD = do_GET

    def _static_file(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (UI_DIR / rel).resolve()
        if UI_DIR.resolve() not in target.parents or not target.is_file():
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=86400" if "/assets/" in path else "no-cache"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": cache})

    def _sensors_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["group", "sensor", "unit", "current", "min", "max", "average", "source"])
        for r in self.server.collector.sensors()["rows"]:
            w.writerow([r["group"], r["label"], r["unit"], r["value"], r["min"], r["max"], r["avg"], r["source"]])
        self._send(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                   {"Content-Disposition": 'attachment; filename="rigwatch-sensors.csv"',
                    "Cache-Control": "no-store"})

    def _history_csv(self):
        hist = self.server.collector.history()
        keys = sorted(hist["series"])
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["time"] + keys)
        for i, t in enumerate(hist["t"]):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
            w.writerow([stamp] + [hist["series"][k][i] if i < len(hist["series"][k]) else "" for k in keys])
        self._send(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                   {"Content-Disposition": 'attachment; filename="rigwatch-history.csv"',
                    "Cache-Control": "no-store"})

    def _stream(self):
        """Server-sent events: one snapshot per sample."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        srv = self.server
        with srv.clients_lock:
            srv.stream_clients += 1
        seq = -1
        try:
            while True:
                new_seq, snap = srv.collector.wait_next(seq, timeout=10.0)
                if new_seq == seq:
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    seq = new_seq
                    payload = json.dumps(snap, separators=(",", ":"), default=str)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with srv.clients_lock:
                srv.stream_clients -= 1
                srv.last_client_seen = time.monotonic()

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        if not self._local_request():
            return self._json({"error": "forbidden"}, 403)
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 65536)) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return self._json({"error": "invalid json"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "invalid body"}, 400)

        if path == "/api/settings":
            values = self.server.settings.update(body)
            self.server.collector.set_interval(values["interval"])
            api = self.server.window_api
            if api is not None and "always_on_top" in body:
                api.set_on_top(values["always_on_top"])
            return self._json(values)
        if path == "/api/sensors/reset":
            self.server.collector.reset_stats()
            return self._json({"ok": True})
        if path == "/api/window":
            api = self.server.window_api
            if api is None:
                return self._json({"ok": False, "reason": "no native window"})
            if api.window is None:
                return self._json({"ok": False, "reason": "no native window"})
            if "mini" in body:
                api.set_mini(bool(body["mini"]))
            if body.get("focus"):
                api.focus()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


def start_server(collector: Collector, settings: Settings, port: int | None = None,
                 window_api=None) -> RigWatchServer:
    """Bind to localhost; fall back to a free port when the preferred one is taken."""
    for candidate in ([port] if port else [DEFAULT_PORT]) + [0]:
        try:
            server = RigWatchServer(("127.0.0.1", candidate), collector, settings, window_api)
            break
        except OSError:
            continue
    else:  # pragma: no cover
        raise OSError("could not bind a local port")
    threading.Thread(target=server.serve_forever, name="rigwatch-http", daemon=True).start()
    return server
