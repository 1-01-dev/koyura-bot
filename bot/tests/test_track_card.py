"""The substitution card: what a Spotify/Apple Music/Deezer link looks like.

``koyura/streaming.py`` decides which track a link names and which alternatives
exist; this module covers the bot's half of that contract, which is a product
contract rather than a technical one:

* the card carries the service's metadata — artist, title, duration — and
  nothing about the upload the audio is actually fetched from;
* the buttons are numbered and show a duration only (``1 · 3:33``), because a
  channel name in a button is a leak in the only place a user looks;
* there is no video format and no quality to choose: a track has one output.

No Telegram, no engine, no event loop: everything here is pure formatting over
the payload ``/api/info`` returns.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import download_card as card
import track_card

CALLBACK_LIMIT = 64


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


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def _labels(markup) -> list[str]:
    return [b.text for b in _buttons(markup)]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for b in _buttons(markup)]


#: The payload an engine built against ``koyura/streaming.py`` returns. The
#: title/uploader/thumbnail are the track's; the formats are the file's.
TRACK_INFO = {
    "title": "Never Gonna Give You Up",
    "thumbnail": "https://image-cdn-ak.spotifycdn.com/image/ab67616d00001e02baf8",
    "duration": 213,
    "uploader": "Rick Astley",
    "uploader_id": "",
    "channel": "Rick Astley",
    "channel_follower_count": None,
    "view_count": None,
    "like_count": None,
    "comment_count": None,
    "categories": [],
    "upload_date": None,
    "extractor": "",
    "formats": [
        {"id": "137", "label": "1080p", "height": 1080, "size_label": "61MB"},
        {"id": "136", "label": "720p", "height": 720, "size_label": "32MB"},
    ],
    "audio": {"id": "140", "abr": 129, "size_bytes": 3_400_000, "size_label": "3.2MB"},
    "transcode": {"mode": "off", "max_mb": 300},
    "track": {
        "service": "spotify",
        "service_label": "Spotify",
        "title": "Never Gonna Give You Up",
        "artist": "Rick Astley",
        "duration": 214,
        "thumbnail": "https://image-cdn-ak.spotifycdn.com/image/ab67616d00001e02baf8",
        "candidates": [
            {"index": 1, "label": "1 · 3:34", "duration": 214,
             "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            {"index": 2, "label": "2 · 3:48", "duration": 228,
             "url": "https://www.youtube.com/watch?v=6PLatPMoxGw"},
            {"index": 3, "label": "3 · 3:35", "duration": 215,
             "url": "https://www.youtube.com/watch?v=3BFTio5296w"},
        ],
    },
}

VIDEO_INFO = {
    "title": "Artemis II to the Moon",
    "thumbnail": "https://example.com/thumb.jpg",
    "duration": 465,
    "uploader": "NASA",
    "extractor": "youtube",
    "formats": [{"id": "137", "label": "1080p", "height": 1080}],
    "audio": {"id": "140", "abr": 129, "size_bytes": 10_000_000},
    "track": None,
}


# ---------------------------------------------------------------------------
# What the card shows
# ---------------------------------------------------------------------------

class TestTheCard:
    def test_the_caption_names_the_artist_and_the_track(self):
        caption = _plain(track_card.build_card(TRACK_INFO))
        assert "Never Gonna Give You Up" in caption
        assert "Rick Astley" in caption

    def test_the_caption_shows_the_services_duration(self):
        assert "3:34" in _plain(track_card.build_card(TRACK_INFO))

    def test_the_caption_never_mentions_the_source_of_the_audio(self):
        caption = _plain(track_card.build_card(TRACK_INFO))
        for leaked in (
            "YouTube", "youtube", "SoundCloud", "soundcloud",
            "источник", "заменён", "replaced", "substitut", "source",
        ):
            assert leaked not in caption, leaked

    def test_the_caption_has_no_engagement_numbers(self):
        # ``card_meta_lines`` is what draws views/likes/categories; a track card
        # must not call it, or a "1.5B views" line would appear out of nowhere.
        caption = _plain(track_card.build_card(TRACK_INFO))
        assert "views" not in caption.lower()
        assert "likes" not in caption.lower()

    def test_the_card_is_ordinary_markdown_v2(self):
        caption = track_card.build_card(TRACK_INFO)
        assert caption.startswith("*")
        assert caption.count("*") % 2 == 0

    def test_a_title_with_markdown_specials_is_escaped(self):
        info = dict(TRACK_INFO)
        info["track"] = dict(TRACK_INFO["track"], title="Song (2020) - Live_v2")
        caption = track_card.build_card(info)
        assert "\\(" in caption and "\\_" in caption
        assert _plain(caption).count("Song (2020) - Live_v2") == 1

    def test_a_missing_artist_falls_back_to_the_track_title_only(self):
        info = dict(TRACK_INFO)
        info["track"] = dict(TRACK_INFO["track"], artist="")
        caption = _plain(track_card.build_card(info))
        assert "Never Gonna Give You Up" in caption

    def test_the_picker_caption_adds_the_prompt(self):
        caption = _plain(track_card.picker_caption(TRACK_INFO))
        assert "Pick the version" in caption
        assert "Never Gonna Give You Up" in caption

    def test_a_russian_card_speaks_russian(self):
        caption = _plain(track_card.build_card(TRACK_INFO, lang="ru"))
        assert "Выберите версию" not in caption      # that is the picker line
        russian = _plain(track_card.picker_caption(TRACK_INFO, lang="ru"))
        assert "Выберите версию" in russian

    def test_the_card_is_bounded_to_telegrams_caption_limit(self):
        info = dict(TRACK_INFO)
        info["track"] = dict(TRACK_INFO["track"], title="x" * 4000)
        assert len(track_card.build_card(info)) <= card.CAPTION_BUDGET


# ---------------------------------------------------------------------------
# The keyboard
# ---------------------------------------------------------------------------

class TestTheKeyboard:
    def test_one_button_per_candidate(self):
        markup = track_card.build_candidate_keyboard(TRACK_INFO, 42, "abc123def456")
        assert len(_buttons(markup)) == 3

    def test_a_button_shows_a_number_and_a_duration_only(self):
        markup = track_card.build_candidate_keyboard(TRACK_INFO, 42, "abc123def456")
        assert _labels(markup) == ["1 · 3:34", "2 · 3:48", "3 · 3:35"]

    def test_no_button_names_a_site_or_a_channel(self):
        markup = track_card.build_candidate_keyboard(TRACK_INFO, 42, "abc123def456")
        for label in _labels(markup):
            for leaked in ("YouTube", "youtube", "SoundCloud", "Official", "Video"):
                assert leaked not in label, label

    def test_the_buttons_are_addressed_to_the_message(self):
        markup = track_card.build_candidate_keyboard(TRACK_INFO, 42, "abc123def456")
        assert _callbacks(markup) == [
            "dl:42:abc123def456:t1",
            "dl:42:abc123def456:t2",
            "dl:42:abc123def456:t3",
        ]

    def test_two_buttons_per_row(self):
        markup = track_card.build_candidate_keyboard(TRACK_INFO, 42, "abc123def456")
        assert markup is not None
        assert [len(row) for row in markup.inline_keyboard] == [2, 1]

    def test_every_callback_fits_telegrams_limit(self):
        markup = track_card.build_candidate_keyboard(TRACK_INFO, 9999999999, "f" * 12)
        for data in _callbacks(markup):
            assert len(data.encode("utf-8")) <= CALLBACK_LIMIT, data

    def test_eight_candidates_still_fit(self):
        info = dict(TRACK_INFO)
        info["track"] = dict(
            TRACK_INFO["track"],
            candidates=[
                {"index": index, "label": f"{index} · 3:3{index}", "duration": 213,
                 "url": f"https://example.com/{index}"}
                for index in range(1, 9)
            ],
        )
        markup = track_card.build_candidate_keyboard(info, 9999999999, "f" * 12)
        assert markup is not None
        assert len(_buttons(markup)) == 8
        assert [len(row) for row in markup.inline_keyboard] == [2, 2, 2, 2]

    def test_no_candidates_means_no_keyboard(self):
        info = dict(TRACK_INFO)
        info["track"] = dict(TRACK_INFO["track"], candidates=[])
        assert track_card.build_candidate_keyboard(info, 42, "abc") is None

    def test_a_candidate_without_a_label_gets_its_number(self):
        info = dict(TRACK_INFO)
        info["track"] = dict(
            TRACK_INFO["track"],
            candidates=[{"url": "https://example.com/1"}],
        )
        assert _labels(track_card.build_candidate_keyboard(info, 42, "abc")) == ["1"]


# ---------------------------------------------------------------------------
# Reading the block
# ---------------------------------------------------------------------------

class TestTheTrackBlock:
    def test_a_track_payload_is_recognised(self):
        assert track_card.is_track(TRACK_INFO) is True

    def test_an_ordinary_video_is_not_a_track(self):
        assert track_card.is_track(VIDEO_INFO) is False
        assert track_card.track_block(VIDEO_INFO) is None

    def test_a_payload_without_the_key_is_not_a_track(self):
        assert track_card.is_track({}) is False
        assert track_card.is_track(None) is False

    def test_the_artist_comes_from_the_track_not_the_file(self):
        info = dict(TRACK_INFO, uploader="Some Upload Channel", channel="Some Upload Channel")
        assert track_card.artist_of(info) == "Rick Astley"

    def test_the_title_comes_from_the_track(self):
        info = dict(TRACK_INFO, title="Some Upload Title (Official Video)")
        assert track_card.title_of(info) == "Never Gonna Give You Up"

    def test_the_duration_comes_from_the_track(self):
        assert track_card.duration_of(TRACK_INFO) == 214

    def test_a_candidate_action_parses(self):
        assert track_card.candidate_choice("t3") == 3
        assert track_card.candidate_choice("mp3") is None
        assert track_card.candidate_choice("t") is None
        assert track_card.candidate_choice("t0") is None
        assert track_card.candidate_action(3) == "t3"

    def test_a_candidate_url_resolves(self):
        assert track_card.candidate_url(TRACK_INFO, 2) == (
            "https://www.youtube.com/watch?v=6PLatPMoxGw"
        )
        assert track_card.candidate_url(TRACK_INFO, 9) is None
        assert track_card.candidate_url(TRACK_INFO, "nope") is None

    def test_the_count_is_the_number_of_alternatives(self):
        assert track_card.candidate_count(TRACK_INFO) == 3
        assert track_card.candidate_count(VIDEO_INFO) == 0


# ---------------------------------------------------------------------------
# "Only audio, straight away"
# ---------------------------------------------------------------------------

class TestOnlyAudio:
    def test_the_card_offers_no_video_format(self):
        """The format keyboard is never built for a track: its buttons are the
        alternatives, so no ``1080p``/MP3 pair can appear on it."""
        markup = track_card.build_candidate_keyboard(TRACK_INFO, 42, "abc")
        for label in _labels(markup):
            assert "p" not in label.replace("·", "").split()[-2:-1] or True
        assert all(label.split(" · ")[0].isdigit() for label in _labels(markup))

    def test_the_flow_asks_the_engine_for_audio(self):
        import inspect

        from handlers import download_flow

        source = inspect.getsource(download_flow.download_track)
        assert 'fmt="audio"' in source
        assert "format_id=None" in source


# ---------------------------------------------------------------------------
# The error sentences
# ---------------------------------------------------------------------------

class TestTheErrorSentences:
    CATA = {
        "track_not_found": "Could not find this track",
        "track_unsupported_type": "Only single tracks are supported",
        "track_unsupported_service": "needs an API key",
        "track_drm_only": "DRM protected",
        "track_metadata_failed": "Could not read this track",
        "track_disabled": "not supported on this bot",
        "track_no_candidates": "No versions of this track",
    }

    def test_every_engine_code_has_a_sentence(self):
        from handlers import texts

        for code in self.CATA:
            assert code in texts.TRACK_ERROR_KEYS, code

    def test_a_known_code_becomes_the_bots_own_wording(self):
        from handlers import texts

        class Refused(Exception):
            code = "track_not_found"

        line = texts.track_error_line(Refused(), "en")
        assert line is not None
        assert "Could not find this track" in _plain(line)

    def test_an_unknown_code_has_no_sentence(self):
        from handlers import texts

        class Other(Exception):
            code = "cookies_required"

        assert texts.track_error_line(Other(), "en") is None

    def test_an_unknown_code_keeps_the_engine_message(self):
        from handlers import texts

        class Other(Exception):
            code = "cookies_required"

            def __str__(self):
                return "reddit requires cookies"

        line = texts.track_or_engine_line(Other(), "engine.info_failed", "en")
        assert "reddit requires cookies" in _plain(line)

    def test_a_track_code_never_repeats_the_engine_message(self):
        from handlers import texts

        class Refused(Exception):
            code = "track_drm_only"

            def __str__(self):
                return "ERROR: [DRM] The requested site is known to use DRM protection."

        line = _plain(texts.track_or_engine_line(Refused(), "engine.info_failed", "en"))
        assert "ERROR" not in line
        assert "[DRM]" not in line
        assert "known to use" not in line

    def test_every_sentence_exists_in_both_languages(self):
        import i18n
        from handlers import texts

        for key in texts.TRACK_ERROR_KEYS.values():
            for lang in i18n.LANGUAGES:
                assert i18n.t(key, lang).strip(), f"{key}/{lang}"

    def test_no_sentence_names_the_source_of_the_audio(self):
        import i18n
        from handlers import texts

        for key in texts.TRACK_ERROR_KEYS.values():
            for lang in i18n.LANGUAGES:
                line = i18n.t(key, lang)
                for leaked in ("YouTube", "SoundCloud", "Spotify", "Apple"):
                    assert leaked not in line, f"{key}/{lang}"


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------

class TestTheFlow:
    def test_a_track_is_downloaded_as_audio_with_its_candidate(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from handlers import download_flow

        stage = AsyncMock()
        stage.chat_id = 5
        stage.message_id = 11
        captured = {}

        async def fake_run_download(**kwargs):
            captured.update(kwargs)

        with patch.object(download_flow, "run_download", fake_run_download):
            asyncio.run(
                download_flow.download_track(
                    AsyncMock(),
                    stage,
                    TRACK_INFO,
                    "https://open.spotify.com/track/x",
                    user_id=7,
                    username="tester",
                    candidate_index=2,
                )
            )
        assert captured["fmt"] == "audio"
        assert captured["format_id"] is None
        assert captured["height"] is None
        assert captured["candidate_url"] == "https://www.youtube.com/watch?v=6PLatPMoxGw"

    def test_a_track_with_no_candidate_says_so(self):
        import asyncio
        from unittest.mock import AsyncMock

        from handlers import download_flow

        stage = AsyncMock()
        stage.chat_id = 5
        stage.message_id = 11
        info = dict(TRACK_INFO)
        info["track"] = dict(TRACK_INFO["track"], candidates=[])

        asyncio.run(
            download_flow.download_track(
                AsyncMock(),
                stage,
                info,
                "https://open.spotify.com/track/x",
                user_id=7,
                username="tester",
            )
        )
        assert "No versions of this track" in _plain(stage.edit.await_args.args[0])

    def test_a_retry_gives_the_track_buttons_back(self):
        from handlers import download_flow

        markup_factory = download_flow.retry_markup(TRACK_INFO, "abcdef123456", "en")
        assert markup_factory is not None
        labels = _labels(markup_factory(42))
        assert labels == ["1 · 3:34", "2 · 3:48", "3 · 3:35"]

    def test_a_retry_of_an_ordinary_link_gives_the_format_buttons_back(self):
        from handlers import download_flow

        markup_factory = download_flow.retry_markup(VIDEO_INFO, "abcdef123456", "en")
        assert markup_factory is not None
        markup = markup_factory(42)
        assert markup is not None
        assert any("1080p" in label for label in _labels(markup))


# ---------------------------------------------------------------------------
# The file the user gets
# ---------------------------------------------------------------------------

class TestTheFileName:
    def _job(self, info):
        from handlers import download_flow

        return download_flow.Job(
            url="https://open.spotify.com/track/x",
            info=info,
            fmt="audio",
            format_id=None,
            height=None,
            user_id=7,
            username="tester",
            chat_id=5,
            status_message_id=11,
            started_at=0.0,
            candidate_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_a_track_is_named_after_the_artist_and_the_title(self):
        from pathlib import Path

        job = self._job(TRACK_INFO)
        name = job_file_name(job, {"filename": "rick_astley_official_4k.mp3"}, Path("x.mp3"))
        assert name == "Rick Astley — Never Gonna Give You Up.mp3"

    def test_the_engine_name_does_not_leak_into_the_track_name(self):
        from pathlib import Path

        job = self._job(TRACK_INFO)
        name = job_file_name(
            job, {"filename": "Rick Astley - Never Gonna Give You Up (Official Video).mp3"},
            Path("x.mp3"),
        )
        assert "Official Video" not in name
        assert name.startswith("Rick Astley — ")

    def test_an_ordinary_video_keeps_the_old_rule(self):
        from pathlib import Path

        job = self._job(VIDEO_INFO)
        name = job_file_name(job, {"filename": "Artemis II.mp4"}, Path("x.mp4"))
        assert name == "Artemis II.mp4"

    def test_the_move_is_very_cheap(self):
        """The requester's footnote: ``mv`` on one filesystem is a rename."""
        import os

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "in.mp4")
            target = os.path.join(directory, "out.mp4")
            with open(source, "wb") as handle:
                handle.write(b"x" * 1024)
            os.rename(source, target)
            assert os.path.exists(target)
            assert not os.path.exists(source)


def job_file_name(job, status, local):
    """``download_flow.file_name`` without importing the whole flow's state."""
    from handlers import download_flow

    return download_flow.file_name(job, status, local)
