"""Admin → Progress fields: the panel section and the API the bot reads.

Two layers, each pinned where it can actually break:

* delivery — ``GET /api/bot-settings`` grew a ``progress`` object, still behind
  ``X-Internal-Token``, which is how ``bot/progress_fields.py`` learns the set
  (``bot/bot_settings.py`` caches the answer for under a minute);
* the panel — the section is a partial (``_admin_progress.html``) included by
  ``admin_bot.html`` in one line, so the rest of the page and this feature
  do not edit the same regions. That single include line and the panel frame are
  asserted, not just the markup.

The model itself (defaults, parse, storage rows) is covered by
``test_progress_fields.py``.

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
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "admin_progress.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import bot_languages  # noqa: E402
import db  # noqa: E402
import progress_fields  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = DASHBOARD_DIR / "templates"
ADMIN_TEMPLATE = TEMPLATES / "admin_bot.html"
PARTIAL = TEMPLATES / "_admin_progress.html"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(db.init_db())


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    monkeypatch.setattr(auth, "login_rate_limiter", auth.LoginRateLimiter())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


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


def _csrf(client: TestClient) -> str:
    return client.cookies.get("koyura_csrf") or ""


async def _stored() -> dict[str, str]:
    return await db.get_settings(prefix=progress_fields.SETTING_PREFIX)


async def _reset() -> None:
    """Start each test from the defaults, not from the previous test's save."""
    await db.save_settings(progress_fields.ProgressFields.defaults().as_stored())


@pytest.fixture(autouse=True)
def _clean_settings():
    asyncio.run(_reset())
    yield


# ---------------------------------------------------------------------------
# The API the bot reads
# ---------------------------------------------------------------------------

class TestBotSettingsApi:
    def test_the_languages_pair_is_still_there(self):
        """Adding progress must not change what the languages client reads."""
        body = TestClient(app).get("/api/bot-settings").json()
        assert body["languages"] == ["en", "ru"]
        assert body["default_language"] == "en"

    def test_the_defaults_are_served(self, client):
        body = client.get("/api/bot-settings").json()
        assert body["progress"] == {
            "show_title": True, "show_bar": True, "show_percent": False,
            "show_size": False, "show_speed": False, "show_eta": False,
            "show_elapsed": False, "show_quality": False,
        }

    def test_a_saved_set_is_served(self, client):
        asyncio.run(
            db.save_settings(
                progress_fields.ProgressFields(values={
                    **progress_fields.DEFAULTS,
                    "show_percent": True,
                    "show_eta": True,
                }).as_stored()
            )
        )
        progress = client.get("/api/bot-settings").json()["progress"]
        assert progress["show_percent"] is True
        assert progress["show_eta"] is True
        assert progress["show_bar"] is True
        assert progress["show_size"] is False

    def test_the_token_is_required_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "s3cret-token-value")
        assert client.get("/api/bot-settings").status_code == 401
        right = client.get(
            "/api/bot-settings", headers={"X-Internal-Token": "s3cret-token-value"}
        )
        assert right.status_code == 200
        assert "progress" in right.json()


# ---------------------------------------------------------------------------
# The admin section
# ---------------------------------------------------------------------------

class TestAdminSection:
    def test_the_section_is_on_the_admin_page(self, admin):
        page = admin.get("/admin/bot")
        assert page.status_code == 200
        body = page.text
        assert 'id="progress-card"' in body
        assert "Progress fields" in body
        assert 'action="/admin/progress"' in body
        for name in progress_fields.FIELD_NAMES:
            assert f'id="progress-{name}"' in body
        assert 'name="fields"' in body
        assert 'name="csrf_token"' in body

    def test_every_checkbox_has_a_label_and_a_hint(self, admin):
        body = admin.get("/admin/bot").text
        for name, label, hint, _default in progress_fields.FIELDS:
            assert f'for="progress-{name}"' in body
            assert label in body
            assert f'id="progress-{name}-hint"' in body
            # The hint itself is rendered, so compare on its first clause.
            first_words = hint.split(" — ")[0].split(",")[0]
            assert first_words.replace("&", "&amp;") in body

    def test_the_defaults_are_ticked(self, admin):
        body = admin.get("/admin/bot").text
        title_tag = body.split('id="progress-show_title"')[1].split(">")[0]
        bar_tag = body.split('id="progress-show_bar"')[1].split(">")[0]
        speed_tag = body.split('id="progress-show_speed"')[1].split(">")[0]
        assert "checked" in title_tag
        assert "checked" in bar_tag
        assert "checked" not in speed_tag

    def test_the_section_lives_in_its_own_partial(self):
        """Kept out of admin_bot.html so the page and this section stay apart."""
        assert PARTIAL.exists()
        assert '{% include "_admin_progress.html" %}' in ADMIN_TEMPLATE.read_text()

    def test_the_include_is_a_single_line(self):
        include_lines = [
            line for line in ADMIN_TEMPLATE.read_text().splitlines()
            if "_admin_progress.html" in line
        ]
        assert include_lines == ['{% include "_admin_progress.html" %}']

    def test_the_partial_is_a_panel_like_its_neighbours(self):
        """Every /admin block is a ``card panel`` in a stack; this one matches."""
        text = PARTIAL.read_text()
        assert 'class="card panel" id="progress-card"' in text
        assert 'class="card-header panel-head"' in text
        body = ADMIN_TEMPLATE.read_text()
        stack = body.split('class="panel-stack"')[1].split("<!-- /panel-stack -->")[0]
        assert "_admin_progress.html" in stack

    def test_the_include_sits_after_the_languages_panel(self):
        """Order on the page is the order of the includes: languages, progress,
        messages. The languages panel moved to its own partial in the same
        split, so it is its include line that has to come first."""
        body = ADMIN_TEMPLATE.read_text()
        assert body.index('"_admin_languages.html"') < body.index("\"_admin_progress.html\"")
        languages = TEMPLATES / "_admin_languages.html"
        assert 'id="languages-card"' in languages.read_text()

    def test_the_partial_brings_no_inline_script_or_style(self):
        text = PARTIAL.read_text()
        assert "<script>" not in text
        assert "onclick=" not in text
        assert "<style>" not in text

    def test_the_state_line_names_what_is_shown(self, admin):
        assert "shown: title,bar" in admin.get("/admin/bot").text

    def test_saving_persists_and_redirects(self, admin):
        response = admin.post(
            "/admin/progress",
            data={
                "csrf_token": _csrf(admin),
                "fields": ["show_title", "show_bar", "show_percent", "show_speed"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/bot?progress_saved=1"
        stored = asyncio.run(_stored())
        assert stored["bot_progress.show_percent"] == "true"
        assert stored["bot_progress.show_speed"] == "true"
        assert stored["bot_progress.show_eta"] == "false"

    def test_saving_nothing_stores_a_minimal_set(self, admin):
        response = admin.post(
            "/admin/progress",
            data={"csrf_token": _csrf(admin)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        stored = asyncio.run(_stored())
        assert set(stored.values()) == {"false"}

    def test_the_saved_set_is_reflected_on_the_page(self, admin):
        admin.post(
            "/admin/progress",
            data={"csrf_token": _csrf(admin), "fields": ["show_title", "show_eta"]},
            follow_redirects=False,
        )
        body = admin.get("/admin?progress_saved=1").text
        assert "shown: title,eta" in body
        assert "Progress fields saved" in body
        eta_tag = body.split('id="progress-show_eta"')[1].split(">")[0]
        bar_tag = body.split('id="progress-show_bar"')[1].split(">")[0]
        assert "checked" in eta_tag
        assert "checked" not in bar_tag

    def test_an_unknown_field_is_refused_and_nothing_is_stored(self, admin):
        response = admin.post(
            "/admin/progress",
            data={"csrf_token": _csrf(admin), "fields": ["show_title", "show_pizza"]},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "Unknown progress field" in response.text
        stored = asyncio.run(_stored())
        assert stored["bot_progress.show_title"] == "true"
        assert stored["bot_progress.show_speed"] == "false"

    def test_a_missing_csrf_token_is_forbidden(self, admin):
        response = admin.post(
            "/admin/progress",
            data={"fields": ["show_speed"]},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert asyncio.run(_stored())["bot_progress.show_speed"] == "false"

    def test_the_save_needs_a_session(self, client):
        response = client.post(
            "/admin/progress",
            data={"fields": ["show_speed"]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_section_does_not_disturb_the_languages_form(self, admin):
        """Two sections, two forms, one page: each keeps its own action."""
        body = admin.get("/admin/bot").text
        assert 'action="/admin/languages"' in body
        assert 'action="/admin/progress"' in body
        # Saving one leaves the other alone: no languages row was written, so
        # the bot keeps its built-in pair.
        admin.post(
            "/admin/progress",
            data={"csrf_token": _csrf(admin), "fields": ["show_speed"]},
            follow_redirects=False,
        )
        languages = asyncio.run(
            db.get_settings(prefix=bot_languages.SETTING_PREFIX)
        )
        assert "bot_settings.languages" not in languages
        assert "bot_settings.default_language" not in languages
