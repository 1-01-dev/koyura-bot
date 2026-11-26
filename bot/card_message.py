"""Turn one Telegram message into a card that changes as the job progresses.

The whole point of this module is that the preview, the progress and the file
live in **one** message. A photo message cannot be edited into a text message
(``editMessageText`` refuses media), so every stage stays a media message:

* preview  – photo + caption + format keyboard (already sent by ``handlers``)
* progress – ``editMessageCaption``: the photo stays the cover, the caption
  carries the bar/percent/speed/ETA; ``reply_markup`` removes the buttons so a
  second format cannot be started from the same card
* file     – ``editMessageMedia`` with ``InputMediaVideo``/``Audio``/``Document``
* error    – caption with the reason and the format keyboard back

Every Telegram failure is classified instead of logged blindly:

* ``Message is not modified`` — the caption already says that; ignore silently.
* something that means "this message cannot be edited" — the caller falls back
  to a new message, so the user always gets the progress and the file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaVideo,
)

logger = logging.getLogger(__name__)

# Phrases Telegram uses when an edit is pointless or impossible. Matching on the
# text is ugly but it is the only signal the Bot API gives us (there is no error
# code for "not modified").
_NOT_MODIFIED_HINTS = ("message is not modified",)
# "the message cannot be edited" family: too old, deleted, wrong type for the
# method, media not supported by the message kind.
_UNEDITABLE_HINTS = (
    "message can't be edited",
    "message cannot be edited",
    "message to edit not found",
    "message_id_invalid",
    "there is no text in the message to edit",
    "there is no caption in the message to edit",
    "message does not contain media",
    "message is too old",
    "chat_write_forbidden",
    "not enough rights to edit message",
)


@dataclass
class EditResult:
    """Outcome of one edit attempt."""

    ok: bool
    reason: str | None = None  # "not_modified" | "unavailable" | "error"
    detail: str | None = None

    @property
    def unrecoverable(self) -> bool:
        """True when the card cannot be edited anymore and needs a fallback."""
        return self.reason in ("unavailable",)

    @property
    def ignored(self) -> bool:
        return self.reason == "not_modified"


def _error_text(exc: BaseException) -> str:
    return str(exc).strip().lower()


def classify(exc: BaseException) -> str:
    """Map a Telegram exception to ``not_modified`` / ``unavailable`` / ``error``."""
    text = _error_text(exc)
    if any(hint in text for hint in _NOT_MODIFIED_HINTS):
        return "not_modified"
    if any(hint in text for hint in _UNEDITABLE_HINTS):
        return "unavailable"
    return "error"


def no_keyboard() -> InlineKeyboardMarkup:
    """An empty inline keyboard: the documented way to drop the buttons."""
    return InlineKeyboardMarkup([])


def retry_keyboard(info: dict, msg_id: int, url_hash: str) -> InlineKeyboardMarkup:
    """The format keyboard a failed card shows so the user can pick again.

    ``callback_data`` is identical to the preview's, so a button that was
    already on screen keeps working.
    """
    import download_card

    return download_card.build_format_keyboard(info, msg_id, url_hash)


class CardMessage:
    """Edit one and the same Telegram message through every stage.

    The object remembers whether the message is still a photo (so it uses
    ``editMessageCaption``) and whether editing gave up (so the caller sends new
    messages instead). It never raises: a broken card must not break a download.
    """

    def __init__(
        self,
        bot,
        chat_id: int,
        message_id: int,
        *,
        photo: bool = True,
        on_fallback=None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        # ``on_fallback(text)`` is awaited when the card can no longer be
        # edited; it must post a fresh message with the same content.
        self._on_fallback = on_fallback
        self.is_photo = bool(photo)
        self.broken = False
        self.last_text: str | None = None
        self.edits_sent = 0

    async def _send_fallback(self, text: str) -> bool:
        self.broken = True
        if self._on_fallback is None:
            return False
        try:
            await self._on_fallback(text)
            return True
        except Exception as exc:  # noqa: BLE001 - the user must still get the file
            logger.warning("Progress card fallback message failed: %s", exc)
            return False

    async def _note(self, result: EditResult, action: str) -> EditResult:
        if result.ok:
            self.edits_sent += 1
            return result
        if result.ignored:
            logger.debug("Card %s: %s ignored (already up to date)", self.message_id, action)
            return result
        if result.reason == "unavailable":
            logger.info(
                "Card %s: %s unavailable (%s); falling back to new messages",
                self.message_id,
                action,
                result.detail,
            )
        else:
            logger.warning("Card %s: %s failed: %s", self.message_id, action, result.detail)
        return result

    async def edit_caption(
        self,
        caption: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = "MarkdownV2",
    ) -> EditResult:
        """Update the caption of the media message (progress stages)."""
        if self.broken:
            await self._send_fallback(caption)
            return EditResult(False, "unavailable", "card is broken")
        if not self.is_photo:
            return await self.edit_text(
                caption, reply_markup=reply_markup, parse_mode=parse_mode
            )
        try:
            await self.bot.edit_message_caption(
                chat_id=self.chat_id,
                message_id=self.message_id,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            result = EditResult(False, classify(exc), _error_text(exc))
            await self._note(result, "caption edit")
            if result.unrecoverable:
                await self._send_fallback(caption)
            return result
        self.last_text = caption
        return await self._note(EditResult(True), "caption edit")

    async def edit_text(
        self,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = "MarkdownV2",
    ) -> EditResult:
        """Update a plain text card (the no-thumbnail preview and direct commands)."""
        if self.broken:
            await self._send_fallback(text)
            return EditResult(False, "unavailable", "card is broken")
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            result = EditResult(False, classify(exc), _error_text(exc))
            await self._note(result, "text edit")
            if result.unrecoverable:
                await self._send_fallback(text)
            return result
        self.last_text = text
        return await self._note(EditResult(True), "text edit")

    async def edit_media(
        self,
        media,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> EditResult:
        """Replace the card's media with the finished file.

        ``media`` is one of the ``InputMedia*`` objects, so the caller decides
        whether the result is a video, an audio track or a document and can keep
        the upload file open around this call.
        """
        if self.broken:
            return EditResult(False, "unavailable", "card is broken")
        try:
            await self.bot.edit_message_media(
                chat_id=self.chat_id,
                message_id=self.message_id,
                media=media,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            result = EditResult(False, classify(exc), _error_text(exc))
            await self._note(result, "media edit")
            if result.unrecoverable:
                self.broken = True
            return result
        # The message is a video/audio/document now; captions still apply.
        self.is_photo = False
        return await self._note(EditResult(True), "media edit")

    async def clear_keyboard(self) -> EditResult:
        """Drop the format buttons (the progress stage locks the card)."""
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id,
                message_id=self.message_id,
                reply_markup=no_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            result = EditResult(False, classify(exc), _error_text(exc))
            await self._note(result, "keyboard removal")
            return result
        return await self._note(EditResult(True), "keyboard removal")


def build_input_media(
    media,
    *,
    path=None,
    fmt: str,
    filename: str,
    caption: str,
    duration: int | None = None,
    width: int | None = None,
    height: int | None = None,
    title: str | None = None,
    performer: str | None = None,
    parse_mode: str | None = "MarkdownV2",
) -> InputMediaVideo | InputMediaAudio | InputMediaDocument:
    """Pick the right ``InputMedia`` for the finished file.

    Mirrors what ``send_video``/``send_audio``/``send_document`` used to decide,
    so the replacement looks exactly like a normally sent file. ``media`` is
    what Telegram uploads (a path, an open handle or an ``InputFile``); ``path``
    is only used to pick the type when it differs from what is uploaded.
    """
    reference = path if path is not None else media
    suffix = str(getattr(reference, "suffix", "") or "").lower()
    if fmt == "audio" or suffix == ".mp3":
        return InputMediaAudio(
            media=media,
            filename=filename,
            caption=caption,
            parse_mode=parse_mode,
            duration=duration,
            title=title or None,
            performer=performer or None,
        )
    if suffix == ".mp4":
        return InputMediaVideo(
            media=media,
            filename=filename,
            caption=caption,
            parse_mode=parse_mode,
            duration=duration,
            width=width,
            height=height,
            supports_streaming=True,
        )
    return InputMediaDocument(
        media=media,
        filename=filename,
        caption=caption,
        parse_mode=parse_mode,
    )


__all__ = [
    "CardMessage",
    "EditResult",
    "build_input_media",
    "classify",
    "no_keyboard",
    "retry_keyboard",
    "InlineKeyboardButton",
]
