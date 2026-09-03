"""Show ALSA playback devices, so you can find the USB speaker's card index.

    python3 tools/list_audio.py

Prints what ``aplay -l`` sees, plus which one glaze/speech.py would
auto-pick (the first card whose name looks like a USB audio device).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glaze.speech import detect_output_device, list_playback_devices  # noqa: E402


def main():
    devices = list_playback_devices()
    if not devices:
        print("no ALSA playback devices found (or 'aplay' is not installed)")
        return

    print("ALSA playback devices (from `aplay -l`)")
    print("-" * 60)
    for device in devices:
        print("  card %s: %s" % (device["card"], device["name"]))

    picked = detect_output_device()
    print()
    if picked:
        print("glaze would auto-select: %s" % picked)
    else:
        print("glaze found no usable output device - set tts_audio_device manually "
             "in /settings (HDMI is never auto-picked)")


if __name__ == "__main__":
    main()
