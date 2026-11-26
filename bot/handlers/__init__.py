"""Chat-facing Telegram handlers, split by the thing they handle.

The bot has exactly one job per module:

| module | responsibility |
| --- | --- |
| ``commands`` | the commands in Telegram's menu and the preference subcommands |
| ``url_flow`` | intake: extract + validate links, post the preview card |
| ``callbacks`` | route ``callback_data`` taps, guard against a second job |
| ``download_flow`` | one job: start, poll, progress captions, deliver the file |
| ``sessions`` | the in-memory picker sessions and their addressing scheme |
| ``texts`` | every string the user sees |
| ``stats`` | the counters behind ``/stats`` |
| ``config`` | environment-driven knobs |

``register_handlers`` is the only thing ``bot.py`` needs: the order and the
filters of the handlers are part of the bot's behaviour (a command never falls
through to the link handler), so they live in one place instead of being
scattered across the modules.
"""
from __future__ import annotations

from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import (
    callbacks,
    commands,
    config,
    download_flow,
    sessions,
    stats,
    texts,
    url_flow,
)

# Every command Telegram shows in the menu, in the order the menu lists them.
COMMAND_HANDLERS = (
    ("start", commands.start_command),
    ("help", commands.help_command),
    ("platforms", commands.platforms_command),
    ("stats", commands.stats_command),
    ("settings", commands.settings_command),
    ("setquality", commands.set_quality_command),
    ("setformat", commands.set_format_command),
    ("mp3", commands.mp3_command),
    ("mp4", commands.mp4_command),
    ("best", commands.best_command),
)

# The current ``dl:`` scheme plus the historical ``fmt:``/``qty:`` ones, so
# tapping a button in an old message still works.
CALLBACK_PATTERN = r"^(dl|fmt|qty):"


def register_handlers(application) -> None:
    """Attach every handler to a ``python-telegram-bot`` application."""
    for name, handler in COMMAND_HANDLERS:
        application.add_handler(CommandHandler(name, handler))
    # Free text only: commands are handled above and must never reach the link
    # handler, which would fetch info for a "/help" the user just typed.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, url_flow.url_handler)
    )
    application.add_handler(
        CallbackQueryHandler(callbacks.on_format_button, pattern=CALLBACK_PATTERN)
    )


__all__ = [
    "CALLBACK_PATTERN",
    "COMMAND_HANDLERS",
    "callbacks",
    "commands",
    "config",
    "download_flow",
    "register_handlers",
    "sessions",
    "stats",
    "texts",
    "url_flow",
]
