"""WiFi fallback access point + setup portal for Raspberry Pi OS (NetworkManager).

Runs as its own always-on systemd service (see ``wifi-portal.service``),
independent of the tracker itself: if the Pi cannot reach any known WiFi
network, it puts its own radio into access-point mode and serves a tiny form
at http://10.42.0.1/ where you can type a new network's SSID and password. Once
that connects, the AP goes down and the Pi rejoins your network like normal.

Zero third-party dependencies - only the standard library and ``nmcli``
(NetworkManager's CLI, installed by default on Raspberry Pi OS Bookworm).

    sudo python3 tools/wifi_portal.py

State machine
-------------
CLIENT   - watching ``nmcli device status`` for wlan0. ``fail_threshold``
           consecutive non-connected checks -> AP_MODE.
AP_MODE  - our own AP is up, portal is serving. Every ``recheck_seconds`` the
           AP briefly comes down so NetworkManager's own autoconnect can try
           known networks that came back in range; if nothing connects within
           a few seconds, the AP goes back up.

A submitted SSID/password is applied in a background thread, because the
browser making the request is itself connected to the AP that is about to go
down mid-request - there is no way to hand that request a "success" response,
same limitation every consumer router's WiFi setup page has.
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse

AP_CON_NAME = "glaze-setup-ap"


def run(args, timeout=25):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timed out"
    except OSError as exc:
        return 1, "", str(exc)


class WifiManager:
    def __init__(self, iface, ap_ssid, ap_password):
        self.iface = iface
        self.ap_ssid = ap_ssid
        self.ap_password = ap_password
        self.ap_active = False
        self._lock = threading.Lock()
        self.last_error = ""

    # -- status -----------------------------------------------------------
    def client_is_connected(self):
        """True if wlan0 is associated with a real (non-AP-of-ours) network."""
        code, out, _ = run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"])
        if code != 0:
            return False
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 3 or parts[0] != self.iface:
                continue
            device_state, connection = parts[1], parts[2]
            if device_state == "connected" and connection != AP_CON_NAME:
                return True
        return False

    # -- AP mode ------------------------------------------------------------
    def _ensure_ap_profile(self):
        code, out, _ = run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        if any(line == AP_CON_NAME for line in out.splitlines()):
            return
        run(["nmcli", "connection", "add", "type", "wifi", "ifname", self.iface,
            "con-name", AP_CON_NAME, "autoconnect", "no", "ssid", self.ap_ssid])
        run(["nmcli", "connection", "modify", AP_CON_NAME, "802-11-wireless.mode", "ap"])
        run(["nmcli", "connection", "modify", AP_CON_NAME, "802-11-wireless.band", "bg"])
        run(["nmcli", "connection", "modify", AP_CON_NAME, "ipv4.method", "shared"])
        run(["nmcli", "connection", "modify", AP_CON_NAME, "wifi-sec.key-mgmt", "wpa-psk"])
        run(["nmcli", "connection", "modify", AP_CON_NAME, "wifi-sec.psk", self.ap_password])

    def start_ap(self):
        with self._lock:
            self._ensure_ap_profile()
            code, _, err = run(["nmcli", "connection", "up", AP_CON_NAME])
            self.ap_active = (code == 0)
            if code != 0:
                self.last_error = "could not start AP: %s" % err
            return self.ap_active

    def stop_ap(self):
        with self._lock:
            run(["nmcli", "connection", "down", AP_CON_NAME])
            self.ap_active = False

    # -- connecting to a real network --------------------------------------
    def try_connect(self, ssid, password):
        args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", self.iface]
        if password:
            args += ["password", password]
        code, out, err = run(args, timeout=30)
        if code == 0:
            with self._lock:
                self.ap_active = False
            return True, out
        return False, (err or out or "connection failed")


PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Glaze - configurare WiFi</title>
<style>
  body {{ margin:0; background:#0d1117; color:#e6edf3; display:flex; min-height:100vh;
         align-items:center; justify-content:center;
         font:15px/1.5 ui-monospace, Menlo, Consolas, monospace; }}
  .card {{ background:#161b22; border:1px solid #262d36; border-radius:10px;
          padding:28px; width:min(92vw, 380px); }}
  h1 {{ font-size:16px; letter-spacing:.08em; text-transform:uppercase; margin:0 0 4px; }}
  p.sub {{ color:#8b949e; margin:0 0 20px; font-size:13px; }}
  label {{ display:block; font-size:12px; color:#8b949e; margin:14px 0 4px; }}
  input {{ width:100%; box-sizing:border-box; background:#21262d; border:1px solid #262d36;
          border-radius:6px; color:#e6edf3; padding:9px 10px; font:inherit; }}
  button {{ width:100%; margin-top:20px; background:#21262d; border:1px solid #4ade80;
           color:#4ade80; border-radius:6px; padding:10px; font:inherit; cursor:pointer; }}
  .msg {{ margin-top:14px; padding:10px; border-radius:6px; font-size:13px; }}
  .msg.ok {{ background:#122117; color:#4ade80; }}
  .msg.err {{ background:#2a1618; color:#f85149; }}
</style></head>
<body>
<div class="card">
  <h1>Glaze</h1>
  <p class="sub">Pi-ul nu a găsit nicio rețea cunoscută. Introdu datele rețelei tale.</p>
  {message}
  <form method="POST">
    <label>Nume rețea (SSID)</label>
    <input type="text" name="ssid" required autofocus>
    <label>Parolă</label>
    <input type="password" name="password">
    <button type="submit">Conectează</button>
  </form>
</div>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    manager: WifiManager = None  # bound by serve()

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._send(PAGE_TEMPLATE.format(message=""))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        fields = urllib.parse.parse_qs(body)
        ssid = (fields.get("ssid") or [""])[0].strip()
        password = (fields.get("password") or [""])[0]

        if not ssid:
            self._send(PAGE_TEMPLATE.format(
                message='<div class="msg err">Numele rețelei nu poate fi gol.</div>'))
            return

        threading.Thread(target=self._attempt, args=(ssid, password), daemon=True).start()

        message = ('<div class="msg ok">Se încearcă conectarea la <b>%s</b>. '
                  "Pagina asta se va închide dacă merge - "
                  "verifică pe telefon/laptop dacă Pi-ul a apărut pe rețeaua nouă. "
                  "Dacă nu, revino aici în ~20 secunde și reîncearcă.</div>"
                  % html.escape(ssid))
        self._send(PAGE_TEMPLATE.format(message=message))

    def _attempt(self, ssid, password):
        time.sleep(1.5)  # let the HTTP response above reach the browser first
        ok, detail = self.manager.try_connect(ssid, password)
        if not ok:
            print("connect to %r failed: %s" % (ssid, detail), file=sys.stderr, flush=True)
            self.manager.start_ap()  # portal stays reachable for another try
        else:
            print("connected to %r" % ssid, flush=True)

    def _send(self, body_html):
        body = body_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve_portal(manager, port):
    handler = type("BoundHandler", (Handler,), {"manager": manager})
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iface", default="wlan0")
    parser.add_argument("--ap-ssid", default="Glaze-Setup")
    parser.add_argument("--ap-password", default="glaze1234",
                        help="min 8 chars (WPA2 requirement)")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--check-interval", type=float, default=10.0,
                        help="seconds between connectivity checks in client mode")
    parser.add_argument("--fail-threshold", type=int, default=3,
                        help="consecutive failed checks before starting the AP")
    parser.add_argument("--recheck-seconds", type=float, default=120.0,
                        help="how often to drop the AP and retry known networks")
    args = parser.parse_args()

    if len(args.ap_password) < 8:
        print("--ap-password must be at least 8 characters (WPA2)", file=sys.stderr)
        return 1

    manager = WifiManager(args.iface, args.ap_ssid, args.ap_password)
    server = None
    fails = 0

    print("watching interface %s, checking every %.0fs" % (args.iface, args.check_interval),
         flush=True)

    while True:
        if manager.ap_active:
            time.sleep(args.recheck_seconds)
            print("AP up %.0fs - briefly dropping it to retry known networks"
                 % args.recheck_seconds, flush=True)
            manager.stop_ap()
            time.sleep(8)  # give NetworkManager's own autoconnect a chance
            if manager.client_is_connected():
                print("a known network reconnected, staying off AP", flush=True)
                if server is not None:
                    server.shutdown()
                    server = None
                fails = 0
            else:
                manager.start_ap()
            continue

        if manager.client_is_connected():
            fails = 0
            time.sleep(args.check_interval)
            continue

        fails += 1
        print("no network (%d/%d)" % (fails, args.fail_threshold), flush=True)
        if fails < args.fail_threshold:
            time.sleep(args.check_interval)
            continue

        print("starting fallback AP '%s' - connect and open http://10.42.0.1/"
             % args.ap_ssid, flush=True)
        if manager.start_ap():
            server = serve_portal(manager, args.port)
        else:
            print("failed to start AP: %s" % manager.last_error, file=sys.stderr, flush=True)
            time.sleep(args.check_interval)


if __name__ == "__main__":
    sys.exit(main())
