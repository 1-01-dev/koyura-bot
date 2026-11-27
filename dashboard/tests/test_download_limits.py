"""Tests for the Admin → Download limits section.

Covers the three layers the feature has:

* validation — the same bounds the engine enforces, with readable messages;
* storage — the dashboard's ``settings`` table, values as strings, one row per
  field, and the environment defaults as the starting point;
* application — the panel calls the engine's ``POST /api/settings`` after a
  save and pushes the stored values again on startup.

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "limits.db")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import download_limits  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csrf(c: TestClient | None = None) -> str:
    return (c or client).cookies.get("koyura_csrf") or ""


def _login(c: TestClient | None = None) -> TestClient:
    target = c or client
    page = target.get("/login")
    assert page.status_code == 200, page.text
    resp = target.post(
        "/login",
        data={
            "username": "admin",
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": _csrf(target),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return target


def _form(**overrides) -> dict[str, str]:
    """A valid form payload, with optional field overrides."""
    values = {
        "max_concurrent": "2",
        "per_user_active": "1",
        "per_user_queued": "1",
        "max_queue_length": "6",
        "max_wait_seconds": "120",
        "estimate_mbps": "2",
    }
    values.update({key: str(value) for key, value in overrides.items()})
    return values


def _post_form(payload: dict[str, str], c: TestClient | None = None):
    target = c or client
    data = dict(payload)
    data["csrf_token"] = _csrf(target)
    return target.post("/admin/limits", data=data, follow_redirects=False)


@pytest.fixture(autouse=True)
def _clean_settings():
    """Each test starts without saved limits and without touching the engine."""
    async def _clear():
        async with db._conn() as conn:  # noqa: SLF001 - test-only cleanup
            await conn.execute("DELETE FROM settings")
            await conn.commit()

    asyncio.run(_clear())
    with patch.object(download_limits, "apply_to_engine", return_value=(True, "applied")) as applied:
        yield applied


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_values_round_trip(self):
        limits, errors = download_limits.Limits.parse(_form())
        assert not errors
        assert limits.max_concurrent == 2
        assert limits.max_wait_seconds == 120
        assert limits.estimate_mbps == 2.0

    @pytest.mark.parametrize(
        "field,value,message",
        [
            ("max_concurrent", "0", "between 1 and 8"),
            ("max_concurrent", "9", "between 1 and 8"),
            ("max_queue_length", "51", "between 0 and 50"),
            ("max_wait_seconds", "10", "between 15 and 900"),
            ("estimate_mbps", "0.1", "between 0.2 and 50"),
            ("per_user_queued", "4", "between 0 and 3"),
        ],
    )
    def test_out_of_range_values_are_explained(self, field, value, message):
        _, errors = download_limits.Limits.parse(_form(**{field: value}))
        assert errors
        assert message in errors[0]

    def test_per_user_active_cannot_exceed_the_slots(self):
        _, errors = download_limits.Limits.parse(_form(max_concurrent="2", per_user_active="3"))
        assert errors and "cannot exceed concurrent downloads" in errors[0]

    def test_a_raised_slot_count_allows_more_per_user_jobs(self):
        limits, errors = download_limits.Limits.parse(
            _form(max_concurrent="4", per_user_active="2")
        )
        assert not errors
        assert limits.per_user_active == 2

    def test_non_numbers_are_rejected_without_tracebacks(self):
        _, errors = download_limits.Limits.parse(_form(max_concurrent="many"))
        assert errors and "enter a number" in errors[0]

    def test_a_fractional_integer_field_is_rejected(self):
        _, errors = download_limits.Limits.parse(_form(max_wait_seconds="120.5"))
        assert errors and "whole number" in errors[0]

    def test_comma_decimals_are_accepted(self):
        limits, errors = download_limits.Limits.parse(_form(estimate_mbps="2,5"))
        assert not errors
        assert limits.estimate_mbps == 2.5


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

class TestPresets:
    def test_the_four_presets_match_the_documented_table(self):
        table = {
            preset.key: (
                preset.max_concurrent,
                preset.max_queue_length,
                preset.max_wait_seconds,
                preset.per_user_active,
            )
            for preset in download_limits.PRESETS
        }
        assert table == {
            "tiny": (1, 4, 90, 1),
            "small": (2, 6, 120, 1),
            "medium": (4, 12, 180, 2),
            "large": (6, 20, 240, 3),
        }

    @pytest.mark.parametrize(
        "slots,expected", [(1, 1), (2, 1), (3, 1), (4, 2), (5, 2), (6, 3), (8, 3)]
    )
    def test_per_user_active_follows_the_formula(self, slots, expected):
        assert download_limits.per_user_active_for(slots) == expected

    def test_every_preset_is_valid_on_its_own(self):
        for preset in download_limits.PRESETS:
            limits, errors = download_limits.Limits.parse(preset.as_limits().as_form())
            assert not errors, (preset.key, errors)

    def test_recommendation_picks_the_largest_fitting_preset(self):
        hardware = download_limits.Hardware(cpu=2, memory_gb=4.0)
        assert download_limits.recommended_preset(hardware).key == "small"

        bigger = download_limits.Hardware(cpu=16, memory_gb=64.0)
        assert download_limits.recommended_preset(bigger).key == "large"

    def test_a_tiny_box_gets_the_smallest_preset(self):
        hardware = download_limits.Hardware(cpu=1, memory_gb=1.0)
        assert download_limits.recommended_preset(hardware).key == "tiny"

    def test_memory_caps_the_recommendation(self):
        # 8 vCPU but only 4 GB must not be recommended the 8 vCPU preset.
        hardware = download_limits.Hardware(cpu=8, memory_gb=4.0)
        assert download_limits.recommended_preset(hardware).key == "small"

    def test_detect_hardware_reports_something_sane(self):
        hardware = download_limits.detect_hardware(cpu=3, memory_gb=7.5)
        assert hardware.cpu == 3
        assert hardware.memory_label == "7.5 GB"

    def test_preset_matching_ignores_the_estimate(self):
        limits = download_limits.PRESETS_BY_KEY["medium"].as_limits()
        assert limits.matches_preset(download_limits.PRESETS_BY_KEY["medium"])
        changed = download_limits.Limits.parse(_form(max_concurrent="1"))[0]
        assert not changed.matches_preset(download_limits.PRESETS_BY_KEY["medium"])


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class TestStorage:
    def test_defaults_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "3")
        monkeypatch.setenv("QUEUE_MAX_WAIT_SECONDS", "150")
        limits = download_limits.Limits.from_env()
        assert limits.max_concurrent == 3
        assert limits.max_wait_seconds == 150

    def test_saved_values_override_the_environment(self, monkeypatch):
        monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "3")
        stored = {"max_concurrent": "1", "max_wait_seconds": "90"}
        limits = download_limits.Limits.from_row(stored, base=download_limits.Limits.from_env())
        assert limits.max_concurrent == 1
        assert limits.max_wait_seconds == 90
        # Untouched fields keep their environment value.
        assert limits.per_user_active == download_limits.Limits.from_env().per_user_active

    def test_invalid_stored_values_fall_back_instead_of_crashing(self):
        base = download_limits.Limits()
        limits = download_limits.Limits.from_row({"max_concurrent": "banana"}, base=base)
        assert limits == base

    def test_settings_table_round_trips_strings(self):
        async def _run():
            await db.save_settings({"download_limits.max_concurrent": "4"})
            return await db.get_settings(prefix="download_limits.")

        stored = asyncio.run(_run())
        assert stored == {"download_limits.max_concurrent": "4"}

    def test_saving_updates_an_existing_row(self):
        async def _run():
            await db.save_settings({"download_limits.max_concurrent": "4"})
            await db.save_settings({"download_limits.max_concurrent": "6"})
            return await db.get_setting("download_limits.max_concurrent")

        assert asyncio.run(_run()) == "6"

    def test_settings_survive_a_reconnect(self):
        async def _write():
            await db.save_settings({"download_limits.max_wait_seconds": "300"})

        async def _read():
            return await db.get_setting("download_limits.max_wait_seconds")

        asyncio.run(_write())
        # A new connection reads the same row (WAL, no in-memory cache).
        assert asyncio.run(_read()) == "300"


# ---------------------------------------------------------------------------
# The Admin page and the save endpoint
# ---------------------------------------------------------------------------

class TestAdminPage:
    def test_page_requires_login(self):
        fresh = TestClient(app)
        resp = fresh.get("/admin/limits", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_section_renders_every_field_and_its_csrf(self):
        c = _login(TestClient(app))
        html = c.get("/admin/limits").text
        for field in download_limits.FIELD_NAMES:
            assert f'name="{field}"' in html, field
        assert 'id="limits-form"' in html
        assert 'id="limits-save"' in html
        assert 'name="csrf_token"' in html
        assert "Download limits" in html

    def test_page_lists_the_presets_and_names_the_recommended_one(self):
        c = _login(TestClient(app))
        html = c.get("/admin/limits").text
        for preset in download_limits.PRESETS:
            assert preset.label in html
        assert "recommended" in html
        assert "This server:" in html

    def test_page_has_no_inline_handlers(self):
        c = _login(TestClient(app))
        html = c.get("/admin/limits").text
        assert "onclick=" not in html
        assert "<script>" not in html

    def test_save_requires_csrf(self):
        c = _login(TestClient(app))
        resp = c.post("/admin/limits", data=_form())
        assert resp.status_code == 403
        assert "session expired" in resp.text.lower()

    def test_save_requires_login(self):
        fresh = TestClient(app)
        fresh.get("/login")
        token = fresh.cookies.get("koyura_csrf") or ""
        fresh.post(
            "/login",
            data={"username": "admin", "password": os.environ["ADMIN_PASSWORD"],
                  "csrf_token": token},
            follow_redirects=False,
        )
        fresh.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        resp = fresh.post("/admin/limits", data=_form(), follow_redirects=False)
        assert resp.status_code == 303

    def test_a_valid_save_stores_the_values(self):
        c = _login(TestClient(app))
        resp = _post_form(
            _form(max_concurrent="4", per_user_active="2", max_queue_length="12"), c
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/limits?saved=1"

        async def _read():
            return await db.get_settings(prefix="download_limits.")

        stored = asyncio.run(_read())
        assert stored["download_limits.max_concurrent"] == "4"
        assert stored["download_limits.per_user_active"] == "2"
        assert stored["download_limits.max_queue_length"] == "12"

    def test_a_valid_save_pushes_the_limits_to_the_engine(self):
        c = _login(TestClient(app))
        with patch.object(
            download_limits, "apply_to_engine", return_value=(True, "applied")
        ) as applied:
            resp = _post_form(_form(max_concurrent="3"), c)
        assert resp.status_code == 303
        assert applied.call_count == 1
        pushed = applied.call_args.args[0]
        assert pushed.max_concurrent == 3

    def test_an_invalid_save_is_rejected_and_keeps_the_typed_values(self):
        c = _login(TestClient(app))
        with patch.object(download_limits, "apply_to_engine") as applied:
            resp = _post_form(_form(max_concurrent="99", max_wait_seconds="42"), c)
        assert resp.status_code == 400
        assert applied.call_count == 0
        assert "between 1 and 8" in resp.text
        # What was typed is still on screen.
        assert 'value="99"' in resp.text
        assert 'value="42"' in resp.text

    def test_an_invalid_save_changes_nothing_in_the_table(self):
        c = _login(TestClient(app))
        _post_form(_form(max_concurrent="0"), c)

        async def _read():
            return await db.get_settings(prefix="download_limits.")

        assert asyncio.run(_read()) == {}

    def test_a_save_that_the_engine_rejects_is_reported_but_kept(self):
        c = _login(TestClient(app))
        with patch.object(
            download_limits,
            "apply_to_engine",
            return_value=(False, "the media engine is not reachable right now"),
        ):
            resp = _post_form(_form(max_concurrent="2"), c)
        assert resp.status_code == 200
        assert "did not apply them" in resp.text

    def test_saving_writes_one_log_line_with_the_change(self, caplog):
        c = _login(TestClient(app))
        with caplog.at_level("INFO", logger="koyura.dashboard"):
            _post_form(_form(max_concurrent="4", per_user_active="2"), c)
        lines = [record.getMessage() for record in caplog.records
                 if "download limits updated" in record.getMessage()]
        assert len(lines) == 1
        assert "admin" in lines[0]
        assert "max_concurrent" in lines[0]
        assert "->" in lines[0]

    def test_the_saved_values_are_rendered_back_into_the_form(self):
        c = _login(TestClient(app))
        _post_form(_form(max_concurrent="3", max_wait_seconds="200"), c)
        html = c.get("/admin/limits").text
        assert 'name="max_concurrent"' in html
        assert 'value="3"' in html
        assert 'value="200"' in html


# ---------------------------------------------------------------------------
# Startup push
# ---------------------------------------------------------------------------

class TestStartupApply:
    def test_startup_pushes_the_saved_limits(self):
        from main import _apply_saved_limits

        async def _setup():
            await db.save_settings({
                "download_limits.max_concurrent": "3",
                "download_limits.max_wait_seconds": "200",
            })

        asyncio.run(_setup())
        with patch.object(
            download_limits, "apply_to_engine", return_value=(True, "applied")
        ) as applied:
            asyncio.run(_apply_saved_limits())
        assert applied.call_count == 1
        assert applied.call_args.args[0].max_concurrent == 3

    def test_startup_without_saved_limits_does_not_call_the_engine(self):
        from main import _apply_saved_limits

        with patch.object(download_limits, "apply_to_engine") as applied:
            asyncio.run(_apply_saved_limits())
        assert applied.call_count == 0

    def test_startup_survives_an_unreachable_engine(self):
        from main import _apply_saved_limits

        async def _setup():
            await db.save_settings({"download_limits.max_concurrent": "2"})

        asyncio.run(_setup())
        with patch.object(
            download_limits, "apply_to_engine", side_effect=OSError("boom")
        ):
            # Must not raise: a dashboard that cannot start is worse than one
            # that runs with the environment defaults.
            asyncio.run(_apply_saved_limits())


# ---------------------------------------------------------------------------
# Engine payload
# ---------------------------------------------------------------------------

class TestEnginePayload:
    def test_payload_uses_the_engine_key_names(self):
        payload = download_limits.Limits().as_engine_payload()
        assert set(payload) == {
            "max_concurrent",
            "per_user_active",
            "per_user_queued",
            "max_queue_length",
            "max_wait_seconds",
            "estimate_mbps",
        }
        assert isinstance(payload["max_concurrent"], int)
        assert isinstance(payload["estimate_mbps"], float)

    def test_change_description_is_empty_when_nothing_moved(self):
        limits = download_limits.Limits()
        assert download_limits.describe_change(limits, limits) == "no value changed"


# ---------------------------------------------------------------------------
# Engine address
# ---------------------------------------------------------------------------

def test_koyura_url_defaults_to_the_compose_service(monkeypatch):
    monkeypatch.delenv("KOYURA_ENGINE_URL", raising=False)
    assert download_limits.koyura_engine_url() == "http://koyura-engine:8899"


def test_koyura_url_comes_from_the_environment_without_a_trailing_slash(monkeypatch):
    monkeypatch.setenv("KOYURA_ENGINE_URL", "http://elsewhere:8899/")
    assert download_limits.koyura_engine_url() == "http://elsewhere:8899"
