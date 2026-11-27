"""The progress caption is the ``progress`` message template.

There is no second set of switches any more: which lines the live message
carries is the body the owner edited in Admin → **Message templates**. This
module pins the four states that decide what a user actually reads:

* **no custom body** — the built-in template, i.e. the title, the stage and the
  12-cell bar (the default the panel documents);
* **a custom body** — the same values, arranged by the owner, e.g. ``@title``
  with ``@percent`` and no bar;
* **an unusable body** — empty, blank or naming a placeholder this kind does not
  offer; the built-in caption is sent instead of nothing;
* **the panel down** — no settings at all, which is the same as no custom body.

Everything here is pure: no Telegram, no engine, no event loop, no network.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_settings  # noqa: E402
import message_kinds  # noqa: E402
import message_templates  # noqa: E402
import progress_card as pc  # noqa: E402

INFO = {"title": "Artemis II to the Moon", "duration": 635, "extractor": "youtube"}

#: A mid-download payload: 62% of 32.1 MB, 3.2 MB/s, 0:42 left.
PROGRESS = {
    "percent": 62.0,
    "downloaded_bytes": 21_000_000,
    "total_bytes": 33_659_750,
    "speed": 3_355_443.2,
    "eta": 42,
}

#: The built-in body for ``progress``, as both catalogues declare it.
DEFAULT_BODY = message_kinds.template("progress", "en")


def _prime(bodies: dict[str, str]) -> None:
    """Install a panel answer whose ``progress`` bodies are ``bodies``."""
    base = bot_settings.defaults()
    bot_settings.prime(
        bot_settings.Settings(
            languages=base.languages,
            default_language=base.default_language,
            source="panel",
            templates={"progress": bodies} if bodies else {},
        )
    )


def _plain(text: str) -> str:
    """Drop MarkdownV2 backslashes so assertions read like the caption."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _caption(**kwargs) -> str:
    progress = kwargs.pop("progress", PROGRESS)
    return _plain(pc.build_progress_caption(INFO, progress, **kwargs))


# ---------------------------------------------------------------------------
# The built-in default
# ---------------------------------------------------------------------------

class TestTheBuiltInDefault:
    def test_the_default_body_is_title_stage_bar(self):
        assert DEFAULT_BODY == "*@title*\n@stage\n@bar"

    def test_the_default_caption_is_three_lines(self):
        assert _caption().splitlines() == [
            "*Artemis II to the Moon*",
            "Downloading…",
            pc.BAR_FULL * 7 + pc.BAR_EMPTY * 5,
        ]

    def test_the_bar_is_twelve_cells_and_carries_no_number(self):
        bar_line = _caption().splitlines()[-1]
        assert len(bar_line) == pc.BAR_WIDTH
        assert "%" not in bar_line

    def test_the_default_body_needs_no_custom_template(self):
        """Nothing stored is the normal state: the catalogue's body is used."""
        assert _plain(pc.build_progress_caption(INFO, PROGRESS)) == _caption()

    def test_the_stage_line_survives_with_nothing_to_measure(self):
        for stage in ("starting", "processing", "uploading"):
            caption = _caption(progress={}, stage=stage)
            assert caption.splitlines()[1] != ""
            assert pc.BAR_EMPTY not in caption, stage

    def test_no_percentage_from_the_engine_means_no_bar(self):
        caption = _caption(progress={"percent": None, "downloaded_bytes": 5_242_880})
        assert pc.BAR_EMPTY not in caption
        assert "Downloading…" in caption

    def test_the_labels_follow_the_chat_language(self):
        assert "Скачиваю…" in _caption(lang="ru")
        assert "Downloading…" in _caption(lang="en")


# ---------------------------------------------------------------------------
# A custom body
# ---------------------------------------------------------------------------

class TestCustomBody:
    def test_a_percent_without_a_bar_puts_the_number_on_its_own(self):
        """The exact case from the request: ``@title — @percent``, no bar."""
        _prime({"en": "@title — @percent"})
        caption = _caption()
        assert caption == "Artemis II to the Moon — 62%"
        assert pc.BAR_FULL not in caption
        assert pc.BAR_EMPTY not in caption

    def test_the_owner_controls_the_order_and_the_prose(self):
        _prime({"en": "@stage\n*@title*\n@bar · @speed"})
        assert _caption().splitlines() == [
            "Downloading…",
            "*Artemis II to the Moon*",
            f"{pc.BAR_FULL * 7}{pc.BAR_EMPTY * 5} · 3.2 MB/s",
        ]

    def test_a_body_only_uses_the_values_its_kind_offers(self):
        allowed = message_kinds.placeholders_for("progress")
        assert "facts" in allowed
        assert message_templates.unknown_placeholders(DEFAULT_BODY, allowed) == []

    def test_the_facts_line_joins_what_the_engine_knows(self):
        _prime({"en": "@facts"})
        caption = _caption(quality="1080p", elapsed=3)
        assert "62%" in caption
        assert "1080p" in caption
        assert "20.0 MB / 32.1 MB" in caption
        assert "3.2 MB/s" in caption
        assert "ETA 0:42" in caption
        assert "0:03 elapsed" in caption
        assert " · " in caption

    def test_the_facts_line_drops_what_is_missing(self):
        _prime({"en": "@facts"})
        assert _caption(progress={"percent": 50.0}) == "50%"

    def test_each_language_can_carry_its_own_body(self):
        _prime({"en": "@title — @percent", "ru": "@title: @stage"})
        assert _caption(lang="en") == "Artemis II to the Moon — 62%"
        assert _caption(lang="ru") == "Artemis II to the Moon: Скачиваю…"

    def test_a_language_without_a_body_falls_back_to_the_built_in_one(self):
        _prime({"ru": "@title: @stage"})
        assert _plain(_caption(lang="en")) == _plain(pc.build_progress_caption(
            INFO, PROGRESS, lang="en"
        ))

    def test_the_values_are_escaped_but_the_markup_is_kept(self):
        _prime({"en": "*@title*"})
        caption = pc.build_progress_caption({"title": "Tom & Jerry (1940)"}, PROGRESS)
        assert caption == "*Tom & Jerry \\(1940\\)*"

    def test_an_unpaired_marker_is_sent_as_literal_text(self):
        _prime({"en": "*@title"})
        caption = pc.build_progress_caption({"title": "a_b*c"}, PROGRESS)
        assert caption == "\\*a\\_b\\*c"


# ---------------------------------------------------------------------------
# A body that cannot be used
# ---------------------------------------------------------------------------

class TestUnusableBody:
    def _assert_built_in(self, caption: str) -> None:
        assert caption == _plain(pc.build_progress_caption(INFO, PROGRESS))

    def test_an_empty_body_falls_back_to_the_default(self):
        _prime({"en": ""})
        # An empty string is dropped by ``normalise_templates``, so the built-in
        # body is what renders.
        self._assert_built_in(_caption())

    def test_a_blank_body_falls_back_to_the_default(self):
        _prime({"en": "   \n\t\n"})
        self._assert_built_in(_caption())

    def test_an_unknown_placeholder_falls_back_to_the_default(self):
        """The panel refuses to save it; a hand-edited row is still safe."""
        _prime({"en": "*@title*\n@pizza"})
        self._assert_built_in(_caption())

    def test_a_body_that_renders_to_nothing_falls_back(self):
        # One line, all of its placeholders empty: nothing is left to send.
        _prime({"en": "@stage"})
        caption = _caption(progress={})
        assert caption != ""
        assert "Downloading…" in caption

    def test_an_unusable_body_never_raises(self):
        for body in ("", "  ", "@nope", "@title @title*"):
            _prime({"en": body})
            assert _caption().strip(), body

    def test_the_fallback_is_logged_at_debug_not_raised(self, caplog):
        _prime({"en": "@nope"})
        with caplog.at_level("DEBUG", logger="progress_card"):
            pc.build_progress_caption(INFO, PROGRESS)
        assert any("unusable" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# The panel down
# ---------------------------------------------------------------------------

class TestPanelDown:
    def test_no_settings_at_all_renders_the_default(self):
        bot_settings.reset_cache()
        assert _plain(pc.build_progress_caption(INFO, PROGRESS)) == _plain(
            pc.build_progress_caption(INFO, PROGRESS)
        )
        assert "Downloading…" in _caption()

    def test_a_body_for_a_retired_kind_is_ignored(self):
        base = bot_settings.defaults()
        settings = bot_settings.Settings(
            languages=base.languages,
            default_language=base.default_language,
            source="panel",
            templates={"progress": {"en": "@title — @percent"}},
        )
        assert settings.custom_template("progress", "en") == "@title — @percent"
        assert settings.custom_template("retired.kind", "en") is None

    def test_an_old_panel_body_without_templates_still_works(self):
        """An older dashboard sends ``progress`` switches and no templates."""
        settings = bot_settings.normalise_payload({
            "languages": ["en", "ru"],
            "default_language": "en",
            "progress": {"show_title": True, "show_bar": True},
        })
        assert settings.templates == {}
        bot_settings.prime(settings)
        assert "Downloading…" in _caption()
        assert pc.BAR_EMPTY in _caption()


# ---------------------------------------------------------------------------
# The panel preview and the chat agree
# ---------------------------------------------------------------------------

class TestTheCatalogueBody:
    def test_the_default_body_renders_with_sample_values(self):
        """A "duplicate" starts from the default: it must render as-is."""
        values = {
            name: f"sample {name}"
            for name in message_kinds.placeholders_for("progress")
        }
        rendered = message_templates.render_body(
            DEFAULT_BODY, values, media="caption"
        )
        assert rendered.strip()

    def test_the_default_body_names_only_values_the_bot_fills(self):
        """The body and ``progress_values`` must agree, or a line vanishes."""
        filled = set(pc.progress_values(INFO, PROGRESS))
        assert set(message_templates.placeholders(DEFAULT_BODY)) <= filled
