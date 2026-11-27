"""Admin → Bot languages, and the read-only API the bot uses.

Three layers, the same shape as the limits tests:

* validation — at least one language stays on, the default is one of them;
* storage — the two rows in the dashboard's ``settings`` table
  (``bot_settings.languages`` = ``"en,ru"``, ``bot_settings.default_language``);
* delivery — ``GET /api/bot-settings`` behind ``X-Internal-Token``, which is how
  ``bot/bot_settings.py`` learns the pair (and caches it for under a minute).

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "languages.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import bot_languages  # noqa: E402
import db  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()


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
    return await db.get_settings(prefix=bot_languages.SETTING_PREFIX)


async def _reset() -> None:
    """Start each test from the defaults, not from the previous test's save."""
    await db.save_settings(bot_languages.BotLanguages.defaults().as_stored())


@pytest.fixture(autouse=True)
def _clean_settings():
    asyncio.run(_reset())
    yield


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestParse:
    def test_both_languages_are_valid(self):
        settings, errors = bot_languages.parse(["en", "ru"], "ru")
        assert errors == []
        assert settings is not None
        assert settings.languages == ("en", "ru")
        assert settings.default_language == "ru"

    def test_order_follows_the_code_not_the_form(self):
        settings, _ = bot_languages.parse(["ru", "en"], "en")
        assert settings is not None
        assert settings.languages == ("en", "ru")

    def test_you_cannot_turn_every_language_off(self):
        settings, errors = bot_languages.parse([], "en")
        assert settings is None
        assert errors == [
            "Keep at least one language enabled — the bot has to answer."
        ]

    def test_the_default_has_to_be_enabled(self):
        settings, errors = bot_languages.parse(["en"], "ru")
        assert settings is None
        assert errors and "Choose the default language" in errors[0]

    def test_an_unknown_code_is_ignored(self):
        settings, errors = bot_languages.parse(["en", "de"], "en")
        assert errors == []
        assert settings is not None
        assert settings.languages == ("en",)

    def test_unknown_codes_alone_are_refused(self):
        settings, errors = bot_languages.parse(["de"], "de")
        assert settings is None
        assert errors


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class TestStorage:
    def test_defaults_when_nothing_is_stored(self):
        settings = bot_languages.BotLanguages.from_row({})
        assert settings.languages == ("en", "ru")
        assert settings.default_language == "en"

    def test_a_stored_row_round_trips(self):
        settings = bot_languages.BotLanguages(languages=("ru",), default_language="ru")
        row = settings.as_stored()
        assert row == {
            "bot_settings.languages": "ru",
            "bot_settings.default_language": "ru",
        }
        assert bot_languages.BotLanguages.from_row(row) == settings

    def test_a_hand_edited_row_does_not_break_the_panel(self):
        settings = bot_languages.BotLanguages.from_row(
            {"bot_settings.languages": "klingon,,", "bot_settings.default_language": "klingon"}
        )
        assert settings.languages == ("en", "ru")
        assert settings.default_language == "en"

    def test_a_default_outside_the_enabled_set_is_repaired(self):
        settings = bot_languages.BotLanguages.from_row(
            {"bot_settings.languages": "ru", "bot_settings.default_language": "en"}
        )
        assert settings.languages == ("ru",)
        assert settings.default_language == "ru"

    def test_the_form_lists_every_language(self):
        form = bot_languages.BotLanguages(languages=("en",), default_language="en").as_form()
        options = form["options"]
        assert isinstance(options, list)
        assert [option["code"] for option in options] == ["en", "ru"]
        assert [option["enabled"] for option in options] == [True, False]

    @pytest.mark.asyncio
    async def test_the_rows_go_to_the_settings_table(self):
        pair = bot_languages.BotLanguages(languages=("en", "ru"), default_language="ru")
        await db.save_settings(pair.as_stored())
        stored = await _stored()
        assert stored["bot_settings.languages"] == "en,ru"
        assert stored["bot_settings.default_language"] == "ru"


# ---------------------------------------------------------------------------
# The API the bot reads
# ---------------------------------------------------------------------------

class TestBotSettingsApi:
    def test_the_default_pair_is_served(self, client):
        response = client.get("/api/bot-settings")
        assert response.status_code == 200
        body = response.json()
        # The languages keys are this module's contract; the body also carries
        # the progress switches (test_progress_fields.py owns those).
        assert body["languages"] == ["en", "ru"]
        assert body["default_language"] == "en"

    def test_a_saved_pair_is_served(self, client):
        asyncio.run(
            db.save_settings(
                bot_languages.BotLanguages(
                    languages=("ru",), default_language="ru"
                ).as_stored()
            )
        )
        body = client.get("/api/bot-settings").json()
        assert body["languages"] == ["ru"]
        assert body["default_language"] == "ru"

    def test_the_token_is_required_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "s3cret-token-value")
        assert client.get("/api/bot-settings").status_code == 401
        wrong = client.get(
            "/api/bot-settings", headers={"X-Internal-Token": "nope"}
        )
        assert wrong.status_code == 401
        right = client.get(
            "/api/bot-settings", headers={"X-Internal-Token": "s3cret-token-value"}
        )
        assert right.status_code == 200
        assert right.json()["languages"] == ["en", "ru"]

    def test_no_session_is_needed(self, client):
        """The bot has no cookie jar: only the shared token guards this route."""
        assert "koyura_session" not in client.cookies
        assert client.get("/api/bot-settings").status_code == 200


# ---------------------------------------------------------------------------
# The admin form
# ---------------------------------------------------------------------------

class TestAdminForm:
    def test_the_section_is_on_the_admin_page(self, admin):
        page = admin.get("/admin")
        assert page.status_code == 200
        body = page.text
        assert 'id="languages-card"' in body
        assert 'action="/admin/languages"' in body
        assert 'name="csrf_token"' in body
        for code in bot_languages.LANGUAGES:
            assert f'id="language-{code}"' in body
            assert 'name="languages"' in body
        assert 'name="default_language"' in body
        assert 'id="default-language"' in body

    def test_the_current_pair_is_reflected(self, admin):
        body = admin.get("/admin").text
        assert 'value="en"' in body and 'value="ru"' in body
        assert "en, ru · default en" in body

    def test_saving_persists_and_redirects(self, admin):
        response = admin.post(
            "/admin/languages",
            data={
                "csrf_token": _csrf(admin),
                "languages": ["ru"],
                "default_language": "ru",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?languages_saved=1"
        stored = asyncio.run(_stored())
        assert stored["bot_settings.languages"] == "ru"
        assert stored["bot_settings.default_language"] == "ru"

    def test_saving_nothing_is_refused_with_the_reason(self, admin):
        response = admin.post(
            "/admin/languages",
            data={"csrf_token": _csrf(admin), "default_language": "en"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "Keep at least one language enabled" in response.text
        stored = asyncio.run(_stored())
        assert stored["bot_settings.languages"] == "en,ru"

    def test_a_default_outside_the_selection_is_refused(self, admin):
        response = admin.post(
            "/admin/languages",
            data={
                "csrf_token": _csrf(admin),
                "languages": ["en"],
                "default_language": "ru",
            },
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "Choose the default language" in response.text

    def test_a_missing_csrf_token_is_forbidden(self, admin):
        response = admin.post(
            "/admin/languages",
            data={"languages": ["ru"], "default_language": "ru"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert asyncio.run(_stored())["bot_settings.languages"] == "en,ru"

    def test_the_save_needs_a_session(self, client):
        response = client.post(
            "/admin/languages",
            data={"languages": ["ru"], "default_language": "ru"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
