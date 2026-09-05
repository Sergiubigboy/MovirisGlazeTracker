"""3D pupil / gaze tracker - Raspberry Pi port.

This is a direct port of ``Orlosky3DEyeTracker.py`` from
https://github.com/JEOresearch/EyeTracker (3DTracker) by Jason Orlosky.
The algorithm is unchanged: find the darkest region, threshold it at three
levels, pick the threshold whose largest contour fits an ellipse best, collect
pupil-normal rays, intersect them to estimate the eye sphere centre, then turn
pupil centre + sphere centre into a 3D gaze origin and direction.

What changed for the Pi:

* No tkinter, no OpenGL, no ``cv2.imshow`` - the module never touches a display.
* Global state moved into :class:`EyeTracker` so the web server can reset it.
* ``get_darkest_area`` was a triple nested Python loop over the whole frame -
  by far the most expensive part of a frame on ARM. It is now a single box
  filter plus ``minMaxLoc``, which is the same "mean of a NxN window, take the
  darkest" computation done in C, at every pixel instead of every 10th.
* ``optimize_contours_by_angle`` was a per-point Python loop, now vectorised.
* Optional ROI mode: instead of masking the full frame around the darkest
  point, the frame is cropped to that square and every threshold / dilate /
  contour / mask pass runs inside the small crop.
* Every constant that was tuned for 640x480 scales with the processing
  resolution, so 320x240 (or 256x192) behaves the same.
* ``gaze_vector.txt`` is no longer written from inside the maths.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Frame helpers (upstream, unchanged behaviour)
# ---------------------------------------------------------------------------

def crop_to_aspect_ratio(image, width=640, height=480):
    """Crop to the target aspect ratio, then resize. Upstream helper."""
    current_height, current_width = image.shape[:2]
    if current_width == width and current_height == height:
        return image

    desired_ratio = width / height
    current_ratio = current_width / current_height

    if current_ratio > desired_ratio:
        new_width = int(desired_ratio * current_height)
        offset = (current_width - new_width) // 2
        cropped = image[:, offset:offset + new_width]
    else:
        new_height = int(current_width / desired_ratio)
        offset = (current_height - new_height) // 2
        cropped = image[offset:offset + new_height, :]

    interpolation = cv2.INTER_AREA if cropped.shape[1] > width else cv2.INTER_LINEAR
    return cv2.resize(cropped, (width, height), interpolation=interpolation)


def apply_binary_threshold(image, darkest_pixel_value, added_threshold):
    """Upstream ``apply_binary_threshold``."""
    threshold = int(darkest_pixel_value) + int(added_threshold)
    _, thresholded = cv2.threshold(image, threshold, 255, cv2.THRESH_BINARY_INV)
    return thresholded


def get_darkest_area(gray, ignore_bounds=20, search_area=20):
    """Centre of the darkest ``search_area`` square, vectorised.

    Upstream scanned candidate positions in Python with a stride of 10 and
    summed a strided 20x20 patch. A box filter computes exactly that mean for
    *every* pixel in one pass, so this is both faster and more precise.
    """
    height, width = gray.shape[:2]
    kernel = max(3, int(search_area))
    blurred = cv2.blur(gray, (kernel, kernel))

    bound = int(ignore_bounds)
    if height - 2 * bound < 1 or width - 2 * bound < 1:
        bound = 0

    region = blurred[bound:height - bound, bound:width - bound]
    _, _, min_loc, _ = cv2.minMaxLoc(region)
    return (int(min_loc[0]) + bound, int(min_loc[1]) + bound)


def mask_outside_square(image, center, size):
    """Upstream ``mask_outside_square`` (used when ROI mode is disabled)."""
    x, y = center
    half_size = size // 2

    mask = np.zeros_like(image)
    top_left_x = max(0, x - half_size)
    top_left_y = max(0, y - half_size)
    bottom_right_x = min(image.shape[1], x + half_size)
    bottom_right_y = min(image.shape[0], y + half_size)
    mask[top_left_y:bottom_right_y, top_left_x:bottom_right_x] = 255
    return cv2.bitwise_and(image, mask)


def square_roi(shape, center, size):
    """Bounding box of the square upstream would have masked."""
    height, width = shape[:2]
    half = size // 2
    x0 = max(0, int(center[0]) - half)
    y0 = max(0, int(center[1]) - half)
    x1 = min(width, int(center[0]) + half)
    y1 = min(height, int(center[1]) + half)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0, 0, width, height
    return x0, y0, x1, y1


def optimize_contours_by_angle(contour):
    """Vectorised version of upstream ``optimize_contours_by_angle``.

    Keeps contour points whose local tangent bisector points towards the
    centroid, which drops the eyelid/eyelash side of the blob. Same maths,
    without the per-point Python loop. The wrap-around at the two ends uses
    ``np.roll`` instead of upstream's clamp-to-one-index, which only differs
    for the first/last ``spacing`` points.
    """
    if contour is None or len(contour) < 5:
        return contour

    points = contour.reshape(-1, 2).astype(np.float32)
    spacing = int(len(points) / 25)
    if spacing < 1:
        return contour

    previous = np.roll(points, spacing, axis=0)
    following = np.roll(points, -spacing, axis=0)
    centroid = points.mean(axis=0)

    bisector = ((previous - points) + (following - points)) * 0.5
    to_centroid = centroid - points
    dots = np.einsum("ij,ij->i", to_centroid, bisector)

    cos_threshold = math.cos(math.radians(60))
    kept = points[dots >= cos_threshold]

    # Upstream returned whatever survived, then called fitEllipse on it, which
    # throws below 5 points. Fall back to the unfiltered contour instead.
    if len(kept) < 5:
        return contour
    return kept.astype(np.int32).reshape(-1, 1, 2)


def filter_contours_by_area_and_return_largest(contours, pixel_thresh, ratio_thresh):
    """Upstream ``filter_contours_by_area_and_return_largest``."""
    max_area = 0
    largest_contour = None

    for contour in contours:
        area = cv2.contourArea(contour)
        if area >= pixel_thresh:
            _, _, w, h = cv2.boundingRect(contour)
            if w == 0 or h == 0:
                continue
            length_to_width_ratio = max(w / h, h / w)
            if length_to_width_ratio <= ratio_thresh and area > max_area:
                max_area = area
                largest_contour = contour

    return [largest_contour] if largest_contour is not None else []


def check_contour_pixels(contour, image_shape, thick=10, thin=4):
    """Upstream ``check_contour_pixels`` (masks are ROI sized in ROI mode)."""
    if len(contour) < 5:
        return [0, 0]

    contour_mask = np.zeros(image_shape, dtype=np.uint8)
    cv2.drawContours(contour_mask, [contour], -1, 255, 1)

    ellipse = cv2.fitEllipse(contour)

    ellipse_mask_thick = np.zeros(image_shape, dtype=np.uint8)
    ellipse_mask_thin = np.zeros(image_shape, dtype=np.uint8)
    cv2.ellipse(ellipse_mask_thick, ellipse, 255, thick)
    cv2.ellipse(ellipse_mask_thin, ellipse, 255, thin)

    overlap_thick = cv2.bitwise_and(contour_mask, ellipse_mask_thick)
    overlap_thin = cv2.bitwise_and(contour_mask, ellipse_mask_thin)

    absolute_pixel_total_thick = int(cv2.countNonZero(overlap_thick))
    absolute_pixel_total_thin = int(cv2.countNonZero(overlap_thin))
    total_border_pixels = int(cv2.countNonZero(contour_mask))

    ratio_under_ellipse = (absolute_pixel_total_thin / total_border_pixels
                           if total_border_pixels > 0 else 0.0)

    return [absolute_pixel_total_thick, ratio_under_ellipse]


def check_ellipse_goodness(binary_image, contour):
    """Upstream ``check_ellipse_goodness``: how well the blob fills its ellipse."""
    goodness = [0.0, 0.0, 0.0]
    if len(contour) < 5:
        return goodness

    ellipse = cv2.fitEllipse(contour)

    mask = np.zeros_like(binary_image)
    cv2.ellipse(mask, ellipse, 255, -1)

    ellipse_area = int(cv2.countNonZero(mask))
    if ellipse_area == 0:
        return goodness

    covered = cv2.bitwise_and(binary_image, mask)
    covered_pixels = int(cv2.countNonZero(covered))

    goodness[0] = covered_pixels / ellipse_area
    axis_a, axis_b = ellipse[1]
    if axis_a > 0 and axis_b > 0:
        goodness[2] = min(axis_b / axis_a, axis_a / axis_b)
    return goodness


def distance_to_pupil_outer_edge(eye_center, pupil_ellipse):
    """Upstream ``distance_to_pupil_outer_edge``."""
    pupil_center, axes, angle_degrees = pupil_ellipse
    direction_x = pupil_center[0] - eye_center[0]
    direction_y = pupil_center[1] - eye_center[1]
    center_distance = math.hypot(direction_x, direction_y)

    semi_axis_x = axes[0] / 2
    semi_axis_y = axes[1] / 2
    if center_distance == 0 or semi_axis_x <= 0 or semi_axis_y <= 0:
        return None

    unit_x = direction_x / center_distance
    unit_y = direction_y / center_distance
    angle_radians = math.radians(angle_degrees)
    cosine = math.cos(angle_radians)
    sine = math.sin(angle_radians)

    local_x = cosine * unit_x + sine * unit_y
    local_y = -sine * unit_x + cosine * unit_y
    edge_offset = 1 / math.sqrt((local_x / semi_axis_x) ** 2
                                + (local_y / semi_axis_y) ** 2)

    return center_distance + edge_offset


def find_line_intersection(ellipse1, ellipse2):
    """Upstream ``find_line_intersection``: cross the two pupil normals."""
    (cx1, cy1), (_, minor_axis1), angle1 = ellipse1
    (cx2, cy2), (_, minor_axis2), angle2 = ellipse2

    angle1_rad = math.radians(angle1)
    angle2_rad = math.radians(angle2)

    dx1 = (minor_axis1 / 2) * math.cos(angle1_rad)
    dy1 = (minor_axis1 / 2) * math.sin(angle1_rad)
    dx2 = (minor_axis2 / 2) * math.cos(angle2_rad)
    dy2 = (minor_axis2 / 2) * math.sin(angle2_rad)

    determinant = dx1 * (-dy2) - (-dx2) * dy1
    if abs(determinant) < 1e-9:
        return None

    bx = cx2 - cx1
    by = cy2 - cy1
    t1 = (bx * (-dy2) - (-dx2) * by) / determinant

    return (int(cx1 + t1 * dx1), int(cy1 + t1 * dy1))


def compute_gaze_vector(x, y, center_x, center_y, screen_width=640, screen_height=480):
    """Upstream ``compute_gaze_vector``, minus the per-frame file write.

    Returns ``(sphere_center_xyz, gaze_direction_xyz)`` in the same synthetic
    camera space the original used, so anything built against ``gaze_vector.txt``
    still works.
    """
    viewport_width = screen_width
    viewport_height = screen_height

    fov_y_deg = 45.0
    aspect_ratio = viewport_width / viewport_height
    far_clip = 100.0

    camera_position = np.array([0.0, 0.0, 3.0])

    fov_y_rad = np.radians(fov_y_deg)
    half_height_far = np.tan(fov_y_rad / 2) * far_clip
    half_width_far = half_height_far * aspect_ratio

    ndc_x = (2.0 * x) / viewport_width - 1.0
    ndc_y = 1.0 - (2.0 * y) / viewport_height

    far_x = ndc_x * half_width_far
    far_y = ndc_y * half_height_far
    far_z = camera_position[2] - far_clip
    far_point = np.array([far_x, far_y, far_z])

    ray_origin = camera_position
    ray_direction = far_point - camera_position
    ray_direction /= np.linalg.norm(ray_direction)
    ray_direction = -ray_direction

    inner_radius = 1.0 / 1.05
    sphere_offset_x = (center_x / screen_width) * 2.0 - 1.0
    sphere_offset_y = 1.0 - (center_y / screen_height) * 2.0
    sphere_center = np.array([sphere_offset_x * 1.5, sphere_offset_y * 1.5, 0.0])

    origin = ray_origin
    direction = -ray_direction
    L = origin - sphere_center

    a = np.dot(direction, direction)
    b = 2 * np.dot(direction, L)
    c = np.dot(L, L) - inner_radius ** 2

    discriminant = b ** 2 - 4 * a * c
    if discriminant < 0:
        # Tangent-point approximation when the ray misses the eye sphere.
        t = -np.dot(direction, L) / np.dot(direction, direction)
    else:
        sqrt_disc = np.sqrt(discriminant)
        t1 = (-b - sqrt_disc) / (2 * a)
        t2 = (-b + sqrt_disc) / (2 * a)

        t = None
        if t1 > 0 and t2 > 0:
            t = min(t1, t2)
        elif t1 > 0:
            t = t1
        elif t2 > 0:
            t = t2
        if t is None:
            return None, None

    intersection_point = origin + t * direction
    intersection_local = intersection_point - sphere_center
    norm = np.linalg.norm(intersection_local)
    if norm < 1e-9:
        return None, None
    target_direction = intersection_local / norm

    circle_local_center = np.array([0.0, 0.0, 1.0])

    rotation_axis = np.cross(circle_local_center, target_direction)
    rotation_axis_norm = np.linalg.norm(rotation_axis)
    if rotation_axis_norm < 1e-6:
        return sphere_center, circle_local_center

    rotation_axis /= rotation_axis_norm
    dot = float(np.clip(np.dot(circle_local_center, target_direction), -1.0, 1.0))
    angle_rad = math.acos(dot)

    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    one_minus = 1 - cos_a
    x_, y_, z_ = rotation_axis

    rotation_matrix = np.array([
        [one_minus * x_ * x_ + cos_a, one_minus * x_ * y_ - sin_a * z_, one_minus * x_ * z_ + sin_a * y_],
        [one_minus * x_ * y_ + sin_a * z_, one_minus * y_ * y_ + cos_a, one_minus * y_ * z_ - sin_a * x_],
        [one_minus * x_ * z_ - sin_a * y_, one_minus * y_ * z_ + sin_a * x_, one_minus * z_ * z_ + cos_a],
    ])

    gaze_local = np.array([0.0, 0.0, inner_radius])
    gaze_rotated = rotation_matrix @ gaze_local
    gaze_rotated /= np.linalg.norm(gaze_rotated)

    return sphere_center, gaze_rotated


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

@dataclass
class TrackingResult:
    ok: bool = False
    timestamp: float = 0.0
    frame_index: int = 0
    fps: float = 0.0
    process_ms: float = 0.0
    confidence: float = 0.0
    pupil_center: Optional[Tuple[float, float]] = None
    pupil_axes: Optional[Tuple[float, float]] = None
    pupil_angle: Optional[float] = None
    eye_center: Optional[Tuple[int, int]] = None
    sphere_radius: float = 0.0
    gaze_origin: Optional[Tuple[float, float, float]] = None
    gaze_direction: Optional[Tuple[float, float, float]] = None
    # Pupil position relative to the eye-sphere centre, normalised by the
    # sphere radius. This is the stable, resolution independent signal used
    # for screen/scene calibration.
    gaze_normalized: Optional[Tuple[float, float]] = None
    sphere_locked: bool = False
    ray_count: int = 0
    # True while the eye has been undetected for a blink-length streak.
    blinking: bool = False
    # Confirmed blinks (full close-open cycles) within the trailing window.
    # This one both decays and gets consumed by triple_blink, so it is for
    # display only - never count edges off it.
    blink_count_recent: int = 0
    # Monotonic blink count since the last reset. The gesture engine watches
    # this, so a blink is never lost to window decay or to the triple-blink
    # counter being cleared.
    blink_total: int = 0
    # How long the eye has been continuously shut, in milliseconds. A blink is
    # a couple of hundred ms; holding it shut for a second is a deliberate,
    # easy gesture that needs no timing skill - unlike triple blinking.
    eye_closed_ms: float = 0.0
    # One-frame pulse: just reached 3 blinks within the window.
    triple_blink: bool = False
    # False during the warm-up period right after a model reset, so gesture
    # detection doesn't fire while the eye-sphere is still settling.
    armed: bool = False
    # Why a candidate was accepted or rejected - shown in the UI so the
    # plausibility gates can be tuned against real hardware.
    metrics: dict = field(default_factory=dict)

    def to_dict(self):
        def rounded(seq, digits=4):
            if seq is None:
                return None
            return [round(float(v), digits) for v in seq]

        return {
            "ok": self.ok,
            "timestamp": round(self.timestamp, 3),
            "frame": self.frame_index,
            "fps": round(self.fps, 2),
            "process_ms": round(self.process_ms, 2),
            "confidence": round(self.confidence, 4),
            "pupil_center": rounded(self.pupil_center, 2),
            "pupil_axes": rounded(self.pupil_axes, 2),
            "pupil_angle": round(self.pupil_angle, 2) if self.pupil_angle is not None else None,
            "eye_center": rounded(self.eye_center, 1),
            "sphere_radius": round(self.sphere_radius, 2),
            "gaze_origin": rounded(self.gaze_origin),
            "gaze_direction": rounded(self.gaze_direction),
            "gaze_normalized": rounded(self.gaze_normalized),
            "sphere_locked": self.sphere_locked,
            "rays": self.ray_count,
            "blinking": self.blinking,
            "blink_count_recent": self.blink_count_recent,
            "blink_total": self.blink_total,
            "eye_closed_ms": round(self.eye_closed_ms),
            "triple_blink": self.triple_blink,
            "armed": self.armed,
            "metrics": self.metrics,
        }


class EyeTracker:
    """Stateful port of the upstream tracker (globals -> instance attributes)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._apply_scale()
        self.reset()

    # -- setup ----------------------------------------------------------
    def _apply_scale(self):
        cfg = self.cfg
        scale = cfg.scale  # proc_width / 640

        self.ignore_bounds = max(4, int(round(20 * scale)))
        self.search_area = max(6, int(round(20 * scale)))
        self.mask_square = max(48, int(round(250 * scale)))
        self.min_contour_area = max(80, int(round(1000 * scale * scale)))
        self.contour_ratio_thresh = 3
        kernel_size = 5 if scale > 0.7 else 3
        self.kernel = np.ones((kernel_size, kernel_size), np.uint8)
        self.dilate_iterations = 2
        self.ellipse_thick = max(4, int(round(10 * scale)))
        self.ellipse_thin = max(2, int(round(4 * scale)))
        self.intersection_pixel_limit = max(8, int(round(30 * scale)))
        self.thresholds = (cfg.threshold_strict, cfg.threshold_medium, cfg.threshold_relaxed)
        # Small kernel: an opening with the main kernel would eat the pupil too.
        self.open_kernel = np.ones((3, 3), np.uint8)
        self.max_contour_area = cfg.pupil_max_area_fraction * cfg.proc_width * cfg.proc_height

    def reset(self):
        """Upstream ``reset_tracking_state``."""
        cfg = self.cfg
        self.ray_lines = []
        self.model_centers = []
        self.stored_intersections = []
        self.prev_model_center_avg = (cfg.proc_width // 2, cfg.proc_height // 2)
        self.max_observed_distance = 0.0
        self.last_sphere_radius_ellipse = None
        self.eye_sphere_adjustment_enabled = True
        self.stuck_ellipses = []
        self.capture_stuck_ellipses = False
        self.capture_frame_counter = 0
        self.frame_index = 0
        self._last_time = None
        self._fps = 0.0
        self.last_result = TrackingResult()

        self.session_start = time.time()
        self._blink_active = False
        self._blink_start = 0.0
        self._blink_events = []
        self.blink_total = 0
        self._last_pupil_center = None
        self._last_pupil_time = 0.0

    # -- controls -------------------------------------------------------
    def toggle_sphere_adjustment(self):
        """Upstream 'F' key: freeze or unfreeze the eye sphere model."""
        self.eye_sphere_adjustment_enabled = not self.eye_sphere_adjustment_enabled
        return self.eye_sphere_adjustment_enabled

    def toggle_ellipse_capture(self):
        """Upstream 'e' key."""
        self.capture_stuck_ellipses = not self.capture_stuck_ellipses
        self.capture_frame_counter = 0
        return self.capture_stuck_ellipses

    def clear_stuck_ellipses(self):
        """Upstream 'c' key."""
        self.stuck_ellipses = []

    # -- eye sphere -----------------------------------------------------
    def update_eye_sphere_radius(self, eye_center, current_pupil_ellipse, confidence):
        """Upstream ``update_eye_sphere_radius``."""
        if self.last_sphere_radius_ellipse is not None:
            anchored = distance_to_pupil_outer_edge(eye_center, self.last_sphere_radius_ellipse)
            if anchored is not None:
                self.max_observed_distance = anchored

        if (current_pupil_ellipse is not None
                and confidence >= self.cfg.pupil_confidence_threshold_sphere
                and len(self.model_centers) >= self.cfg.min_model_centers):
            current = distance_to_pupil_outer_edge(eye_center, current_pupil_ellipse)
            if current is not None and (self.last_sphere_radius_ellipse is None
                                        or current > self.max_observed_distance):
                self.max_observed_distance = current
                self.last_sphere_radius_ellipse = current_pupil_ellipse

    def update_and_average_point(self, new_point, window):
        """Upstream ``update_and_average_point``."""
        self.model_centers.append(new_point)
        if len(self.model_centers) > window:
            del self.model_centers[:len(self.model_centers) - window]
        if not self.model_centers:
            return None
        array = np.asarray(self.model_centers, dtype=np.float32)
        mean = array.mean(axis=0)
        return (int(mean[0]), int(mean[1]))

    def compute_average_intersection(self, shape, number_lines, total_lines,
                                     minimum_angle_degrees):
        """Upstream ``compute_average_intersection``.

        Returns ``None`` when nothing usable was found this frame. Upstream
        returned ``(0, 0)`` and then tested ``model_center_average[0] == 320``
        as a "no data" sentinel, which silently mis-fires whenever the real
        centre lands on x=320. The explicit ``None`` avoids that.
        """
        pixel_limit = self.intersection_pixel_limit
        angle_threshold = 5

        if len(self.ray_lines) < 2 or number_lines < 2:
            return None

        height, width = shape[:2]
        selected = random.sample(self.ray_lines, min(number_lines, len(self.ray_lines)))

        intersections = []
        for i in range(len(selected) - 1):
            line1, line2 = selected[i], selected[i + 1]
            if abs(line1[2] - line2[2]) >= minimum_angle_degrees:
                point = find_line_intersection(line1, line2)
                if point and 0 <= point[0] < width and 0 <= point[1] < height:
                    intersections.append(point)

        if not intersections:
            return None

        accept = True
        if len(intersections) >= 2:
            for i in range(len(intersections)):
                for j in range(i + 1, len(intersections)):
                    dx = intersections[i][0] - intersections[j][0]
                    dy = intersections[i][1] - intersections[j][1]
                    if math.hypot(dx, dy) > pixel_limit:
                        accept = False
                        break
                    if abs(selected[i][2] - selected[j][2]) < angle_threshold:
                        accept = False
                        break
                if not accept:
                    break

        if accept:
            self.stored_intersections.extend(intersections)

        if len(self.stored_intersections) > total_lines:
            self.stored_intersections = self.stored_intersections[-total_lines:]

        if not self.stored_intersections:
            return None

        array = np.asarray(self.stored_intersections, dtype=np.float32)
        mean = array.mean(axis=0)
        if not np.all(np.isfinite(mean)):
            return None
        return (int(mean[0]), int(mean[1]))

    # -- gestures ---------------------------------------------------------
    def _update_blink(self, ok, now):
        """Dropout-based blink detection: a short 'pupil not found' streak.

        Distinguishes a blink from real tracking loss purely by duration -
        a blink is quick (``blink_min_ms``..``blink_max_ms``); glasses
        slipping or the eye leaving the frame lasts much longer and is
        silently ignored. Three confirmed blinks inside ``blink_window_ms``
        raise ``triple_blink`` for one frame, but only once the model has
        had ``blink_warmup_s`` to settle after the last reset - otherwise
        the noisy first seconds after a reset could fire it by accident.
        """
        cfg = self.cfg
        triple = False

        if not ok:
            if not self._blink_active:
                self._blink_active = True
                self._blink_start = now
        elif self._blink_active:
            self._blink_active = False
            duration_ms = (now - self._blink_start) * 1000.0
            if cfg.blink_min_ms <= duration_ms <= cfg.blink_max_ms:
                self._blink_events.append(now)
                self.blink_total += 1

        window_s = cfg.blink_window_ms / 1000.0
        self._blink_events = [t for t in self._blink_events if now - t <= window_s]

        armed = (now - self.session_start) >= cfg.blink_warmup_s
        if armed and len(self._blink_events) >= 3:
            triple = True
            self._blink_events = []  # consume so it fires once, not every frame

        closed_ms = (now - self._blink_start) * 1000.0 if self._blink_active else 0.0
        return (self._blink_active, len(self._blink_events), triple, armed,
                self.blink_total, closed_ms)

    # -- per frame ------------------------------------------------------
    def process_frame(self, frame, draw=True, timestamp=None):
        """Detect the pupil in one BGR frame and update the 3D eye model.

        ``timestamp`` overrides the wall clock. Live capture leaves it None;
        offline analysis passes the frame's real presentation time, otherwise
        blink durations are measured against how fast the file decodes rather
        than against the video's own timeline.
        """
        started = time.perf_counter()
        now = time.time() if timestamp is None else float(timestamp)
        cfg = self.cfg

        # Drop a stale position reference so the jump gate does not block
        # re-acquiring the pupil somewhere else after a real blink.
        if (self._last_pupil_center is not None
                and now - self._last_pupil_time > cfg.pupil_track_timeout_s):
            self._last_pupil_center = None

        # Some V4L2 nodes open and deliver "frames" that are not images at all
        # (metadata streams, 1xN buffers). Say so plainly instead of throwing
        # from somewhere deep in the pipeline.
        if frame is None or frame.ndim < 2 or min(frame.shape[:2]) < 16:
            raise ValueError("frame is not an image: shape=%s"
                             % (None if frame is None else frame.shape,))
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        frame = crop_to_aspect_ratio(frame, cfg.proc_width, cfg.proc_height)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

        darkest_point = get_darkest_area(gray, self.ignore_bounds, self.search_area)
        darkest_value = int(gray[darkest_point[1], darkest_point[0]])

        if cfg.roi_mode:
            x0, y0, x1, y1 = square_roi(gray.shape, darkest_point, self.mask_square)
            work = gray[y0:y1, x0:x1]
            offset = (x0, y0)
        else:
            work = gray
            offset = (0, 0)

        best = self._select_best_contour(work, darkest_value, cfg.roi_mode, darkest_point, offset)
        contour, confidence, center, metrics = best

        pupil_ellipse = None
        if contour is not None:
            contour = optimize_contours_by_angle(contour)
            if contour is not None and len(contour) >= 5:
                ellipse = cv2.fitEllipse(contour)
                pupil_ellipse = ((ellipse[0][0] + offset[0], ellipse[0][1] + offset[1]),
                                 ellipse[1], ellipse[2])
                center = (int(pupil_ellipse[0][0]), int(pupil_ellipse[0][1]))

        # Collect pupil rays only when the fit is trustworthy (upstream logic).
        if (pupil_ellipse is not None and self.eye_sphere_adjustment_enabled
                and confidence >= cfg.pupil_confidence_threshold):
            self.ray_lines.append(pupil_ellipse)
            if len(self.ray_lines) > cfg.max_rays:
                del self.ray_lines[:len(self.ray_lines) - cfg.max_rays]

        model_center_average = None
        if self.eye_sphere_adjustment_enabled:
            model_center = self.compute_average_intersection(
                gray.shape,
                cfg.intersection_ray_count,
                cfg.max_stored_intersections,
                cfg.minimum_intersection_angle_degrees,
            )
            if model_center is not None:
                model_center_average = self.update_and_average_point(
                    model_center, cfg.model_center_average_window)

        if model_center_average is None:
            model_center_average = self.prev_model_center_avg
        else:
            self.prev_model_center_avg = model_center_average

        if self.eye_sphere_adjustment_enabled:
            self.update_eye_sphere_radius(model_center_average, pupil_ellipse, confidence)

        # Stuck-ellipse capture (upstream 'e' mode).
        if pupil_ellipse is not None and self.capture_stuck_ellipses:
            self.capture_frame_counter += 1
            if self.capture_frame_counter % 5 == 0:
                self.stuck_ellipses.append(pupil_ellipse)
                if len(self.stuck_ellipses) > 20:
                    self.stuck_ellipses = self.stuck_ellipses[-20:]

        if self._last_time is not None:
            delta = now - self._last_time
            if delta > 0:
                instant = 1.0 / delta
                self._fps = instant if self._fps == 0 else self._fps * 0.85 + instant * 0.15
        self._last_time = now
        self.frame_index += 1

        result = TrackingResult(
            timestamp=now,
            frame_index=self.frame_index,
            fps=self._fps,
            confidence=float(confidence),
            eye_center=model_center_average,
            sphere_radius=float(self.max_observed_distance),
            sphere_locked=not self.eye_sphere_adjustment_enabled,
            ray_count=len(self.ray_lines),
            metrics=metrics,
        )

        # Physical gate: once the eye-sphere model exists, a real pupil sits on
        # the eyeball surface. A candidate further from the sphere centre than
        # the sphere radius is off the eyeball entirely - typically a lash or
        # lid blob during a blink - so it cannot be a pupil.
        if pupil_ellipse is not None and center is not None and self.max_observed_distance > 1:
            offset_from_eye = math.hypot(center[0] - model_center_average[0],
                                         center[1] - model_center_average[1])
            limit = self.max_observed_distance * cfg.pupil_max_eye_radius_fraction
            if offset_from_eye > limit:
                metrics = {"rejected": {"outside_eyeball":
                                        round(offset_from_eye / self.max_observed_distance, 2)}}
                result.metrics = metrics
                pupil_ellipse, center = None, None

        if pupil_ellipse is not None and center is not None:
            result.ok = True
            self._last_pupil_center = center
            self._last_pupil_time = now
            result.pupil_center = (float(pupil_ellipse[0][0]), float(pupil_ellipse[0][1]))
            result.pupil_axes = (float(pupil_ellipse[1][0]), float(pupil_ellipse[1][1]))
            result.pupil_angle = float(pupil_ellipse[2])

            origin, direction = compute_gaze_vector(
                center[0], center[1],
                model_center_average[0], model_center_average[1],
                cfg.proc_width, cfg.proc_height,
            )
            if origin is not None and direction is not None:
                result.gaze_origin = tuple(float(v) for v in origin)
                result.gaze_direction = tuple(float(v) for v in direction)

            radius = self.max_observed_distance if self.max_observed_distance > 1 else max(
                cfg.proc_width * 0.25, 1.0)
            # The eye-sphere centre estimate carries a bias, so looking
            # straight ahead can read as a hard stare to one side - enough to
            # trip the side menus permanently. gaze_offset_* is captured once
            # by "set centre" in the UI and removed here, at the source, so
            # every consumer downstream sees a centred signal.
            result.gaze_normalized = (
                (center[0] - model_center_average[0]) / radius - cfg.gaze_offset_x,
                (center[1] - model_center_average[1]) / radius - cfg.gaze_offset_y,
            )

        (blinking, blink_count, triple, armed, total,
         closed_ms) = self._update_blink(result.ok, now)
        result.blinking = blinking
        result.blink_count_recent = blink_count
        result.triple_blink = triple
        result.armed = armed
        result.blink_total = total
        result.eye_closed_ms = closed_ms

        if draw:
            self._draw_overlay(frame, result, model_center_average, center, pupil_ellipse)

        result.process_ms = (time.perf_counter() - started) * 1000.0
        self.last_result = result
        return result, frame

    def _select_best_contour(self, work_gray, darkest_value, roi_mode, darkest_point, offset):
        """Upstream's three-threshold beauty contest, run on the ROI."""
        best_contour = None
        best_goodness = 0.0
        best_ratio = 0.0
        best_center = None
        best_metrics = {}
        rejected = {}
        frame_area = float(self.cfg.proc_width * self.cfg.proc_height)

        for added in self.thresholds:
            binary = apply_binary_threshold(work_gray, darkest_value, added)
            if not roi_mode:
                binary = mask_outside_square(binary, darkest_point, self.mask_square)

            # Eyelashes are thin dark streaks. Opening (erode then dilate)
            # deletes structures thinner than the kernel while leaving the
            # pupil - which is big and solid - essentially untouched. Without
            # it, the dilation below fattens lashes into pupil-sized blobs.
            if self.cfg.lash_open_iterations > 0:
                binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, self.open_kernel,
                                          iterations=self.cfg.lash_open_iterations)

            dilated = cv2.dilate(binary, self.kernel, iterations=self.dilate_iterations)
            contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            reduced = filter_contours_by_area_and_return_largest(
                contours, self.min_contour_area, self.contour_ratio_thresh)

            if not reduced or len(reduced[0]) <= 5:
                continue

            contour = reduced[0]
            ellipse = cv2.fitEllipse(contour)
            axis_a, axis_b = ellipse[1]
            if axis_a <= 0 or axis_b <= 0:
                continue

            # Reject implausible pupils before they can win the contest: a
            # pupil is roughly round and cannot cover a third of the frame.
            # A lash clump or lid crease fails one or both.
            circularity = min(axis_a, axis_b) / max(axis_a, axis_b)
            area = math.pi * axis_a * axis_b / 4.0
            if circularity < self.cfg.pupil_min_circularity:
                rejected["circularity"] = round(circularity, 3)
                continue
            if area > self.max_contour_area:
                rejected["area"] = int(area)
                continue

            goodness = check_ellipse_goodness(dilated, contour)
            pixels = check_contour_pixels(contour, dilated.shape,
                                          self.ellipse_thick, self.ellipse_thin)

            # goodness[2] (roundness) was computed upstream but never used;
            # folding it in makes the rounder candidate win a close contest.
            final_goodness = goodness[0] * pixels[0] * pixels[0] * pixels[1] * goodness[2]
            if final_goodness > 0 and final_goodness > best_goodness:
                best_goodness = final_goodness
                best_ratio = pixels[1]
                best_contour = contour
                best_center = (int(ellipse[0][0]) + offset[0], int(ellipse[0][1]) + offset[1])
                best_metrics = {"circularity": round(circularity, 3),
                                "area_fraction": round(area / frame_area, 3)}

        # Final gate: a weak fit is not a pupil, it is a closed eye or noise.
        if best_contour is not None and best_ratio < self.cfg.pupil_min_confidence:
            rejected["confidence"] = round(best_ratio, 3)
            best_contour, best_center = None, None

        # Temporal gate: an unconvincing candidate that jumped across the
        # frame is a lash or lid lock-on, not a saccade.
        if (best_contour is not None
                and best_ratio < self.cfg.pupil_jump_trust_confidence
                and self._last_pupil_center is not None):
            jump = math.hypot(best_center[0] - self._last_pupil_center[0],
                              best_center[1] - self._last_pupil_center[1])
            if jump > self.cfg.pupil_max_jump_fraction * self.cfg.proc_width:
                rejected["jump"] = int(jump)
                best_contour, best_center = None, None

        if best_contour is None and rejected:
            best_metrics = {"rejected": rejected}

        return best_contour, best_ratio, best_center, best_metrics

    def _draw_overlay(self, frame, result, model_center, center, pupil_ellipse):
        """Same overlay upstream drew, written onto the frame we stream."""
        if frame.ndim != 3:
            return

        model_center = (int(model_center[0]), int(model_center[1]))
        radius = int(self.max_observed_distance)
        if 0 < radius < max(frame.shape) * 2:
            cv2.circle(frame, model_center, radius, (255, 50, 50), 1)
        cv2.circle(frame, model_center, 5, (255, 255, 0), -1)

        for ellipse in self.stuck_ellipses:
            cv2.ellipse(frame, ellipse, (0, 255, 255), 1)

        if pupil_ellipse is not None and center is not None:
            cv2.line(frame, model_center, center, (255, 150, 50), 1)
            cv2.ellipse(frame, pupil_ellipse, (20, 255, 255), 1)
            extended = (int(model_center[0] + 2 * (center[0] - model_center[0])),
                        int(model_center[1] + 2 * (center[1] - model_center[1])))
            cv2.line(frame, center, extended, (200, 255, 0), 2)

        scale = 0.38 if self.cfg.proc_width < 480 else 0.5
        step = int(round(15 * scale / 0.38))

        top = "conf %.0f%%  fps %.1f  rays %d%s%s" % (
            result.confidence * 100, result.fps, len(self.ray_lines),
            "  LOCKED" if not self.eye_sphere_adjustment_enabled else "",
            "  BLINK" if result.blinking else "")
        self._put_text(frame, top, (6, step), scale)

        if self.cfg.show_guide:
            self._draw_guide(frame, result)

        if not result.armed:
            self._put_text(frame, "gestures warming up...", (6, step * 2), scale)
        elif result.blink_count_recent > 0:
            self._put_text(frame, "blinks: %d" % result.blink_count_recent, (6, step * 2), scale)

        # Upstream printed the gaze vector along the bottom edge; keeping it
        # there leaves the eye itself unobstructed.
        if result.gaze_origin is not None:
            height = frame.shape[0]
            self._put_text(frame, "O %.2f %.2f %.2f" % result.gaze_origin,
                           (6, height - step - 4), scale)
            self._put_text(frame, "D %.2f %.2f %.2f" % result.gaze_direction,
                           (6, height - 5), scale)

    def _draw_guide(self, frame, result):
        """Legend on the eye view: what each direction actually does.

        Without it there is no way to learn the controls except by reading
        source or guessing, and the person using this cannot do either.
        """
        height, width = frame.shape[:2]
        centre = (width // 2, height // 2)
        gaze = result.gaze_normalized

        # Circle marking where the answer threshold sits, drawn at the same
        # scale the thresholds are actually evaluated in.
        radius = int(self.cfg.answer_zone_threshold * min(width, height) * 0.5)
        if 4 < radius < max(width, height):
            cv2.circle(frame, centre, radius, (90, 90, 90), 1)

        labels = [
            ("DA", centre[0] - 10, 12),
            ("NU", centre[0] - 10, height - 4),
            ("nevoi", 4, centre[1]),
            ("dureri", width - 44, centre[1]),
        ]
        for text, x, y in labels:
            cv2.putText(frame, text, (x + 1, y + 1), cv2.FONT_HERSHEY_SIMPLEX,
                        0.32, (0, 0, 0), 2, cv2.LINE_AA)
            cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                        0.32, (150, 150, 150), 1, cv2.LINE_AA)

        # Live dot showing where the current gaze sits relative to those zones.
        if gaze is not None:
            dot = (int(centre[0] + gaze[0] * min(width, height) * 0.5),
                   int(centre[1] + gaze[1] * min(width, height) * 0.5))
            if 0 <= dot[0] < width and 0 <= dot[1] < height:
                cv2.circle(frame, dot, 4, (0, 0, 0), -1)
                cv2.circle(frame, dot, 3, (80, 200, 255), -1)

    @staticmethod
    def _put_text(frame, text, origin, scale):
        cv2.putText(frame, text, (origin[0] + 1, origin[1] + 1),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, text, origin,
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 255, 0), 1, cv2.LINE_AA)
