"""Benchmark the tracker on a video file (no cameras needed).

Run it on the Pi to pick a processing resolution:

    python3 tools/bench.py upstream/eye_test.mp4 --frames 200

It reports, per configuration, the average frame time, the resulting FPS, the
share of frames where a pupil was found, and the mean confidence, so you can
see what a lower resolution actually costs in detection quality.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glaze.config import Config  # noqa: E402
from glaze.tracker_core import EyeTracker  # noqa: E402


def load_frames(path, count, flip_vertical):
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise SystemExit("could not open " + path)
    frames = []
    while len(frames) < count:
        ok, frame = capture.read()
        if not ok:
            break
        if flip_vertical:
            frame = cv2.flip(frame, 0)
        frames.append(frame)
    capture.release()
    if not frames:
        raise SystemExit("no frames read from " + path)
    return frames


def run(frames, width, height, roi_mode, draw):
    cfg = Config(proc_width=width, proc_height=height, roi_mode=roi_mode)
    tracker = EyeTracker(cfg)

    times = []
    detected = 0
    confidences = []

    for frame in frames:
        started = time.perf_counter()
        result, _ = tracker.process_frame(frame, draw=draw)
        times.append((time.perf_counter() - started) * 1000.0)
        if result.ok:
            detected += 1
            confidences.append(result.confidence)

    return {
        "label": "%dx%d %s%s" % (width, height, "roi" if roi_mode else "full",
                                 "+draw" if draw else ""),
        "mean_ms": statistics.mean(times),
        "median_ms": statistics.median(times),
        "p95_ms": sorted(times)[int(len(times) * 0.95) - 1],
        "fps": 1000.0 / statistics.mean(times),
        "detect_rate": detected / len(frames),
        "confidence": statistics.mean(confidences) if confidences else 0.0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video")
    parser.add_argument("--frames", type=int, default=150)
    parser.add_argument("--flip", action="store_true", help="flip vertically first")
    parser.add_argument("--draw", action="store_true", help="include the overlay cost")
    parser.add_argument("--quick", action="store_true", help="only the default config")
    args = parser.parse_args()

    frames = load_frames(args.video, args.frames, args.flip)
    print("%d frames of %dx%d\n" % (len(frames), frames[0].shape[1], frames[0].shape[0]))

    # (proc width, proc height, roi mode, capture already at proc size)
    configurations = [(320, 240, True, False)]
    if not args.quick:
        configurations = [
            (320, 240, True, True),    # what --preset lite does
            (320, 240, True, False),   # what --preset balanced does
            (320, 240, False, False),  # upstream's full-frame masking
            (640, 480, True, False),   # --preset quality
            (640, 480, False, False),
        ]

    header = "%-30s %9s %9s %9s %8s %9s %7s" % (
        "config", "mean ms", "median", "p95", "fps", "detected", "conf")
    print(header)
    print("-" * len(header))

    for width, height, roi_mode, native in configurations:
        # "native" simulates a camera that already outputs the processing
        # resolution, i.e. no rescale in the hot path.
        source = ([cv2.resize(f, (width, height), interpolation=cv2.INTER_AREA)
                   for f in frames] if native else frames)
        stats = run(source, width, height, roi_mode, args.draw)
        label = stats["label"] + (" native" if native else "")
        print("%-30s %9.2f %9.2f %9.2f %8.1f %8.0f%% %6.0f%%" % (
            label, stats["mean_ms"], stats["median_ms"], stats["p95_ms"],
            stats["fps"], stats["detect_rate"] * 100, stats["confidence"] * 100))


if __name__ == "__main__":
    main()
