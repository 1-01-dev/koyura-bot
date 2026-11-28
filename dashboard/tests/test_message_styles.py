"""The four preset message styles: the files, the apply, and the Default.

The styles are data (``bot/template_styles/*.yaml``) plus one route
(``POST /admin/templates/apply-style``). This module pins both ends:

* the files are *complete and valid* — every kind the catalogue knows, both
  languages, only offered placeholders, inside Telegram's limit;
* applying one writes the active rows and the ``templates.style`` setting;
* ``Default`` clears every override so the bot renders its built-in texts;
* a body that breaks a rule is *skipped and named*, never silently dropped;
* the route refuses to run without a session or without the CSRF token.

The "applied N of 56" wording is asserted as behaviour, not as a string: the
engine's ``plan`` and its notice are exercised together, because a summary that
counts the wrong number is the failure mode that matters.

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "message_styles.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import message_catalog  # noqa: E402
import message_styles  # noqa: E402
import message_templates  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()

REPO_ROOT = Path(__file__).resolve().parents[2]
STYLES_DIR = REPO_ROOT / "bot" / "template_styles"
ADMIN_TEMPLATE = REPO_ROOT / "dashboard" / "templates" / "_admin_templates.html"

#: The catalogue minus the eight ``track.*`` kinds the owner's styles predate.
#: Those fall back to their built-in text; see ``test_the_style_covers...``.
STYLE_KINDS = 56
CATALOGUE_KINDS = len(message_catalog.KINDS)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(db.init_db())


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """A clean template table and settings row around every test."""
    monkeypatch.setattr(auth, "login_rate_limiter", auth.LoginRateLimiter())

    async def clean() -> None:
        await db.clear_message_templates()
        await db.save_settings({message_styles.SETTING_KEY: message_styles.DEFAULT_STYLE})

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


def _apply(client: TestClient, style: str, *, kind: str = "welcome", lang: str = "en",
           csrf: str | None = None):
    return client.post(
        "/admin/templates/apply-style",
        data={
            "csrf_token": _csrf(client) if csrf is None else csrf,
            "style": style,
            "kind": kind,
            "lang": lang,
        },
        follow_redirects=False,
    )


async def _rows() -> list[dict]:
    return await db.list_message_templates()


async def _stored_style() -> str | None:
    stored = await db.get_settings(prefix=message_styles.SETTING_KEY)
    return stored.get(message_styles.SETTING_KEY)


def _dump_style(path: Path, bodies: dict[str, dict[str, str]]) -> None:
    """Write ``{kind: {lang: body}}`` in the file's category-grouped shape.

    Used by the tests that need one *broken* body: the shipped files must stay
    untouched, so a copy is rebuilt and pointed at with
    ``TEMPLATE_STYLES_PATH``.
    """
    import yaml

    payload: dict[str, dict[str, object]] = {}
    for kind_id, by_language in bodies.items():
        kind = message_catalog.get(kind_id)
        category = kind.category if kind is not None else "buttons"
        entry: dict[str, object] = {
            "media": kind.media if kind is not None else "text",
            "placeholders": list(kind.placeholders) if kind is not None else [],
            **by_language,
        }
        payload.setdefault(category, {})[kind_id] = entry
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The files in the repository
# ---------------------------------------------------------------------------

class TestTheStyleFiles:
    def test_all_four_are_committed(self):
        for preset in message_styles.STYLES:
            assert (STYLES_DIR / preset.filename).is_file(), preset.filename
        assert message_styles.styles_dir() == STYLES_DIR.resolve()

    def test_they_are_listed_in_the_order_the_panel_shows_them(self):
        assert [preset.id for preset in message_styles.STYLES] == [
            "01-quiet-premium",
            "02-terminal",
            "03-media-studio",
            "04-friendly",
        ]
        assert [preset.label for preset in message_styles.STYLES] == [
            "Quiet premium", "Terminal", "Media studio", "Friendly",
        ]

    @pytest.mark.parametrize("preset", message_styles.STYLES, ids=lambda p: p.id)
    def test_every_style_imports_56_kinds(self, preset):
        """All four carry the same 56 kinds — the set the owner wrote."""
        bodies = message_styles.read(preset.id)
        assert len(bodies) == STYLE_KINDS
        for by_language in bodies.values():
            assert set(by_language) == set(message_catalog.LANGUAGES)
            for body in by_language.values():
                assert body.strip()

    @pytest.mark.parametrize("preset", message_styles.STYLES, ids=lambda p: p.id)
    def test_no_body_uses_an_unknown_placeholder(self, preset):
        """A placeholder the kind does not offer would be sent literally."""
        bodies = message_styles.read(preset.id)
        for kind_id, by_language in bodies.items():
            kind = message_catalog.BY_ID[kind_id]
            for language, body in by_language.items():
                unknown = message_templates.unknown_placeholders(
                    body, kind.placeholders
                )
                assert not unknown, f"{preset.id} {kind_id}/{language}: {unknown}"

    @pytest.mark.parametrize("preset", message_styles.STYLES, ids=lambda p: p.id)
    def test_no_body_is_over_the_telegram_limit(self, preset):
        """The 1024 caption / 4096 text limit is hard: Telegram rejects, not cuts."""
        bodies = message_styles.read(preset.id)
        for kind_id, by_language in bodies.items():
            kind = message_catalog.BY_ID[kind_id]
            for language, body in by_language.items():
                assert len(body) <= kind.limit, (
                    f"{preset.id} {kind_id}/{language}: "
                    f"{len(body)} > {kind.limit}"
                )

    @pytest.mark.parametrize("preset", message_styles.STYLES, ids=lambda p: p.id)
    def test_a_style_validates_without_a_single_skip(self, preset):
        plan = message_styles.plan(preset.id)
        assert plan.skipped == []
        assert plan.unknown == []
        assert plan.applied == STYLE_KINDS

    def test_the_only_kinds_left_out_are_the_newer_track_ones(self):
        """A style predating a kind leaves that one message on the default.

        Pinned rather than tolerated: if a *later* kind goes missing from all
        four files the test above (56 kinds) fails first, and this list tells the
        next person which eight the files do not cover.
        """
        covered = set(message_styles.read("04-friendly"))
        missing = set(message_catalog.BY_ID) - covered
        assert all(kind.startswith("track.") for kind in missing), sorted(missing)
        assert len(missing) == CATALOGUE_KINDS - STYLE_KINDS

    def test_a_missing_setting_means_default_not_custom(self):
        assert message_styles.normalise_style(None) == message_styles.DEFAULT_STYLE
        assert message_styles.normalise_style("") == message_styles.DEFAULT_STYLE
        assert message_styles.normalise_style("nonsense") == message_styles.DEFAULT_STYLE
        assert message_styles.normalise_style("02-terminal") == "02-terminal"
        assert message_styles.normalise_style("custom") == "custom"


# ---------------------------------------------------------------------------
# Applying a style
# ---------------------------------------------------------------------------

class TestApply:
    def test_applying_friendly_writes_both_languages_for_every_kind(self, admin):
        response = _apply(admin, "04-friendly")
        assert response.status_code == 303

        rows = asyncio.run(_rows())
        assert len(rows) == STYLE_KINDS * len(message_catalog.LANGUAGES)
        assert all(row["is_active"] for row in rows), "every written row is active"
        assert {row["kind"] for row in rows} == set(message_styles.read("04-friendly"))

    def test_it_records_which_style_is_active(self, admin):
        _apply(admin, "02-terminal")
        assert asyncio.run(_stored_style()) == "02-terminal"

    def test_the_bot_settings_api_serves_the_new_bodies(self, admin):
        written = message_styles.read("04-friendly")
        _apply(admin, "04-friendly")

        templates = admin.get("/api/bot-settings").json()["templates"]
        assert templates["welcome"]["en"] == written["welcome"]["en"]
        assert templates["welcome"]["ru"] == written["welcome"]["ru"]
        assert len(templates) == STYLE_KINDS

    def test_applying_a_second_style_replaces_the_first(self, admin):
        _apply(admin, "04-friendly")
        first = admin.get("/api/bot-settings").json()["templates"]
        _apply(admin, "02-terminal")
        second = admin.get("/api/bot-settings").json()["templates"]

        terminal = message_styles.read("02-terminal")
        assert second["welcome"]["en"] == terminal["welcome"]["en"]
        assert second["welcome"]["en"] != first["welcome"]["en"]
        # One active body per pair: the unique index would have rejected a
        # second one, and the count proves nothing stacked up.
        rows = asyncio.run(_rows())
        assert len(rows) == len(second) * len(message_catalog.LANGUAGES)

    def test_the_notice_says_applied_n_of_56(self, admin):
        response = _apply(admin, "04-friendly")
        location = response.headers["location"]
        assert "templates_style_notice=" in location
        notice = message_styles.plan("04-friendly")
        text = message_styles.apply_notice(notice)
        assert f"Applied {STYLE_KINDS} of {CATALOGUE_KINDS}" in text
        assert "Friendly" in text

    def test_the_panel_shows_the_new_active_style(self, admin):
        _apply(admin, "04-friendly")
        body = admin.get("/admin/bot").text
        assert 'class="templates-style is-active"' in body
        assert 'data-style="04-friendly"' in body
        # The active label is next to the row, in the operator's words.
        assert "активен: Friendly" in body

    def test_the_preview_uses_the_style_text(self, admin):
        written = message_styles.read("04-friendly")
        _apply(admin, "04-friendly")

        page = admin.get("/admin/bot?kind=welcome&lang=en").text
        rendered = message_templates.preview(
            "welcome", "en", written["welcome"]["en"]
        )
        # The preview elides the HTML, so compare on a distinctive sentence.
        first_line = str(rendered["text"]).splitlines()[0]
        assert first_line[:40] in page

    def test_it_redirects_back_to_the_same_kind_and_language(self, admin):
        response = _apply(admin, "01-quiet-premium", kind="progress", lang="ru")
        location = response.headers["location"]
        assert "kind=progress" in location
        assert "lang=ru" in location


# ---------------------------------------------------------------------------
# Default clears everything
# ---------------------------------------------------------------------------

class TestDefault:
    def test_default_deletes_every_override(self, admin):
        _apply(admin, "03-media-studio")
        assert asyncio.run(_rows())

        response = _apply(admin, "default")
        assert response.status_code == 303
        assert asyncio.run(_rows()) == []

    def test_default_puts_the_style_setting_back(self, admin):
        _apply(admin, "03-media-studio")
        _apply(admin, "default")
        assert asyncio.run(_stored_style()) == "default"

    def test_the_bot_gets_the_factory_texts_again(self, admin):
        _apply(admin, "04-friendly")
        _apply(admin, "default")
        assert admin.get("/api/bot-settings").json()["templates"] == {}

    def test_the_notice_says_what_was_cleared(self, admin):
        _apply(admin, "01-quiet-premium")
        response = _apply(admin, "default")
        assert "templates_style_notice=" in response.headers["location"]
        text = message_styles.default_notice(cleared=STYLE_KINDS * 2)
        assert "Cleared" in text

    def test_default_on_a_clean_panel_says_there_was_nothing_to_do(self, admin):
        response = _apply(admin, "default")
        assert response.status_code == 303
        assert "nothing to clear" in message_styles.default_notice(cleared=0)


# ---------------------------------------------------------------------------
# Skipping an invalid body — never silent
# ---------------------------------------------------------------------------

class TestSkips:
    def _write_style(self, tmp_path, monkeypatch, *, welcome_en: str):
        """A one-off style directory with a single deliberately broken body.

        The file is *rebuilt* from the imported bodies rather than edited as
        text: the YAML stores a body as a folded scalar, so the string the
        panel reads does not appear verbatim in the source.
        """
        bodies = message_styles.read("04-friendly")
        bodies["welcome"]["en"] = welcome_en
        _dump_style(tmp_path / "04-friendly.yaml", bodies)
        monkeypatch.setenv("TEMPLATE_STYLES_PATH", str(tmp_path))

    def test_an_unknown_placeholder_is_skipped_and_named(self, admin, tmp_path, monkeypatch):
        self._write_style(
            tmp_path, monkeypatch, welcome_en="Hello @nobody, welcome!"
        )
        plan = message_styles.plan("04-friendly")
        assert len(plan.skipped) == 1
        assert plan.skipped[0].kind == "welcome"
        assert "@nobody" in plan.skipped[0].reason
        assert plan.applied == STYLE_KINDS - 1

        text = message_styles.apply_notice(plan)
        assert f"Applied {STYLE_KINDS - 1} of {CATALOGUE_KINDS}" in text
        assert "Skipped 1" in text
        assert "welcome" in text

    def test_the_skip_notice_reaches_the_panel(self, admin, tmp_path, monkeypatch):
        self._write_style(
            tmp_path, monkeypatch, welcome_en="Hello @nobody, welcome!"
        )
        response = _apply(admin, "04-friendly")
        assert response.status_code == 303
        notice = unquote(response.headers["location"])
        assert "Skipped 1" in notice
        assert "welcome" in notice
        assert "@nobody" in notice

    def test_the_skipped_kind_is_not_written(self, admin, tmp_path, monkeypatch):
        self._write_style(
            tmp_path, monkeypatch, welcome_en="Hello @nobody, welcome!"
        )
        _apply(admin, "04-friendly")

        rows = asyncio.run(_rows())
        kinds = {row["kind"] for row in rows}
        assert "welcome" not in kinds, "a refused body must not be stored"
        assert len(rows) == (STYLE_KINDS - 1) * len(message_catalog.LANGUAGES)
        # The rest of the style still applied: a partly bad file is not a
        # all-or-nothing failure.
        assert "help" in kinds

    def test_an_over_limit_body_is_skipped(self):
        kind = message_catalog.get("done")
        assert kind is not None
        reason = message_styles.validate_body("x" * (kind.limit + 1), "done")
        assert reason is not None and "over" in reason

    def test_an_empty_body_is_skipped(self):
        assert message_styles.validate_body("   ", "welcome") == "empty body"

    def test_an_unknown_kind_is_reported_separately(self, tmp_path, monkeypatch):
        preset = message_styles.STYLES[-1]
        bodies = message_styles.read(preset.id)
        # A whole extra kind the catalogue has never heard of.
        bodies["button.not_a_thing"] = {"en": "Nope", "ru": "Нет"}
        _dump_style(tmp_path / preset.filename, bodies)
        monkeypatch.setenv("TEMPLATE_STYLES_PATH", str(tmp_path))

        plan = message_styles.plan(preset.id)
        assert plan.unknown == ["button.not_a_thing"]
        assert plan.applied == STYLE_KINDS
        text = message_styles.apply_notice(plan)
        assert "button.not_a_thing" in text


# ---------------------------------------------------------------------------
# The route refuses without a session or a token
# ---------------------------------------------------------------------------

class TestRefusals:
    def test_without_a_session_it_redirects_to_the_login(self, client):
        response = _apply(client, "04-friendly")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert asyncio.run(_rows()) == []

    def test_a_wrong_csrf_token_is_refused(self, admin):
        response = admin.post(
            "/admin/templates/apply-style",
            data={
                "csrf_token": "not-the-token",
                "style": "04-friendly",
                "kind": "welcome",
                "lang": "en",
            },
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert asyncio.run(_rows()) == []

    def test_a_missing_csrf_token_is_refused(self, admin):
        response = admin.post(
            "/admin/templates/apply-style",
            data={"style": "04-friendly"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert asyncio.run(_rows()) == []

    def test_a_default_without_a_token_clears_nothing(self, admin):
        _apply(admin, "04-friendly")
        before = len(asyncio.run(_rows()))
        response = admin.post(
            "/admin/templates/apply-style",
            data={"style": "default"},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert len(asyncio.run(_rows())) == before, "a refusal must not write"

    def test_an_unknown_style_is_a_rendered_error(self, admin):
        response = _apply(admin, "99-does-not-exist")
        assert response.status_code == 400
        assert "Unknown style" in response.text
        assert asyncio.run(_rows()) == []

    def test_a_missing_file_is_a_rendered_error(self, admin, tmp_path, monkeypatch):
        monkeypatch.setenv("TEMPLATE_STYLES_PATH", str(tmp_path))
        response = _apply(admin, "04-friendly")
        assert response.status_code == 400
        assert "not installed" in response.text
        assert asyncio.run(_rows()) == []


# ---------------------------------------------------------------------------
# The panel's row of buttons
# ---------------------------------------------------------------------------

class TestThePanel:
    def test_the_row_lists_all_five_buttons(self, admin):
        body = admin.get("/admin/bot").text
        for preset in message_styles.STYLES:
            assert preset.label in body
            assert f'data-style="{preset.id}"' in body
        assert 'data-style="default"' in body
        assert "Default" in body

    def test_every_button_is_a_csrf_form(self, admin):
        body = admin.get("/admin/bot").text
        row = body.split('id="templates-styles"', 1)[1].split("</section>", 1)[0]
        assert row.count('action="/admin/templates/apply-style"') == len(
            message_styles.STYLES
        ) + 1  # the four presets plus Default
        assert row.count('name="csrf_token"') >= len(message_styles.STYLES) + 1
        assert 'method="post"' in row

    def test_default_is_marked_active_on_a_fresh_panel(self, admin):
        body = admin.get("/admin/bot").text
        assert 'data-active-style="default"' in body

    def test_no_style_button_is_lit_when_default_is_active(self, admin):
        body = admin.get("/admin/bot").text
        row = body.split('id="templates-styles"', 1)[1].split("</section>", 1)[0]
        lit = [line for line in row.splitlines() if "templates-style is-active" in line]
        # Exactly one lit button on a clean panel, and it is Default.
        assert len(lit) == 1
        assert 'data-style="default"' in row.split(
            "templates-style is-active", 1
        )[1]

    def test_the_preset_is_lit_after_it_is_applied(self, admin):
        _apply(admin, "02-terminal")
        body = admin.get("/admin/bot").text
        row = body.split('id="templates-styles"', 1)[1].split("</section>", 1)[0]
        lit = [line for line in row.splitlines() if "templates-style is-active" in line]
        assert len(lit) == 1, "exactly one style can be active"
        assert 'data-style="02-terminal"' in row.split(
            "templates-style is-active", 1
        )[1]

    def test_hand_editing_after_a_style_marks_it_custom(self, admin):
        _apply(admin, "04-friendly")
        assert asyncio.run(_stored_style()) == "04-friendly"

        # Save one body by hand: the set is no longer that preset.
        admin.post(
            "/admin/templates/save",
            data={
                "csrf_token": _csrf(admin),
                "kind": "welcome",
                "lang": "en",
                "name": "Mine",
                "body": "Hello @platforms",
            },
            follow_redirects=False,
        )
        assert asyncio.run(_stored_style()) == "custom"
        body = admin.get("/admin/bot").text
        assert 'data-active-style="custom"' in body

    def test_the_partial_is_what_renders_the_row(self):
        """The section stays one include, so the page around it is untouched."""
        partial = ADMIN_TEMPLATE.read_text()
        assert 'id="templates-styles"' in partial
        assert 'action="/admin/templates/apply-style"' in partial
        bot_page = (ADMIN_TEMPLATE.parent / "admin_bot.html").read_text()
        assert "{% include" in bot_page
