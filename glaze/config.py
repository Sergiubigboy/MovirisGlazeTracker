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
    # "auto"   -> pick from the cameras that actually deliver frames, in order
    #             (first = eye, second = scene), flipped by camera_swapped.
    #             Survives /dev/videoN renumbering when ports change.
    # "usb:N"  -> /dev/videoN through V4L2
    # "csi"    -> Raspberry Pi CSI camera through picamera2
    # "/path/to/file.mp4" -> video file (for testing without hardware)
    # ""       -> disabled
    eye_source: str = "auto"
    scene_source: str = "auto"
    # Which of the two auto-detected cameras is the eye. Toggled by the swap
    # button and persisted, because the detection order is arbitrary - there
    # is no way to tell two identical camera modules apart from their nodes.
    camera_swapped: bool = False

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
    # Seconds the tracking loop may go without completing an iteration before
    # the watchdog reopens the cameras. Generous: a slow frame is normal, a
    # loop that has not moved in eight seconds is stuck.
    watchdog_timeout: float = 8.0

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

    # ---- pupil plausibility gates ----------------------------------------
    # Upstream always reported whatever ellipse fitted best, however bad, so a
    # closed eye still produced a confident "pupil" sitting on the lid crease
    # or a clump of eyelashes - which also made blink detection impossible.
    # A candidate now has to survive these before it counts as a pupil.
    pupil_min_confidence: float = 0.35   # share of contour lying on the ellipse
    pupil_min_circularity: float = 0.45  # minor/major axis; lashes are streaks
    pupil_max_area_fraction: float = 0.35  # of the processing frame
    # A pupil cannot teleport across the frame between two frames. A weak
    # candidate that did is a lash/lid lock-on, not a saccade - so the jump
    # limit only applies below pupil_jump_trust_confidence, letting a
    # genuinely confident fast saccade through.
    pupil_max_jump_fraction: float = 0.35  # of the processing width, per frame
    pupil_jump_trust_confidence: float = 0.60
    # Forget the previous position after this long without a pupil, so
    # re-acquisition anywhere in the frame is allowed after a real blink.
    pupil_track_timeout_s: float = 0.5
    # Once the eye-sphere model is established, the pupil must lie on the
    # eyeball. Anything beyond this multiple of the sphere radius is off the
    # eyeball entirely - a lash or lid blob, not a pupil. The slack above 1.0
    # absorbs error in the sphere estimate itself.
    pupil_max_eye_radius_fraction: float = 1.15
    # Eyelashes are thin and dark; an opening (erode then dilate) removes them
    # before the dilation that would otherwise fatten them into blobs.
    lash_open_iterations: int = 1

    # ---- blink detection -------------------------------------------------
    # A blink is a "no pupil found" streak whose duration lands in this
    # window - short enough to not be tracking loss (glasses moved, eye out
    # of frame), long enough to not be one noisy frame.
    blink_min_ms: float = 60.0
    blink_max_ms: float = 400.0
    # 3 blinks within this window count as one "triple blink" gesture.
    blink_window_ms: float = 1500.0
    # Gesture detection is armed only this long after the model was last
    # reset, so the eye-sphere calibration settles before gestures can fire.
    blink_warmup_s: float = 8.0

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
    gestures_path: str = "gestures.json"
    # Settings changed from the web UI are written here and reloaded at
    # startup. Without this, every tweak - including which camera is the eye -
    # would be lost on the next reboot, which is useless for a device meant to
    # be left running.
    runtime_config_path: str = "runtime.json"

    # ---- gestures --------------------------------------------------------
    # How far the pupil must move off centre (in eye-radius units) to count as
    # looking left/right/up/down, and how far back it must come before that
    # direction can fire again.
    gaze_zone_enter: float = 0.35
    gaze_zone_exit: float = 0.20

    # Subtracted from gaze_normalized at the source. The eye-sphere centre
    # estimate is biased in practice, so "looking straight" can read as a
    # steady offset to one side - which permanently trips the side menus and
    # skews every direction. Captured once by "setează centrul" in the UI.
    gaze_offset_x: float = 0.0
    gaze_offset_y: float = 0.0

    # Holding both eyes shut this long is the main control gesture: once to
    # start choosing, once more when finished. Far easier to perform reliably
    # than triple-blinking, which needs timing the person may not have.
    # What may START a session: "none" (only the button / API), "long_close"
    # or "triple_blink". Defaults to none because every gesture tried so far
    # is derived from "no pupil found", which also fires on tracking dropouts -
    # and an unwanted session costs the wearer far more than a missed one.
    start_gesture: str = "none"
    long_close_ms: float = 900.0
    # ...and no longer than this. "Eye closed" really means "no pupil found",
    # which also covers tracking dropouts and simply resting with the eyes
    # shut. Bounding the hold at both ends, and only acting once the eye
    # reopens, is what separates a deliberate command from either.
    long_close_max_ms: float = 2500.0
    # Ignore further closures this long after one fires, so a single hold
    # cannot immediately count twice - and so the natural blink right after a
    # sentence is spoken does not start a new session.
    long_close_cooldown_s: float = 4.0
    # Ask before starting. A false start costs the wearer real time and
    # attention, so one cheap yes/no is worth it - the same reasoning as the
    # menu confirmation.
    confirm_start: bool = True
    # Draw a legend on the eye view showing what each direction does.
    show_guide: bool = True

    # ---- vision model ----------------------------------------------------
    # The API key is NOT stored here - it comes from the GEMINI_API_KEY
    # environment variable or a gitignored gemini_key.txt, so the settings
    # page can never read it back out.
    # Google retires model names periodically (2.5-flash-lite stopped taking
    # new users), and the API says which one to move to in the 404 body. This
    # is editable from the settings page so a rename needs no code change.
    vision_model: str = "gemini-3.5-flash-lite"
    vision_enabled: bool = True
    # Object names come back in this language, and it also picks the espeak
    # voice - the two must agree, or you get English text read with a
    # Romanian voice (or vice versa) and it comes out mangled.
    vision_language: str = "Romanian"

    # ---- conversation (AAC slot filling) ----------------------------------
    # The whole flow: trigger -> collect a few gaze-marked photos -> one AI
    # call that fills four pillars (person / action / object / emotion) with
    # confidences -> spoken yes-no questions for whatever came back unsure ->
    # final sentence out loud.
    conversation_enabled: bool = True
    # Hold the scene cursor still this long to capture one photo.
    capture_dwell_ms: float = 1000.0
    # How far the cursor must move (fraction of the frame) before it counts as
    # a different target - stops one long stare becoming three photos.
    capture_move_fraction: float = 0.15
    max_captures: int = 3
    # Give up collecting and analyse what we have after this long.
    capture_window_s: float = 25.0

    # How photos are taken during selection.
    #   "manual" - only when the button is pressed (predictable, testable)
    #   "dwell"  - automatically after holding the gaze still
    capture_mode: str = "manual"
    # How questions get answered.
    #   "gaze"    - look up = yes, look down = no
    #   "buttons" - the DA / NU buttons in the web UI
    #   "both"    - either
    answer_mode: str = "both"

    # Answering: look up = yes, look down = no, held for answer_dwell_ms.
    answer_dwell_ms: float = 500.0
    # No answer within this long counts as "no", move to the next option.
    answer_timeout_ms: float = 4000.0
    # Vertical gaze offset (in eye-radius units) that counts as up/down.
    # Deliberately far out: small involuntary movements must never register as
    # an answer. Tune it live on the /test page, which shows the live value
    # against the threshold.
    answer_zone_threshold: float = 0.45
    # A pillar at or above this confidence is accepted without asking.
    pillar_confidence_threshold: float = 0.90
    max_options_per_pillar: int = 4
    # Hold a long look left/right to open the needs / pain menus, which need
    # no camera and no AI - they matter most when nothing is in frame.
    menu_dwell_ms: float = 1200.0
    # Deliberately further out than answer_zone_threshold: glancing aside at
    # something must not open a menu, only a pointed look does.
    menu_zone_threshold: float = 0.55
    # And even then, confirm before starting to talk in the person's ear -
    # a false trigger here interrupts whatever they were actually doing.
    menu_confirm: bool = True
    # After a menu ends or is declined, ignore that direction this long so a
    # gaze still parked there does not immediately re-open it.
    menu_cooldown_s: float = 6.0
    # Do not let the menus fire until the eye-sphere model has actually
    # converged. Before that, gaze_normalized is measured against a default
    # centre and a zero radius, so it sits at a constant offset - which reads
    # as "looking hard right" forever and opens the pain menu on a loop.
    menu_require_model: bool = True
    model_ready_rays: int = 25

    # Audio cues so the wearer can follow the flow without a screen.
    sound_cues: bool = True

    # Questions go to the earpiece, the final sentence to the loudspeaker.
    # Empty = use tts_audio_device for both (fine with a single speaker).
    tts_question_device: str = ""

    conversation_log_size: int = 400
    gaze_log_interval_s: float = 0.5

    # ---- text-to-speech ---------------------------------------------------
    tts_enabled: bool = False
    # espeak-ng voice code - "ro" for Romanian, "en" for English, etc.
    tts_voice: str = "ro"
    tts_rate: int = 165
    # "" = auto-detect the first USB audio card (see glaze/speech.py). Set
    # explicitly (e.g. "plughw:CARD=1,DEV=0") if auto-detect picks wrong when
    # more than one USB audio device is plugged in - tools/list_audio.py
    # shows what is available.
    tts_audio_device: str = ""

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
    parser.add_argument("--reset-settings", action="store_true",
                        help="discard settings saved from the web UI (runtime.json)")
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
    "watchdog_timeout",
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

    if args.reset_settings and os.path.exists(cfg.runtime_config_path):
        os.remove(cfg.runtime_config_path)
        print("Removed saved settings: " + os.path.abspath(cfg.runtime_config_path))

    if args.save_config:
        cfg.save(args.save_config)
        print("Config written to " + os.path.abspath(args.save_config))
        raise SystemExit(0)

    return cfg


# Settings the web UI may persist. Anything not listed here (ports, paths,
# camera capture sizes) stays under the command line's control, so a saved
# value can never make the service impossible to start.
PERSISTABLE = {
    # Which physical camera plays which role. Picked in the dashboard, because
    # nothing in a USB node says whether it is pointed at an eye or a room.
    "eye_source", "scene_source",
    "camera_swapped", "flip_eye_vertical", "flip_eye_horizontal",
    "flip_scene_vertical", "flip_scene_horizontal", "rotate_eye_degrees",
    "draw_overlay", "roi_mode", "write_gaze_file", "jpeg_quality",
    "stream_fps", "scene_stream_fps", "max_tracking_fps",
    "threshold_strict", "threshold_medium", "threshold_relaxed",
    "pupil_confidence_threshold", "pupil_confidence_threshold_sphere",
    "min_model_centers", "max_rays", "intersection_ray_count",
    "minimum_intersection_angle_degrees", "max_stored_intersections",
    "model_center_average_window", "pupil_min_confidence",
    "pupil_min_circularity", "pupil_max_area_fraction",
    "pupil_max_jump_fraction", "pupil_max_eye_radius_fraction",
    "lash_open_iterations", "gaze_zone_enter", "gaze_zone_exit",
    "blink_min_ms", "blink_max_ms", "blink_window_ms", "blink_warmup_s",
    "vision_model", "vision_enabled", "vision_language",
    "tts_enabled", "tts_voice", "tts_rate", "tts_audio_device",
    "tts_question_device", "conversation_enabled", "capture_dwell_ms",
    "capture_move_fraction", "max_captures", "capture_window_s",
    "answer_dwell_ms", "answer_timeout_ms", "answer_zone_threshold",
    "pillar_confidence_threshold", "max_options_per_pillar",
    "menu_dwell_ms", "menu_zone_threshold", "menu_confirm",
    "menu_cooldown_s", "menu_require_model", "model_ready_rays",
    "sound_cues", "gaze_log_interval_s", "show_guide",
    "long_close_ms", "long_close_max_ms", "long_close_cooldown_s",
    "confirm_start", "start_gesture", "capture_mode", "answer_mode",
    "gaze_offset_x", "gaze_offset_y",
}


def load_runtime_settings(cfg):
    """Apply settings previously saved from the web UI. Returns what applied."""
    path = cfg.runtime_config_path
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}

    applied = {}
    for key, value in saved.items():
        if key in PERSISTABLE and hasattr(cfg, key):
            setattr(cfg, key, value)
            applied[key] = value
    return applied


def save_runtime_settings(cfg, values):
    """Merge ``values`` into the saved settings file."""
    path = cfg.runtime_config_path
    saved = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                saved = loaded
        except (OSError, ValueError):
            saved = {}

    saved.update({k: v for k, v in values.items() if k in PERSISTABLE})
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(saved, handle, indent=2, sort_keys=True)
    except OSError:
        return False
    return True
