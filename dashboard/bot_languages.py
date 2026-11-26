"""Which languages the bot offers, as an admin setting.

The bot can speak English and Russian. Which of the two a *deployment* offers,
and which one it uses when nobody has picked yet, is an operator decision — so it
lives in the dashboard's ``settings`` table next to the download limits, and the
bot reads it from ``GET /api/bot-settings``.

Three pieces:

* :data:`LANGUAGES` / :data:`NAMES` — what the code can actually render. A code
  that is not here is not offered in the form, so the panel cannot enable a
  language ``bot/i18n.py`` would not know how to answer in.
* :class:`BotLanguages` — the validated pair (enabled languages, default), with
  the storage value kept a plain comma-separated string
  (``bot_settings.languages = "en,ru"``).
* :func:`parse` — the form validator. The one rule that matters: **at least one
  language stays on**, and the default has to be one of the enabled ones.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# What the bot can render, in the order the picker shows. Mirrors
# ``bot/i18n.py``: adding a language is a change in both services.
LANGUAGES: tuple[str, ...] = ("en", "ru")

# Autonyms: the form and the picker name a language in that language.
NAMES: dict[str, str] = {"en": "English", "ru": "Русский"}

# Storage: the same ``settings`` table the limits use, one row per key.
SETTING_PREFIX = "bot_settings."
LANGUAGES_KEY = "languages"
DEFAULT_KEY = "default_language"
SEPARATOR = ","

# A deployment that never saved anything offers both, and answers in English.
DEFAULT_LANGUAGES: tuple[str, ...] = LANGUAGES
DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class BotLanguages:
    """Enabled languages (in display order) plus the one used by default."""

    languages: tuple[str, ...]
    default_language: str

    # -- construction ------------------------------------------------------

    @classmethod
    def from_row(cls, stored: Mapping[str, str]) -> BotLanguages:
        """Read the two stored strings, tolerating anything an old row holds.

        A value that cannot be parsed falls back to the default rather than
        raising: a bot that answers is better than a panel that 500s on a row
        somebody edited by hand.
        """
        raw = (stored.get(f"{SETTING_PREFIX}{LANGUAGES_KEY}") or "").strip()
        enabled = tuple(
            code for code in (part.strip() for part in raw.split(SEPARATOR))
            if code in LANGUAGES
        )
        if not enabled:
            enabled = DEFAULT_LANGUAGES
        default = (stored.get(f"{SETTING_PREFIX}{DEFAULT_KEY}") or "").strip()
        if default not in enabled:
            default = enabled[0]
        return cls(languages=enabled, default_language=default)

    @classmethod
    def defaults(cls) -> BotLanguages:
        return cls(languages=DEFAULT_LANGUAGES, default_language=DEFAULT_LANGUAGE)

    # -- serialisation -----------------------------------------------------

    def as_stored(self) -> dict[str, str]:
        """The rows to write, in the order the languages are offered."""
        ordered = [code for code in LANGUAGES if code in self.languages]
        return {
            f"{SETTING_PREFIX}{LANGUAGES_KEY}": SEPARATOR.join(ordered),
            f"{SETTING_PREFIX}{DEFAULT_KEY}": self.default_language,
        }

    def as_form(self) -> dict[str, object]:
        """What the template needs: the checkboxes and the default select."""
        return {
            "languages": list(self.languages),
            "default_language": self.default_language,
            "options": [
                {
                    "code": code,
                    "name": NAMES[code],
                    "enabled": code in self.languages,
                    "is_default": code == self.default_language,
                }
                for code in LANGUAGES
            ],
        }

    def describe(self) -> str:
        """One line for the log: ``en,ru (default ru)``."""
        return f"{SEPARATOR.join(self.languages)} (default {self.default_language})"


def parse(enabled: Iterable[str], default: str) -> tuple[BotLanguages | None, list[str]]:
    """Validate a submitted form; returns the settings or the error messages."""
    chosen = [code for code in LANGUAGES if code in {str(c).strip() for c in enabled}]
    if not chosen:
        return None, ["Keep at least one language enabled — the bot has to answer."]

    wanted = str(default or "").strip()
    if wanted not in chosen:
        names = ", ".join(NAMES[code] for code in chosen)
        return None, [f"Choose the default language from the enabled ones ({names})."]

    return BotLanguages(languages=tuple(chosen), default_language=wanted), []


def options() -> list[dict[str, str]]:
    """Every language the form may offer (checked or not)."""
    return [{"code": code, "name": NAMES[code]} for code in LANGUAGES]


__all__ = [
    "BotLanguages",
    "DEFAULT_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "DEFAULT_KEY",
    "LANGUAGES",
    "LANGUAGES_KEY",
    "NAMES",
    "SEPARATOR",
    "SETTING_PREFIX",
    "options",
    "parse",
]
