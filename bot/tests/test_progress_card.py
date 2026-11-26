"""Tests for the progress/error captions and the edit throttle.

Everything here is pure: no Telegram, no engine, no event loop. The captions are
what the user reads, so the format is asserted literally.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import progress_card as pc

INFO = {
    "title": "Artemis II to the Moon",
    "duration": 635,
    "channel": "NASA",
    "extractor": "youtube",
}


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


def _progress(**extra):
    base = {
        "percent": 62.0,
        "downloaded_bytes": 21_000_000,
        "total_bytes": 33_659_750,
        "speed": 3_355_443.2,
        "eta": 42,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_bar_has_fixed_width_and_percent(self):
        bar = pc.progress_bar(62)
        cells = bar.split(" ")[0]
        assert len(cells) == pc.BAR_WIDTH
        assert bar.endswith("62%")
        assert cells.count(pc.BAR_FULL) == 7
        assert cells.count(pc.BAR_EMPTY) == 5

    def test_bar_edges(self):
        assert pc.progress_bar(0).split(" ")[0] == pc.BAR_EMPTY * 12
        assert pc.progress_bar(100).split(" ")[0] == pc.BAR_FULL * 12
        assert pc.progress_bar(None) == ""
        # Out-of-range values are clamped, never overflow the bar.
        assert pc.progress_bar(-5).split(" ")[0] == pc.BAR_EMPTY * 12
        assert pc.progress_bar(140).split(" ")[0] == pc.BAR_FULL * 12

    def test_bar_width_is_configurable(self):
        assert len(pc.progress_bar(50, width=8).split(" ")[0]) == 8

    def test_speed(self):
        assert pc.format_speed(3_355_443.2) == "3.2 MB/s"
        assert pc.format_speed(1024) == "1.0 KB/s"
        assert pc.format_speed(None) is None
        assert pc.format_speed("NA") is None
        assert pc.format_speed(0) is None

    def test_size(self):
        assert pc.format_size(13_002_342) == "12.4 MB"
        assert pc.format_size(786_432) == "768 KB"
        assert pc.format_size(1_288_490_188) == "1.2 GB"
        assert pc.format_size(None) is None
        assert pc.format_size("NA") is None
        assert pc.format_size(0) is None

    def test_eta_shape(self):
        assert pc.format_eta(42) == "0:42"
        assert pc.format_eta(725) == "12:05"
        assert pc.format_eta(3723) == "1:02:03"
        assert pc.format_eta(0) == "0:00"
        assert pc.format_eta(None) is None
        assert pc.format_eta(-1) is None

    def test_nan_and_garbage_are_ignored(self):
        assert pc.as_number(float("nan")) is None
        assert pc.as_number(float("inf")) is None
        assert pc.as_number("not a number") is None
        assert pc.as_number(True) is None


# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------

class TestDerived:
    def test_eta_comes_from_the_engine_when_present(self):
        assert pc.derive_eta(_progress(eta=42)) == 42

    def test_eta_is_computed_from_speed_and_remaining_bytes(self):
        eta = pc.derive_eta({
            "eta": None,
            "speed": 1_000_000,
            "downloaded_bytes": 1_000_000,
            "total_bytes": 11_000_000,
        })
        assert eta == 10.0

    def test_eta_is_none_without_speed(self):
        assert pc.derive_eta({
            "eta": None, "speed": None,
            "downloaded_bytes": 1, "total_bytes": 10,
        }) is None

    def test_eta_falls_back_to_the_estimate(self):
        eta = pc.derive_eta({
            "eta": None,
            "speed": 1_000,
            "downloaded_bytes": 1_000,
            "total_bytes": None,
            "total_bytes_estimate": 5_000,
        })
        assert eta == 4.0

    def test_percent_computed_from_bytes(self):
        assert pc.derive_percent({
            "percent": None, "downloaded_bytes": 50, "total_bytes": 100,
        }) == 50.0

    def test_percent_is_none_without_a_total(self):
        assert pc.derive_percent({"percent": None, "downloaded_bytes": 50}) is None

    def test_koyura_percent_wins(self):
        assert pc.derive_percent({"percent": 12.5, "downloaded_bytes": 1,
                                  "total_bytes": 100}) == 12.5


# ---------------------------------------------------------------------------
# Progress caption
# ---------------------------------------------------------------------------

class TestProgressCaption:
    def test_downloading_caption_has_bar_percent_speed_size_eta(self):
        caption = _plain(pc.build_progress_caption(INFO, _progress(), elapsed=3))
        assert "Artemis II to the Moon" in caption
        assert "▓" in caption and "░" in caption
        assert "62%" in caption
        assert "3.2 MB/s" in caption
        assert "20.0 MB / 32.1 MB" in caption
        assert "ETA 0:42" in caption
        assert "0:03 elapsed" in caption

    def test_bar_is_always_the_same_length(self):
        for percent in (0, 3, 25, 50, 71, 99, 100):
            caption = _plain(pc.build_progress_caption(
                INFO, _progress(percent=percent, eta=1)))
            bar_line = next(
                line for line in caption.splitlines()
                if pc.BAR_FULL in line or pc.BAR_EMPTY in line
            )
            cells = bar_line.split(" ")[0]
            assert len(cells) == pc.BAR_WIDTH, (percent, bar_line)

    def test_no_total_size_means_no_invented_percent(self):
        caption = _plain(pc.build_progress_caption(
            INFO,
            {"percent": None, "downloaded_bytes": 13_002_342,
             "total_bytes": None, "speed": 1_000_000, "eta": None},
        ))
        assert "%" not in caption.replace("100%", "")
        assert "12.4 MB" in caption
        assert "977 KB/s" in caption
        assert "░" not in caption  # no bar without a percentage

    def test_downloaded_only_caption_still_shows_progress_data(self):
        caption = _plain(pc.build_progress_caption(
            INFO, {"percent": None, "downloaded_bytes": 5_242_880,
                   "speed": None, "eta": None},
        ))
        assert "5.0 MB" in caption
        assert "Downloading" in caption

    def test_no_progress_at_all_shows_the_stage(self):
        caption = _plain(pc.build_progress_caption(INFO, {}))
        assert "Downloading…" in caption
        assert "%" not in caption

    def test_processing_stage_has_no_eta(self):
        caption = _plain(pc.build_progress_caption(
            INFO, _progress(percent=100, eta=0), stage="processing"))
        assert "Processing…" in caption
        assert "ETA" not in caption
        assert "100%" in caption

    def test_quality_is_shown_when_known(self):
        caption = _plain(pc.build_progress_caption(
            INFO, _progress(), quality="1080p"))
        assert "1080p" in caption

    def test_caption_fits_the_telegram_limit(self):
        info = {
            "title": "X" * 2000,
            "channel": "C" * 500,
            "duration": 10,
        }
        caption = pc.build_progress_caption(info, _progress(), elapsed=1)
        assert len(caption) <= 1024

    def test_caption_is_markdown_escaped(self):
        caption = pc.build_progress_caption(
            {"title": "a_b*c"}, _progress())
        assert r"a\_b\*c" in caption

    def test_na_values_from_the_engine_are_ignored(self):
        # yt-dlp used to emit the literal string "NA" for absent fields.
        caption = _plain(pc.build_progress_caption(
            INFO,
            {"percent": None, "downloaded_bytes": 1_000,
             "total_bytes": "NA", "speed": "NA", "eta": "NA"},
        ))
        assert "NA" not in caption
        assert "%" not in caption


# ---------------------------------------------------------------------------
# Error caption
# ---------------------------------------------------------------------------

class TestErrorCaption:
    def test_error_shows_the_reason_and_a_hint(self):
        caption = _plain(pc.build_error_caption(INFO, "Video unavailable"))
        assert "Artemis II to the Moon" in caption
        assert "Download failed" in caption
        assert "Video unavailable" in caption
        assert "Tap a format to try again." in caption

    def test_error_keeps_only_the_first_line(self):
        caption = _plain(pc.build_error_caption(
            INFO, "ERROR: boom\nTraceback (most recent call last):\n  ..."))
        assert "ERROR: boom" in caption
        assert "Traceback" not in caption

    def test_long_error_is_truncated(self):
        caption = _plain(pc.build_error_caption(INFO, "x" * 1000))
        assert len(caption) <= 1024
        assert "…" in caption

    def test_empty_error_has_a_placeholder(self):
        assert "Unknown error" in _plain(pc.build_error_caption(INFO, None))

    def test_hint_can_be_suppressed(self):
        caption = _plain(pc.build_error_caption(INFO, "boom", retry_hint=False))
        assert "Tap a format" not in caption


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

class TestThrottle:
    def test_first_update_always_goes_through(self):
        throttle = pc.ProgressThrottle()
        assert throttle.should_update(0, 100, now=0.0)

    def test_second_update_after_one_second_is_dropped(self):
        throttle = pc.ProgressThrottle(min_interval=3.0)
        assert throttle.should_update(10, 100, now=100.0)
        assert not throttle.should_update(50, 50, now=101.0)
        assert not throttle.should_update(90, 10, now=102.9)

    def test_update_after_the_interval_and_a_delta_goes_through(self):
        throttle = pc.ProgressThrottle(min_interval=3.0, min_percent_delta=1.5)
        assert throttle.should_update(10, 100, now=100.0)
        assert throttle.should_update(15, 90, now=103.5)

    def test_small_percentage_change_is_dropped(self):
        throttle = pc.ProgressThrottle(min_interval=3.0, min_percent_delta=1.5)
        assert throttle.should_update(10, 100, now=100.0)
        assert not throttle.should_update(11, 99, now=110.0)

    def test_noticeable_eta_drop_counts_even_without_a_percent_jump(self):
        throttle = pc.ProgressThrottle(
            min_interval=3.0, min_percent_delta=5.0, min_eta_drop=5.0)
        assert throttle.should_update(10, 60, now=100.0)
        assert throttle.should_update(10.5, 40, now=110.0)

    def test_tiny_eta_drop_does_not_count(self):
        throttle = pc.ProgressThrottle(
            min_interval=3.0, min_percent_delta=5.0, min_eta_drop=5.0)
        assert throttle.should_update(10, 60, now=100.0)
        assert not throttle.should_update(10.5, 59, now=110.0)

    def test_unknown_percent_does_not_stall_the_caption(self):
        # No percentage at all (site without a size): still show the bytes.
        throttle = pc.ProgressThrottle(min_interval=3.0)
        assert throttle.should_update(None, None, now=0.0)
        assert throttle.should_update(None, None, now=4.0)

    def test_force_records_an_edit(self):
        throttle = pc.ProgressThrottle(min_interval=3.0)
        throttle.force(10, 100, now=50.0)
        assert not throttle.should_update(50, 10, now=51.0)
        assert throttle.should_update(60, 5, now=54.0)


# ---------------------------------------------------------------------------
# Real engine payloads (as produced by the engine after the parsing fix)
# ---------------------------------------------------------------------------

class TestRealEngineShapes:
    """Shapes observed from the engine on real downloads."""

    def test_hls_job_shows_percent_without_a_drifting_denominator(self):
        # YouTube HLS: no total_bytes, only an estimate that drifts 340MB->504MB.
        # The percentage is real (fragment-based); the denominator must not be
        # printed, or the caption jumps backwards between edits.
        caption = _plain(pc.build_progress_caption(INFO, {
            "percent": 37.4,
            "downloaded_bytes": 127_795_200,
            "total_bytes": None,
            "total_bytes_estimate": 504_346_284,
            "speed": 1_048_576,
            "eta": None,
            "elapsed": 61.0,
        }, elapsed=61.0))

        assert "37%" in caption
        assert "▓" in caption
        assert "122 MB" in caption            # downloaded, shown alone
        assert "481" not in caption           # the drifting estimate is hidden
        # No "X / Y" pair: only the downloaded amount is shown (MB/s is fine).
        assert "MB /" not in caption and "B /" not in caption

    def test_audio_job_uses_the_exact_total(self):
        caption = _plain(pc.build_progress_caption(INFO, {
            "percent": 49.0,
            "downloaded_bytes": 5_096_000,
            "total_bytes": 10_202_210,
            "speed": 512_000,
            "eta": 10,
        }, elapsed=4.0))
        assert "49%" in caption
        assert "4.9 MB / 9.7 MB" in caption

    def test_non_fragment_progress_without_any_total(self):
        # A source that reports neither size nor fragments: no invented percent.
        caption = _plain(pc.build_progress_caption(INFO, {
            "percent": None,
            "downloaded_bytes": 13_002_342,
            "total_bytes": None,
            "total_bytes_estimate": None,
            "speed": 1_258_291,
            "eta": None,
        }, elapsed=9.0))
        assert "%" not in caption
        assert "░" not in caption
        assert "12.4 MB" in caption
        assert "1.2 MB/s" in caption
        assert "0:09 elapsed" in caption

    def test_koyura_elapsed_is_used_when_present(self):
        caption = _plain(pc.build_progress_caption(INFO, {
            "percent": 50.0, "downloaded_bytes": 500, "total_bytes": 1000,
        }, elapsed=125.0))
        assert "2:05 elapsed" in caption
