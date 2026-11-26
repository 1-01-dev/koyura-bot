"""Environment-driven knobs for the chat handlers.

Read once, at import, exactly like the module constants of the old flat
``handlers.py``: the bot reads its configuration from the environment before it
starts polling and has no reload path. Everything that needs a value at call
time (``DOWNLOADS_PATH`` in tests, the progress throttle) is read through this
module's attributes, so a single monkeypatch reaches every caller.
"""
from __future__ import annotations

import os
import re

# The shared downloads volume: the engine writes, the bot uploads.
DOWNLOADS_PATH = os.environ.get("DOWNLOADS_PATH", "/downloads")

# Anything that looks like a link. Telegram does not autolink bare domains, so
# the scheme is required; quotes and angle brackets end a link in practice.
URL_REGEX = re.compile(r"https?://[^\s<>\"']+")

# A picker session describes one Telegram message, so it is only meaningful for
# as long as the conversation is: ten minutes, then the tap says "expired".
STATE_TTL = 600

# One message must not queue dozens of jobs (and hit Telegram's message rate).
MAX_LINKS_PER_MESSAGE = 5

# Telegram rate-limits edits; one caption update every few seconds is plenty.
PROGRESS_EDIT_INTERVAL = float(os.environ.get("PROGRESS_EDIT_INTERVAL", "3"))
PROGRESS_MIN_PERCENT_DELTA = float(os.environ.get("PROGRESS_MIN_PERCENT_DELTA", "1.5"))

# The status poll: two seconds is honest enough for a progress bar, and 450
# attempts cover the engine's own 900 s download timeout with headroom.
POLL_INTERVAL = 2.0
POLL_ATTEMPTS = 450
