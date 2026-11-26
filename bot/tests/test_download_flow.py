"""Tests for the bot's download flow: naming, caption, upload and events.

The engine and Telegram are both stubbed; the point is that a finished job is
uploaded under a human name with the new caption and that the dashboard event
keeps carrying title/format/quality/file_size_bytes.
"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from handlers import callbacks, config, download_flow, sessions  # noqa: E402


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    """The poll loop sleeps 2s between checks; tests never need to wait."""
    real_sleep = download_flow.asyncio.sleep
    async def _fast(delay, *args, **kwargs):
        await real_sleep(0)
    monkeypatch.setattr(download_flow.asyncio, "sleep", _fast)


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


INFO = {
    "title": "Artemis II to the Moon",
    "duration": 635,
    "extractor": "youtube",
    "channel": "NASA",
    "uploader": "NASA",
    "formats": [
        {"id": "137", "label": "1080p", "height": 1080, "button_label": "1080p · 32.1MB"},
    ],
    "audio": {"abr": 129, "size_bytes": 10_271_496},
}


@pytest.fixture()
def media_file(tmp_path, monkeypatch):
    """Point DOWNLOADS_PATH at a temp dir, as the shared volume would."""
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    return tmp_path


def _bot(in_place: bool = True):
    """A bot mock where ``edit_message_media`` works (or fails, for fallback)."""
    bot = MagicMock()
    bot.send_video = AsyncMock()
    bot.send_audio = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_message = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    if in_place:
        bot.edit_message_media = AsyncMock()
    else:
        from telegram.error import BadRequest
        bot.edit_message_media = AsyncMock(
            side_effect=BadRequest("Message can't be edited")
        )
    return bot


def _context(bot):
    context = MagicMock()
    context.bot = bot
    return context


def _status(name, **extra):
    return {"status": "done", "filename": name, "file_size_bytes": 2048,
            "duration": 635, "height": 720, "width": 1280, **extra}


async def _run(monkeypatch, bot, fmt, status, *, format_id=None, height=None,
               card=None, entry=None, url_hash=None):
    # Write the finished artifact the engine would have left on the volume.
    name = status.get("filename") or "job.mp4"
    target = Path(config.DOWNLOADS_PATH) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * 2048)
    monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "job1", "status": "downloading"}))
    monkeypatch.setattr(download_flow, "poll_status", AsyncMock(return_value=status))
    started = AsyncMock()
    done = AsyncMock()
    monkeypatch.setattr(download_flow.event_client, "send_download_start", started)
    monkeypatch.setattr(download_flow.event_client, "send_download_done", done)
    monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_progress", AsyncMock())

    await download_flow.run_download(
        context=_context(bot),
        chat_id=1,
        status_message_id=2,
        url="https://youtube.com/watch?v=x",
        info=INFO,
        fmt=fmt,
        format_id=format_id,
        height=height,
        user_id=3,
        username="tester",
        started_at=0.0,
        card=card,
        entry=entry,
        url_hash=url_hash,
    )
    return started, done


def _delivered(bot) -> dict:
    """The file as the user receives it, normalised to one shape.

    In-place delivery arrives as an ``InputMedia*`` object (where the human name
    lives on the attached ``InputFile``); the fallback path passes the same
    values straight to ``send_video``/``send_audio``/``send_document``.
    """
    if bot.edit_message_media.await_count:
        media = bot.edit_message_media.await_args.kwargs["media"]
        return {
            "filename": getattr(media.media, "filename", None),
            "caption": media.caption,
            "title": getattr(media, "title", None),
            "performer": getattr(media, "performer", None),
            "duration": getattr(media, "duration", None),
        }
    for name in ("send_video", "send_audio", "send_document"):
        sender = getattr(bot, name)
        if sender.await_count:
            return sender.await_args.kwargs
    raise AssertionError("nothing was delivered to the user")


class TestUpload:
    @pytest.mark.asyncio
    async def test_video_uses_engine_filename_and_new_caption(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "video", _status("50c710f337.mp4"),
                   format_id="137", height=1080)

        kwargs = _delivered(bot)
        assert kwargs["filename"] == "50c710f337.mp4"
        caption = _plain(kwargs["caption"])
        assert "Artemis II to the Moon" in caption
        assert "2.0KB" in caption
        assert "10:35" in caption
        assert "1080p" in caption
        assert "Sent:" not in caption

    @pytest.mark.asyncio
    async def test_koyura_filename_is_kept_when_human(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "video", _status("Artemis II to the Moon [720p].mp4"))
        assert _delivered(bot)["filename"] == "Artemis II to the Moon [720p].mp4"

    @pytest.mark.asyncio
    async def test_audio_is_sent_as_audio_with_tags(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "audio", _status("job.mp3"))

        kwargs = _delivered(bot)
        caption = _plain(kwargs["caption"])
        assert kwargs["title"] == "Artemis II to the Moon"
        assert kwargs["performer"] == "NASA"
        assert "KB" in caption
        assert "MP3" in caption
        assert kwargs["duration"] == 635
        assert bot.send_video.call_count == 0

    @pytest.mark.asyncio
    async def test_falls_back_to_built_name_without_engine_filename(self, media_file, monkeypatch):
        # The engine records file_path but no human filename: the bot must
        # build one from the title and the probed height.
        bot = _bot()
        target = media_file / "job.mp4"
        target.write_bytes(b"x" * 2048)
        await _run(monkeypatch, bot, "video",
                   _status(None, file_path=str(target)), height=720)
        assert _delivered(bot)["filename"] == "Artemis II to the Moon [720p].mp4"


class TestEvents:
    @pytest.mark.asyncio
    async def test_start_event_keeps_title_format_quality(self, media_file, monkeypatch):
        bot = _bot()
        started, _ = await _run(monkeypatch, bot, "video", _status("job.mp4"),
                                 format_id="137", height=1080)
        kwargs = started.call_args.kwargs
        assert kwargs["title"] == "Artemis II to the Moon"
        assert kwargs["format"] == "video"
        assert kwargs["quality"] == "1080p"

    @pytest.mark.asyncio
    async def test_audio_quality_is_labelled_mp3(self, media_file, monkeypatch):
        bot = _bot()
        started, _ = await _run(monkeypatch, bot, "audio", _status("job.mp3"))
        assert started.call_args.kwargs["quality"] == "MP3 129kbps"

    @pytest.mark.asyncio
    async def test_done_event_carries_file_size_and_filename(self, media_file, monkeypatch):
        bot = _bot()
        _, done = await _run(monkeypatch, bot, "video", _status("job.mp4"))
        kwargs = done.call_args.kwargs
        assert kwargs["file_size_bytes"] == 2048
        assert kwargs["filename"] == "job.mp4"

    @pytest.mark.asyncio
    async def test_error_status_is_reported_not_uploaded(self, media_file, monkeypatch):
        bot = _bot()
        monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "j", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status",
                            AsyncMock(return_value={"status": "error", "error": "boom"}))
        error = AsyncMock()
        monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
        monkeypatch.setattr(download_flow.event_client, "send_download_error", error)

        await download_flow.run_download(
            context=_context(bot), chat_id=1, status_message_id=2,
            url="https://youtube.com/x", info=INFO, fmt="video", format_id=None,
            height=None, user_id=3, username="t", started_at=0.0,
        )
        assert error.call_args.kwargs["error_message"] == "boom"
        assert bot.send_video.call_count == 0


class TestChoiceResolution:
    def test_mp3_action(self):
        assert download_flow.resolve_choice(INFO, "mp3") == ("audio", None, None)

    def test_best_action(self):
        assert download_flow.resolve_choice(INFO, "best") == ("video", None, None)

    def test_format_id_action_keeps_height(self):
        assert download_flow.resolve_choice(INFO, "137") == ("video", "137", 1080)

    def test_unknown_format_id_is_still_forwarded(self):
        # An expired session must not silently download the wrong thing.
        assert download_flow.resolve_choice(INFO, "999") == ("video", "999", None)

    def test_quality_labels(self):
        assert download_flow.quality_label("video", "137", 1080, INFO) == "1080p"
        assert download_flow.quality_label("video", None, None, INFO) == "best"
        assert download_flow.quality_label("audio", None, None, INFO) == "MP3 129kbps"
        assert download_flow.quality_label("audio", None, None, {}) == "MP3"


class TestCallbackParsing:
    def test_new_scheme(self):
        assert sessions.parse_callback_data("dl:42:abcdef123456:137") == (
            42, "abcdef123456", "137")

    def test_legacy_fmt_scheme(self):
        assert sessions.parse_callback_data("fmt:42:abcdef123456:video") == (
            42, "abcdef123456", "video")

    def test_legacy_qty_scheme(self):
        assert sessions.parse_callback_data("qty:42:abcdef123456:best") == (
            42, "abcdef123456", "best")

    def test_malformed(self):
        assert sessions.parse_callback_data("") is None
        assert sessions.parse_callback_data("dl:notanint:hash:137") is None
        assert sessions.parse_callback_data("dl:42") is None


# ---------------------------------------------------------------------------
# One message, three stages
# ---------------------------------------------------------------------------

def _progress_status(percent, **extra):
    """A non-terminal engine status with real progress data."""
    base = {
        "status": "downloading",
        "progress": {
            "percent": percent,
            "downloaded_bytes": 21_000_000,
            "total_bytes": 33_659_750,
            "speed": 3_355_443.2,
            "eta": 42,
        },
    }
    base.update(extra)
    return base


class TestOneMessageFlow:
    @pytest.mark.asyncio
    async def test_caption_then_caption_then_media(self, media_file, monkeypatch):
        """The card is edited in place from start to finish."""
        bot = _bot()
        await _run(monkeypatch, bot, "video", _status("job.mp4"),
                   format_id="137", height=1080)

        # The finished file replaced the card's media instead of being sent as a
        # third message.
        assert bot.edit_message_media.await_count == 1
        assert bot.send_video.call_count == 0
        assert bot.send_document.call_count == 0
        # ...and the progress captions went to the same message.
        captions = [c.kwargs["caption"] for c in bot.edit_message_caption.await_args_list]
        assert any("Starting" in _plain(c) for c in captions)
        assert all(c.kwargs["message_id"] == 2
                   for c in bot.edit_message_caption.await_args_list)
        assert all(c.kwargs["message_id"] == 2
                   for c in bot.edit_message_media.await_args_list)

    @pytest.mark.asyncio
    async def test_final_media_is_a_video_with_the_file_caption(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "video", _status("job.mp4"), height=720)
        media = bot.edit_message_media.await_args.kwargs["media"]
        assert media.type == "video"
        # The human name travels on the attached upload, not on the media object.
        assert media.media.filename == "job.mp4"
        caption = _plain(media.caption)
        assert "Artemis II to the Moon" in caption
        assert "2.0KB" in caption
        assert "10:35" in caption
        assert "720p" in caption

    @pytest.mark.asyncio
    async def test_audio_finishes_as_audio_with_tags(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "audio", _status("job.mp3"))
        media = bot.edit_message_media.await_args.kwargs["media"]
        assert media.type == "audio"
        assert media.title == "Artemis II to the Moon"
        assert media.performer == "NASA"
        assert bot.send_audio.call_count == 0

    @pytest.mark.asyncio
    async def test_progress_removes_the_keyboard(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "video", _status("job.mp4"))
        # The button row is dropped as soon as the job starts.
        assert bot.edit_message_reply_markup.await_count == 1
        markup = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        assert tuple(markup.inline_keyboard) == ()
        # Progress captions never re-add a keyboard.
        for call in bot.edit_message_caption.await_args_list:
            assert call.kwargs.get("reply_markup") is None

    @pytest.mark.asyncio
    async def test_media_edit_failure_still_delivers_the_file(self, media_file, monkeypatch):
        """An uneditable card must not swallow the download."""
        bot = _bot(in_place=False)
        await _run(monkeypatch, bot, "video", _status("job.mp4"), height=720)
        assert bot.send_video.await_count == 1
        assert bot.send_video.await_args.kwargs["filename"] == "job.mp4"

    @pytest.mark.asyncio
    async def test_error_restores_the_format_keyboard(self, media_file, monkeypatch):
        bot = _bot()
        monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "j", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status",
                            AsyncMock(return_value={"status": "error", "error": "Video unavailable"}))
        monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
        monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())

        await download_flow.run_download(
            context=_context(bot), chat_id=1, status_message_id=2,
            url="https://youtube.com/x", info=INFO, fmt="video", format_id=None,
            height=None, user_id=3, username="t", started_at=0.0,
            url_hash="abcdef123456",
        )

        last = bot.edit_message_caption.await_args.kwargs
        caption = _plain(last["caption"])
        assert "Video unavailable" in caption
        assert "Download failed" in caption
        # The buttons come back, with the same callback data as the preview.
        markup = last["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "dl:2:abcdef123456:137" in datas
        assert all(len(d.encode()) <= 64 for d in datas)
        assert bot.send_video.call_count == 0

    @pytest.mark.asyncio
    async def test_error_without_a_hash_has_no_buttons(self, media_file, monkeypatch):
        bot = _bot()
        monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "j", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status",
                            AsyncMock(return_value={"status": "error", "error": "boom"}))
        monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
        monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())

        await download_flow.run_download(
            context=_context(bot), chat_id=1, status_message_id=2,
            url="https://youtube.com/x", info=INFO, fmt="video", format_id=None,
            height=None, user_id=3, username="t", started_at=0.0,
        )
        assert bot.edit_message_caption.await_args.kwargs["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_job_flag_is_cleared_when_the_download_ends(self, media_file, monkeypatch):
        bot = _bot()
        entry = {"job_active": True}
        await _run(monkeypatch, bot, "video", _status("job.mp4"), entry=entry)
        assert entry["job_active"] is False


class TestCallbackGuard:
    @pytest.mark.asyncio
    async def test_second_format_while_downloading_is_refused(self, monkeypatch):
        """A second tap on the same card must not start a second job."""
        monkeypatch.setattr(sessions, "evict_stale", lambda: None)
        entry = {
            "url": "https://youtube.com/x", "chat_id": 1, "user_id": 3,
            "username": "t", "info": INFO, "message_id": 2, "photo": True,
            "job_active": True, "created": 0.0,
        }
        monkeypatch.setattr(
            sessions, "find", lambda query, message_id, uhash: entry
        )
        created = []
        monkeypatch.setattr(callbacks.asyncio, "create_task", created.append)

        query = MagicMock()
        query.data = "dl:2:abcdef123456:137"
        query.message = MagicMock()
        query.message.chat.id = 1
        query.message.message_id = 2
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        await callbacks.on_format_button(update, _context(_bot()))

        assert created == []
        # Answered politely instead of silently doing nothing, and answered
        # exactly once (Telegram rejects a second answer for the same query).
        assert query.answer.await_count == 1
        text = " ".join(str(part) for part in query.answer.await_args.args)
        assert "Already downloading" in text
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_tap_starts_a_job_and_marks_the_card_busy(self, monkeypatch):
        monkeypatch.setattr(sessions, "evict_stale", lambda: None)
        entry = {
            "url": "https://youtube.com/x", "chat_id": 1, "user_id": 3,
            "username": "t", "info": INFO, "message_id": 2, "photo": True,
            "job_active": False, "created": 0.0,
        }
        monkeypatch.setattr(
            sessions, "find", lambda query, message_id, uhash: entry
        )
        created = []

        def _capture(coro):
            # Close it instead of awaiting: the job itself is covered elsewhere.
            coro.close()
            created.append(coro)

        monkeypatch.setattr(callbacks.asyncio, "create_task", _capture)

        query = MagicMock()
        query.data = "dl:2:abcdef123456:137"
        query.message = MagicMock()
        query.message.chat.id = 1
        query.message.message_id = 2
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        await callbacks.on_format_button(update, _context(_bot()))

        assert len(created) == 1
        assert entry["job_active"] is True


class TestProgressThrottleInFlow:
    @pytest.mark.asyncio
    async def test_caption_is_not_edited_on_every_poll(self, media_file, monkeypatch):
        """Polls one second apart produce one caption edit, not one per poll."""
        bot = _bot()
        polls = [
            _progress_status(10.0),
            _progress_status(11.0),   # inside the 3s window -> dropped
            _progress_status(30.0),   # later and +20% -> sent
            _status("job.mp4"),
        ]
        monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "j", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status", AsyncMock(side_effect=polls))
        monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
        monkeypatch.setattr(download_flow.event_client, "send_download_done", AsyncMock())
        monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())
        progress_events = AsyncMock()
        monkeypatch.setattr(download_flow.event_client, "send_progress", progress_events)

        # A deterministic clock: every poll advances it by one second while the
        # throttle sees a 4s gap between the two captions it keeps.
        clock = [1000.0]
        real_monotonic = download_flow.progress_card.time.monotonic
        monkeypatch.setattr(download_flow.progress_card.time, "monotonic", lambda: clock[0])
        real_sleep = download_flow.asyncio.sleep
        gaps = iter([1.0, 1.0, 4.0, 1.0])

        async def _tick(delay, *a, **k):
            clock[0] += next(gaps, 1.0)

        monkeypatch.setattr(download_flow.asyncio, "sleep", _tick)
        try:
            target = media_file / "job.mp4"
            target.write_bytes(b"x" * 2048)
            await download_flow.run_download(
                context=_context(bot), chat_id=1, status_message_id=2,
                url="https://youtube.com/x", info=INFO, fmt="video",
                format_id=None, height=None, user_id=3, username="t",
                started_at=0.0,
            )
        finally:
            monkeypatch.setattr(download_flow.asyncio, "sleep", real_sleep)
            monkeypatch.setattr(
                download_flow.progress_card.time, "monotonic", real_monotonic
            )

        captions = [_plain(c.kwargs["caption"])
                    for c in bot.edit_message_caption.await_args_list]
        progress_captions = [c for c in captions if "%" in c]
        # Three polls produced two captions: the 11% one fell inside the 3s
        # window after the 10% caption and was dropped (not queued up).
        assert len(progress_captions) == 2
        assert "10%" in progress_captions[0]
        assert "30%" in progress_captions[1]
        assert not any("11%" in c for c in progress_captions)
        # The dashboard still receives every progress event, throttled or not.
        assert progress_events.await_count == 3


class TestErrorOnUneditableCard:
    @pytest.mark.asyncio
    async def test_error_lands_in_one_message_with_buttons(self, media_file, monkeypatch):
        """When the card is gone, the error is sent once, with its buttons."""
        from telegram.error import BadRequest
        bot = _bot()
        bot.edit_message_caption = AsyncMock(side_effect=BadRequest("Message can't be edited"))
        monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "j", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status",
                            AsyncMock(return_value={"status": "error", "error": "gone"}))
        monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
        monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())

        await download_flow.run_download(
            context=_context(bot), chat_id=1, status_message_id=2,
            url="https://youtube.com/x", info=INFO, fmt="video", format_id=None,
            height=None, user_id=3, username="t", started_at=0.0,
            url_hash="abcdef123456",
        )

        # Each caption write fell back once; the buttons ride on the last one.
        assert bot.send_message.await_count >= 1
        with_buttons = [
            c for c in bot.send_message.await_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        assert len(with_buttons) == 1
        datas = [b.callback_data for row in with_buttons[0].kwargs["reply_markup"].inline_keyboard
                 for b in row]
        assert "dl:2:abcdef123456:137" in datas
