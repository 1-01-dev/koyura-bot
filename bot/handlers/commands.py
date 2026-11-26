"""The commands in Telegram's menu, plus the preference subcommands.

Each handler is a thin script: read what the user sent, decide, reply. Anything
that needs the engine or the event loop is left to ``url_flow`` and
``download_flow`` — commands themselves never block.

``/mp3``, ``/mp4`` and ``/best`` are shortcuts *around* the picker: they take
links (in the command or in the message being replied to) and start a download
directly, exactly like tapping a button would.
"""
from __future__ import annotations

import logging
from pathlib import Path

from telegram import Message, Update, User
from telegram.ext import ContextTypes

import state_store

from . import config, download_flow, sessions, stats, texts, url_flow

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Static answers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, _ = target
    await message.reply_text(texts.START)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, _ = target
    await message.reply_text(texts.HELP, parse_mode="MarkdownV2")


async def platforms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, _ = target
    await message.reply_text(texts.platforms_report())


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Uptime, counters, and what the shared volume currently holds."""
    target = _addressed(update)
    if target is None:
        return
    message, _ = target

    cached_files = 0
    disk_mb = 0.0
    downloads_dir = Path(config.DOWNLOADS_PATH)
    if downloads_dir.exists():
        for file in downloads_dir.iterdir():
            if file.is_file():
                disk_mb += file.stat().st_size / (1024 * 1024)
                cached_files += 1

    await message.reply_text(
        texts.stats_report(
            uptime_seconds=stats.uptime_seconds(),
            downloads=stats.COUNTERS["downloads"],
            errors=stats.COUNTERS["errors"],
            cached_files=cached_files,
            disk_mb=disk_mb,
            sessions=sessions.live_count(),
        )
    )


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    prefs = state_store.get_prefs(user.id)
    await message.reply_text(
        texts.settings_report(prefs.get("quality", "best"), prefs.get("format", "video"))
    )


async def set_quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    if not context.args:
        await message.reply_text(texts.SETQUALITY_USAGE)
        return
    quality = context.args[0].lower()
    if quality not in texts.QUALITY_OPTIONS:
        await message.reply_text(texts.invalid_quality())
        return
    state_store.set_pref(user.id, "quality", quality)
    await message.reply_text(texts.quality_saved(quality))


async def set_format_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _addressed(update)
    if target is None:
        return
    message, user = target
    if not context.args:
        await message.reply_text(texts.SETFORMAT_USAGE)
        return
    fmt = context.args[0].lower()
    if fmt not in texts.FORMAT_OPTIONS:
        await message.reply_text(texts.invalid_format())
        return
    state_store.set_pref(user.id, "format", fmt)
    await message.reply_text(texts.format_saved(fmt))


# ---------------------------------------------------------------------------
# Direct downloads
# ---------------------------------------------------------------------------

async def _direct(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    fmt: str,
    started: str,
    usage: str,
) -> None:
    """Shared body of ``/mp3`` and ``/mp4``: links in, one job per link."""
    target = _addressed(update)
    if target is None:
        return
    message, _ = target

    urls, notice = await url_flow.safe_links(url_flow.command_urls(update))
    if notice:
        await message.reply_text(notice)
    if not urls:
        await message.reply_text(usage)
        return

    for url in urls:
        status = await message.reply_text(started)
        await download_flow.run_direct(update, context, status, url, fmt, None)


async def mp3_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """MP3 directly, no picker."""
    await _direct(update, context, fmt="audio", started=texts.MP3_STARTED, usage=texts.MP3_USAGE)


async def mp4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Best-quality MP4 directly, no picker."""
    await _direct(update, context, fmt="video", started=texts.MP4_STARTED, usage=texts.MP4_USAGE)


async def best_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/best`` is ``/mp4``: same engine choice, same card, same wording."""
    await mp4_command(update, context)
