"""The inline keyboards the bot draws, and the ``callback_data`` they carry.

Three keyboards exist, and each one has its own ``callback_data`` scheme so a
router can dispatch on the first field alone:

    lang:<code>          the first-contact language picker
    lang:<code>:s        the same switch, tapped on the /settings screen
    welcome:<action>     help / platforms / settings / stats / mp3 / mp4
    nav:home             the Back button every sub-screen carries

Keeping the data format next to the buttons means a button and its router can
never disagree about the shape of the string, and the two ``lang:`` variants
differ by a suffix instead of a second prefix, so an old message keeps working.

Everything here is pure: no Telegram calls, no state.
"""
from __future__ import annotations

import i18n
from i18n import t

from . import texts

# Two buttons per row: a phone shows them side by side, and a label the width of
# "Статистика" still fits.
BUTTONS_PER_ROW = 2

# Callback prefixes. ``callback_prefixes`` is what ``register_handlers`` wires.
LANGUAGE_PREFIX = "lang"
WELCOME_PREFIX = "welcome"
NAV_PREFIX = "nav"

# The one navigation target so far: the welcome screen. A button carries a
# ``callback_data`` and nothing else, so an old message stays valid.
HOME_ACTION = "home"

# The suffix that marks "this tap came from /settings, stay here".
_SETTINGS_SUFFIX = "s"


# ---------------------------------------------------------------------------
# callback_data
# ---------------------------------------------------------------------------

def language_data(code: str, *, from_settings: bool = False) -> str:
    """``lang:en`` for the picker, ``lang:en:s`` for the /settings screen."""
    base = f"{LANGUAGE_PREFIX}:{i18n.normalise(code)}"
    return f"{base}:{_SETTINGS_SUFFIX}" if from_settings else base


def welcome_data(action: str) -> str:
    return f"{WELCOME_PREFIX}:{action}"


def nav_data(action: str = HOME_ACTION) -> str:
    """``nav:home`` — the Back button on every sub-screen."""
    return f"{NAV_PREFIX}:{action}"


def parse_language(data: object) -> tuple[str, bool] | None:
    """``"lang:ru:s"`` -> ``("ru", True)``; anything else -> ``None``."""
    parts = str(data or "").split(":")
    if len(parts) < 2 or parts[0] != LANGUAGE_PREFIX:
        return None
    code = i18n.normalise(parts[1])
    return code, len(parts) > 2 and parts[2] == _SETTINGS_SUFFIX


def parse_welcome(data: object) -> str | None:
    """``"welcome:help"`` -> ``"help"``; anything else -> ``None``."""
    parts = str(data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != WELCOME_PREFIX:
        return None
    action = parts[1].strip()
    return action or None


def parse_nav(data: object) -> str | None:
    """``"nav:home"`` -> ``"home"``; anything else -> ``None``."""
    parts = str(data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != NAV_PREFIX:
        return None
    action = parts[1].strip()
    return action or None


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _rows(buttons):
    return [
        buttons[index : index + BUTTONS_PER_ROW]
        for index in range(0, len(buttons), BUTTONS_PER_ROW)
    ]


def language_switch(
    config, current: str, *, from_settings: bool = False
):
    """One button per **enabled** language, the current one ticked.

    ``config`` is ``bot_settings.Settings``; a language the operator turned off
    is not offered, and the tick makes "which one am I on" answerable without a
    second line of text.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = []
    for code in config.languages:
        label = texts.language_name(code)
        if code == current:
            label = f"✓ {label}"
        buttons.append(
            InlineKeyboardButton(
                label, callback_data=language_data(code, from_settings=from_settings)
            )
        )
    return InlineKeyboardMarkup(_rows(buttons))


def welcome(lang: object = None):
    """``[Help] [Platforms] / [Settings] [Stats] / [MP3] [MP4]``.

    The keyboard replaces the list of commands the welcome used to spell out: the
    same answers, one tap away, in the language of the chat.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = (
        ("help", "button.help"),
        ("platforms", "button.platforms"),
        ("settings", "button.settings"),
        ("stats", "button.stats"),
        ("mp3", "button.mp3"),
        ("mp4", "button.mp4"),
    )
    buttons = [
        InlineKeyboardButton(t(label_key, lang), callback_data=welcome_data(action))
        for action, label_key in rows
    ]
    return InlineKeyboardMarkup(_rows(buttons))


def back(lang: object = None):
    """The single ``Back`` button every sub-screen carries (``nav:home``).

    It is a plain callback rather than a link, so the answer is the welcome
    screen **in the message the button sits on** — the user does not have to send
    ``/start`` again and the chat does not grow a second copy of the welcome.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t("nav.back", lang), callback_data=nav_data())]
    ])


def with_back(markup, lang: object = None):
    """``markup`` with the Back row appended (``None`` markup -> Back only)."""
    from telegram import InlineKeyboardMarkup

    rows = list(getattr(markup, "inline_keyboard", []) or [])
    rows.extend(back(lang).inline_keyboard)
    return InlineKeyboardMarkup(rows)


__all__ = [
    "BUTTONS_PER_ROW",
    "HOME_ACTION",
    "LANGUAGE_PREFIX",
    "NAV_PREFIX",
    "WELCOME_PREFIX",
    "back",
    "language_data",
    "language_switch",
    "nav_data",
    "parse_language",
    "parse_nav",
    "parse_welcome",
    "welcome",
    "welcome_data",
    "with_back",
]
