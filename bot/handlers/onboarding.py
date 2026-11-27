"""The first contact: the language screen, the welcome screen and its buttons.

A new chat walks through exactly one of two paths:

    two or more languages enabled, no saved choice
        "Choose the language"  --tap-->  welcome, in that language
    one language enabled (or a choice already saved)
        welcome, immediately

The welcome screen is the owner's text with an inline keyboard instead of a list
of commands: ``[Help] [Platforms] / [Settings] [Stats] / [MP3] [MP4]``. Tapping a
button answers *in place of* the welcome — the stage rule (one stage, one
message: post the new one, delete the old one) is the same helper the download
flow uses, so the chat never accumulates stale screens.

``MP3``/``MP4`` arm a mode instead of downloading: the next link the user sends
is downloaded in that format and the mode clears itself. If the welcome message
is itself a reply to a link, there is nothing to wait for and the download starts
immediately.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from telegram import Message, Update, User
from telegram.ext import ContextTypes

import state_store

from . import commands, keyboards, language, stages, texts, url_flow

logger = logging.getLogger(__name__)

PARSE_MODE = "MarkdownV2"

# Which engine format a welcome button arms.
MODE_FORMATS = {"mp3": "audio", "mp4": "video"}


def _addressed(update: Update) -> tuple[Message, User] | None:
    """``(message, user)``, or ``None`` for updates that carry neither."""
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return None
    return message, user


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/start``: ask for a language once, then show the welcome."""
    target = _addressed(update)
    if target is None:
        return
    message, user = target

    config = await language.settings()
    if language.needs_picker(config, user.id):
        await message.reply_text(
            texts.language_prompt(config.default_language),
            parse_mode=PARSE_MODE,
            reply_markup=keyboards.language_switch(config, config.default_language),
        )
        return

    await show_welcome(message, config.resolve(language.chosen(user.id)))


async def show_welcome(message: Message, lang: str) -> None:
    """The welcome text plus its keyboard, in one message."""
    await message.reply_text(
        texts.welcome_text(lang),
        parse_mode=PARSE_MODE,
        reply_markup=keyboards.welcome(lang),
    )


# ---------------------------------------------------------------------------
# The language screen
# ---------------------------------------------------------------------------

async def on_language_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A ``lang:`` tap: remember the choice and continue where the tap came from.

    From the first-contact screen the next screen is the welcome (in the language
    just picked); from ``/settings`` it is the settings screen again, so the user
    sees the tick move instead of being thrown back to the start.
    """
    query = update.callback_query
    if query is None:
        return
    parsed = keyboards.parse_language(query.data)
    if parsed is None:
        await query.answer()
        return

    code, from_settings = parsed
    config = await language.settings()
    user = update.effective_user

    if user is None:
        await query.answer()
        return
    if code not in config.languages:
        # A button from a message posted before the operator disabled a language.
        await query.answer(
            texts.plain(
                "language.unknown", config.resolve(language.chosen(user.id))
            ),
            show_alert=True,
        )
        return

    state_store.set_language(user.id, code)
    await query.answer()

    tracker = _tracker(update, context, query)
    if tracker is None:
        return

    if from_settings:
        text, markup = await commands.settings_payload(user)
        await tracker.replace_text(text, reply_markup=markup)
        return
    await tracker.replace_text(texts.welcome_text(code), reply_markup=keyboards.welcome(code))


# ---------------------------------------------------------------------------
# The welcome keyboard
# ---------------------------------------------------------------------------

async def on_welcome_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route ``welcome:help|platforms|settings|stats|mp3|mp4``."""
    query = update.callback_query
    if query is None:
        return
    action = keyboards.parse_welcome(query.data)
    if action is None:
        await query.answer()
        return

    user = update.effective_user
    if user is None:
        await query.answer()
        return

    lang = await language.current(user.id)
    await query.answer()

    tracker = _tracker(update, context, query)
    if tracker is None:
        return

    payloads = {
        "help": commands.help_payload,
        "platforms": commands.platforms_payload,
        "stats": commands.stats_payload,
    }
    if action in payloads:
        await tracker.replace_text(
            await payloads[action](user), reply_markup=keyboards.back(lang)
        )
        return

    if action == "settings":
        text, markup = await commands.settings_payload(user)
        await tracker.replace_text(text, reply_markup=markup)
        return

    fmt = MODE_FORMATS.get(action)
    if fmt is None:
        logger.debug("Unknown welcome action: %s", action)
        return

    await _arm_or_download(update, context, tracker, user, action, fmt, lang)


async def _arm_or_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tracker,
    user: User,
    action: str,
    fmt: str,
    lang: str,
) -> None:
    """MP3/MP4: download the replied-to link now, or arm the mode for the next."""
    replied = getattr(getattr(update.callback_query, "message", None), "reply_to_message", None)
    urls = url_flow.extract_urls(
        getattr(replied, "text", None) or getattr(replied, "caption", None) or ""
    )
    safe, notice = await url_flow.safe_links(urls, lang=lang) if urls else ([], None)
    if notice:
        await tracker.replace_text(notice, reply_markup=keyboards.back(lang))
        return

    if safe:
        # The welcome was a reply to a link: nothing to wait for.
        state_store.set_pending_format(user.id, None)
        result = await tracker.replace_text(
            texts.mode_armed(fmt, lang), reply_markup=keyboards.back(lang)
        )
        if not result.ok or result.message_id is None:
            return
        status = SimpleNamespace(chat_id=tracker.chat_id, message_id=result.message_id)
        await commands.download_flow.run_direct(
            update, context, status, safe[0], fmt, None, lang=lang
        )
        return

    state_store.set_pending_format(user.id, fmt)
    await tracker.replace_text(
        texts.mode_armed(fmt, lang), reply_markup=keyboards.back(lang)
    )


# ---------------------------------------------------------------------------
# Back to the welcome screen
# ---------------------------------------------------------------------------

async def on_nav_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``nav:home``: rewrite the tapped sub-screen into the welcome.

    This is the one place where "back" is an **edit**, not a replacement: the
    message that carries the button becomes the welcome again, so the chat does
    not collect a second copy of the start screen and the user does not have to
    send ``/start``. The armed MP3/MP4 mode is cleared on the way out — Back means
    "forget the pending action and start over" — which is how a user who tapped
    MP3 by mistake changes their mind.
    """
    query = update.callback_query
    if query is None:
        return
    action = keyboards.parse_nav(query.data)
    if action is None:
        await query.answer()
        return

    user = update.effective_user
    if user is None:
        await query.answer()
        return

    lang = await language.current(user.id)
    await query.answer()
    if action != keyboards.HOME_ACTION:
        logger.debug("Unknown navigation action: %s", action)
        return

    state_store.set_pending_format(user.id, None)
    tracker = _tracker(update, context, query)
    if tracker is None:
        return
    result = await tracker.edit(
        texts.welcome_text(lang), reply_markup=keyboards.welcome(lang)
    )
    if not result.ok:
        logger.debug("Could not go back to the welcome: %s", result.detail)


# ---------------------------------------------------------------------------
# The stage the tap replaces
# ---------------------------------------------------------------------------

def _tracker(update: Update, context: ContextTypes.DEFAULT_TYPE, query) -> stages.StageTracker | None:
    """The tracker for the message the tapped button sits on.

    ``query.message`` is optional in the types (an inaccessible message has no
    chat we can act on); when it is missing there is nothing to replace, and a
    stage tracker that would post into the void is not created.
    """
    message = getattr(query, "message", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is None:
        chat = update.effective_chat
        chat_id = getattr(chat, "id", None)
    if chat_id is None:
        logger.debug("Cannot replace a screen: the tapped message has no chat")
        return None
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        logger.debug("Cannot replace a screen: chat id %r is not a number", chat_id)
        return None
    return stages.StageTracker(
        context.bot,
        chat_id,
        getattr(message, "message_id", None),
        photo=bool(getattr(message, "photo", None)),
    )


__all__ = [
    "MODE_FORMATS",
    "on_language_button",
    "on_nav_button",
    "on_welcome_button",
    "show_welcome",
    "start_command",
]
