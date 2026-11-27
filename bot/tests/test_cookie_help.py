"""The sentence the chat shows when a service wants cookies.

The engine reports ``error_code: "cookies_required"`` and the service it
computed from the URL; these tests pin what the bot does with that, and only
that. Three rules matter:

* **no site is hard-coded.** The name comes from the payload — a new service
  needs no bot release;
* **only a privileged user gets a link**, and only when a *public* panel
  address is configured. A link to the internal compose host would fail for
  every user, which is worse than no link;
* **an absent or unreadable payload still produces a sentence**, because a
  download must never end in an empty error card.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("BOT_TOKEN", "123:test-token")

import pytest  # noqa: E402

import cookie_help  # noqa: E402
import i18n  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PRIVILEGED_USER_IDS", raising=False)
    monkeypatch.delenv("DASHBOARD_PUBLIC_URL", raising=False)
    yield


def _status(service: str = "reddit") -> dict:
    return {"status": "error", "error_code": "cookies_required", "cookies_service": service}


# ---------------------------------------------------------------------------
# Detecting the verdict
# ---------------------------------------------------------------------------

class TestDetection:
    def test_a_cookies_required_status_is_recognised(self):
        assert cookie_help.needs_cookies(_status()) is True

    def test_an_ordinary_error_is_not(self):
        assert cookie_help.needs_cookies({"status": "error", "error": "boom"}) is False

    def test_a_done_status_is_not(self):
        assert cookie_help.needs_cookies({"status": "done"}) is False

    def test_none_and_nonsense_are_not(self):
        for value in (None, "x", [], 3):
            assert cookie_help.needs_cookies(value) is False

    def test_the_service_is_read_from_the_payload(self):
        assert cookie_help.service_of(_status("instagram")) == "instagram"

    def test_a_missing_service_is_an_empty_string(self):
        assert cookie_help.service_of({"error_code": "cookies_required"}) == ""
        assert cookie_help.service_of(None) == ""


# ---------------------------------------------------------------------------
# The sentence
# ---------------------------------------------------------------------------

class TestMessage:
    def test_a_user_is_told_which_service_and_where(self):
        message = cookie_help.message(_status("reddit"))
        assert "Reddit" in message
        assert "cookies" in message.lower()
        assert "panel" in message.lower()

    def test_the_service_name_is_capitalised_for_the_sentence(self):
        assert "Reddit" in cookie_help.message(_status("reddit"))

    def test_a_domain_name_is_left_as_it_is(self):
        message = cookie_help.message(_status("1tv.ru"))
        assert "1tv.ru" in message
        assert "1tv.ru".capitalize() not in message.replace("1tv.ru", "X")

    def test_no_site_is_hard_coded(self):
        """The module names no service in *code*; the payload is the only source."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(cookie_help.__file__).read_text())
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        }
        for site in ("reddit", "youtube", "instagram", "tiktok", "twitter"):
            assert site not in literals, f"{site} is hard-coded in cookie_help.py"

    def test_any_service_name_travels_through(self):
        """Whatever the engine computed reaches the sentence unchanged."""
        for service in ("vimeo", "soundcloud", "9gag", "some-new-site"):
            message = cookie_help.message(_status(service))
            assert service.lower() in message.lower(), message

    def test_a_missing_service_still_produces_a_sentence(self):
        message = cookie_help.message({"error_code": "cookies_required"})
        assert message.strip()
        assert "cookies" in message.lower()

    def test_russian_is_translated(self):
        message = cookie_help.message(_status("reddit"), lang="ru")
        assert "Reddit" in message
        assert "cookies" in message.lower()
        assert "Storage" in message

    def test_the_generic_service_phrase_exists_in_both_languages(self):
        for lang in i18n.LANGUAGES:
            phrase = i18n.t("download.cookies_generic_service", lang)
            assert phrase.strip()
            assert "{" not in phrase, "the phrase takes no placeholder"

    def test_message_for_service_matches_the_status_form(self):
        direct = cookie_help.message_for_service("reddit")
        from_status = cookie_help.message(_status("reddit"))
        assert direct == from_status


# ---------------------------------------------------------------------------
# Privileged users and the panel link
# ---------------------------------------------------------------------------

class TestPrivileged:
    def test_nobody_is_privileged_by_default(self):
        assert cookie_help.is_privileged(12345) is False

    def test_a_listed_id_is_privileged(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "111,222, 333")
        assert cookie_help.is_privileged(222) is True
        assert cookie_help.is_privileged("333") is True

    def test_an_unlisted_id_is_not(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "111,222")
        assert cookie_help.is_privileged(999) is False

    def test_garbage_entries_are_ignored_not_fatal(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "111,not-a-number,222")
        assert cookie_help.privileged_ids() == frozenset({111, 222})
        assert cookie_help.is_privileged(111) is True

    def test_a_none_or_junk_user_id_is_not_privileged(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "111")
        assert cookie_help.is_privileged(None) is False
        assert cookie_help.is_privileged("abc") is False

    def test_no_public_url_means_no_link(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "111")
        assert cookie_help.cookies_url() == ""
        assert "http" not in cookie_help.message(_status(), user_id=111)

    def test_a_public_url_gives_a_privileged_user_a_link(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "111")
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://panel.example.org")

        message = cookie_help.message(_status("reddit"), user_id=111)
        assert "https://panel.example.org/admin/cookies" in message

    def test_an_unprivileged_user_never_gets_the_link(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://panel.example.org")
        message = cookie_help.message(_status("reddit"), user_id=999)
        assert "panel.example.org" not in message

    def test_the_internal_compose_address_is_never_linked(self, monkeypatch):
        """A link to ``dashboard:8080`` cannot work in a chat."""
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "111")
        for internal in (
            "http://dashboard:8080",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ):
            monkeypatch.setenv("DASHBOARD_PUBLIC_URL", internal)
            assert cookie_help.public_dashboard_url() == ""
            assert "http" not in cookie_help.message(_status(), user_id=111)

    def test_a_non_http_public_url_is_refused(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "ftp://panel.example.org")
        assert cookie_help.public_dashboard_url() == ""

    def test_a_trailing_slash_does_not_double_up(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://panel.example.org/")
        assert cookie_help.cookies_url() == "https://panel.example.org/admin/cookies"

    def test_the_link_points_at_the_renamed_page(self, monkeypatch):
        """The section is "Cookies" now; a link to /admin/storage still works
        but would take one redirect more than it needs to."""
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://panel.example.org")
        assert cookie_help.cookies_url().endswith("/admin/cookies")


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

class TestCatalogue:
    @pytest.mark.parametrize(
        "key",
        [
            "download.cookies_required",
            "download.cookies_required_admin",
            "download.cookies_panel_link",
            "download.cookies_configured",
            "download.cookies_generic_service",
        ],
    )
    def test_every_key_exists_in_every_language(self, key):
        for lang in i18n.LANGUAGES:
            assert i18n.has(key, lang), f"{lang} is missing {key}"
            assert i18n.t(key, lang).strip()

    def test_the_two_languages_use_the_same_placeholders(self):
        for key in (
            "download.cookies_required",
            "download.cookies_required_admin",
            "download.cookies_configured",
        ):
            english = i18n.placeholders(key, "en")
            for lang in i18n.LANGUAGES:
                assert i18n.placeholders(key, lang) == english, f"{lang}:{key}"


# ---------------------------------------------------------------------------
# A blocked address is not a spent session
# ---------------------------------------------------------------------------

def _blocked_status(service: str = "reddit") -> dict:
    return {
        "status": "error",
        "error_code": "service_blocked",
        "cookies_service": service,
        # The raw yt-dlp line the engine classified; the bot must not show it.
        "error": "ERROR: [Reddit] ... HTTP Error 403: Blocked",
    }


class TestBlockedDetection:
    def test_a_blocked_status_is_recognised(self):
        assert cookie_help.is_blocked(_blocked_status()) is True

    def test_a_cookies_required_status_is_not_blocked(self):
        assert cookie_help.is_blocked(_status()) is False

    def test_none_and_nonsense_are_not_blocked(self):
        for value in (None, "x", [], 3):
            assert cookie_help.is_blocked(value) is False

    def test_the_service_is_read_from_the_payload(self):
        assert cookie_help.service_of(_blocked_status("instagram")) == "instagram"


class TestBlockedMessage:
    def test_the_sentence_names_the_service_and_the_fix(self):
        text = cookie_help.blocked_message(_blocked_status())
        assert "Reddit" in text
        assert "proxy" in text.lower()

    def test_the_sentence_says_cookies_will_not_help(self):
        """Advice that cannot work is worse than none: say so explicitly."""
        text = cookie_help.blocked_message(_blocked_status())
        assert "cookies will not help" in text.lower()

    def test_the_raw_yt_dlp_line_is_not_shown(self):
        text = cookie_help.blocked_message(_blocked_status())
        assert "403" not in text
        assert "ERROR:" not in text

    def test_a_generic_service_still_produces_a_sentence(self):
        text = cookie_help.blocked_message({"error_code": "service_blocked"})
        assert text.strip()
        assert "proxy" in text.lower()

    def test_a_privileged_user_gets_the_proxy_link_not_the_cookies_one(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "42")
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://panel.example.org")
        text = cookie_help.blocked_message(_blocked_status(), user_id=42)
        assert "https://panel.example.org/admin/proxy" in text
        assert "/admin/cookies" not in text

    def test_everyone_else_gets_the_sentence_without_a_link(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "42")
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "https://panel.example.org")
        text = cookie_help.blocked_message(_blocked_status(), user_id=7)
        assert "panel.example.org" not in text
        assert "proxy" in text.lower()

    def test_an_internal_panel_address_is_never_linked(self, monkeypatch):
        monkeypatch.setenv("PRIVILEGED_USER_IDS", "42")
        monkeypatch.setenv("DASHBOARD_PUBLIC_URL", "http://dashboard:8080")
        text = cookie_help.blocked_message(_blocked_status(), user_id=42)
        assert "dashboard:8080" not in text

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_both_languages_have_the_sentence(self, lang):
        text = cookie_help.blocked_message(_blocked_status(), lang=lang)
        assert text.strip()
        assert "proxy" in text.lower() or "прокси" in text.lower()


class TestBlockedCatalogue:
    @pytest.mark.parametrize(
        "key",
        [
            "download.service_blocked",
            "download.service_blocked_admin",
            "download.proxy_panel_link",
        ],
    )
    def test_every_key_exists_in_every_language(self, key):
        for lang in i18n.LANGUAGES:
            assert i18n.has(key, lang), f"{lang} is missing {key}"
            assert i18n.t(key, lang).strip()

    def test_the_two_languages_use_the_same_placeholders(self):
        for key in ("download.service_blocked", "download.service_blocked_admin"):
            english = i18n.placeholders(key, "en")
            for lang in i18n.LANGUAGES:
                assert i18n.placeholders(key, lang) == english, f"{lang}:{key}"
