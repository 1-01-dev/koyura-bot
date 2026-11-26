"""One stage — one message: post the next stage, then drop the one it replaces.

The chat is a chain, not a log. Every stage of a download *replaces* the one
before it, so the user always sees exactly one live status:

    "Fetching info..." -> preview card -> progress -> file (or error)

and the file/error is the only thing left behind. ``StageTracker`` holds the
chat id and the id of the transient message and offers the four operations the
flows need:

* ``replace_text`` / ``replace_photo`` — move to the next stage;
* ``edit`` — a progress tick (the same message, never a new one);
* ``finish_with_text`` / ``finish_with_file`` — the result, which stays.

Two rules make the chain safe, and both are enforced here rather than in the
callers:

* **send first, delete second.** If the new message cannot be posted the old
  one is kept and the new text is written into it, so the user never ends up
  with no card at all.
* **deleting is best effort.** A message older than 48 hours, already deleted by
  the user, or in a chat where the bot may not delete is not an error: it is a
  DEBUG line and the flow carries on.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PARSE_MODE = "MarkdownV2"

# Telegram's answer when an edit changed nothing; not a failure.
_NOT_MODIFIED_HINTS = ("message is not modified",)

# "this message is not there (any more)" — deleted by the user, too old, or not
# editable/deletable at all. The Bot API has no error code for this, so the
# message text is the only signal.
_GONE_HINTS = (
    "message to delete not found",
    "message can't be deleted",
    "message cannot be deleted",
    "message to edit not found",
    "message can't be edited",
    "message cannot be edited",
    "message_id_invalid",
    "there is no text in the message to edit",
    "there is no caption in the message to edit",
    "message does not contain media",
    "message is too old",
    "chat_write_forbidden",
    "not enough rights",
)

GONE = "gone"
NOT_MODIFIED = "not_modified"
FAILED = "failed"


def classify(exc: BaseException) -> str:
    """Map a Telegram exception to ``not_modified`` / ``gone`` / ``failed``."""
    detail = str(exc).strip().lower()
    if any(hint in detail for hint in _NOT_MODIFIED_HINTS):
        return NOT_MODIFIED
    if any(hint in detail for hint in _GONE_HINTS):
        return GONE
    return FAILED


def _detail(exc: BaseException) -> str:
    return str(exc).strip() or exc.__class__.__name__


@dataclass
class StageResult:
    """Outcome of one stage operation."""

    ok: bool
    message_id: int | None = None
    detail: str | None = None


async def delete_quietly(bot, chat_id: int, message_id: int | None) -> bool:
    """Delete one message and never raise: an unclearable chat is not an error.

    A message older than 48 hours, one the user already removed, or one the bot
    has no right to delete all land here and are logged at DEBUG without a
    traceback, because none of them is a bot failure.
    """
    if not message_id:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as exc:  # noqa: BLE001 - deletion is always best effort
        logger.debug(
            "Message %s in chat %s was not deleted (%s): %s",
            message_id,
            chat_id,
            classify(exc),
            _detail(exc),
        )
        return False


class StageTracker:
    """The single transient message of a flow, and the moves between stages.

    ``message_id`` is the message the user currently sees as "the status"; it
    changes with every replacement. ``finished`` is set by the ``finish_*``
    operations: after a result arrives nothing is tracked any more, so a late
    stage cannot delete the file the user just received.
    """

    def __init__(
        self,
        bot,
        chat_id: int,
        message_id: int | None = None,
        *,
        photo: bool = False,
        reply_to=None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        # Whether the current message is a media message (caption edits) or a
        # text one (text edits). A new text stage always resets it.
        self.photo = photo
        # Replies keep the "this is about your link" look for the first stages.
        self.reply_to = reply_to
        self.finished = False

    # -- sending -----------------------------------------------------------

    async def _post_text(self, text: str, *, reply_markup=None):
        """Post a text message; ``None`` when Telegram refused it."""
        try:
            if self.reply_to is not None:
                return await self.reply_to.reply_text(
                    text=text, parse_mode=PARSE_MODE, reply_markup=reply_markup
                )
            return await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=PARSE_MODE,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001 - the caller keeps the old message
            logger.warning("Could not post a new stage message: %s", _detail(exc))
            return None

    async def _post_photo(self, photo, caption: str, *, reply_markup=None):
        """Post a photo card; ``None`` when Telegram refused it."""
        try:
            if self.reply_to is not None:
                return await self.reply_to.reply_photo(
                    photo=photo,
                    caption=caption,
                    parse_mode=PARSE_MODE,
                    reply_markup=reply_markup,
                )
            return await self.bot.send_photo(
                chat_id=self.chat_id,
                photo=photo,
                caption=caption,
                parse_mode=PARSE_MODE,
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001 - the caller falls back to text
            logger.info("Could not post a photo stage message: %s", _detail(exc))
            return None

    # -- the pointer -------------------------------------------------------

    async def _retire(self) -> int | None:
        """Delete the message the tracker points at and forget it."""
        previous, self.message_id = self.message_id, None
        if previous is not None:
            await delete_quietly(self.bot, self.chat_id, previous)
        return previous

    # -- moving between stages --------------------------------------------

    async def replace_text(
        self,
        text: str,
        *,
        reply_markup=None,
        markup_factory: Callable[[int], object] | None = None,
    ) -> StageResult:
        """Post the next stage, then delete the message it replaces.

        ``reply_markup`` goes out with the new message; ``markup_factory(message_id)``
        is for keyboards that have to carry the id of the *new* message (a
        callback button addresses its own message), so it is called once the post
        has an id.
        """
        sent = await self._post_text(text, reply_markup=reply_markup)
        if sent is None:
            # Posting failed: keep what the user has and put the stage into it.
            markup = reply_markup
            if markup_factory is not None and self.message_id is not None:
                markup = _build_markup(markup_factory, self.message_id)
            return await self.edit(text, reply_markup=markup)
        message_id = getattr(sent, "message_id", None)
        if markup_factory is not None and message_id is not None:
            await self._attach_markup(message_id, markup_factory)
        await self._retire()
        self.message_id = message_id
        self.photo = False
        return StageResult(True, message_id)

    async def replace_photo(
        self,
        photo,
        caption: str,
        *,
        reply_markup=None,
        markup_factory: Callable[[int], object] | None = None,
        fallback_text: str | None = None,
    ) -> StageResult:
        """Post a photo card, then delete the plain status it replaces.

        A photo that Telegram will not take is not a reason to lose the card:
        the caption is written into the message already on screen instead.
        """
        sent = await self._post_photo(photo, caption, reply_markup=reply_markup)
        if sent is None:
            text = fallback_text or caption
            markup = reply_markup
            if markup_factory is not None and self.message_id is not None:
                markup = _build_markup(markup_factory, self.message_id)
            return await self.edit(text, reply_markup=markup)
        message_id = getattr(sent, "message_id", None)
        if markup_factory is not None and message_id is not None:
            await self._attach_markup(message_id, markup_factory)
        await self._retire()
        self.message_id = message_id
        self.photo = True
        return StageResult(True, message_id)

    async def edit(self, text: str, *, reply_markup=None) -> StageResult:
        """Rewrite the current message (progress ticks, captions, errors)."""
        if self.message_id is None:
            return StageResult(False, None, "no message to edit")
        try:
            if self.photo:
                await self.bot.edit_message_caption(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    caption=text,
                    parse_mode=PARSE_MODE,
                    reply_markup=reply_markup,
                )
            else:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode=PARSE_MODE,
                    reply_markup=reply_markup,
                )
        except Exception as exc:  # noqa: BLE001 - classified below
            reason = classify(exc)
            if reason == NOT_MODIFIED:
                return StageResult(True, self.message_id)
            logger.debug(
                "Stage edit on message %s failed (%s): %s",
                self.message_id,
                reason,
                _detail(exc),
            )
            if reason == GONE:
                # The message is gone: post the stage again so the user still
                # sees where the download is, and forget the dead one.
                return await self.replace_text(text, reply_markup=reply_markup)
            logger.warning("Stage edit failed: %s", _detail(exc))
            return StageResult(False, self.message_id, _detail(exc))
        return StageResult(True, self.message_id)

    # -- results -----------------------------------------------------------

    async def finish_with_text(
        self,
        text: str,
        *,
        markup_factory: Callable[[int], object] | None = None,
    ) -> StageResult:
        """Post a result message (usually the error card) and delete the status.

        ``markup_factory`` builds the keyboard once the new message has an id, so
        the buttons address the message they are attached to.
        """
        sent = await self._post_text(text)
        if sent is None:
            # Nothing new can be posted: the result has to land in what we have.
            markup = None
            if markup_factory is not None and self.message_id is not None:
                markup = _build_markup(markup_factory, self.message_id)
            result = await self.edit(text, reply_markup=markup)
            self.finished = True
            return result

        message_id = getattr(sent, "message_id", None)
        if markup_factory is not None and message_id is not None:
            await self._attach_markup(message_id, markup_factory)
        await self._retire()
        self.message_id = message_id
        self.photo = False
        self.finished = True
        return StageResult(True, message_id)

    async def finish_with_file(
        self, send: Callable[[], Awaitable[object]]
    ) -> StageResult:
        """Send the file, then delete the progress message it replaces.

        ``send`` performs the upload and returns the posted message (or a falsy
        value when the upload failed). On failure the progress message is kept,
        so the caller can still turn it into the error card.
        """
        try:
            sent = await send()
        except Exception:  # noqa: BLE001 - the caller reports the failure
            logger.exception("Posting the finished file failed")
            sent = None
        if not sent:
            return StageResult(False, self.message_id, "file was not posted")
        await self._retire()
        self.finished = True
        return StageResult(True, getattr(sent, "message_id", None))

    # -- helpers -----------------------------------------------------------

    async def _attach_markup(self, message_id: int, markup_factory) -> None:
        markup = _build_markup(markup_factory, message_id)
        if markup is None:
            return
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=message_id, reply_markup=markup
            )
        except Exception as exc:  # noqa: BLE001 - the text matters more
            logger.debug(
                "Could not attach the keyboard to message %s: %s",
                message_id,
                _detail(exc),
            )


def _build_markup(markup_factory, message_id: int):
    try:
        return markup_factory(message_id)
    except Exception:  # noqa: BLE001 - a keyboard must never swallow the text
        logger.debug("Could not build the retry keyboard", exc_info=True)
        return None


__all__ = [
    "FAILED",
    "GONE",
    "NOT_MODIFIED",
    "PARSE_MODE",
    "StageResult",
    "StageTracker",
    "classify",
    "delete_quietly",
]
