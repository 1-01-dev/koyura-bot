"""Tests for the bot's download flow: naming, caption, upload and events.

The engine and Telegram are both stubbed; the point is that a finished job is
uploaded under a human name with the new caption and that the dashboard event
keeps carrying title/format/quality/file_size_bytes.

The second half tests what the owner sees in the chat: **one stage — one
message**. A recording ``Bot`` captures the exact order of Telegram calls
(``send → delete → edits → send → delete``), so "the previous stage is replaced,
not stacked on" is an assertion rather than a hope.
"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import bot_settings  # noqa: E402
from handlers import config, download_flow, sessions, stages  # noqa: E402

# The tests below read a percentage off the card. ``conftest`` primes the panel
# defaults, whose built-in ``progress`` body draws the bar alone; a deployment
# that wants the number writes it into its own template instead.
SHOW_PERCENT_BODY = "*@title*\n@stage\n@bar @percent"


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    """The poll loop sleeps 2s between checks; tests never need to wait."""
    real_sleep = download_flow.asyncio.sleep
    async def _fast(delay, *args, **kwargs):
        await real_sleep(0)
    monkeypatch.setattr(download_flow.asyncio, "sleep", _fast)


@pytest.fixture(autouse=True)
def _show_the_percentage(monkeypatch):
    """Prime a panel answer whose ``progress`` template prints the number.

    ``test_progress_card.py`` owns the built-in default (title, stage, bar);
    this module only needs the percentage to be on the caption it reads.
    """
    base = bot_settings.defaults()
    shown = bot_settings.Settings(
        languages=base.languages,
        default_language=base.default_language,
        source="panel",
        templates={"progress": {"en": SHOW_PERCENT_BODY}},
    )
    # Patch ``get`` itself, not just the cache: a test that fakes the clock
    # (``time.monotonic``) would otherwise make the cached answer look stale and
    # reach for the dashboard. ``monkeypatch`` undoes this after the test.
    monkeypatch.setattr(bot_settings, "get", AsyncMock(return_value=shown))
    # Rendering is synchronous and reads the cached snapshot (``cached()``), so
    # the answer has to be in the cache as well as behind ``get``.
    bot_settings.prime(shown)
    yield
    bot_settings.reset_cache()


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


class RecordingBot:
    """A fake Telegram bot that records every call, in order.

    ``calls`` is a list of ``(name, kwargs)``; ``fail`` makes a named method
    raise, which is how the "deletion refused" and "upload refused" behaviours
    are exercised. Every successful send returns a message whose id comes from
    ``_next_id``, so the ids in the recorded sequence are predictable.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, BaseException] = {}
        self._next_id = 100

    def _record(self, name: str, kwargs: dict, *, returns_message: bool = False):
        self.calls.append((name, kwargs))
        error = self.fail.get(name)
        if error is not None:
            raise error
        if returns_message:
            return MagicMock(message_id=self._next_message_id())
        return True

    def _next_message_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # Only the ``send_*`` methods post a message (and so allocate an id); an
    # edit or a delete returns something the flows never inspect.
    async def send_message(self, **kwargs):
        return self._record("send_message", kwargs, returns_message=True)

    async def send_photo(self, **kwargs):
        return self._record("send_photo", kwargs, returns_message=True)

    async def send_video(self, **kwargs):
        return self._record("send_video", kwargs, returns_message=True)

    async def send_audio(self, **kwargs):
        return self._record("send_audio", kwargs, returns_message=True)

    async def send_document(self, **kwargs):
        return self._record("send_document", kwargs, returns_message=True)

    async def delete_message(self, **kwargs):
        return self._record("delete_message", kwargs)

    async def edit_message_text(self, **kwargs):
        return self._record("edit_message_text", kwargs)

    async def edit_message_caption(self, **kwargs):
        return self._record("edit_message_caption", kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        return self._record("edit_message_reply_markup", kwargs)

    # -- helpers ----------------------------------------------------------

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def of(self, name: str) -> list[dict]:
        return [kwargs for called, kwargs in self.calls if called == name]

    def stage_script(self) -> list[str]:
        """The sequence collapsed to the operations the owner actually sees."""
        return [
            name
            for name in self.names()
            if name in ("send_message", "send_photo", "send_video", "send_audio",
                        "send_document", "delete_message", "edit_message_text",
                        "edit_message_caption", "edit_message_reply_markup")
        ]


def _bot(in_place: bool = True):
    """A recording bot; ``in_place=False`` keeps the old call shape available."""
    return RecordingBot()


def _context(bot):
    context = MagicMock()
    context.bot = bot
    return context


def _status(name, **extra):
    return {"status": "done", "filename": name, "file_size_bytes": 2048,
            "duration": 635, "height": 720, "width": 1280, **extra}


def _write_artifact(status) -> None:
    name = status.get("filename") or "job.mp4"
    target = Path(config.DOWNLOADS_PATH) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * 2048)


async def _run(monkeypatch, bot, fmt, status, *, format_id=None, height=None,
               tracker=None, entry=None, url_hash=None, poll=None):
    # Write the finished artifact the engine would have left on the volume.
    _write_artifact(status)
    monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "job1", "status": "downloading"}))
    if isinstance(poll, list):
        monkeypatch.setattr(download_flow, "poll_status", AsyncMock(side_effect=poll))
    else:
        monkeypatch.setattr(download_flow, "poll_status", AsyncMock(return_value=poll or status))
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
        tracker=tracker,
        entry=entry,
        url_hash=url_hash,
    )
    return started, done


def _delivered(bot) -> dict:
    """The file as the user receives it, read off the upload call."""
    for name in ("send_video", "send_audio", "send_document"):
        calls = bot.of(name)
        if calls:
            return calls[-1]
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
        assert bot.of("send_video") == []

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
        assert bot.of("send_video") == []


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
# One stage, one message
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
    async def test_preview_is_deleted_then_progress_then_file(self, media_file, monkeypatch):
        """send → delete → edits → send → delete: the canonical chain."""
        bot = _bot()
        await _run(
            monkeypatch, bot, "video", _status("job.mp4"),
            format_id="137", height=1080,
            poll=[_progress_status(42.0), _status("job.mp4")],
        )

        script = bot.stage_script()
        assert script[0] == "send_message"      # "Starting download…"
        assert script[1] == "delete_message"    # the preview card goes
        assert script[-2] == "send_video"       # the file arrives as its own message
        assert script[-1] == "delete_message"   # the progress message goes
        # Everything between the two deletes is an edit of one message.
        middle = script[2:-2]
        assert middle == ["edit_message_text"], middle

        # The same message was edited throughout; the file got a new id.
        edited = {kwargs["message_id"] for kwargs in bot.of("edit_message_text")}
        progress_id = 101  # first id the recording bot hands out
        assert edited == {progress_id}

        # Both deletions point at a message that was once on screen.
        deleted = [kwargs["message_id"] for kwargs in bot.of("delete_message")]
        assert deleted == [2, progress_id]

        # Both deletions point at a message that was once on screen.
        deleted = [kwargs["message_id"] for kwargs in bot.of("delete_message")]
        assert deleted == [2, progress_id]

    @pytest.mark.asyncio
    async def test_the_file_is_never_deleted(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "video", _status("job.mp4"), height=720)

        file_ids = {kwargs["message_id"] for kwargs in bot.of("delete_message")}
        delivered_id = 103  # second message posted in this scenario
        assert delivered_id not in file_ids
        assert bot.of("send_video"), "the file has to be posted"

    @pytest.mark.asyncio
    async def test_progress_never_posts_a_new_message(self, media_file, monkeypatch):
        """Ticks edit the progress message; only the file is a new message."""
        bot = _bot()
        polls = [_progress_status(10.0), _progress_status(55.0), _status("job.mp4")]
        monkeypatch.setattr(download_flow, "start_download", AsyncMock(return_value={"job_id": "j", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status", AsyncMock(side_effect=polls))
        for name in ("send_download_start", "send_download_done", "send_download_error", "send_progress"):
            monkeypatch.setattr(download_flow.event_client, name, AsyncMock())
        (media_file / "job.mp4").write_bytes(b"x" * 2048)

        await download_flow.run_download(
            context=_context(bot), chat_id=1, status_message_id=2,
            url="https://youtube.com/x", info=INFO, fmt="video", format_id=None,
            height=None, user_id=3, username="t", started_at=0.0,
        )

        # One status message, then the file: no third message in between.
        assert len(bot.of("send_message")) == 1
        assert len(bot.of("send_video")) == 1
        captions = [_plain(kwargs["text"]) for kwargs in bot.of("edit_message_text")]
        assert any("10%" in caption for caption in captions)

    @pytest.mark.asyncio
    async def test_progress_edits_drop_the_keyboard(self, media_file, monkeypatch):
        bot = _bot()
        await _run(monkeypatch, bot, "video", _status("job.mp4"))
        for kwargs in bot.of("edit_message_text"):
            assert kwargs.get("reply_markup") is None

    @pytest.mark.asyncio
    async def test_error_replaces_progress_with_a_ready_keyboard(self, media_file, monkeypatch):
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

        # The error is its own message and the progress message is retired.
        assert bot.names()[-3:] == [
            "send_message", "edit_message_reply_markup", "delete_message"
        ]
        error = bot.of("send_message")[-1]
        caption = _plain(error["text"])
        assert "Video unavailable" in caption
        assert "Download failed" in caption
        # The buttons are attached to the message that shows them.
        markup = bot.of("edit_message_reply_markup")[-1]["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        error_id = 102  # second message the recording bot hands out
        assert f"dl:{error_id}:abcdef123456:137" in datas
        assert all(len(data.encode()) <= 64 for data in datas)
        assert bot.of("send_video") == []

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
        assert bot.of("edit_message_reply_markup") == []
        assert bot.of("send_message")[-1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_job_flag_is_cleared_when_the_download_ends(self, media_file, monkeypatch):
        bot = _bot()
        entry = {"job_active": True}
        await _run(monkeypatch, bot, "video", _status("job.mp4"), entry=entry)
        assert entry["job_active"] is False


# ---------------------------------------------------------------------------
# Best-effort deletion and the send-before-delete rule
# ---------------------------------------------------------------------------

class TestStageDiscipline:
    @pytest.mark.asyncio
    async def test_undeletable_message_is_not_fatal(self, media_file, monkeypatch):
        """A 48-hour-old or already-deleted message is a DEBUG line, not an error."""
        from telegram.error import BadRequest

        bot = _bot()
        bot.fail["delete_message"] = BadRequest("Message to delete not found")
        await _run(monkeypatch, bot, "video", _status("job.mp4"))

        # The flow ran to the end: the file still arrived.
        assert bot.of("send_video"), "the download must survive a refused delete"
        assert download_flow.stats.COUNTERS["downloads"] >= 1
        # Every delete was attempted, none of them raised.
        assert len(bot.of("delete_message")) == 2

    @pytest.mark.asyncio
    async def test_deleted_by_user_is_not_fatal(self, media_file, monkeypatch):
        from telegram.error import BadRequest

        bot = _bot()
        bot.fail["delete_message"] = BadRequest(
            "Bad Request: message can't be deleted for everyone"
        )
        await _run(monkeypatch, bot, "video", _status("job.mp4"))
        assert bot.of("send_video")

    @pytest.mark.asyncio
    async def test_a_failed_new_stage_keeps_the_old_message(self, media_file, monkeypatch):
        """Send first, delete second: a refused send must not lose the card."""
        bot = _bot()
        bot.fail["send_message"] = RuntimeError("too many requests")
        await _run(monkeypatch, bot, "video", _status("job.mp4"))

        # The "Starting download…" post failed, so that stage was written into
        # the message already on screen instead of deleting it and failing.
        assert bot.calls[0][0] == "send_message"
        # The preview card of the button flow is a photo, so the fallback is a
        # caption edit of the card that stayed on screen.
        assert bot.calls[1][0] in ("edit_message_text", "edit_message_caption")
        assert bot.calls[1][1]["message_id"] == 2
        text = bot.calls[1][1].get("text") or bot.calls[1][1].get("caption")
        assert text is not None and "Starting" in _plain(text)
        # The progress message *is* the original card here (the fallback edit
        # kept its id), so that is the one retired once the file is posted.
        deleted = [kwargs["message_id"] for kwargs in bot.of("delete_message")]
        assert deleted == [2], deleted
        # The download itself still ran and the file was posted.
        assert bot.of("send_video")

    @pytest.mark.asyncio
    async def test_a_failed_upload_keeps_the_progress_and_shows_the_error(self, media_file, monkeypatch):
        bot = _bot()
        bot.fail["send_video"] = RuntimeError("upload refused")
        await _run(monkeypatch, bot, "video", _status("job.mp4"))

        # The upload never succeeded, so the progress message was not retired
        # for the file; the error card replaced it instead.
        attempts = bot.of("send_video")
        assert len(attempts) == 2, "the upload is retried once"
        deleted = [kwargs["message_id"] for kwargs in bot.of("delete_message")]
        assert deleted == [2, 101], deleted
        errors = [kwargs for kwargs in bot.of("send_message")
                  if "Download failed" in _plain(kwargs["text"])]
        assert len(errors) == 1
        assert "Failed to upload file to Telegram." in _plain(errors[0]["text"])


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

        captions = [_plain(kwargs["text"]) for kwargs in bot.of("edit_message_text")]
        progress_captions = [c for c in captions if "%" in c]
        # Three polls produced two captions: the 11% one fell inside the 3s
        # window after the 10% caption and was dropped (not queued up).
        assert len(progress_captions) == 2
        assert "10%" in progress_captions[0]
        assert "30%" in progress_captions[1]
        assert not any("11%" in c for c in progress_captions)
        # The dashboard still receives every progress event, throttled or not.
        assert progress_events.await_count == 3
        # And every edit went to the same message.
        assert len({k["message_id"] for k in bot.of("edit_message_text")}) == 1


class TestErrorOnUneditableCard:
    @pytest.mark.asyncio
    async def test_error_still_arrives_when_the_status_message_is_gone(self, media_file, monkeypatch):
        """A user who deleted the progress message still gets the error."""
        from telegram.error import BadRequest
        bot = _bot()
        bot.fail["edit_message_text"] = BadRequest("Message to edit not found")
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

        # The error text ends up on screen exactly once, with its buttons.
        errors = [kwargs for kwargs in bot.of("send_message")
                  if "Download failed" in _plain(kwargs["text"])]
        assert len(errors) == 1
        markup = bot.of("edit_message_reply_markup")[-1]["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        error_id = 102  # second message the recording bot hands out
        assert f"dl:{error_id}:abcdef123456:137" in datas


# ---------------------------------------------------------------------------
# The stage helper itself
# ---------------------------------------------------------------------------

class TestStageTrackerUnit:
    @pytest.mark.asyncio
    async def test_replace_posts_then_deletes(self):
        bot = RecordingBot()
        tracker = stages.StageTracker(bot, 1, 2)
        await tracker.replace_text("next")
        assert bot.names() == ["send_message", "delete_message"]
        assert bot.of("delete_message")[0]["message_id"] == 2
        assert tracker.message_id == 101

    @pytest.mark.asyncio
    async def test_delete_failure_is_only_logged(self):
        from telegram.error import BadRequest

        bot = RecordingBot()
        bot.fail["delete_message"] = BadRequest("Message to delete not found")
        tracker = stages.StageTracker(bot, 1, 2)
        result = await tracker.replace_text("next")
        assert result.ok is True
        assert tracker.message_id == 101

    @pytest.mark.asyncio
    async def test_failed_send_falls_back_to_editing_the_old_message(self):
        bot = RecordingBot()
        bot.fail["send_message"] = RuntimeError("429")
        tracker = stages.StageTracker(bot, 1, 2)
        result = await tracker.replace_text("next")
        assert result.ok is True
        assert bot.of("delete_message") == []
        assert bot.of("edit_message_text")[0]["message_id"] == 2
        assert tracker.message_id == 2

    @pytest.mark.asyncio
    async def test_finish_with_file_keeps_progress_when_the_upload_fails(self):
        bot = RecordingBot()
        bot.fail["send_video"] = RuntimeError("nope")
        tracker = stages.StageTracker(bot, 1, 2)
        result = await tracker.finish_with_file(lambda: bot.send_video(chat_id=1))
        assert result.ok is False
        assert bot.of("delete_message") == []
        assert tracker.message_id == 2

    @pytest.mark.asyncio
    async def test_finish_with_file_deletes_progress_after_the_upload(self):
        bot = RecordingBot()
        tracker = stages.StageTracker(bot, 1, 2)
        result = await tracker.finish_with_file(lambda: bot.send_video(chat_id=1))
        assert result.ok is True
        assert bot.names() == ["send_video", "delete_message"]
        assert tracker.finished is True

    @pytest.mark.asyncio
    async def test_not_modified_edit_is_not_a_failure(self):
        from telegram.error import BadRequest

        bot = RecordingBot()
        bot.fail["edit_message_text"] = BadRequest("Message is not modified")
        tracker = stages.StageTracker(bot, 1, 2)
        result = await tracker.edit("same")
        assert result.ok is True
        assert bot.of("send_message") == []

    @pytest.mark.asyncio
    async def test_caption_edit_is_used_for_a_photo_stage(self):
        bot = RecordingBot()
        tracker = stages.StageTracker(bot, 1, 2, photo=True)
        await tracker.edit("later")
        assert bot.names() == ["edit_message_caption"]

    @pytest.mark.asyncio
    async def test_delete_quietly_tolerates_everything(self):
        from telegram.error import BadRequest

        bot = RecordingBot()
        bot.fail["delete_message"] = BadRequest("message can't be deleted")
        assert await stages.delete_quietly(bot, 1, 2) is False
        assert await stages.delete_quietly(bot, 1, None) is False
