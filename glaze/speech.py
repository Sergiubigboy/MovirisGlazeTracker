"""Text-to-speech through a specific audio device.

Uses ``espeak-ng`` (apt-installable, tiny, no model download) piped into
``aplay`` targeting a specific ALSA card - not whatever the system's current
default output happens to be. That matters here because the Pi likely has more
than one audio sink (its own onboard jack, HDMI, and now the USB speaker), and
"say it on the USB speaker" only works if we name that device explicitly
instead of hoping it became the default.

For nicer voices later, this is the one place to swap in Piper TTS - the
``speak()`` call site elsewhere in the codebase would not need to change.
"""

from __future__ import annotations

import re
import subprocess
import threading


def list_playback_devices():
    """Parse ``aplay -l``. Returns [{"card": "1", "name": "..."}]."""
    try:
        result = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []

    devices = []
    for line in result.stdout.splitlines():
        match = re.match(r"card (\d+): \S+ \[(.+?)\]", line)
        if match:
            devices.append({"card": match.group(1), "name": match.group(2)})
    return devices


def detect_output_device():
    """Best-guess playback device, in priority order.

    1. A USB sound card (the speaker itself carries the audio over USB).
    2. The Pi's own onboard headphone jack - just as common a setup: a
       USB-*powered* speaker with a plain analogue jack input has no USB
       audio interface at all, the signal comes from the Pi's own jack.
    3. Nothing else: HDMI is skipped, since on a headless Pi it is almost
       never actually connected to anything that plays sound.

    Returns an ALSA device string like ``plughw:CARD=1,DEV=0``, or ``None``.
    """
    devices = list_playback_devices()

    for device in devices:
        if "usb" in device["name"].lower():
            return "plughw:CARD=%s,DEV=0" % device["card"]

    for device in devices:
        name = device["name"].lower()
        if "headphone" in name or "bcm2835" in name or "jack" in name:
            return "plughw:CARD=%s,DEV=0" % device["card"]

    return None


def speak(text, device=None, voice="ro", rate=165, blocking=False):
    """Speak ``text`` through ``device`` (auto-detected USB speaker if None).

    Fire-and-forget by default: runs in a background thread so a slow or
    stuck audio pipeline can never stall the caller (the tracking loop, or an
    HTTP request handler). Returns True if playback was started, False if no
    audio device could be found or espeak-ng is missing.
    """
    text = (text or "").strip()
    if not text:
        return False

    target = device or detect_output_device()
    if not target:
        return False

    def run():
        try:
            espeak = subprocess.Popen(
                ["espeak-ng", "-v", voice, "-s", str(rate), "--stdout", text],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            player = subprocess.Popen(
                ["aplay", "-q", "-D", target],
                stdin=espeak.stdout, stderr=subprocess.DEVNULL)
            espeak.stdout.close()  # let player get SIGPIPE if espeak dies first
            player.wait(timeout=15)
            espeak.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if blocking:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()
    return True
