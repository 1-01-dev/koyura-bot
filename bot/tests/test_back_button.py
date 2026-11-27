"""The Back button: every sub-screen returns to the welcome by *editing*.

The owner's complaint was that leaving ``Help`` or ``Settings`` meant sending
``/start`` again. Back (``nav:home``) therefore does the opposite of the download
stages: it rewrites the message the button sits on instead of posting a new one —
the chat keeps exactly one welcome, and the sequence is one ``editMessageText``
with no ``sendMessage`` in it.

Covered here:

* every sub-screen carries Back — opened from the welcome keyboard **and** from
  the plain command (``/help``, ``/platforms``, ``/settings``, ``/stats``);
* the tap edits the message (asserted on a recording bot: an edit, not a message);
* the edit produces the welcome text and the main keyboard in the user's language;
* the settings screen keeps its Back after a language switch and Back is
  translated (``Back`` / ``Назад``);
* Back from the MP3/MP4 confirmation clears the armed mode.
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import bot_settings  # noqa: E402
import state_store  # noqa: E402
from handlers import commands, keyboards, onboarding, texts  # noqa: E402
from tests.test_download_flow import RecordingBot, _plain  # noqa: E402
from tests.test_onboarding import fake_query, fake_update  # noqa: E402
from tests.test_stage_emulation import _log  # noqa: E402

SUBSCREENS = ("help", "platforms", "stats", "settings")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(tmp_path / "state.db"))
    state_store.reset_cache()
    state_store.init()
    bot_settings.reset_cache()
    bot_settings.prime(
        bot_settings.Settings(languages=("en", "ru"), default_language="en",
                              source="panel")
    )
    yield
    state_store.reset_cache()
    bot_settings.reset_cache()


def buttons_of(kwargs) -> list[tuple[str, str]]:
    """``(label, callback_data)`` for every button of one recorded message."""
    markup = kwargs.get("reply_markup")
    if markup is None:
        return []
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


def back_buttons(kwargs) -> list[tuple[str, str]]:
    return [pair for pair in buttons_of(kwargs) if pair[1] == "nav:home"]


def opened_screen(bot) -> dict:
    """The single message a sub-screen was drawn into."""
    posted = bot.of("send_message")
    assert len(posted) == 1, f"expected one message, got {bot.names()}"
    return posted[0]


async def open_subscreen(bot, context, action: str) -> dict:
    """Tap a welcome button and return the recorded sub-screen message."""
    tap, query = fake_query(bot, f"welcome:{action}")
    await onboarding.on_welcome_button(tap, context)
    query.answer.assert_awaited_once()
    return opened_screen(bot)


async def open_command(bot, context, command) -> dict:
    """Call a command (``/help`` …) and return the recorded answer message."""
    update = fake_update(bot)
    await command(update, MagicMock(bot=bot))
    return opened_screen(bot)


async def go_back(bot, context, *, message_id: int = 103):
    """Tap Back on the message and return the query (for ``answer`` checks)."""
    tap, query = fake_query(bot, "nav:home", message_id=message_id)
    await onboarding.on_nav_button(tap, context)
    return query


class TestKeyboard:
    def test_back_is_one_row_one_button(self):
        markup = keyboards.back("en")
        assert [[button.text for button in row] for row in markup.inline_keyboard] == [
            ["Back"]
        ]
        assert markup.inline_keyboard[0][0].callback_data == "nav:home"

    def test_back_is_translated(self):
        assert keyboards.back("ru").inline_keyboard[0][0].text == "Назад"

    def test_with_back_appends_a_row(self):
        combined = keyboards.with_back(keyboards.welcome("en"), "en")
        labels = [[button.text for button in row] for row in combined.inline_keyboard]
        assert labels[-1] == ["Back"]
        assert labels[0] == ["Help", "Platforms"]

    def test_with_back_works_without_an_existing_keyboard(self):
        combined = keyboards.with_back(None, "ru")
        assert [[button.text for button in row] for row in combined.inline_keyboard] == [
            ["Назад"]
        ]

    @pytest.mark.parametrize(
        "data, expected",
        [
            ("nav:home", "home"),
            ("nav:back", "back"),
            ("lang:en", None),
            ("welcome:help", None),
            ("nav:", None),
            ("nav", None),
            (None, None),
        ],
    )
    def test_parse(self, data, expected):
        assert keyboards.parse_nav(data) == expected


class TestEverySubscreenHasBack:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", SUBSCREENS)
    async def test_opened_from_the_welcome(self, action):
        bot = RecordingBot()
        message = await open_subscreen(bot, MagicMock(bot=bot), action)
        assert back_buttons(message), f"{action} has no Back button"
        assert back_buttons(message) == [("Back", "nav:home")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            commands.help_command,
            commands.platforms_command,
            commands.stats_command,
            commands.settings_command,
        ],
    )
    async def test_opened_by_a_command(self, command):
        bot = RecordingBot()
        message = await open_command(bot, MagicMock(bot=bot), command)
        assert back_buttons(message) == [("Back", "nav:home")]

    @pytest.mark.asyncio
    async def test_the_mode_confirmation_has_back(self):
        bot = RecordingBot()
        tap, _ = fake_query(bot, "welcome:mp3")
        await onboarding.on_welcome_button(tap, MagicMock(bot=bot))
        assert back_buttons(opened_screen(bot)) == [("Back", "nav:home")]

    @pytest.mark.asyncio
    async def test_the_welcome_itself_has_no_back(self):
        """Back from the welcome is a no-op with nothing to loop to."""
        bot = RecordingBot()
        tap, _ = fake_query(bot, "nav:home")
        await onboarding.on_nav_button(tap, MagicMock(bot=bot))
        assert bot.names() == ["edit_message_text"]
        assert back_buttons(bot.of("edit_message_text")[0]) == []


class TestBackReturnsHome:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", SUBSCREENS)
    async def test_tap_edits_the_message_into_the_welcome(self, action):
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        screen = await open_subscreen(bot, context, action)
        bot.calls.clear()

        query = await go_back(bot, context)

        query.answer.assert_awaited_once()
        # An edit, not a new message: the sub-screen *becomes* the welcome.
        assert bot.names() == ["edit_message_text"]
        edited = bot.of("edit_message_text")[0]
        assert edited["message_id"] == 103
        assert edited["text"] == texts.welcome_text("en")
        assert _plain(edited["text"]).startswith("Koyura Bot — a quiet downloader.")
        assert buttons_of(edited) == [
            ("Help", "welcome:help"),
            ("Platforms", "welcome:platforms"),
            ("Settings", "welcome:settings"),
            ("Stats", "welcome:stats"),
            ("MP3", "welcome:mp3"),
            ("MP4", "welcome:mp4"),
        ]
        # The screen that was there is gone, and nothing new was posted.
        assert screen is not None
        assert bot.of("send_message") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            commands.help_command,
            commands.platforms_command,
            commands.stats_command,
            commands.settings_command,
        ],
    )
    async def test_command_answers_also_go_back_home(self, command):
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        await open_command(bot, context, command)
        bot.calls.clear()

        await go_back(bot, context, message_id=101)

        assert bot.names() == ["edit_message_text"]
        assert bot.of("edit_message_text")[0]["text"] == texts.welcome_text("en")
        assert back_buttons(bot.of("edit_message_text")[0]) == []

    @pytest.mark.asyncio
    async def test_back_in_russian_returns_the_russian_welcome(self):
        state_store.set_language(7, "ru")
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        await open_subscreen(bot, context, "help")
        bot.calls.clear()

        await go_back(bot, context)

        edited = bot.of("edit_message_text")[0]
        assert _plain(edited["text"]).startswith("Koyura Bot — тихий загрузчик.")
        assert ("Помощь", "welcome:help") in buttons_of(edited)

    @pytest.mark.asyncio
    async def test_back_clears_an_armed_mode(self):
        state_store.set_pending_format(7, "audio")
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        await open_subscreen(bot, context, "settings")

        await go_back(bot, context)

        assert state_store.get_pending_format(7) is None

    @pytest.mark.asyncio
    async def test_back_from_the_mode_confirmation_cancels_it(self):
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        tap, _ = fake_query(bot, "welcome:mp3")
        await onboarding.on_welcome_button(tap, context)
        assert state_store.get_pending_format(7) == "audio"
        bot.calls.clear()

        await go_back(bot, context, message_id=102)

        assert state_store.get_pending_format(7) is None
        assert bot.names() == ["edit_message_text"]
        assert bot.of("edit_message_text")[0]["text"] == texts.welcome_text("en")

    @pytest.mark.asyncio
    async def test_unknown_navigation_is_ignored(self):
        bot = RecordingBot()
        tap, query = fake_query(bot, "nav:elsewhere")

        await onboarding.on_nav_button(tap, MagicMock(bot=bot))

        query.answer.assert_awaited_once()
        assert bot.names() == []


class TestBackAfterLanguageSwitch:
    @pytest.mark.asyncio
    async def test_settings_is_redrawn_in_the_new_language_and_back_stays(self):
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        await open_subscreen(bot, context, "settings")
        bot.calls.clear()

        switch, query = fake_query(bot, "lang:ru:s")
        await onboarding.on_language_button(switch, context)

        query.answer.assert_awaited_once()
        screen = opened_screen(bot)
        assert _plain(screen["text"]).startswith("Ваши настройки:")
        # The switch stays on the settings screen, and Back is Russian now.
        assert buttons_of(screen) == [
            ("English", "lang:en:s"),
            ("✓ Русский", "lang:ru:s"),
            ("Назад", "nav:home"),
        ]

        bot.calls.clear()
        await go_back(bot, context, message_id=104)
        edited = bot.of("edit_message_text")[0]
        assert edited["text"] == texts.welcome_text("ru")
        assert bot.of("send_message") == []

    def test_the_back_label_exists_in_both_catalogues(self):
        import i18n

        assert i18n.t("nav.back", "en") == "Back"
        assert i18n.t("nav.back", "ru") == "Назад"
        assert i18n.placeholders("nav.back", "en") == i18n.placeholders("nav.back", "ru") == set()


class TestEmulatedBackSequences:
    """The sequences quoted in the pull request (``-s`` prints them).

    ``cd bot && python -m pytest tests/test_back_button.py -s -q``
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", SUBSCREENS)
    async def test_welcome_opened_subscreen(self, action):
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        await open_subscreen(bot, context, action)
        print("\n" + _log(f"подэкран «{action}» открыт кнопкой", bot))

        bot.calls.clear()
        await go_back(bot, context)
        print("\n" + _log(f"Back на подэкране «{action}»: приветствие в том же сообщении", bot))

        assert bot.names() == ["edit_message_text"]

    @pytest.mark.asyncio
    async def test_command_opened_subscreen(self):
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        await open_command(bot, context, commands.help_command)
        print("\n" + _log("подэкран «help» открыт командой /help", bot))

        bot.calls.clear()
        await go_back(bot, context, message_id=101)
        print("\n" + _log("Back на подэкране, открытом командой: приветствие на месте", bot))

        assert bot.names() == ["edit_message_text"]

    @pytest.mark.asyncio
    async def test_mode_confirmation(self):
        bot = RecordingBot()
        context = MagicMock(bot=bot)
        tap, _ = fake_query(bot, "welcome:mp3")
        await onboarding.on_welcome_button(tap, context)
        print("\n" + _log("подтверждение режима MP3", bot))

        bot.calls.clear()
        await go_back(bot, context, message_id=102)
        print("\n" + _log("Back в подтверждении режима: режим сброшен, приветствие", bot))

        assert state_store.get_pending_format(7) is None
        assert bot.names() == ["edit_message_text"]
