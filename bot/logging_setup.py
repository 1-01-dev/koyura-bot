"""Logging setup for the bot that keeps Telegram tokens out of the journal.

``python-telegram-bot`` talks to the Bot API through ``httpx``. ``httpx`` logs
every request at INFO level as ``POST http://host/bot<token>/getMe ...``, which
would put the full BOT_TOKEN into journald on every start. Two independent
guards are applied here:

1. ``httpx``/``httpcore`` are pinned to WARNING, so no request line (and no
   header dump at DEBUG) is emitted at all.
2. A redaction filter is attached to every root handler. It removes the
   configured ``BOT_TOKEN``/``INTERNAL_API_TOKEN`` values and any
   ``bot<digits>:<secret>`` fragment from messages and their arguments, so a
   third-party logger or a future ``logger.info("... %s", url)`` cannot leak a
   secret even if it bypasses the level guard.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
REDACTED = "<redacted>"

# Bot API URLs look like https://api.telegram.org/bot123456789:AA.../getMe.
_BOT_TOKEN_RE = re.compile(r"\bbot\d{4,}:[A-Za-z0-9_-]{10,}")
# The bare "<bot_id>:<secret>" fragment, e.g. when the URL is passed as an
# argument object (httpx.URL) that is stringified only during formatting.
_BARE_TOKEN_RE = re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{30,}\b")

# Logger names whose INFO records contain full request URLs.
_NOISY_LOGGERS = ("httpx", "httpcore")

# Only redact secrets that are long enough to be unambiguous.
_MIN_SECRET_LENGTH = 8


def _secret_values() -> tuple[str, ...]:
    """Secret values present in the environment, longest first."""
    values = {
        (os.environ.get("BOT_TOKEN") or "").strip(),
        (os.environ.get("INTERNAL_API_TOKEN") or "").strip(),
    }
    return tuple(
        sorted(
            (value for value in values if len(value) >= _MIN_SECRET_LENGTH),
            key=len,
            reverse=True,
        )
    )


def redact_secrets(text: str) -> str:
    """Return ``text`` with known secrets and bot-token fragments removed."""
    if not text:
        return text
    for secret in _secret_values():
        text = text.replace(secret, REDACTED)
    text = _BOT_TOKEN_RE.sub(f"bot:{REDACTED}", text)
    return _BARE_TOKEN_RE.sub(REDACTED, text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


class RedactSecretsFilter(logging.Filter):
    """Strip Telegram/API tokens from log records before they are written."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            record.args = _redact_value(record.args)

        # Arguments may be objects (httpx.URL, exceptions) that are only turned
        # into text during formatting, so also redact the rendered message.
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        redacted = redact_secrets(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(
    level: str | None = None,
    *,
    stream: Any | None = None,
    force: bool = False,
) -> None:
    """Configure root logging, redaction and third-party logger levels.

    ``stream``/``force`` exist for tests; production calls it with no arguments.
    """
    resolved_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        format=LOG_FORMAT,
        level=resolved_level,
        stream=stream,
        force=force,
    )

    redactor = RedactSecretsFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactSecretsFilter) for f in handler.filters):
            handler.addFilter(redactor)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
