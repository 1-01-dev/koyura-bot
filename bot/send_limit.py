"""The size Telegram will accept, and the sizes the engine reports.

A bot backed by the self-hosted Bot API can upload 2 GB; the public cloud API
stops at 20 MB. The ceiling is therefore a deployment fact, not a constant of
the code:

    MAX_SEND_MB=2000    the default: the self-hosted Bot API server
    MAX_SEND_MB=20      api.telegram.org without a paid premium account

Offering a format the bot cannot send is worse than not offering it at all: the
user taps, waits for a download, and only then reads that the file was too big.
``download_card`` filters the keyboard through :func:`fits`; a size the engine
does not know is **not** filtered — the engine still refuses it with its own
``MAX_FILESIZE_MB``, but the user keeps the choice.

Everything here is pure and reads ``MAX_SEND_MB`` at call time, so a test (or a
future panel setting) can change the ceiling without re-importing the module.
"""
from __future__ import annotations

import os

BYTES_PER_MB = 1024 * 1024
DEFAULT_MAX_SEND_MB = 2000


def _read_mb(name: str, default: int) -> int:
    """A positive whole number of megabytes from the environment, or ``default``.

    A typo in the environment must not stop the bot from starting: this is a
    comfort setting, and the fallback is the documented one.
    """
    raw = os.environ.get(name, "")
    try:
        value = int(float(raw.strip()))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Read once at import, like the rest of the bot's configuration; the helpers
# below read the module attribute on every call.
MAX_SEND_MB = _read_mb("MAX_SEND_MB", DEFAULT_MAX_SEND_MB)


def max_send_bytes() -> int:
    """The ceiling in bytes."""
    return int(MAX_SEND_MB) * BYTES_PER_MB


def as_bytes(value: object) -> int | None:
    """A positive whole number of bytes, or ``None`` for "not reported"."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def known_size(entry: object) -> int | None:
    """The size a format summary reports, when it reports one.

    The engine's payload uses ``size_bytes``; the ``filesize``/``filesize_approx``
    names are yt-dlp's and are accepted so a raw blob (or an older engine) works
    too.
    """
    if not isinstance(entry, dict):
        return None
    for key in ("size_bytes", "filesize", "filesize_approx"):
        size = as_bytes(entry.get(key))
        if size is not None:
            return size
    return None


def audio_size(info: object) -> int | None:
    """The MP3 the bot would send, in bytes, or ``None`` when it cannot tell.

    The engine already estimates the audio summary (bitrate × duration); when
    only the raw bitrate and the duration are there, the same arithmetic is done
    here — ``bits/second × seconds / 8`` — because the MP3 button has to know how
    big the result will be before it is tapped.
    """
    if not isinstance(info, dict):
        return None
    reported = known_size(info.get("audio"))
    if reported is not None:
        return reported
    audio = info.get("audio")
    if not isinstance(audio, dict):
        return None
    bitrate = as_bytes(audio.get("abr")) or as_bytes(audio.get("tbr"))
    duration = as_bytes(info.get("duration"))
    if bitrate and duration:
        return int(bitrate * 1000 / 8 * duration)
    return None


def fits(size: object, limit_bytes: int | None = None) -> bool:
    """Whether a known size may be sent; an unknown size always "fits".

    The comparison is strict: ``size == limit`` is allowed, one byte more is
    not, and there is no safety margin — the number the engine reports is the
    number Telegram checks.
    """
    known = as_bytes(size)
    if known is None:
        return True
    ceiling = max_send_bytes() if limit_bytes is None else int(limit_bytes)
    return known <= ceiling


__all__ = [
    "BYTES_PER_MB",
    "DEFAULT_MAX_SEND_MB",
    "MAX_SEND_MB",
    "as_bytes",
    "audio_size",
    "fits",
    "known_size",
    "max_send_bytes",
]
