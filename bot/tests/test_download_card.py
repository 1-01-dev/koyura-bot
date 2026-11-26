"""Tests for the preview card, the inline keyboard and the file naming.

The helpers under test are pure functions of the ``/api/info`` payload, so no
Telegram network, engine or event loop is involved.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import download_card as card

CALLBACK_LIMIT = 64


FULL_INFO = {
    "title": "Artemis II to the Moon: Launch to Splashdown (NASA Mission Animation)",
    "thumbnail": "https://example.com/thumb.jpg",
    "duration": 465,
    "uploader": "NASA",
    "uploader_id": "@NASA",
    "channel": "NASA",
    "channel_follower_count": 15_100_000,
    "view_count": 2_927_499,
    "like_count": 37_485,
    "comment_count": 5_200,
    "categories": ["Science & Technology"],
    "upload_date": "2025-02-10",
    "extractor": "youtube",
    "formats": [
        {"id": "137", "label": "1080p", "height": 1080, "size_label": "32.1MB",
         "button_label": "1080p · 32.1MB"},
        {"id": "136", "label": "720p", "height": 720, "size_label": "17.6MB",
         "button_label": "720p · 17.6MB"},
        {"id": "135", "label": "480p", "height": 480, "size_label": "9.2MB",
         "button_label": "480p · 9.2MB"},
        {"id": "134", "label": "360p", "height": 360, "size_label": "5.1MB",
         "button_label": "360p · 5.1MB"},
        {"id": "133", "label": "240p", "height": 240, "size_label": "3.4MB",
         "button_label": "240p · 3.4MB"},
        {"id": "160", "label": "144p", "height": 144, "size_label": "1.9MB",
         "button_label": "144p · 1.9MB"},
    ],
    "audio": {"id": "140", "abr": 129, "size_bytes": 10_271_496, "size_label": "9.8MB"},
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


def _button_labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

class TestCard:
    def test_full_card_shows_every_field(self):
        caption = _plain(card.build_card(FULL_INFO))
        assert "Artemis II to the Moon" in caption
        assert "NASA" in caption
        # The channel and its handle are both "NASA" in this capture, so the
        # card prints the name once and no "@NASA" line at all.
        assert "@NASA" not in caption
        # followers, duration, engagement, category and date
        assert "15.1M" in caption
        assert "7:45" in caption
        assert "2.9M views" in caption
        assert "likes" in caption
        assert "comments" in caption
        assert "Science & Technology" in caption
        assert "2025-02-10" in caption

    def test_card_lists_formats_and_mp3(self):
        caption = _plain(card.build_card(FULL_INFO))
        for label in ("1080p", "720p", "480p", "360p", "240p", "144p"):
            assert label in caption
        assert "MP3" in caption
        assert "32.1MB" in caption

    def test_card_is_markdown_escaped(self):
        info = dict(FULL_INFO, title="A [bracket] and *stars*")
        caption = card.build_card(info)
        assert r"\[bracket\]" in caption
        assert r"\*stars\*" in caption

    def test_missing_fields_are_not_drawn(self):
        minimal = {"title": "Only a title", "extractor": "youtube", "formats": []}
        caption = _plain(card.build_card(minimal))
        assert "Only a title" in caption
        for absent in ("Followers", "Duration", "views", "Category", "Uploaded", "@"):
            assert absent not in caption

    def test_absent_handle_does_not_duplicate_channel(self):
        info = {"title": "T", "channel": "NASA", "formats": []}
        caption = _plain(card.build_card(info))
        assert caption.count("NASA") == 1

    def test_caption_fits_telegram_limit(self):
        many = {
            "title": "X" * 2000,
            "channel": "C" * 300,
            "uploader_id": "@" + "h" * 300,
            "channel_follower_count": 5,
            "view_count": 5,
            "like_count": 5,
            "comment_count": 5,
            "categories": ["cat"] * 20,
            "upload_date": "20200101",
            "duration": 10,
            "formats": [
                {"id": str(i), "label": f"{i}p", "height": i,
                 "button_label": f"{i}p · 9.9MB"} for i in range(40)
            ],
            "audio": {"size_bytes": 123456},
        }
        caption = card.build_card(many)
        assert len(caption) <= 1024

    def test_short_duration_and_counts(self):
        assert card.format_duration(62) == "1:02"
        assert card.format_duration(3723) == "1:02:03"
        assert card.format_duration(None) is None
        assert card.format_duration(0) is None
        assert card.format_count(942) == "942"
        assert card.format_count(110_575) == "110.6K"
        assert card.format_count(1_260_000) == "1.3M"
        assert card.format_count(None) is None
        assert card.format_filesize(33_659_750) == "32.1MB"
        assert card.format_filesize(None) is None
        assert card.format_upload_date("20141110") == "2014-11-10"
        assert card.format_upload_date("2014-11-10") == "2014-11-10"
        assert card.format_upload_date(None) is None


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

class TestKeyboard:
    def test_one_button_per_format_plus_mp3(self):
        markup = card.build_format_keyboard(FULL_INFO, 42, "abcdef123456")
        labels = _button_labels(markup)
        assert len(labels) == len(FULL_INFO["formats"]) + 1
        assert labels[-1].startswith("MP3")
        # Sizes travel in the button caption.
        assert "1080p · 32.1MB" in labels

    def test_two_buttons_per_row(self):
        markup = card.build_format_keyboard(FULL_INFO, 42, "abcdef123456")
        rows = [len(row) for row in markup.inline_keyboard]
        assert all(count <= 2 for count in rows), rows

    def test_callback_data_within_telegram_limit(self):
        info = {
            "formats": [
                {"id": "a" * 40, "label": "1p", "height": 1},
                {"id": "137", "label": "1080p", "height": 1080},
            ],
            "audio": {"size_bytes": 1},
        }
        markup = card.build_format_keyboard(info, 2**31 - 1, "f" * 12)
        for data in _callbacks(markup):
            assert len(data.encode("utf-8")) <= CALLBACK_LIMIT, data

    def test_long_format_id_falls_back_to_best(self):
        info = {
            "formats": [{"id": "z" * 64, "label": "1p", "height": 1}],
            "audio": {"size_bytes": 1},
        }
        markup = card.build_format_keyboard(info, 2**31 - 1, "f" * 12)
        expected = f"dl:{2**31 - 1}:{'f' * 12}:best"
        assert expected in _callbacks(markup)

    def test_many_formats_are_truncated_with_best_button(self):
        info = {
            "formats": [
                {"id": str(i), "label": f"{i}p", "height": i} for i in range(1, 25)
            ],
            "audio": {"size_bytes": 1},
        }
        markup = card.build_format_keyboard(info, 7, "abcdef123456")
        labels = _button_labels(markup)
        assert "Best quality" in labels
        video_buttons = [b for b in labels if b != "Best quality" and not b.startswith("MP3")]
        assert len(video_buttons) == card.MAX_FORMAT_BUTTONS

    def test_formats_are_sorted_high_to_low(self):
        markup = card.build_format_keyboard(FULL_INFO, 1, "abcdef123456")
        heights = [int(b.text.split("p")[0]) for b in markup.inline_keyboard[0]
                   if not b.text.startswith("MP3")]
        assert heights == sorted(heights, reverse=True)

    def test_no_formats_still_offers_mp3(self):
        markup = card.build_format_keyboard({"formats": []}, 1, "abcdef123456")
        assert _button_labels(markup) == ["MP3"]

    def test_callback_data_fits_helper(self):
        assert card.callback_data_fits("a" * 64)
        assert not card.callback_data_fits("a" * 65)


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

class TestFilename:
    def test_video_name_has_title_and_height(self):
        assert card.build_filename("Artemis II to the Moon", ".mp4", 1080) == \
            "Artemis II to the Moon [1080p].mp4"

    def test_audio_name_has_no_height(self):
        assert card.build_filename("Artemis II to the Moon", ".mp3") == "Artemis II to the Moon.mp3"

    def test_unsafe_characters_are_stripped(self):
        name = card.build_filename('a/b\\c:d*e?f"g<h>i|j', ".mp4")
        assert name == "abcdefghij.mp4"

    def test_unicode_is_preserved(self):
        assert card.build_filename("Привет 世界", ".mp3") == "Привет 世界.mp3"

    def test_length_is_bounded(self):
        name = card.build_filename("X" * 200, ".mp4", 720)
        assert len(name) <= 64
        assert name.endswith(" [720p].mp4")

    def test_empty_title_falls_back(self):
        assert card.build_filename("", ".mp3") == "koyura.mp3"
        assert card.build_filename("///", ".mp3") == "koyura.mp3"

    def test_control_characters_removed(self):
        assert card.build_filename("a\x00b\nc", ".mp3") == "a b c.mp3"


# ---------------------------------------------------------------------------
# Caption under the uploaded file
# ---------------------------------------------------------------------------

class TestFileCaption:
    def test_caption_has_title_size_duration_quality(self):
        caption = _plain(card.build_file_caption(
            FULL_INFO, quality="1080p", size_bytes=33_659_750, duration=465
        ))
        assert "Artemis II to the Moon" in caption
        assert "32.1MB" in caption
        assert "7:45" in caption
        assert "1080p" in caption
        assert "Sent:" not in caption

    def test_caption_falls_back_to_info_duration(self):
        caption = _plain(card.build_file_caption(FULL_INFO, quality="720p"))
        assert "7:45" in caption
        assert "720p" in caption

    def test_caption_without_optional_facts(self):
        caption = _plain(card.build_file_caption({"title": "Only"}, quality=None))
        assert "Only" in caption

    def test_caption_is_escaped(self):
        caption = card.build_file_caption({"title": "a_b*c"}, quality="1080p")
        assert r"a\_b" in caption and r"c" in caption

    def test_caption_is_bounded(self):
        caption = card.build_file_caption({"title": "X" * 5000})
        assert len(caption) <= 1024
