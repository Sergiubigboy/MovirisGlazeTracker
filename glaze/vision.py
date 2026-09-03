"""Ask a vision model what the user is looking at.

Takes the current scene frame, marks the calibrated gaze point with a circle
whose radius is the calibration's own error, and sends it to the Gemini API
asking which object is inside the circle. The answer comes back as JSON with a
probability per candidate object, so downstream code can decide how confident
to be rather than trusting a single label.

Only the standard library is used for the HTTP call - the Pi build deliberately
avoids pulling in `requests`.

The API key is read from the GEMINI_API_KEY environment variable (or a
`gemini_key.txt` file next to the code, which .gitignore excludes). It is never
written to the config file that the web UI can read back.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import cv2

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "%s:generateContent")

PROMPT = """This image is the forward-facing view of a person wearing an eye tracker.
The yellow circle marks where they are looking; its radius is the tracker's
uncertainty, so the intended object is inside or very near the circle.

Identify which physical object the person is most likely looking at.
Respond with JSON only, in this exact shape:
{"objects": [{"name": "<short object name>", "probability": <0..1>}], "scene": "<5 word description>"}
List between 1 and 5 candidates, most likely first, probabilities summing to about 1.
Use short everyday names a person would say out loud."""


def load_api_key(explicit=None):
    """Env var first, then a local key file. Returns None when unset."""
    if explicit:
        return explicit.strip()
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(os.path.dirname(here), "gemini_key.txt"),
                      os.path.join(here, "gemini_key.txt")):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    return handle.read().strip()
            except OSError:
                pass
    return None


def annotate_gaze(frame, point, uncertainty=0.12):
    """Draw the gaze circle. ``point`` and ``uncertainty`` are normalised."""
    marked = frame.copy()
    height, width = marked.shape[:2]
    x = int(max(0.0, min(1.0, point[0])) * width)
    y = int(max(0.0, min(1.0, point[1])) * height)
    radius = max(18, int(uncertainty * min(width, height)))

    cv2.circle(marked, (x, y), radius, (0, 0, 0), 5)
    cv2.circle(marked, (x, y), radius, (0, 235, 255), 3)
    cv2.drawMarker(marked, (x, y), (0, 235, 255), cv2.MARKER_CROSS, 14, 2)
    return marked


def ask_gemini(frame, model="gemini-2.5-flash-lite", api_key=None,
               jpeg_quality=80, timeout=20.0):
    """Send one annotated frame. Returns a dict with the parsed answer."""
    key = load_api_key(api_key)
    if not key:
        return {"ok": False, "error": "no API key: set GEMINI_API_KEY or create gemini_key.txt"}

    ok, buffer = cv2.imencode(".jpg", frame,
                              [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
    if not ok:
        return {"ok": False, "error": "could not encode frame"}

    body = {
        "contents": [{
            "parts": [
                {"text": PROMPT},
                {"inline_data": {"mime_type": "image/jpeg",
                                 "data": base64.b64encode(buffer.tobytes()).decode("ascii")}},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.0},
    }

    request = urllib.request.Request(
        (ENDPOINT % model) + "?key=" + urllib.parse.quote(key),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "error": "HTTP %s: %s" % (exc.code, detail)}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": "network: %s" % exc}
    except ValueError as exc:
        return {"ok": False, "error": "bad response: %s" % exc}

    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "error": "unexpected response shape",
                "raw": str(payload)[:300]}

    try:
        answer = json.loads(text)
    except ValueError:
        # The model ignored the JSON instruction; hand back the raw text
        # rather than throwing the whole answer away.
        return {"ok": True, "objects": [], "scene": text.strip()[:200], "raw_text": True}

    objects = answer.get("objects") or []
    normalised = []
    for entry in objects[:5]:
        try:
            normalised.append({"name": str(entry["name"])[:60],
                               "probability": round(float(entry.get("probability", 0)), 3)})
        except (KeyError, TypeError, ValueError):
            continue

    return {"ok": True, "objects": normalised, "scene": str(answer.get("scene", ""))[:200]}


def uncertainty_from_calibration(calibration, floor=0.08):
    """Turn the calibration's RMS error into a circle radius (normalised)."""
    error = getattr(calibration, "rms_error", None)
    if error is None:
        return 0.2  # uncalibrated: a wide circle is honest about that
    return max(floor, min(0.5, float(error) * 1.5))
