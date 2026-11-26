"""One download, from the button to the file in the chat.

The engine does the work; this module is the choreography around it:

    button (/mp3) -> start_download -> status poll -> upload -> done event

and every stage rewrites **the same Telegram message** (see ``card_message``):
the preview loses its buttons and becomes the progress card, and the progress
card becomes the file. When a message can no longer be edited the card falls
back to posting fresh messages, so a download is never lost to a broken chat
message.

Two entry points:

* ``run_download`` — a card exists and a format was chosen (the button flow);
* ``run_direct`` — ``/mp3``, ``/mp4``, ``/best``: fetch info, then the same
  stages with a plain text card and no picker.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

import card_message
import download_card
import event_client
import progress_card
from koyura_client import (
    KoyuraBusy,
    KoyuraError,
    KoyuraServiceDown,
    get_info,
    poll_status,
    start_download,
)

from . import config, stats, texts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# What the user picked
# ---------------------------------------------------------------------------

def audio_track(info: dict) -> dict:
    """The engine's audio summary, or an empty dict when it is missing/odd."""
    audio = info.get("audio")
    return audio if isinstance(audio, dict) else {}


def video_formats(info: dict) -> list[dict]:
    return download_card.video_formats(info)


def find_format(info: dict, format_id: str | None) -> dict | None:
    for fmt in video_formats(info):
        if str(fmt.get("id")) == str(format_id):
            return fmt
    return None


def resolve_choice(info: dict, action: str) -> tuple[str, str | None, int | None]:
    """Map a button action to ``(engine format, format_id, height)``.

    ``mp3`` is the only audio choice; everything else is a video, and ``best``
    means "no id, engine decides". An id that is no longer in ``info`` is still
    forwarded: the engine validates it, so a button from an expired session
    keeps working instead of silently downloading something else.
    """
    if action == "mp3":
        return "audio", None, None
    if action == "best":
        return "video", None, None
    found = find_format(info, action)
    height = found.get("height") if found else None
    return "video", action, int(height) if height else None


def quality_label(fmt: str, format_id: str | None, height: int | None, info: dict) -> str:
    """The quality tag for the dashboard event and the caption under the file."""
    if fmt == "audio":
        bitrate = audio_track(info).get("abr")
        return f"MP3 {bitrate}kbps" if bitrate else "MP3"
    if height:
        return f"{height}p"
    if format_id:
        found = find_format(info, format_id)
        return str(found.get("label") or format_id) if found else str(format_id)
    return "best"


def size_hint(info: dict, fmt: str, format_id: str | None, height: int | None) -> int | None:
    """Best known size of the file the user is about to get, in bytes.

    Only reported numbers are used: the selected format's ``filesize`` (plus the
    audio track for a video, since the engine merges both), or the MP3
    ``filesize_approx``. The engine turns this into a queue wait estimate, so a
    missing number is better than a made-up one.
    """
    audio = audio_track(info)
    if fmt == "audio":
        for field_name in ("filesize", "filesize_approx"):
            value = progress_card.as_number(audio.get(field_name))
            if value and value > 0:
                return int(value)
        return None

    formats = video_formats(info)
    chosen = None
    for candidate in formats:
        if format_id and str(candidate.get("id")) == str(format_id):
            chosen = candidate
            break
        if height and candidate.get("height") == height:
            chosen = candidate
    if chosen is None and formats:
        chosen = formats[0]
    if not chosen:
        return None

    size = progress_card.as_number(chosen.get("filesize"))
    if not size or size <= 0:
        size = progress_card.as_number(chosen.get("filesize_approx"))
    if not size or size <= 0:
        return None
    total = float(size)
    audio_size = progress_card.as_number(audio.get("filesize"))
    if audio_size and audio_size > 0:
        total += audio_size
    return int(total)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

@dataclass
class Job:
    """One download, so the stages pass a single object instead of 13 kwargs."""

    url: str
    info: dict
    fmt: str
    format_id: str | None
    height: int | None
    user_id: int
    username: str
    chat_id: int
    status_message_id: int
    started_at: float
    url_hash: str | None = None

    @property
    def title(self) -> str:
        return str(self.info.get("title") or "").strip() or "download"

    @property
    def quality(self) -> str:
        return quality_label(self.fmt, self.format_id, self.height, self.info)


def fallback_sender(bot, chat_id: int):
    """Build the hook a card calls when it can no longer be edited."""

    async def send(text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="MarkdownV2")

    return send


async def run_download(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_message_id: int,
    url: str,
    info: dict,
    fmt: str,
    format_id: str | None,
    height: int | None,
    user_id: int,
    username: str,
    started_at: float,
    direct: bool = False,
    url_hash: str | None = None,
    card=None,
    entry: dict | None = None,
) -> None:
    """Start a job, keep the card updated and finish it with the file itself.

    ``entry`` is the picker session; it is unlocked in a ``finally`` so a failed
    download gives the format buttons back instead of leaving the card dead.
    """
    job = Job(
        url=url,
        info=info,
        fmt=fmt,
        format_id=format_id,
        height=height,
        user_id=user_id,
        username=username,
        chat_id=chat_id,
        status_message_id=status_message_id,
        started_at=started_at,
        url_hash=url_hash,
    )
    if card is None:
        card = card_message.CardMessage(
            context.bot,
            chat_id,
            status_message_id,
            photo=bool((entry or {}).get("photo", True)),
            on_fallback=fallback_sender(context.bot, chat_id),
        )

    try:
        await _advance(job, context, card, direct=direct)
    finally:
        if entry is not None:
            entry["job_active"] = False


async def _advance(job: Job, context: ContextTypes.DEFAULT_TYPE, card, *, direct: bool) -> None:
    """The stages of one download, each rewriting the same card."""

    async def fail(message: str, *, retry_hint: bool = True) -> None:
        """Show a failure and give the format buttons back."""
        await report_failure(
            card,
            job.info,
            message,
            job.status_message_id,
            job.url_hash,
            retry_hint=retry_hint,
        )
        stats.record_error()

    # Stage 1 -> 2: the preview becomes the progress card. The buttons go away
    # so the same message cannot start a second job.
    await card.edit_caption(
        progress_card.build_status_caption(job.info, texts.STARTING),
        reply_markup=None,
    )
    await card.clear_keyboard()

    try:
        accepted = await start_download(
            job.url,
            job.fmt,
            job.format_id,
            job.title,
            uploader=str(job.info.get("channel") or job.info.get("uploader") or ""),
            height=job.height,
            user_id=job.user_id,
            size_bytes=size_hint(job.info, job.fmt, job.format_id, job.height),
        )
    except KoyuraBusy as refusal:
        # A refusal has its own wording: the wait is known and the same buttons
        # are still on screen, so "tap a format to try again" is noise.
        await fail(progress_card.busy_message(refusal), retry_hint=False)
        return
    except KoyuraServiceDown:
        await fail(texts.SERVICE_DOWN)
        return
    except KoyuraError as error:
        await fail(str(error))
        return

    job_id = str(accepted.get("job_id") or "")
    if not job_id:
        await fail(texts.UNEXPECTED_ANSWER)
        return

    # A queued job keeps the same card and shows its place; the poll loop below
    # turns the wait into the usual progress as soon as a slot frees up.
    if accepted.get("status") == "queued":
        await card.edit_caption(
            progress_card.build_queued_caption(
                job.info,
                accepted.get("queue_position"),
                accepted.get("wait_seconds"),
                approximate=bool(accepted.get("queue_estimate_approximate", True)),
            ),
            reply_markup=None,
        )

    await _announce_start(job, job_id)

    status = await poll_until_finished(job, job_id, card)
    if status is None:
        stats.record_error()
        return

    if status.get("status") == "error":
        await fail(str(status.get("error") or texts.UNKNOWN_ERROR))
        await event_client.send_download_error(
            job_id=job_id, error_message=status.get("error", texts.EVENT_UNKNOWN_ERROR)
        )
        return

    await _deliver(job, job_id, card, context.bot, status, direct=direct)


async def _announce_start(job: Job, job_id: str) -> None:
    """Tell the dashboard a job began; a dashboard hiccup is not an error."""
    with contextlib.suppress(Exception):
        await event_client.send_download_start(
            job_id=job_id,
            user_id=job.user_id,
            username=job.username,
            chat_id=job.chat_id,
            url=job.url,
            platform=job.info.get("extractor", "unknown"),
            format=job.fmt,
            quality=job.quality,
            title=job.title,
        )


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def poll_until_finished(job: Job, job_id: str, card) -> dict | None:
    """Poll the engine every couple of seconds, rewriting the card as it goes.

    Caption edits are throttled (``progress_card.ProgressThrottle``) so a fast
    job does not hit Telegram's edit limit, while the dashboard still receives
    one ``progress`` event per poll.

    Returns the terminal status dict, or ``None`` when polling gave up (service
    lost or timed out) — the caller counts that as a failure.
    """
    throttle = progress_card.ProgressThrottle(
        min_interval=config.PROGRESS_EDIT_INTERVAL,
        min_percent_delta=config.PROGRESS_MIN_PERCENT_DELTA,
    )

    for _ in range(config.POLL_ATTEMPTS):
        await asyncio.sleep(config.POLL_INTERVAL)
        try:
            status = await poll_status(job_id)
        except KoyuraServiceDown:
            await report_failure(
                card, job.info, texts.SERVICE_DOWN, job.status_message_id, None
            )
            await event_client.send_download_error(
                job_id=job_id, error_message=texts.EVENT_SERVICE_DOWN
            )
            return None
        except KoyuraError:
            # One unreadable status is not a reason to fail a download that is
            # very likely still running; the next poll asks again.
            continue

        state = status.get("status")
        if state in ("done", "error"):
            return status

        if state == "queued":
            await _show_queue_state(card, job.info, status, throttle)
            continue

        progress = status.get("progress")
        if not isinstance(progress, dict):
            progress = {}
        percent = progress_card.derive_percent(progress)
        eta = progress_card.derive_eta(progress)

        # The dashboard gets every poll, throttled or not: its timeline is
        # the record of the job, the caption is only a comfort.
        with contextlib.suppress(Exception):
            await event_client.send_progress(
                job_id=job_id,
                percent=progress.get("percent"),
                speed=progress.get("speed"),
                eta=progress.get("eta"),
                downloaded_bytes=progress.get("downloaded_bytes"),
                total_bytes=progress.get("total_bytes"),
            )

        if not throttle.should_update(percent, eta):
            continue

        elapsed = progress_card.as_number(progress.get("elapsed"))
        if elapsed is None:
            elapsed = time.time() - job.started_at
        await card.edit_caption(
            progress_card.build_progress_caption(
                job.info,
                progress,
                stage="downloading",
                quality=job.quality,
                elapsed=elapsed,
            ),
            reply_markup=None,
        )

    await report_failure(card, job.info, texts.TIMED_OUT, job.status_message_id, None)
    await event_client.send_download_error(
        job_id=job_id, error_message=texts.EVENT_TIMED_OUT
    )
    return None


async def _show_queue_state(card, info: dict, status: dict, throttle) -> None:
    """Rewrite the card with the job's place in the queue.

    The same throttle as the progress caption applies, so a job moving up the
    queue cannot spam Telegram with edits.
    """
    position = progress_card.as_number(status.get("queue_position"))
    wait = progress_card.as_number(status.get("wait_seconds"))
    if not throttle.should_update(position, wait):
        return
    await card.edit_caption(
        progress_card.build_queued_caption(
            info,
            position,
            wait,
            approximate=bool(status.get("queue_estimate_approximate", True)),
        ),
        reply_markup=None,
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def local_file(status: dict) -> Path | None:
    """Resolve the downloaded file through the shared downloads volume."""
    raw = status.get("file_path") or status.get("filename")
    if not raw:
        return None
    for candidate in (Path(config.DOWNLOADS_PATH) / Path(raw).name, Path(raw)):
        if candidate.exists():
            return candidate
    return None


def file_name(job: Job, status: dict, local: Path) -> str:
    """The name the user sees: the engine's, else one built from the title."""
    height = status.get("height") or job.height
    extension = local.suffix or (".mp3" if job.fmt == "audio" else ".mp4")
    engine_name = status.get("filename")
    if engine_name and download_card.sanitize_filename(Path(engine_name).stem):
        # The engine already names the file after the title and quality; keep
        # its name but normalise the extension we are about to upload.
        return f"{download_card.sanitize_filename(Path(engine_name).stem)}{extension}"
    return download_card.build_filename(job.title, extension, height)


async def _deliver(
    job: Job, job_id: str, card, bot, status: dict, *, direct: bool
) -> None:
    """Put the finished file in the chat, in the card when possible."""
    local = local_file(status)
    if local is None:
        await report_failure(
            card, job.info, texts.FILE_MISSING, job.status_message_id, job.url_hash
        )
        stats.record_error()
        await event_client.send_download_error(
            job_id=job_id, error_message=texts.EVENT_FILE_MISSING
        )
        return

    height = status.get("height") or job.height
    filename = file_name(job, status, local)
    size_bytes = status.get("file_size_bytes")
    if not size_bytes:
        try:
            size_bytes = local.stat().st_size
        except OSError:
            size_bytes = None
    caption = download_card.build_file_caption(
        job.info,
        quality=job.quality,
        size_bytes=size_bytes,
        duration=status.get("duration"),
    )

    # Stage 3: the card itself becomes the file. When that is impossible the
    # file is sent as a new message, so the download is never lost.
    in_place = await _replace_media(
        card,
        local,
        filename=filename,
        fmt=job.fmt,
        info=job.info,
        caption=caption,
        status=status,
        height=height,
    )
    if not in_place:
        uploaded = await _send_file(
            bot,
            job.chat_id,
            local,
            filename=filename,
            fmt=job.fmt,
            info=job.info,
            caption=caption,
            status=status,
            height=height,
        )
        if not uploaded:
            await report_failure(
                card, job.info, texts.UPLOAD_FAILED, job.status_message_id, job.url_hash
            )
            stats.record_error()
            await event_client.send_download_error(
                job_id=job_id, error_message=texts.EVENT_UPLOAD_FAILED
            )
            unlink_quietly(local)
            return
        # The card is still the preview/progress message: retire it so the chat
        # keeps showing the file and not a stale percentage.
        await card.edit_caption(
            download_card.build_file_caption(
                job.info,
                quality=job.quality,
                size_bytes=size_bytes,
                duration=status.get("duration"),
            ),
        )

    stats.record_download()
    with contextlib.suppress(Exception):
        await event_client.send_download_done(
            job_id=job_id,
            file_size_bytes=size_bytes or 0,
            duration_seconds=time.time() - job.started_at,
            filename=filename,
        )

    if not in_place and not direct:
        # Nothing to rewrite: the file already arrived as its own message.
        logger.debug("Delivered %s as a new message (card could not be edited)", filename)
    unlink_quietly(local)


async def _replace_media(
    card,
    local: Path,
    *,
    filename: str,
    fmt: str,
    info: dict,
    caption: str,
    status: dict,
    height: int | None,
) -> bool:
    """Turn the card into the finished file with ``editMessageMedia``.

    Returns False when the edit did not happen, in which case the caller sends
    the file as a separate message.
    """
    if getattr(card, "broken", False):
        return False
    duration = status.get("duration")
    try:
        with open(local, "rb") as handle:
            # The handle goes in as ``media`` at construction time: Telegram
            # objects are frozen, so it cannot be assigned afterwards.
            media = card_message.build_input_media(
                handle,
                path=local,
                fmt=fmt,
                filename=filename,
                caption=caption,
                duration=int(duration) if duration else None,
                width=status.get("width"),
                height=height,
                title=str(info.get("title") or "") or None,
                performer=str(info.get("channel") or info.get("uploader") or "") or None,
            )
            result = await card.edit_media(media)
    except Exception as exc:  # noqa: BLE001 - a fallback upload must still run
        logger.info("In-place media edit failed for %s: %s", local.name, exc)
        return False
    if not result.ok and not result.ignored:
        logger.info("Card media edit did not apply: %s", result.detail or result.reason)
    return bool(result.ok)


async def _send_file(
    bot,
    chat_id: int,
    local: Path,
    *,
    filename: str,
    fmt: str,
    info: dict,
    caption: str,
    status: dict,
    height: int | None,
) -> bool:
    """Send the file under a human name, retrying once on a transient failure."""
    duration = status.get("duration")
    seconds = int(duration) if duration else None
    performer = str(info.get("channel") or info.get("uploader") or "") or None

    for attempt in range(2):
        try:
            with open(local, "rb") as handle:
                suffix = local.suffix.lower()
                if fmt == "audio" or suffix == ".mp3":
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=handle,
                        filename=filename,
                        caption=caption,
                        parse_mode="MarkdownV2",
                        title=str(info.get("title") or "") or None,
                        performer=performer,
                        duration=seconds,
                    )
                elif suffix == ".mp4":
                    await bot.send_video(
                        chat_id=chat_id,
                        video=handle,
                        filename=filename,
                        caption=caption,
                        parse_mode="MarkdownV2",
                        supports_streaming=True,
                        width=status.get("width"),
                        height=height,
                        duration=seconds,
                    )
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=handle,
                        filename=filename,
                        caption=caption,
                        parse_mode="MarkdownV2",
                    )
            return True
        except Exception:
            if attempt == 0:
                logger.warning("Upload failed, retrying once")
                await asyncio.sleep(1)
            else:
                logger.exception("Upload failed after retry")
    return False


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

async def report_failure(
    card,
    info: dict,
    message: str,
    message_id: int,
    url_hash: str | None,
    *,
    retry_hint: bool = True,
) -> None:
    """Show a friendly failure and put the format buttons back."""
    text = progress_card.build_error_caption(info, message, retry_hint=retry_hint)
    markup = None
    if url_hash:
        try:
            markup = card_message.retry_keyboard(info, message_id, url_hash)
        except Exception:  # noqa: BLE001 - the message matters more than buttons
            logger.debug("Could not rebuild the format keyboard", exc_info=True)

    if getattr(card, "broken", False):
        # The card cannot be edited anymore; the fallback is a single message
        # that carries both the reason and the buttons.
        await _send_error_fallback(card, text, markup)
        return

    result = await card.edit_caption(text, reply_markup=markup)
    if not result.ok and result.unrecoverable:
        # ``edit_caption`` already posted the text as a fallback; only the
        # buttons are missing at that point.
        await _send_error_fallback(card, text, markup)


async def _send_error_fallback(card, text: str, markup) -> None:
    try:
        await card.bot.send_message(
            chat_id=card.chat_id,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=markup,
        )
    except Exception:
        logger.debug("Error message fallback failed", exc_info=True)


def unlink_quietly(path: Path) -> None:
    """Best-effort cleanup: a locked file must never break the chat flow."""
    try:
        path.unlink()
    except OSError:
        logger.debug("Could not delete %s", path)


# ---------------------------------------------------------------------------
# Direct commands (/mp3, /mp4, /best)
# ---------------------------------------------------------------------------

async def run_direct(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_message,
    url: str,
    fmt: str,
    format_id: str | None,
) -> None:
    """Download without the picker: fetch info, then the usual stages.

    The command's own status message is the card. It has no media and no
    buttons, so every stage edits text.
    """
    user = update.effective_user
    if user is None:
        return
    try:
        info = await get_info(url)
    except KoyuraServiceDown:
        await _edit_or_ignore(status_message, texts.SERVICE_DOWN)
        stats.record_error()
        return
    except KoyuraError as error:
        await _edit_or_ignore(status_message, texts.engine_error(error))
        stats.record_error()
        return

    height = None
    if format_id:
        found = find_format(info, format_id)
        height = found.get("height") if found else None

    card = card_message.CardMessage(
        context.bot,
        status_message.chat_id,
        status_message.message_id,
        photo=False,
        on_fallback=fallback_sender(context.bot, status_message.chat_id),
    )
    await run_download(
        context=context,
        chat_id=status_message.chat_id,
        status_message_id=status_message.message_id,
        url=url,
        info=info,
        fmt=fmt,
        format_id=format_id,
        height=height,
        user_id=user.id,
        username=user.username or str(user.id),
        started_at=time.time(),
        direct=True,
        card=card,
    )


async def _edit_or_ignore(message, text: str) -> None:
    """Edit a status message, tolerating one the user already deleted."""
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(caption=text)
        else:
            await message.edit_text(text)
    except Exception:
        logger.debug("Failed to edit message with: %s", text)
