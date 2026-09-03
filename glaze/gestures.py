"""Gaze/blink gesture recognition.

The tracker produces a continuous signal; this turns it into a small stream of
discrete tokens and matches user-defined patterns against them.

Tokens
------
``L`` ``R`` ``U`` ``D``  gaze entered that zone (once per entry)
``C``                    gaze returned to the centre
``B``                    one confirmed blink

Entering a zone only emits a token once - the gaze has to come back through
the centre band before that direction can fire again. Without that hysteresis
a gaze parked near a threshold would emit a token every frame.

A pattern is a token sequence plus a time budget, e.g. ``L R L R`` within
3000 ms, or ``B B B`` within 1500 ms. Patterns live in a JSON file so they can
be edited from the web UI without touching code.
"""

from __future__ import annotations

import json
import os
import threading
import time

DIRECTION_TOKENS = ("L", "R", "U", "D")
ALL_TOKENS = DIRECTION_TOKENS + ("C", "B")

# Shipped as a starting point; the user can edit or delete them in the UI.
DEFAULT_GESTURES = [
    {
        "name": "triple blink",
        "tokens": ["B", "B", "B"],
        "window_ms": 1500,
        "cooldown_ms": 3000,
        "action": "identify_object",
        "enabled": True,
    },
    {
        "name": "look left-right-left-right",
        "tokens": ["L", "R", "L", "R"],
        "window_ms": 4000,
        "cooldown_ms": 3000,
        "action": "reset_model",
        "enabled": True,
    },
]


class GestureEngine:
    """Turns tracker results into tokens and matches patterns against them."""

    # Tokens older than this are dropped from the buffer no matter what.
    BUFFER_SECONDS = 15.0

    def __init__(self, path="gestures.json", enter=0.35, exit_=0.20):
        self.path = path
        self.enter_threshold = enter
        self.exit_threshold = exit_

        self._lock = threading.Lock()
        self.gestures = []
        self.tokens = []          # (token, timestamp)
        self.last_fired = {}      # name -> timestamp
        self.history = []         # recent fires, for the UI
        self._zone = "C"          # which zone the gaze is currently in
        self._blink_count_seen = 0

        self.load()

    # -- storage --------------------------------------------------------
    def load(self):
        data = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = None

        with self._lock:
            self.gestures = data if isinstance(data, list) else list(DEFAULT_GESTURES)
        return self.gestures

    def save(self):
        with self._lock:
            payload = list(self.gestures)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        return self.path

    def replace_all(self, gestures):
        """Validate and store a whole gesture list from the UI."""
        cleaned = []
        for raw in gestures:
            tokens = [str(t).upper() for t in raw.get("tokens", [])]
            if not tokens or any(t not in ALL_TOKENS for t in tokens):
                raise ValueError("gesture %r has invalid tokens (allowed: %s)"
                                 % (raw.get("name"), " ".join(ALL_TOKENS)))
            cleaned.append({
                "name": str(raw.get("name") or "unnamed"),
                "tokens": tokens,
                "window_ms": max(200, int(raw.get("window_ms", 2000))),
                "cooldown_ms": max(0, int(raw.get("cooldown_ms", 2000))),
                "action": str(raw.get("action") or "nothing"),
                "action_arg": str(raw.get("action_arg") or ""),
                "enabled": bool(raw.get("enabled", True)),
            })
        with self._lock:
            self.gestures = cleaned
        self.save()
        return cleaned

    # -- token stream ---------------------------------------------------
    def _zone_for(self, gaze):
        """Which zone a normalised gaze vector is in, with hysteresis."""
        x, y = gaze
        # Leaving a zone needs the gaze back inside the (smaller) centre band.
        if self._zone != "C":
            if abs(x) < self.exit_threshold and abs(y) < self.exit_threshold:
                return "C"
            return self._zone

        if abs(x) >= abs(y):
            if x <= -self.enter_threshold:
                return "L"
            if x >= self.enter_threshold:
                return "R"
        else:
            if y <= -self.enter_threshold:
                return "U"
            if y >= self.enter_threshold:
                return "D"
        return "C"

    def update(self, result, now=None):
        """Feed one tracker result. Returns the list of gestures that fired."""
        now = time.time() if now is None else now
        new_tokens = []

        # Blinks: watch the monotonic total, not the windowed count - that one
        # decays and gets cleared by the tracker's own triple-blink flag, so
        # counting edges off it would silently drop blinks.
        total = getattr(result, "blink_total", 0)
        if total < self._blink_count_seen:      # tracker was reset under us
            self._blink_count_seen = 0
        if total > self._blink_count_seen:
            new_tokens.extend("B" for _ in range(total - self._blink_count_seen))
        self._blink_count_seen = total

        if result.ok and result.gaze_normalized is not None:
            zone = self._zone_for(result.gaze_normalized)
            if zone != self._zone:
                self._zone = zone
                new_tokens.append(zone)

        if not new_tokens:
            return []

        with self._lock:
            for token in new_tokens:
                self.tokens.append((token, now))
            cutoff = now - self.BUFFER_SECONDS
            self.tokens = [t for t in self.tokens if t[1] >= cutoff]

        return self._match(now)

    def _match(self, now):
        fired = []
        with self._lock:
            for gesture in self.gestures:
                if not gesture.get("enabled", True):
                    continue

                pattern = gesture["tokens"]

                # Going left then right necessarily crosses the centre, so
                # the raw stream is "L C R C L C R". Unless the pattern asks
                # for C explicitly, those passes are not part of the gesture.
                if "C" in pattern:
                    candidates = list(self.tokens)
                else:
                    candidates = [t for t in self.tokens if t[0] != "C"]

                if len(candidates) < len(pattern):
                    continue

                tail = candidates[-len(pattern):]
                if [t[0] for t in tail] != pattern:
                    continue
                if (now - tail[0][1]) * 1000.0 > gesture["window_ms"]:
                    continue

                last = self.last_fired.get(gesture["name"], 0.0)
                if (now - last) * 1000.0 < gesture.get("cooldown_ms", 0):
                    continue

                self.last_fired[gesture["name"]] = now
                # Consume everything up to and including the match, so the
                # same input cannot fire the gesture twice.
                start = tail[0][1]
                self.tokens = [t for t in self.tokens if t[1] < start]
                fired.append(dict(gesture))
                self.history.insert(0, {"name": gesture["name"],
                                        "action": gesture.get("action"),
                                        "at": now})
                del self.history[20:]
        return fired

    # -- introspection --------------------------------------------------
    def state(self):
        with self._lock:
            now = time.time()
            return {
                "zone": self._zone,
                "recent_tokens": [t[0] for t in self.tokens[-12:]],
                "gestures": list(self.gestures),
                "history": [{"name": h["name"], "action": h["action"],
                             "seconds_ago": round(now - h["at"], 1)}
                            for h in self.history[:8]],
            }
