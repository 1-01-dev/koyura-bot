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
import logging

from telegram import Update
from telegram.ext import ContextTypes

import download_card

from . import download_flow, sessions, stages, texts

logger = logging.getLogger(__name__)

# Older releases shipped two separate keyboards: ``fmt:`` chose video/audio and
# ``qty:`` chose quality. ``video``/``back`` therefore mean "show me the
# formats", not "download the best one".
PICKER_ACTIONS = frozenset({"video", "back"})

# The picker's audio button used to be called ``audio``; the engine format is
# still ``audio``, the action is now ``mp3``.
ACTION_ALIASES = {"audio": "mp3"}


def _card_chat_id(query) -> int | None:
    """The chat the tapped card lives in, or ``None`` when it is inaccessible."""
    chat = getattr(getattr(query, "message", None), "chat", None)
    chat_id = getattr(chat, "id", None)
    return int(chat_id) if chat_id else None


def _is_photo(query) -> bool:
    """Whether the tapped card is a media message (caption edits) or text."""
    message = getattr(query, "message", None)
    return bool(getattr(message, "photo", None))


async def _expired_card(query, context, card) -> None:
    """Tell the user the session is gone — by rewriting the tapped card.

    The tapped message is the stage that has to change, so it is edited in
    place: no extra message is added to the chat. ``edit_message_caption`` is
    used for a photo card because ``edit_message_text`` refuses media; when the
    card cannot be edited at all the notice is posted as one new message and the
    old one is retired, which is the same replace-not-add rule.
    """
    message = getattr(query, "message", None)
    try:
        if getattr(message, "photo", None):
            await query.edit_message_caption(caption=texts.SESSION_EXPIRED)
        else:
            await query.edit_message_text(texts.SESSION_EXPIRED)
        return
    except Exception as exc:  # noqa: BLE001 - classified by the tracker below
        logger.debug("Could not rewrite the expired card: %s", exc)
    if card is not None:
        await card.replace_text(texts.SESSION_EXPIRED)


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
        chat_id = _card_chat_id(query)
        card = None
        if chat_id is not None:
            card = stages.StageTracker(
                context.bot, chat_id, message_id, photo=_is_photo(query)
            )
        await _expired_card(query, context, card)
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
    # message would race for the same output and upload the wrong file.
    if entry.get("job_active"):
        await query.answer(texts.ALREADY_DOWNLOADING, show_alert=False)
        return
    entry["job_active"] = True
    await query.answer()

    fmt, format_id, height = download_flow.resolve_choice(info, action)
    tracker = stages.StageTracker(
        context.bot,
        entry["chat_id"],
        message_id,
        photo=bool(entry.get("photo", True)),
    )
    _spawn(
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
            tracker=tracker,
            entry=entry,
        ),
        entry,
    )


def _spawn(coro, entry: dict) -> None:
    """Schedule a job; a loop that refuses the task must not lock the card."""
    try:
        asyncio.create_task(coro)
    except Exception:  # noqa: BLE001 - the query is already answered
        coro.close()
        entry["job_active"] = False
        logger.exception("Could not schedule the download task")
