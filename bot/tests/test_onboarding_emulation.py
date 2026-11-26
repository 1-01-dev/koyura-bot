"""Emulate the owner's onboarding scenarios and log the Telegram call sequence.

There is **no real Telegram account** in this test either: a fake ``Bot`` records
every call, the dashboard is answered from a primed cache and the engine client
is patched, so the four sequences the owner will check in ``@KoyuraBot`` are
verified without touching the live bot.

Running it with ``-s`` prints the log quoted in the pull request:

    cd bot && python -m pytest tests/test_onboarding_emulation.py -s -q
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import bot_settings  # noqa: E402
import state_store  # noqa: E402
from handlers import commands, config, download_flow, onboarding, url_flow  # noqa: E402
from tests.test_download_flow import RecordingBot, _plain  # noqa: E402
from tests.test_onboarding import fake_message, fake_query, fake_update  # noqa: E402
from tests.test_stage_emulation import _log  # noqa: E402

INFO = {
    "title": "Big Buck Bunny",
    "duration": 635,
    "extractor": "youtube",
    "channel": "Blender",
    "uploader": "Blender",
    "formats": [
        {"id": "137", "label": "1080p", "height": 1080, "button_label": "1080p · 301MB"},
    ],
    "audio": {"abr": 129, "size_bytes": 10_271_496},
}
URL = "https://youtube.com/watch?v=x"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    state_store.reset_cache()
    state_store.init()

    real_sleep = download_flow.asyncio.sleep

    async def _instant(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(download_flow.asyncio, "sleep", _instant)
    monkeypatch.setattr(config, "PROGRESS_EDIT_INTERVAL", 0.0)
    for name in ("send_download_start", "send_download_done",
                 "send_download_error", "send_progress"):
        monkeypatch.setattr(download_flow.event_client, name, AsyncMock())
    yield
    state_store.reset_cache()


def prime(languages, default):
    bot_settings.prime(
        bot_settings.Settings(languages=tuple(languages), default_language=default,
                              source="panel")
    )


class TestEmulatedOnboarding:
    @pytest.mark.asyncio
    async def test_first_contact_with_two_languages(self):
        prime(["en", "ru"], "en")
        bot = RecordingBot()
        context = MagicMock(bot=bot)

        await onboarding.start_command(fake_update(bot), context)
        print("\n" + _log("первый контакт: два языка включены → экран выбора", bot))

        bot.calls.clear()
        tap, query = fake_query(bot, "lang:ru")
        await onboarding.on_language_button(tap, context)
        print("\n" + _log("выбор «Русский»: экран выбора заменяется приветствием", bot))

        assert bot.stage_script() == ["send_message", "delete_message"]
        assert state_store.get_language(7) == "ru"
        assert _plain(bot.of("send_message")[0]["text"]).startswith(
            "Koyura Bot — тихий загрузчик."
        )
        rows = [
            [b.text for b in row]
            for row in bot.of("send_message")[0]["reply_markup"].inline_keyboard
        ]
        assert rows == [
            ["Помощь", "Платформы"],
            ["Настройки", "Статистика"],
            ["MP3", "MP4"],
        ]

    @pytest.mark.asyncio
    async def test_first_contact_with_one_language(self):
        prime(["en"], "en")
        bot = RecordingBot()
        context = MagicMock(bot=bot)

        update = fake_update(bot)
        await onboarding.start_command(update, context)
        print("\n" + _log("первый контакт: включён один язык → сразу приветствие", bot))

        assert bot.names() == ["send_message"]
        message = bot.of("send_message")[0]
        assert _plain(message["text"]) == (
            "Koyura Bot — a quiet downloader.\n\n"
            "Send a video or audio link and the file arrives in this chat.\n\n"
            "Supported platforms: YouTube, TikTok, Instagram, Twitter, Reddit, "
            "and 1000+ more."
        )
        rows = [[b.text for b in row] for row in message["reply_markup"].inline_keyboard]
        assert rows == [
            ["Help", "Platforms"],
            ["Settings", "Stats"],
            ["MP3", "MP4"],
        ]

    @pytest.mark.asyncio
    async def test_settings_and_language_switch(self):
        prime(["en", "ru"], "en")
        state_store.set_language(7, "en")
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        update = fake_update(bot)

        await commands.settings_command(update, context)
        print("\n" + _log("/settings на английском, обе кнопки языка", bot))

        bot.calls.clear()
        tap, query = fake_query(bot, "lang:ru:s")
        await onboarding.on_language_button(tap, context)
        print("\n" + _log("переключение на «Русский» из /settings", bot))

        assert state_store.get_language(7) == "ru"
        assert bot.stage_script() == ["send_message", "delete_message"]
        text = _plain(bot.of("send_message")[0]["text"])
        assert "Ваши настройки:" in text
        assert "Язык: Русский" in text
        labels = [
            b.text for row in bot.of("send_message")[0]["reply_markup"].inline_keyboard
            for b in row
        ]
        assert labels == ["English", "✓ Русский"]

    @pytest.mark.asyncio
    async def test_mp3_mode_then_link(self, monkeypatch):
        prime(["en"], "en")
        bot = RecordingBot()
        context = MagicMock(bot=bot)

        # 1. MP3 on the welcome keyboard: arm the mode, no download yet.
        tap, _ = fake_query(bot, "welcome:mp3")
        await onboarding.on_welcome_button(tap, context)
        print("\n" + _log("кнопка MP3 на приветствии: режим включён", bot))
        assert state_store.get_pending_format(7) == "audio"
        assert _plain(bot.of("send_message")[0]["text"]) == (
            "Mode: MP3\nSend the link — it downloads as MP3."
        )

        # 2. The next link: no picker, straight to a download in that format.
        bot.calls.clear()
        monkeypatch.setattr(
            url_flow, "safe_links", AsyncMock(return_value=([URL], None))
        )
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
        (download_flow.Path(config.DOWNLOADS_PATH) / "job.mp3").write_bytes(b"x" * 2048)

        message = fake_message(bot)
        message.text = URL
        await url_flow.url_handler(fake_update(bot, message=message), context)
        print("\n" + _log("ссылка при включённом MP3-режиме: сразу файл", bot))

        assert state_store.get_pending_format(7) is None
        assert bot.stage_script() == [
            "send_message",    # "Mode: MP3 — send the link" (the intake answer)
            "send_message",    # "Starting…"
            "delete_message",  # the mode message it replaces
            "send_audio",      # the file
            "delete_message",  # the progress message
        ]
        assert bot.of("send_audio")[0]["filename"].endswith(".mp3")
