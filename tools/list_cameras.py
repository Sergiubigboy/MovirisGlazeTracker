"""Show which cameras the Pi can actually see.

    python3 tools/list_cameras.py

Prints every /dev/videoN that opens, the formats V4L2 reports for it (when
v4l2-ctl is installed), and whether picamera2 finds a CSI camera. Use the
numbers it prints for --eye usb:N.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glaze.cameras import list_v4l2_devices  # noqa: E402


def v4l2_formats(index):
    try:
        output = subprocess.run(
            ["v4l2-ctl", "-d", "/dev/video%d" % index, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return output.stdout.strip()


def main():
    print("USB / V4L2 devices")
    print("-" * 60)
    devices = list_v4l2_devices()
    if not devices:
        print("  none found")
    for device in devices:
        print("  index %d  %s  %dx%d  %s" % (
            device["index"], device["path"], device["width"], device["height"],
            "readable" if device["readable"] else "opens but no frames"))
        formats = v4l2_formats(device["index"])
        if formats:
            for line in formats.splitlines():
                stripped = line.strip()
                if stripped.startswith("[") or "Size: Discrete" in stripped:
                    print("      " + stripped)

    print()
    print("CSI camera (picamera2)")
    print("-" * 60)
    try:
        from picamera2 import Picamera2
    except ImportError:
        print("  picamera2 not installed (sudo apt install -y python3-picamera2)")
        return
    try:
        info = Picamera2.global_camera_info()
    except Exception as exc:
        print("  picamera2 error: %s" % exc)
        return
    if not info:
        print("  no CSI camera detected")
    for entry in info:
        print("  %s" % entry)


if __name__ == "__main__":
    main()
