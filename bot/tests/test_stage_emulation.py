"""Emulate the owner's scenarios and assert the exact Telegram call sequence.

There is **no real Telegram account** in this test: a fake ``Bot`` records every
call and the engine client is patched per scenario, so the sequence the owner
sees in ``@KoyuraBot`` is verified without touching the live bot.

Running it with ``-s`` prints the log quoted in the pull request:

    cd bot && python -m pytest tests/test_stage_emulation.py -s -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from handlers import callbacks, config, download_flow, sessions, url_flow  # noqa: E402
from koyura_client import KoyuraBusy, KoyuraError, KoyuraServiceDown  # noqa: E402
from tests.test_download_flow import RecordingBot, _plain  # noqa: E402

INFO = {
    "title": "Big Buck Bunny",
    "duration": 635,
    "extractor": "youtube",
    "channel": "Blender",
    "uploader": "Blender",
    "uploader_id": "@BlenderOfficial",
    "channel_follower_count": 1_260_000,
    "view_count": 23_594_224,
    "like_count": 110_575,
    "comment_count": 5_200,
    "categories": ["Film & Animation"],
    "upload_date": "20141110",
    "thumbnail": "https://cdn.example/thumb.jpg",
    "formats": [
        {"id": "137", "label": "1080p", "height": 1080, "button_label": "1080p · 301MB"},
        {"id": "22", "label": "720p", "height": 720, "button_label": "720p · 132MB"},
    ],
    "audio": {"abr": 129, "size_bytes": 10_271_496, "size_label": "9.8MB"},
}

URL = "https://youtube.com/watch?v=x"
HASH = sessions.url_hash(URL)
DONE_VIDEO = {
    "status": "done", "filename": "job.mp4", "file_size_bytes": 2_097_152,
    "duration": 635, "height": 1080, "width": 1920,
}
DONE_AUDIO = {
    "status": "done", "filename": "job.mp3", "file_size_bytes": 1_048_576,
    "duration": 635, "height": 720, "width": 1280,
}


def _describe(name: str, kwargs: dict) -> str:
    """One readable line per Telegram call, for the report log."""
    target = kwargs.get("message_id")
    if name.startswith("send_"):
        kind = name[len("send_"):]
        body = kwargs.get("caption") or kwargs.get("text") or ""
        if kind == "photo":
            return f"sendPhoto     -> NEW msg | caption {len(_plain(body))} chars"
        if kind in ("video", "audio", "document"):
            return f"send{kind.title():<10}-> NEW msg | {kwargs.get('filename')}"
        first = _plain(body).splitlines()[0] if _plain(body).strip() else ""
        return f'sendMessage   -> NEW msg | "{first}"'
    if name == "delete_message":
        return f"deleteMessage    msg {target}"
    if name == "edit_message_text":
        text = _plain(str(kwargs.get("text") or ""))
        lines = [line for line in text.splitlines() if line.strip()]
        body = " / ".join(lines[1:] or lines[:1])
        return f"editText         msg {target} | {body[:58]}"
    if name == "edit_message_caption":
        caption = str(kwargs.get("caption") or "")
        return f"editCaption      msg {target} | {_plain(caption)[:58]}"
    if name == "edit_message_reply_markup":
        markup = kwargs.get("reply_markup")
        rows = getattr(markup, "inline_keyboard", []) or []
        datas = [b.callback_data for row in rows for b in row]
        return f"editMarkup       msg {target} | buttons {datas}"
    return name


def _log(title: str, bot: RecordingBot) -> str:
    lines = [f"--- {title} ---"]
    for index, (name, kwargs) in enumerate(bot.calls, start=1):
        lines.append(f"{index:2d}. {_describe(name, kwargs)}")
    return "\n".join(lines)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    real_sleep = download_flow.asyncio.sleep

    async def _instant(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(download_flow.asyncio, "sleep", _instant)
    monkeypatch.setattr(config, "PROGRESS_EDIT_INTERVAL", 0.0)
    for name in ("send_download_start", "send_download_done",
                 "send_download_error", "send_progress"):
        monkeypatch.setattr(download_flow.event_client, name, AsyncMock())
    sessions.forget_all()
    yield
    sessions.forget_all()


@pytest.fixture()
def downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    return tmp_path


async def _download(bot, *, fmt="video", height=None, format_id=None, url_hash=HASH):
    await download_flow.run_download(
        context=MagicMock(bot=bot),
        chat_id=1,
        status_message_id=11,   # the preview card the tap replaced
        url=URL,
        info=INFO,
        fmt=fmt,
        format_id=format_id,
        height=height,
        user_id=7,
        username="tester",
        started_at=0.0,
        url_hash=url_hash,
    )


class TestEmulatedScenarios:
    @pytest.mark.asyncio
    async def test_video_download_replaces_each_stage(self, downloads, monkeypatch):
        bot = RecordingBot()
        (downloads / "job.mp4").write_bytes(b"x" * 2048)
        polls = [
            {"status": "downloading", "progress": {
                "percent": 12.0, "downloaded_bytes": 25_000_000,
                "total_bytes": 209_715_200, "speed": 3_355_443.2, "eta": 55}},
            {"status": "downloading", "progress": {
                "percent": 61.0, "downloaded_bytes": 128_000_000,
                "total_bytes": 209_715_200, "speed": 3_355_443.2, "eta": 24}},
            DONE_VIDEO,
        ]
        monkeypatch.setattr(download_flow, "start_download",
                            AsyncMock(return_value={"job_id": "j1", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status", AsyncMock(side_effect=polls))

        await _download(bot, height=1080, format_id="137")

        print("\n" + _log("успешная загрузка видео (кнопка 1080p)", bot))
        assert bot.stage_script() == [
            "send_message",     # progress message ("Starting…")
            "delete_message",   # the preview card
            "edit_message_text",  # 12%
            "edit_message_text",  # 61%
            "send_video",       # the result
            "delete_message",   # the progress message
        ]
        assert bot.of("delete_message")[0]["message_id"] == 11

    @pytest.mark.asyncio
    async def test_mp3_download_uses_the_same_chain(self, downloads, monkeypatch):
        bot = RecordingBot()
        (downloads / "job.mp3").write_bytes(b"x" * 2048)
        monkeypatch.setattr(download_flow, "start_download",
                            AsyncMock(return_value={"job_id": "j2", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status", AsyncMock(return_value=DONE_AUDIO))

        await _download(bot, fmt="audio")

        print("\n" + _log("загрузка mp3", bot))
        assert bot.stage_script() == ["send_message", "delete_message",
                                      "send_audio", "delete_message"]

    @pytest.mark.asyncio
    async def test_engine_error_replaces_progress_with_the_error_card(self, downloads, monkeypatch):
        bot = RecordingBot()
        monkeypatch.setattr(download_flow, "start_download",
                            AsyncMock(return_value={"job_id": "j3", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status",
                            AsyncMock(return_value={"status": "error", "error": "Video unavailable"}))

        await _download(bot)

        print("\n" + _log("ошибка движка после старта (status: error)", bot))
        assert bot.stage_script() == [
            "send_message", "delete_message",          # progress replaced the card
            "send_message", "edit_message_reply_markup", "delete_message",  # error card
        ]
        assert "Video unavailable" in _plain(bot.of("send_message")[-1]["text"])

    @pytest.mark.asyncio
    async def test_queue_refusal_429_replaces_progress_with_the_error(self, downloads, monkeypatch):
        bot = RecordingBot()
        monkeypatch.setattr(download_flow, "start_download",
                            AsyncMock(side_effect=KoyuraBusy("busy", code="busy", retry_after=145)))

        await _download(bot)

        print("\n" + _log("отказ очереди (HTTP 429, KoyuraBusy)", bot))
        assert bot.stage_script() == [
            "send_message", "delete_message",
            "send_message", "edit_message_reply_markup", "delete_message",
        ]
        caption = _plain(bot.of("send_message")[-1]["text"])
        assert "overloaded" in caption.lower()
        # The buttons are back on the error card, ready for a retry.
        markup = bot.of("edit_message_reply_markup")[-1]["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "dl:103:abcdef123456:137" in datas or any(
            data.endswith(":137") for data in datas
        )

    @pytest.mark.asyncio
    async def test_broken_link_rewrites_the_single_status_line(self, monkeypatch):
        """``get_info`` fails before a card exists: one message, rewritten."""
        bot = RecordingBot()
        update = MagicMock()
        message = MagicMock()
        message.text = "https://youtube.com/watch?v=broken"
        message.caption = None
        message.reply_to_message = None
        message.message_id = 40
        message.chat_id = 1
        message.reply_text = AsyncMock(return_value=MagicMock(message_id=11))
        update.message = message
        update.effective_user = MagicMock(id=7, username="tester")
        update.effective_chat = MagicMock(id=1)
        update.get_bot = MagicMock(return_value=bot)
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(side_effect=KoyuraError("Unsupported URL")))
        monkeypatch.setattr(url_flow, "validate_url", lambda url: url)

        await url_flow.send_preview(update, "https://youtube.com/watch?v=broken")

        print("\n" + _log("битая ссылка (get_info упал до карточки)", bot))
        assert bot.names() == ["edit_message_text"]
        assert "Error: Unsupported URL" in _plain(bot.of("edit_message_text")[0]["text"])

    @pytest.mark.asyncio
    async def test_service_down_rewrites_the_single_status_line(self, monkeypatch):
        bot = RecordingBot()
        update = MagicMock()
        message = MagicMock()
        message.text = URL
        message.caption = None
        message.reply_to_message = None
        message.message_id = 40
        message.chat_id = 1
        message.reply_text = AsyncMock(return_value=MagicMock(message_id=11))
        update.message = message
        update.effective_user = MagicMock(id=7, username="tester")
        update.effective_chat = MagicMock(id=1)
        update.get_bot = MagicMock(return_value=bot)
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(side_effect=KoyuraServiceDown("down")))
        monkeypatch.setattr(url_flow, "validate_url", lambda url: url)

        await url_flow.send_preview(update, URL)

        print("\n" + _log("движок недоступен на этапе info", bot))
        assert _plain(bot.of("edit_message_text")[-1]["text"]) == (
            "Download service temporarily unavailable."
        )

    @pytest.mark.asyncio
    async def test_expired_session_rewrites_the_tapped_card(self, monkeypatch):
        bot = RecordingBot()
        query = MagicMock()
        query.data = f"dl:11:{HASH}:137"
        query.message = MagicMock()
        query.message.chat.id = 1
        query.message.message_id = 11
        query.message.photo = None
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock(
            side_effect=lambda *args, **kwargs: bot.calls.append(
                ("edit_message_text", {**kwargs, "message_id": 11, "text": args[0] if args else kwargs.get("text")})
            )
        )
        query.edit_message_caption = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        monkeypatch.setattr(callbacks.sessions, "evict_stale", lambda: None)

        await callbacks.on_format_button(update, MagicMock(bot=bot))

        print("\n" + _log("истёкшая сессия (кнопка старой карточки)", bot))
        # One answer, and the card itself now says the session expired.
        assert bot.names() == ["edit_message_text"]
        assert bot.of("edit_message_text")[0]["text"] == (
            "Session expired. Please send the link again."
        )

    @pytest.mark.asyncio
    async def test_undeletable_message_does_not_break_the_download(self, downloads, monkeypatch):
        from telegram.error import BadRequest

        bot = RecordingBot()
        bot.fail["delete_message"] = BadRequest("Message to delete not found")
        (downloads / "job.mp4").write_bytes(b"x" * 2048)
        monkeypatch.setattr(download_flow, "start_download",
                            AsyncMock(return_value={"job_id": "j4", "status": "downloading"}))
        monkeypatch.setattr(download_flow, "poll_status", AsyncMock(return_value=DONE_VIDEO))

        await _download(bot)

        print("\n" + _log("удаление невозможно (message to delete not found)", bot))
        assert bot.of("send_video"), "the file must still arrive"
        # Both deletions were attempted; neither raised.
        assert len(bot.of("delete_message")) == 2

    def test_the_caption_no_longer_repeats_the_formats(self):
        """Owner follow-up: the format list belongs to the buttons only."""
        import download_card

        caption = _plain(download_card.build_card(INFO))
        print("\n--- caption of the preview card ---\n" + caption)
        assert "Formats" not in caption
        assert "1080p" not in caption
        markup = download_card.build_format_keyboard(INFO, 11, HASH)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert "1080p · 301MB" in labels
        assert any(label.startswith("MP3") for label in labels)
