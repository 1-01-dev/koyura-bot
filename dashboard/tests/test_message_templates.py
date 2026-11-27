"""The panel's renderer, validator and preview.

The bot has its own copy of the renderer (the services cannot import each
other), so this module pins the behaviour that must match: values escaped,
balanced markup kept, an unpaired marker sent as plain text, an empty-looking
line removed, an unknown placeholder refused. The cross-service contract test
renders the same bodies through both implementations.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import message_catalog  # noqa: E402
import message_templates as templates  # noqa: E402


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRendering:
    def test_a_value_is_escaped_and_the_markup_kept(self):
        assert (
            templates.render_body("*@title*", {"title": "Tom & Jerry (1940)"})
            == "*Tom & Jerry \\(1940\\)*"
        )

    def test_an_unpaired_marker_goes_plain(self):
        assert templates.render_body("*broken", {}) == "\\*broken"

    def test_a_line_of_empty_values_disappears(self):
        assert (
            templates.render_body("*@title*\nDuration: @duration", {
                "title": "Clip", "duration": "",
            })
            == "*Clip*"
        )

    def test_an_empty_body_is_refused(self):
        with pytest.raises(templates.TemplateError):
            templates.render_body("  ", {})

    def test_an_unknown_placeholder_is_refused(self):
        with pytest.raises(templates.TemplateError) as error:
            templates.render_body("@nope", {"title": ""})
        assert "@nope" in str(error.value)

    def test_a_caption_is_cut_under_the_limit(self):
        text = templates.render_body("@body", {"body": "x" * 4000}, media="caption")
        assert len(text) <= message_catalog.BUDGETS["caption"]
        assert text.endswith("…")

    def test_the_plain_renderer_escapes_nothing(self):
        assert templates.render_plain("@title", {"title": "a.b"}) == "a.b"

    def test_escape_matches_the_bot_character_set(self):
        assert templates.escape_md("a.b-c") == "a\\.b\\-c"
        assert templates.escape_md("plain") == "plain"


class TestPlaceholders:
    def test_they_are_listed_once_in_first_use_order(self):
        assert templates.placeholders("@b @a @b") == ("b", "a")

    def test_unknown_ones_are_named(self):
        assert templates.unknown_placeholders("@a @b", ("a",)) == ["b"]

    def test_an_at_sign_inside_a_word_is_not_a_placeholder(self):
        assert templates.placeholders("user@example.com") == ()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_a_good_body_passes(self):
        assert templates.validate("@title — @views", "card", name="Mine") == []

    def test_an_unknown_placeholder_is_an_error_that_lists_the_offered_ones(self):
        errors = templates.validate("@nope", "card")
        assert errors and "@nope" in errors[0]
        assert "@title" in errors[0]

    def test_an_empty_body_is_an_error(self):
        assert templates.validate("   ", "card", name="Mine")

    def test_a_missing_name_is_an_error(self):
        assert templates.validate("@title", "card", name="  ")

    def test_an_unknown_kind_is_an_error(self):
        assert templates.validate("@title", "nope")

    def test_a_missing_placeholder_is_not_an_error(self):
        """A custom body may ignore any of the offered placeholders."""
        assert templates.validate("Fixed text", "card", name="Mine") == []


class TestWarnings:
    def test_a_short_body_has_no_warning(self):
        assert templates.warnings("@title", "card") == []

    def test_a_body_over_the_telegram_limit_warns(self):
        warnings = templates.warnings("x" * 1500, "card")
        assert warnings and "1024" in warnings[0]

    def test_a_body_over_the_renderer_budget_warns_earlier(self):
        warnings = templates.warnings("x" * 1010, "card")
        assert warnings and "1024" in warnings[0]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

class TestPreview:
    def test_the_default_renders_with_sample_values(self):
        preview = templates.preview("card", "ru", None)
        assert preview["error"] is None
        assert "Как приготовить пасту карбонара" in str(preview["text"])
        assert preview["limit"] == 1024
        assert preview["over"] is False

    def test_a_custom_body_renders(self):
        preview = templates.preview(
            "card", "en", "@title — @views просмотров, @source"
        )
        assert "просмотров" in str(preview["text"])

    def test_a_broken_body_returns_the_error_not_a_render(self):
        preview = templates.preview("card", "en", "@nope")
        assert preview["text"] == ""
        assert "@nope" in str(preview["error"])

    def test_an_unknown_kind_returns_an_error(self):
        preview = templates.preview("nope", "en", "@title")
        assert preview["error"] and preview["limit"] == 0


# ---------------------------------------------------------------------------
# Stored rows
# ---------------------------------------------------------------------------

class TestStoredRows:
    def test_the_api_payload_keeps_only_active_rows_of_known_kinds(self):
        rows = [
            {"kind": "card", "lang": "en", "body": "@title", "is_active": 1},
            {"kind": "card", "lang": "ru", "body": "@title", "is_active": 0},
            {"kind": "gone", "lang": "en", "body": "@title", "is_active": 1},
            {"kind": "done", "lang": "en", "body": "  ", "is_active": 1},
        ]
        payload = templates.active_bodies(rows)
        assert payload == {"card": {"en": "@title"}}

    def test_coverage_counts_distinct_kinds(self):
        rows = [
            {"kind": "card", "lang": "en", "is_active": 1},
            {"kind": "card", "lang": "ru", "is_active": 1},
            {"kind": "done", "lang": "en", "is_active": 1},
            {"kind": "done", "lang": "ru", "is_active": 0},
        ]
        coverage = templates.coverage(rows)
        assert coverage["covered"] == 2
        assert coverage["total"] == len(message_catalog.KINDS)

    def test_a_language_variant_is_normalised(self):
        assert templates.normalise_lang("RU") == "ru"
        assert templates.normalise_lang("ru-RU") == "ru"
        assert templates.normalise_lang("xx") == "en"

    def test_a_row_is_read_tolerantly(self):
        template = templates.Template.from_row({"id": "7", "kind": "card"})
        assert template.id == 7
        assert template.lang == ""
        assert template.active is False
