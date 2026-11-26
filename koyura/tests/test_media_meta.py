"""Tests for the engine's /api/info shaping helpers (``koyura/media_meta.py``)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media_meta


# A trimmed but *real* yt-dlp blob, captured by hand rather than invented, so
# the shaping helpers are tested against the shape YouTube actually returns:
# storyboards, video-only and audio-only streams, several codecs per height,
# muxed duplicates and one format whose size yt-dlp does not know.
#
# Source: https://www.youtube.com/watch?v=Ke6XX8FHOHM — "Artemis II to the Moon:
# Launch to Splashdown (NASA Mission Animation)" by NASA. NASA footage is in the
# public domain (a work of the U.S. federal government, 17 U.S.C. § 105), so the
# metadata below can live in this repository. Captured 2026-11-27 with
# ``yt-dlp -J``; nothing here is fetched at test time.
YTDLP_INFO = {
    "title": "Artemis II to the Moon: Launch to Splashdown (NASA Mission Animation)",
    "thumbnail": "https://i.ytimg.com/vi/Ke6XX8FHOHM/maxresdefault.jpg",
    "duration": 465,
    "uploader": "NASA",
    "uploader_id": "@NASA",
    "channel": "NASA",
    "channel_follower_count": 15100000,
    "view_count": 2927499,
    "like_count": 37485,
    # NASA exposes no comment count; yt-dlp reports null and so does the payload.
    "comment_count": None,
    "categories": ["Science & Technology"],
    "upload_date": "20250210",
    "extractor": "youtube",
    "formats": [
        {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none",
         "height": 180},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 129.485, "filesize": 7519596},
        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus",
         "abr": 133.299, "filesize": 7740742},
        # 1080p: VP9 and AV1 have a size, H.264 has none -> H.264 wins anyway.
        {"format_id": "614", "ext": "mp4", "vcodec": "vp09.00.40.0", "height": 1080,
         "fps": 30, "filesize": 53758431},
        {"format_id": "399", "ext": "mp4", "vcodec": "av01.0.08M.0", "height": 1080,
         "fps": 30, "filesize": 40856217},
        {"format_id": "270", "ext": "mp4", "vcodec": "avc1.640028", "height": 1080,
         "fps": 30},
        {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4D401F", "height": 720,
         "fps": 30, "filesize": 23460805},
        {"format_id": "135", "ext": "mp4", "vcodec": "avc1.4D401F", "height": 480,
         "fps": 30, "filesize_approx": 13561053},
        {"format_id": "134", "ext": "mp4", "vcodec": "avc1.4D401E", "height": 360,
         "fps": 30, "filesize": 7941073},
        {"format_id": "229", "ext": "mp4", "vcodec": "avc1.4D4015", "height": 240,
         "fps": 30},
        {"format_id": "160", "ext": "mp4", "vcodec": "avc1.4D400C", "height": 144,
         "fps": 30, "filesize": 1949671},
    ],
}


class TestScalars:
    def test_filesize(self):
        assert media_meta.format_filesize(33_659_750) == "32.1MB"
        assert media_meta.format_filesize(4_194_304) == "4.0MB"
        assert media_meta.format_filesize(1_288_490_188) == "1.2GB"
        assert media_meta.format_filesize(786_432) == "768KB"
        assert media_meta.format_filesize(512) == "512B"
        assert media_meta.format_filesize(None) is None
        assert media_meta.format_filesize(0) is None
        assert media_meta.format_filesize("nonsense") is None  # type: ignore[arg-type]

    def test_count(self):
        assert media_meta.format_count(942) == "942"
        assert media_meta.format_count(37_485) == "37.5K"
        assert media_meta.format_count(15_100_000) == "15.1M"
        assert media_meta.format_count(None) is None
        assert media_meta.format_count(-1) is None

    def test_duration(self):
        assert media_meta.format_duration(465) == "7:45"
        assert media_meta.format_duration(3723) == "1:02:03"
        assert media_meta.format_duration(0) is None
        assert media_meta.format_duration(None) is None

    def test_upload_date(self):
        assert media_meta.format_upload_date("20250210") == "2025-02-10"
        assert media_meta.format_upload_date("2025-02-10") == "2025-02-10"
        assert media_meta.format_upload_date("2025") is None
        assert media_meta.format_upload_date(None) is None


class TestFormats:
    def test_one_entry_per_height_high_to_low(self):
        formats = media_meta.collect_formats(YTDLP_INFO)
        heights = [f["height"] for f in formats]
        assert heights == sorted(heights, reverse=True)
        assert len(heights) == len(set(heights))
        assert heights == [1080, 720, 480, 360, 240, 144]

    def test_storyboards_and_audio_are_not_video_formats(self):
        ids = {f["id"] for f in media_meta.collect_formats(YTDLP_INFO)}
        assert "sb0" not in ids
        assert "140" not in ids
        assert "251" not in ids

    def test_prefers_h264_over_vp9_and_av1(self):
        formats = media_meta.collect_formats(YTDLP_INFO)
        best_1080 = next(f for f in formats if f["height"] == 1080)
        # The capture offers 1080p as VP9, AV1 and H.264; only H.264 is picked
        # even though the other two are the only ones carrying a size.
        assert best_1080["id"] == "270"
        assert best_1080["vcodec"].startswith("avc")

    def test_sizes_and_labels(self):
        formats = {f["height"]: f for f in media_meta.collect_formats(YTDLP_INFO)}
        assert formats[720]["size_bytes"] == 23460805
        assert formats[720]["size_label"] == "22.4MB"
        assert formats[720]["button_label"] == "720p · 22.4MB"
        # filesize_approx is used when the exact size is missing.
        assert formats[480]["size_label"] == "12.9MB"

    def test_unknown_size_has_no_size_label(self):
        formats = {f["height"]: f for f in media_meta.collect_formats(YTDLP_INFO)}
        assert formats[240]["size_bytes"] is None
        assert formats[240]["size_label"] is None
        assert formats[240]["button_label"] == "240p"

    def test_historical_label_shape_is_kept(self):
        for fmt in media_meta.collect_formats(YTDLP_INFO):
            assert fmt["label"].startswith(f"{fmt['height']}p")
            assert fmt["id"]
            assert isinstance(fmt["height"], int)

    def test_empty_and_garbage_formats(self):
        assert media_meta.collect_formats({}) == []
        assert media_meta.collect_formats({"formats": None}) == []
        assert media_meta.collect_formats({"formats": [{}, "junk", None]}) == []

    def test_high_frame_rate_is_marked(self):
        """Only >=50 fps earns the suffix; the capture above is 30 fps."""
        assert media_meta.collect_formats(YTDLP_INFO)[0]["label"] == "1080p"
        info = {"formats": [{"format_id": "1", "ext": "mp4", "vcodec": "avc1.640028",
                             "height": 1080, "fps": 60}]}
        assert media_meta.collect_formats(info)[0]["label"] == "1080p60"


class TestAudio:
    def test_best_audio_is_the_highest_bitrate(self):
        audio = media_meta.describe_audio(YTDLP_INFO)
        assert audio is not None
        # Opus at 133 kbit/s beats the 129 kbit/s AAC track in this capture.
        assert audio["id"] == "251"
        assert audio["abr"] == 133
        assert audio["size_label"] == "7.4MB"

    def test_size_is_estimated_from_duration_and_bitrate(self):
        info = {
            "duration": 60,
            "formats": [{"format_id": "1", "vcodec": "none", "acodec": "opus", "abr": 128}],
        }
        audio = media_meta.describe_audio(info)
        assert audio is not None
        assert audio["size_bytes"] == 960_000  # 128 kbit/s * 60 s / 8
        assert audio["size_label"] == "938KB"  # 960000 B rounds up

    def test_no_audio_streams(self):
        assert media_meta.describe_audio({"formats": []}) is None


class TestInfoPayload:
    def test_every_expected_field_is_present(self):
        payload = media_meta.build_info_payload(YTDLP_INFO)
        for key in (
            "title", "thumbnail", "duration", "uploader", "uploader_id", "channel",
            "channel_follower_count", "view_count", "like_count", "comment_count",
            "categories", "upload_date", "extractor", "formats", "audio",
        ):
            assert key in payload

    def test_values_are_mapped(self):
        payload = media_meta.build_info_payload(YTDLP_INFO)
        assert payload["title"].startswith("Artemis II to the Moon")
        assert payload["uploader_id"] == "@NASA"
        assert payload["channel"] == "NASA"
        assert payload["channel_follower_count"] == 15100000
        assert payload["view_count"] == 2927499
        assert payload["like_count"] == 37485
        assert payload["comment_count"] is None
        assert payload["categories"] == ["Science & Technology"]
        assert payload["upload_date"] == "2025-02-10"
        assert payload["duration"] == 465
        assert payload["formats"]

    def test_missing_fields_are_none_not_absent(self):
        payload = media_meta.build_info_payload({"title": "T", "extractor": "x"})
        assert payload["channel_follower_count"] is None
        assert payload["view_count"] is None
        assert payload["categories"] == []
        assert payload["upload_date"] is None
        assert payload["formats"] == []
        assert payload["uploader"] == ""

    def test_historical_keys_keep_their_types(self):
        payload = media_meta.build_info_payload(YTDLP_INFO)
        assert isinstance(payload["title"], str)
        assert isinstance(payload["thumbnail"], str)
        assert isinstance(payload["uploader"], str)
        assert isinstance(payload["extractor"], str)
        assert isinstance(payload["formats"], list)
        for fmt in payload["formats"]:
            assert {"id", "label", "height"} <= set(fmt)

    def test_uploader_falls_back_to_channel(self):
        payload = media_meta.build_info_payload({"channel": "Only Channel"})
        assert payload["uploader"] == "Only Channel"

    def test_counters_are_ints(self):
        payload = media_meta.build_info_payload(
            {"view_count": 12.0, "like_count": "42", "comment_count": None}
        )
        assert payload["view_count"] == 12
        assert payload["like_count"] is None
        assert payload["comment_count"] is None


class TestFilenames:
    def test_video_and_audio_names(self):
        assert media_meta.build_filename("Artemis II mission", ".mp4", 1080) == \
            "Artemis II mission [1080p].mp4"
        assert media_meta.build_filename("Artemis II mission", ".mp3") == "Artemis II mission.mp3"

    def test_unsafe_characters_removed(self):
        assert media_meta.build_filename('a/b\\c:d*e?f"g<h>i|j.z', ".mp4") == \
            "abcdefghij.z.mp4"

    def test_unicode_kept(self):
        assert media_meta.build_filename("Привет 世界", ".mp3") == "Привет 世界.mp3"

    def test_length_bounded(self):
        name = media_meta.build_filename("X" * 300, ".mp4", 720)
        assert len(name) <= 64
        assert name.endswith(" [720p].mp4")

    def test_empty_title_falls_back(self):
        assert media_meta.build_filename("", ".mp3") == "koyura.mp3"
