"""Keep the Bot API token out of the panel's logs.

The Channels page is the one place the dashboard talks to Telegram: it calls
``getChat``/``getChatMember`` to answer "is the bot an administrator here?", and
that request URL is ``https://<api>/bot<token>/getChat``. ``httpx`` logs every
request at INFO as that whole URL, and an ``httpx`` exception message carries it
too — so a single failed channel check could put ``BOT_TOKEN`` into journald.

Two independent guards, the same two the bot applies in ``bot/logging_setup.py``:

1. **the noisy loggers are pinned to WARNING** — no request line is emitted at
   all, so there is nothing to redact in the common case;
2. **a redaction filter is attached to every handler** — it removes the
   configured ``BOT_TOKEN``/``INTERNAL_API_TOKEN`` values and any
   ``bot<digits>:<secret>`` fragment, so a third-party logger (or a future
   ``logger.info("... %s", url)``) cannot leak one either.

This has to stay in the startup path: :func:`configure_logging` is called from
``create_app`` before the application is built.
"""
from __future__ import annotations

import logging
import os
import re

REDACTED = "<redacted>"

# A Bot API URL: https://api.telegram.org/bot123456789:AA.../getMe
_BOT_TOKEN_RE = re.compile(r"\bbot\d{4,}:[A-Za-z0-9_-]{10,}")
# The bare "<bot_id>:<secret>" fragment (an httpx.URL stringified in a message).
_BARE_TOKEN_RE = re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{30,}\b")

# Logger names whose INFO records contain full request URLs.
_NOISY_LOGGERS = ("httpx", "httpcore")

# Only redact secrets that are long enough to be unambiguous.
_MIN_SECRET_LENGTH = 8

_configured = False


def _secret_values() -> tuple[str, ...]:
    """The secret values present in the environment, longest first."""
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


class RedactSecrets(logging.Filter):
    """Replace every known secret in a record with ``<redacted>``."""

    def __init__(self, secrets: tuple[str, ...] | None = None) -> None:
        super().__init__()
        self._secrets = secrets if secrets is not None else _secret_values()

    def _scrub(self, text: str) -> str:
        cleaned = text
        for secret in self._secrets:
            cleaned = cleaned.replace(secret, REDACTED)
        cleaned = _BOT_TOKEN_RE.sub(REDACTED, cleaned)
        return _BARE_TOKEN_RE.sub(REDACTED, cleaned)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must not break logging
            return True
        scrubbed = self._scrub(message)
        if scrubbed != message:
            # The arguments are folded into the message, so clear them: a
            # handler that formats the record again would leak them.
            record.msg = scrubbed
            record.args = ()
        return True


def configure_logging(force: bool = False) -> None:
    """Pin the noisy loggers and attach the redaction filter to every handler.

    Idempotent: ``create_app`` runs once per process in production and once per
    test module in the suite, and attaching the same filter twice is harmless
    but a second pin of the loggers is not worth doing. ``force`` re-runs it for
    a test that adds a handler afterwards.
    """
    global _configured
    if _configured and not force:
        return
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    secret_filter = RedactSecrets()

    def attach(logger: logging.Logger) -> None:
        for handler in logger.handlers:
            if not any(
                isinstance(existing, RedactSecrets) for existing in handler.filters
            ):
                handler.addFilter(secret_filter)

    attach(logging.getLogger())
    for name in (logger_name for logger_name in logging.root.manager.loggerDict):
        candidate = logging.getLogger(name)
        if candidate.handlers:
            attach(candidate)
    _configured = True


__all__ = [
    "REDACTED",
    "RedactSecrets",
    "configure_logging",
]
