"""Glaze - a Raspberry Pi port of the Orlosky 3D eye tracker with a web UI.

Upstream: https://github.com/JEOresearch/EyeTracker (3DTracker), by Jason
Orlosky. The detection and eye-model maths are his; this package adapts them to
run headless on a Raspberry Pi and to publish the result over HTTP.
"""

__version__ = "1.0.0"

from .config import Config, config_from_args  # noqa: F401
