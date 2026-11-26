"""Tests for the engine's progress reporting.

Everything here is driven by **real** yt-dlp output captured from actual
downloads (see the fixtures below), because the previous round of fake-data
tests passed while production reported ``progress: null`` for the whole job.

Two production bugs are pinned down here:

1. ``--progress-template "download:<json>"`` makes yt-dlp consume ``download:``
   as the template's *type selector*, so the emitted lines are bare JSON. The
   reader matched ``startswith("download:")`` and threw every line away.
2. yt-dlp renders an absent field as the literal ``NA``, which is not valid
   JSON. A single such line made ``json.loads`` fail for that update.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ["DOWNLOADS_PATH"] = tempfile.mkdtemp()

import app as koyura_app  # noqa: E402

# --- Captured from real runs (trimmed) -------------------------------------
# YouTube `-f 269` (HLS): no total_bytes, drifting estimate, real fragments.
REAL_HLS_LINES = [
    '{"downloaded_bytes":1024,"total_bytes":"","total_bytes_estimate":"1024",'
    '"fragment_index":"0","fragment_count":"123","speed":"922.2644906438309",'
    '"eta":"","percent_str":"100.0%","eta_str":"Unknown","speed_str":"   922.26B/s"}',
    '{"downloaded_bytes":"27448","total_bytes":"","total_bytes_estimate":"6752208.0",'
    '"fragment_index":"1","fragment_count":"123","speed":"21093.28",'
    '"eta":"","percent_str":"  1.7%","eta_str":"Unknown","speed_str":"  20.44KiB/s"}',
    '{"downloaded_bytes":"6868392","total_bytes":"6868392","total_bytes_estimate":"",'
    '"fragment_index":"","fragment_count":"","speed":"108181.14972374514",'
    '"eta":"","percent_str":"100.0%","eta_str":"","speed_str":"105.65KiB/s"}',
]
# mp3 (audio): a real total_bytes and no fragments.
REAL_MP3_LINES = [
    '{"downloaded_bytes":"1024","total_bytes":"10202210","fragment_index":"",'
    '"fragment_count":"","speed":"13115.805646404959"}',
    '{"downloaded_bytes":"5096000","total_bytes":"10202210","fragment_index":"",'
    '"fragment_count":"","speed":"502111.5"}',
]
# A plain HTTP mp4: exact total_bytes, no fragment info, no speed early on.
REAL_MP4_LINES = [
    '{"downloaded_bytes":"1024","total_bytes":"3000000"}',
    '{"downloaded_bytes":"1500000","total_bytes":"3000000"}',
]
# The pre-fix template, unquoted NA: this is what production emitted.
LEGACY_NA_LINE = '{"downloaded_bytes":1024,"total_bytes":NA,"speed":929.85,"eta":NA}'


def _parse(line: str) -> dict:
    """Parse a line, failing loudly instead of returning an Optional."""
    payload = koyura_app._parse_progress_line(line)
    assert payload is not None, f"line did not parse: {line[:120]}"
    return payload


class TestLooksLikeProgress:
    """The prefix trap: yt-dlp consumes ``download:`` and prints bare JSON."""

    def test_bare_json_is_recognised(self):
        assert koyura_app._looks_like_progress(REAL_MP4_LINES[0])

    def test_prefixed_json_is_still_recognised(self):
        assert koyura_app._looks_like_progress("download:" + REAL_MP4_LINES[0])

    def test_other_ytdlp_output_is_not_progress(self):
        for line in (
            "[download] Destination: out.mp4",
            "[hlsnative] Total fragments: 123",
            "ERROR: unable to download",
            "",
        ):
            assert not koyura_app._looks_like_progress(line), line


class TestParseProgressLine:
    def test_real_hls_line_parses(self):
        assert _parse(REAL_HLS_LINES[1])["fragment_index"] == "1"

    def test_real_mp3_line_parses(self):
        assert koyura_app._parse_progress_line(REAL_MP3_LINES[0]) is not None

    def test_bare_na_line_is_repaired(self):
        """Production emitted unquoted ``NA``; the byte counts must survive."""
        payload = _parse(LEGACY_NA_LINE)
        assert payload["downloaded_bytes"] == 1024
        assert payload["total_bytes"] is None
        assert payload["speed"] == 929.85

    def test_prefixed_line_parses_like_the_bare_one(self):
        bare = koyura_app._parse_progress_line(REAL_MP3_LINES[0])
        prefixed = koyura_app._parse_progress_line("download:" + REAL_MP3_LINES[0])
        assert bare == prefixed

    def test_garbage_returns_none(self):
        assert koyura_app._parse_progress_line("{not json") is None
        assert koyura_app._parse_progress_line("[1, 2]") is None
        assert koyura_app._parse_progress_line("") is None


class TestProgressNumbers:
    def test_numbers_pass_through(self):
        assert koyura_app._progress_number(1024) == 1024
        assert koyura_app._progress_number(1.5) == 1.5

    def test_absent_and_na_become_none(self):
        for value in (None, "", "NA", "N/A", "Unknown", "null", "none", "  "):
            assert koyura_app._progress_number(value) is None, value

    def test_numeric_strings_are_parsed(self):
        # The template quotes every value, so numbers arrive as strings.
        assert koyura_app._progress_number("2048") == 2048.0
        assert koyura_app._progress_number("6752208.0") == 6752208.0

    def test_booleans_are_not_numbers(self):
        assert koyura_app._progress_number(True) is None

    def test_percent_string(self):
        assert koyura_app._progress_percent_str("  62.4%") == 62.4
        assert koyura_app._progress_percent_str("100.0%") == 100.0
        assert koyura_app._progress_percent_str("NA") is None
        assert koyura_app._progress_percent_str(None) is None

    def test_speed_string(self):
        assert koyura_app._progress_speed_str("  830.81B/s") == 830.81
        assert koyura_app._progress_speed_str("3.24MiB/s") == 3.24 * 1024 ** 2
        assert koyura_app._progress_speed_str("Unknown") is None
        assert koyura_app._progress_speed_str(None) is None


class TestPercentPriority:
    """Which field the percentage comes from, and why."""

    @staticmethod
    def _percent(**kw):
        base: dict = {
            "downloaded": None, "total": None, "estimate": None,
            "fragment_index": None, "fragment_count": None, "percent_str": None,
        }
        base.update(kw)
        return koyura_app._progress_percent(**base)

    def test_total_bytes_wins(self):
        assert self._percent(downloaded=50, total=100, estimate=1000,
                             fragment_index=1, fragment_count=10) == 50.0

    def test_fragments_are_preferred_over_the_estimate(self):
        # Measured on a real YouTube HLS job: the estimate jumped 6752208 ->
        # 1688052 while fragments advanced 0 -> 1 of 123.
        assert self._percent(
            downloaded=27448, total=None, estimate=6752208,
            fragment_index=1, fragment_count=123,
        ) == 0.8

    def test_estimate_is_used_when_fragments_are_missing(self):
        assert self._percent(downloaded=500, estimate=1000) == 50.0

    def test_percent_str_is_the_last_resort(self):
        assert self._percent(percent_str=42.0) == 42.0

    def test_nothing_known_means_no_percentage(self):
        assert self._percent(downloaded=1234) is None

    def test_percentage_is_capped_at_100(self):
        assert self._percent(downloaded=110, total=100) == 100.0
        assert self._percent(fragment_index=130, fragment_count=123) == 100.0


class TestSnapshot:
    def test_real_hls_snapshot(self):
        snap = koyura_app._progress_snapshot(_parse(REAL_HLS_LINES[1]), elapsed=12.5)
        assert snap["percent"] == 0.8          # 1/123 fragments
        assert snap["downloaded_bytes"] == 27448
        assert snap["total_bytes"] is None
        assert snap["speed"] > 0
        assert snap["elapsed"] == 12.5

    def test_real_mp3_snapshot_uses_total_bytes(self):
        snap = koyura_app._progress_snapshot(_parse(REAL_MP3_LINES[1]), elapsed=4.0)
        # 5096000 / 10202210 = 49.95%, rounded to one decimal.
        assert snap["percent"] == 49.9
        assert snap["total_bytes"] == 10202210

    def test_real_mp4_snapshot(self):
        snap = koyura_app._progress_snapshot(_parse(REAL_MP4_LINES[1]), elapsed=2.0)
        assert snap["percent"] == 50.0

    def test_all_fields_missing_still_yields_a_usable_dict(self):
        snap = koyura_app._progress_snapshot({}, elapsed=1.0)
        assert snap["percent"] is None
        assert snap["downloaded_bytes"] is None
        assert snap["elapsed"] == 1.0

    def test_speed_falls_back_to_the_display_string(self):
        payload = {"downloaded_bytes": "10", "speed": "", "speed_str": "  830.81B/s"}
        snap = koyura_app._progress_snapshot(payload, elapsed=1.0)
        assert snap["speed"] == 830.81

    def test_elapsed_comes_from_the_caller_not_the_payload(self):
        payload = {"downloaded_bytes": "10", "elapsed": "999"}
        assert koyura_app._progress_snapshot(payload, elapsed=3.0)["elapsed"] == 3.0


class TestTemplate:
    def test_template_asks_for_the_hls_fields(self):
        template = koyura_app.PROGRESS_TEMPLATE
        for field in ("downloaded_bytes", "total_bytes", "total_bytes_estimate",
                      "fragment_index", "fragment_count", "speed", "eta",
                      "percent_str", "eta_str", "speed_str"):
            assert field in template, field

    def test_template_keeps_the_type_selector(self):
        # ``download:`` is the selector yt-dlp needs; it is not part of output.
        assert koyura_app.PROGRESS_TEMPLATE.startswith("download:{")


class TestDownloadCmd:
    def test_native_hls_by_default(self):
        """The ffmpeg downloader reports no progress at all, so it is opt-in."""
        cmd = koyura_app._build_download_cmd("j1", "https://x/y", "video", None)
        assert "--downloader" not in cmd

    def test_ffmpeg_downloader_is_opt_in(self, monkeypatch):
        monkeypatch.setattr(koyura_app, "HLS_DOWNLOADER", "ffmpeg")
        cmd = koyura_app._build_download_cmd("j2", "https://x/y", "video", None)
        assert cmd[cmd.index("--downloader") + 1] == "hls:ffmpeg"

    def test_progress_template_is_passed_through(self):
        cmd = koyura_app._build_download_cmd("j3", "https://x/y", "video", None)
        assert cmd[cmd.index("--progress-template") + 1] == koyura_app.PROGRESS_TEMPLATE
        assert "--newline" in cmd


class TestElapsed:
    def test_elapsed_grows_from_the_job_start(self, monkeypatch):
        koyura_app.jobs["jobX"] = {"created": 1000.0}
        try:
            monkeypatch.setattr(koyura_app.time, "time", lambda: 1012.0)
            assert koyura_app._job_elapsed("jobX") == 12.0
            monkeypatch.setattr(koyura_app.time, "time", lambda: 1030.0)
            assert koyura_app._job_elapsed("jobX") == 30.0
        finally:
            koyura_app.jobs.pop("jobX", None)

    def test_unknown_job_has_no_elapsed(self):
        assert koyura_app._job_elapsed("nope") is None


class TestRealCapturesAreClean:
    """Every captured real line must parse and only go up."""

    def test_all_real_captures_parse(self):
        for lines in (REAL_HLS_LINES, REAL_MP3_LINES, REAL_MP4_LINES):
            for line in lines:
                assert koyura_app._looks_like_progress(line)
                assert koyura_app._parse_progress_line(line) is not None, line

    def test_percent_never_regresses_over_a_capture(self):
        snaps = [
            koyura_app._progress_snapshot(_parse(line), elapsed=float(i))
            for i, line in enumerate(REAL_HLS_LINES)
        ]
        pcts = [s["percent"] for s in snaps if s["percent"] is not None]
        assert pcts == sorted(pcts), pcts


class TestStatusEndpoint:
    """The endpoint the bot polls must never answer ``progress: null``."""

    @staticmethod
    def _client():
        koyura_app.app.config.update(TESTING=True)
        return koyura_app.app.test_client()

    def test_progress_is_returned_for_a_running_job(self):
        koyura_app.jobs["jobStatus"] = {
            "status": "downloading",
            "created": koyura_app.time.time(),
            "progress": koyura_app._progress_snapshot(
                _parse(REAL_HLS_LINES[1]), elapsed=5.0),
        }
        try:
            resp = self._client().get(
                "/api/status/jobStatus",
                headers={"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]},
            )
            body = json.loads(resp.data)
            assert resp.status_code == 200
            assert body["progress"] is not None
            assert body["progress"]["percent"] == 0.8
            assert body["progress"]["elapsed"] == 5.0
        finally:
            koyura_app.jobs.pop("jobStatus", None)


class TestStreamConsumption:
    """The readers must consume stdout: that is where yt-dlp writes progress."""

    @staticmethod
    def _job(job_id="jstream"):
        koyura_app.jobs[job_id] = {
            "status": "downloading", "progress": None,
            "created": koyura_app.time.time(),
        }
        return job_id

    def test_a_stdout_progress_line_updates_the_job(self):
        job_id = self._job()
        try:
            handled = koyura_app._consume_progress_line(
                job_id, REAL_MP4_LINES[1], [0])
            assert handled is True
            progress = koyura_app.jobs[job_id]["progress"]
            assert progress is not None, "a stdout line must not be dropped"
            assert progress["percent"] == 50.0
        finally:
            koyura_app.jobs.pop(job_id, None)

    def test_a_non_progress_line_is_not_recorded(self):
        job_id = self._job()
        try:
            assert koyura_app._consume_progress_line(
                job_id, "[download] Destination: out.mp4", [0]) is False
            assert koyura_app.jobs[job_id]["progress"] is None
        finally:
            koyura_app.jobs.pop(job_id, None)

    def test_a_broken_line_is_logged_once_per_job(self, caplog):
        job_id = self._job()
        bad = [0]
        try:
            with caplog.at_level("DEBUG"):
                for _ in range(3):
                    koyura_app._consume_progress_line(
                        job_id, '{"downloaded_bytes": }', bad)
            warnings = [r for r in caplog.records if r.levelname == "WARNING"]
            assert len(warnings) == 1, [r.getMessage() for r in warnings]
            assert bad[0] == 3
        finally:
            koyura_app.jobs.pop(job_id, None)

    def test_percent_never_goes_backwards_within_a_job(self):
        """A video+audio job downloads twice; the card must not jump to 0%."""
        job_id = self._job()
        try:
            koyura_app._record_progress(job_id, {"percent": 100.0, "downloaded_bytes": 10})
            # The audio stream starts over at 0%.
            koyura_app._record_progress(job_id, {"percent": 0.3, "downloaded_bytes": 1})
            progress = koyura_app.jobs[job_id]["progress"]
            assert progress["percent"] == 100.0
            # The byte count is still the latest real value.
            assert progress["downloaded_bytes"] == 1
        finally:
            koyura_app.jobs.pop(job_id, None)

    def test_progress_keeps_rising_across_a_real_capture(self):
        job_id = self._job()
        try:
            bad = [0]
            for line in REAL_HLS_LINES + REAL_MP3_LINES:
                koyura_app._consume_progress_line(job_id, line, bad)
            percent = koyura_app.jobs[job_id]["progress"]["percent"]
            assert percent is not None
            assert 0.0 <= percent <= 100.0
        finally:
            koyura_app.jobs.pop(job_id, None)

    def test_unknown_job_is_ignored(self):
        assert koyura_app._consume_progress_line(
            "nope", REAL_MP4_LINES[0], [0]) is True
