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
import transcode_policy as policy

from . import download_flow, language, sessions, stages, texts

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


async def _answer_conversion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    entry: dict,
    message_id: int,
    url_hash: str,
    answer: str,
    action: str,
    lang: str | None,
) -> None:
    """Handle ``Send as file`` / ``Convert (~4 min)`` on a codec question.

    The download has not started when the question is asked, so both answers
    start the same job with a different ``transcode`` request and the engine
    never converts without this tap:

    * ``file`` → ``transcode=never``: the flow delivers a document;
    * ``conv`` → ``transcode=force``: the engine converts and the card shows the
      ``Converting…`` stage with ffmpeg's percentage.

    The keyboard is removed from the question card first: it must not survive
    the answer and start a second job. A refusal from the engine (``user_busy``
    and friends) is turned into the usual error card by ``run_download``.
    """
    query = update.callback_query
    if query is None:
        return

    info = entry["info"]
    choice = download_flow.plan_choice(info, action)
    if choice.refusal:
        await query.answer(
            texts.t(choice.refusal, lang, limit=texts.send_limit_label(lang)),
            show_alert=True,
        )
        return
    if entry.get("job_active"):
        await query.answer(texts.t("download.already", lang), show_alert=False)
        return

    await query.answer()
    entry["job_active"] = True
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001 - the job matters more than the keyboard
        logger.debug("Could not retire the conversion keyboard: %s", exc)

    delivery = policy.CONVERT if answer == "conv" else policy.SEND_FILE
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
            fmt=choice.fmt,
            format_id=choice.format_id,
            height=choice.height,
            user_id=entry["user_id"],
            username=entry.get("username", ""),
            started_at=entry["created"],
            url_hash=url_hash,
            tracker=tracker,
            entry=entry,
            lang=lang,
            skipped=choice.skipped,
            delivery=delivery,
        ),
        entry,
    )


async def _expired_card(query, context, card, lang: object = None) -> None:
    """Tell the user the session is gone — by rewriting the tapped card.

    The tapped message is the stage that has to change, so it is edited in
    place: no extra message is added to the chat. ``edit_message_caption`` is
    used for a photo card because ``edit_message_text`` refuses media; when the
    card cannot be edited at all the notice is posted as one new message and the
    old one is retired, which is the same replace-not-add rule.
    """
    notice = texts.t("download.session_expired", lang)
    message = getattr(query, "message", None)
    try:
        if getattr(message, "photo", None):
            await query.edit_message_caption(caption=notice)
        else:
            await query.edit_message_text(notice)
        return
    except Exception as exc:  # noqa: BLE001 - classified by the tracker below
        logger.debug("Could not rewrite the expired card: %s", exc)
    if card is not None:
        await card.replace_text(texts.md("download.session_expired", lang))


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
    # The conversion question has one extra field, so the lenient parser above
    # reads its answer as the hash. It is recognised by its own shape and
    # resolved here, *before* the session lookup that would fail on it.
    conversion = sessions.parse_conversion_data(query.data or "")
    if conversion is not None:
        message_id, url_hash = conversion[0], conversion[1]
        action = conversion[3]

    # ``query.message`` is Optional and may be an inaccessible message, so the
    # card is addressed by the ids recorded when it was posted.
    entry = sessions.find(query, message_id, url_hash)
    user = update.effective_user
    lang = await language.current(
        entry["user_id"] if entry is not None else getattr(user, "id", None)
    )
    if entry is None:
        await query.answer()
        chat_id = _card_chat_id(query)
        card = None
        if chat_id is not None:
            card = stages.StageTracker(
                context.bot, chat_id, message_id, photo=_is_photo(query)
            )
        await _expired_card(query, context, card, lang)
        return

    if conversion is not None:
        # ``answer``/``target`` come from the same tuple the branch is keyed on,
        # so the destructuring above is what defines them.
        _message_id, _url_hash, answer_text, target = conversion
        await _answer_conversion(
            update, context, entry, message_id, url_hash, answer_text, target, lang
        )
        return

    info = entry["info"]
    if action in PICKER_ACTIONS:
        # Legacy MP4 button: show the per-format keyboard instead of guessing.
        markup = download_card.build_format_keyboard(
            info, message_id, url_hash, lang=lang
        )
        if markup is None:
            # Nothing fits the send limit: the tap gets the reason, not a card
            # with a "best quality" button that cannot be delivered.
            await query.answer(
                texts.t(download_card.limit_notice_key(info), lang,
                        limit=texts.send_limit_label(lang)),
                show_alert=True,
            )
            return
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=markup)
        return

    action = ACTION_ALIASES.get(action, action)

    # The format that will actually be sent, decided before the answer so a
    # "nothing fits" refusal can still use the only ``answer()`` this query gets.
    choice = download_flow.plan_choice(info, action)
    if choice.refusal:
        await query.answer(
            texts.t(choice.refusal, lang, limit=texts.send_limit_label(lang)),
            show_alert=True,
        )
        return

    # The card locks itself while a job runs: a second format on the same
    # message would race for the same output and upload the wrong file.
    if entry.get("job_active"):
        await query.answer(texts.t("download.already", lang), show_alert=False)
        return

    if choice.ask:
        # A codec Telegram may not play and no standing permission to convert:
        # ask before spending a core for minutes. No job has started yet.
        await query.answer()
        tracker = stages.StageTracker(
            context.bot,
            entry["chat_id"],
            message_id,
            photo=bool(entry.get("photo", True)),
        )
        await download_flow.ask_transcode(
            tracker, info, choice, url_hash=url_hash, lang=lang
        )
        _rebind(entry, tracker, url_hash)
        return

    entry["job_active"] = True
    await query.answer()

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
            fmt=choice.fmt,
            format_id=choice.format_id,
            height=choice.height,
            user_id=entry["user_id"],
            username=entry.get("username", ""),
            started_at=entry["created"],
            url_hash=url_hash,
            tracker=tracker,
            entry=entry,
            lang=lang,
            skipped=choice.skipped,
            delivery=choice.delivery,
        ),
        entry,
    )


def _rebind(entry: dict, tracker, url_hash: str) -> None:
    """Point the session at the message that carries the new buttons.

    Asking a question posts a new card, so the session must follow it — the
    same rule the format keyboard follows after a failure, otherwise a tap on
    the fresh question would answer "session expired".
    """
    if tracker.message_id is None:
        return
    sessions.rebind(entry["chat_id"], entry["message_id"], url_hash, tracker.message_id)
    entry["message_id"] = tracker.message_id


def _spawn(coro, entry: dict) -> None:
    """Schedule a job; a loop that refuses the task must not lock the card."""
    try:
        asyncio.create_task(coro)
    except Exception:  # noqa: BLE001 - the query is already answered
        coro.close()
        entry["job_active"] = False
        logger.exception("Could not schedule the download task")
