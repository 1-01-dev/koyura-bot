"""The conversion policy: three modes, one CPU-bounded encoder, one estimate.

Two things are pinned here because they cost the owner real money when they
drift:

* **nothing converts by default.** ``TRANSCODE_MODE`` defaults to ``off``, an
  unknown value is treated as ``off``, and ``auto`` refuses a file above
  ``TRANSCODE_MAX_MB``. A VP9/AV1 pass is ~180% CPU on a two-vCPU host, so the
  only way it may start is an explicit decision: ``TRANSCODE_MODE=always``, a
  small file in ``auto``, or the user's own *Convert* tap (``forced``).
* **one conversion, one core.** The ffmpeg vector carries ``-threads 1`` and
  ``-preset veryfast`` and the process-wide lock serialises conversions, which
  is what keeps the host under its 90% average-load limit.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media_meta
import transcode

MB = 1024 * 1024


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------

class TestCodecFamilies:
    @pytest.mark.parametrize(
        "vcodec,family",
        [
            ("avc1.640028", "h264"),
            ("h264", "h264"),
            ("av01.0.08M.0", "av1"),
            ("av1", "av1"),
            ("vp09.00.40.0", "vp9"),
            ("vp9", "vp9"),
            ("vp8", "vp8"),
            ("hev1.1.6.L93.B0", "hevc"),
        ],
    )
    def test_family_from_a_ytdlp_name(self, vcodec, family):
        assert transcode.codec_family(vcodec) == family

    def test_unknown_and_missing(self):
        assert transcode.codec_family(None) is None
        assert transcode.codec_family("") is None
        assert transcode.codec_family("none") is None
        assert transcode.codec_family("theora") is None

    def test_labels_are_the_short_names_a_button_carries(self):
        assert transcode.codec_label("av01.0.08M.0") == "AV1"
        assert transcode.codec_label("vp09.00.40.0") == "VP9"
        assert transcode.codec_label("avc1.640028") == "H.264"
        assert transcode.codec_label("theora") is None

    def test_h264_is_the_only_guaranteed_codec(self):
        assert transcode.is_compatible("avc1.640028")
        assert not transcode.is_compatible("vp09.00.40.0")
        assert not transcode.is_compatible("av01.0.08M.0")
        # HEVC plays on Android and desktop but not on iOS: same trap as VP9.
        assert not transcode.is_compatible("hev1.1.6.L93.B0")

    def test_an_unknown_codec_is_treated_as_compatible(self):
        """The engine must not burn CPU on a guess."""
        assert transcode.is_compatible("")
        assert transcode.is_compatible("theora")

    def test_only_vp8_vp9_av1_are_worth_converting(self):
        assert transcode.needs_conversion("vp9")
        assert transcode.needs_conversion("av1")
        assert transcode.needs_conversion("vp8")
        assert not transcode.needs_conversion("avc1.640028")
        assert not transcode.needs_conversion("hev1.1.6.L93.B0")


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

class TestModes:
    def test_default_mode_is_off(self, monkeypatch):
        monkeypatch.delenv("TRANSCODE_MODE", raising=False)
        assert transcode.mode_from_env() == "off"

    @pytest.mark.parametrize("raw", ["OFF", " off ", "nonsense", "", None])
    def test_an_unknown_mode_never_converts(self, raw):
        """A typo must fall back to the safe mode, not to converting."""
        assert transcode.normalize_mode(raw) == "off"

    @pytest.mark.parametrize("raw", ["auto", "always", "off"])
    def test_known_modes_survive(self, raw):
        assert transcode.normalize_mode(raw) == raw

    def test_max_mb_default_and_garbage(self, monkeypatch):
        monkeypatch.delenv("TRANSCODE_MAX_MB", raising=False)
        assert transcode.max_mb_from_env() == transcode.DEFAULT_MAX_MB == 300
        monkeypatch.setenv("TRANSCODE_MAX_MB", "120")
        assert transcode.max_mb_from_env() == 120
        for broken in ("", "abc", "0", "-5"):
            monkeypatch.setenv("TRANSCODE_MAX_MB", broken)
            assert transcode.max_mb_from_env() == transcode.DEFAULT_MAX_MB

    def test_off_never_converts_whatever_the_size(self):
        for size in (None, 0, 10 * MB, 5000 * MB):
            assert not transcode.should_convert("vp9", mode="off", size_bytes=size)
            assert not transcode.should_convert("av1", mode="off")

    def test_always_converts_every_incompatible_codec(self):
        assert transcode.should_convert("vp9", mode="always", size_bytes=5000 * MB)
        assert transcode.should_convert("av1", mode="always")
        # ... and leaves a playable file alone.
        assert not transcode.should_convert("avc1.640028", mode="always")

    def test_auto_converts_only_inside_the_size_ceiling(self):
        assert transcode.should_convert(
            "vp9", mode="auto", size_bytes=299 * MB, max_mb=300
        )
        assert transcode.should_convert(
            "vp9", mode="auto", size_bytes=300 * MB, max_mb=300
        )
        assert not transcode.should_convert(
            "vp9", mode="auto", size_bytes=301 * MB, max_mb=300
        )
        assert not transcode.should_convert(
            "av1", mode="auto", size_bytes=400 * MB, max_mb=300
        )

    def test_auto_without_a_size_refuses_to_convert(self):
        """No size means no evidence that the pass is cheap: send it as is."""
        assert not transcode.should_convert("vp9", mode="auto", size_bytes=None)
        assert not transcode.should_convert("vp9", mode="auto", size_bytes=0)

    def test_the_users_own_tap_overrides_off(self):
        """Explicit consent: the *Convert* button works even with mode=off."""
        assert transcode.should_convert("vp9", mode="off", forced=True)
        assert transcode.should_convert("av1", mode="off", forced=True)
        assert not transcode.should_convert("avc1.640028", mode="off", forced=True)

    def test_force_does_not_convert_an_already_playable_file(self):
        assert not transcode.should_convert("avc1.640028", mode="always", forced=True)

    @pytest.mark.parametrize(
        "kwargs,reason",
        [
            ({"mode": "off", "size_bytes": 100 * MB}, "mode_off"),
            ({"mode": "auto", "size_bytes": 900 * MB, "max_mb": 300}, "too_big"),
            ({"mode": "auto", "size_bytes": 100 * MB, "max_mb": 300}, None),
            ({"mode": "off", "forced": True}, None),
        ],
    )
    def test_reason_explains_a_document_delivery(self, kwargs, reason):
        assert transcode.conversion_reason("vp9", **kwargs) == reason

    def test_reason_for_a_playable_file(self):
        assert transcode.conversion_reason("avc1.640028", mode="off") == "compatible"


# ---------------------------------------------------------------------------
# The ffmpeg invocation and its progress
# ---------------------------------------------------------------------------

class TestFfmpeg:
    def test_the_command_is_cpu_bounded(self):
        cmd = transcode.ffmpeg_command("in.mp4", "out.mp4")
        assert cmd[0] == "ffmpeg"
        assert cmd[cmd.index("-threads") + 1] == "1"
        assert cmd[cmd.index("-preset") + 1] == "veryfast"
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        # AAC audio and a faststart moov atom: what Telegram needs to stream it.
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert "+faststart" in cmd
        # Machine-readable progress instead of a scraped human line.
        assert cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert "-nostats" in cmd
        assert cmd[-1] == "out.mp4"

    def test_one_encoder_thread_is_a_constant_not_a_prayer(self):
        assert transcode.VIDEO_THREADS == 1
        # Two threads would let one conversion use both vCPUs of the shared
        # host, which is exactly what the 90% load limit forbids.
        assert transcode.VIDEO_THREADS < 2

    def test_progress_lines_are_parsed(self):
        assert transcode.parse_progress_line("out_time_us=1500000") == {
            "out_time_us": 1_500_000
        }
        # Older ffmpeg reports out_time_ms, which is microseconds too.
        assert transcode.parse_progress_line("out_time_ms=2500000") == {
            "out_time_us": 2_500_000
        }

    def test_noise_is_ignored(self):
        for line in ("", "progress=continue", "out_time_us=", "out_time_us=N/A",
                     "out_time_us=-5", "frame=12"):
            assert transcode.parse_progress_line(line) is None

    def test_percent_from_position_and_duration(self):
        assert transcode.percent_from(0, 60) is None
        assert transcode.percent_from(30_000_000, 60) == 50.0
        assert transcode.percent_from(60_000_000, 60) == 100.0
        # A clip that runs slightly past its probed duration stays at 100.
        assert transcode.percent_from(61_000_000, 60) == 100.0
        assert transcode.percent_from(None, 60) is None
        assert transcode.percent_from(1_000_000, None) is None


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------

class TestEstimate:
    def test_fallback_formula_from_the_size(self):
        """~8 MB per minute until something has been measured."""
        stats = transcode.TranscodeStats(None)
        seconds, approximate = stats.estimate(size_bytes=16 * MB)
        assert seconds == pytest.approx(120.0, abs=1.0)
        assert approximate is True

    def test_fallback_half_realtime_when_only_the_duration_is_known(self):
        stats = transcode.TranscodeStats(None)
        seconds, approximate = stats.estimate(duration=600)
        assert seconds == pytest.approx(300.0, abs=1.0)
        assert approximate is True

    def test_measuring_beats_guessing(self):
        stats = transcode.TranscodeStats(None)
        # 600 s of video converted in 300 s -> 0.5 s of wall clock per second.
        stats.record(300.0, 600)
        seconds, approximate = stats.estimate(duration=600)
        assert seconds == pytest.approx(300.0, abs=1.0)
        # One sample is still "approximate"; the counter says why.
        assert approximate is True
        stats.record(300.0, 600)
        seconds, approximate = stats.estimate(duration=600)
        assert approximate is False

    def test_the_ewma_moves_towards_the_new_measurement(self):
        stats = transcode.TranscodeStats(None)
        stats.record(600.0, 600)   # 1.0x
        stats.record(60.0, 600)    # 0.1x, alfa 0.3 -> 0.73x
        assert stats.snapshot()["seconds_per_video_second"] == pytest.approx(0.73)

    def test_nonsense_samples_are_skipped(self):
        stats = transcode.TranscodeStats(None)
        stats.record(0, 600)
        stats.record(-5, 600)
        stats.record(300, 0)
        stats.record(300, None)
        assert stats.snapshot()["samples"] == 0

    def test_the_estimate_is_clamped_to_a_usable_range(self):
        stats = transcode.TranscodeStats(None)
        stats.record(1.0, 10_000)  # absurdly fast
        seconds, _ = stats.estimate(duration=10_000)
        assert seconds <= transcode.ESTIMATE_MAX_SECONDS
        assert transcode.clamp_estimate(0.5) == transcode.ESTIMATE_MIN_SECONDS

    def test_the_ewma_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "transcode.json")
        first = transcode.TranscodeStats(path)
        first.record(180.0, 600)
        again = transcode.TranscodeStats(path)
        assert again.snapshot()["samples"] == 1
        assert again.snapshot()["seconds_per_video_second"] == pytest.approx(0.3)

    def test_a_broken_file_is_ignored(self, tmp_path):
        path = tmp_path / "transcode.json"
        path.write_text("{not json")
        stats = transcode.TranscodeStats(str(path))
        assert stats.snapshot()["samples"] == 0
        assert stats.estimate(duration=60)[0] > 0


# ---------------------------------------------------------------------------
# /api/info: the codec facts the bot's keyboard is built from
# ---------------------------------------------------------------------------

TRANSPARENT_INFO = {
    "title": "Clip",
    "duration": 300,
    "formats": [
        {"format_id": "399", "ext": "mp4", "vcodec": "av01.0.08M.0", "height": 2160,
         "filesize": 2_400 * MB},
        {"format_id": "271", "ext": "mp4", "vcodec": "vp09.00.40.0", "height": 1440,
         "filesize": 451 * MB},
        {"format_id": "299", "ext": "mp4", "vcodec": "avc1.64002a", "height": 1080,
         "filesize": 220 * MB},
    ],
    "audio": {"format_id": "140", "abr": 129, "filesize": 4 * MB},
}


class TestInfoPayloadCodecFacts:
    def test_every_format_carries_its_codec_verdict(self):
        payload = media_meta.build_info_payload(TRANSPARENT_INFO)
        by_height = {fmt["height"]: fmt for fmt in payload["formats"]}
        assert by_height[2160]["codec"] == "av1"
        assert by_height[2160]["codec_label"] == "AV1"
        assert by_height[2160]["compatible"] is False
        assert by_height[2160]["convertible"] is True
        assert by_height[1440]["codec_label"] == "VP9"
        assert by_height[1080]["compatible"] is True
        assert by_height[1080]["convertible"] is False

    def test_h264_wins_over_vp9_at_the_same_height(self):
        info = {
            "formats": [
                {"format_id": "vp9", "ext": "mp4", "vcodec": "vp09.00.40.0", "height": 1080,
                 "filesize": 90 * MB},
                {"format_id": "avc", "ext": "mp4", "vcodec": "avc1.640028", "height": 1080,
                 "filesize": 120 * MB},
            ]
        }
        formats = media_meta.collect_formats(info)
        assert [f["id"] for f in formats] == ["avc"]
        assert formats[0]["label"] == "1080p"

    def test_incompatible_formats_are_labelled_not_hidden(self):
        payload = media_meta.build_info_payload(TRANSPARENT_INFO)
        heights = [fmt["height"] for fmt in payload["formats"]]
        assert heights == [2160, 1440, 1080]
        assert payload["formats"][0]["button_label"] == "2160p · 2.3GB"

    def test_only_convertible_formats_get_an_estimate(self):
        stats = transcode.TranscodeStats(None)
        payload = media_meta.build_info_payload(
            TRANSPARENT_INFO, estimate=stats.estimate
        )
        by_height = {fmt["height"]: fmt for fmt in payload["formats"]}
        # AV1 2160p: 2400 MB / 8 MB-per-minute = 300 min, clamped.
        assert by_height[2160]["transcode_estimate_seconds"] == int(
            transcode.ESTIMATE_MAX_SECONDS
        )
        assert by_height[1440]["transcode_estimate_seconds"] > 0
        assert by_height[1080]["transcode_estimate_seconds"] is None
        assert by_height[1080]["transcode_estimate_approximate"] is None

    def test_the_estimate_follows_a_measured_sample(self):
        stats = transcode.TranscodeStats(None)
        stats.record(150.0, 300)  # 0.5x of a 300 s clip
        stats.record(150.0, 300)
        payload = media_meta.build_info_payload(
            TRANSPARENT_INFO, estimate=stats.estimate
        )
        by_height = {fmt["height"]: fmt for fmt in payload["formats"]}
        assert by_height[1440]["transcode_estimate_seconds"] == pytest.approx(150, abs=2)
        assert by_height[1440]["transcode_estimate_approximate"] is False

    def test_the_policy_travels_with_the_payload(self):
        payload = media_meta.build_info_payload(
            TRANSPARENT_INFO, transcode_info={"mode": "off", "max_mb": 300}
        )
        assert payload["transcode"] == {"mode": "off", "max_mb": 300}

    def test_an_older_payload_shape_is_unchanged(self):
        """Additive only: the historical keys keep their types."""
        payload = media_meta.build_info_payload(TRANSPARENT_INFO)
        for fmt in payload["formats"]:
            assert {"id", "label", "height", "size_bytes", "button_label"} <= set(fmt)
            assert isinstance(fmt["id"], str)
        assert payload["transcode"] == {}
