"""Entry point: ``python3 -m glaze [options]``."""

from __future__ import annotations

import signal
import socket
import sys
import time

from .app import GlazeApp
from .config import config_from_args


def _local_ip():
    """Best-effort LAN address, so the console prints a URL you can click."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main(argv=None):
    cfg = config_from_args(argv)
    app = GlazeApp(cfg)
    app.start()

    host = _local_ip() if cfg.host in ("0.0.0.0", "") else cfg.host
    print("Glaze eye tracker running", flush=True)
    print("  eye camera   : %s" % (cfg.eye_source or "disabled"))
    print("  scene camera : %s" % (cfg.scene_source or "disabled"))
    print("  processing   : %dx%d%s" % (cfg.proc_width, cfg.proc_height,
                                        " (ROI mode)" if cfg.roi_mode else ""))
    print("  open on your laptop: http://%s:%d/" % (host, cfg.port))
    if app.notice:
        print("  notice: %s" % app.notice)
    print("Ctrl+C to stop.", flush=True)

    stop = {"requested": False}

    def handle_signal(signum, frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (AttributeError, ValueError):
        pass

    try:
        while not stop["requested"]:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nstopping...")
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
