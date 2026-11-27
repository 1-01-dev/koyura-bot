"""First contact: the language screen, the welcome and its buttons.

These are the behaviours the owner described in one paragraph and would notice
instantly if they broke: the picker appears **once**, a single-language bot never
shows it, the welcome keyboard answers with the same screens as the commands, and
MP3/MP4 arm a mode that survives until the next link.

The panel is never contacted: the shared autouse fixture primes the built-in
answer, and the fallback tests call ``bot_settings.fetch`` with a dead address.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import bot_settings  # noqa: E402
import i18n  # noqa: E402
import state_store  # noqa: E402
from handlers import (  # noqa: E402
    download_flow,
    keyboards,
    language,
    onboarding,
    texts,
)
from tests.test_download_flow import RecordingBot, _plain  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(tmp_path / "state.db"))
    state_store.reset_cache()
    state_store.init()
    yield
    state_store.reset_cache()


def prime(languages, default):
    bot_settings.prime(
        bot_settings.Settings(languages=tuple(languages), default_language=default,
                              source="panel")
    )


def fake_message(bot, *, chat_id=5, message_id=11):
    """A message whose ``reply_text`` posts through the recording bot."""
    message = MagicMock()
    message.chat_id = chat_id
    message.message_id = message_id
    message.text = "/start"
    message.caption = None
    message.reply_to_message = None

    async def reply_text(text, **kwargs):
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)

    message.reply_text = reply_text
    return message


def fake_update(bot, *, user_id=7, message=None, chat_id=5):
    update = MagicMock()
    update.message = message if message is not None else fake_message(bot, chat_id=chat_id)
    user = MagicMock()
    user.id = user_id
    user.username = "tester"
    update.effective_user = user
    update.effective_chat = MagicMock(id=chat_id)
    update.get_bot = MagicMock(return_value=bot)
    return update


def fake_query(bot, data, *, chat_id=5, message_id=11, replied=None, photo=None):
    query = MagicMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat.id = chat_id
    query.message.message_id = message_id
    query.message.photo = photo
    query.message.reply_to_message = replied
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    user = MagicMock()
    user.id = 7
    user.username = "tester"
    update.effective_user = user
    update.effective_chat = MagicMock(id=chat_id)
    return update, query


def called_args(mock):
    """The arguments of the last await (``await_args`` is Optional in the types)."""
    call = mock.await_args
    assert call is not None, "the mock was never awaited"
    return call.args


def keyboard_of(kwargs):
    markup = kwargs.get("reply_markup")
    return [button for row in markup.inline_keyboard for button in row]


class TestFirstContact:
    @pytest.mark.asyncio
    async def test_two_languages_ask_first_and_greet_afterwards(self):
        prime(["en", "ru"], "en")
        bot = RecordingBot()
        update = fake_update(bot)

        await onboarding.start_command(update, MagicMock(bot=bot))

        assert bot.names() == ["send_message"]
        text = _plain(bot.of("send_message")[0]["text"])
        assert text == "Choose the language of this chat."
        buttons = keyboard_of(bot.of("send_message")[0])
        assert [b.text for b in buttons] == ["✓ English", "Русский"]
        assert [b.callback_data for b in buttons] == ["lang:en", "lang:ru"]

        # Tapping a language replaces the screen with the welcome.
        bot.calls.clear()
        tap, query = fake_query(bot, "lang:ru")
        await onboarding.on_language_button(tap, MagicMock(bot=bot))

        query.answer.assert_awaited_once()
        assert state_store.get_language(7) == "ru"
        assert bot.stage_script() == [
            "send_message",   # the welcome, in Russian
            "delete_message",  # the picker it replaces
        ]
        welcome = _plain(bot.of("send_message")[-1]["text"])
        assert welcome.startswith("Koyura Bot — тихий загрузчик.")
        labels = [b.text for b in keyboard_of(bot.of("send_message")[-1])]
        assert labels == ["Помощь", "Платформы", "Настройки", "Статистика", "MP3", "MP4"]

    @pytest.mark.asyncio
    async def test_the_picker_is_shown_once(self):
        prime(["en", "ru"], "en")
        bot = RecordingBot()
        update = fake_update(bot)
        await onboarding.start_command(update, MagicMock(bot=bot))

        tap, _ = fake_query(bot, "lang:en")
        await onboarding.on_language_button(tap, MagicMock(bot=bot))
        bot.calls.clear()

        await onboarding.start_command(fake_update(bot), MagicMock(bot=bot))

        assert bot.names() == ["send_message"]
        assert _plain(bot.of("send_message")[0]["text"]).startswith(
            "Koyura Bot — a quiet downloader."
        )

    @pytest.mark.asyncio
    async def test_one_enabled_language_never_asks(self):
        prime(["ru"], "ru")
        bot = RecordingBot()

        await onboarding.start_command(fake_update(bot), MagicMock(bot=bot))

        assert bot.names() == ["send_message"]
        message = bot.of("send_message")[0]
        assert _plain(message["text"]).startswith("Koyura Bot — тихий загрузчик.")
        # No language buttons: the only language is already the answer.
        assert keyboard_of(message)[:4] == [
            b for b in keyboard_of(message) if not b.callback_data.startswith("lang:")
        ][:4]
        assert all(not b.callback_data.startswith("lang:") for b in keyboard_of(message))

    @pytest.mark.asyncio
    async def test_a_tap_on_a_disabled_language_is_refused(self):
        prime(["en"], "en")
        bot = RecordingBot()
        tap, query = fake_query(bot, "lang:ru")

        await onboarding.on_language_button(tap, MagicMock(bot=bot))

        assert query.answer.await_args.kwargs["show_alert"] is True
        assert state_store.get_language(7) is None
        assert bot.names() == []

    @pytest.mark.asyncio
    async def test_switching_from_settings_stays_on_settings(self):
        prime(["en", "ru"], "en")
        bot = RecordingBot()
        tap, _ = fake_query(bot, "lang:ru:s")

        await onboarding.on_language_button(tap, MagicMock(bot=bot))

        assert state_store.get_language(7) == "ru"
        text = _plain(bot.of("send_message")[-1]["text"])
        assert "Ваши настройки:" in text
        labels = [b.text for b in keyboard_of(bot.of("send_message")[-1])]
        assert labels == ["English", "✓ Русский", "Назад"]


class TestLanguageResolution:
    @pytest.mark.asyncio
    async def test_saved_choice_wins_over_the_default(self):
        prime(["en", "ru"], "ru")
        state_store.set_language(7, "en")
        assert await language.current(7) == "en"

    @pytest.mark.asyncio
    async def test_no_choice_means_the_default(self):
        prime(["en", "ru"], "ru")
        assert await language.current(7) == "ru"

    @pytest.mark.asyncio
    async def test_a_choice_the_operator_disabled_is_not_honoured(self):
        prime(["en"], "en")
        state_store.set_language(7, "ru")
        assert await language.current(7) == "en"

    @pytest.mark.asyncio
    async def test_an_unknown_user_gets_the_default(self):
        prime(["en", "ru"], "ru")
        assert await language.current(None) == "ru"

    def test_needs_picker_only_with_a_choice_available(self):
        two = bot_settings.Settings(("en", "ru"), "en")
        one = bot_settings.Settings(("ru",), "ru")
        assert language.needs_picker(two, 7) is True
        assert language.needs_picker(one, 7) is False
        state_store.set_language(7, "ru")
        assert language.needs_picker(two, 7) is False


class TestPanelSettings:
    def test_an_unknown_code_is_dropped(self):
        settings = bot_settings.normalise_payload(
            {"languages": ["en", "de", "ru"], "default_language": "ru"}
        )
        assert settings.languages == ("en", "ru")
        assert settings.default_language == "ru"

    def test_an_empty_language_list_falls_back_to_the_builtins(self):
        settings = bot_settings.normalise_payload({"languages": [], "default_language": "ru"})
        assert settings.languages == ("en", "ru")

    def test_a_default_that_is_not_enabled_becomes_the_first(self):
        settings = bot_settings.normalise_payload(
            {"languages": ["ru"], "default_language": "en"}
        )
        assert settings.default_language == "ru"

    def test_a_comma_separated_string_is_accepted(self):
        settings = bot_settings.normalise_payload({"languages": "en,ru", "default_language": "en"})
        assert settings.languages == ("en", "ru")

    def test_garbage_is_the_builtin_default(self):
        assert bot_settings.normalise_payload(None) == bot_settings.defaults()
        garbage = bot_settings.normalise_payload({"languages": "??"})
        default = bot_settings.defaults()
        assert garbage.languages == default.languages
        assert garbage.default_language == default.default_language

    @pytest.mark.asyncio
    async def test_an_unreachable_panel_uses_the_builtin_defaults(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_URL", "http://127.0.0.1:1")
        monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
        bot_settings.reset_cache()

        settings = await bot_settings.fetch()

        assert settings.languages == ("en", "ru")
        assert settings.default_language == "en"
        assert settings.source == "fallback"

    @pytest.mark.asyncio
    async def test_the_answer_is_cached_for_the_ttl(self, monkeypatch):
        calls = []

        async def _fetch():
            calls.append(1)
            return bot_settings.Settings(("en",), "en", source="panel")

        monkeypatch.setattr(bot_settings, "fetch", _fetch)
        bot_settings.reset_cache()

        await bot_settings.get(now=100.0)
        await bot_settings.get(now=100.0 + bot_settings.TTL_SECONDS - 1)
        assert len(calls) == 1

        await bot_settings.get(now=100.0 + bot_settings.TTL_SECONDS + 1)
        assert len(calls) == 2


class TestWelcomeKeyboard:
    def test_the_layout_is_two_per_row(self):
        markup = keyboards.welcome("en")
        rows = [[b.text for b in row] for row in markup.inline_keyboard]
        assert rows == [
            ["Help", "Platforms"],
            ["Settings", "Stats"],
            ["MP3", "MP4"],
        ]

    def test_the_keys_are_routed_by_their_own_scheme(self):
        markup = keyboards.welcome("en")
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert datas == [
            "welcome:help", "welcome:platforms", "welcome:settings",
            "welcome:stats", "welcome:mp3", "welcome:mp4",
        ]

    def test_the_labels_are_translated(self):
        markup = keyboards.welcome("ru")
        rows = [[b.text for b in row] for row in markup.inline_keyboard]
        assert rows[0] == ["Помощь", "Платформы"]

    @pytest.mark.parametrize(
        "action, expected",
        [
            ("help", "How to use Koyura Bot:"),
            ("platforms", "Supported platforms:"),
            ("settings", "Your preferences:"),
            ("stats", "Koyura Bot Stats:"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_button_answers_with_the_command_screen(self, action, expected):
        prime(["en"], "en")
        bot = RecordingBot()
        tap, query = fake_query(bot, f"welcome:{action}")

        await onboarding.on_welcome_button(tap, MagicMock(bot=bot))

        query.answer.assert_awaited_once()
        # The welcome is replaced, not stacked on: post, then delete.
        assert bot.stage_script() == ["send_message", "delete_message"]
        assert expected in _plain(bot.of("send_message")[0]["text"])

    @pytest.mark.asyncio
    async def test_help_keeps_the_markdown(self):
        prime(["en"], "en")
        bot = RecordingBot()
        tap, _ = fake_query(bot, "welcome:help")

        await onboarding.on_welcome_button(tap, MagicMock(bot=bot))

        assert bot.of("send_message")[0]["parse_mode"] == "MarkdownV2"
        assert bot.of("send_message")[0]["text"].startswith("*How to use Koyura Bot:*")

    @pytest.mark.asyncio
    async def test_unknown_actions_are_ignored(self):
        prime(["en"], "en")
        bot = RecordingBot()
        tap, query = fake_query(bot, "welcome:nope")

        await onboarding.on_welcome_button(tap, MagicMock(bot=bot))

        query.answer.assert_awaited_once()
        assert bot.names() == []


class TestMode:
    @pytest.mark.asyncio
    async def test_mp3_arms_the_mode_and_says_so(self):
        prime(["en"], "en")
        bot = RecordingBot()
        tap, _ = fake_query(bot, "welcome:mp3")

        await onboarding.on_welcome_button(tap, MagicMock(bot=bot))

        assert state_store.get_pending_format(7) == "audio"
        text = _plain(bot.of("send_message")[0]["text"])
        assert text == "Mode: MP3\nSend the link — it downloads as MP3."

    @pytest.mark.asyncio
    async def test_the_next_link_uses_the_mode_and_clears_it(self, monkeypatch):
        prime(["en"], "en")
        state_store.set_pending_format(7, "video")

        from handlers import url_flow

        safe = AsyncMock(return_value=(["https://a.example/1", "https://a.example/2"], None))
        monkeypatch.setattr(url_flow, "safe_links", safe)
        direct = AsyncMock()
        monkeypatch.setattr(download_flow, "run_direct", direct)
        preview = AsyncMock()
        monkeypatch.setattr(url_flow, "send_preview", preview)

        bot = RecordingBot()
        message = fake_message(bot)
        message.text = "https://a.example/1 https://a.example/2"
        update = fake_update(bot, message=message)

        await url_flow.url_handler(update, MagicMock(bot=bot))

        assert state_store.get_pending_format(7) is None
        # Only the first link takes the armed format; the rest get a card.
        assert direct.await_count == 1
        assert called_args(direct)[3] == "https://a.example/1"
        assert called_args(direct)[4] == "video"
        assert preview.await_count == 1
        assert called_args(preview)[1] == "https://a.example/2"
        assert "Mode: MP4" in _plain(bot.of("send_message")[0]["text"])

    @pytest.mark.asyncio
    async def test_a_reply_to_a_link_downloads_immediately(self, monkeypatch):
        prime(["en"], "en")
        from handlers import url_flow

        monkeypatch.setattr(
            url_flow, "safe_links", AsyncMock(return_value=(["https://a.example/9"], None))
        )
        direct = AsyncMock()
        monkeypatch.setattr(download_flow, "run_direct", direct)

        bot = RecordingBot()
        replied = MagicMock()
        replied.text = "https://a.example/9"
        replied.caption = None
        tap, _ = fake_query(bot, "welcome:mp3", replied=replied)

        await onboarding.on_welcome_button(tap, MagicMock(bot=bot))

        assert direct.await_count == 1
        assert called_args(direct)[4] == "audio"
        # Nothing is armed: there was nothing to wait for.
        assert state_store.get_pending_format(7) is None

    @pytest.mark.asyncio
    async def test_the_mode_is_visible_in_settings(self):
        state_store.set_pending_format(7, "audio")
        from handlers import commands

        bot = RecordingBot()
        update = fake_update(bot)

        await commands.settings_command(update, MagicMock(bot=bot))

        assert "Next link: MP3" in _plain(bot.of("send_message")[0]["text"])


class TestLanguageNames:
    def test_language_buttons_use_the_autonym(self):
        assert texts.language_name("en") == "English"
        assert texts.language_name("ru") == "Русский"

    def test_the_picker_offers_only_enabled_languages(self):
        settings = bot_settings.Settings(("en",), "en")
        markup = keyboards.language_switch(settings, "en")
        assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["lang:en"]

    def test_an_escaped_welcome_has_no_raw_specials(self):
        assert "\\." in texts.welcome_text("en")
        assert i18n.escape_md("a-b") == "a\\-b"
