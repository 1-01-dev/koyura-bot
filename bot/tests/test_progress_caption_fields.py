"""What each progress switch draws, as the admin panel's checkboxes shape it.

One test per flag, both languages, the default (title + bar), the empty
configuration (the stage line, so the message is never blank) and an unknown
size. The switch set itself and the panel plumbing are covered by
``test_progress_fields.py``.

Everything here is pure: no Telegram, no engine, no event loop.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import progress_card as pc  # noqa: E402
import progress_fields  # noqa: E402

INFO = {"title": "Artemis II to the Moon", "duration": 635, "extractor": "youtube"}

OFF = dict.fromkeys(progress_fields.FIELD_NAMES, False)


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


def _fields(**overrides) -> progress_fields.Fields:
    """A field set from the defaults with the given switches overridden."""
    values = dict(progress_fields.DEFAULTS)
    values.update(overrides)
    return progress_fields.Fields(values=values)


def _only(name: str) -> progress_fields.Fields:
    """Just one switch on — the per-flag table in the report."""
    return progress_fields.Fields(values={**OFF, name: True})


def _progress(**extra) -> dict:
    """A mid-download payload: half of 30.8 MB, 58.9 MB/s, 42 s left."""
    base = {
        "percent": 62.0,
        "downloaded_bytes": 21_000_000,
        "total_bytes": 33_659_750,
        "speed": 3_355_443.2,
        "eta": 42,
    }
    base.update(extra)
    return base


def _caption(fields=None, progress=None, **kwargs) -> str:
    return _plain(pc.build_progress_caption(
        INFO,
        _progress() if progress is None else progress,
        fields=fields,
        **kwargs,
    ))


# ---------------------------------------------------------------------------
# What each switch draws
# ---------------------------------------------------------------------------

class TestDefaultCaption:
    def test_the_default_is_the_title_and_the_bar(self):
        caption = _caption()
        assert "Artemis II to the Moon" in caption
        assert pc.BAR_FULL in caption and pc.BAR_EMPTY in caption
        # Nothing else: the owner's complaint was exactly these lines.
        assert "%" not in caption
        assert "MB" not in caption
        assert "MB/s" not in caption
        assert "ETA" not in caption
        assert "elapsed" not in caption
        assert "Downloading" not in caption

    def test_the_default_caption_is_two_lines(self):
        assert len(_caption().splitlines()) == 2

    def test_the_bar_keeps_12_cells_without_the_percent(self):
        for percent in (0, 3, 25, 62, 99, 100):
            caption = _caption(progress=_progress(percent=percent))
            bar_line = next(
                line for line in caption.splitlines()
                if pc.BAR_FULL in line or pc.BAR_EMPTY in line
            )
            assert len(bar_line) == pc.BAR_WIDTH, (percent, bar_line)

    def test_no_percent_sign_anywhere_in_the_default(self):
        assert "%" not in _caption()


class TestEachFlagAlone:
    """One switch on, everything else off: exactly one line must appear."""

    def test_title_only(self):
        # A title and no bar/percentage: the stage line comes along, because a
        # lone title does not say that anything is happening.
        assert _caption(_only("show_title")).splitlines() == [
            "*Artemis II to the Moon*",   # the title line is bold MarkdownV2
            "Downloading…",
        ]

    def test_bar_only(self):
        caption = _caption(_only("show_bar"))
        assert caption.strip(pc.BAR_FULL + pc.BAR_EMPTY) == ""
        assert "%" not in caption

    def test_percent_only(self):
        # No bar to sit next to, so the percentage becomes its own fact.
        caption = _caption(_only("show_percent"))
        assert "62%" in caption

    def test_percent_with_the_bar(self):
        caption = _caption(_fields(show_percent=True))
        assert "▓▓▓▓▓▓▓░░░░░ 62%" in caption

    def test_size_only(self):
        caption = _caption(_only("show_size"))
        assert "20.0 MB / 32.1 MB" in caption

    def test_speed_only(self):
        assert "3.2 MB/s" in _caption(_only("show_speed"))

    def test_eta_only(self):
        assert "ETA 0:42" in _caption(_only("show_eta"))

    def test_elapsed_only(self):
        assert "0:03 elapsed" in _caption(_only("show_elapsed"), elapsed=3)

    def test_quality_only(self):
        assert "1080p" in _caption(_only("show_quality"), quality="1080p")

    def test_a_lone_fact_keeps_the_stage_line(self):
        # Facts alone would not say what is happening; the stage line stays.
        caption = _caption(_only("show_speed"))
        assert "Downloading…" in caption

    def test_the_facts_share_one_line(self):
        caption = _caption(_fields(
            show_percent=False, show_speed=True, show_eta=True, show_size=True))
        facts = caption.splitlines()[-1]
        assert " · " in facts
        assert "20.0 MB / 32.1 MB" in facts
        assert "3.2 MB/s" in facts
        assert "ETA 0:42" in facts


class TestEverythingOff:
    def test_the_minimum_is_the_stage_line(self):
        caption = _caption(progress_fields.Fields(values=OFF))
        assert caption == "Downloading…"

    def test_the_minimum_is_never_empty(self):
        for stage in ("starting", "downloading", "processing", "uploading"):
            caption = _caption(
                progress_fields.Fields(values=OFF),
                progress=_progress(percent=50),
                stage=stage,
            )
            assert caption.strip(), stage

    def test_the_minimum_is_localised(self):
        caption = _caption(progress_fields.Fields(values=OFF), lang="ru")
        assert caption == "Скачиваю…"

    def test_the_minimum_is_still_markdown_safe(self):
        caption = pc.build_progress_caption(
            INFO, _progress(), fields=progress_fields.Fields(values=OFF))
        assert "…" in caption


class TestUnknownSize:
    """No exact total: the percentage/size/ETA lines simply do not appear."""

    HLS = {
        "percent": 37.4,
        "downloaded_bytes": 127_795_200,
        "total_bytes": None,
        "total_bytes_estimate": 504_346_284,
        "speed": 1_048_576,
        "eta": None,
    }

    def test_size_is_not_drawn_without_an_exact_total(self):
        caption = _caption(_only("show_size"), progress=self.HLS)
        assert "122 MB" in caption          # downloaded alone is still useful
        assert " / " not in caption         # but never a made-up denominator

    def test_the_drifting_estimate_is_never_shown(self):
        caption = _caption(_only("show_size"), progress=self.HLS)
        assert "481" not in caption and "504" not in caption

    def test_eta_is_not_drawn_when_nothing_can_be_estimated(self):
        caption = _caption(
            _only("show_eta"),
            progress={"percent": 37.4, "downloaded_bytes": 127_795_200,
                      "speed": None, "eta": None},
        )
        assert "ETA" not in caption

    def test_eta_may_come_from_the_estimate(self):
        # An ETA is a guess anyway, so the drifting estimate is fine to divide
        # by (unlike the size, which would be printed as a fact).
        caption = _caption(_only("show_eta"), progress=self.HLS)
        assert "ETA" in caption

    def test_percent_is_not_drawn_without_a_source_number(self):
        caption = _caption(
            _fields(show_percent=True),
            progress={"percent": None, "downloaded_bytes": 5_242_880},
        )
        assert "%" not in caption
        assert "░" not in caption

    def test_size_without_any_bytes_is_not_drawn(self):
        caption = _caption(_only("show_size"), progress={"percent": 50.0})
        assert "MB" not in caption


class TestBothLanguages:
    @pytest.mark.parametrize("lang,eta,elapsed", [
        ("en", "ETA 0:42", "0:03 elapsed"),
        ("ru", "осталось 0:42", "0:03 прошло"),
    ])
    def test_the_labels_follow_the_chat_language(self, lang, eta, elapsed):
        caption = _caption(_fields(show_eta=True, show_elapsed=True),
                           elapsed=3, lang=lang)
        assert eta in caption
        assert elapsed in caption

    @pytest.mark.parametrize("lang,stage_line", [
        ("en", "Downloading…"), ("ru", "Скачиваю…"),
    ])
    def test_the_default_needs_no_translation_of_its_own(self, lang, stage_line):
        # The default draws no words at all, in either language.
        caption = _caption(lang=lang)
        assert stage_line not in caption
        assert "Artemis II to the Moon" in caption

    def test_the_stage_line_appears_in_the_right_language_with_everything_off(self):
        assert "Downloading…" in _caption(progress_fields.Fields(values=OFF))
        assert "Скачиваю…" in _caption(
            progress_fields.Fields(values=OFF), lang="ru")
