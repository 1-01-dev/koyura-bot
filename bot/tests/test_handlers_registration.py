"""What actually reaches python-telegram-bot: handlers, order and filters.

The menu Telegram shows comes from ``bot.py`` and the dispatch comes from
``register_handlers``; if those two drift apart a command exists that nobody can
use (or a help line points at nothing), so both are checked here.
"""
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from telegram.ext import (  # noqa: E402
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)

from handlers import callbacks, commands, register_handlers, texts, url_flow  # noqa: E402

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"


class FakeApplication:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler):
        self.handlers.append(handler)


@pytest.fixture()
def registered():
    application = FakeApplication()
    register_handlers(application)
    return application.handlers


def command_handlers(handler_list):
    return [h for h in handler_list if isinstance(h, CommandHandler)]


def command_names(handler):
    """python-telegram-bot keeps the commands as a frozenset."""
    return sorted(getattr(handler, "commands", set()))


def commands_in(handler_list):
    return [name for h in command_handlers(handler_list) for name in command_names(h)]


def message_update(text, entities=None) -> Any:
    """The smallest thing a filter needs to decide on a text message."""
    message = SimpleNamespace(
        text=text, caption=None, entities=entities, caption_entities=None
    )
    return SimpleNamespace(
        message=message,
        edited_message=None,
        channel_post=None,
        edited_channel_post=None,
        effective_message=message,
        effective_user=None,
        effective_chat=None,
    )


def bot_command_entity(text):
    return [SimpleNamespace(type="bot_command", offset=0, length=len(text))]


def bot_source() -> str:
    with open(BOT_PY, encoding="utf-8") as handle:
        return handle.read()


class TestRegistration:
    def test_every_menu_command_is_registered(self, registered):
        assert commands_in(registered) == [
            "start", "help", "platforms", "stats", "settings",
            "setquality", "setformat", "mp3", "mp4", "best",
        ]

    def test_commands_point_at_the_command_module(self, registered):
        expected = {
            "start": commands.start_command,
            "help": commands.help_command,
            "platforms": commands.platforms_command,
            "stats": commands.stats_command,
            "settings": commands.settings_command,
            "setquality": commands.set_quality_command,
            "setformat": commands.set_format_command,
            "mp3": commands.mp3_command,
            "mp4": commands.mp4_command,
            "best": commands.best_command,
        }
        for handler in command_handlers(registered):
            (name,) = command_names(handler)
            assert handler.callback is expected[name]

    def test_free_text_goes_to_the_link_handler(self, registered):
        text_handlers = [h for h in registered if isinstance(h, MessageHandler)]
        assert len(text_handlers) == 1
        assert text_handlers[0].callback is url_flow.url_handler
        # A link is handled, a command is not: otherwise "/help" would be
        # fetched from the engine like a URL.
        text_filter = text_handlers[0].filters
        assert text_filter.check_update(message_update("https://x.example/1"))
        assert not text_filter.check_update(
            message_update("/help", bot_command_entity("/help"))
        )
        assert not text_filter.check_update(message_update(None))

    def test_buttons_go_to_one_handler_with_the_legacy_pattern(self, registered):
        button_handlers = [h for h in registered if isinstance(h, CallbackQueryHandler)]
        assert len(button_handlers) == 1
        assert button_handlers[0].callback is callbacks.on_format_button
        pattern: Any = getattr(button_handlers[0], "pattern", None)
        assert pattern.pattern == r"^(dl|fmt|qty):"
        for data in ("dl:1:h:137", "fmt:1:h:video", "qty:1:h:best"):
            assert pattern.match(data)
        assert not pattern.match("nonsense")

    def test_the_text_handler_runs_last(self, registered):
        assert isinstance(registered[-1], CallbackQueryHandler)
        assert isinstance(registered[-2], MessageHandler)

    def test_cancel_is_not_registered(self, registered):
        """/cancel is documented as "coming soon"; it must stay unimplemented."""
        assert "cancel" not in commands_in(registered)
        # ...and the help text still says so, so the gap stays visible.
        assert "/cancel \\- Cancel \\(coming soon\\)" in texts.HELP

    def test_registering_twice_does_not_duplicate(self, registered):
        application = FakeApplication()
        register_handlers(application)
        register_handlers(application)
        assert len(application.handlers) == 2 * len(registered)


class TestMenuParity:
    """``setMyCommands`` in bot.py and the registered handlers must agree."""

    def menu_commands(self):
        return re.findall(r'BotCommand\("([a-z0-9_]+)"', bot_source())

    def test_menu_matches_the_handlers(self, registered):
        # The menu order in bot.py is curated (the shortcuts sit high); only
        # the set has to agree with what is registered.
        assert sorted(self.menu_commands()) == sorted(commands_in(registered))

    def test_menu_entries_have_a_description(self):
        described = re.findall(r'BotCommand\("([a-z0-9_]+)", "([^"]+)"\)', bot_source())
        assert len(described) == len(self.menu_commands())
        assert all(description.strip() for _, description in described)


class TestFacade:
    def test_the_facade_exposes_the_modules(self):
        import handlers

        for name in (
            "callbacks", "commands", "config", "download_flow",
            "sessions", "stats", "texts", "url_flow",
        ):
            assert getattr(handlers, name) is not None

    def test_register_handlers_is_the_documented_entry_point(self):
        import inspect

        assert list(inspect.signature(register_handlers).parameters) == ["application"]

    def test_bot_py_still_imports_register_handlers(self):
        source = bot_source()
        assert "from handlers import register_handlers" in source
        assert "register_handlers(app)" in source
