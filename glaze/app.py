"""Application glue: cameras -> tracker -> web streams.

Threads
-------
* one per camera (see :mod:`glaze.cameras`), each keeping only the newest frame
* the tracking loop, which owns the :class:`~glaze.tracker_core.EyeTracker`
* the scene loop, which draws the calibrated gaze marker on the world camera
* the HTTP server's own threads

JPEG encoding only runs while a browser is attached, and is throttled
independently of tracking, so an unattended Pi spends its CPU on the eye.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time

import cv2

from . import cameras
from . import speech
from . import vision
from . import webserver
from .calibration import Calibration, Smoother
from . import config as config_module
from .config import Config
from .conversation import ConversationEngine
from .eventlog import EventLog
from .gestures import GestureEngine
from .tracker_core import EyeTracker


class JpegChannel:
    """One MJPEG stream: newest encoded frame plus a viewer count."""

    def __init__(self, name):
        self.name = name
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._data = None
        self._sequence = 0
        self.viewers = 0

    def publish(self, data):
        if data is None:
            return
        with self._lock:
            self._data = data
            self._sequence += 1
            self._condition.notify_all()

    def get(self, last_sequence, timeout=2.0):
        with self._lock:
            if last_sequence is not None and self._sequence == last_sequence:
                self._condition.wait(timeout)
            if self._data is None or self._sequence == last_sequence:
                return None, self._sequence
            return self._data, self._sequence

    def add_viewer(self):
        with self._lock:
            self.viewers += 1

    def remove_viewer(self):
        with self._lock:
            self.viewers = max(0, self.viewers - 1)

    def wake(self):
        with self._lock:
            self._condition.notify_all()


class GlazeApp:
    """The hub the web server talks to."""

    event_rate = 10.0  # SSE updates per second

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Apply what was saved from the web UI before anything reads the
        # config - camera roles and tracker scaling both depend on it.
        self.restored_settings = config_module.load_runtime_settings(cfg)
        self.tracker = EyeTracker(cfg)
        self.calibration = Calibration(cfg.calibration_path)
        self.calibration.load()
        self.smoother = Smoother()
        self.gestures = GestureEngine(cfg.gestures_path,
                                      enter=cfg.gaze_zone_enter,
                                      exit_=cfg.gaze_zone_exit)
        self.log = EventLog(cfg.conversation_log_size)
        self.conversation = ConversationEngine(cfg, self, self.log)
        self._last_gaze_log = 0.0

        self.channels = {"eye": JpegChannel("eye"), "scene": JpegChannel("scene")}
        self.stopping = False

        self.eye_camera = None
        self.scene_camera = None
        self._camera_lock = threading.Lock()
        self.detected_cameras = []
        self.active_sources = {"eye": "", "scene": ""}
        self._threads = []
        self._server = None

        self._state_lock = threading.Lock()
        self._scene_point = None      # normalised (u, v) or None
        self._last_gaze_line = ""
        self._last_file_write = 0.0
        self._udp_socket = None
        self._udp_target = None
        self.notice = ""
        self._last_triple_blink_at = None
        self._last_vision = None      # most recent vision-model answer
        self._vision_busy = False     # one outstanding request at a time

        self._setup_udp()

    # -- setup ----------------------------------------------------------
    def _setup_udp(self):
        if not self.cfg.udp_target:
            return
        try:
            host, port = self.cfg.udp_target.rsplit(":", 1)
            self._udp_target = (host, int(port))
            self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except (ValueError, OSError) as exc:
            self.notice = "UDP disabled: %s" % exc
            self._udp_socket = None

    def _resolve_camera_sources(self):
        """Turn "auto" into concrete devices from whatever is plugged in now.

        Both roles are assigned from the same probe so they can never land on
        the same node, and ``camera_swapped`` decides which of the two is the
        eye - there is nothing in a V4L2 node that distinguishes two identical
        camera modules, so that choice has to come from the user.
        """
        cfg = self.cfg
        if "auto" not in (cfg.eye_source, cfg.scene_source):
            return cfg.eye_source, cfg.scene_source

        indices = cameras.readable_device_indices()
        self.detected_cameras = list(indices)
        if cfg.camera_swapped:
            indices = list(reversed(indices))

        self.log.add("state", "auto-detected cameras",
                     {"working": self.detected_cameras, "swapped": cfg.camera_swapped})

        def pick(position):
            return "usb:%d" % indices[position] if len(indices) > position else ""

        eye = cfg.eye_source if cfg.eye_source != "auto" else pick(0)
        scene = cfg.scene_source if cfg.scene_source != "auto" else pick(1)
        return eye, scene

    def _open_cameras(self):
        cfg = self.cfg
        eye_spec, scene_spec = self._resolve_camera_sources()
        self.notice = ""

        self.eye_camera = cameras.create_source(
            eye_spec, cfg.eye_capture_width, cfg.eye_capture_height,
            cfg.eye_capture_fps, cfg.eye_fourcc, name="eye")
        if self.eye_camera is not None:
            try:
                self.eye_camera.start()
            except Exception as exc:
                # A camera failing to open (busy device, missing driver, bad
                # config) must not take the whole process down - report it
                # and keep serving the web UI with a placeholder frame.
                self.notice = "eye camera: %s" % exc
                self.eye_camera = None
        elif eye_spec == "":
            self.notice = "no eye camera detected"

        self.scene_camera = cameras.create_source(
            scene_spec, cfg.scene_capture_width, cfg.scene_capture_height,
            cfg.scene_capture_fps, "MJPG", name="scene")
        if self.scene_camera is not None:
            try:
                self.scene_camera.start()
            except Exception as exc:
                self.notice = ("%s | scene camera: %s" % (self.notice, exc)).strip(" |")
                self.scene_camera = None

        self.active_sources = {"eye": eye_spec, "scene": scene_spec}
        self.log.add("state", "cameras opened", dict(self.active_sources))

    def start(self):
        cfg = self.cfg
        self._open_cameras()

        self._spawn(self._tracking_loop, "tracking")
        # Always spawn it: the scene camera can appear later, after a swap or
        # a re-detect, and the loop handles "not there yet" on its own.
        self._spawn(self._scene_loop, "scene")

        self._server = webserver.serve(self, cfg.host, cfg.port)
        return self

    def restart_cameras(self):
        """Re-probe and reopen both cameras without restarting the process."""
        with self._camera_lock:
            for camera in (self.eye_camera, self.scene_camera):
                if camera is not None:
                    try:
                        camera.stop()
                    except Exception:
                        pass
            self.eye_camera = None
            self.scene_camera = None
            time.sleep(0.3)          # let the driver release the nodes
            self._open_cameras()

        return {"ok": True, "sources": dict(self.active_sources),
                "detected": list(self.detected_cameras)}

    def _spawn(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self):
        self.stopping = True
        for channel in self.channels.values():
            channel.wake()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        for camera in (self.eye_camera, self.scene_camera):
            if camera is not None:
                camera.stop()
        for thread in self._threads:
            thread.join(timeout=2.0)

    # -- loops ----------------------------------------------------------
    def _tracking_loop(self):
        cfg = self.cfg
        sequence = None
        last_encode = 0.0
        next_due = time.perf_counter()

        while not self.stopping:
            if self.eye_camera is None:
                self._publish_placeholder("eye", "no eye camera")
                time.sleep(0.5)
                continue

            frame, sequence = self.eye_camera.wait_for_frame(sequence, timeout=1.0)
            if frame is None:
                self._publish_placeholder("eye", self.eye_camera.error or "waiting for eye camera")
                continue

            # Throttle so the web server and the scene camera get CPU too.
            # Read max_tracking_fps fresh every frame - it's live-adjustable
            # from the settings page, not just a startup value.
            min_period = 1.0 / cfg.max_tracking_fps if cfg.max_tracking_fps > 0 else 0.0
            now = time.perf_counter()
            if min_period and now < next_due:
                time.sleep(min(0.02, next_due - now))
            next_due = max(now, next_due) + min_period

            frame = cameras.orient(frame, cfg.flip_eye_vertical,
                                   cfg.flip_eye_horizontal, cfg.rotate_eye_degrees)

            watching = self.channels["eye"].viewers > 0
            draw = cfg.draw_overlay and watching

            try:
                result, annotated = self.tracker.process_frame(frame, draw=draw)
            except Exception as exc:
                self.notice = "tracking error: %s" % exc
                time.sleep(0.05)
                continue

            self._publish_gaze(result)

            if result.triple_blink:
                self._last_triple_blink_at = time.time()

            with self._state_lock:
                scene_point = self._scene_point

            self._log_gaze(result, scene_point)

            if cfg.conversation_enabled:
                self.conversation.update(result, scene_point)

            fired = self.gestures.update(result)
            busy = self.conversation.state != "IDLE"
            for gesture in fired:
                # While a conversation is running the eyes are answering
                # questions, so the same up/down/blink movements must not also
                # fire unrelated gestures underneath it.
                if busy:
                    self.log.add("state", "gesture %r ignored, conversation busy"
                                 % gesture.get("name"))
                    continue
                self._dispatch_gesture(gesture)

            if watching:
                elapsed = time.perf_counter() - last_encode
                if elapsed >= 1.0 / max(1.0, cfg.stream_fps):
                    jpeg = cameras.encode_jpeg(annotated, cfg.jpeg_quality)
                    self.channels["eye"].publish(jpeg)
                    last_encode = time.perf_counter()

    def _scene_loop(self):
        cfg = self.cfg
        sequence = None
        last_encode = 0.0

        while not self.stopping:
            # The camera can be absent at startup, or briefly during a swap /
            # re-detect, so re-read it every iteration instead of capturing it
            # once outside the loop.
            camera = self.scene_camera
            if camera is None:
                self._publish_placeholder("scene", "no scene camera")
                sequence = None
                time.sleep(0.5)
                continue

            frame, sequence = camera.wait_for_frame(sequence, timeout=1.0)
            if frame is None:
                self._publish_placeholder("scene", camera.error or "waiting for scene camera")
                continue

            if self.channels["scene"].viewers <= 0:
                time.sleep(0.1)
                continue

            elapsed = time.perf_counter() - last_encode
            if elapsed < 1.0 / max(1.0, cfg.scene_stream_fps):
                time.sleep(0.005)
                continue

            frame = cameras.orient(frame, cfg.flip_scene_vertical, cfg.flip_scene_horizontal)
            frame = frame.copy()
            self._draw_scene_marker(frame)

            self.channels["scene"].publish(cameras.encode_jpeg(frame, cfg.jpeg_quality))
            last_encode = time.perf_counter()

    def _draw_scene_marker(self, frame):
        with self._state_lock:
            point = self._scene_point
        if point is None:
            cv2.putText(frame, "not calibrated", (8, 18), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, (60, 200, 255), 1, cv2.LINE_AA)
            return

        height, width = frame.shape[:2]
        x = int(max(0.0, min(1.0, point[0])) * width)
        y = int(max(0.0, min(1.0, point[1])) * height)
        cv2.circle(frame, (x, y), 16, (0, 0, 0), 4)
        cv2.circle(frame, (x, y), 16, (60, 255, 255), 2)
        cv2.line(frame, (x - 22, y), (x - 6, y), (60, 255, 255), 2)
        cv2.line(frame, (x + 6, y), (x + 22, y), (60, 255, 255), 2)
        cv2.line(frame, (x, y - 22), (x, y - 6), (60, 255, 255), 2)
        cv2.line(frame, (x, y + 6), (x, y + 22), (60, 255, 255), 2)

    def _publish_placeholder(self, which, text):
        channel = self.channels[which]
        if channel.viewers <= 0:
            return
        frame = cameras.placeholder_frame(self.cfg.proc_width, self.cfg.proc_height, text)
        channel.publish(cameras.encode_jpeg(frame, 50))
        time.sleep(0.5)

    # -- outputs --------------------------------------------------------
    def _publish_gaze(self, result):
        point = None
        if result.ok and result.gaze_normalized is not None:
            mapped = self.calibration.apply(result.gaze_normalized)
            if mapped is not None:
                point = self.smoother.update(mapped)

        line = ""
        if result.gaze_origin is not None and result.gaze_direction is not None:
            values = list(result.gaze_origin) + list(result.gaze_direction)
            line = ",".join("%.6f" % v for v in values)

        with self._state_lock:
            self._scene_point = point
            if line:
                self._last_gaze_line = line

        if not line:
            return

        if self.cfg.write_gaze_file:
            now = time.perf_counter()
            # Upstream rewrote the file every frame; 30 Hz is plenty and is far
            # kinder to an SD card.
            if now - self._last_file_write >= 1 / 30.0:
                self._last_file_write = now
                try:
                    with open(self.cfg.gaze_file_path, "w", encoding="ascii") as handle:
                        handle.write(line + "\n")
                except OSError:
                    pass

        if self._udp_socket is not None:
            try:
                self._udp_socket.sendto(line.encode("ascii"), self._udp_target)
            except OSError:
                pass

    def _log_gaze(self, result, scene_point):
        """Throttled gaze samples - useful history without flooding the log."""
        now = time.time()
        if now - self._last_gaze_log < self.cfg.gaze_log_interval_s:
            return
        self._last_gaze_log = now
        self.log.add("gaze", "pupil %s" % ("tracked" if result.ok else "lost"), {
            "normalized": ([round(v, 3) for v in result.gaze_normalized]
                           if result.gaze_normalized else None),
            "scene_point": ([round(v, 3) for v in scene_point]
                            if scene_point else None),
            "confidence": round(result.confidence, 3),
            "blinking": result.blinking,
        })

    def scene_frame(self):
        """Newest scene frame, oriented - used by the conversation engine."""
        if self.scene_camera is None:
            return None
        frame = self.scene_camera.latest()
        if frame is None:
            return None
        return cameras.orient(frame, self.cfg.flip_scene_vertical,
                              self.cfg.flip_scene_horizontal)

    # -- gesture actions -------------------------------------------------
    def _dispatch_gesture(self, gesture):
        """Run the action bound to a gesture that just fired."""
        action = gesture.get("action") or "nothing"

        if action == "nothing":
            return
        if action == "reset_model":
            self.tracker.reset()
            self.smoother.reset()
            return
        if action == "toggle_sphere":
            self.tracker.toggle_sphere_adjustment()
            return
        if action == "start_conversation":
            self.conversation.start("triple_blink")
            return
        if action == "save_calibration":
            self.calibration.save()
            return
        if action in ("identify_object", "webhook"):
            # Network calls must never run on the tracking thread - a slow
            # API would stall the eye camera loop.
            threading.Thread(target=self._run_network_action,
                             args=(action, gesture), daemon=True).start()
            return

        self.notice = "unknown gesture action: %s" % action

    def _run_network_action(self, action, gesture):
        if action == "webhook":
            url = gesture.get("action_arg")
            if not url:
                self.notice = "webhook gesture has no URL configured"
                return
            try:
                import urllib.request
                payload = json.dumps({"gesture": gesture.get("name"),
                                      "state": self.state()}).encode("utf-8")
                request = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(request, timeout=10).close()
            except Exception as exc:
                self.notice = "webhook failed: %s" % exc
            return

        self.identify_object()

    def identify_object(self):
        """Snapshot the scene, mark the gaze point, ask the vision model."""
        if self._vision_busy:
            return {"ok": False, "error": "a vision request is already running"}
        if not self.cfg.vision_enabled:
            return {"ok": False, "error": "vision is disabled in the config"}
        if self.scene_camera is None:
            return {"ok": False, "error": "no scene camera"}

        frame = self.scene_camera.latest()
        if frame is None:
            return {"ok": False, "error": "no scene frame yet"}

        with self._state_lock:
            point = self._scene_point
        if point is None:
            return {"ok": False, "error": "not calibrated - no gaze point to mark"}

        self._vision_busy = True
        try:
            frame = cameras.orient(frame, self.cfg.flip_scene_vertical,
                                   self.cfg.flip_scene_horizontal)
            radius = vision.uncertainty_from_calibration(self.calibration)
            marked = vision.annotate_gaze(frame, point, radius)
            answer = vision.ask_gemini(marked, model=self.cfg.vision_model,
                                       language=self.cfg.vision_language)
            answer["at"] = time.time()
            answer["gaze_point"] = [round(point[0], 3), round(point[1], 3)]
            answer["uncertainty"] = round(radius, 3)
            with self._state_lock:
                self._last_vision = answer

            if self.cfg.tts_enabled and answer.get("ok") and answer.get("objects"):
                top = max(answer["objects"], key=lambda o: o.get("probability", 0))
                speech.speak(top["name"], device=self.cfg.tts_audio_device or None,
                            voice=self.cfg.tts_voice, rate=self.cfg.tts_rate)

            return answer
        finally:
            self._vision_busy = False

    def gaze_vector_line(self):
        with self._state_lock:
            return self._last_gaze_line + "\n" if self._last_gaze_line else "\n"

    # -- web server API -------------------------------------------------
    def get_jpeg(self, which, last_sequence, timeout=2.0):
        return self.channels[which].get(last_sequence, timeout)

    def stream_opened(self, which):
        self.channels[which].add_viewer()

    def stream_closed(self, which):
        self.channels[which].remove_viewer()

    def list_cameras(self):
        return cameras.list_v4l2_devices()

    def config_dict(self):
        cfg = self.cfg
        return {
            "eye_source": cfg.eye_source,
            "scene_source": cfg.scene_source,
            "proc_width": cfg.proc_width,
            "proc_height": cfg.proc_height,
            "roi_mode": cfg.roi_mode,
            "flip_eye_vertical": cfg.flip_eye_vertical,
            "flip_eye_horizontal": cfg.flip_eye_horizontal,
            "flip_scene_vertical": cfg.flip_scene_vertical,
            "flip_scene_horizontal": cfg.flip_scene_horizontal,
            "rotate_eye_degrees": cfg.rotate_eye_degrees,
            "thresholds": list(self.tracker.thresholds),
            "draw_overlay": cfg.draw_overlay,
            "jpeg_quality": cfg.jpeg_quality,
            "stream_fps": cfg.stream_fps,
            "scene_stream_fps": cfg.scene_stream_fps,
            "max_tracking_fps": cfg.max_tracking_fps,
            "scene_enabled": self.scene_camera is not None,
            "camera_swapped": cfg.camera_swapped,
            "detected_cameras": list(self.detected_cameras),
            "active_sources": dict(self.active_sources),
            "write_gaze_file": cfg.write_gaze_file,
            "udp_target": cfg.udp_target,
            "pupil_confidence_threshold": cfg.pupil_confidence_threshold,
            "pupil_confidence_threshold_sphere": cfg.pupil_confidence_threshold_sphere,
            "min_model_centers": cfg.min_model_centers,
            "max_rays": cfg.max_rays,
            "intersection_ray_count": cfg.intersection_ray_count,
            "minimum_intersection_angle_degrees": cfg.minimum_intersection_angle_degrees,
            "max_stored_intersections": cfg.max_stored_intersections,
            "model_center_average_window": cfg.model_center_average_window,
            "smoothing": self.smoother.alpha,
            "blink_min_ms": cfg.blink_min_ms,
            "blink_max_ms": cfg.blink_max_ms,
            "blink_window_ms": cfg.blink_window_ms,
            "blink_warmup_s": cfg.blink_warmup_s,
            "pupil_min_confidence": cfg.pupil_min_confidence,
            "pupil_min_circularity": cfg.pupil_min_circularity,
            "pupil_max_area_fraction": cfg.pupil_max_area_fraction,
            "pupil_max_jump_fraction": cfg.pupil_max_jump_fraction,
            "pupil_max_eye_radius_fraction": cfg.pupil_max_eye_radius_fraction,
            "lash_open_iterations": cfg.lash_open_iterations,
            "gaze_zone_enter": cfg.gaze_zone_enter,
            "gaze_zone_exit": cfg.gaze_zone_exit,
            "vision_model": cfg.vision_model,
            "vision_enabled": cfg.vision_enabled,
            "vision_language": cfg.vision_language,
            "vision_key_present": vision.load_api_key() is not None,
            "tts_enabled": cfg.tts_enabled,
            "tts_voice": cfg.tts_voice,
            "tts_rate": cfg.tts_rate,
            "tts_audio_device": cfg.tts_audio_device,
            "tts_device_detected": speech.detect_output_device(),
            "tts_question_device": cfg.tts_question_device,
            "conversation_enabled": cfg.conversation_enabled,
            "capture_dwell_ms": cfg.capture_dwell_ms,
            "capture_move_fraction": cfg.capture_move_fraction,
            "max_captures": cfg.max_captures,
            "capture_window_s": cfg.capture_window_s,
            "answer_dwell_ms": cfg.answer_dwell_ms,
            "answer_timeout_ms": cfg.answer_timeout_ms,
            "answer_zone_threshold": cfg.answer_zone_threshold,
            "pillar_confidence_threshold": cfg.pillar_confidence_threshold,
            "max_options_per_pillar": cfg.max_options_per_pillar,
            "menu_dwell_ms": cfg.menu_dwell_ms,
            "menu_zone_threshold": cfg.menu_zone_threshold,
            "menu_confirm": cfg.menu_confirm,
            "menu_cooldown_s": cfg.menu_cooldown_s,
            "menu_require_model": cfg.menu_require_model,
            "model_ready_rays": cfg.model_ready_rays,
            "sound_cues": cfg.sound_cues,
            "show_guide": cfg.show_guide,
            "long_close_ms": cfg.long_close_ms,
            "long_close_max_ms": cfg.long_close_max_ms,
            "long_close_cooldown_s": cfg.long_close_cooldown_s,
            "confirm_start": cfg.confirm_start,
            "start_gesture": cfg.start_gesture,
            "gaze_offset_x": cfg.gaze_offset_x,
            "gaze_offset_y": cfg.gaze_offset_y,
            "gaze_log_interval_s": cfg.gaze_log_interval_s,
        }

    def state(self):
        result = self.tracker.last_result
        with self._state_lock:
            point = self._scene_point

        payload = result.to_dict()
        payload["scene_point"] = ([round(point[0], 4), round(point[1], 4)]
                                  if point is not None else None)
        payload["calibration"] = {
            "ready": self.calibration.ready,
            "points": len(self.calibration.points),
            "rms_error": (round(self.calibration.rms_error, 4)
                          if self.calibration.rms_error is not None else None),
        }
        payload["cameras"] = {
            "eye": {
                "connected": self.eye_camera is not None,
                "fps": round(self.eye_camera.fps, 1) if self.eye_camera else 0.0,
                "error": self.eye_camera.error if self.eye_camera else "not configured",
            },
            "scene": {
                "connected": self.scene_camera is not None,
                "fps": round(self.scene_camera.fps, 1) if self.scene_camera else 0.0,
                "error": self.scene_camera.error if self.scene_camera else "not configured",
            },
        }
        payload["notice"] = self.notice
        payload["viewers"] = {name: channel.viewers for name, channel in self.channels.items()}
        payload["last_triple_blink_seconds_ago"] = (
            round(time.time() - self._last_triple_blink_at, 1)
            if self._last_triple_blink_at is not None else None
        )

        gesture_state = self.gestures.state()
        payload["gestures"] = {
            "zone": gesture_state["zone"],
            "recent_tokens": gesture_state["recent_tokens"],
            "history": gesture_state["history"],
        }

        with self._state_lock:
            last_vision = self._last_vision
        payload["vision"] = dict(last_vision) if last_vision else None
        if payload["vision"] is not None:
            payload["vision"]["seconds_ago"] = round(time.time() - last_vision["at"], 1)
        payload["vision_busy"] = self._vision_busy
        payload["conversation"] = self.conversation.state_dict()
        return payload

    def command(self, action, payload):
        cfg = self.cfg
        tracker = self.tracker

        if action == "reset":
            tracker.reset()
            self.smoother.reset()
            return {"ok": True, "message": "tracking state reset"}

        if action == "toggle_sphere":
            enabled = tracker.toggle_sphere_adjustment()
            return {"ok": True, "sphere_auto": enabled}

        if action == "toggle_capture":
            enabled = tracker.toggle_ellipse_capture()
            return {"ok": True, "capturing": enabled}

        if action == "clear_ellipses":
            tracker.clear_stuck_ellipses()
            return {"ok": True}

        if action == "set":
            return self._apply_settings(payload.get("values") or {})

        if action == "calib_add":
            target = payload.get("target")
            if not target or len(target) != 2:
                return {"ok": False, "error": "target must be [u, v] in 0..1"}
            gaze = tracker.last_result.gaze_normalized
            if gaze is None:
                return {"ok": False, "error": "no pupil detected right now"}
            self.calibration.add_point(gaze, target)
            self.smoother.reset()
            return {"ok": True, "calibration": self.calibration.to_dict()}

        if action == "calib_undo":
            self.calibration.remove_last()
            return {"ok": True, "calibration": self.calibration.to_dict()}

        if action == "calib_clear":
            self.calibration.clear()
            self.smoother.reset()
            return {"ok": True, "calibration": self.calibration.to_dict()}

        if action == "calib_save":
            path = self.calibration.save()
            return {"ok": True, "path": os.path.abspath(path)}

        if action == "calib_load":
            loaded = self.calibration.load()
            self.smoother.reset()
            return {"ok": loaded, "calibration": self.calibration.to_dict()}

        if action == "gestures_get":
            return {"ok": True, "gestures": self.gestures.load()}

        if action == "gestures_set":
            try:
                saved = self.gestures.replace_all(payload.get("gestures") or [])
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "gestures": saved}

        if action == "set_gaze_center":
            gaze = tracker.last_result.gaze_normalized
            if gaze is None:
                return {"ok": False, "error": "nu văd pupila acum - încearcă din nou"}
            # last_result is already offset-corrected, so accumulate rather
            # than replace, otherwise a second press would undo the first.
            cfg.gaze_offset_x = round(cfg.gaze_offset_x + gaze[0], 4)
            cfg.gaze_offset_y = round(cfg.gaze_offset_y + gaze[1], 4)
            config_module.save_runtime_settings(cfg, {
                "gaze_offset_x": cfg.gaze_offset_x,
                "gaze_offset_y": cfg.gaze_offset_y})
            self.log.add("state", "gaze centre set",
                         {"offset": [cfg.gaze_offset_x, cfg.gaze_offset_y]})
            return {"ok": True, "offset": [cfg.gaze_offset_x, cfg.gaze_offset_y],
                    "note": "recalibrează scena după asta"}

        if action == "swap_cameras":
            cfg.camera_swapped = not cfg.camera_swapped
            config_module.save_runtime_settings(cfg, {"camera_swapped": cfg.camera_swapped})
            self.log.add("state", "cameras swapped", {"swapped": cfg.camera_swapped})
            result = self.restart_cameras()
            result["swapped"] = cfg.camera_swapped
            # The old calibration mapped gaze onto the *other* camera, so it
            # is meaningless now - say so rather than letting it silently
            # point at the wrong things.
            result["note"] = "recalibrează - calibrarea veche era pentru cealaltă cameră"
            return result

        if action == "redetect_cameras":
            return self.restart_cameras()

        if action == "conversation_start":
            # Pressing a button is unambiguous, so skip the "începem?" step
            # that exists only to filter accidental gesture triggers.
            return self.conversation.start(payload.get("trigger") or "button",
                                           confirm=payload.get("confirm", False))

        if action == "conversation_cancel":
            return self.conversation.cancel()

        if action == "log_clear":
            self.log.clear()
            return {"ok": True}

        if action == "cue_test":
            name = str(payload.get("cue") or "start")
            device = cfg.tts_question_device or cfg.tts_audio_device or None
            if not speech.cue(name, device=device, blocking=False):
                return {"ok": False, "error": "no audio device, or unknown cue %r" % name}
            return {"ok": True, "cue": name}

        if action == "tts_test":
            text = str(payload.get("text") or "Salut, te aud bine?")
            started = speech.speak(text, device=cfg.tts_audio_device or None,
                                   voice=cfg.tts_voice, rate=cfg.tts_rate)
            if not started:
                return {"ok": False, "error":
                        "no USB audio device found (or espeak-ng missing) - "
                        "run tools/list_audio.py on the Pi"}
            return {"ok": True}

        if action == "identify_object":
            # Run it inline here: this is a user pressing a button and
            # waiting for the answer, not the tracking loop.
            return self.identify_object()

        if action in ("poweroff", "reboot", "restart_service"):
            return self._power_action(action)

        return {"ok": False, "error": "unknown action: %s" % action}

    def _power_action(self, action):
        # Requires a NOPASSWD sudoers entry for these exact systemctl calls -
        # see README. Popen (not run) so the HTTP response can still be sent
        # back before this process is killed by its own request.
        commands = {
            "poweroff": ["sudo", "systemctl", "poweroff"],
            "reboot": ["sudo", "systemctl", "reboot"],
            "restart_service": ["sudo", "systemctl", "restart", "glaze"],
        }
        try:
            subprocess.Popen(commands[action])
        except OSError as exc:
            return {"ok": False, "error": "could not run %s: %s" % (action, exc)}
        return {"ok": True, "message": action}

    def _apply_settings(self, values):
        cfg = self.cfg
        applied = {}

        booleans = ("flip_eye_vertical", "flip_eye_horizontal", "flip_scene_vertical",
                    "flip_scene_horizontal", "draw_overlay", "roi_mode", "write_gaze_file",
                    "tts_enabled", "vision_enabled", "conversation_enabled",
                    "menu_confirm", "menu_require_model", "sound_cues",
                    "show_guide", "confirm_start")
        for key in booleans:
            if key in values:
                setattr(cfg, key, bool(values[key]))
                applied[key] = getattr(cfg, key)

        if "rotate_eye_degrees" in values:
            degrees = int(values["rotate_eye_degrees"])
            if degrees not in (0, 90, 180, 270):
                return {"ok": False, "error": "rotate must be 0/90/180/270"}
            cfg.rotate_eye_degrees = degrees
            applied["rotate_eye_degrees"] = degrees

        numbers = {
            "jpeg_quality": (int, 20, 95),
            "stream_fps": (float, 1.0, 30.0),
            "scene_stream_fps": (float, 1.0, 30.0),
            "max_tracking_fps": (float, 1.0, 120.0),
            "pupil_confidence_threshold": (float, 0.1, 1.0),
            "pupil_confidence_threshold_sphere": (float, 0.1, 1.0),
            "min_model_centers": (int, 1, 500),
            "max_rays": (int, 5, 500),
            "intersection_ray_count": (int, 2, 20),
            "minimum_intersection_angle_degrees": (float, 0.0, 45.0),
            "max_stored_intersections": (int, 50, 5000),
            "model_center_average_window": (int, 5, 1000),
            "blink_min_ms": (float, 10.0, 500.0),
            "blink_max_ms": (float, 50.0, 2000.0),
            "blink_window_ms": (float, 300.0, 5000.0),
            "blink_warmup_s": (float, 0.0, 60.0),
            "pupil_min_confidence": (float, 0.0, 1.0),
            "pupil_min_circularity": (float, 0.0, 1.0),
            "pupil_max_area_fraction": (float, 0.02, 1.0),
            "pupil_max_jump_fraction": (float, 0.05, 2.0),
            "pupil_max_eye_radius_fraction": (float, 0.5, 3.0),
            "lash_open_iterations": (int, 0, 4),
            "gaze_zone_enter": (float, 0.05, 2.0),
            "gaze_zone_exit": (float, 0.0, 1.0),
            "tts_rate": (int, 80, 400),
            "capture_dwell_ms": (float, 200.0, 5000.0),
            "capture_move_fraction": (float, 0.02, 1.0),
            "max_captures": (int, 1, 6),
            "capture_window_s": (float, 3.0, 120.0),
            "answer_dwell_ms": (float, 100.0, 3000.0),
            "answer_timeout_ms": (float, 500.0, 20000.0),
            "answer_zone_threshold": (float, 0.05, 2.0),
            "pillar_confidence_threshold": (float, 0.1, 1.0),
            "max_options_per_pillar": (int, 1, 8),
            "menu_dwell_ms": (float, 300.0, 5000.0),
            "menu_zone_threshold": (float, 0.1, 2.0),
            "menu_cooldown_s": (float, 0.0, 60.0),
            "model_ready_rays": (int, 1, 200),
            "long_close_ms": (float, 300.0, 4000.0),
            "long_close_max_ms": (float, 500.0, 10000.0),
            "long_close_cooldown_s": (float, 0.0, 30.0),
            "gaze_offset_x": (float, -3.0, 3.0),
            "gaze_offset_y": (float, -3.0, 3.0),
            "gaze_log_interval_s": (float, 0.1, 10.0),
        }
        for key, (cast, low, high) in numbers.items():
            if key in values:
                value = max(low, min(high, cast(values[key])))
                setattr(cfg, key, value)
                applied[key] = value

        # Free-text: the model name changes when Google retires one, and the
        # 404 body tells you the replacement, so this must be editable.
        if "vision_model" in values:
            name = str(values["vision_model"]).strip()
            if not name or len(name) > 80 or "/" in name:
                return {"ok": False, "error": "invalid model name"}
            cfg.vision_model = name
            applied["vision_model"] = name

        for key, max_len in (("vision_language", 40), ("tts_voice", 10),
                             ("tts_audio_device", 60), ("tts_question_device", 60)):
            if key in values:
                text = str(values[key]).strip()
                if len(text) > max_len:
                    return {"ok": False, "error": "%s too long" % key}
                setattr(cfg, key, text)
                applied[key] = text

        if "thresholds" in values:
            try:
                thresholds = tuple(int(v) for v in values["thresholds"])
            except (TypeError, ValueError):
                return {"ok": False, "error": "thresholds must be three integers"}
            if len(thresholds) != 3:
                return {"ok": False, "error": "thresholds must be three integers"}
            cfg.threshold_strict, cfg.threshold_medium, cfg.threshold_relaxed = thresholds
            self.tracker.thresholds = thresholds
            applied["thresholds"] = list(thresholds)

        if "smoothing" in values:
            self.smoother.alpha = max(0.05, min(1.0, float(values["smoothing"])))
            applied["smoothing"] = self.smoother.alpha

        # These are cached in derived form, so pushing the cfg value alone
        # would silently do nothing until the next restart.
        if "pupil_max_area_fraction" in applied:
            self.tracker.max_contour_area = (cfg.pupil_max_area_fraction
                                             * cfg.proc_width * cfg.proc_height)
        if "gaze_zone_enter" in applied:
            self.gestures.enter_threshold = cfg.gaze_zone_enter
        if "gaze_zone_exit" in applied:
            self.gestures.exit_threshold = cfg.gaze_zone_exit

        if applied:
            config_module.save_runtime_settings(cfg, applied)

        return {"ok": True, "applied": applied}
