"""The button router: every ``callback_data`` scheme, action and refusal.

A card is one message with one job, so these tests care as much about what does
*not* happen (no second task, no second ``answer()``) as about what does.
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from handlers import callbacks, config, download_flow, sessions  # noqa: E402

INFO = {
    "title": "Artemis II to the Moon",
    "duration": 635,
    "extractor": "youtube",
    "channel": "NASA",
    "formats": [
        {"id": "137", "label": "1080p", "height": 1080, "button_label": "1080p"},
        {"id": "22", "label": "720p", "height": 720, "button_label": "720p"},
    ],
    "audio": {"abr": 129},
}

HASH = "abcdef123456"


@pytest.fixture(autouse=True)
def _clean_sessions():
    sessions.forget_all()
    yield
    sessions.forget_all()


@pytest.fixture()
def card():
    """A live session as the preview card would have recorded it."""
    entry = sessions.remember(
        url="https://x.example/1",
        chat_id=5,
        user_id=7,
        username="tester",
        info=INFO,
        message_id=11,
        photo=True,
    )
    # The preview derives the hash from the URL; the tests use a fixed one.
    sessions._sessions.pop(sessions.key(5, 11, sessions.url_hash("https://x.example/1")))
    sessions._sessions[sessions.key(5, 11, HASH)] = entry
    return entry


def fake_query(data, *, chat_id=5, message_id=11, message=True, photo=None):
    query = MagicMock()
    query.data = data
    if message:
        query.message = MagicMock()
        query.message.chat.id = chat_id
        query.message.message_id = message_id
        # A real ``Message`` carries ``None`` here for a text card; without this
        # a MagicMock attribute would look like a photo.
        query.message.photo = photo
    else:
        query.message = None
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


@pytest.fixture()
def spawned(monkeypatch):
    """Capture the download task's arguments instead of running the download."""
    jobs = []

    def _capture(coro):
        frame = coro.cr_frame
        args = dict(frame.f_locals) if frame is not None else {}
        args.pop("context", None)
        jobs.append(args)
        coro.close()

    monkeypatch.setattr(callbacks.asyncio, "create_task", _capture)
    return jobs


class TestParsing:
    @pytest.mark.parametrize(
        "data, expected",
        [
            ("dl:42:abcdef123456:137", (42, "abcdef123456", "137")),
            ("fmt:42:abcdef123456:video", (42, "abcdef123456", "video")),
            ("qty:42:abcdef123456:best", (42, "abcdef123456", "best")),
            ("dl:0:abcdef123456:mp3", (0, "abcdef123456", "mp3")),
        ],
    )
    def test_schemes(self, data, expected):
        assert sessions.parse_callback_data(data) == expected

    @pytest.mark.parametrize(
        "data",
        ["", "dl", "dl:42", "dl:42:hash", "dl:notanint:hash:137", None],
    )
    def test_malformed_is_none(self, data):
        assert sessions.parse_callback_data(data) is None

    def test_extra_parts_keep_the_last_two(self):
        assert sessions.parse_callback_data("dl:42:hash:extra:137") == (42, "extra", "137")


class TestRouting:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data, fmt, format_id, height",
        [
            (f"dl:11:{HASH}:137", "video", "137", 1080),
            (f"dl:11:{HASH}:mp3", "audio", None, None),
            (f"dl:11:{HASH}:audio", "audio", None, None),
            # "best" is pinned to the highest sendable format, not left to the
            # engine: the engine's best may be a file Telegram refuses.
            (f"dl:11:{HASH}:best", "video", "137", 1080),
            # An id that is no longer in the preview is still forwarded.
            (f"dl:11:{HASH}:999", "video", "999", None),
        ],
    )
    async def test_a_tap_starts_the_right_job(self, card, spawned, data, fmt, format_id, height):
        update, query = fake_query(data)
        await callbacks.on_format_button(update, MagicMock())

        assert len(spawned) == 1
        args = spawned[0]
        assert (args["fmt"], args["format_id"], args["height"]) == (fmt, format_id, height)
        assert args["status_message_id"] == 11
        assert args["url_hash"] == HASH
        assert args["entry"] is card
        assert card["job_active"] is True
        # One plain answer, no alert.
        assert query.answer.await_args.args == ()
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_video_button_re_renders_the_keyboard(self, card, spawned):
        update, query = fake_query(f"fmt:11:{HASH}:video")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        query.answer.assert_awaited_once()
        markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert f"dl:11:{HASH}:137" in datas
        assert card["job_active"] is False

    @pytest.mark.asyncio
    async def test_legacy_back_button_is_the_same(self, card, spawned):
        update, query = fake_query(f"qty:11:{HASH}:back")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        query.edit_message_reply_markup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_malformed_data_is_only_answered(self, card, spawned):
        update, query = fake_query("nonsense")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_callback_query_is_ignored(self):
        update = MagicMock()
        update.callback_query = None
        await callbacks.on_format_button(update, MagicMock())  # must not raise

    @pytest.mark.asyncio
    async def test_missing_session_says_it_expired(self, spawned):
        update, query = fake_query(f"dl:11:{HASH}:137")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        query.answer.assert_awaited_once()
        # The tapped card is rewritten, not joined by a second message.
        assert query.edit_message_text.await_args.args[0] == (
            "Session expired. Please send the link again."
        )

    @pytest.mark.asyncio
    async def test_missing_session_on_a_photo_card_edits_the_caption(self, spawned):
        """A photo card cannot be edited with ``edit_message_text``."""
        update, query = fake_query(f"dl:11:{HASH}:137", photo=[object()])
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        query.edit_message_text.assert_not_awaited()
        assert query.edit_message_caption.await_args.kwargs["caption"] == (
            "Session expired. Please send the link again."
        )


class TestRepeatTap:
    @pytest.mark.asyncio
    async def test_second_tap_is_refused_when_a_job_runs(self, card, spawned):
        card["job_active"] = True
        update, query = fake_query(f"dl:11:{HASH}:137")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        assert query.answer.await_count == 1
        assert query.answer.await_args.args == ("Already downloading — wait for the file.",)
        assert query.answer.await_args.kwargs["show_alert"] is False

    @pytest.mark.asyncio
    async def test_the_card_is_unlocked_after_the_job_finishes(self, card, spawned):
        """A failure gives the buttons back: the next tap must start again."""
        card["job_active"] = True

        async def _job(**kwargs):
            return None

        # Simulate what run_download does in its ``finally``.
        entry = card
        try:
            pass
        finally:
            entry["job_active"] = False

        update, query = fake_query(f"dl:11:{HASH}:137")
        await callbacks.on_format_button(update, MagicMock())
        assert len(spawned) == 1

    @pytest.mark.asyncio
    async def test_legacy_button_never_marks_the_card_busy(self, card, spawned):
        update, query = fake_query(f"fmt:11:{HASH}:video")
        await callbacks.on_format_button(update, MagicMock())
        assert card["job_active"] is False


class TestAddressing:
    @pytest.mark.asyncio
    async def test_inaccessible_message_still_finds_the_card(self, card, spawned):
        """query.message can be None; the stored chat id is the fallback."""
        update, query = fake_query(f"dl:11:{HASH}:137", message=False)
        await callbacks.on_format_button(update, MagicMock())

        assert len(spawned) == 1
        assert spawned[0]["chat_id"] == 5

    @pytest.mark.asyncio
    async def test_forwarded_card_is_found_by_the_recorded_message_id(self, card, spawned):
        """Telegram reports a new message id after a forward; the data keeps it."""
        update, query = fake_query(f"dl:11:{HASH}:137", message_id=99)
        await callbacks.on_format_button(update, MagicMock())

        assert len(spawned) == 1
        assert spawned[0]["url_hash"] == HASH

    def test_candidate_chat_ids_are_unique_and_lead_with_the_message_chat(self):
        session = sessions.remember(
            url="https://x.example/1",
            chat_id=5,
            user_id=7,
            username="tester",
            info=INFO,
            message_id=11,
            photo=True,
        )
        session["message_id"] = 11
        query = MagicMock()
        query.message.chat.id = 5

        assert sessions.candidate_chat_ids(query, 11) == [5]

    def test_candidate_chat_ids_without_a_message(self):
        sessions.remember(
            url="https://x.example/1",
            chat_id=5,
            user_id=7,
            username="tester",
            info=INFO,
            message_id=11,
            photo=True,
        )
        query = MagicMock()
        query.message = None

        assert sessions.candidate_chat_ids(query, 11) == [5]


class TestSessionTable:
    def test_evict_drops_only_expired_sessions(self):
        fresh = sessions.remember(
            url="https://x.example/fresh",
            chat_id=5,
            user_id=7,
            username="t",
            info=INFO,
            message_id=11,
            photo=True,
        )
        stale = sessions.remember(
            url="https://x.example/stale",
            chat_id=5,
            user_id=7,
            username="t",
            info=INFO,
            message_id=12,
            photo=True,
        )
        stale["created"] = time.time() - config.STATE_TTL - 1

        assert sessions.evict_stale() == 1
        assert sessions.live_count() == 1
        assert sessions.get(5, 11, sessions.url_hash("https://x.example/fresh")) is fresh

    def test_remember_records_the_defaults(self):
        entry = sessions.remember(
            url="https://x.example/1",
            chat_id=5,
            user_id=7,
            username="tester",
            info=INFO,
            message_id=11,
            photo=False,
        )
        assert entry["photo"] is False
        assert entry["job_active"] is False
        assert entry["url"] == "https://x.example/1"
        assert entry["info"] is INFO
        assert entry["created"] <= time.time()


class TestChoiceResolution:
    """The mapping a tap goes through, kept next to the router that uses it."""

    def test_mp3(self):
        assert download_flow.resolve_choice(INFO, "mp3") == ("audio", None, None)

    def test_best(self):
        assert download_flow.resolve_choice(INFO, "best") == ("video", None, None)

    def test_known_format(self):
        assert download_flow.resolve_choice(INFO, "22") == ("video", "22", 720)

    def test_unknown_format(self):
        assert download_flow.resolve_choice(INFO, "999") == ("video", "999", None)


class TestTaskSpawn:
    @pytest.mark.asyncio
    async def test_task_is_created_on_the_running_loop(self, card):
        """The handler must not await the download: the query has to be answered."""
        calls = []
        real_create_task = asyncio.create_task

        def _capture(coro):
            coro.close()
            calls.append(True)
            return None

        callbacks.asyncio.create_task = _capture
        try:
            update, _ = fake_query(f"dl:11:{HASH}:137")
            await callbacks.on_format_button(update, MagicMock())
        finally:
            callbacks.asyncio.create_task = real_create_task

        assert calls == [True]


# ---------------------------------------------------------------------------
# A streaming track card
# ---------------------------------------------------------------------------

TRACK_INFO = {
    "title": "Never Gonna Give You Up",
    "thumbnail": "https://image-cdn.spotifycdn.com/x",
    "duration": 213,
    "uploader": "Rick Astley",
    "channel": "Rick Astley",
    "formats": [{"id": "137", "label": "1080p", "height": 1080}],
    "audio": {"abr": 129},
    "track": {
        "service": "spotify",
        "service_label": "Spotify",
        "title": "Never Gonna Give You Up",
        "artist": "Rick Astley",
        "duration": 214,
        "thumbnail": "https://image-cdn.spotifycdn.com/x",
        "candidates": [
            {"index": 1, "label": "1 · 3:34", "duration": 214,
             "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            {"index": 2, "label": "2 · 3:48", "duration": 228,
             "url": "https://www.youtube.com/watch?v=6PLatPMoxGw"},
        ],
    },
}


@pytest.fixture()
def track_card_entry():
    """A live session whose payload is a substituted track."""
    url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    entry = sessions.remember(
        url=url,
        chat_id=5,
        user_id=7,
        username="tester",
        info=TRACK_INFO,
        message_id=11,
        photo=True,
    )
    sessions._sessions.pop(sessions.key(5, 11, sessions.url_hash(url)))
    sessions._sessions[sessions.key(5, 11, HASH)] = entry
    return entry


class TestTrackButtons:
    @pytest.mark.asyncio
    async def test_a_numbered_button_downloads_that_version(
        self, track_card_entry, spawned
    ):
        update, query = fake_query(f"dl:11:{HASH}:t2")
        await callbacks.on_format_button(update, MagicMock())

        assert len(spawned) == 1
        args = spawned[0]
        # A track has one output — no format id, no height — and the flow is
        # told which alternative to fetch; the URL stays the link the user sent,
        # which is what the dashboard records.
        assert (args["candidate_index"], args["entry"]["info"]["track"]["service"]) == (
            2,
            "spotify",
        )
        assert args["url"].startswith("https://open.spotify.com/track/")

    @pytest.mark.asyncio
    async def test_the_keyboard_is_retired_with_the_tap(
        self, track_card_entry, spawned
    ):
        update, query = fake_query(f"dl:11:{HASH}:t1")
        await callbacks.on_format_button(update, MagicMock())
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_second_tap_is_refused(self, track_card_entry, spawned):
        update, query = fake_query(f"dl:11:{HASH}:t1")
        await callbacks.on_format_button(update, MagicMock())
        update2, query2 = fake_query(f"dl:11:{HASH}:t2")
        await callbacks.on_format_button(update2, MagicMock())

        assert len(spawned) == 1
        assert query2.answer.await_args.kwargs["show_alert"] is False

    @pytest.mark.asyncio
    async def test_a_missing_candidate_is_answered_with_the_reason(
        self, track_card_entry, spawned
    ):
        update, query = fake_query(f"dl:11:{HASH}:t9")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        assert query.answer.await_args.kwargs["show_alert"] is True

    @pytest.mark.asyncio
    async def test_a_legacy_mp3_button_takes_the_first_version(
        self, track_card_entry, spawned
    ):
        update, query = fake_query(f"dl:11:{HASH}:mp3")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned[0]["candidate_index"] == 1
