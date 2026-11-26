"""In-process counters behind ``/stats``.

Deliberately not persisted and deliberately not per-user: these are "how is
this process doing" numbers, and the dashboard is the durable record. A restart
resets them, which is what the uptime line already implies.
"""
from __future__ import annotations

import time

COUNTERS = {"downloads": 0, "errors": 0, "started": time.time()}


def record_download() -> None:
    """A file reached the user."""
    COUNTERS["downloads"] += 1


def record_error() -> None:
    """A job failed somewhere between the button and the file."""
    COUNTERS["errors"] += 1


def uptime_seconds(now: float | None = None) -> int:
    moment = time.time() if now is None else now
    return max(0, int(moment - COUNTERS["started"]))
