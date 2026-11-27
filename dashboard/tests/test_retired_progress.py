"""The retired ``Progress fields`` section leaves nothing behind that breaks.

The section, the ``bot_progress.*`` switches and the ``progress`` object in
``/api/bot-settings`` are gone: which lines the live message carries is the
``progress`` message template now. A deployed panel still has the old rows in its
``settings`` table, so this module pins the upgrade path:

* the page renders with those rows present, and does not show the section;
* ``/api/bot-settings`` carries no ``progress`` object, whatever the rows say;
* the startup sweep deletes them, and a failure there is logged, never fatal;
* a submitted ``/admin/progress`` form is redirected, not stored.

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "retired_progress.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import main  # noqa: E402

app = main.create_app()

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = DASHBOARD_DIR / "templates"

#: What an upgraded deployment still has in ``settings``.
RETIRED_ROWS = {
    "bot_progress.show_title": "true",
    "bot_progress.show_bar": "true",
    "bot_progress.show_percent": "true",
    "bot_progress.show_speed": "false",
    "bot_progress.show_eta": "false",
    "bot_progress.show_elapsed": "false",
    "bot_progress.show_size": "false",
    "bot_progress.show_quality": "false",
}

#: The real sweep, captured before any test monkeypatches it for the failure case.
REAL_DELETE = db.delete_settings


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(db.init_db())


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    monkeypatch.setattr(auth, "login_rate_limiter", auth.LoginRateLimiter())


@pytest.fixture(autouse=True)
def _retired_rows():
    """Every test starts with the old rows in the table."""
    asyncio.run(db.save_settings(dict(RETIRED_ROWS)))
    yield
    asyncio.run(REAL_DELETE("bot_progress."))


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    page = client.get("/login")
    assert page.status_code == 200, page.text
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": client.cookies.get("koyura_csrf") or "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, f"login failed: {response.status_code}"
    return client


def _stored() -> dict[str, str]:
    return asyncio.run(db.get_settings(prefix="bot_progress."))


# ---------------------------------------------------------------------------
# Stale rows do not break anything
# ---------------------------------------------------------------------------

class TestThePanelIgnoresThem:
    def test_the_bot_page_renders_with_the_old_rows(self, admin):
        page = admin.get("/admin/bot")
        assert page.status_code == 200
        # The two panels that stayed are there...
        assert 'id="languages-card"' in page.text
        assert 'id="templates-card"' in page.text
        # ...and the retired one is not.
        assert 'id="progress-card"' not in page.text
        assert "Progress fields" not in page.text

    def test_the_languages_section_points_at_the_templates(self, admin):
        body = admin.get("/admin/bot").text
        assert 'id="languages-templates-note"' in body
        assert "Message templates" in body
        # The link is a fragment, so it works with JS off and cannot 404.
        assert 'href="#templates-card"' in body

    def test_the_api_serves_no_progress_object(self, client):
        body = client.get("/api/bot-settings").json()
        assert "progress" not in body
        assert body["languages"] == ["en", "ru"]
        assert body["templates"] == {}

    def test_the_templates_carry_the_progress_body_instead(self, admin):
        """The body the bot renders from is the templates one."""
        body = admin.get("/api/bot-settings").json()
        assert "progress" in {
            item["id"] for item in admin.get("/api/message-templates").json()["kinds"]
        }
        # Nothing was saved, so no custom body — the bot uses its built-in text.
        assert body["templates"] == {}

    def test_the_delete_leaves_the_other_settings_alone(self):
        asyncio.run(db.save_settings({"bot_settings.languages": "en,ru"}))
        removed = asyncio.run(db.delete_settings("bot_progress."))
        assert removed == len(RETIRED_ROWS)
        assert _stored() == {}
        kept = asyncio.run(db.get_settings(prefix="bot_settings."))
        assert kept["bot_settings.languages"] == "en,ru"

    def test_deleting_by_a_prefix_with_wildcards_is_literal(self):
        stored = _stored()
        assert asyncio.run(db.delete_settings("bot_progress%")) == 0
        assert _stored() == stored


# ---------------------------------------------------------------------------
# The startup sweep
# ---------------------------------------------------------------------------

class TestTheStartupSweep:
    def test_the_old_rows_are_removed(self):
        assert _stored() != {}
        asyncio.run(main._drop_retired_progress_rows())
        assert _stored() == {}

    def test_a_clean_table_is_not_an_error(self):
        asyncio.run(REAL_DELETE("bot_progress."))
        asyncio.run(main._drop_retired_progress_rows())
        assert _stored() == {}

    def test_a_failure_is_logged_and_swallowed(self, monkeypatch, caplog):
        """A panel that starts with stale rows is fine; one that refuses is not."""

        async def _boom(prefix: str) -> int:
            raise RuntimeError("no such table: settings")

        monkeypatch.setattr(db, "delete_settings", _boom)
        with caplog.at_level("INFO", logger="koyura.dashboard"):
            asyncio.run(main._drop_retired_progress_rows())
        assert any("retired" in record.message for record in caplog.records)
        # Nothing was raised, and the rows are simply still there.
        assert _stored() != {}


# ---------------------------------------------------------------------------
# The retired endpoint
# ---------------------------------------------------------------------------

class TestTheRetiredEndpoint:
    def test_a_submission_redirects_to_the_templates_section(self, admin):
        response = admin.post(
            "/admin/progress",
            data={
                "csrf_token": admin.cookies.get("koyura_csrf") or "",
                "fields": ["show_speed"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/admin/bot?kind=progress")
        assert location.endswith("#templates-card")

    def test_nothing_is_written(self, admin):
        before = dict(_stored())
        admin.post(
            "/admin/progress",
            data={
                "csrf_token": admin.cookies.get("koyura_csrf") or "",
                "fields": ["show_speed", "show_percent"],
            },
            follow_redirects=False,
        )
        assert _stored() == before

    def test_the_csrf_token_is_still_required(self, admin):
        response = admin.post(
            "/admin/progress",
            data={"fields": ["show_speed"]},
            follow_redirects=False,
        )
        assert response.status_code == 403

    def test_the_session_is_still_required(self, client):
        response = client.post(
            "/admin/progress",
            data={"fields": ["show_speed"]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_old_query_string_still_lands_somewhere_sensible(self, admin):
        """``?progress_saved=1`` from a bookmarked redirect explains itself."""
        page = admin.get("/admin/bot?progress_saved=1")
        assert page.status_code == 200
        assert "Message templates" in page.text


# ---------------------------------------------------------------------------
# The partials are gone
# ---------------------------------------------------------------------------

class TestTheFilesAreGone:
    def test_the_partial_is_not_shipped(self):
        assert not (TEMPLATES / "_admin_progress.html").exists()

    def test_the_page_no_longer_includes_it(self):
        body = (TEMPLATES / "admin_bot.html").read_text()
        assert "_admin_progress.html" not in body

    def test_the_module_is_not_shipped(self):
        assert not (DASHBOARD_DIR / "progress_fields.py").exists()
