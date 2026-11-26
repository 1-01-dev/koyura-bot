"""Regression tests: the bot token must never reach the logs."""
from __future__ import annotations

import io
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from logging_setup import RedactSecretsFilter, configure_logging, redact_secrets

# Built from parts so the literal does not look like a real credential.
FAKE_TOKEN = "123456789:" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


def _capturing_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """A logger that writes through the redaction filter into a StringIO."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(RedactSecretsFilter())

    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


# ---------------------------------------------------------------------------
# Third-party HTTP loggers stay quiet
# ---------------------------------------------------------------------------

def test_bot_entrypoint_silences_httpx_loggers():
    import bot  # noqa: F401 - importing runs configure_logging()

    for name in ("httpx", "httpcore"):
        level = logging.getLogger(name).getEffectiveLevel()
        assert level >= logging.WARNING, (
            f"{name} must not log INFO request lines (token in URL), got {level}"
        )


def test_httpx_info_records_are_not_enabled():
    configure_logging()
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)


def test_configure_logging_attaches_redaction_to_root_handlers():
    configure_logging()
    root = logging.getLogger()
    assert any(
        isinstance(log_filter, RedactSecretsFilter)
        for handler in root.handlers
        for log_filter in handler.filters
    ), "root handlers must filter secrets"


# ---------------------------------------------------------------------------
# Redaction of anything that still reaches a handler
# ---------------------------------------------------------------------------

def test_bot_api_url_with_token_is_redacted():
    logger, stream = _capturing_logger("test.koyura.token-url")
    logger.info(
        'HTTP Request: POST http://127.0.0.1:8081/bot%s/getMe "HTTP/1.1 200 OK"',
        FAKE_TOKEN,
    )

    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    assert "A1b2C3d4" not in output
    assert "<redacted>" in output
    assert "getMe" in output  # the useful part of the line survives


def test_token_inside_argument_object_is_redacted():
    """httpx logs its URL as an object, not a string argument."""

    class FakeURL:
        def __str__(self) -> str:
            return f"http://127.0.0.1:8081/bot{FAKE_TOKEN}/deleteWebhook"

    logger, stream = _capturing_logger("test.koyura.object-arg")
    logger.info('HTTP Request: POST %s "HTTP/1.1 200 OK"', FakeURL())

    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    assert "deleteWebhook" in output


def test_token_in_message_string_is_redacted():
    logger, stream = _capturing_logger("test.koyura.token-message")
    logger.warning(f"calling https://api.telegram.org/bot{FAKE_TOKEN}/setMyCommands")

    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    assert "setMyCommands" in output  # the useful part of the line survives


def test_configured_internal_token_is_redacted(monkeypatch):
    secret = "internal-" + "s" * 24
    monkeypatch.setenv("INTERNAL_API_TOKEN", secret)

    logger, stream = _capturing_logger("test.koyura.internal-token")
    logger.warning("engine rejected header X-Internal-Token: %s", secret)

    output = stream.getvalue()
    assert secret not in output
    assert "<redacted>" in output


def test_redact_secrets_ignores_short_values(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "short")
    assert redact_secrets("value=short") == "value=short"


def test_ordinary_messages_are_unchanged():
    logger, stream = _capturing_logger("test.koyura.ordinary")
    logger.info("Download job %s finished with %d bytes", "job-42", 1024)

    output = stream.getvalue()
    assert "Download job job-42 finished with 1024 bytes" in output


def test_filter_does_not_drop_records():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain message",
        args=(),
        exc_info=None,
    )
    assert RedactSecretsFilter().filter(record) is True
    assert record.msg == "plain message"


def test_redact_secrets_tolerates_non_string_arguments():
    logger, stream = _capturing_logger("test.koyura.args")
    logger.info("job %s took %s seconds", "job-7", 3.5)

    output = stream.getvalue()
    assert "job job-7 took 3.5 seconds" in output
