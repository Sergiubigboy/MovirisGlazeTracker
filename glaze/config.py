"""Configuration for the Raspberry Pi port of the Orlosky 3D eye tracker.

All tunables live here so the tracker itself stays close to the upstream code.
Values that were hard-coded for 640x480 in the original script are expressed as
a function of the processing resolution, so lowering ``proc_width`` on a weak
Pi does not silently break the detector.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict, field


@dataclass
class Config:
    # ---- cameras -------------------------------------------------------
    # "usb:N"  -> /dev/videoN through V4L2
    # "csi"    -> Raspberry Pi CSI camera through picamera2
    # "/path/to/file.mp4" -> video file (for testing without hardware)
    # ""       -> disabled
    eye_source: str = "usb:0"
    scene_source: str = "csi"

    # Capture resolution asked from the driver (not the processing resolution).
    eye_capture_width: int = 640
    eye_capture_height: int = 480
    eye_capture_fps: int = 30
    # GC0308-class UVC cameras often only expose YUYV; MJPG is tried first and
    # we silently fall back when the driver refuses it.
    eye_fourcc: str = "MJPG"

    scene_capture_width: int = 640
    scene_capture_height: int = 480
    scene_capture_fps: int = 15

    # ---- processing ----------------------------------------------------
    # The upstream tracker runs at 640x480. On a Pi 3A+ that costs far too
    # much, 320x240 keeps the same behaviour at ~4x less pixel work.
    proc_width: int = 320
    proc_height: int = 240

    # Upstream masked the full frame around the darkest point. We instead crop
    # to that square and do every threshold/dilate/contour pass inside it.
    roi_mode: bool = True

    # Upstream flipped the camera vertically before processing (cv2.flip(f, 0)).
    flip_eye_vertical: bool = True
    flip_eye_horizontal: bool = False
    rotate_eye_degrees: int = 0  # 0 / 90 / 180 / 270

    flip_scene_vertical: bool = False
    flip_scene_horizontal: bool = False

    # Cap the tracking loop so the Pi keeps some CPU for the web server.
    max_tracking_fps: float = 30.0

    # ---- detector tunables (upstream names kept) ------------------------
    threshold_strict: int = 5
    threshold_medium: int = 15
    threshold_relaxed: int = 25
    min_model_centers: int = 30
    max_rays: int = 100
    pupil_confidence_threshold: float = 0.85
    pupil_confidence_threshold_sphere: float = 0.65
    intersection_ray_count: int = 4
    minimum_intersection_angle_degrees: float = 8.0
    max_stored_intersections: int = 1500
    model_center_average_window: int = 200

    # ---- output --------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    jpeg_quality: int = 60
    # Streaming is throttled independently of tracking; encoding only happens
    # while a browser is actually attached.
    stream_fps: float = 15.0
    scene_stream_fps: float = 8.0
    # Draw the debug overlay (ellipse, sphere, gaze line). Turning this off
    # gives a few extra FPS on the Pi when nobody is watching the video.
    draw_overlay: bool = True

    # Upstream rewrote gaze_vector.txt on every single frame. On an SD card
    # that is a bad idea, so it is opt-in here.
    write_gaze_file: bool = False
    gaze_file_path: str = "gaze_vector.txt"
    # Optional raw UDP feed, "host:port", e.g. for Unity/Processing on the laptop.
    udp_target: str = ""

    calibration_path: str = "calibration.json"

    # Populated at runtime, not user facing.
    extra: dict = field(default_factory=dict)

    # ---- derived -------------------------------------------------------
    @property
    def scale(self) -> float:
        """Ratio between the processing resolution and upstream's 640px."""
        return self.proc_width / 640.0

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("extra", None)
        return json.dumps(data, indent=2)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        return cls(**{k: v for k, v in data.items() if k in known})


# Keep the processing resolution an exact half/quarter of the capture
# resolution (never a non-integer downscale - it loses OpenCV's fast
# INTER_AREA path, see tools/bench.py). Always CAPTURE at each camera's native
# resolution, though, not at the (small) processing size directly: cheap UVC
# sensors like the GC0308 commonly implement their low-resolution modes as a
# centre CROP of the sensor rather than a scaled-down full field of view, so
# capturing straight at 320x240 quietly loses most of the picture. The saved
# resize cost (~0.1ms) is not worth trading away field of view for.
PRESETS = {
    # Pi 3A+ / Pi Zero 2 W: 512 MB RAM. Still captures at native
    # resolution (correct field of view) and downsamples in software.
    "lite": dict(proc_width=320, proc_height=240,
                 eye_capture_width=640, eye_capture_height=480,
                 scene_capture_width=640, scene_capture_height=480,
                 stream_fps=8.0, scene_stream_fps=5.0,
                 jpeg_quality=55, max_tracking_fps=20.0),
    # Default: capture at 640x480 for a sharper pupil edge, process at half.
    "balanced": dict(proc_width=320, proc_height=240,
                     eye_capture_width=640, eye_capture_height=480),
    # Pi 4/5 or a desktop.
    "quality": dict(proc_width=640, proc_height=480,
                    eye_capture_width=640, eye_capture_height=480,
                    stream_fps=20.0, scene_stream_fps=12.0,
                    jpeg_quality=70, max_tracking_fps=60.0),
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glaze",
        description="Headless 3D eye tracker for Raspberry Pi with a web UI "
                    "(port of JEOresearch/EyeTracker Orlosky3DEyeTracker).",
    )
    parser.add_argument("--config", help="load a JSON config file first")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="performance preset")
    parser.add_argument("--eye", dest="eye_source",
                        help="eye camera: usb:0 | csi | /path/video.mp4")
    parser.add_argument("--scene", dest="scene_source",
                        help="scene camera: csi | usb:1 | empty string to disable")
    parser.add_argument("--proc-width", type=int, dest="proc_width")
    parser.add_argument("--proc-height", type=int, dest="proc_height")
    parser.add_argument("--port", type=int)
    parser.add_argument("--host")
    parser.add_argument("--jpeg-quality", type=int, dest="jpeg_quality")
    parser.add_argument("--stream-fps", type=float, dest="stream_fps")
    parser.add_argument("--max-fps", type=float, dest="max_tracking_fps")
    parser.add_argument("--no-flip", action="store_true",
                        help="do not flip the eye image vertically")
    parser.add_argument("--rotate", type=int, dest="rotate_eye_degrees",
                        choices=[0, 90, 180, 270])
    parser.add_argument("--no-overlay", action="store_true",
                        help="skip drawing the debug overlay (faster)")
    parser.add_argument("--no-roi", action="store_true",
                        help="use the original full-frame masking instead of the ROI crop")
    parser.add_argument("--write-gaze-file", action="store_true",
                        help="also write gaze_vector.txt like the original script")
    parser.add_argument("--udp", dest="udp_target", help="stream gaze over UDP, host:port")
    parser.add_argument("--save-config", help="write the effective config to this path and exit")
    return parser


def config_from_args(argv=None) -> Config:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cfg = Config.load(args.config) if args.config else Config()

    if args.preset:
        for key, value in PRESETS[args.preset].items():
            setattr(cfg, key, value)

    simple = ("eye_source", "scene_source", "proc_width", "proc_height", "port",
              "host", "jpeg_quality", "stream_fps", "max_tracking_fps",
              "rotate_eye_degrees", "udp_target")
    for key in simple:
        value = getattr(args, key, None)
        if value is not None:
            setattr(cfg, key, value)

    if args.no_flip:
        cfg.flip_eye_vertical = False
    if args.no_overlay:
        cfg.draw_overlay = False
    if args.no_roi:
        cfg.roi_mode = False
    if args.write_gaze_file:
        cfg.write_gaze_file = True

    if args.save_config:
        cfg.save(args.save_config)
        print("Config written to " + os.path.abspath(args.save_config))
        raise SystemExit(0)

    return cfg
