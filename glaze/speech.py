"""Text-to-speech through a specific audio device.

Uses ``espeak-ng`` (apt-installable, tiny, no model download) piped into
``aplay`` targeting a specific ALSA card - not whatever the system's current
default output happens to be. That matters here because the Pi likely has more
than one audio sink (its own onboard jack, HDMI, and now the USB speaker), and
"say it on the USB speaker" only works if we name that device explicitly
instead of hoping it became the default.

On a Windows laptop none of that exists, so the same two calls fall through
to the built-in Windows voice and sound player instead. That is purely so the
whole flow can be tried out at a desk before it goes on the glasses; the Pi
path is untouched and is still the one that matters.

For nicer voices later, this is the one place to swap in Piper TTS - the
``speak()`` call site elsewhere in the codebase would not need to change.
"""

from __future__ import annotations

import base64
import io
import math
import os
import re
import struct
import subprocess
import threading
import wave

IS_WINDOWS = os.name == "nt"
WINDOWS_DEVICE = "windows-default"


def list_playback_devices():
    """Parse ``aplay -l``. Returns [{"card": "1", "name": "..."}]."""
    if IS_WINDOWS:
        return [{"card": WINDOWS_DEVICE, "name": "Windows default output"}]
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
    On Windows there is no card to pick: the system default is the only sink
    worth using, so it short-circuits to a sentinel the players understand.
    """
    if IS_WINDOWS:
        return WINDOWS_DEVICE

    devices = list_playback_devices()

    for device in devices:
        if "usb" in device["name"].lower():
            return "plughw:CARD=%s,DEV=0" % device["card"]

    for device in devices:
        name = device["name"].lower()
        if "headphone" in name or "bcm2835" in name or "jack" in name:
            return "plughw:CARD=%s,DEV=0" % device["card"]

    return None


# Short distinct cues so the wearer can follow what the system is doing
# without looking at a screen - which is the whole point, since the person
# this is built for cannot look away at a dashboard to check.
# (frequency Hz, milliseconds) pairs, played in order.
CUES = {
    "start":    [(660, 90), (880, 140)],           # session began: rising
    "capture":  [(1200, 70)],                      # photo taken: short blip
    "thinking": [(500, 80), (0, 60), (500, 80)],   # sent to the model
    "question": [(760, 60)],                       # a question follows
    "yes":      [(700, 70), (1050, 110)],          # answer registered: rising
    "no":       [(700, 70), (440, 110)],           # falling
    "done":     [(660, 90), (880, 90), (1170, 160)],  # sentence coming
    "error":    [(300, 200), (240, 250)],          # low, unmistakable
}


def _tone_wav(steps, rate=22050, volume=0.35):
    """Build a small WAV in memory from (frequency, ms) steps. 0 Hz = silence."""
    frames = bytearray()
    for frequency, milliseconds in steps:
        count = int(rate * milliseconds / 1000.0)
        for index in range(count):
            if frequency <= 0:
                frames += struct.pack("<h", 0)
                continue
            # Fade both ends of every step, otherwise the abrupt start and
            # stop of the waveform clicks louder than the tone itself.
            fade = min(1.0, index / 120.0, (count - index) / 120.0)
            value = math.sin(2 * math.pi * frequency * index / rate)
            frames += struct.pack("<h", int(value * fade * volume * 32767))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def cue(name, device=None, blocking=True, volume=0.35):
    """Play one of the CUES tones. Returns False if there is no audio device."""
    steps = CUES.get(name)
    if not steps:
        return False

    target = device or detect_output_device()
    if not target:
        return False

    payload = _tone_wav(steps, volume=volume)

    def run():
        if IS_WINDOWS:
            try:
                import winsound
                winsound.PlaySound(payload, winsound.SND_MEMORY)
            except Exception:
                pass
            return
        try:
            player = subprocess.Popen(["aplay", "-q", "-D", target],
                                      stdin=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
            player.communicate(payload, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if blocking:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()
    return True


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
        if IS_WINDOWS:
            _speak_windows(text, voice, rate)
            return
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


def _speak_windows(text, voice="ro", rate=165):
    """Speak through the Windows built-in voice (SAPI).

    The command is handed over base64-encoded UTF-16, which is what
    PowerShell's -EncodedCommand expects, so Romanian diacritics survive the
    trip and no quoting in the sentence can break the call.
    """
    # espeak counts words per minute around 165; SAPI wants -10..10.
    sapi_rate = max(-10, min(10, int(round((rate - 165) / 25.0))))
    wanted = (voice or "ro").split("-")[0].lower()
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate = %d;"
        # Prefer a voice for the requested language; fall back to the default
        # one, which on a Romanian-less Windows means an English accent rather
        # than silence.
        "$v = $s.GetInstalledVoices() | Where-Object {"
        " $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq '%s' } |"
        " Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) };"
        "$s.Speak(%s);"
    ) % (sapi_rate, wanted, _ps_quote(text))

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-EncodedCommand", encoded],
                       timeout=30, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _ps_quote(text):
    """A PowerShell single-quoted literal: only the quote itself needs care."""
    return "'" + text.replace("'", "''") + "'"
