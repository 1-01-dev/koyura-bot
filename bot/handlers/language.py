"""Which language a chat is answered in.

One question, asked in one place: *what language does this user get?* The answer
is a saved choice from the state store when the operator still offers it,
otherwise the panel's default. Everything that renders text goes through
:func:`current`, so a user who switches language in ``/settings`` sees the very
next message change.

The set of enabled languages and the default come from ``bot_settings`` (the
panel's ``GET /api/bot-settings``, cached, with built-in defaults when the panel
is down). Nothing here talks to Telegram: the picker itself is in
``onboarding``.
"""
from __future__ import annotations

import logging

import bot_settings
import state_store

logger = logging.getLogger(__name__)


async def settings() -> bot_settings.Settings:
    """The languages this bot currently offers."""
    return await bot_settings.get()


async def current(user_id: int | None) -> str:
    """The language for one user: saved choice, else the panel's default."""
    config = await bot_settings.get()
    if user_id is None:
        return config.default_language
    return config.resolve(state_store.get_language(user_id))


async def current_with_settings(user_id: int | None) -> tuple[bot_settings.Settings, str]:
    """Both at once, for callers that also draw the language buttons."""
    config = await bot_settings.get()
    if user_id is None:
        return config, config.default_language
    return config, config.resolve(state_store.get_language(user_id))


def chosen(user_id: int | None) -> str | None:
    """The saved choice, without asking the panel (``None`` when unset)."""
    if user_id is None:
        return None
    return state_store.get_language(user_id)


def needs_picker(config: bot_settings.Settings, user_id: int | None) -> bool:
    """Whether this user must still be asked which language they want.

    Only when the bot actually offers a choice *and* the user has never picked:
    a single-language bot answers straight away, and a returning user never sees
    the screen twice.
    """
    return config.supports_choice() and chosen(user_id) is None


__all__ = [
    "chosen",
    "current",
    "current_with_settings",
    "needs_picker",
    "settings",
]
