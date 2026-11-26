"""Tests for the one-message card: edit classification, fallback, media swap.

A fake ``Bot`` records the calls, so the test asserts the *sequence* of edits a
real chat would see: caption -> caption -> editMessageMedia.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import card_message  # noqa: E402
from card_message import (  # noqa: E402
    InputMediaAudio,
    InputMediaDocument,
    InputMediaVideo,
)

INFO = {
    "title": "Artemis II to the Moon",
    "formats": [{"id": "137", "label": "1080p", "height": 1080,
                 "button_label": "1080p · 32.1MB"}],
    "audio": {"size_bytes": 10_271_496},
}


class RecordingBot:
    """Records every Telegram call in order and can be told to fail on one."""

    def __init__(self):
        self.calls = []
        self.fail = {}

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        error = self.fail.get(name)
        if error:
            raise error

    async def edit_message_caption(self, **kwargs):
        self._record("edit_message_caption", kwargs)
        return MagicMock()

    async def edit_message_text(self, **kwargs):
        self._record("edit_message_text", kwargs)
        return MagicMock()

    async def edit_message_media(self, **kwargs):
        self._record("edit_message_media", kwargs)
        return MagicMock()

    async def edit_message_reply_markup(self, **kwargs):
        self._record("edit_message_reply_markup", kwargs)
        return MagicMock()

    async def send_message(self, **kwargs):
        self._record("send_message", kwargs)
        return MagicMock()

    def names(self):
        return [name for name, _ in self.calls]


def _card(bot, **kwargs):
    kwargs.setdefault("on_fallback", None)
    return card_message.CardMessage(bot, chat_id=1, message_id=2, **kwargs)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class TestClassify:
    def test_not_modified(self):
        from telegram.error import BadRequest
        exc = BadRequest("Message is not modified: specified new message content "
                         "and reply markup are exactly the same")
        assert card_message.classify(exc) == "not_modified"

    def test_cannot_be_edited(self):
        from telegram.error import BadRequest
        assert card_message.classify(
            BadRequest("Message can't be edited")) == "unavailable"

    def test_message_to_edit_not_found(self):
        from telegram.error import BadRequest
        assert card_message.classify(
            BadRequest("Message to edit not found")) == "unavailable"

    def test_no_text_to_edit(self):
        from telegram.error import BadRequest
        assert card_message.classify(
            BadRequest("Bad Request: there is no text in the message to edit")
        ) == "unavailable"

    def test_other_errors_are_reported(self):
        from telegram.error import BadRequest
        assert card_message.classify(
            BadRequest("MEDIA_CAPTION_TOO_LONG")) == "error"


# ---------------------------------------------------------------------------
# Style comparison: photo card vs text card
# ---------------------------------------------------------------------------

class TestCardStyles:
    @pytest.mark.asyncio
    async def test_photo_card_edits_the_caption(self):
        bot = RecordingBot()
        card = _card(bot)
        result = await card.edit_caption("hello")
        assert result.ok
        assert bot.names() == ["edit_message_caption"]
        assert bot.calls[0][1]["caption"] == "hello"

    @pytest.mark.asyncio
    async def test_text_card_edits_the_text(self):
        bot = RecordingBot()
        card = _card(bot, photo=False)
        await card.edit_caption("hello")  # same API, right method underneath
        assert bot.names() == ["edit_message_text"]

    @pytest.mark.asyncio
    async def test_progress_drops_the_keyboard(self):
        bot = RecordingBot()
        card = _card(bot)
        await card.edit_caption("62%", reply_markup=None)
        assert bot.calls[0][1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_clear_keyboard_sends_an_empty_markup(self):
        bot = RecordingBot()
        card = _card(bot)
        await card.clear_keyboard()
        assert bot.names() == ["edit_message_reply_markup"]
        assert tuple(bot.calls[0][1]["reply_markup"].inline_keyboard) == ()


# ---------------------------------------------------------------------------
# Not-modified / failure handling
# ---------------------------------------------------------------------------

class TestFailures:
    @pytest.mark.asyncio
    async def test_not_modified_is_not_an_error(self, caplog):
        from telegram.error import BadRequest
        bot = RecordingBot()
        bot.fail["edit_message_caption"] = BadRequest(
            "Message is not modified: specified new message content and reply "
            "markup are exactly the same")
        card = _card(bot)
        with caplog.at_level("DEBUG"):
            result = await card.edit_caption("same")
        assert not result.ok
        assert result.ignored
        assert not result.unrecoverable
        # No fallback message, and nothing logged above DEBUG.
        assert "send_message" not in bot.names()
        assert not [r for r in caplog.records if r.levelname in ("ERROR", "WARNING")]

    @pytest.mark.asyncio
    async def test_uneditable_message_falls_back_to_a_new_message(self):
        from telegram.error import BadRequest
        bot = RecordingBot()
        bot.fail["edit_message_caption"] = BadRequest("Message can't be edited")
        seen: list[str] = []

        async def fallback(text: str) -> None:
            seen.append(text)

        card = _card(bot, on_fallback=fallback)
        result = await card.edit_caption("62%")
        assert result.unrecoverable
        assert len(seen) == 1
        assert "62%" in seen[0]
        # Once broken, further edits go straight to the fallback.
        await card.edit_caption("70%")
        assert len(seen) == 2
        assert "edit_message_caption" not in bot.names()[1:]

    @pytest.mark.asyncio
    async def test_unexpected_error_does_not_raise(self):
        from telegram.error import BadRequest
        bot = RecordingBot()
        bot.fail["edit_message_caption"] = BadRequest("MEDIA_CAPTION_TOO_LONG")
        card = _card(bot)
        result = await card.edit_caption("x")
        assert not result.ok
        assert not result.unrecoverable
        assert not card.broken

    @pytest.mark.asyncio
    async def test_fallback_failure_is_swallowed(self):
        from telegram.error import BadRequest
        bot = RecordingBot()
        bot.fail["edit_message_caption"] = BadRequest("Message can't be edited")
        bot.fail["send_message"] = RuntimeError("network down")
        card = _card(bot, on_fallback=None)  # no hook at all
        result = await card.edit_caption("62%")
        assert result.unrecoverable  # caller still knows to send the file itself


# ---------------------------------------------------------------------------
# Media replacement
# ---------------------------------------------------------------------------

class TestMedia:
    @pytest.mark.asyncio
    async def test_edit_media_becomes_a_document_edit(self, tmp_path):
        bot = RecordingBot()
        card = _card(bot)
        path = tmp_path / "video.mkv"
        path.write_bytes(b"x")
        media = card_message.build_input_media(
            path, fmt="video", filename="video.mkv", caption="done")
        result = await card.edit_media(media)
        assert result.ok
        assert bot.names() == ["edit_message_media"]
        assert bot.calls[0][1]["media"].type == "document"

    @pytest.mark.asyncio
    async def test_media_edit_failure_marks_the_card_broken(self):
        from telegram.error import BadRequest
        bot = RecordingBot()
        bot.fail["edit_message_media"] = BadRequest("Message can't be edited")
        card = _card(bot)
        result = await card.edit_media(MagicMock())
        assert result.unrecoverable
        assert card.broken


# ---------------------------------------------------------------------------
# InputMedia selection
# ---------------------------------------------------------------------------

class TestInputMedia:
    def test_mp4_becomes_input_media_video(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"x")
        media = card_message.build_input_media(
            path, fmt="video", filename="clip.mp4", caption="c",
            duration=10, width=1280, height=720)
        assert isinstance(media, InputMediaVideo)
        assert media.type == "video"
        assert media.width == 1280
        assert media.height == 720
        assert media.duration == 10
        assert media.supports_streaming is True
        assert media.caption == "c"

    def test_mp3_becomes_input_media_audio_with_tags(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"x")
        media = card_message.build_input_media(
            path, fmt="audio", filename="song.mp3", caption="c",
            duration=200, title="Song", performer="Artist")
        assert isinstance(media, InputMediaAudio)
        assert media.type == "audio"
        assert media.title == "Song"
        assert media.performer == "Artist"
        assert media.duration == 200

    def test_audio_format_wins_over_the_extension(self, tmp_path):
        # An odd container must still arrive as audio when the job asked for it.
        path = tmp_path / "song.m4a"
        path.write_bytes(b"x")
        media = card_message.build_input_media(
            path, fmt="audio", filename="song.m4a", caption="c")
        assert media.type == "audio"

    def test_other_containers_become_documents(self, tmp_path):
        path = tmp_path / "clip.webm"
        path.write_bytes(b"x")
        media = card_message.build_input_media(
            path, fmt="video", filename="clip.webm", caption="c")
        assert isinstance(media, InputMediaDocument)
        assert media.type == "document"


# ---------------------------------------------------------------------------
# Retry keyboard
# ---------------------------------------------------------------------------

class TestRetryKeyboard:
    def test_retry_keyboard_reuses_the_preview_callback_data(self):
        import download_card
        markup = card_message.retry_keyboard(INFO, 42, "abcdef123456")
        preview = download_card.build_format_keyboard(INFO, 42, "abcdef123456")
        assert markup.to_dict() == preview.to_dict()

    def test_retry_keyboard_fits_the_callback_limit(self):
        markup = card_message.retry_keyboard(INFO, 2**31 - 1, "f" * 12)
        for row in markup.inline_keyboard:
            for button in row:
                assert len(str(button.callback_data).encode()) <= 64


# ---------------------------------------------------------------------------
# Broken card: exactly one message per event
# ---------------------------------------------------------------------------

class TestBrokenCard:
    @pytest.mark.asyncio
    async def test_broken_card_edits_go_to_the_hook_only(self):
        from telegram.error import BadRequest
        bot = RecordingBot()
        bot.fail["edit_message_caption"] = BadRequest("Message can't be edited")
        seen = []

        async def fallback(text):
            seen.append(text)

        card = _card(bot, on_fallback=fallback)
        await card.edit_caption("one")
        await card.edit_caption("two")
        # One hook call per edit, and no further doomed edit attempts.
        assert seen == ["one", "two"]
        assert bot.names().count("edit_message_caption") == 1
