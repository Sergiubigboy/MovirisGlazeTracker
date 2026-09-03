"""A small in-memory event log, readable from the web UI.

Everything the system decides goes through here: gaze samples, dwell
captures, what was sent to the model, what came back, every question asked and
every yes/no answer. When a conversation produces the wrong sentence, this is
the only way to see *where* it went wrong - was the pupil lost, did the model
guess badly, or was an answer misread as no?

Entries are numbered so the page can poll for "everything after N" instead of
re-fetching the whole buffer.
"""

from __future__ import annotations

import threading
import time

CATEGORIES = ("gaze", "capture", "ai", "pillar", "question", "answer",
              "speech", "state", "error")


class EventLog:
    def __init__(self, size=400):
        self.size = size
        self._lock = threading.Lock()
        self._entries = []
        self._next_id = 1

    def add(self, category, message, data=None):
        entry = {
            "id": 0,
            "at": time.time(),
            "category": category,
            "message": str(message)[:400],
            "data": data,
        }
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._entries.append(entry)
            if len(self._entries) > self.size:
                del self._entries[:len(self._entries) - self.size]
        return entry["id"]

    def since(self, last_id=0, limit=200, categories=None):
        """Entries newer than ``last_id``, oldest first."""
        with self._lock:
            entries = [e for e in self._entries if e["id"] > last_id]
        if categories:
            wanted = set(categories)
            entries = [e for e in entries if e["category"] in wanted]
        return entries[-limit:]

    def clear(self):
        with self._lock:
            self._entries = []
