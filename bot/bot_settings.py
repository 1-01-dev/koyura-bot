"""The bot's operator-set preferences: the languages it offers and the message
bodies it renders.

The panel owns both settings (``bot_settings.*`` in its ``settings`` table and
the ``message_templates`` table); the bot only reads them from the internal API:

    GET {DASHBOARD_URL}/api/bot-settings
    X-Internal-Token: ****

    {"languages": ["en", "ru"], "default_language": "en",
     "templates": {"progress": {"en": "*@title*\n@stage\n@bar"}}}

Three properties matter more than the request itself:

* **the bot works with the panel down.** A dashboard that is restarting must not
  take the chat down with it, so an unreachable panel (or a payload we cannot
  parse, or an empty language set) falls back to the built-in defaults
  ``("en", "ru")`` with ``en`` as the default and no custom bodies — which is
  exactly the same state as "nothing was ever saved". The fallback is a normal
  state, not an error: it is logged at DEBUG.
* **one fetch per minute, not one per message.** Every message would otherwise
  cost a round trip before the bot could answer. The answer is cached for
  ``TTL_SECONDS``; a settings change in the panel reaches the bot within that
  window, which is what the owner expects from a preference.
* **only the languages the code can render.** ``i18n`` knows ``en`` and ``ru``;
  an unknown code in the setting is dropped instead of producing a bot that
  answers in a catalogue that does not exist. The same rule applies to a custom
  body: a template for a kind this build does not send is dropped (see
  :func:`normalise_templates`), never an error.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

import i18n

logger = logging.getLogger(__name__)

# Same address and secret as the event stream (``event_client``), read per call
# so a test can point the bot somewhere else without re-importing the module.
DEFAULT_DASHBOARD_URL = "http://dashboard:8080"
SETTINGS_PATH = "/api/bot-settings"

# The panel is polled at most this often. Well under a minute, so an operator who
# flips a language does not wonder whether it "took".
TTL_SECONDS = 45.0

# A panel that cannot answer within two seconds is not worth blocking a reply on.
_TIMEOUT_SECONDS = 2.0

# What the bot offers when it has never reached the panel.
DEFAULT_LANGUAGES: tuple[str, ...] = ("en", "ru")
DEFAULT_LANGUAGE: str = i18n.DEFAULT_LANGUAGE


@dataclass(frozen=True)
class Settings:
    """Enabled languages, their default, and the custom message bodies."""

    languages: tuple[str, ...]
    default_language: str
    source: str = "fallback"
    # ``{kind: {lang: body}}``: the custom message bodies the panel activated.
    # Empty means "every message is the built-in default", which is also what a
    # panel we cannot reach gives us.
    templates: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def custom_template(self, kind: str, lang: object) -> str | None:
        """The active body for one kind in one language, or ``None``."""
        by_language = self.templates.get(str(kind))
        if not isinstance(by_language, Mapping):
            return None
        body = by_language.get(i18n.normalise(lang))
        return str(body) if body else None

    def resolve(self, choice: object) -> str:
        """The language to speak to one user: their choice, else the default.

        A saved choice that the operator has since disabled is not honoured —
        otherwise turning a language off would only affect new users.
        """
        wanted = i18n.normalise(choice)
        if str(choice or "").strip() and wanted in self.languages:
            return wanted
        return self.default_language

    def supports_choice(self) -> bool:
        """Whether the first-contact language screen makes sense at all."""
        return len(self.languages) > 1


def defaults() -> Settings:
    """The built-in answer: used before the first fetch and when it fails."""
    return Settings(
        languages=DEFAULT_LANGUAGES,
        default_language=DEFAULT_LANGUAGE,
        templates={},
    )


def _dashboard_url() -> str:
    return os.environ.get("DASHBOARD_URL", "").strip() or DEFAULT_DASHBOARD_URL


def _headers() -> dict[str, str]:
    token = os.environ.get("INTERNAL_API_TOKEN", "").strip()
    return {"X-Internal-Token": token} if token else {}


def normalise_templates(raw: object) -> dict[str, dict[str, str]]:
    """``{kind: {lang: body}}`` from an API body, dropping what cannot render.

    A body that is not a string, an empty body and a language the catalogue
    does not know are skipped: the bot then sends the built-in default for that
    message, which is exactly what the panel promises for a broken template.
    """
    if not isinstance(raw, Mapping):
        return {}
    import message_kinds  # local: the registry is not needed on the hot path

    cleaned: dict[str, dict[str, str]] = {}
    for kind, by_language in raw.items():
        if not isinstance(kind, str) or not isinstance(by_language, Mapping):
            continue
        if message_kinds.get(kind) is None:
            # A body for a kind this build does not send (a retired message):
            # shipping it would only have the bot fall back to the default.
            continue
        entry: dict[str, str] = {}
        for lang, body in by_language.items():
            name = i18n.normalise(lang)
            if not isinstance(body, str) or not body.strip():
                continue
            entry[name] = body
        if entry:
            cleaned[kind] = entry
    return cleaned


def normalise_payload(payload: object) -> Settings:
    """Turn an API body into ``Settings``, dropping what cannot be rendered.

    ``{"languages": ["en", "ru"], "default_language": "ru", "templates": {...}}``
    is the contract; an unknown code is skipped, an empty result is the default
    set, and a default that is not enabled becomes the first enabled language.
    A body without ``templates`` (an older panel) keeps every built-in default.
    """
    if not isinstance(payload, dict):
        return defaults()

    raw = payload.get("languages")
    if isinstance(raw, str):
        raw = raw.split(",")
    language_list = raw if isinstance(raw, (list, tuple)) else []

    enabled: list[str] = []
    for entry in language_list:
        code = i18n.normalise(entry)
        if str(entry or "").strip().lower().startswith(code) and code not in enabled:
            enabled.append(code)
    if not enabled:
        enabled = list(DEFAULT_LANGUAGES)

    wanted = i18n.normalise(payload.get("default_language"))
    default = wanted if wanted in enabled else enabled[0]
    return Settings(
        languages=tuple(enabled),
        default_language=default,
        source="panel",
        templates=normalise_templates(payload.get("templates")),
    )


async def fetch() -> Settings:
    """Ask the panel once; never raises."""
    url = f"{_dashboard_url()}{SETTINGS_PATH}"
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS, headers=_headers()
        ) as client:
            response = await client.get(url)
        if response.status_code != 200:
            logger.debug("Bot settings unavailable (HTTP %s)", response.status_code)
            return defaults()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - the panel must never stop the bot
        logger.debug("Bot settings unavailable (%s); using built-in defaults", exc)
        return defaults()
    settings = normalise_payload(payload)
    logger.debug(
        "Bot settings from the panel: %s (default %s), %d custom template(s)",
        ",".join(settings.languages),
        settings.default_language,
        sum(len(bodies) for bodies in settings.templates.values()),
    )
    return settings


_cache: Settings | None = None
_cache_at: float = 0.0


async def get(now: float | None = None) -> Settings:
    """The current settings, from cache when it is fresh enough."""
    global _cache, _cache_at
    moment = time.monotonic() if now is None else float(now)
    if _cache is not None and moment - _cache_at < TTL_SECONDS:
        return _cache
    settings = await fetch()
    _cache = settings
    _cache_at = moment
    return settings


def reset_cache() -> None:
    """Test helper: forget the cached answer (and the built-in fallback)."""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def cached() -> Settings | None:
    """The last answer, without a round trip (``None`` before the first fetch).

    Message rendering is synchronous — a caption cannot await a settings fetch
    — so it reads this snapshot. A bot that has not polled yet, or whose panel
    is down, has ``None`` here and renders the built-in defaults.
    """
    return _cache


def prime(settings: Settings, *, now: float | None = None) -> None:
    """Test helper: install an answer without a network round trip."""
    global _cache, _cache_at
    _cache = settings
    _cache_at = time.monotonic() if now is None else float(now)


__all__ = [
    "DEFAULT_LANGUAGE",
    "DEFAULT_LANGUAGES",
    "SETTINGS_PATH",
    "Settings",
    "TTL_SECONDS",
    "cached",
    "defaults",
    "fetch",
    "get",
    "normalise_payload",
    "normalise_templates",
    "prime",
    "reset_cache",
]
