"""The message-template catalogue: the data the panel and the docs are built on.

Three invariants matter here, and each one is the kind of mistake a hand-edited
catalogue makes:

* every placeholder a kind offers has a **description and an example** (the owner
  asked for the reference table, so a blank row is a bug);
* every default template only uses placeholders its kind actually offers, so a
  default can always be rendered and a "duplicate" never starts broken;
* ``docs/message-templates.md`` is exactly :func:`message_catalog.document`, so
  the documentation cannot drift from the code.

The renderer itself is covered by ``test_message_templates.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import message_catalog  # noqa: E402
import message_templates  # noqa: E402

DOCS = Path(__file__).resolve().parents[2] / "docs" / "message-templates.md"


# ---------------------------------------------------------------------------
# The catalogue itself
# ---------------------------------------------------------------------------

class TestTheCatalogue:
    def test_ids_are_unique(self):
        ids = [kind.id for kind in message_catalog.KINDS]
        assert len(ids) == len(set(ids))

    def test_every_kind_has_a_title_and_a_known_category(self):
        for kind in message_catalog.KINDS:
            assert kind.title.strip(), kind.id
            assert kind.category in message_catalog.CATEGORIES, kind.id
            assert kind.media in ("text", "caption"), kind.id

    def test_every_placeholder_is_documented_with_an_example(self):
        for kind in message_catalog.KINDS:
            for placeholder in kind.placeholder_objects():
                assert placeholder.summary.strip(), f"{kind.id}: @{placeholder.name}"
                assert placeholder.example.strip(), f"{kind.id}: @{placeholder.name}"
                assert placeholder.token == f"@{placeholder.name}"

    def test_every_kind_has_defaults_in_every_language(self):
        for kind in message_catalog.KINDS:
            assert set(kind.templates) == set(message_catalog.LANGUAGES), kind.id
            for body in kind.templates.values():
                assert body.strip(), kind.id

    def test_a_default_only_uses_placeholders_its_kind_offers(self):
        for kind in message_catalog.KINDS:
            for body in kind.templates.values():
                unknown = message_templates.unknown_placeholders(
                    body, kind.placeholders
                )
                assert not unknown, f"{kind.id} uses {unknown}"

    def test_the_defaults_render_with_the_sample_values(self):
        for kind in message_catalog.KINDS:
            text = message_templates.render_body(
                kind.template("en"), kind.samples(), media=kind.media
            )
            assert text.strip(), kind.id
            assert len(text) <= kind.budget, kind.id

    def test_the_sample_values_cover_the_kind(self):
        for kind in message_catalog.KINDS:
            assert set(kind.samples()) == set(kind.placeholders), kind.id

    def test_the_limits_match_telegram(self):
        assert message_catalog.LIMITS == {"text": 4096, "caption": 1024}
        card = message_catalog.get("card")
        welcome = message_catalog.get("welcome")
        assert card is not None and card.limit == 1024
        assert welcome is not None and welcome.limit == 4096

    def test_the_catalogue_covers_the_bots_messages(self):
        """The panel claims full coverage; the count is the promise."""
        assert len(message_catalog.KINDS) >= 50
        for required in (
            "welcome", "help", "platforms", "settings", "stats",
            "card", "progress", "queued", "done", "error", "transcode.ask",
        ):
            assert message_catalog.get(required) is not None, required

    def test_categories_list_their_kinds(self):
        grouped = message_catalog.categories()
        assert {group["id"] for group in grouped} == set(message_catalog.CATEGORIES)
        listed = [kind.id for group in grouped for kind in group["kinds"]]
        assert sorted(listed) == sorted(kind.id for kind in message_catalog.KINDS)


# ---------------------------------------------------------------------------
# Talking to the panel
# ---------------------------------------------------------------------------

class TestKindsLookup:
    def test_an_unknown_kind_is_none(self):
        assert message_catalog.get("nope") is None
        assert message_catalog.samples("nope") == {}

    def test_a_language_variant_falls_back_to_the_language(self):
        kind = message_catalog.get("card")
        assert kind is not None
        assert kind.template("ru-RU") == kind.template("ru")
        assert kind.template("xx") == kind.template("en")


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------

class TestDocumentation:
    def test_the_document_exists(self):
        assert DOCS.is_file(), DOCS

    def test_the_document_is_generated_from_the_catalogue(self):
        assert DOCS.read_text() == message_catalog.document()

    def test_every_kind_and_placeholder_is_in_the_document(self):
        text = DOCS.read_text()
        for kind in message_catalog.KINDS:
            assert f"`{kind.id}`" in text, kind.id
        for name, placeholder in message_catalog.PLACEHOLDERS.items():
            assert f"`@{name}`" in text, name
            assert placeholder.summary in text, name

    def test_the_document_names_the_limits_and_the_fallbacks(self):
        text = DOCS.read_text()
        assert "1024" in text and "4096" in text
        assert "Unknown placeholder" in text or "unknown placeholder" in text


@pytest.mark.parametrize("lang", list(message_catalog.LANGUAGES))
def test_every_default_template_is_present_in_the_docs(lang):
    text = DOCS.read_text()
    for kind in message_catalog.KINDS:
        assert kind.template(lang) in text, f"{kind.id}/{lang}"
