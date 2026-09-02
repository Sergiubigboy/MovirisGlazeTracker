#!/usr/bin/env bash
# Setup for Raspberry Pi OS Lite (Bookworm) on a Pi 3A+.
#
# Deliberately uses apt packages instead of pip: building numpy/OpenCV wheels
# on a 512 MB Pi either takes hours or runs out of memory. The apt builds are
# compiled for this hardware and share memory with the rest of the system.
#
#   chmod +x install_pi.sh && ./install_pi.sh

set -euo pipefail

echo "==> Updating package lists"
sudo apt-get update

echo "==> Installing OpenCV, NumPy and picamera2"
sudo apt-get install -y \
    python3-opencv \
    python3-numpy \
    python3-picamera2 \
    v4l-utils

echo "==> Checking imports"
python3 - <<'PY'
import cv2, numpy
print("  opencv", cv2.__version__)
print("  numpy ", numpy.__version__)
try:
    import picamera2
    print("  picamera2 ok")
except ImportError as exc:
    print("  picamera2 MISSING:", exc)
PY

echo
echo "==> Detected cameras"
python3 tools/list_cameras.py || true

cat <<'EOF'

Done. Start the tracker with:

    python3 -m glaze --preset lite --eye usb:0 --scene csi

Then open http://<pi-ip>:8000/ from your laptop.

To run it at boot:

    sudo cp glaze.service /etc/systemd/system/
    sudo nano /etc/systemd/system/glaze.service   # check User= and WorkingDirectory=
    sudo systemctl enable --now glaze
    journalctl -u glaze -f
EOF
