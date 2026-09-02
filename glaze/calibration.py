"""Map the eye tracker output onto the scene camera image.

The tracker gives a pupil offset from the eye-sphere centre normalised by the
sphere radius (``gaze_normalized``). That is a stable, resolution independent
2D signal. Mapping it to a point in the scene camera image is a small least
squares fit: the user looks at a spot, clicks that spot in the web UI, repeat.

Six or more points fit a full second order polynomial (this absorbs most of the
lens distortion and the non-linearity of the eye-to-image projection). Three to
five points fall back to an affine fit so a quick 4-point calibration still
works.
"""

from __future__ import annotations

import json
import os
import threading

import numpy as np


def _features(gx, gy, quadratic=True):
    if quadratic:
        return [1.0, gx, gy, gx * gx, gy * gy, gx * gy]
    return [1.0, gx, gy]


class Calibration:
    """Least squares gaze -> normalised scene coordinates."""

    MIN_POINTS_AFFINE = 3
    MIN_POINTS_QUADRATIC = 6

    def __init__(self, path="calibration.json"):
        self.path = path
        self._lock = threading.Lock()
        self.points = []       # list of {gaze: [gx, gy], target: [u, v]}
        self.coefficients = None   # 2 x N
        self.quadratic = True
        self.rms_error = None

    # -- editing --------------------------------------------------------
    def add_point(self, gaze, target):
        """``gaze`` = (gx, gy) from the tracker, ``target`` = (u, v) in [0, 1]."""
        with self._lock:
            self.points.append({"gaze": [float(gaze[0]), float(gaze[1])],
                                "target": [float(target[0]), float(target[1])]})
        return self.fit()

    def remove_last(self):
        with self._lock:
            if self.points:
                self.points.pop()
        return self.fit()

    def clear(self):
        with self._lock:
            self.points = []
            self.coefficients = None
            self.rms_error = None
        return False

    # -- fitting --------------------------------------------------------
    def fit(self):
        with self._lock:
            count = len(self.points)
            if count < self.MIN_POINTS_AFFINE:
                self.coefficients = None
                self.rms_error = None
                return False

            quadratic = count >= self.MIN_POINTS_QUADRATIC
            design = np.array([_features(p["gaze"][0], p["gaze"][1], quadratic)
                               for p in self.points], dtype=np.float64)
            targets = np.array([p["target"] for p in self.points], dtype=np.float64)

            try:
                solution, *_ = np.linalg.lstsq(design, targets, rcond=None)
            except np.linalg.LinAlgError:
                self.coefficients = None
                return False

            residual = design @ solution - targets
            self.coefficients = solution          # shape (N, 2)
            self.quadratic = quadratic
            self.rms_error = float(np.sqrt((residual ** 2).sum(axis=1).mean()))
            return True

    @property
    def ready(self):
        return self.coefficients is not None

    def apply(self, gaze):
        """Map a gaze vector to normalised scene coordinates, or ``None``."""
        if gaze is None:
            return None
        with self._lock:
            if self.coefficients is None:
                return None
            features = np.array(_features(gaze[0], gaze[1], self.quadratic))
            point = features @ self.coefficients
        return (float(point[0]), float(point[1]))

    # -- persistence ----------------------------------------------------
    def to_dict(self):
        with self._lock:
            return {
                "points": list(self.points),
                "quadratic": self.quadratic,
                "rms_error": self.rms_error,
                "ready": self.coefficients is not None,
            }

    def save(self, path=None):
        path = path or self.path
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
        return path

    def load(self, path=None):
        path = path or self.path
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        with self._lock:
            self.points = data.get("points", [])
        return self.fit()


class Smoother:
    """Exponential moving average with an outlier-friendly reset.

    A raw pupil signal at 15-25 FPS looks jittery on screen; this is the
    smoothing step the upstream tutorial suggests adding in its sample prompts.
    """

    def __init__(self, alpha=0.35, jump_threshold=0.35):
        self.alpha = alpha
        self.jump_threshold = jump_threshold
        self.value = None

    def update(self, point):
        if point is None:
            return self.value
        if self.value is None:
            self.value = tuple(point)
            return self.value

        dx = point[0] - self.value[0]
        dy = point[1] - self.value[1]
        # A saccade should not be smoothed into a slow drift, so snap on big jumps.
        alpha = 1.0 if (dx * dx + dy * dy) ** 0.5 > self.jump_threshold else self.alpha

        self.value = (self.value[0] + alpha * dx, self.value[1] + alpha * dy)
        return self.value

    def reset(self):
        self.value = None
