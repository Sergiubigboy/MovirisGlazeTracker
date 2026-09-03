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

import os
import socket
import subprocess
import threading
import time

import cv2

from . import cameras
from .calibration import Calibration, Smoother
from .config import Config
from .tracker_core import EyeTracker
from . import webserver


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
        self.tracker = EyeTracker(cfg)
        self.calibration = Calibration(cfg.calibration_path)
        self.calibration.load()
        self.smoother = Smoother()

        self.channels = {"eye": JpegChannel("eye"), "scene": JpegChannel("scene")}
        self.stopping = False

        self.eye_camera = None
        self.scene_camera = None
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

    def start(self):
        cfg = self.cfg

        self.eye_camera = cameras.create_source(
            cfg.eye_source, cfg.eye_capture_width, cfg.eye_capture_height,
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

        self.scene_camera = cameras.create_source(
            cfg.scene_source, cfg.scene_capture_width, cfg.scene_capture_height,
            cfg.scene_capture_fps, "MJPG", name="scene")
        if self.scene_camera is not None:
            try:
                self.scene_camera.start()
            except Exception as exc:
                self.notice = ("%s | scene camera: %s" % (self.notice, exc)).strip(" |")
                self.scene_camera = None

        self._spawn(self._tracking_loop, "tracking")
        if self.scene_camera is not None:
            self._spawn(self._scene_loop, "scene")

        self._server = webserver.serve(self, cfg.host, cfg.port)
        return self

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
            frame, sequence = self.scene_camera.wait_for_frame(sequence, timeout=1.0)
            if frame is None:
                self._publish_placeholder("scene", self.scene_camera.error or "waiting for scene camera")
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
                    "flip_scene_horizontal", "draw_overlay", "roi_mode", "write_gaze_file")
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
        }
        for key, (cast, low, high) in numbers.items():
            if key in values:
                value = max(low, min(high, cast(values[key])))
                setattr(cfg, key, value)
                applied[key] = value

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

        return {"ok": True, "applied": applied}
