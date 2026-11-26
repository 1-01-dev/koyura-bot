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
