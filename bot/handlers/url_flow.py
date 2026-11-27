"""From a chat message to a preview card with format buttons.

Three things happen here and nowhere else:

* **intake** — the links in a message (or in the message it replies to) are
  extracted, validated against ``url_safety`` and capped, so the rest of the bot
  only ever sees links that are safe to fetch;
* **the armed mode** — if the user tapped MP3/MP4 on the welcome screen, the next
  link skips the picker and downloads in that format (and the mode is cleared);
* **the preview** — one ``/api/info`` call per link, then the card (photo when
  the engine has a thumbnail, text otherwise) with the per-format keyboard.

Validation touches DNS, so it runs in a worker thread: the bot's event loop must
keep answering other chats while one link is being resolved. The language is
resolved on the loop first, because the thread only formats text.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import download_card
import state_store
from koyura_client import KoyuraError, KoyuraInfoError, KoyuraServiceDown, get_info
from url_safety import UnsafeURLError, validate_url

from . import config, download_flow, language, sessions, stages, texts

logger = logging.getLogger(__name__)

PARSE_MODE = "MarkdownV2"


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

def extract_urls(text: object) -> list[str]:
    """Every link-shaped token in a piece of text (order preserved)."""
    return config.URL_REGEX.findall(str(text or ""))


def command_urls(update: Update) -> list[str]:
    """Links for ``/mp3`` and friends: the command itself, or the reply target.

    Replying is how a user forwards a link from another chat or strips a long
    message down to one link, so it is worth the extra lookup.
    """
    message = update.message
    if message is None:
        return []
    urls = extract_urls(message.text)
    replied = message.reply_to_message
    if not urls and replied is not None:
        urls = extract_urls(replied.text or replied.caption or "")
    return urls


def validated_links(
    urls: list[str], limit: int | None = None, lang: str | None = None
) -> tuple[list[str], str | None]:
    """Validate and cap user-supplied links (blocking; DNS may be involved).

    Returns the safe links plus the one notice the user gets, if any: a link was
    refused, or the message held more links than the bot will process.
    """
    limit = config.MAX_LINKS_PER_MESSAGE if limit is None else limit
    safe: list[str] = []
    rejected = False
    for candidate in urls[:limit]:
        try:
            safe.append(validate_url(candidate))
        except UnsafeURLError:
            rejected = True
    notice = texts.intake_notice(limit, rejected, len(urls) > limit, lang)
    return safe, notice


async def safe_links(
    urls: list[str], lang: str | None = None
) -> tuple[list[str], str | None]:
    """``validated_links`` off the event loop."""
    return await asyncio.to_thread(validated_links, urls, None, lang)


# ---------------------------------------------------------------------------
# The text handler
# ---------------------------------------------------------------------------

async def url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command text: cards for the links it holds, silence otherwise."""
    sessions.evict_stale()
    message = update.message
    if message is None:
        return
    found = extract_urls(message.text)
    if not found:
        return

    user = update.effective_user
    lang = await language.current(getattr(user, "id", None))

    urls, notice = await safe_links(found, lang=lang)
    if notice:
        await message.reply_text(notice, parse_mode=PARSE_MODE)
    if not urls:
        return

    # A mode armed from the welcome keyboard applies to the next link only.
    pending = state_store.get_pending_format(user.id) if user is not None else None
    if pending is not None and user is not None:
        state_store.set_pending_format(user.id, None)
        first, urls = urls[0], urls[1:]
        status = await message.reply_text(
            texts.mode_armed(pending, lang), parse_mode=PARSE_MODE
        )
        await download_flow.run_direct(
            update, context, status, first, pending, None, lang=lang
        )

    for url in urls:
        await send_preview(update, url, lang=lang)


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

async def send_preview(update: Update, url: str, lang: str | None = None) -> None:
    """Fetch metadata and post the preview card with a per-format keyboard."""
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return

    if lang is None:
        lang = await language.current(user.id)

    fingerprint = sessions.url_hash(url)
    # The magnifier is the "searching" stage; it is the first message of the
    # chain and the tracker replaces it from here on.
    status = await message.reply_text(
        texts.md("stage.searching", lang), parse_mode=PARSE_MODE
    )

    tracker = stages.StageTracker(update.get_bot(), chat.id, status.message_id)

    try:
        info = await get_info(url)
    except KoyuraServiceDown:
        await tracker.edit(texts.md("engine.down", lang))
        return
    except KoyuraInfoError as error:
        await tracker.edit(texts.md("engine.info_failed", lang, error=error))
        return
    except KoyuraError as error:
        await tracker.edit(texts.md("engine.error", lang, error=error))
        return

    offer = download_card.format_offer(info)
    if offer.all_videos_hidden or offer.empty:
        # Nothing sendable is left to put on a keyboard: the status line becomes
        # the explanation (with the title, so several links in a row stay
        # distinguishable) instead of a card whose only button would fail.
        await tracker.edit(
            "\n\n".join(
                (
                    download_card.card_headline(info, lang),
                    texts.limit_message(download_card.limit_notice_key(info), lang),
                )
            )
        )
        return

    caption = download_card.build_card(info, lang=lang)

    def keyboard(message_id: int):
        """The format buttons, addressing the message they are attached to."""
        return download_card.build_format_keyboard(
            info, message_id, fingerprint, lang=lang
        )

    def remember(message_id: int, photo: bool) -> None:
        sessions.remember(
            url=url,
            chat_id=chat.id,
            user_id=user.id,
            username=user.username or str(user.id),
            info=info,
            message_id=message_id,
            photo=photo,
        )

    if info.get("thumbnail"):
        # The thumbnail becomes the card, so the searching line is deleted
        # **after** the photo arrives: one message per link, not two. A photo
        # Telegram refuses falls back to the caption in the message that is
        # already on screen, so the same ``ok`` check covers both.
        card = await tracker.replace_photo(
            info["thumbnail"],
            caption,
            fallback_text=caption,
            markup_factory=keyboard,
        )
        if card.ok and tracker.message_id is not None:
            remember(tracker.message_id, photo=tracker.photo)
            return

    # No thumbnail, or the photo could not be posted: the caption and the
    # buttons go into the message that is already on screen.
    result = await tracker.edit(
        caption,
        reply_markup=keyboard(tracker.message_id or status.message_id),
    )
    if result.ok and tracker.message_id is not None:
        remember(tracker.message_id, photo=tracker.photo)
        return

    logger.warning("Failed to render the download card: %s", result.detail)
    await tracker.edit(texts.md("card.render_failed", lang))


__all__ = [
    "command_urls",
    "extract_urls",
    "safe_links",
    "send_preview",
    "url_handler",
    "validated_links",
]
