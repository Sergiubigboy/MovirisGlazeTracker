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
import hashlib
import io
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import wave

IS_WINDOWS = os.name == "nt"
WINDOWS_DEVICE = "windows-default"

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "tts_cache")
_cache_lock = threading.Lock()


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


def options(cfg):
    """Voice settings from a config, as keyword arguments for ``speak``.

    Duck-typed on purpose: this module stays free of the config import, and
    the four call sites stay one line each.
    """
    return {
        "voice": cfg.tts_voice,
        "rate": cfg.tts_rate,
        "engine": getattr(cfg, "tts_engine", "auto"),
        "edge_voice": getattr(cfg, "tts_edge_voice", "ro-RO-AlinaNeural"),
        "cache_entries": getattr(cfg, "tts_cache_entries", 300),
    }


def speak(text, device=None, voice="ro", rate=165, blocking=False,
          engine="auto", edge_voice="ro-RO-AlinaNeural", cache_entries=300):
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
        # Try the good voice first. Anything at all going wrong with it -
        # no network, no package, a service hiccup - has to end in the local
        # voice speaking rather than in silence, because silence is
        # indistinguishable from the device being broken.
        if engine in ("auto", "edge"):
            if _speak_edge(text, edge_voice, rate, target, cache_entries):
                return
            if engine == "edge":
                return
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


# ---- neural voice -------------------------------------------------------

def _speak_edge(text, voice, rate, device, cache_entries=300):
    """Speak with a Microsoft neural voice. True if it actually played.

    Free and keyless, but it is a network call, so every phrase is kept on
    disk: the questions this asks are a short fixed set, and after the first
    time each one plays instantly and keeps working with no network at all.
    """
    if not voice:
        return False

    path = _cached_audio(text, voice, rate, cache_entries)
    if path is None:
        return False
    return _play_audio_file(path, device)


def _cached_audio(text, voice, rate, cache_entries=300):
    key = hashlib.sha256(("%s|%s|%s" % (voice, rate, text)).encode("utf-8")).hexdigest()[:32]
    path = os.path.join(CACHE_DIR, key + ".mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path

    # espeak counts words per minute around 165; edge wants a percentage.
    percent = max(-50, min(100, int(round((rate - 165) / 1.65))))
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        partial = path + ".part"
        result = subprocess.run(
            [_python(), "-m", "edge_tts", "--voice", voice,
             "--rate=%+d%%" % percent, "--text", text, "--write-media", partial],
            capture_output=True, timeout=20)
        if result.returncode != 0 or not os.path.exists(partial)                 or os.path.getsize(partial) == 0:
            _discard(partial)
            return None
        os.replace(partial, path)
    except (OSError, subprocess.SubprocessError):
        _discard(path + ".part")
        return None

    _trim_cache(cache_entries)
    return path


def _python():
    """The interpreter running us, so edge_tts is found in the same venv."""
    import sys
    return sys.executable or "python"


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _trim_cache(keep):
    """Drop the least recently used files. The phrase set is small and fixed,
    so this should never actually bite - it is here so a long-running device
    cannot slowly fill its card."""
    with _cache_lock:
        try:
            files = [os.path.join(CACHE_DIR, name) for name in os.listdir(CACHE_DIR)
                     if name.endswith(".mp3")]
        except OSError:
            return
        if len(files) <= max(1, keep):
            return
        files.sort(key=lambda f: os.path.getmtime(f))
        for path in files[:len(files) - keep]:
            _discard(path)


def _play_audio_file(path, device=None):
    """Play an mp3. Returns True only if a player actually ran."""
    if IS_WINDOWS:
        return _play_windows(path)

    # mpg123 takes an explicit ALSA device, which is the whole reason this
    # module names devices instead of trusting the system default.
    if shutil.which("mpg123"):
        command = ["mpg123", "-q"]
        if device and device != WINDOWS_DEVICE:
            command += ["-a", device]
        command.append(path)
    elif shutil.which("ffplay"):
        command = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]
    else:
        return False

    try:
        return subprocess.run(command, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _play_windows(path):
    script = (
        "Add-Type -AssemblyName presentationCore;"
        "$p = New-Object System.Windows.Media.MediaPlayer;"
        "$p.Open([uri]%s);"
        # Open() is asynchronous; the duration is unknown until it has read
        # the header, so wait for that rather than guessing at a sleep.
        "$n = 0; while (-not $p.NaturalDuration.HasTimeSpan -and $n -lt 50) "
        "{ Start-Sleep -Milliseconds 100; $n++ };"
        "$p.Play();"
        "if ($p.NaturalDuration.HasTimeSpan) "
        "{ Start-Sleep -Milliseconds ([int]$p.NaturalDuration.TimeSpan.TotalMilliseconds + 350) } "
        "else { Start-Sleep -Seconds 3 };"
        "$p.Close();"
    ) % _ps_quote(os.path.abspath(path))

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                 "-EncodedCommand", encoded],
                                timeout=90, capture_output=True)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def prewarm(phrases, engine="auto", edge_voice="ro-RO-AlinaNeural",
            rate=165, cache_entries=300):
    """Fetch the audio for a list of phrases in the background, once.

    Nothing is played and every failure is ignored: this is an optimisation,
    and the fallbacks in speak() already cover the case where it does not
    happen at all.
    """
    if engine not in ("auto", "edge") or not edge_voice:
        return None

    def run():
        for phrase in phrases:
            try:
                _cached_audio(phrase, edge_voice, rate, cache_entries)
            except Exception:
                return          # offline, or no package: stop trying

    thread = threading.Thread(target=run, name="tts-prewarm", daemon=True)
    thread.start()
    return thread
