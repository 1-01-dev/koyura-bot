"""Custom message templates: the bot's renderer and its fallbacks.

The panel is only half of the feature; this module pins the half that must not
break a chat:

* substitution escapes the **value** and keeps the owner's markup;
* a template that cannot be MarkdownV2 is sent as literal text, never rejected;
* an unknown placeholder, an empty body or a body that renders to nothing falls
  back to the built-in default;
* the cache is the panel's answer, so a panel that is down is a normal state;
* every kind in the registry renders its default in both languages, inside the
  Telegram limit of its media type.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_settings  # noqa: E402
import message_kinds  # noqa: E402
import message_templates as templates  # noqa: E402
from handlers import keyboards, texts  # noqa: E402


def custom(kind: str, lang: str, body: str) -> None:
    """Install one custom body the way a panel answer would."""
    bot_settings.prime(
        bot_settings.Settings(
            languages=("en", "ru"),
            default_language="en",
            source="panel",
            templates={kind: {lang: body}},
        )
    )


# ---------------------------------------------------------------------------
# Substitution and escaping
# ---------------------------------------------------------------------------

class TestSubstitution:
    def test_a_value_is_escaped_and_the_prose_is_not(self):
        rendered = templates.render_body(
            "*@title* — @views просмотров", {"title": "Tom & Jerry", "views": "1.3K"}
        )
        assert rendered == "*Tom & Jerry* — 1\\.3K просмотров"

    def test_a_missing_value_becomes_an_empty_string(self):
        assert (
            templates.render_body("[@title] @other", {"title": None, "other": "x"})
            == "\\[\\] x"
        )

    def test_a_line_of_nothing_but_a_missing_value_disappears(self):
        with pytest.raises(templates.TemplateError):
            templates.render_body("[@title]", {"title": None})

    def test_a_line_whose_placeholders_are_all_empty_disappears(self):
        rendered = templates.render_body(
            "*@title*\nDuration: @duration\n@views views",
            {"title": "Clip", "duration": "", "views": None},
        )
        assert rendered == "*Clip*"

    def test_balanced_markup_is_kept(self):
        rendered = templates.render_body(
            "*bold* _italic_ ~strike~ ||spoiler|| `code`", {}
        )
        assert rendered == "*bold* _italic_ ~strike~ ||spoiler|| `code`"

    def test_the_escaped_marker_is_literal(self):
        assert templates.render_body(r"\*not bold\*", {}) == "\\*not bold\\*"

    def test_an_unpaired_marker_goes_out_as_plain_text(self):
        rendered = templates.render_body("*broken @title", {"title": "Clip"})
        assert rendered == "\\*broken Clip"

    def test_an_unknown_placeholder_is_an_error(self):
        with pytest.raises(templates.TemplateError) as error:
            templates.render_body("@nope", {"title": "Clip"})
        assert "@nope" in str(error.value)

    def test_an_empty_body_is_an_error(self):
        with pytest.raises(templates.TemplateError):
            templates.render_body("   \n  ", {})

    def test_a_body_that_renders_to_nothing_is_an_error(self):
        with pytest.raises(templates.TemplateError):
            templates.render_body("@only", {"only": ""})

    def test_a_caption_is_cut_under_telegrams_limit(self):
        rendered = templates.render_body("@body", {"body": "x" * 4000}, media="caption")
        assert len(rendered) <= templates.BUDGETS["caption"]
        assert rendered.endswith("…")

    def test_a_text_is_cut_under_telegrams_limit(self):
        rendered = templates.render_body("@body", {"body": "x" * 5000}, media="text")
        assert len(rendered) <= templates.BUDGETS["text"]

    def test_plain_rendering_does_not_escape(self):
        assert templates.render_plain("@title", {"title": "a.b"}) == "a.b"


# ---------------------------------------------------------------------------
# Where the body comes from
# ---------------------------------------------------------------------------

class TestThePanelAnswer:
    def test_without_a_custom_body_the_default_is_returned(self):
        assert templates.render("card", "en", {}, "DEFAULT") == "DEFAULT"

    def test_the_custom_body_wins_when_it_renders(self):
        custom("card", "en", "@title — @views")
        assert (
            templates.render(
                "card", "en", {"title": "Clip", "views": "12"}, "DEFAULT"
            )
            == "Clip — 12"
        )

    def test_the_language_is_part_of_the_lookup(self):
        custom("card", "ru", "@title")
        assert templates.render("card", "en", {"title": "T"}, "DEFAULT") == "DEFAULT"
        assert templates.render("card", "ru", {"title": "T"}, "DEFAULT") == "T"

    def test_a_broken_body_falls_back_to_the_default(self):
        custom("card", "en", "@nope")
        assert templates.render("card", "en", {"title": "T"}, "DEFAULT") == "DEFAULT"

    def test_an_empty_body_is_ignored(self):
        bot_settings.prime(
            bot_settings.Settings(
                languages=("en",),
                default_language="en",
                templates={"card": {"en": "   "}},
            )
        )
        assert templates.render("card", "en", {}, "DEFAULT") == "DEFAULT"

    def test_an_unknown_language_in_the_body_is_dropped(self):
        settings = bot_settings.normalise_payload(
            {"languages": ["en"], "templates": {"card": {"xx": "@title"}}}
        )
        assert settings.templates == {"card": {"en": "@title"}}

    def test_the_payload_keeps_only_usable_bodies(self):
        settings = bot_settings.normalise_payload(
            {
                "languages": ["en", "ru"],
                "templates": {
                    "card": {"en": "@title", "ru": ""},
                    "gone": {"en": "@title"},
                },
            }
        )
        assert settings.templates == {"card": {"en": "@title"}}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

class TestTheRegistry:
    def test_ids_are_unique(self):
        ids = [kind.id for kind in message_kinds.KINDS]
        assert len(ids) == len(set(ids))

    def test_every_kind_has_defaults_in_every_language(self):
        for kind in message_kinds.KINDS:
            assert set(kind.templates) == set(message_kinds.LANGUAGES), kind.id
            for body in kind.templates.values():
                assert body.strip(), kind.id

    def test_a_default_only_uses_the_placeholders_it_offers(self):
        for kind in message_kinds.KINDS:
            for body in kind.templates.values():
                unknown = templates.unknown_placeholders(body, kind.placeholders)
                assert not unknown, f"{kind.id} uses {unknown}"

    def test_every_kind_renders_with_sample_values(self):
        for kind in message_kinds.KINDS:
            values = {
                name: f"sample {name}" for name in kind.placeholders
            }
            rendered = templates.render_body(
                kind.template("en"), values, media=kind.media
            )
            assert rendered.strip(), kind.id

    def test_every_i18n_key_exists_in_every_language(self):
        import i18n

        for kind in message_kinds.KINDS:
            for key in kind.i18n_keys:
                for lang in message_kinds.LANGUAGES:
                    assert i18n.has(key, lang), f"{kind.id}: {key} is missing in {lang}"

    def test_media_is_one_of_the_two_telegram_kinds(self):
        for kind in message_kinds.KINDS:
            assert kind.media in ("text", "caption"), kind.id

    def test_an_unknown_kind_has_no_template(self):
        assert message_kinds.template("nope", "en") is None
        assert message_kinds.placeholders_for("nope") == ()


# ---------------------------------------------------------------------------
# The wired call sites
# ---------------------------------------------------------------------------

class TestTheWiredMessages:
    def test_a_custom_one_liner_replaces_the_catalogued_line(self):
        custom("download.already", "en", "Hold on, please")
        assert texts.md("download.already", "en") == "Hold on, please"

    def test_a_custom_welcome_replaces_the_screen(self):
        custom("welcome", "en", "Welcome!\n\n@platforms")
        rendered = texts.welcome_text("en")
        assert rendered.startswith("Welcome\\!")
        assert "YouTube" in rendered

    def test_a_custom_card_replaces_the_caption(self):
        custom("card", "en", "@title — @views просмотров, @source")
        import download_card

        caption = download_card.build_card(
            {"title": "Clip", "view_count": 1300}, lang="en", url="https://youtu.be/x"
        )
        assert caption == "Clip — 1\\.3K просмотров, youtu\\.be"

    def test_the_default_card_is_unchanged_without_a_custom_body(self):
        import download_card

        caption = download_card.build_card({"title": "Clip"}, lang="en")
        assert caption.startswith("*Clip*")

    def test_buttons_can_be_renamed(self):
        custom("nav.back", "en", "Return")
        button = keyboards.back("en").inline_keyboard[0][0]
        assert button.text == "Return"

    def test_a_custom_progress_caption_is_used(self):
        custom("progress", "en", "@percent of @title")
        import progress_card

        caption = progress_card.build_progress_caption(
            {"title": "Clip"}, {"percent": 62}, lang="en"
        )
        assert caption == "62% of Clip"

    def test_a_custom_busy_sentence_is_used(self):
        custom("download.busy_service", "en", "Busy — retry in @when")
        import progress_card

        assert (
            progress_card.busy_message("overloaded", seconds=120, lang="en")
            == "Busy — retry in ~2 min"
        )

    def test_a_custom_plain_status_line_is_used(self):
        custom("download.timed_out", "en", "Too slow: @title")
        assert texts.plain("download.timed_out", "en", title="Clip") == "Too slow: Clip"

    def test_the_panel_down_renders_the_defaults(self):
        bot_settings.reset_cache()  # nothing cached: the panel never answered
        assert texts.md("download.already", "en") == texts.escape_md(
            "Already downloading — wait for the file."
        )
