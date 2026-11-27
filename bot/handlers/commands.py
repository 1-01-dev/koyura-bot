"""The commands in Telegram's menu, plus the preference subcommands.

Each handler is a thin script: read what the user sent, decide, reply. Anything
that needs the engine or the event loop is left to ``url_flow`` and
``download_flow`` — commands themselves never block.

``/mp3``, ``/mp4`` and ``/best`` are shortcuts *around* the picker: they take
links (in the command or in the message being replied to) and start a download
directly, exactly like tapping a button would.

The answers themselves are built by the ``reply_*`` helpers below, because the
welcome keyboard (``onboarding``) offers the same four screens as buttons. Both
entry points therefore render from one place, in the language of the chat.
"""
from __future__ import annotations

import logging
from pathlib import Path

from telegram import Message, Update, User
from telegram.ext import ContextTypes

import state_store

from . import (
    config,
    download_flow,
    keyboards,
    language,
    sessions,
    stats,
    texts,
    url_flow,
)

logger = logging.getLogger(__name__)

# Every answer is sent with MarkdownV2: ``texts`` escapes the prose, so a title
# or a platform name with a special character cannot break the message.
PARSE_MODE = "MarkdownV2"


def _addressed(update: Update) -> tuple[Message, User] | None:
    """``(message, user)``, or ``None`` for updates that carry neither.

    Channel posts and service messages reach command handlers too; they have no
    user to attribute a preference to and no message to reply to.
    """
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return None
    return message, user


async def _lang(user: User | None) -> str:
    return await language.current(getattr(user, "id", None))


# ---------------------------------------------------------------------------
# The four screens (commands and welcome buttons share them)
#
# Each screen has a ``*_payload`` that only *builds* the answer, and a ``reply_*``
# that sends it as a new message. The welcome keyboard uses the payloads, because
# its answer replaces the message the button sits on instead of adding one more.
# ---------------------------------------------------------------------------

async def help_payload(user: User | None = None) -> str:
    return texts.help_text(await _lang(user))


async def platforms_payload(user: User | None = None) -> str:
    return texts.platforms_report(await _lang(user))


async def stats_payload(user: User | None = None) -> str:
    """Uptime, counters, and what the shared volume currently holds."""
    lang = await _lang(user)

    cached_files = 0
    disk_mb = 0.0
    downloads_dir = Path(config.DOWNLOADS_PATH)
    if downloads_dir.exists():
        for file in downloads_dir.iterdir():
            if file.is_file():
                disk_mb += file.stat().st_size / (1024 * 1024)
                cached_files += 1

    return texts.stats_report(
        uptime_seconds=stats.uptime_seconds(),
        downloads=stats.COUNTERS["downloads"],
        errors=stats.COUNTERS["errors"],
        cached_files=cached_files,
        disk_mb=disk_mb,
        sessions=sessions.live_count(),
        lang=lang,
    )


async def settings_payload(user: User | None = None):
    """The /settings answer, its language buttons and Back (``(text, markup)``)."""
    user_id = getattr(user, "id", None)
    settings, lang = await language.current_with_settings(user_id)
    prefs = state_store.get_prefs(user_id) if user_id is not None else {}
    mode = state_store.get_pending_format(user_id) if user_id is not None else None

    text = texts.settings_report(
        quality=prefs.get("quality", "best"),
        fmt=prefs.get("format", "video"),
        language=lang,
        mode=mode,
        lang=lang,
    )
    return text, keyboards.with_back(
        keyboards.language_switch(settings, lang, from_settings=True), lang
    )


async def reply_help(message: Message, user: User | None = None) -> None:
    """A sub-screen always carries Back: the chat can never strand the user."""
    lang = await _lang(user)
    await message.reply_text(
        await help_payload(user),
        parse_mode=PARSE_MODE,
        reply_markup=keyboards.back(lang),
    )


async def reply_platforms(message: Message, user: User | None = None) -> None:
    lang = await _lang(user)
    await message.reply_text(
        await platforms_payload(user),
        parse_mode=PARSE_MODE,
        reply_markup=keyboards.back(lang),
    )


async def reply_stats(message: Message, user: User | None = None) -> None:
    lang = await _lang(user)
    await message.reply_text(
        await stats_payload(user),
        parse_mode=PARSE_MODE,
        reply_markup=keyboards.back(lang),
    )


async def reply_settings(message: Message, user: User | None = None) -> None:
    """The stored defaults, the current language and the armed mode."""
    text, markup = await settings_payload(user)
    await message.reply_text(text, parse_mode=PARSE_MODE, reply_markup=markup)


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    await reply_help(message, user)


async def platforms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    await reply_platforms(message, user)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    await reply_stats(message, user)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    await reply_settings(message, user)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

async def set_quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    lang = await _lang(user)
    if not context.args:
        await message.reply_text(texts.md("quality.usage", lang), parse_mode=PARSE_MODE)
        return
    quality = context.args[0].lower()
    if quality not in texts.QUALITY_OPTIONS:
        await message.reply_text(
            texts.md("quality.invalid", lang, options=", ".join(texts.QUALITY_OPTIONS)),
            parse_mode=PARSE_MODE,
        )
        return
    state_store.set_pref(user.id, "quality", quality)
    await message.reply_text(
        texts.md("quality.saved", lang, quality=quality), parse_mode=PARSE_MODE
    )


async def set_format_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    lang = await _lang(user)
    if not context.args:
        await message.reply_text(texts.md("format.usage", lang), parse_mode=PARSE_MODE)
        return
    fmt = context.args[0].lower()
    if fmt not in texts.FORMAT_OPTIONS:
        await message.reply_text(
            texts.md("format.invalid", lang, options=", ".join(texts.FORMAT_OPTIONS)),
            parse_mode=PARSE_MODE,
        )
        return
    state_store.set_pref(user.id, "format", fmt)
    await message.reply_text(
        texts.md("format.saved", lang, format=fmt), parse_mode=PARSE_MODE
    )


# ---------------------------------------------------------------------------
# Direct downloads
# ---------------------------------------------------------------------------

async def _direct(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    fmt: str,
    started_key: str,
    usage_key: str,
) -> None:
    """Shared body of ``/mp3`` and ``/mp4``: links in, one job per link."""
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    lang = await _lang(user)

    urls, notice = await url_flow.safe_links(url_flow.command_urls(update), lang=lang)
    if notice:
        await message.reply_text(notice, parse_mode=PARSE_MODE)
    if not urls:
        await message.reply_text(texts.md(usage_key, lang), parse_mode=PARSE_MODE)
        return

    # An explicit format on the command line supersedes a mode armed from the
    # welcome keyboard: the user just said exactly what they want.
    state_store.set_pending_format(user.id, None)
    started = texts.md(started_key, lang)
    for url in urls:
        status = await message.reply_text(started, parse_mode=PARSE_MODE)
        await download_flow.run_direct(
            update, context, status, url, fmt, None, lang=lang
        )


async def mp3_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """MP3 directly, no picker."""
    await _direct(
        update,
        context,
        fmt="audio",
        started_key="mp3.started",
        usage_key="mp3.usage",
    )


async def mp4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Best-quality MP4 directly, no picker."""
    await _direct(
        update,
        context,
        fmt="video",
        started_key="mp4.started",
        usage_key="mp4.usage",
    )


async def best_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/best`` is ``/mp4``: same engine choice, same card, same wording."""
    await mp4_command(update, context)


__all__ = [
    "PARSE_MODE",
    "best_command",
    "help_command",
    "help_payload",
    "mp3_command",
    "mp4_command",
    "platforms_command",
    "platforms_payload",
    "reply_help",
    "reply_platforms",
    "reply_settings",
    "reply_stats",
    "set_format_command",
    "set_quality_command",
    "settings_command",
    "settings_payload",
    "stats_command",
    "stats_payload",
]
