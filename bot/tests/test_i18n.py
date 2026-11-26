"""The catalogues: every key in every language, with the same holes.

A missing translation is not a crash — ``t`` falls back to English and then to the
key — so nothing at runtime would tell the owner that a Russian user sees
``download.upload_failed``. These tests are that alarm: the two catalogues must
carry the same keys, every value must be non-empty, and the ``{placeholders}`` of
one language must match the other, or a sentence with a number in it would break
in the language nobody proofread.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import i18n  # noqa: E402
from handlers import texts  # noqa: E402

CORE_LANGUAGES = ["en", "ru"]


class TestCompleteness:
    def test_both_catalogues_exist(self):
        assert set(i18n.CATALOGS) == set(CORE_LANGUAGES)
        assert tuple(CORE_LANGUAGES) == i18n.LANGUAGES

    def test_every_key_exists_in_every_language(self):
        english = set(i18n.keys("en"))
        for language in CORE_LANGUAGES:
            missing = english - set(i18n.keys(language))
            assert not missing, f"{language} is missing: {sorted(missing)}"

    def test_no_catalogue_has_extra_keys(self):
        english = set(i18n.keys("en"))
        for language in CORE_LANGUAGES:
            extra = set(i18n.keys(language)) - english
            assert not extra, f"{language} has keys English does not: {sorted(extra)}"

    def test_no_value_is_empty(self):
        for language in CORE_LANGUAGES:
            for key in i18n.keys(language):
                assert i18n.t(key, language).strip(), f"{language}:{key} is empty"

    def test_placeholders_match_between_languages(self):
        for key in i18n.keys("en"):
            english = i18n.placeholders(key, "en")
            for language in CORE_LANGUAGES:
                assert i18n.placeholders(key, language) == english, (
                    f"{language}:{key} uses {i18n.placeholders(key, language)}, "
                    f"English uses {english}"
                )

    def test_the_two_languages_are_actually_different(self):
        """A copy-pasted catalogue would pass every test above."""
        translated = [key for key in i18n.keys("en") if i18n.t(key, "en") != i18n.t(key, "ru")]
        assert len(translated) > 60


class TestLookup:
    def test_unknown_language_falls_back_to_english(self):
        assert i18n.t("welcome.title", "de") == i18n.t("welcome.title", "en")

    def test_none_and_garbage_do_not_raise(self):
        assert i18n.t("welcome.title", None)
        assert i18n.t("welcome.title", "")
        assert i18n.t("welcome.title", object())

    def test_regional_codes_resolve_to_their_language(self):
        assert i18n.normalise("ru_RU") == "ru"
        assert i18n.normalise("en-US") == "en"

    def test_unknown_key_returns_the_key(self):
        assert i18n.t("nope.not.a.key", "en") == "nope.not.a.key"

    def test_placeholders_are_substituted(self):
        line = i18n.t("intake.too_many", "en", limit=5)
        assert line == "Only the first 5 links of a message are processed."
        assert i18n.t("intake.too_many", "ru", limit=5).startswith("Обрабатываются")

    def test_has_reports_presence(self):
        assert i18n.has("welcome.body", "ru")
        assert not i18n.has("welcome.body.not", "ru")


class TestEscaping:
    def test_escape_md_covers_every_special(self):
        assert i18n.escape_md("a.b_c*d") == "a\\.b\\_c\\*d"
        assert i18n.escape_md("[x](y)") == "\\[x\\]\\(y\\)"

    def test_escaping_leaves_cyrillic_and_emoji_alone(self):
        assert i18n.escape_md("Привет 🔍") == "Привет 🔍"

    def test_there_is_exactly_one_escaper(self):
        """``download_card`` must not grow a second, subtly different escaper."""
        import download_card

        assert download_card.escape_md is not None
        assert download_card.escape_md("a.b") == i18n.escape_md("a.b")

    def test_catalogue_values_are_plain_text(self):
        """Escaping happens once, at the send site; catalogues hold prose."""
        for key in i18n.keys("en"):
            assert "\\\\." not in i18n.t(key, "en"), key


class TestStageLabels:
    def test_the_searching_stage_is_a_magnifier_in_both_languages(self):
        assert i18n.t("stage.searching", "en") == "🔍"
        assert i18n.t("stage.searching", "ru") == "🔍"

    def test_every_stage_has_a_word(self):
        import progress_card

        for stage in ("starting", "downloading", "processing", "uploading"):
            for language in CORE_LANGUAGES:
                label = progress_card.stage_label(stage, language)
                assert label
                assert label != f"stage.{stage}"

    def test_an_unknown_stage_falls_back_to_downloading(self):
        import progress_card

        assert progress_card.stage_label("teleporting", "en") == i18n.t(
            "stage.downloading", "en"
        )


class TestTextsBuilders:
    def test_welcome_is_the_owners_wording(self):
        assert texts.welcome_text("en") == i18n.escape_md(
            "Koyura Bot — a quiet downloader.\n\n"
            "Send a video or audio link and the file arrives in this chat.\n\n"
            "Supported platforms: YouTube, TikTok, Instagram, Twitter, Reddit, "
            "and 1000+ more."
        )

    def test_help_has_bold_headings_and_catalogued_lines(self):
        english = texts.help_text("en")
        assert english.startswith("*How to use Koyura Bot:*")
        assert "*Shortcuts:*" in english
        assert "*Preferences:*" in english
        assert "*Other:*" in english

    def test_help_is_translated(self):
        russian = texts.help_text("ru")
        assert russian.startswith("*Как пользоваться Koyura Bot:*")
        assert "Быстрые команды" in russian

    def test_settings_report_shows_language_and_mode(self):
        english = texts.settings_report(
            quality="best", fmt="video", language="en", mode="audio"
        )
        assert "Language: English" in english
        assert "Next link: MP3" in english
        russian = texts.settings_report(
            quality="best", fmt="audio", language="ru", mode=None, lang="ru"
        )
        assert "Язык: Русский" in russian
        assert "Следующая ссылка: не задан" in russian

    def test_stats_report_is_translated(self):
        russian = texts.stats_report(
            uptime_seconds=3661, downloads=1, errors=2,
            cached_files=3, disk_mb=4.5, sessions=5, lang="ru",
        )
        assert "Аптайм: 1 ч 1 мин 1 с" in russian
        assert "Загрузок: 1" in russian


class TestNoStrayEnglish:
    """The bug this catches: a new line added to a handler instead of the catalogue."""

    def test_handlers_do_not_carry_user_facing_literals(self):
        import pathlib
        import re

        handlers = pathlib.Path(__file__).resolve().parent.parent / "handlers"
        suspects = []
        pattern = re.compile(r"(reply_text|edit_message_text|send_message|stage\.edit)\(\s*[f]?\"")
        for path in sorted(handlers.glob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if pattern.search(line):
                    suspects.append(f"{path.name}:{number}: {line.strip()}")
        assert suspects == [], "user-facing text belongs in i18n: " + "; ".join(suspects)


@pytest.mark.parametrize("key", ["welcome.title", "help.title", "stats.title"])
def test_headline_keys_are_translated(key):
    assert i18n.t(key, "en") != i18n.t(key, "ru")
