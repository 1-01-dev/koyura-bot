"""From a chat message to a preview card with format buttons.

Two things happen here and nowhere else:

* **intake** — the links in a message (or in the message it replies to) are
  extracted, validated against ``url_safety`` and capped, so the rest of the bot
  only ever sees links that are safe to fetch;
* **the preview** — one ``/api/info`` call per link, then the card (photo when
  the engine has a thumbnail, text otherwise) with the per-format keyboard.

Validation touches DNS, so it runs in a worker thread: the bot's event loop must
keep answering other chats while one link is being resolved.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import download_card
from koyura_client import KoyuraError, KoyuraInfoError, KoyuraServiceDown, get_info
from url_safety import UnsafeURLError, validate_url

from . import config, sessions, stages, texts

logger = logging.getLogger(__name__)


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


def validated_links(urls: list[str], limit: int | None = None) -> tuple[list[str], str | None]:
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
    notice = texts.intake_notice(limit, rejected, len(urls) > limit)
    return safe, notice


async def safe_links(urls: list[str]) -> tuple[list[str], str | None]:
    """``validated_links`` off the event loop."""
    return await asyncio.to_thread(validated_links, urls)


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

    urls, notice = await safe_links(found)
    if notice:
        await message.reply_text(notice)
    if not urls:
        return
    for url in urls:
        await send_preview(update, url)


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

async def send_preview(update: Update, url: str) -> None:
    """Fetch metadata and post the preview card with a per-format keyboard."""
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return

    fingerprint = sessions.url_hash(url)
    status = await message.reply_text(texts.FETCHING_INFO)
    # ``status`` is the first stage of the chain; the tracker owns it from here
    # on, so a failure replaces it instead of piling one more message on top.
    tracker = stages.StageTracker(update.get_bot(), chat.id, status.message_id)

    try:
        info = await get_info(url)
    except KoyuraServiceDown:
        await tracker.edit(texts.SERVICE_DOWN)
        return
    except KoyuraInfoError as error:
        await tracker.edit(texts.info_failed(error))
        return
    except KoyuraError as error:
        await tracker.edit(texts.engine_error(error))
        return

    caption = download_card.build_card(info)

    def keyboard(message_id: int):
        """The format buttons, addressing the message they are attached to."""
        return download_card.build_format_keyboard(info, message_id, fingerprint)

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
        # The thumbnail becomes the card, so the "Fetching info..." line is
        # deleted **after** the photo arrives: one message per link, not two.
        # A photo Telegram refuses falls back to the caption in the message that
        # is already on screen, so the same ``ok`` check covers both.
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
    await tracker.edit(texts.CARD_RENDER_FAILED)
