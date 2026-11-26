"""``/mp3``, ``/mp4`` and ``/best`` go through the same stages as a button tap.

The direct commands skip the picker, so their chat history should be even
shorter than the button flow's: the command's "Downloading MP3..." line is the
first stage, and every later stage replaces it.

The fake ``Bot`` records the exact call order, so the assertions are on the
sequence the owner sees in Telegram rather than on internal state.
"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import url_safety  # noqa: E402
from handlers import commands, config, download_flow, texts  # noqa: E402
from koyura_client import KoyuraBusy, KoyuraError  # noqa: E402
from tests.test_download_flow import INFO, RecordingBot, _plain  # noqa: E402


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    real_sleep = download_flow.asyncio.sleep

    async def _fast(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(download_flow.asyncio, "sleep", _fast)


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    monkeypatch.setattr(url_safety, "validate_url", lambda url: url)


@pytest.fixture()
def downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    monkeypatch.setattr(config, "PROGRESS_EDIT_INTERVAL", 0.0)
    for name in ("send_download_start", "send_download_done", "send_download_error", "send_progress"):
        monkeypatch.setattr(download_flow.event_client, name, AsyncMock())
    return tmp_path


def _update(text: str, *, message_id: int = 11):
    """An update shaped like the ones a command handler receives."""
    bot = RecordingBot()
    status = MagicMock()
    status.message_id = message_id
    status.chat_id = 5
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()

    message = MagicMock()
    message.text = text
    message.caption = None
    message.reply_to_message = None
    message.message_id = message_id
    message.chat_id = 5
    message.reply_text = AsyncMock(return_value=status)

    user = MagicMock()
    user.id = 7
    user.username = "tester"

    update = MagicMock()
    update.message = message
    update.effective_user = user
    update.effective_chat = MagicMock(id=5)
    update.get_bot = MagicMock(return_value=bot)
    update.bot = bot

    context = MagicMock()
    context.args = []
    context.bot = bot
    return update, context, bot


def _write(downloads: Path, name: str) -> None:
    (downloads / name).write_bytes(b"x" * 2048)


class TestDirectCommandStages:
    @pytest.mark.asyncio
    async def test_mp3_status_then_progress_then_file(self, downloads, monkeypatch):
        monkeypatch.setattr(download_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        monkeypatch.setattr(
            download_flow, "start_download",
            AsyncMock(return_value={"job_id": "j1", "status": "downloading"}),
        )
        monkeypatch.setattr(
            download_flow, "poll_status",
            AsyncMock(return_value={
                "status": "done", "filename": "job.mp3", "file_size_bytes": 2048,
                "duration": 635, "height": 720, "width": 1280,
            }),
        )
        _write(downloads, "job.mp3")

        update, context, bot = _update("/mp3 https://a.example/1")
        status_message = await update.message.reply_text(texts.md("mp3.started"))
        await download_flow.run_direct(
            update, context, status_message, "https://a.example/1", "audio", None
        )

        # "Downloading MP3..." (id 11) is replaced by a progress message, which
        # is replaced by the audio file.
        assert bot.stage_script()[0] == "send_message"
        assert bot.stage_script()[1] == "delete_message"
        assert bot.of("delete_message")[0]["message_id"] == 11
        assert bot.names()[-2:] == ["send_audio", "delete_message"]
        assert bot.of("send_audio")[0]["title"] == INFO["title"]

        # The "Starting…" stage was written into that one new message, and the
        # audio was the only other message posted.
        assert len(bot.of("send_message")) == 1
        assert len({kwargs["chat_id"] for kwargs in bot.of("send_audio")}) == 1

    @pytest.mark.asyncio
    async def test_mp4_status_then_progress_then_video(self, downloads, monkeypatch):
        monkeypatch.setattr(download_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        monkeypatch.setattr(
            download_flow, "start_download",
            AsyncMock(return_value={"job_id": "j1", "status": "downloading"}),
        )
        monkeypatch.setattr(
            download_flow, "poll_status",
            AsyncMock(return_value={
                "status": "done", "filename": "job.mp4", "file_size_bytes": 2048,
                "duration": 635, "height": 720, "width": 1280,
            }),
        )
        _write(downloads, "job.mp4")

        update, context, bot = _update("/mp4 https://a.example/1")
        status_message = await update.message.reply_text(texts.md("mp4.started"))
        await download_flow.run_direct(
            update, context, status_message, "https://a.example/1", "video", None
        )

        assert bot.names()[-2:] == ["send_video", "delete_message"]
        assert bot.of("delete_message")[0]["message_id"] == 11

    @pytest.mark.asyncio
    async def test_best_is_the_same_chain_as_mp4(self, downloads, monkeypatch):
        monkeypatch.setattr(download_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        monkeypatch.setattr(
            download_flow, "start_download",
            AsyncMock(return_value={"job_id": "j1", "status": "downloading"}),
        )
        monkeypatch.setattr(
            download_flow, "poll_status",
            AsyncMock(return_value={
                "status": "done", "filename": "job.mp4", "file_size_bytes": 2048,
                "duration": 635, "height": 720, "width": 1280,
            }),
        )
        _write(downloads, "job.mp4")

        update, context, bot = _update("/best https://a.example/1")
        # ``best`` shares ``mp4``'s handler, so the command line is identical.
        status_message = await update.message.reply_text(texts.md("mp4.started"))
        await download_flow.run_direct(
            update, context, status_message, "https://a.example/1", "video", None
        )

        assert bot.names()[-2:] == ["send_video", "delete_message"]
        assert bot.of("delete_message")[0]["message_id"] == 11

    @pytest.mark.asyncio
    async def test_info_failure_rewrites_the_status_line(self, downloads, monkeypatch):
        monkeypatch.setattr(
            download_flow, "get_info", AsyncMock(side_effect=KoyuraError("boom"))
        )
        update, context, bot = _update("/mp3 https://a.example/1")
        status_message = await update.message.reply_text(texts.md("mp3.started"))

        await download_flow.run_direct(
            update, context, status_message, "https://a.example/1", "audio", None
        )

        # One message, rewritten: no extra reply is piled on the command.
        assert bot.names() == ["edit_message_text"]
        assert bot.of("edit_message_text")[0]["message_id"] == 11
        assert "Error: boom" in _plain(bot.of("edit_message_text")[0]["text"])

    @pytest.mark.asyncio
    async def test_engine_refusal_replaces_the_status_with_the_error(self, downloads, monkeypatch):
        monkeypatch.setattr(download_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        monkeypatch.setattr(
            download_flow, "start_download",
            AsyncMock(side_effect=KoyuraBusy("busy", code="busy", retry_after=120)),
        )
        update, context, bot = _update("/mp3 https://a.example/1")
        status_message = await update.message.reply_text(texts.md("mp3.started"))

        await download_flow.run_direct(
            update, context, status_message, "https://a.example/1", "audio", None
        )

        # progress message -> error message, and the progress one is retired.
        assert bot.names()[-2:] == ["send_message", "delete_message"]
        assert "overloaded" in _plain(bot.of("send_message")[-1]["text"]).lower()
        assert bot.of("send_audio") == []


class TestCommandReplyStaysOneMessage:
    @pytest.mark.asyncio
    async def test_the_command_posts_exactly_one_starting_line(self, monkeypatch):
        async def _safe(urls, *, lang=None):
            return list(urls), None

        monkeypatch.setattr(download_flow, "run_direct", AsyncMock())
        monkeypatch.setattr(
            commands.url_flow, "safe_links", _safe
        )
        update, context, bot = _update("/mp3 https://a.example/1")

        await commands.mp3_command(update, context)

        # The command"s own line is MarkdownV2-escaped: the dots carry a
        # backslash, and the user still reads "Downloading MP3...".
        assert replies(update.message) == [texts.md("mp3.started")]
        assert bot.names() == []


def replies(message):
    return [call.args[0] for call in message.reply_text.await_args_list]
