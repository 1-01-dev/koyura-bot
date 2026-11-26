"""Every command in the menu: the exact answer, and the refusal paths.

The wording is the contract (see ``docs/bot-behavior.md``), so the assertions are
on the user-visible text, not on the constants — a rename inside ``texts`` must
not be able to change what the user reads without failing a test.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import state_store  # noqa: E402
from handlers import commands, config, download_flow, stats, texts, url_flow  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Preferences go to a throwaway DB, never to the developer's state file."""
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(tmp_path / "state.db"))
    state_store.reset_cache()
    state_store.init()
    yield
    state_store.reset_cache()


@pytest.fixture()
def downloads(tmp_path, monkeypatch):
    """A downloads volume of its own: the state DB must not be counted as a file."""
    volume = tmp_path / "downloads"
    volume.mkdir()
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(volume))
    return volume


def fake_update(text="/cmd", *, args=None, user_id=7, username="tester", replied=None):
    """An update shaped like the ones python-telegram-bot hands to a command."""
    message = MagicMock()
    message.text = text
    message.caption = None
    message.reply_to_message = replied
    message.message_id = 11
    message.chat_id = 5
    message.reply_text = AsyncMock()

    user = MagicMock()
    user.id = user_id
    user.username = username

    update = MagicMock()
    update.message = message
    update.effective_user = user
    update.effective_chat = MagicMock(id=5)

    context = MagicMock()
    context.args = list(args or [])
    return update, context


def replies(message):
    return [call.args[0] for call in message.reply_text.await_args_list]


def last_call(mock):
    """The most recent awaited call, as a real object (``await_args`` is Optional)."""
    call = mock.await_args
    assert call is not None, "the mock was never awaited"
    return call


class TestStartAndHelp:
    @pytest.mark.asyncio
    async def test_start_is_plain_text_with_the_menu(self):
        update, context = fake_update()
        await commands.start_command(update, context)

        (text,) = replies(update.message)
        assert text.startswith("Koyura Bot — a quiet downloader.")
        assert "Send a video or audio link and the file arrives in this chat." in text
        for command in ("/help", "/platforms", "/settings", "/stats", "/mp3 <link>", "/mp4 <link>"):
            assert command in text
        # No parse_mode: the welcome text is not Markdown and must not be parsed.
        assert update.message.reply_text.await_args.kwargs == {}

    @pytest.mark.asyncio
    async def test_help_is_markdown_v2_and_pre_escaped(self):
        update, context = fake_update()
        await commands.help_command(update, context)

        (text,) = replies(update.message)
        assert update.message.reply_text.await_args.kwargs["parse_mode"] == "MarkdownV2"
        assert text.startswith("*How to use Koyura Bot:*")
        # Escaped dots and parentheses: Telegram rejects unescaped ones.
        assert "1\\. Send a link \\(" in text
        assert "Ship it" not in text
        assert "/cancel \\- Cancel \\(coming soon\\)" in text

    @pytest.mark.asyncio
    async def test_platforms_lists_every_supported_site(self):
        update, context = fake_update()
        await commands.platforms_command(update, context)

        (text,) = replies(update.message)
        assert text.startswith("Supported platforms:\n\n")
        for platform in texts.SUPPORTED_PLATFORMS:
            assert f"  {platform}" in text
        assert "and 1000+ more via yt-dlp" in text


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_counts_files_and_prints_counters(self, downloads, monkeypatch):
        (downloads / "a.mp4").write_bytes(b"x" * 1024)
        (downloads / "b.mp3").write_bytes(b"x" * 1024)
        (downloads / "subdir").mkdir()
        monkeypatch.setitem(stats.COUNTERS, "downloads", 12)
        monkeypatch.setitem(stats.COUNTERS, "errors", 3)

        update, context = fake_update()
        await commands.stats_command(update, context)

        (text,) = replies(update.message)
        assert "Downloads: 12" in text
        assert "Errors: 3" in text
        # A directory on the volume is not a cached file.
        assert "Cached files: 2" in text
        assert "Disk usage: 0.0 MB" in text
        assert "Active sessions: 0" in text
        assert "Uptime: " in text

    @pytest.mark.asyncio
    async def test_missing_downloads_dir_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path / "gone"))
        update, context = fake_update()
        await commands.stats_command(update, context)
        (text,) = replies(update.message)
        assert "Cached files: 0" in text


class TestPreferences:
    @pytest.mark.asyncio
    async def test_settings_shows_the_stored_defaults(self):
        saved, save_context = fake_update(args=["720"])
        await commands.set_quality_command(saved, save_context)

        update, context = fake_update()
        await commands.settings_command(update, context)
        (text,) = replies(update.message)
        assert "Default quality: 720" in text
        assert "Default format: video" in text
        assert "/setquality <best/720/480>" in text

    @pytest.mark.asyncio
    async def test_setquality_stores_the_choice(self):
        update, context = fake_update(args=["1080"])
        await commands.set_quality_command(update, context)

        assert replies(update.message) == ["Default quality set to: 1080"]
        assert state_store.get_prefs(7)["quality"] == "1080"

    @pytest.mark.asyncio
    async def test_setquality_without_argument_prints_usage(self):
        update, context = fake_update()
        await commands.set_quality_command(update, context)

        assert replies(update.message) == ["Usage: /setquality <best/1080/720/480/360>"]

    @pytest.mark.asyncio
    async def test_setquality_rejects_unknown_values(self):
        update, context = fake_update(args=["4K"])
        await commands.set_quality_command(update, context)

        assert replies(update.message) == [
            "Invalid quality. Options: best, 1080, 720, 480, 360"
        ]
        assert state_store.get_prefs(7)["quality"] == "best"

    @pytest.mark.asyncio
    async def test_setquality_lowercases_the_argument(self):
        update, context = fake_update(args=["BEST"])
        await commands.set_quality_command(update, context)
        assert replies(update.message) == ["Default quality set to: best"]

    @pytest.mark.asyncio
    async def test_setformat_stores_the_choice(self):
        update, context = fake_update(args=["audio"])
        await commands.set_format_command(update, context)

        assert replies(update.message) == ["Default format set to: audio"]
        assert state_store.get_prefs(7)["format"] == "audio"

    @pytest.mark.asyncio
    async def test_setformat_usage_and_refusal(self):
        update, context = fake_update()
        await commands.set_format_command(update, context)
        assert replies(update.message) == ["Usage: /setformat <video/audio>"]

        update, context = fake_update(args=["mkv"])
        await commands.set_format_command(update, context)
        assert replies(update.message) == ["Invalid format. Options: video, audio"]


class TestDirectDownloads:
    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        async def _safe(urls):
            return list(urls), None

        monkeypatch.setattr(url_flow, "safe_links", _safe)
        self.direct = AsyncMock()
        monkeypatch.setattr(download_flow, "run_direct", self.direct)

    @pytest.mark.asyncio
    async def test_mp3_starts_one_audio_job_per_link(self):
        update, context = fake_update("/mp3 https://a.example/1 https://a.example/2")
        await commands.mp3_command(update, context)

        assert replies(update.message) == ["Downloading MP3...", "Downloading MP3..."]
        assert self.direct.await_count == 2
        first = self.direct.await_args_list[0]
        assert first.args[3] == "https://a.example/1"
        assert first.args[4] == "audio"
        assert first.args[5] is None

    @pytest.mark.asyncio
    async def test_mp4_starts_a_best_quality_video_job(self):
        update, context = fake_update("/mp4 https://a.example/1")
        await commands.mp4_command(update, context)

        assert replies(update.message) == ["Downloading MP4 (best quality)..."]
        args = last_call(self.direct).args
        assert (args[3], args[4], args[5]) == ("https://a.example/1", "video", None)

    @pytest.mark.asyncio
    async def test_best_is_mp4(self):
        update, context = fake_update("/best https://a.example/1")
        await commands.best_command(update, context)

        assert replies(update.message) == ["Downloading MP4 (best quality)..."]
        assert last_call(self.direct).args[4] == "video"

    @pytest.mark.asyncio
    async def test_mp3_without_a_link_explains_the_usage(self):
        update, context = fake_update("/mp3")
        await commands.mp3_command(update, context)

        assert replies(update.message) == [
            "Usage: /mp3 <link>\nOr reply to a message containing a link."
        ]
        self.direct.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mp4_without_a_link_explains_the_usage(self):
        update, context = fake_update("/mp4")
        await commands.mp4_command(update, context)

        assert replies(update.message) == [
            "Usage: /mp4 <link>\nOr reply to a message containing a link."
        ]

    @pytest.mark.asyncio
    async def test_links_are_taken_from_the_replied_message(self):
        replied = MagicMock()
        replied.text = "https://a.example/replied"
        replied.caption = None
        update, context = fake_update("/mp3", replied=replied)
        await commands.mp3_command(update, context)

        assert last_call(self.direct).args[3] == "https://a.example/replied"

    @pytest.mark.asyncio
    async def test_a_refused_link_is_announced_before_the_usage(self, monkeypatch):
        async def _safe(urls):
            return [], (
                "I can only download from public http(s) links. "
                "That link was skipped because it is not allowed."
            )

        monkeypatch.setattr(url_flow, "safe_links", _safe)
        update, context = fake_update("/mp3 http://127.0.0.1/x")
        await commands.mp3_command(update, context)

        assert replies(update.message) == [
            "I can only download from public http(s) links. "
            "That link was skipped because it is not allowed.",
            "Usage: /mp3 <link>\nOr reply to a message containing a link.",
        ]
        self.direct.assert_not_awaited()


class TestUpdatesWithoutAMessage:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler",
        [
            commands.start_command,
            commands.help_command,
            commands.platforms_command,
            commands.stats_command,
            commands.settings_command,
            commands.set_quality_command,
            commands.set_format_command,
            commands.mp3_command,
            commands.mp4_command,
            commands.best_command,
        ],
    )
    async def test_channel_posts_are_ignored(self, handler):
        update, context = fake_update()
        update.message = None
        await handler(update, context)  # must not raise
        update, context = fake_update()
        update.effective_user = None
        await handler(update, context)  # must not raise
