"""The AAC conversation state machine.

Flow, end to end:

    trigger (triple blink)
      -> CAPTURING     hold the gaze cursor still ~1s to grab a photo, up to
                       max_captures, each marked with its own gaze circle
      -> ANALYZING     ONE model call with all the photos -> four pillars
                       (persoana / actiune / obiect / emotie) + confidences
      -> ASKING        every pillar under the confidence threshold gets its
                       candidates read out in the earpiece, one word at a
                       time; look up = yes, look down = no
      -> SPEAKING      the finished sentence goes out the loudspeaker
      -> IDLE

A long look left or right instead opens the needs / pain menu, which skips
the camera and the model entirely - those are fixed short lists, and they are
exactly what has to keep working when the network is down.

Threading: ``update()`` is called from the tracking loop and must stay cheap -
it only records the latest gaze and detects dwells. Everything slow (model
calls, speech, waiting for answers) runs on a worker thread, so the eye camera
never stalls behind the network.
"""

from __future__ import annotations

import threading
import time

from . import phrases
from . import speech
from . import vision

# Pillars are asked in this order on purpose: confirming the person and the
# action first narrows what the object can plausibly be.
PILLAR_ORDER = ("persoana", "actiune", "obiect")


class ConversationEngine:
    def __init__(self, cfg, app, log):
        self.cfg = cfg
        self.app = app          # for scene frames, calibration, gaze point
        self.log = log

        self._lock = threading.Lock()
        self.state = "IDLE"
        self.worker = None

        # Latest gaze, written by update(), read by the worker.
        self._gaze = None           # (x, y) normalised eye offset
        self._scene_point = None    # (u, v) calibrated point in the scene
        self._zone = "C"
        self._zone_since = 0.0

        # Capture bookkeeping.
        self._captures = []          # list of annotated frames
        self._dwell_anchor = None
        self._dwell_start = 0.0
        self._last_capture_point = None

        # What the UI shows.
        self.pillars = {}
        self.current_question = ""
        self.last_sentence = ""
        self.last_error = ""
        self._menu_blocked_until = 0.0
        # Set every frame by update(); needed here because the web UI can ask
        # for state before the first frame has been processed.
        self.model_ready = False
        self._long_close_armed = True
        self._long_close_blocked_until = 0.0
        # Set when the wearer signals "done choosing" by holding their eyes
        # shut; the capture loop waits on it instead of only a timeout.
        self._finish_capturing = threading.Event()

    # -- called from the tracking loop (must stay cheap) -------------------
    def update(self, result, scene_point, now=None):
        now = time.time() if now is None else now
        cfg = self.cfg

        # The eye model is only meaningful once it has collected rays from
        # several directions and settled on a sphere. Until then
        # gaze_normalized is measured against a placeholder centre with zero
        # radius, so it sits at a fixed offset that looks like a hard stare
        # in one direction - enough to trip the menus over and over.
        self.model_ready = (result.sphere_radius > 1.0
                            and result.ray_count >= cfg.model_ready_rays)

        with self._lock:
            self._gaze = result.gaze_normalized if result.ok else None
            self._scene_point = scene_point

            zone = self._vertical_zone(self._gaze)
            if zone != self._zone:
                self._zone = zone
                self._zone_since = now

            state = self.state

        if cfg.conversation_enabled:
            self._check_long_close(result, now)

        if state == "CAPTURING" and scene_point is not None:
            self._check_capture_dwell(scene_point, now)

        if state == "IDLE" and cfg.conversation_enabled:
            self._check_menu_dwell(now)

    def _vertical_zone(self, gaze):
        """Up / down / centre, using a threshold separate from the gestures one."""
        if gaze is None:
            return "C"
        threshold = self.cfg.answer_zone_threshold
        if gaze[1] <= -threshold:
            return "U"
        if gaze[1] >= threshold:
            return "D"
        if gaze[0] <= -threshold:
            return "L"
        if gaze[0] >= threshold:
            return "R"
        return "C"

    def _check_long_close(self, result, now):
        """Eyes held shut = the one control gesture.

        Once to start choosing, once more when finished choosing. Holding a
        closure is far more reliable than triple blinking, which needs timing
        that the person this is built for may not have - and which, in
        practice, never once fired a session.
        """
        cfg = self.cfg

        if result.eye_closed_ms <= 0:
            self._long_close_armed = True      # eyes open again, re-arm
            return
        if not self._long_close_armed:
            return
        if result.eye_closed_ms < cfg.long_close_ms:
            return
        if now < self._long_close_blocked_until:
            return

        self._long_close_armed = False
        self._long_close_blocked_until = now + cfg.long_close_cooldown_s

        with self._lock:
            state = self.state

        if state == "IDLE":
            self.log.add("state", "long eye closure -> start choosing")
            self.start("triple_blink")
        elif state == "CAPTURING":
            self.log.add("state", "long eye closure -> done choosing")
            self._finish_capturing.set()

    def _check_menu_dwell(self, now):
        """A pointed, sustained look left or right opens a fixed menu.

        Uses its own wider threshold and a cooldown: a glance aside at
        something in the room must never start talking in the person's ear.
        """
        cfg = self.cfg
        if now < self._menu_blocked_until:
            return
        if cfg.menu_require_model and not self.model_ready:
            return

        with self._lock:
            gaze, since = self._gaze, self._zone_since
        if gaze is None:
            return

        if gaze[0] <= -cfg.menu_zone_threshold:
            menu = "needs"
        elif gaze[0] >= cfg.menu_zone_threshold:
            menu = "pain"
        else:
            return

        if (now - since) * 1000.0 < cfg.menu_dwell_ms:
            return

        self._menu_blocked_until = now + cfg.menu_cooldown_s
        self.start(menu)

    def _check_capture_dwell(self, point, now):
        cfg = self.cfg
        move_limit = cfg.capture_move_fraction

        if self._dwell_anchor is None:
            self._dwell_anchor, self._dwell_start = point, now
            return

        drift = max(abs(point[0] - self._dwell_anchor[0]),
                    abs(point[1] - self._dwell_anchor[1]))
        if drift > move_limit:
            self._dwell_anchor, self._dwell_start = point, now
            return

        if (now - self._dwell_start) * 1000.0 < cfg.capture_dwell_ms:
            return

        # Require the gaze to have moved somewhere new since the last photo,
        # otherwise one long stare would fill the whole quota with one target.
        if self._last_capture_point is not None:
            moved = max(abs(point[0] - self._last_capture_point[0]),
                        abs(point[1] - self._last_capture_point[1]))
            if moved <= move_limit:
                return

        self._capture_photo(point)
        self._dwell_anchor = None
        self._last_capture_point = point

    def _capture_photo(self, point):
        frame = self.app.scene_frame()
        if frame is None:
            self.log.add("capture", "no scene frame available")
            return

        radius = vision.uncertainty_from_calibration(self.app.calibration)
        marked = vision.annotate_gaze(frame, point, radius)
        with self._lock:
            self._captures.append(marked)
            count = len(self._captures)
        self.log.add("capture", "photo %d captured" % count,
                     {"point": [round(point[0], 3), round(point[1], 3)],
                      "uncertainty": round(radius, 3)})
        self.cue("capture")

    # -- session control ---------------------------------------------------
    def start(self, trigger="triple_blink"):
        with self._lock:
            if self.state != "IDLE":
                return {"ok": False, "error": "conversation already running (%s)" % self.state}
            self.state = "CAPTURING" if trigger == "triple_blink" else "ASKING"
            self._captures = []
            self._dwell_anchor = None
            self._last_capture_point = None
            self._finish_capturing.clear()
            self.pillars = {}
            self.current_question = ""
            self.last_error = ""

        self.log.add("state", "session started", {"trigger": trigger})
        self.cue("start")
        self.worker = threading.Thread(target=self._run, args=(trigger,), daemon=True)
        self.worker.start()
        return {"ok": True, "trigger": trigger}

    def cancel(self):
        with self._lock:
            was = self.state
            self.state = "IDLE"
            self._captures = []
            self.current_question = ""
        if was != "IDLE":
            self.log.add("state", "session cancelled", {"from": was})
        return {"ok": True, "cancelled_from": was}

    # -- the worker --------------------------------------------------------
    def _run(self, trigger):
        try:
            if trigger in ("needs", "pain"):
                self._run_menu(trigger)
            else:
                self._run_visual()
        except Exception as exc:               # never kill the thread silently
            self.last_error = str(exc)
            self.log.add("error", "conversation failed: %s" % exc)
        finally:
            with self._lock:
                self.state = "IDLE"
                self.current_question = ""

    def _run_menu(self, menu):
        """Fixed list, no camera, no model - works with the network down."""
        self.log.add("state", "menu opened", {"menu": menu})

        # Confirm first: the trigger is a sustained sideways look, which can
        # still happen by accident, and reading a whole list into someone's
        # ear uninvited is worse than asking one extra question.
        if self.cfg.menu_confirm:
            label = "nevoi" if menu == "needs" else "dureri"
            if not self._ask(label):
                self.log.add("state", "menu declined at confirmation", {"menu": menu})
                self._menu_blocked_until = time.time() + self.cfg.menu_cooldown_s
                return

        for word in phrases.menu_options(menu):
            if not self._still_running():
                return
            if self._ask(word):
                sentence = phrases.menu_sentence(menu, word)
                self._finish(sentence)
                return
        self.log.add("state", "menu exhausted with no confirmation")
        self.cue("error")

    def _run_visual(self):
        cfg = self.cfg
        deadline = time.time() + cfg.capture_window_s

        self.speak_question("alege")
        while self._still_running():
            with self._lock:
                count = len(self._captures)
            if count >= cfg.max_captures or time.time() > deadline:
                break
            if self._finish_capturing.is_set():
                self.log.add("capture", "finished choosing on request",
                             {"photos": count})
                break
            time.sleep(0.05)

        with self._lock:
            captures = list(self._captures)
            self.state = "ANALYZING"

        if not captures:
            self.log.add("state", "no photos captured, nothing to analyse")
            self.cue("error")
            return

        self.log.add("ai", "sending %d photo(s) for analysis" % len(captures))
        self.cue("thinking")

        answer = vision.analyze_pillars(
            captures, model=cfg.vision_model, language=cfg.vision_language,
            threshold=cfg.pillar_confidence_threshold,
            max_options=cfg.max_options_per_pillar)

        if not answer.get("ok"):
            self.last_error = answer.get("error", "analysis failed")
            self.log.add("error", "analysis failed: %s" % self.last_error)
            self.cue("error")
            return

        self.log.add("ai", "pillars received", answer)
        with self._lock:
            self.pillars = answer["piloni"]

        confirmed, changed = self._disambiguate(answer["piloni"])
        if not self._still_running():
            return

        # Only spend a second model call when the confirmed combination is not
        # the one it already wrote a sentence for.
        sentence = answer.get("propozitie_probabila", "")
        if changed or not sentence:
            self.log.add("ai", "composing sentence for confirmed pillars", confirmed)
            composed = vision.compose_sentence(
                confirmed, model=cfg.vision_model, language=cfg.vision_language)
            if composed.get("ok") and composed.get("propozitie"):
                sentence = composed["propozitie"]
            else:
                sentence = phrases.fallback_sentence(confirmed)
                self.log.add("ai", "compose failed, using local template",
                             {"error": composed.get("error")})
        else:
            self.log.add("ai", "top combination confirmed as-is, no second call")

        self._finish(sentence)

    def _disambiguate(self, pillars):
        """Ask about every pillar the model was not sure enough about."""
        cfg = self.cfg
        confirmed = {}
        changed = False

        with self._lock:
            self.state = "ASKING"

        emotion = (pillars.get("emotie") or {}).get("valoare") or "neutru"
        confirmed["emotie"] = emotion     # inferred, never asked

        for name in PILLAR_ORDER:
            if not self._still_running():
                break

            entry = pillars.get(name) or {}
            value = entry.get("valoare", "")
            confidence = entry.get("incredere", 0.0)

            if confidence >= cfg.pillar_confidence_threshold and value:
                confirmed[name] = value
                self.log.add("pillar", "%s accepted without asking" % name,
                             {"value": value, "confidence": confidence})
                continue

            options = list(entry.get("optiuni") or [])
            if value and value not in options:
                options.insert(0, value)
            options = options[:cfg.max_options_per_pillar]

            self.log.add("pillar", "asking about %s" % name,
                         {"confidence": confidence, "options": options})

            picked = None
            for option in options:
                if not self._still_running():
                    break
                if self._ask(option):
                    picked = option
                    break

            if picked is None:
                self.log.add("pillar", "%s left unknown" % name)
                changed = True
                continue

            confirmed[name] = picked
            if picked != value:
                changed = True

        return confirmed, changed

    def _ask(self, word):
        """Speak one option, then wait for a held look up (yes) or down (no)."""
        cfg = self.cfg
        with self._lock:
            self.current_question = word

        self.log.add("question", word)
        self.cue("question")
        self.speak_question(word + "?")

        deadline = time.time() + cfg.answer_timeout_ms / 1000.0
        # Ignore whatever zone the gaze was already in when the question
        # started - an answer has to be a fresh, deliberate move.
        with self._lock:
            stale_since = self._zone_since

        while time.time() < deadline:
            if not self._still_running():
                return False

            now = time.time()
            with self._lock:
                zone, since = self._zone, self._zone_since
            if since == stale_since:
                time.sleep(0.02)
                continue

            held_ms = (now - since) * 1000.0
            if zone == "U" and held_ms >= cfg.answer_dwell_ms:
                self.log.add("answer", "YES to %r" % word, {"held_ms": round(held_ms)})
                self.cue("yes")
                return True
            if zone == "D" and held_ms >= cfg.answer_dwell_ms:
                self.log.add("answer", "NO to %r" % word, {"held_ms": round(held_ms)})
                self.cue("no")
                return False
            time.sleep(0.02)

        self.log.add("answer", "timeout on %r, treated as no" % word)
        return False

    def _finish(self, sentence):
        with self._lock:
            self.state = "SPEAKING"
            self.last_sentence = sentence
        self.log.add("speech", "speaking final sentence", {"sentence": sentence})
        self.cue("done")
        self.speak_aloud(sentence)

    def _still_running(self):
        with self._lock:
            return self.state not in ("IDLE",)

    # -- audio -------------------------------------------------------------
    def cue(self, name):
        """Short tone in the earpiece marking a step in the flow."""
        if not self.cfg.sound_cues:
            return
        cfg = self.cfg
        device = cfg.tts_question_device or cfg.tts_audio_device or None
        speech.cue(name, device=device)

    def speak_question(self, text):
        """Into the earpiece - short prompts only the wearer needs to hear."""
        cfg = self.cfg
        device = cfg.tts_question_device or cfg.tts_audio_device or None
        speech.speak(text, device=device, voice=cfg.tts_voice, rate=cfg.tts_rate)

    def speak_aloud(self, text):
        """Out the loudspeaker - this is what the room hears."""
        cfg = self.cfg
        speech.speak(text, device=cfg.tts_audio_device or None,
                     voice=cfg.tts_voice, rate=cfg.tts_rate)

    # -- for the UI --------------------------------------------------------
    def state_dict(self):
        with self._lock:
            return {
                "state": self.state,
                "captures": len(self._captures),
                "question": self.current_question,
                "pillars": self.pillars,
                "last_sentence": self.last_sentence,
                "error": self.last_error,
                "zone": self._zone,
                "model_ready": self.model_ready,
            }
