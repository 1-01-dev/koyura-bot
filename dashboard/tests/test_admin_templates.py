"""Admin → Message templates: the section, its actions and the API.

Two layers, each pinned where it can break:

* delivery — ``GET /api/bot-settings`` grew a ``templates`` object and
  ``GET /api/message-templates`` serves the catalogue, both behind
  ``X-Internal-Token``; that is how ``bot/message_templates.py`` learns the
  bodies (``bot/bot_settings.py`` caches the answer for under a minute);
* the panel — the section is a partial included by ``admin_bot.html`` in one
  line, so the page and this feature never edit the same region. The include
  line, the panel frame, the editor, the preview and the placeholder
  reference table are asserted, not just the routes.

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
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "admin_templates.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import message_catalog  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = DASHBOARD_DIR / "templates"
ADMIN_TEMPLATE = TEMPLATES / "admin_bot.html"
PARTIAL = TEMPLATES / "_admin_templates.html"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(db.init_db())


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(auth, "login_rate_limiter", auth.LoginRateLimiter())

    async def clean() -> None:
        for row in await db.list_message_templates():
            await db.delete_message_template(int(row["id"]))

    asyncio.run(clean())
    yield
    asyncio.run(clean())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _csrf(client: TestClient) -> str:
    return client.cookies.get("koyura_csrf") or ""


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    """A client that has completed the login form."""
    page = client.get("/login")
    assert page.status_code == 200, page.text
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, f"login failed: {response.status_code}"
    return client


def _save(admin: TestClient, *, kind="card", lang="en", name="Mine", body="@title"):
    return admin.post(
        "/admin/templates/save",
        data={
            "csrf_token": _csrf(admin),
            "kind": kind,
            "lang": lang,
            "name": name,
            "body": body,
        },
        follow_redirects=False,
    )


async def _rows() -> list[dict]:
    return await db.list_message_templates()


# ---------------------------------------------------------------------------
# The API the bot reads
# ---------------------------------------------------------------------------

class TestBotSettingsApi:
    def test_templates_start_empty(self, client):
        body = client.get("/api/bot-settings").json()
        assert body["templates"] == {}
        # The older fields are untouched.
        assert body["languages"] == ["en", "ru"]

    def test_the_retired_progress_switches_are_not_served(self, client):
        """The object that used to carry them is gone; nothing reads it now.

        The bot decides what a progress message says from the ``progress``
        template, so serving a second set of switches would only give it two
        sources of truth again.
        """
        assert "progress" not in client.get("/api/bot-settings").json()

    def test_an_active_body_is_served(self, admin):
        _save(admin)
        templates = admin.get("/api/bot-settings").json()["templates"]
        assert templates == {"card": {"en": "@title"}}

    def test_an_inactive_body_is_not_served(self, admin):
        _save(admin, name="First", body="@title")
        admin.post(
            "/admin/templates/save",
            data={
                "csrf_token": _csrf(admin), "kind": "card", "lang": "en",
                "name": "Second", "body": "@title — @views",
            },
            follow_redirects=False,
        )
        templates = admin.get("/api/bot-settings").json()["templates"]
        assert templates == {"card": {"en": "@title — @views"}}

    def test_the_catalogue_endpoint_lists_the_kinds(self, client):
        body = client.get("/api/message-templates").json()
        assert len(body["kinds"]) == len(message_catalog.KINDS)
        ids = {kind["id"] for kind in body["kinds"]}
        assert {"welcome", "card", "progress", "done", "error"} <= ids
        assert body["coverage"]["total"] == len(message_catalog.KINDS)

    def test_the_token_is_required(self, client, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "s3cret-token-value")
        assert client.get("/api/message-templates").status_code == 401
        assert client.get("/api/bot-settings").status_code == 401
        right = client.get(
            "/api/message-templates", headers={"X-Internal-Token": "s3cret-token-value"}
        )
        assert right.status_code == 200


# ---------------------------------------------------------------------------
# The section on /admin
# ---------------------------------------------------------------------------

class TestAdminSection:
    def test_the_section_is_on_the_admin_page(self, admin):
        page = admin.get("/admin/bot")
        assert page.status_code == 200
        body = page.text
        assert 'id="templates-card"' in body
        assert "Message templates" in body
        assert 'action="/admin/templates/save"' in body
        assert 'action="/admin/templates/reset"' in body
        assert 'id="template-body"' in body
        assert 'id="template-preview"' in body
        assert 'name="csrf_token"' in body

    def test_the_kind_list_is_grouped(self, admin):
        body = admin.get("/admin/bot").text
        for group in message_catalog.categories():
            assert str(group["label"]) in body
        for kind in message_catalog.KINDS[:5]:
            assert f"kind={kind.id}" in body

    def test_the_placeholder_reference_is_rendered(self, admin):
        body = admin.get("/admin/bot?kind=card&lang=en").text
        assert "Справочник плейсхолдеров" in body
        assert "Что подставляет" in body
        card = message_catalog.get("card")
        assert card is not None
        for placeholder in card.placeholder_objects():
            assert placeholder.token in body
            assert placeholder.summary in body

    def test_the_default_is_offered_and_previewed(self, admin):
        body = admin.get("/admin/bot?kind=card&lang=en").text
        assert "Default" in body
        assert "встроенный" in body
        card = message_catalog.get("card")
        assert card is not None
        assert card.samples()["title"] in body

    def test_the_editor_shows_the_default_body_for_a_new_template(self, admin):
        body = admin.get("/admin/bot?kind=card&lang=ru").text
        card = message_catalog.get("card")
        assert card is not None
        default = card.template("ru")
        assert default in body

    def test_the_section_lives_in_its_own_partial(self):
        assert PARTIAL.exists()
        assert '{% include "_admin_templates.html" %}' in ADMIN_TEMPLATE.read_text()

    def test_the_include_is_a_single_line(self):
        include_lines = [
            line for line in ADMIN_TEMPLATE.read_text().splitlines()
            if "_admin_templates.html" in line
        ]
        assert include_lines == ['{% include "_admin_templates.html" %}']

    def test_the_partial_is_a_panel_like_its_neighbours(self):
        text = PARTIAL.read_text()
        assert 'class="card panel" id="templates-card"' in text
        assert 'class="card-header panel-head"' in text
        assert "data-insert" in text
        assert "data-confirm" in text


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

class TestSaving:
    def test_a_new_body_is_stored_and_activated(self, admin):
        response = _save(admin, name="Short card", body="@title — @views")
        assert response.status_code == 303
        rows = asyncio.run(_rows())
        assert len(rows) == 1
        assert rows[0]["name"] == "Short card"
        assert rows[0]["is_active"] == 1

    def test_the_redirect_opens_the_saved_template(self, admin):
        response = _save(admin)
        assert "kind=card" in response.headers["location"]
        assert "template=" in response.headers["location"]

    def test_an_existing_body_is_updated(self, admin):
        _save(admin, name="Mine", body="@title")
        row = asyncio.run(_rows())[0]
        admin.post(
            "/admin/templates/save",
            data={
                "csrf_token": _csrf(admin), "kind": "card", "lang": "en",
                "name": "Renamed", "body": "@title — @source",
                "template_id": str(row["id"]),
            },
            follow_redirects=False,
        )
        rows = asyncio.run(_rows())
        assert len(rows) == 1
        assert rows[0]["name"] == "Renamed"
        assert rows[0]["body"] == "@title — @source"

    def test_an_unknown_placeholder_is_refused_and_nothing_is_saved(self, admin):
        response = _save(admin, body="@nope")
        assert response.status_code == 400
        assert "@nope" in response.text
        assert asyncio.run(_rows()) == []

    def test_an_empty_body_is_refused(self, admin):
        response = _save(admin, body="   ")
        assert response.status_code == 400
        assert asyncio.run(_rows()) == []

    def test_a_missing_name_is_refused(self, admin):
        response = _save(admin, name="   ")
        assert response.status_code == 400
        assert asyncio.run(_rows()) == []

    def test_a_rejected_body_is_echoed_back(self, admin):
        response = _save(admin, body="@nope keeps typing")
        assert "@nope keeps typing" in response.text

    def test_a_missing_csrf_token_is_refused(self, admin):
        response = admin.post(
            "/admin/templates/save",
            data={"kind": "card", "lang": "en", "name": "Mine", "body": "@title"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert asyncio.run(_rows()) == []

    def test_a_script_tag_is_escaped_in_the_editor(self, admin):
        _save(admin, body="<script>alert(1)</script> @title")
        body = admin.get("/admin/bot?kind=card&lang=en").text
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# The four row actions
# ---------------------------------------------------------------------------

class TestActions:
    def _two(self, admin: TestClient) -> tuple[int, int]:
        _save(admin, name="First", body="@title")
        _save(admin, name="Second", body="@title — @views")
        rows = asyncio.run(_rows())
        second = next(row for row in rows if row["name"] == "Second")
        first = next(row for row in rows if row["name"] == "First")
        return int(first["id"]), int(second["id"])

    def test_activating_leaves_exactly_one_active(self, admin):
        first, _second = self._two(admin)
        response = admin.post(
            "/admin/templates/activate",
            data={"csrf_token": _csrf(admin), "template_id": str(first)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        rows = asyncio.run(_rows())
        active = [row for row in rows if row["is_active"]]
        assert len(active) == 1
        assert active[0]["id"] == first

    def test_duplicating_creates_an_inactive_copy(self, admin):
        first, _second = self._two(admin)
        response = admin.post(
            "/admin/templates/duplicate",
            data={"csrf_token": _csrf(admin), "template_id": str(first)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        rows = asyncio.run(_rows())
        copies = [row for row in rows if str(row["name"]).endswith("(copy)")]
        assert len(copies) == 1
        assert copies[0]["is_active"] == 0

    def test_deleting_an_active_body_falls_back_to_the_default(self, admin):
        first, second = self._two(admin)  # "Second" is the active one
        admin.post(
            "/admin/templates/delete",
            data={"csrf_token": _csrf(admin), "template_id": str(second)},
            follow_redirects=False,
        )
        # Deleting the active body leaves no active one: the bot renders the
        # built-in default again.
        assert admin.get("/api/bot-settings").json()["templates"] == {}
        assert len(asyncio.run(_rows())) == 1  # the inactive "First" is still stored

        admin.post(
            "/admin/templates/delete",
            data={"csrf_token": _csrf(admin), "template_id": str(first)},
            follow_redirects=False,
        )
        assert asyncio.run(_rows()) == []

    def test_resetting_removes_every_body_for_the_pair(self, admin):
        self._two(admin)
        response = admin.post(
            "/admin/templates/reset",
            data={"csrf_token": _csrf(admin), "kind": "card", "lang": "en"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert asyncio.run(_rows()) == []

    def test_resetting_one_language_leaves_the_other(self, admin):
        _save(admin, lang="en", name="English", body="@title")
        _save(admin, lang="ru", name="Russian", body="@title")
        admin.post(
            "/admin/templates/reset",
            data={"csrf_token": _csrf(admin), "kind": "card", "lang": "en"},
            follow_redirects=False,
        )
        rows = asyncio.run(_rows())
        assert [row["lang"] for row in rows] == ["ru"]


# ---------------------------------------------------------------------------
# Preview endpoint
# ---------------------------------------------------------------------------

class TestPreviewEndpoint:
    def test_a_body_renders_with_the_sample_values(self, admin):
        response = admin.post(
            "/api/message-templates/preview",
            json={"kind": "card", "lang": "en", "body": "@title — @views"},
            headers={"X-CSRF-Token": _csrf(admin)},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["error"] is None
        assert payload["limit"] == 1024
        card = message_catalog.get("card")
        assert card is not None
        assert card.samples()["title"] in payload["text"]

    def test_a_broken_body_comes_back_as_a_sentence(self, admin):
        payload = admin.post(
            "/api/message-templates/preview",
            json={"kind": "card", "lang": "en", "body": "@nope"},
            headers={"X-CSRF-Token": _csrf(admin)},
        ).json()
        assert payload["text"] == ""
        assert "@nope" in payload["error"]

    def test_the_default_renders_when_no_body_is_sent(self, admin):
        payload = admin.post(
            "/api/message-templates/preview",
            json={"kind": "welcome", "lang": "en"},
            headers={"X-CSRF-Token": _csrf(admin)},
        ).json()
        assert payload["error"] is None
        assert "Koyura" in payload["text"]

    def test_an_unknown_kind_is_a_404(self, admin):
        response = admin.post(
            "/api/message-templates/preview",
            json={"kind": "nope", "body": "@title"},
            headers={"X-CSRF-Token": _csrf(admin)},
        )
        assert response.status_code == 404

    def test_the_session_is_required(self, client):
        response = client.post(
            "/api/message-templates/preview",
            json={"kind": "card", "body": "@title"},
        )
        assert response.status_code == 401

    def test_the_csrf_token_is_required(self, admin):
        response = admin.post(
            "/api/message-templates/preview",
            json={"kind": "card", "body": "@title"},
        )
        assert response.status_code == 403
