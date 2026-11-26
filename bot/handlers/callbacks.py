"""Routing for everything that arrives as a ``callback_data`` tap.

One handler, one pattern (``^(dl|fmt|qty):``), so the current ``dl:`` cards and
the historical ``fmt:``/``qty:`` buttons sitting in old chats all land here and
behave the same way:

* the tap is resolved to a live session (or the user is told it expired);
* a legacy "MP4" button re-renders the format keyboard instead of guessing;
* an already-running card refuses a second job with an explanation;
* anything else becomes a download task.

``query.answer()`` is called exactly once on every path — Telegram rejects a
second answer for the same query, so the generator of this handler must be
straight-line code.
"""
from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

import download_card

from . import download_flow, sessions, texts

# Older releases shipped two separate keyboards: ``fmt:`` chose video/audio and
# ``qty:`` chose a quality. ``video``/``back`` therefore mean "show me the
# formats", not "download the best one".
PICKER_ACTIONS = frozenset({"video", "back"})

# The picker's audio button used to be called ``audio``; the engine format is
# still ``audio``, the action is now ``mp3``.
ACTION_ALIASES = {"audio": "mp3"}


async def on_format_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single entry point for every card button (new and legacy data)."""
    sessions.evict_stale()
    query = update.callback_query
    if query is None:
        return

    parsed = sessions.parse_callback_data(query.data or "")
    if parsed is None:
        await query.answer()
        return

    message_id, url_hash, action = parsed
    # ``query.message`` is Optional and may be an inaccessible message, so the
    # card is addressed by the ids recorded when it was posted.
    entry = sessions.find(query, message_id, url_hash)
    if entry is None:
        await query.answer()
        await query.edit_message_text(texts.SESSION_EXPIRED)
        return

    info = entry["info"]
    if action in PICKER_ACTIONS:
        # Legacy MP4 button: show the per-format keyboard instead of guessing.
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=download_card.build_format_keyboard(info, message_id, url_hash)
        )
        return

    action = ACTION_ALIASES.get(action, action)

    # The card locks itself while a job runs: a second format on the same
    # message would race for the same caption and upload the wrong file.
    if entry.get("job_active"):
        await query.answer(texts.ALREADY_DOWNLOADING, show_alert=False)
        return
    entry["job_active"] = True
    await query.answer()

    fmt, format_id, height = download_flow.resolve_choice(info, action)
    asyncio.create_task(
        download_flow.run_download(
            context=context,
            chat_id=entry["chat_id"],
            status_message_id=message_id,
            url=entry["url"],
            info=info,
            fmt=fmt,
            format_id=format_id,
            height=height,
            user_id=entry["user_id"],
            username=entry.get("username", ""),
            started_at=entry["created"],
            url_hash=url_hash,
            entry=entry,
        )
    )
