"""Small SQLite-backed store for bot state that must survive a restart.

Only per-user preferences live here today. Download sessions are intentionally
in-memory: they describe an in-flight Telegram message and are not meaningful
after a restart (the user simply sends the link again).

The store is best effort: a read-only or missing volume must never stop the bot
from answering, so every operation falls back to an in-memory cache.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/data/koyura_bot_state.db"
DEFAULTS = {"quality": "best", "format": "video"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id INTEGER PRIMARY KEY,
    quality TEXT,
    format TEXT,
    updated_at TEXT NOT NULL
);
"""

_cache: dict[int, dict[str, str]] = {}
_cache_lock = threading.Lock()
_warned_unavailable = False


def _db_path() -> str:
    return os.environ.get("BOT_STATE_DB_PATH", DEFAULT_DB_PATH)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise sqlite3.OperationalError(f"cannot create state directory {directory}: {exc}") from exc
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init() -> None:
    """Create the schema. Safe to call on every start (idempotent)."""
    try:
        with _connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
    except (sqlite3.Error, OSError) as exc:
        _warn_once(f"Bot state DB unavailable ({exc}); preferences are in-memory only")


def _warn_once(message: str) -> None:
    global _warned_unavailable
    if not _warned_unavailable:
        logger.warning(message)
        _warned_unavailable = True


def get_prefs(user_id: int) -> dict[str, str]:
    """Return the stored preferences for a user, falling back to defaults."""
    with _cache_lock:
        cached = _cache.get(user_id)
    if cached is not None:
        return dict(cached)

    prefs = dict(DEFAULTS)
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT quality, format FROM user_prefs WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is not None:
            if row["quality"]:
                prefs["quality"] = row["quality"]
            if row["format"]:
                prefs["format"] = row["format"]
    except (sqlite3.Error, OSError) as exc:
        _warn_once(f"Could not read bot preferences ({exc}); using defaults")

    with _cache_lock:
        _cache[user_id] = prefs
    return dict(prefs)


def set_pref(user_id: int, key: str, value: str) -> None:
    """Persist a single preference value."""
    if key not in DEFAULTS:
        raise ValueError(f"Unknown preference: {key}")

    prefs = get_prefs(user_id)
    prefs[key] = value
    with _cache_lock:
        _cache[user_id] = prefs

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO user_prefs (user_id, quality, format, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    quality=excluded.quality,
                    format=excluded.format,
                    updated_at=excluded.updated_at
                """,
                (user_id, prefs["quality"], prefs["format"], stamp),
            )
            conn.commit()
    except (sqlite3.Error, OSError) as exc:
        _warn_once(f"Could not persist bot preferences ({exc}); kept in memory")


def reset_cache() -> None:
    """Test helper: drop the in-memory cache."""
    with _cache_lock:
        _cache.clear()
