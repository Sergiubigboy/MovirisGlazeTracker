"""Standard-library web server for the tracker.

No Flask, no aiohttp: a 512 MB Pi 3A+ should not spend RAM on a framework to
serve one page and two MJPEG streams. ``ThreadingHTTPServer`` handles the
browser's parallel connections (page + eye stream + scene stream + SSE) fine.

Routes
------
GET  /                 dashboard
GET  /eye.mjpg         annotated eye camera stream
GET  /scene.mjpg       scene camera stream with the gaze marker
GET  /events           server-sent events, live gaze JSON
GET  /api/state        one JSON snapshot
GET  /api/config       effective configuration
GET  /api/cameras      probe /dev/video*
GET  /gaze_vector.txt  the six upstream values, for other programs
POST /api/command      {"action": "...", ...}
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
BOUNDARY = "glazeframe"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GlazeTracker/1.0"
    hub = None  # injected by serve()

    # Keep the console readable on a headless Pi.
    def log_message(self, fmt, *args):
        pass

    # -- helpers --------------------------------------------------------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, content_type="text/plain; charset=utf-8", status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ---------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            return self._serve_static_page("index.html")
        if path in ("/settings", "/settings.html"):
            return self._serve_static_page("settings.html")
        if path in ("/gestures", "/gestures.html"):
            return self._serve_static_page("gestures.html")
        if path == "/eye.mjpg":
            return self._serve_mjpeg("eye")
        if path == "/scene.mjpg":
            return self._serve_mjpeg("scene")
        if path == "/events":
            return self._serve_events()
        if path == "/api/state":
            return self._send_json(self.hub.state())
        if path == "/api/config":
            return self._send_json(self.hub.config_dict())
        if path == "/api/cameras":
            return self._send_json({"devices": self.hub.list_cameras()})
        if path == "/api/calibration":
            return self._send_json(self.hub.calibration.to_dict())
        if path == "/gaze_vector.txt":
            return self._send_text(self.hub.gaze_vector_line())
        if path == "/favicon.ico":
            return self._send_text("", "image/x-icon", 404)

        self._send_text("not found", status=404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/command":
            return self._send_text("not found", status=404)

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return self._send_json({"ok": False, "error": "bad JSON: %s" % exc}, 400)

        action = payload.get("action")
        if not action:
            return self._send_json({"ok": False, "error": "missing action"}, 400)

        try:
            result = self.hub.command(action, payload)
        except Exception as exc:  # a bad command must not kill the server
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        return self._send_json(result)

    # -- payloads -------------------------------------------------------
    def _serve_static_page(self, filename):
        file_path = os.path.join(STATIC_DIR, filename)
        try:
            with open(file_path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._send_text("static/%s is missing" % filename, status=500)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_mjpeg(self, which):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        self.hub.stream_opened(which)
        sequence = None
        try:
            while not self.hub.stopping:
                jpeg, sequence = self.hub.get_jpeg(which, sequence, timeout=2.0)
                if jpeg is None:
                    continue
                header = ("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                          % (BOUNDARY, len(jpeg))).encode("ascii")
                self.wfile.write(header)
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # browser navigated away
        finally:
            self.hub.stream_closed(which)

    def _serve_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while not self.hub.stopping:
                payload = json.dumps(self.hub.state())
                self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                time.sleep(1.0 / max(1.0, self.hub.event_rate))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # A browser opens 3-4 long-lived connections; a couple of extra viewers
    # would otherwise starve the Pi.
    request_queue_size = 16


def serve(hub, host="0.0.0.0", port=8000):
    """Start the HTTP server in a background thread and return it."""
    handler = type("BoundHandler", (Handler,), {"hub": hub})
    server = _Server((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return server
