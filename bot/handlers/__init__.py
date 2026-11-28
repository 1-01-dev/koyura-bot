"""Chat-facing Telegram handlers, split by the thing they handle.

The bot has exactly one job per module:

| module | responsibility |
| --- | --- |
| ``commands`` | the commands in Telegram's menu and the preference subcommands |
| ``onboarding`` | first contact: the language screen, the welcome and its keyboard |
| ``url_flow`` | intake: extract + validate links, post the preview card |
| ``callbacks`` | route ``callback_data`` taps, guard against a second job |
| ``download_flow`` | one job: start, poll, progress captions, deliver the file |
| ``sessions`` | the in-memory picker sessions and their addressing scheme |
| ``stages`` | the replace-not-add chain (one stage, one message) |
| ``language`` | which language one user is answered in |
| ``keyboards`` | the inline keyboards and their callback_data |
| ``texts`` | the builders that assemble catalogued strings |
| ``stats`` | the counters behind ``/stats`` |
| ``config`` | environment-driven knobs |

The words themselves are in ``bot/i18n.py``; ``texts`` only decides how the
lines are arranged (which ones ``/help`` shows, in what order).

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
    keyboards,
    language,
    onboarding,
    sessions,
    stages,
    stats,
    subscription,
    texts,
    url_flow,
)

# Every command Telegram shows in the menu, in the order the menu lists them.
COMMAND_HANDLERS = (
    ("start", onboarding.start_command),
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

# The keyboards this release adds. They keep their own prefixes (rather than
# extending ``CALLBACK_PATTERN``) so the card router's contract does not change:
# a ``lang:`` tap never looks like a download, and ``nav:home`` never looks like
# a language switch.
LANGUAGE_PATTERN = r"^lang:"
WELCOME_PATTERN = r"^welcome:"
NAV_PATTERN = r"^nav:"
#: The subscription gate's buttons (``sub:check``, ``sub:open:<id>``). Its own
#: scheme, so a gate button never looks like a language or a download.
SUBSCRIPTION_PATTERN = r"^sub:"


def register_handlers(application) -> None:
    """Attach every handler to a ``python-telegram-bot`` application."""
    for name, handler in COMMAND_HANDLERS:
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(
        CallbackQueryHandler(callbacks.on_format_button, pattern=CALLBACK_PATTERN)
    )
    application.add_handler(
        CallbackQueryHandler(onboarding.on_language_button, pattern=LANGUAGE_PATTERN)
    )
    application.add_handler(
        CallbackQueryHandler(onboarding.on_welcome_button, pattern=WELCOME_PATTERN)
    )
    application.add_handler(
        CallbackQueryHandler(onboarding.on_nav_button, pattern=NAV_PATTERN)
    )
    application.add_handler(
        CallbackQueryHandler(
            subscription.on_subscription_button, pattern=SUBSCRIPTION_PATTERN
        )
    )
    # Free text only: commands are handled above and must never reach the link
    # handler, which would fetch info for a "/help" the user just typed.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, url_flow.url_handler)
    )


__all__ = [
    "CALLBACK_PATTERN",
    "COMMAND_HANDLERS",
    "LANGUAGE_PATTERN",
    "NAV_PATTERN",
    "SUBSCRIPTION_PATTERN",
    "WELCOME_PATTERN",
    "callbacks",
    "commands",
    "config",
    "download_flow",
    "keyboards",
    "language",
    "onboarding",
    "register_handlers",
    "sessions",
    "stages",
    "stats",
    "subscription",
    "texts",
    "url_flow",
]
