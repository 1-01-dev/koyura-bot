"""Admin → Progress fields: the eight switches, their defaults and their storage.

The panel section and the API the bot reads are covered by
``test_admin_progress.py``; this module pins only the model — what the defaults
are, how a submitted form and a stored row turn into a set, and that an
unreadable value never raises.

Environment must be set BEFORE importing db/main, like the other dashboard test
modules.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "progress_fields.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

import db  # noqa: E402
import progress_fields  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(db.init_db())


async def _reset() -> None:
    """Start each test from the defaults, not from the previous test's save."""
    await db.save_settings(progress_fields.ProgressFields.defaults().as_stored())


@pytest.fixture(autouse=True)
def _clean_settings():
    asyncio.run(_reset())
    yield


# ---------------------------------------------------------------------------
# Validation and storage
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_the_defaults_are_the_title_and_the_bar(self):
        assert progress_fields.DEFAULTS == {
            "show_title": True, "show_bar": True, "show_percent": False,
            "show_size": False, "show_speed": False, "show_eta": False,
            "show_elapsed": False, "show_quality": False,
        }

    def test_an_empty_table_is_the_default(self):
        fields = progress_fields.ProgressFields.from_row({})
        assert fields["show_title"] is True
        assert fields["show_bar"] is True
        assert fields.describe() == "title,bar"

    def test_every_key_is_under_the_bot_progress_prefix(self):
        for key in progress_fields.ProgressFields.defaults().as_stored():
            assert key.startswith("bot_progress.")
            assert key.removeprefix("bot_progress.") in progress_fields.FIELD_NAMES


class TestParse:
    def test_ticked_boxes_are_on_and_the_rest_off(self):
        fields, errors = progress_fields.parse(["show_title", "show_speed"])
        assert errors == []
        assert fields is not None
        assert fields["show_title"] is True
        assert fields["show_speed"] is True
        assert fields["show_bar"] is False

    def test_nothing_ticked_is_a_valid_minimal_set(self):
        """The bot falls back to the stage line, so refusing this would be noise."""
        fields, errors = progress_fields.parse([])
        assert errors == []
        assert fields is not None
        assert fields.describe() == "none"

    def test_an_unknown_field_is_refused_with_its_name(self):
        fields, errors = progress_fields.parse(["show_title", "show_pizza"])
        assert fields is None
        assert errors and "show_pizza" in errors[0]

    def test_duplicates_are_harmless(self):
        fields, errors = progress_fields.parse(["show_bar", "show_bar"])
        assert errors == []
        assert fields is not None and fields["show_bar"] is True


class TestStorage:
    def test_values_are_written_as_true_and_false(self):
        row = progress_fields.ProgressFields.defaults().as_stored()
        assert row["bot_progress.show_title"] == "true"
        assert row["bot_progress.show_bar"] == "true"
        assert row["bot_progress.show_speed"] == "false"

    def test_a_written_row_reads_back_the_same(self):
        fields = progress_fields.ProgressFields(values={
            **progress_fields.DEFAULTS, "show_percent": True, "show_title": False,
        })
        assert progress_fields.ProgressFields.from_row(fields.as_stored()) == fields

    def test_garbage_in_a_row_falls_back_to_the_default(self):
        fields = progress_fields.ProgressFields.from_row({
            "bot_progress.show_title": "maybe",
            "bot_progress.show_bar": "0",
        })
        assert fields["show_title"] is True    # unreadable -> its default
        assert fields["show_bar"] is False     # a real false

    def test_the_api_body_is_plain_booleans(self):
        payload = progress_fields.ProgressFields.defaults().as_api()
        assert payload["show_title"] is True
        assert payload["show_speed"] is False
        assert set(payload) == set(progress_fields.FIELD_NAMES)
