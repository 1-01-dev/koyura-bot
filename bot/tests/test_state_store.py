"""Tests for the SQLite-backed bot preference store."""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import state_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(tmp_path / "bot_state.db"))
    state_store.reset_cache()
    state_store.init()
    yield state_store
    state_store.reset_cache()


def test_defaults_when_nothing_stored(store):
    assert store.get_prefs(1) == {"quality": "best", "format": "video"}


def test_set_pref_persists_across_restart(store):
    store.set_pref(42, "quality", "720")
    store.set_pref(42, "format", "audio")

    # Simulate a restart: drop the in-memory cache and read from SQLite.
    store.reset_cache()
    prefs = store.get_prefs(42)
    assert prefs["quality"] == "720"
    assert prefs["format"] == "audio"


def test_prefs_are_per_user(store):
    store.set_pref(1, "format", "audio")
    assert store.get_prefs(1)["format"] == "audio"
    assert store.get_prefs(2)["format"] == "video"


def test_unknown_preference_is_rejected(store):
    with pytest.raises(ValueError):
        store.set_pref(1, "theme", "dark")


def test_unwritable_state_path_degrades_to_memory(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(blocker / "state.db"))
    state_store.reset_cache()

    with pytest.raises(sqlite3.OperationalError), state_store._connect():
        pass

    # The public API never raises: it keeps working from the cache.
    state_store.init()
    state_store.set_pref(7, "quality", "480")
    assert state_store.get_prefs(7)["quality"] == "480"
    state_store.reset_cache()


# ---------------------------------------------------------------------------
# Regression: a stored row used to break every reader
#
# ``EXTRA_COLUMNS`` grew a third entry (``subscription_passed_at``) while the
# SELECT in ``get_extras`` stayed at two columns, so ``row[name]`` raised
# ``IndexError`` for every user we had a row for. The tests that shipped with
# that change only exercised the ``row is None`` path, which never reaches the
# row lookup, so the suite stayed green while the bot answered nothing.
# ---------------------------------------------------------------------------

#: A row with every column the migrated schema has, inserted with a literal
#: statement so the test does not depend on the setters whose SELECT it checks.
FULL_ROW = (7, "720", "audio", "ru", "audio", "2024-01-01 00:00:00", "2024-01-01 00:00:00")


def _insert_full_row(store):
    """Write the row directly, bypassing the setters whose SELECT is under test."""
    # One literal statement, every value bound: the column names never reach the
    # SQL text as data, and the row is written the way an older release left it.
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, quality, format, language, pending_format, subscription_passed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            FULL_ROW,
        )
        conn.commit()
    store.reset_cache()
    return FULL_ROW[0]


def test_reader_on_a_stored_row_does_not_raise(store):
    """The exact production failure: a row in ``user_prefs`` broke every message."""
    user_id = _insert_full_row(store)

    assert store.get_extras(user_id) == {
        "language": "ru",
        "pending_format": "audio",
        "subscription_passed_at": "2024-01-01 00:00:00",
    }
    assert store.get_language(user_id) == "ru"
    assert store.get_pending_format(user_id) == "audio"
    # The stamp is in the future of the epoch, so the pass is stale, not missing.
    assert store.get_subscription_passed(user_id) is False


def test_prefs_reader_on_a_stored_row_does_not_raise(store):
    user_id = _insert_full_row(store)

    assert store.get_prefs(user_id) == {"quality": "720", "format": "audio"}


def test_set_extra_on_a_stored_row_does_not_raise(store):
    user_id = _insert_full_row(store)

    store.set_extra(user_id, "language", "en")
    store.reset_cache()
    assert store.get_language(user_id) == "en"
    # The other extras of the row survive the write.
    assert store.get_pending_format(user_id) == "audio"


def test_set_extra_clears_one_column_and_keeps_the_others(store):
    user_id = _insert_full_row(store)

    store.set_subscription_passed(user_id, False)
    store.reset_cache()
    assert store.get_extras(user_id)["subscription_passed_at"] is None
    assert store.get_language(user_id) == "ru"


def test_a_fresh_pass_is_accepted(store):
    """A stamp made now is inside the TTL, so the gate does not ask again."""
    user_id = _insert_full_row(store)

    store.set_subscription_passed(user_id, True)
    store.reset_cache()

    assert store.get_subscription_passed(user_id) is True


# ---------------------------------------------------------------------------
# Regression: the SELECT must stay in step with the constant it iterates
# ---------------------------------------------------------------------------

def _selected_columns(monkeypatch, store, reader) -> list[tuple[str, ...]]:
    """The column list of each SELECT the store runs, read from the statement it sends.

    A trace callback is used rather than wrapping the connection: the statement is
    then the store's own, and this test never composes SQL itself. ``get_extras``
    reads each column back by name, so a column missing from the SELECT fails the
    runtime tests above with ``IndexError``; this pins the *order* as well.
    """
    statements: list[str] = []
    real_connect = store._connect

    def traced_connect():
        conn = real_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_connect", traced_connect)
    reader(FULL_ROW[0])

    selected: list[tuple[str, ...]] = []
    for statement in statements:
        head, _, tail = statement.partition(" FROM ")
        if not tail or not head.upper().startswith("SELECT"):
            continue
        columns = head[len("SELECT "):]
        selected.append(tuple(part.strip() for part in columns.split(",")))
    return selected


def test_extras_select_returns_exactly_extra_columns(store, monkeypatch):
    """A column added to ``EXTRA_COLUMNS`` must reach the SELECT in the same order."""
    _insert_full_row(store)

    selected = _selected_columns(monkeypatch, store, store.get_extras)

    assert selected == [store.EXTRA_COLUMNS]


def test_prefs_select_returns_exactly_prefs_columns(store, monkeypatch):
    _insert_full_row(store)

    selected = _selected_columns(monkeypatch, store, store.get_prefs)

    assert selected == [store.PREFS_COLUMNS]


def test_extra_column_names_are_the_ones_the_schema_has(store):
    """A name the SELECT lists but no migration creates fails the same way."""
    with store._connect() as conn:
        present = tuple(row["name"] for row in conn.execute("PRAGMA table_info(user_prefs)"))

    assert set(store.EXTRA_COLUMNS) <= set(present)
    assert set(store.PREFS_COLUMNS) <= set(present)
    assert set(store.DEFAULTS) == set(store.PREFS_COLUMNS)
    # A repeated name would make the description shorter than the constant.
    assert len(set(store.EXTRA_COLUMNS)) == len(store.EXTRA_COLUMNS)


def test_set_pref_persists_every_prefs_column(store):
    """The write side must cover the same columns the reader selects.

    ``set_pref`` still spells its INSERT out by hand. That is deliberate: the
    literals keep column names out of the SQL text entirely. The risk is the
    mirror of the bug this file pins — a column added to ``PREFS_COLUMNS`` and
    read back by ``get_prefs`` but never written, so it reverts on restart. This
    writes through the public API only and reads the row back from SQLite.
    """
    for name in store.PREFS_COLUMNS:
        store.set_pref(11, name, "changed-" + name)

    store.reset_cache()
    assert store.get_prefs(11) == {name: "changed-" + name for name in store.PREFS_COLUMNS}
