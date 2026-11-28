"""Small SQLite-backed store for bot state that must survive a restart.

Per-user preferences live here: the format/quality defaults, the chosen
*language* and the MP3/MP4 *mode* armed from the welcome keyboard. Download
sessions are intentionally in-memory: they describe an in-flight Telegram
message and are not meaningful after a restart (the user simply sends the link
again).

Two shapes are kept apart on purpose:

* ``get_prefs``/``set_pref`` — the original ``quality``/``format`` pair. Their
  return shape is a contract (``/settings`` and the download flow read it), so
  the newer fields do **not** leak into that dict.
* ``get_language``/``set_language`` and ``get_pending_format``/
  ``set_pending_format`` — the fields added with the two-language release. They
  live in the same row and are created by an additive, replay-safe migration.

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

#: Preferences that are *not* part of ``DEFAULTS``; ``None`` means "not set".
EXTRA_COLUMNS = ("language", "pending_format", "subscription_passed_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id INTEGER PRIMARY KEY,
    quality TEXT,
    format TEXT,
    language TEXT,
    pending_format TEXT,
    updated_at TEXT NOT NULL
);
"""

# Columns added after the first release. Each ``ALTER TABLE ... ADD COLUMN`` is
# issued only when the column is missing, so replaying them on every start is
# safe; the SQL is a literal in ``_ensure_columns`` rather than a formatted
# string, because a migration is exactly the place an injection would hide.

_cache: dict[int, dict[str, str]] = {}
_extras: dict[int, dict[str, str | None]] = {}
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


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add the post-release columns to an existing table (idempotent)."""
    present = {row["name"] for row in conn.execute("PRAGMA table_info(user_prefs)")}
    if "language" not in present:
        conn.execute("ALTER TABLE user_prefs ADD COLUMN language TEXT")
    if "pending_format" not in present:
        conn.execute("ALTER TABLE user_prefs ADD COLUMN pending_format TEXT")
    if "subscription_passed_at" not in present:
        conn.execute("ALTER TABLE user_prefs ADD COLUMN subscription_passed_at TEXT")


def init() -> None:
    """Create (and migrate) the schema. Safe to call on every start."""
    try:
        with _connect() as conn:
            conn.executescript(_SCHEMA)
            _ensure_columns(conn)
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

    stamp = _stamp()
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


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Language and the MP3/MP4 mode
# ---------------------------------------------------------------------------

def get_extras(user_id: int) -> dict[str, str | None]:
    """``{language, pending_format}`` for one user; ``None`` means "not set"."""
    with _cache_lock:
        cached = _extras.get(user_id)
    if cached is not None:
        return dict(cached)

    extras: dict[str, str | None] = dict.fromkeys(EXTRA_COLUMNS)
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT language, pending_format FROM user_prefs WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row is not None:
            for name in EXTRA_COLUMNS:
                value = row[name]
                extras[name] = str(value) if value else None
    except (sqlite3.Error, OSError) as exc:
        _warn_once(f"Could not read bot preferences ({exc}); using defaults")

    with _cache_lock:
        _extras[user_id] = extras
    return dict(extras)


def set_extra(user_id: int, name: str, value: str | None) -> None:
    """Persist ``language`` or ``pending_format`` (``None`` clears the column)."""
    if name not in EXTRA_COLUMNS:
        raise ValueError(f"Unknown preference: {name}")

    extras = get_extras(user_id)
    extras[name] = value
    with _cache_lock:
        _extras[user_id] = extras

    stamp = _stamp()
    try:
        with _connect() as conn:
            # One literal statement per allowed column: the column name never
            # reaches SQL as text, so this cannot become an injection point.
            if name == "language":
                conn.execute(
                    "INSERT INTO user_prefs (user_id, language, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "language=excluded.language, updated_at=excluded.updated_at",
                    (user_id, value, stamp),
                )
            elif name == "subscription_passed_at":
                conn.execute(
                    "INSERT INTO user_prefs (user_id, subscription_passed_at, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "subscription_passed_at=excluded.subscription_passed_at, "
                    "updated_at=excluded.updated_at",
                    (user_id, value, stamp),
                )
            else:
                conn.execute(
                    "INSERT INTO user_prefs (user_id, pending_format, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET "
                    "pending_format=excluded.pending_format, "
                    "updated_at=excluded.updated_at",
                    (user_id, value, stamp),
                )
            conn.commit()
    except (sqlite3.Error, OSError) as exc:
        _warn_once(f"Could not persist bot preferences ({exc}); kept in memory")


def get_language(user_id: int) -> str | None:
    """The language this user picked, or ``None`` when they never picked one."""
    return get_extras(user_id)["language"]


def set_language(user_id: int, language: str) -> None:
    """Remember the language of one chat so the picker is not shown again."""
    set_extra(user_id, "language", language)


def get_pending_format(user_id: int) -> str | None:
    """``audio``/``video`` when MP3/MP4 was tapped, else ``None``."""
    return get_extras(user_id)["pending_format"]


def set_pending_format(user_id: int, fmt: str | None) -> None:
    """Arm (``audio``/``video``) or clear (``None``) the format of the next link."""
    set_extra(user_id, "pending_format", fmt)


#: How long a passed subscription gate is trusted before the next link asks
#: again. A day matches the background re-check, so a user who left a channel
#: stops being treated as subscribed when the sweep notices, not never.
SUBSCRIPTION_PASS_TTL_SECONDS = 24 * 60 * 60


def get_subscription_passed(user_id: int | None) -> bool:
    """Whether this user passed the gate within the last day.

    The gate is checked when the user *passes it*, not before every link: asking
    Telegram on each message would be slow and rate-limited. A timestamp older
    than a day (or one we cannot read) answers ``False``, so the gate is asked
    again — a lapsed subscription must not last forever on the strength of one
    check.
    """
    if user_id is None:
        return False
    stamp = get_extras(int(user_id))["subscription_passed_at"]
    if not stamp:
        return False
    try:
        passed_at = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - passed_at).total_seconds()
    return 0 <= elapsed < SUBSCRIPTION_PASS_TTL_SECONDS


def set_subscription_passed(user_id: int | None, passed: bool) -> None:
    """Remember (or forget) that this user passed the subscription gate."""
    if user_id is None:
        return
    if passed:
        set_extra(int(user_id), "subscription_passed_at", _stamp())
    else:
        set_extra(int(user_id), "subscription_passed_at", None)


def reset_cache() -> None:
    """Test helper: drop the in-memory cache."""
    with _cache_lock:
        _cache.clear()
        _extras.clear()
