"""Contract tests for the dashboard data layer (``dashboard/db.py``).

Each test gets its own database file: ``DB_PATH`` is redirected before the first
call and the module re-reads it on every connection, so nothing here depends on
test order or on state another test left behind.

Three promises are pinned:

* the **schema** (tables, columns, indexes) — an existing ``koyura_bot.db`` is
  migrated, never recreated, so a change here would break a live install;
* the **public API** the routes and the bot's events call, by name and
  behaviour;
* the **read semantics** the pages rely on: day windows are UTC, filters are
  combined rather than widened, and pagination never overlaps.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Columns every release since the first one has written. Read paths and the
#: bot both depend on the names, so they are asserted rather than assumed.
EXPECTED_COLUMNS = {
    "id",
    "job_id",
    "user_id",
    "username",
    "chat_id",
    "url",
    "title",
    "platform",
    "format",
    "quality",
    "file_size_bytes",
    "download_duration_sec",
    "status",
    "error_message",
    "started_at",
    "completed_at",
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh, migrated database for one test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "koyura_bot.db"))
    asyncio.run(db.init_db())
    return db


def _stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime(TIME_FORMAT)


def _backdate(job_id: str, moment: datetime) -> None:
    """Move a row's ``started_at``; the writer always stamps "now".

    Only the read paths care about the historical timestamp, so a direct UPDATE
    is the shortest honest way to build a fixture that spans several days.
    """
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        connection.execute(
            "UPDATE downloads SET started_at = ? WHERE job_id = ?",
            (_stamp(moment), job_id),
        )


async def _seed(
    job_id: str,
    *,
    platform: str = "youtube",
    status: str = "done",
    user_id: int = 1,
    username: str = "alice",
    title: str = "Test Video",
) -> None:
    """Insert one row in a given final state."""
    await db.insert_download_start(
        job_id=job_id,
        user_id=user_id,
        username=username,
        chat_id=100,
        url=f"https://example.com/{job_id}",
        platform=platform,
        title=title,
        format="mp4",
        quality="720p",
    )
    if status == "done":
        await db.update_download_done(
            job_id=job_id,
            file_size_bytes=1_000_000,
            download_duration_sec=3.5,
        )
    elif status == "error":
        await db.update_download_error(job_id=job_id, error_message="network timeout")


async def _row(job_id: str):
    """One stored row, with the "it exists" assertion every reader needs."""
    row = await db.get_download_by_job_id(job_id)
    assert row is not None, f"{job_id} was not stored"
    return row


def _columns() -> set[str]:
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(downloads)")}


def _indexes() -> set[str]:
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        return {row[1] for row in connection.execute("PRAGMA index_list(downloads)")}


# ---------------------------------------------------------------------------
# Schema: an existing file is migrated in place
# ---------------------------------------------------------------------------

def test_schema_has_the_columns_every_release_writes(store):
    assert _columns() == EXPECTED_COLUMNS


def test_schema_has_the_indexes_the_queries_need(store):
    assert {"idx_downloads_started_at", "idx_downloads_status"} <= _indexes()


def test_configuration_tables_exist(store):
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"downloads", "disk_snapshots", "settings"} <= names


def test_journal_mode_is_wal(store):
    """The bot writes while the panel reads; a rollback journal would block it."""
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_database_from_an_older_release_is_kept(store):
    """Simulate an upgrade: rows written before, schema applied after."""
    asyncio.run(_seed("legacy-1"))
    _backdate("legacy-1", datetime.now(timezone.utc) - timedelta(days=30))

    asyncio.run(db.init_db())
    asyncio.run(db.init_db())

    row = asyncio.run(db.get_download_by_job_id("legacy-1"))
    assert row is not None
    assert row["title"] == "Test Video"
    assert _columns() == EXPECTED_COLUMNS


# ---------------------------------------------------------------------------
# Download lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_download_start_records_the_request(store):
    await db.insert_download_start(
        job_id="start-1",
        user_id=42,
        username="bob",
        chat_id=999,
        url="https://youtube.com/watch?v=abc",
        platform="youtube",
    )

    row = await db.get_download_by_job_id("start-1")
    assert row is not None
    assert row["status"] == "started"
    assert row["user_id"] == 42
    assert row["username"] == "bob"
    assert row["chat_id"] == 999
    assert row["url"] == "https://youtube.com/watch?v=abc"
    assert row["platform"] == "youtube"
    assert row["completed_at"] is None


@pytest.mark.asyncio
async def test_insert_download_start_stores_metadata_known_at_start(store):
    await db.insert_download_start(
        job_id="start-meta",
        user_id=1,
        username="alice",
        chat_id=1,
        url="https://example.com/v",
        platform="tiktok",
        title="City walk",
        format="audio",
        quality="mp3",
    )

    row = await _row("start-meta")
    assert row["title"] == "City walk"
    assert row["format"] == "audio"
    assert row["quality"] == "mp3"


@pytest.mark.asyncio
async def test_insert_download_start_is_idempotent(store):
    for _ in range(2):
        await db.insert_download_start(
            job_id="dupe",
            user_id=1,
            username="alice",
            chat_id=1,
            url="https://example.com/z",
        )

    page = await db.get_downloads_page(per_page=100)
    assert [row["job_id"] for row in page["rows"]].count("dupe") == 1


@pytest.mark.asyncio
async def test_update_download_done_records_the_result(store):
    await _seed("done-1", platform="tiktok")
    await db.update_download_done(
        job_id="done-1",
        title="Cool Clip",
        format="mp4",
        quality="1080p",
        file_size_bytes=5_000_000,
        download_duration_sec=10.2,
    )

    row = await _row("done-1")
    assert row["status"] == "done"
    assert row["title"] == "Cool Clip"
    assert row["format"] == "mp4"
    assert row["quality"] == "1080p"
    assert row["file_size_bytes"] == 5_000_000
    assert abs(row["download_duration_sec"] - 10.2) < 0.01
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_update_download_done_keeps_a_title_it_was_not_given(store):
    await _seed("done-keep", title="Original title")

    await db.update_download_done(job_id="done-keep", file_size_bytes=10)

    row = await _row("done-keep")
    assert row["title"] == "Original title"


@pytest.mark.asyncio
async def test_update_download_error_records_the_message(store):
    await _seed("err-1", status="error")

    row = await _row("err-1")
    assert row["status"] == "error"
    assert row["error_message"] == "network timeout"
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_update_download_title_backfills_and_ignores_blanks(store):
    await _seed("title-1", status="started", title="")
    await db.update_download_title(job_id="title-1", title="  Late metadata  ")
    assert (await _row("title-1"))["title"] == "Late metadata"

    await db.update_download_title(job_id="title-1", title=None)
    assert (await _row("title-1"))["title"] == "Late metadata"


@pytest.mark.asyncio
async def test_event_for_an_unknown_job_creates_the_row(store):
    """A lost start event must not lose the failure that follows it."""
    await db.update_download_error(job_id="unknown", error_message="engine restarted")

    row = await db.get_download_by_job_id("unknown")
    assert row is not None
    assert row["status"] == "error"
    assert row["error_message"] == "engine restarted"


@pytest.mark.asyncio
async def test_unknown_job_id_returns_none(store):
    assert await db.get_download_by_job_id("does-not-exist") is None


# ---------------------------------------------------------------------------
# KPI strip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_stats_shape_on_an_empty_database(store):
    stats = await db.get_dashboard_stats()

    assert set(stats) == {
        "downloads_today",
        "downloads_yesterday",
        "active_users_24h",
        "error_rate",
        "error_rate_yesterday",
        "errors_today",
        "total_today",
    }
    assert stats["downloads_today"] == 0
    assert stats["error_rate"] == 0.0


@pytest.mark.asyncio
async def test_dashboard_stats_separate_today_from_yesterday(store):
    now = datetime.now(timezone.utc)
    await _seed("today-done")
    await _seed("today-err", status="error", user_id=2, username="bob")
    await _seed("yesterday-done", user_id=3, username="carol")
    # 25 h back: yesterday's calendar day, outside the rolling 24 h window.
    _backdate("yesterday-done", now - timedelta(hours=25))
    await _seed("ancient-done", user_id=4, username="dave")
    _backdate("ancient-done", now - timedelta(days=10))

    stats = await db.get_dashboard_stats()

    assert stats["downloads_today"] == 1
    assert stats["total_today"] == 2
    assert stats["errors_today"] == 1
    assert stats["downloads_yesterday"] == 1
    # One of two attempts failed today.
    assert stats["error_rate"] == 50.0
    # dave is far outside the rolling 24 h window.
    assert stats["active_users_24h"] == 2


@pytest.mark.asyncio
async def test_dashboard_stats_ignore_a_row_without_a_user(store):
    await db.insert_download_start(
        job_id="anon", user_id=None, username=None, chat_id=None,
        url="https://example.com/anon",
    )
    await db.update_download_done(job_id="anon")

    stats = await db.get_dashboard_stats()
    assert stats["downloads_today"] == 1
    assert stats["active_users_24h"] == 0


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("range_key, buckets", [("1D", 24), ("7D", 7), ("1M", 30), ("1Y", 12)])
async def test_chart_ranges_have_one_bucket_per_label(store, range_key, buckets):
    data = await db.get_chart_data(range_key)

    assert set(data) == {"labels", "values", "platforms", "top_users"}
    assert len(data["labels"]) == buckets
    assert len(data["values"]) == buckets
    assert all(isinstance(value, int) for value in data["values"])


@pytest.mark.asyncio
async def test_chart_counts_only_finished_downloads(store):
    await _seed("chart-done")
    await _seed("chart-err", status="error")

    data = await db.get_chart_data("1D")

    assert sum(data["values"]) == 1


@pytest.mark.asyncio
async def test_chart_buckets_a_row_by_its_own_day(store):
    """A download from two days ago must not land in today's bucket."""
    await _seed("chart-old")
    _backdate("chart-old", datetime.now(timezone.utc) - timedelta(days=2))

    weekly = await db.get_chart_data("7D")
    daily = await db.get_chart_data("1D")

    assert sum(weekly["values"]) == 1
    assert sum(daily["values"]) == 0


@pytest.mark.asyncio
async def test_chart_top_lists_are_capped_at_five(store):
    for index in range(7):
        await _seed(f"top-{index}", platform=f"site{index}", username=f"user{index}")

    data = await db.get_chart_data("7D")

    assert len(data["platforms"]) == 5
    assert len(data["top_users"]) == 5
    assert set(data["platforms"][0]) == {"platform", "count"}
    assert set(data["top_users"][0]) == {"username", "count"}
    # Highest count first, so the busiest source is the one shown.
    assert data["platforms"][0]["count"] >= data["platforms"][-1]["count"]


@pytest.mark.asyncio
async def test_chart_ignores_rows_without_a_platform_or_user(store):
    await db.insert_download_start(
        job_id="bare", user_id=None, username=None, chat_id=None,
        url="https://example.com/bare",
    )
    await db.update_download_done(job_id="bare")

    data = await db.get_chart_data("7D")
    assert data["platforms"] == []
    assert data["top_users"] == []


# ---------------------------------------------------------------------------
# History: filters, day window, pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_page_returns_the_documented_shape(store):
    page = await db.get_downloads_page(page=1, per_page=10)

    assert set(page) == {"rows", "total", "pages", "page"}
    assert page["page"] == 1
    assert page["rows"] == []
    # An empty table still has one (empty) page.
    assert page["pages"] == 1


@pytest.mark.asyncio
async def test_history_pages_do_not_overlap(store):
    for index in range(5):
        await _seed(f"page-{index}", user_id=10 + index, username=f"user{index}")

    first = await db.get_downloads_page(page=1, per_page=3)
    second = await db.get_downloads_page(page=2, per_page=3)

    assert len(first["rows"]) == 3
    assert len(second["rows"]) == 2
    assert first["total"] == 5
    assert first["pages"] == 2
    assert {row["id"] for row in first["rows"]}.isdisjoint(
        {row["id"] for row in second["rows"]}
    )


@pytest.mark.asyncio
async def test_history_page_numbers_below_one_are_clamped(store):
    await _seed("clamp-1")

    page = await db.get_downloads_page(page=0)
    assert page["page"] == 1
    assert len(page["rows"]) == 1


@pytest.mark.asyncio
async def test_history_page_beyond_the_end_is_empty_but_total_holds(store):
    await _seed("beyond-1")

    page = await db.get_downloads_page(page=9, per_page=5)
    assert page["rows"] == []
    assert page["total"] == 1
    assert page["page"] == 9


@pytest.mark.asyncio
async def test_history_orders_newest_first(store):
    await _seed("old")
    _backdate("old", datetime.now(timezone.utc) - timedelta(days=2))
    await _seed("new")

    page = await db.get_downloads_page()
    assert [row["job_id"] for row in page["rows"]] == ["new", "old"]


@pytest.mark.asyncio
async def test_history_filters_combine(store):
    await _seed("tw-done", platform="twitter", username="twitteruser", user_id=999)
    await _seed("tw-err", platform="twitter", status="error", username="other", user_id=998)
    await _seed("yt-done", platform="youtube")

    by_platform = await db.get_downloads_page(per_page=50, platform="twitter")
    assert {row["job_id"] for row in by_platform["rows"]} == {"tw-done", "tw-err"}

    by_status = await db.get_downloads_page(per_page=50, status="error")
    assert [row["job_id"] for row in by_status["rows"]] == ["tw-err"]

    by_user = await db.get_downloads_page(per_page=50, user="twitteruser")
    assert [row["job_id"] for row in by_user["rows"]] == ["tw-done"]

    both = await db.get_downloads_page(per_page=50, platform="twitter", status="done")
    assert [row["job_id"] for row in both["rows"]] == ["tw-done"]


@pytest.mark.asyncio
async def test_history_filter_values_look_like_sql_but_are_not(store):
    await _seed("inject", platform="tiktok", username="bob")

    injected = await db.get_downloads_page(platform="tiktok' OR '1'='1")
    assert injected["rows"] == []
    assert injected["total"] == 0


@pytest.mark.asyncio
async def test_history_day_window_covers_the_whole_day(store):
    day = "2024-03-05"
    await _seed("in-window")
    _backdate("in-window", datetime(2024, 3, 5, 23, 30, tzinfo=timezone.utc))
    await _seed("after-window")
    _backdate("after-window", datetime(2024, 3, 6, 0, 30, tzinfo=timezone.utc))

    page = await db.get_downloads_page(date_from=day, date_to=day)

    assert [row["job_id"] for row in page["rows"]] == ["in-window"]


@pytest.mark.asyncio
async def test_error_list_contains_only_failures(store):
    await _seed("err-a", status="error")
    await _seed("err-b", status="error")
    await _seed("ok-c", status="done")

    errors = await db.get_error_downloads()

    assert {row["job_id"] for row in errors} == {"err-a", "err-b"}
    assert all(row["status"] == "error" for row in errors)


@pytest.mark.asyncio
async def test_error_list_respects_the_date_window(store):
    await _seed("err-today", status="error")
    await _seed("err-old", status="error")
    _backdate("err-old", datetime.now(timezone.utc) - timedelta(days=30))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    errors = await db.get_error_downloads(date_from=today, date_to=today)

    assert [row["job_id"] for row in errors] == ["err-today"]


# ---------------------------------------------------------------------------
# Disk snapshots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disk_snapshot_round_trip(store):
    await db.insert_disk_snapshot(total_bytes=1_000_000_000, file_count=42)

    snapshot = await db.get_latest_disk_snapshot()
    assert snapshot is not None
    assert snapshot["total_bytes"] == 1_000_000_000
    assert snapshot["file_count"] == 42
    assert snapshot["snapshot_at"] is not None


@pytest.mark.asyncio
async def test_latest_disk_snapshot_is_the_newest(store):
    for size, count in ((100, 1), (200, 2), (300, 3)):
        await db.insert_disk_snapshot(total_bytes=size, file_count=count)

    snapshot = await db.get_latest_disk_snapshot()
    assert snapshot is not None
    assert snapshot["total_bytes"] == 300
    assert snapshot["file_count"] == 3


@pytest.mark.asyncio
async def test_latest_disk_snapshot_is_none_before_the_first_scan(store):
    assert await db.get_latest_disk_snapshot() is None


# ---------------------------------------------------------------------------
# Startup maintenance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_fails_every_in_flight_row(store):
    await _seed("interrupted", status="started")
    await _seed("downloading", status="started")

    updated = await db.mark_interrupted_downloads()
    assert updated == 2

    row = await _row("interrupted")
    assert row["status"] == "error"
    assert "restart" in row["error_message"].lower()
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_restart_leaves_finished_rows_alone(store):
    await _seed("finished", status="done")
    await _seed("failed", status="error")

    assert await db.mark_interrupted_downloads() == 0

    assert (await _row("finished"))["status"] == "done"
    assert (await _row("failed"))["status"] == "error"


@pytest.mark.asyncio
async def test_restart_is_a_no_op_the_second_time(store):
    await _seed("only-once", status="started")

    assert await db.mark_interrupted_downloads() == 1
    assert await db.mark_interrupted_downloads() == 0


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settings_round_trip(store):
    await db.save_settings({"download_limits.max_concurrent": "3"})

    assert await db.get_settings() == {"download_limits.max_concurrent": "3"}
    assert await db.get_setting("download_limits.max_concurrent") == "3"


@pytest.mark.asyncio
async def test_missing_setting_falls_back_to_the_default(store):
    assert await db.get_setting("nope", "fallback") == "fallback"
    assert await db.get_setting("nope") is None


@pytest.mark.asyncio
async def test_settings_are_upserted_not_duplicated(store):
    await db.save_settings({"download_limits.estimate_mbps": "10"})
    await db.save_settings({"download_limits.estimate_mbps": "20"})

    assert await db.get_settings() == {"download_limits.estimate_mbps": "20"}


@pytest.mark.asyncio
async def test_settings_prefix_keeps_other_keys_out(store):
    await db.save_settings({
        "download_limits.max_concurrent": "3",
        "unrelated.key": "kept",
    })

    prefixed = await db.get_settings(prefix="download_limits.")
    assert prefixed == {"download_limits.max_concurrent": "3"}
    assert "unrelated.key" in await db.get_settings()


@pytest.mark.asyncio
async def test_saving_nothing_changes_nothing(store):
    await db.save_settings({})
    assert await db.get_settings() == {}
