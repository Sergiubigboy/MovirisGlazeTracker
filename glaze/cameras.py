"""Camera backends for the Pi build.

Three sources are supported behind one interface:

* ``usb:N``  - a UVC camera (the GC0308 IR eye camera) through V4L2.
* ``csi``    - the Raspberry Pi camera module through picamera2 (libcamera).
* a file path - an .mp4/.avi, so the tracker can be developed without hardware.

Every source runs in its own thread and keeps only the newest frame. That
matters on a Pi: if the tracking loop is slower than the camera, a queued
pipeline would hand it stale frames and the latency would grow without bound.
"""

from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np


class CameraError(RuntimeError):
    pass


def _fourcc(code):
    """``VideoWriter_fourcc`` moved between OpenCV majors; accept either name."""
    factory = getattr(cv2, "VideoWriter_fourcc", None)
    if factory is None:
        factory = getattr(cv2.VideoWriter, "fourcc", None)
    if factory is None:
        return None
    return factory(*code)


class FrameSource:
    """Base class: a thread that publishes the most recent frame."""

    def __init__(self, name="camera"):
        self.name = name
        self._lock = threading.Lock()
        self._frame = None
        self._sequence = 0
        self._new_frame = threading.Condition(self._lock)
        self._running = False
        self._thread = None
        self.error = None
        self.fps = 0.0
        self._last_stamp = None

    # -- lifecycle ------------------------------------------------------
    def start(self):
        self._open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._close()

    # -- to implement ---------------------------------------------------
    def _open(self):
        raise NotImplementedError

    def _read(self):
        raise NotImplementedError

    def _close(self):
        pass

    # -- plumbing -------------------------------------------------------
    def _publish(self, frame):
        now = time.time()
        if self._last_stamp is not None:
            delta = now - self._last_stamp
            if delta > 0:
                instant = 1.0 / delta
                self.fps = instant if self.fps == 0 else self.fps * 0.9 + instant * 0.1
        self._last_stamp = now
        with self._lock:
            self._frame = frame
            self._sequence += 1
            self._new_frame.notify_all()

    def _loop(self):
        failures = 0
        while self._running:
            try:
                frame = self._read()
            except Exception as exc:  # keep the process alive, report upstream
                self.error = str(exc)
                frame = None

            if frame is None:
                failures += 1
                if failures > 60:
                    self.error = self.error or "no frames from " + self.name
                    time.sleep(0.5)
                    failures = 0
                else:
                    time.sleep(0.01)
                continue

            failures = 0
            self.error = None
            self._publish(frame)

    def latest(self):
        """Newest frame, or ``None`` if nothing has arrived yet."""
        with self._lock:
            return self._frame

    def wait_for_frame(self, last_sequence=None, timeout=1.0):
        """Block until a frame newer than ``last_sequence`` exists.

        Returns ``(frame, sequence)``; ``frame`` is ``None`` on timeout.
        """
        with self._lock:
            if last_sequence is not None and self._sequence == last_sequence:
                self._new_frame.wait(timeout)
            if self._frame is None or self._sequence == last_sequence:
                return None, self._sequence
            return self._frame, self._sequence


class V4L2Source(FrameSource):
    """USB / UVC camera. Used for the GC0308 infrared eye camera."""

    def __init__(self, index, width, height, fps, fourcc="MJPG", name="usb"):
        super().__init__(name)
        self.index = index
        self.width = width
        self.height = height
        self.target_fps = fps
        self.fourcc = fourcc
        self.capture = None
        self.actual = (0, 0, 0.0)

    def _open(self):
        backend = cv2.CAP_V4L2 if hasattr(cv2, "CAP_V4L2") and os.name != "nt" else cv2.CAP_ANY
        capture = cv2.VideoCapture(self.index, backend)
        if not capture.isOpened():
            capture.release()
            capture = cv2.VideoCapture(self.index)
        if not capture.isOpened():
            raise CameraError("could not open USB camera index %s" % self.index)

        # MJPG keeps the USB bus free on a Pi; plenty of GC0308 modules only
        # offer YUYV, in which case the driver quietly ignores this.
        if self.fourcc:
            code = _fourcc(self.fourcc)
            if code is not None:
                capture.set(cv2.CAP_PROP_FOURCC, code)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.target_fps)
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        self.capture = capture
        self.actual = (int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                       int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                       float(capture.get(cv2.CAP_PROP_FPS)))

    def _read(self):
        ok, frame = self.capture.read()
        if not ok:
            return None
        return frame

    def _close(self):
        if self.capture is not None:
            self.capture.release()
            self.capture = None


class PiCameraSource(FrameSource):
    """Raspberry Pi CSI camera (camera module v2 / IMX219) through picamera2."""

    def __init__(self, width, height, fps, name="csi"):
        super().__init__(name)
        self.width = width
        self.height = height
        self.target_fps = fps
        self.picam = None

    def _open(self):
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraError(
                "picamera2 is not installed. On Raspberry Pi OS Lite run: "
                "sudo apt install -y python3-picamera2"
            ) from exc

        picam = Picamera2()
        # picamera2 labels this format RGB888 but hands back BGR-ordered arrays,
        # which is exactly what OpenCV wants, so no conversion is needed.
        video_config = picam.create_video_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"},
            buffer_count=3,
            controls={"FrameDurationLimits": (int(1e6 / self.target_fps),
                                              int(1e6 / self.target_fps))},
        )
        picam.configure(video_config)
        picam.start()
        time.sleep(0.4)  # let AE/AWB settle
        self.picam = picam

    def _read(self):
        return self.picam.capture_array()

    def _close(self):
        if self.picam is not None:
            try:
                self.picam.stop()
                self.picam.close()
            except Exception:
                pass
            self.picam = None


class VideoFileSource(FrameSource):
    """Loops a video file at its native frame rate. For testing on a laptop."""

    def __init__(self, path, name="file"):
        super().__init__(name)
        self.path = path
        self.capture = None
        self._interval = 1 / 30.0
        self._next_due = 0.0

    def _open(self):
        if not os.path.exists(self.path):
            raise CameraError("video file not found: " + self.path)
        capture = cv2.VideoCapture(self.path)
        if not capture.isOpened():
            raise CameraError("could not open video file: " + self.path)
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        self._interval = 1.0 / max(1.0, min(fps, 120.0))
        self.capture = capture

    def _read(self):
        now = time.perf_counter()
        if now < self._next_due:
            time.sleep(min(0.02, self._next_due - now))
        self._next_due = max(now, self._next_due) + self._interval

        ok, frame = self.capture.read()
        if not ok:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.capture.read()
            if not ok:
                return None
        return frame

    def _close(self):
        if self.capture is not None:
            self.capture.release()
            self.capture = None


def create_source(spec, width, height, fps, fourcc="MJPG", name="camera"):
    """Build a :class:`FrameSource` from a config string. ``None`` if disabled."""
    if not spec:
        return None

    spec = str(spec).strip()
    if spec.lower() in ("none", "off", "disabled"):
        return None
    if spec.lower() in ("csi", "picam", "picamera", "pi"):
        return PiCameraSource(width, height, fps, name=name)
    if spec.lower().startswith("usb:"):
        return V4L2Source(int(spec.split(":", 1)[1]), width, height, fps, fourcc, name=name)
    if spec.isdigit():
        return V4L2Source(int(spec), width, height, fps, fourcc, name=name)
    return VideoFileSource(spec, name=name)


def list_v4l2_devices(max_index=8):
    """Probe /dev/video* so the web UI can show what is plugged in."""
    devices = []
    for index in range(max_index):
        path = "/dev/video%d" % index
        if os.name != "nt" and not os.path.exists(path):
            continue
        capture = cv2.VideoCapture(index, cv2.CAP_V4L2 if os.name != "nt" else cv2.CAP_ANY)
        if capture.isOpened():
            ok, frame = capture.read()
            devices.append({
                "index": index,
                "path": path,
                "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "readable": bool(ok and frame is not None),
            })
        capture.release()
    return devices


def orient(frame, flip_vertical=False, flip_horizontal=False, rotate_degrees=0):
    """Apply the flips/rotation from the config to a frame."""
    if frame is None:
        return None
    if rotate_degrees == 90:
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    elif rotate_degrees == 180:
        frame = cv2.rotate(frame, cv2.ROTATE_180)
    elif rotate_degrees == 270:
        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

    if flip_vertical and flip_horizontal:
        frame = cv2.flip(frame, -1)
    elif flip_vertical:
        frame = cv2.flip(frame, 0)
    elif flip_horizontal:
        frame = cv2.flip(frame, 1)
    return frame


def encode_jpeg(frame, quality=60):
    """Encode BGR -> JPEG bytes, or ``None`` on failure."""
    if frame is None:
        return None
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return None
    return buffer.tobytes()


def placeholder_frame(width, height, text):
    """Grey frame with a message, shown when a camera is missing."""
    frame = np.full((height, width, 3), 40, dtype=np.uint8)
    cv2.putText(frame, text, (10, height // 2), cv2.FONT_HERSHEY_SIMPLEX,
                0.45, (200, 200, 200), 1, cv2.LINE_AA)
    return frame
