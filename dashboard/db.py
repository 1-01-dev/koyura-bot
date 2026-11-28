"""SQLite persistence for the Koyura admin panel.

One module owns the whole data layer: the schema, the migrations, and every
query the panel, the API routes and the bot's event stream need. It is async
throughout (aiosqlite) because FastAPI endpoints are, and a blocking sqlite3
call would stall the event loop for every other request.

Design notes that outlive any single query:

* **The schema is a contract, not a convenience.** ``koyura_bot.db`` is created
  by an older release and then reused forever, so every statement here is
  ``IF NOT EXISTS`` and runs on each new connection. A restart migrates, it
  never drops. ``tests/test_db.py`` pins the columns; do not "tidy" the DDL.
* **The path is read per call.** ``DB_PATH`` is resolved on every connection
  rather than at import, which keeps the file relocatable for tests and for a
  deployment that mounts a volume late.
* **Timestamps are UTC strings.** ``%Y-%m-%d %H:%M:%S`` sorts correctly as text,
  matches what the bot writes, and is the format every ``LIKE 'YYYY-MM-DD%'``
  day filter below assumes.
* **User input never reaches SQL text.** Optional filters are applied with
  ``column = ? OR ? IS NULL`` so one statement serves every combination.

WAL mode and a 5 s busy timeout let the bot write while the panel reads.
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = "/data/koyura_bot.db"

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# The whole schema in one script: executescript is atomic from the caller's
# point of view and leaves no half-created table behind on failure. Every
# statement is IF NOT EXISTS, so this doubles as the migration for an existing
# file and is safe to run on every connection.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    user_id INTEGER,
    username TEXT,
    chat_id INTEGER,
    url TEXT NOT NULL,
    title TEXT,
    platform TEXT,
    format TEXT,
    quality TEXT,
    file_size_bytes INTEGER,
    download_duration_sec REAL,
    status TEXT NOT NULL DEFAULT 'started',
    error_message TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS disk_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_bytes INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Admin-editable settings, currently the download limits. Values stay text:
-- a malformed number must be a validation error on read, never a broken row.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The owner's own message bodies. One row per (kind, language, name); the
-- partial unique index below is what makes "one active template per pair" a
-- database rule rather than a convention the routes have to remember.
CREATE TABLE IF NOT EXISTS message_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    lang TEXT NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_downloads_started_at ON downloads(started_at);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_message_templates_pair ON message_templates(kind, lang);
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_templates_active
    ON message_templates(kind, lang) WHERE is_active = 1;

-- Cookie jars per service, in the order the engine tries them. The path is
-- derived from ``service`` and ``stored_name`` (never stored): the file lives
-- at COOKIES_PATH/<service>/<stored_name>, outside the panel's own volume.
-- ``position`` is 0-based and renumbered by the routes on every move/delete,
-- so the engine can trust the order it receives. No column ever holds cookie
-- content: name, size and hash are enough to show and to spot a change.
CREATE TABLE IF NOT EXISTS service_cookies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    original_name TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    checked_at TEXT,
    check_status TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_service_cookies_file
    ON service_cookies(service, stored_name);
CREATE INDEX IF NOT EXISTS idx_service_cookies_order
    ON service_cookies(service, position);
"""


# ---------------------------------------------------------------------------
# Connection plumbing
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Resolve ``DB_PATH`` now (not at import) so callers can redirect it."""
    return os.environ.get("DB_PATH", _DEFAULT_DB_PATH)


def _utc_now() -> str:
    """Current UTC time in the storage format."""
    return datetime.now(timezone.utc).strftime(_TIME_FORMAT)


def _day_start(day: str) -> str:
    """Inclusive lower bound for a ``YYYY-MM-DD`` filter value."""
    return f"{day} 00:00:00"


def _day_end(day: str) -> str:
    """Inclusive upper bound for a ``YYYY-MM-DD`` filter value."""
    return f"{day} 23:59:59"


@asynccontextmanager
async def _conn() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Open one migrated, row-dict connection and close it on exit."""
    path = _db_path()
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"DB_PATH {path!r} is not writable: {exc}") from exc

    async with aiosqlite.connect(path, timeout=10.0) as conn:
        conn.row_factory = aiosqlite.Row
        # journal_mode is persistent per file, the busy timeout is per session:
        # a writer waits up to 5 s for the lock instead of failing at once.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        yield conn


async def _ensure_job_row(conn: aiosqlite.Connection, job_id: str) -> None:
    """Create a placeholder row when an event names a job we never saw start.

    The engine can answer the dashboard faster than the bot reports it (or the
    start event is simply lost), and dropping the late event would hide the
    failure the admin needs to see.
    """
    cursor = await conn.execute(
        "SELECT 1 FROM downloads WHERE job_id = ?", (job_id,)
    )
    known = await cursor.fetchone()
    await cursor.close()
    if known is None:
        cursor = await conn.execute(
            "INSERT INTO downloads (job_id, url, status, started_at) "
            "VALUES (?, '', 'started', ?)",
            (job_id, _utc_now()),
        )
        await cursor.close()


# ---------------------------------------------------------------------------
# Startup / maintenance
# ---------------------------------------------------------------------------

async def init_db(path: str | None = None) -> None:
    """Create or migrate the schema.

    ``path`` is an explicit override for scripts and tests; the environment is
    restored afterwards so a temporary database never leaks into later calls.
    """
    if path is None:
        async with _conn():
            return

    previous = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = path
    try:
        async with _conn():
            pass
    finally:
        if previous is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = previous


async def mark_interrupted_downloads(
    reason: str = "Interrupted by service restart",
) -> int:
    """Fail every row that was still in flight when the panel stopped.

    Job state lives in the engine's memory, so after a restart nothing will
    ever finish these rows, and leaving them "downloading" keeps the panel
    claiming work that no longer exists. Returns the number of rows updated.
    """
    async with _conn() as conn:
        cursor = await conn.execute(
            "UPDATE downloads SET status = 'error', error_message = ?, "
            "completed_at = ? "
            "WHERE status IN ('started', 'downloading') AND completed_at IS NULL",
            (reason, _utc_now()),
        )
        updated = cursor.rowcount
        await cursor.close()
        await conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Download lifecycle (written from the bot's event stream)
# ---------------------------------------------------------------------------

async def insert_download_start(
    *,
    job_id: str,
    user_id: int | None,
    username: str | None,
    chat_id: int | None,
    url: str,
    platform: str | None = None,
    title: str | None = None,
    format: str | None = None,
    quality: str | None = None,
) -> None:
    """Record a job the engine accepted.

    The bot already knows the metadata when it asks for a download, so title,
    format and quality are stored here instead of waiting for a later event.
    Re-delivery is harmless: the unique ``job_id`` wins and the row is kept.
    """
    async with _conn() as conn:
        cursor = await conn.execute(
            "INSERT INTO downloads (job_id, user_id, username, chat_id, url, "
            "platform, title, format, quality, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO NOTHING",
            (job_id, user_id, username, chat_id, url,
             platform, title, format, quality, _utc_now()),
        )
        await cursor.close()
        await conn.commit()


async def update_download_title(*, job_id: str, title: str | None) -> None:
    """Backfill a title discovered after the start event.

    Empty values are ignored: a progress tick without a title must not erase
    the one already stored.
    """
    cleaned = (title or "").strip()
    if not cleaned:
        return
    async with _conn() as conn:
        cursor = await conn.execute(
            "UPDATE downloads SET title = ? WHERE job_id = ?",
            (cleaned, job_id),
        )
        await cursor.close()
        await conn.commit()


async def update_download_done(
    *,
    job_id: str,
    title: str | None = None,
    format: str | None = None,
    quality: str | None = None,
    file_size_bytes: int | None = None,
    download_duration_sec: float | None = None,
) -> None:
    """Close a job successfully and store the numbers the KPIs use."""
    async with _conn() as conn:
        await _ensure_job_row(conn, job_id)
        cursor = await conn.execute(
            "UPDATE downloads SET status = 'done', "
            "title = COALESCE(?, title), "
            "format = COALESCE(?, format), "
            "quality = COALESCE(?, quality), "
            "file_size_bytes = ?, download_duration_sec = ?, completed_at = ? "
            "WHERE job_id = ?",
            (title, format, quality, file_size_bytes, download_duration_sec,
             _utc_now(), job_id),
        )
        await cursor.close()
        await conn.commit()


async def update_download_error(*, job_id: str, error_message: str) -> None:
    """Close a job as failed, keeping the message the user was shown."""
    async with _conn() as conn:
        await _ensure_job_row(conn, job_id)
        cursor = await conn.execute(
            "UPDATE downloads SET status = 'error', error_message = ?, "
            "completed_at = ? WHERE job_id = ?",
            (error_message, _utc_now(), job_id),
        )
        await cursor.close()
        await conn.commit()


async def get_download_by_job_id(job_id: str) -> aiosqlite.Row | None:
    """One download row, or ``None`` when the id is unknown."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM downloads WHERE job_id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
    return row


# ---------------------------------------------------------------------------
# KPI strip
# ---------------------------------------------------------------------------

async def _day_totals(conn: aiosqlite.Connection, day: str) -> tuple[int, int, int]:
    """``(finished, failed, started)`` for one UTC calendar day."""
    prefix = f"{day}%"
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM downloads WHERE status = 'done' AND started_at LIKE ?",
        (prefix,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    finished = int(row[0]) if row else 0

    cursor = await conn.execute(
        "SELECT COUNT(*) FROM downloads WHERE status = 'error' AND started_at LIKE ?",
        (prefix,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    failed = int(row[0]) if row else 0

    cursor = await conn.execute(
        "SELECT COUNT(*) FROM downloads WHERE started_at LIKE ?",
        (prefix,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    started = int(row[0]) if row else 0

    return finished, failed, started


def _error_rate(failed: int, started: int) -> float:
    """Failures as a percentage of everything attempted, rounded for display."""
    if not started:
        return 0.0
    return round(failed / started * 100, 2)


async def get_dashboard_stats() -> dict[str, Any]:
    """Everything the four KPI cells need, in one call.

    "Today" and "yesterday" are UTC calendar days so the numbers never depend
    on the server's local timezone, matching how ``started_at`` is written.
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    window_start = (now - timedelta(hours=24)).strftime(_TIME_FORMAT)

    async with _conn() as conn:
        done_today, failed_today, started_today = await _day_totals(conn, today)
        done_yesterday, failed_yesterday, started_yesterday = await _day_totals(
            conn, yesterday
        )
        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM downloads WHERE started_at >= ?",
            (window_start,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        active_users = int(row[0]) if row and row[0] is not None else 0

    return {
        "downloads_today": done_today,
        "downloads_yesterday": done_yesterday,
        "active_users_24h": active_users,
        "error_rate": _error_rate(failed_today, started_today),
        "error_rate_yesterday": _error_rate(failed_yesterday, started_yesterday),
        "errors_today": failed_today,
        "total_today": started_today,
    }


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

#: ``(labels, bucket keys, key format, window start)`` for one range switch.
BucketPlan = tuple[list[str], list[str], str, str]


def _hourly_plan(now: datetime) -> BucketPlan:
    """Last 24 hours, one bucket per hour."""
    stamps = [now - timedelta(hours=offset) for offset in range(23, -1, -1)]
    return (
        [stamp.strftime("%H:00") for stamp in stamps],
        [stamp.strftime("%Y-%m-%d %H") for stamp in stamps],
        "%Y-%m-%d %H",
        (now - timedelta(hours=24)).strftime(_TIME_FORMAT),
    )


def _daily_plan(now: datetime, days: int, label_format: str) -> BucketPlan:
    """``days`` whole days ending today, one bucket per day."""
    stamps = [now - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    return (
        [stamp.strftime(label_format) for stamp in stamps],
        [stamp.strftime("%Y-%m-%d") for stamp in stamps],
        "%Y-%m-%d",
        (now - timedelta(days=days)).strftime(_TIME_FORMAT),
    )


def _monthly_plan(now: datetime) -> BucketPlan:
    """Twelve months ending with the current one.

    Stepping back in 30-day strides from the first of the month (rather than in
    calendar months) is what the panel has always drawn; the axis is labelled by
    month, so which day of the previous month a stride lands on does not show.
    """
    first_of_month = now.replace(day=1)
    stamps = [
        first_of_month - timedelta(days=30 * offset) for offset in range(11, -1, -1)
    ]
    return (
        [stamp.strftime("%b %Y") for stamp in stamps],
        [stamp.strftime("%Y-%m") for stamp in stamps],
        "%Y-%m",
        (now - timedelta(days=365)).strftime(_TIME_FORMAT),
    )


def _bucket_plan(range_key: str, now: datetime) -> BucketPlan:
    """Map the range switch to its bucket plan (1Y is the fallback)."""
    if range_key == "1D":
        return _hourly_plan(now)
    if range_key == "7D":
        return _daily_plan(now, 7, "%a %d")
    if range_key == "1M":
        return _daily_plan(now, 30, "%b %d")
    return _monthly_plan(now)


async def get_chart_data(range_key: str) -> dict[str, Any]:
    """Time series plus the two "top five" lists for one range."""
    now = datetime.now(timezone.utc)
    labels, bucket_keys, bucket_format, start = _bucket_plan(range_key, now)

    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT started_at FROM downloads "
            "WHERE started_at >= ? AND status = 'done'",
            (start,),
        )
        finished = await cursor.fetchall()
        await cursor.close()

        # Bucketing happens here rather than in SQL so both statements stay
        # static and the key format lives right next to the labels.
        counts: dict[str, int] = {}
        for row in finished:
            try:
                moment = datetime.strptime(row["started_at"] or "", _TIME_FORMAT)
            except ValueError:
                # A hand-edited or legacy timestamp must not break the chart.
                continue
            key = moment.strftime(bucket_format)
            counts[key] = counts.get(key, 0) + 1

        cursor = await conn.execute(
            "SELECT platform, COUNT(*) FROM downloads "
            "WHERE started_at >= ? AND platform IS NOT NULL "
            "GROUP BY platform ORDER BY COUNT(*) DESC LIMIT 5",
            (start,),
        )
        platform_rows = await cursor.fetchall()
        await cursor.close()

        cursor = await conn.execute(
            "SELECT username, COUNT(*) FROM downloads "
            "WHERE started_at >= ? AND username IS NOT NULL "
            "GROUP BY username ORDER BY COUNT(*) DESC LIMIT 5",
            (start,),
        )
        user_rows = await cursor.fetchall()
        await cursor.close()

    return {
        "labels": labels,
        "values": [counts.get(key, 0) for key in bucket_keys],
        "platforms": [
            {"platform": row[0], "count": row[1]} for row in platform_rows
        ],
        "top_users": [{"username": row[0], "count": row[1]} for row in user_rows],
    }


# ---------------------------------------------------------------------------
# History and error tables
# ---------------------------------------------------------------------------

#: The five optional filters, each as `:name IS NULL OR column = :name`. One
#: named-parameter statement serves every combination, and the caller passes a
#: dict literal, so no value is ever spliced into the SQL text.
_HISTORY_WHERE = """
    (:platform IS NULL OR platform = :platform)
    AND (:status IS NULL OR status = :status)
    AND (:username IS NULL OR username = :username)
    AND (:date_from IS NULL OR started_at >= :date_from)
    AND (:date_to IS NULL OR started_at <= :date_to)
"""


def _window(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """Turn two ``YYYY-MM-DD`` inputs into inclusive timestamp bounds."""
    return (
        _day_start(date_from) if date_from else None,
        _day_end(date_to) if date_to else None,
    )


async def get_downloads_page(
    page: int = 1,
    per_page: int = 20,
    platform: str | None = None,
    status: str | None = None,
    user: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """One page of download history, newest first, with optional filters."""
    current = max(1, page)
    per_page = max(1, per_page)
    offset = (current - 1) * per_page
    lower, upper = _window(date_from, date_to)

    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM downloads
            WHERE (:platform IS NULL OR platform = :platform)
              AND (:status IS NULL OR status = :status)
              AND (:username IS NULL OR username = :username)
              AND (:date_from IS NULL OR started_at >= :date_from)
              AND (:date_to IS NULL OR started_at <= :date_to)
            """,
            {
                "platform": platform,
                "status": status,
                "username": user,
                "date_from": lower,
                "date_to": upper,
            },
        )
        row = await cursor.fetchone()
        await cursor.close()
        total = int(row[0]) if row else 0

        cursor = await conn.execute(
            """
            SELECT * FROM downloads
            WHERE (:platform IS NULL OR platform = :platform)
              AND (:status IS NULL OR status = :status)
              AND (:username IS NULL OR username = :username)
              AND (:date_from IS NULL OR started_at >= :date_from)
              AND (:date_to IS NULL OR started_at <= :date_to)
            ORDER BY started_at DESC
            LIMIT :limit OFFSET :offset
            """,
            {
                "platform": platform,
                "status": status,
                "username": user,
                "date_from": lower,
                "date_to": upper,
                "limit": per_page,
                "offset": offset,
            },
        )
        rows = await cursor.fetchall()
        await cursor.close()

    return {
        "rows": [dict(row) for row in rows],
        "total": total,
        "pages": (total + per_page - 1) // per_page if total else 1,
        "page": current,
    }


async def get_error_downloads(
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Every failed download in the window, newest first, full messages."""
    lower, upper = _window(date_from, date_to)
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM downloads
            WHERE (:platform IS NULL OR platform = :platform)
              AND (:status IS NULL OR status = :status)
              AND (:username IS NULL OR username = :username)
              AND (:date_from IS NULL OR started_at >= :date_from)
              AND (:date_to IS NULL OR started_at <= :date_to)
            ORDER BY started_at DESC
            """,
            {
                "platform": None,
                "status": "error",
                "username": None,
                "date_from": lower,
                "date_to": upper,
            },
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Disk snapshots
# ---------------------------------------------------------------------------

async def insert_disk_snapshot(total_bytes: int, file_count: int) -> None:
    """Record what ``DOWNLOADS_PATH`` held at this moment."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "INSERT INTO disk_snapshots (total_bytes, file_count, snapshot_at) "
            "VALUES (?, ?, ?)",
            (total_bytes, file_count, _utc_now()),
        )
        await cursor.close()
        await conn.commit()


async def get_latest_disk_snapshot() -> dict[str, Any] | None:
    """The newest snapshot, or ``None`` before the first scan."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM disk_snapshots ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Admin settings (download limits)
# ---------------------------------------------------------------------------

async def get_settings(prefix: str = "") -> dict[str, str]:
    """Stored settings as ``{key: value}``, optionally limited to a prefix.

    Values stay strings all the way to the validator, which is the only place
    allowed to decide what a number means.
    """
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ? AND ? != '' "
            "UNION ALL SELECT key, value FROM settings WHERE ? = ''",
            (f"{prefix}%", prefix, prefix),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return {str(row[0]): str(row[1]) for row in rows}


async def get_setting(key: str, default: str | None = None) -> str | None:
    """One setting, or ``default`` when it was never saved."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
    return str(row[0]) if row else default


async def save_settings(values: dict[str, str]) -> None:
    """Upsert a group of settings in one transaction.

    A half-applied set of limits would be worse than an old one, so the whole
    group is written together or not at all.
    """
    if not values:
        return
    updated_at = _utc_now()
    async with _conn() as conn:
        for key, value in values.items():
            cursor = await conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (str(key), str(value), updated_at),
            )
            await cursor.close()
        await conn.commit()


async def delete_settings(prefix: str) -> int:
    """Remove every setting under ``prefix``; returns how many rows went.

    For the keys a retired feature leaves behind (the ``bot_progress.*``
    switches). Scoped to a prefix on purpose — the same table holds the limits
    and the languages — and the prefix is escaped before it becomes a ``LIKE``
    pattern, so a literal prefix never matches more than it names.
    """
    if not prefix:
        return 0
    escaped = (
        prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    async with _conn() as conn:
        cursor = await conn.execute(
            "DELETE FROM settings WHERE key LIKE ? ESCAPE '\\'",
            (f"{escaped}%",),
        )
        removed = int(cursor.rowcount or 0)
        await cursor.close()
        await conn.commit()
    return removed


# ---------------------------------------------------------------------------
# Message templates (admin-editable bodies, one active per kind + language)
# ---------------------------------------------------------------------------

_TEMPLATES_ALL = (
    "SELECT id, kind, lang, name, body, is_active, created_at, updated_at "
    "FROM message_templates ORDER BY is_active DESC, updated_at DESC, id DESC"
)
_TEMPLATES_BY_KIND = (
    "SELECT id, kind, lang, name, body, is_active, created_at, updated_at "
    "FROM message_templates WHERE kind = ? "
    "ORDER BY is_active DESC, updated_at DESC, id DESC"
)
_TEMPLATES_BY_LANG = (
    "SELECT id, kind, lang, name, body, is_active, created_at, updated_at "
    "FROM message_templates WHERE lang = ? "
    "ORDER BY is_active DESC, updated_at DESC, id DESC"
)
_TEMPLATES_BY_PAIR = (
    "SELECT id, kind, lang, name, body, is_active, created_at, updated_at "
    "FROM message_templates WHERE kind = ? AND lang = ? "
    "ORDER BY is_active DESC, updated_at DESC, id DESC"
)
_TEMPLATE_BY_ID = (
    "SELECT id, kind, lang, name, body, is_active, created_at, updated_at "
    "FROM message_templates WHERE id = ?"
)
_TEMPLATES_ACTIVE = (
    "SELECT id, kind, lang, name, body, is_active, created_at, updated_at "
    "FROM message_templates WHERE is_active = 1 ORDER BY kind, lang"
)


def _template_row(row) -> dict[str, Any]:
    return dict(row)


async def list_message_templates(
    *, kind: str | None = None, lang: str | None = None
) -> list[dict[str, Any]]:
    """Stored bodies, newest first, optionally for one kind/language pair.

    Four static statements instead of a composed ``WHERE``: the filters the
    panel offers are exactly these two, and a fixed query keeps the SQL
    literal (a reviewer can see there is nothing to inject).
    """
    async with _conn() as conn:
        if kind is not None and lang is not None:
            cursor = await conn.execute(_TEMPLATES_BY_PAIR, (str(kind), str(lang)))
        elif kind is not None:
            cursor = await conn.execute(_TEMPLATES_BY_KIND, (str(kind),))
        elif lang is not None:
            cursor = await conn.execute(_TEMPLATES_BY_LANG, (str(lang),))
        else:
            cursor = await conn.execute(_TEMPLATES_ALL)
        rows = await cursor.fetchall()
        await cursor.close()
    return [_template_row(row) for row in rows]


async def get_message_template(template_id: int) -> dict[str, Any] | None:
    """One stored body, or ``None``."""
    async with _conn() as conn:
        cursor = await conn.execute(
            _TEMPLATE_BY_ID,
            (int(template_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
    return _template_row(row) if row is not None else None


async def active_message_templates() -> list[dict[str, Any]]:
    """Only the rows the bot should render (``is_active = 1``)."""
    async with _conn() as conn:
        cursor = await conn.execute(
            _TEMPLATES_ACTIVE
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [_template_row(row) for row in rows]


async def _clear_active(
    conn: aiosqlite.Connection, kind: str, lang: str, moment: str
) -> None:
    cursor = await conn.execute(
        "UPDATE message_templates SET is_active = 0, updated_at = ? "
        "WHERE kind = ? AND lang = ? AND is_active = 1",
        (moment, kind, lang),
    )
    await cursor.close()


async def create_message_template(
    *, kind: str, lang: str, name: str, body: str, activate: bool = True
) -> int:
    """Store a body; ``activate`` makes it the one the bot renders.

    The insertion and the activation are one transaction, so a reader never
    sees two active rows for the pair (the unique index would reject that).
    """
    moment = _utc_now()
    async with _conn() as conn:
        if activate:
            await _clear_active(conn, kind, lang, moment)
        cursor = await conn.execute(
            "INSERT INTO message_templates "
            "(kind, lang, name, body, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, lang, name, body, 1 if activate else 0, moment, moment),
        )
        template_id = int(cursor.lastrowid or 0)
        await cursor.close()
        await conn.commit()
    return template_id


async def update_message_template(
    template_id: int, *, name: str, body: str
) -> bool:
    """Rewrite the name and body of one row; ``False`` when it is gone."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "UPDATE message_templates SET name = ?, body = ?, updated_at = ? "
            "WHERE id = ?",
            (name, body, _utc_now(), int(template_id)),
        )
        changed = cursor.rowcount
        await cursor.close()
        await conn.commit()
    return bool(changed)


async def activate_message_template(template_id: int) -> bool:
    """Make one row the active body for its pair; ``False`` when it is gone."""
    moment = _utc_now()
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT kind, lang FROM message_templates WHERE id = ?",
            (int(template_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return False
        await _clear_active(conn, str(row["kind"]), str(row["lang"]), moment)
        cursor = await conn.execute(
            "UPDATE message_templates SET is_active = 1, updated_at = ? WHERE id = ?",
            (moment, int(template_id)),
        )
        await cursor.close()
        await conn.commit()
    return True


async def delete_message_template(template_id: int) -> bool:
    """Delete one row; deleting the active one falls back to the default."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "DELETE FROM message_templates WHERE id = ?", (int(template_id),)
        )
        changed = cursor.rowcount
        await cursor.close()
        await conn.commit()
    return bool(changed)


async def reset_message_templates(kind: str, lang: str) -> int:
    """Delete every stored body for one pair: the bot renders the default again."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "DELETE FROM message_templates WHERE kind = ? AND lang = ?",
            (str(kind), str(lang)),
        )
        deleted = cursor.rowcount
        await cursor.close()
        await conn.commit()
    return int(deleted)


async def clear_message_templates() -> int:
    """Delete every stored body: the whole bot is back on the factory texts.

    This is the ``Default`` style. It deletes rows rather than inserting copies
    of the built-in text, because a stored copy would freeze today's wording: an
    upgraded panel would then keep sending the old sentences while the chat
    forgets what "default" means.
    """
    async with _conn() as conn:
        cursor = await conn.execute("DELETE FROM message_templates")
        deleted = cursor.rowcount
        await cursor.close()
        await conn.commit()
    return int(deleted)


async def replace_message_templates(
    bodies: dict[str, dict[str, str]], names: dict[str, str] | None = None
) -> dict[str, int]:
    """Make ``bodies`` the stored set, one active row per ``(kind, lang)``.

    This is what applying a style does. For every pair it names, the previous
    rows (active or not) are deleted and the new body inserted, so the templates
    list shows exactly one row per message instead of a history of every style
    ever applied. Rows for pairs outside ``bodies`` are left alone: a style that
    could not fill a kind must not silently drop what the owner had there.

    All of it is one transaction, so the chat never renders half of one voice
    and half of the previous one.

    Returns ``{"written": n, "replaced": m}`` where ``replaced`` counts the
    pairs that already had a stored row.
    """
    moment = _utc_now()
    labels = names or {}
    written = 0
    replaced = 0
    async with _conn() as conn:
        for kind, by_language in bodies.items():
            for lang, body in by_language.items():
                if not str(body or "").strip():
                    continue
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM message_templates "
                    "WHERE kind = ? AND lang = ?",
                    (str(kind), str(lang)),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row and int(row[0]):
                    replaced += 1
                await conn.execute(
                    "DELETE FROM message_templates WHERE kind = ? AND lang = ?",
                    (str(kind), str(lang)),
                )
                await conn.execute(
                    "INSERT INTO message_templates "
                    "(kind, lang, name, body, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (
                        str(kind),
                        str(lang),
                        labels.get(kind, "Style"),
                        str(body),
                        moment,
                        moment,
                    ),
                )
                written += 1
        await conn.commit()
    return {"written": written, "replaced": replaced}


# ---------------------------------------------------------------------------
# Service cookies (the Storage page)
# ---------------------------------------------------------------------------
#
# One row per jar. ``position`` is the priority order the engine tries the files
# in, kept dense (0, 1, 2, …) inside a service: every writer here renumbers the
# siblings after an insert, a move or a delete, so no reader has to sort around
# holes. Nothing in this section stores or returns cookie content.

async def list_service_cookies(service: str | None = None) -> list[dict[str, Any]]:
    """Every jar, ordered by service and then by the engine's priority."""
    async with _conn() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM service_cookies
            WHERE (:service IS NULL OR service = :service)
            ORDER BY service ASC, position ASC, id ASC
            """,
            {"service": service},
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


async def get_service_cookie(cookie_id: int) -> dict[str, Any] | None:
    """One jar by id, or ``None`` when it was deleted meanwhile."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM service_cookies WHERE id = ?", (int(cookie_id),)
        )
        row = await cursor.fetchone()
        await cursor.close()
    return dict(row) if row else None


async def services_with_cookies() -> list[str]:
    """Service names that currently hold at least one jar, alphabetical."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT service FROM service_cookies ORDER BY service ASC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [str(row["service"]) for row in rows]


async def _next_cookie_position(conn: aiosqlite.Connection, service: str) -> int:
    """One past the last position of a service (0 for a new one)."""
    cursor = await conn.execute(
        "SELECT MAX(position) FROM service_cookies WHERE service = ?", (service,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    highest = row[0] if row and row[0] is not None else -1
    return int(highest) + 1


async def _renumber_cookies(conn: aiosqlite.Connection, service: str) -> None:
    """Rewrite the positions of one service as 0..n-1 in their current order."""
    cursor = await conn.execute(
        "SELECT id FROM service_cookies WHERE service = ? ORDER BY position ASC, id ASC",
        (service,),
    )
    ids = [int(row["id"]) for row in await cursor.fetchall()]
    await cursor.close()
    for position, cookie_id in enumerate(ids):
        cursor = await conn.execute(
            "UPDATE service_cookies SET position = ? WHERE id = ?",
            (position, cookie_id),
        )
        await cursor.close()


async def add_service_cookie(
    *,
    service: str,
    stored_name: str,
    original_name: str | None,
    size: int,
    sha256: str,
) -> dict[str, Any]:
    """Append one jar to a service (last in the priority order)."""
    async with _conn() as conn:
        position = await _next_cookie_position(conn, service)
        cursor = await conn.execute(
            "INSERT INTO service_cookies "
            "(service, stored_name, original_name, size, sha256, position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (service, stored_name, original_name, int(size), sha256, position, _utc_now()),
        )
        cookie_id = cursor.lastrowid
        await cursor.close()
        await conn.commit()
    return dict(await get_service_cookie(int(cookie_id or 0)) or {})


async def update_service_cookie_file(
    cookie_id: int,
    *,
    stored_name: str,
    original_name: str | None,
    size: int,
    sha256: str,
) -> bool:
    """Record a replaced file: new name, size and hash, same position.

    The check result describes the old bytes, so it is cleared — an "ok" that
    was measured against a jar nobody has any more would be a lie.
    """
    async with _conn() as conn:
        cursor = await conn.execute(
            "UPDATE service_cookies SET stored_name = ?, original_name = ?, "
            "size = ?, sha256 = ?, checked_at = NULL, check_status = NULL "
            "WHERE id = ?",
            (stored_name, original_name, int(size), sha256, int(cookie_id)),
        )
        changed = cursor.rowcount
        await cursor.close()
        await conn.commit()
    return bool(changed)


async def move_service_cookie(cookie_id: int, delta: int) -> bool:
    """Move one jar up (``-1``) or down (``+1``) and renumber its service.

    A move at the edge is a no-op that still answers ``True``: the operator
    pressed a button that exists, so the page must not show an error for it.
    """
    row = await get_service_cookie(cookie_id)
    if row is None:
        return False
    service = str(row["service"])
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT id FROM service_cookies WHERE service = ? ORDER BY position ASC, id ASC",
            (service,),
        )
        order = [int(entry["id"]) for entry in await cursor.fetchall()]
        await cursor.close()
        if int(cookie_id) not in order:
            return False
        index = order.index(int(cookie_id))
        target = index + int(delta)
        if target < 0 or target >= len(order):
            return True
        order.insert(target, order.pop(index))
        for position, entry_id in enumerate(order):
            cursor = await conn.execute(
                "UPDATE service_cookies SET position = ? WHERE id = ?",
                (position, entry_id),
            )
            await cursor.close()
        await conn.commit()
    return True


async def delete_service_cookie(cookie_id: int) -> dict[str, Any] | None:
    """Delete one row and renumber what is left; returns the deleted row."""
    row = await get_service_cookie(cookie_id)
    if row is None:
        return None
    async with _conn() as conn:
        cursor = await conn.execute(
            "DELETE FROM service_cookies WHERE id = ?", (int(cookie_id),)
        )
        await cursor.close()
        await _renumber_cookies(conn, str(row["service"]))
        await conn.commit()
    return row


async def delete_service(service: str) -> list[dict[str, Any]]:
    """Delete every jar of a service; returns the rows (for the disk unlink)."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "SELECT * FROM service_cookies WHERE service = ? ORDER BY position ASC, id ASC",
            (service,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        if rows:
            cursor = await conn.execute(
                "DELETE FROM service_cookies WHERE service = ?", (service,)
            )
            await cursor.close()
            await conn.commit()
    return rows


async def record_cookie_check(cookie_id: int, status: str) -> None:
    """Store the verdict of a ``yt-dlp --simulate`` run (``ok``/``auth``/``error``)."""
    async with _conn() as conn:
        cursor = await conn.execute(
            "UPDATE service_cookies SET checked_at = ?, check_status = ? WHERE id = ?",
            (_utc_now(), str(status), int(cookie_id)),
        )
        await cursor.close()
        await conn.commit()
