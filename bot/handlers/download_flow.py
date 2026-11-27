"""One download, from the button to the file in the chat.

The engine does the work; this module is the choreography around it:

    button (/mp3) -> start_download -> status poll -> upload -> done event

and every stage **replaces** the one before it (see ``handlers.stages``): the
preview card is deleted once the progress message is up, the progress message is
deleted once the file (or the error card) is up. The chat therefore shows one
status at a time, and only the result — the file, or the error with its format
buttons — stays behind.

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

import cookie_help
import download_card
import event_client
import progress_card
import transcode_policy as policy
from koyura_client import (
    KoyuraBusy,
    KoyuraError,
    KoyuraServiceDown,
    get_info,
    poll_status,
    start_download,
)

from . import config, language, sessions, stages, stats, texts

logger = logging.getLogger(__name__)

PARSE_MODE = "MarkdownV2"


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


@dataclass(frozen=True)
class Choice:
    """What a tap (or ``/mp4``) will actually ask the engine for.

    ``refusal`` is the catalogue key of the "nothing can be sent" line: the
    caller renders it in the user's language instead of starting a job.
    ``skipped`` is ``(chosen, skipped)`` quality labels when the best quality was
    over the send limit and a smaller one was pinned, so the caption can say so.

    ``delivery`` is what Telegram gets: a video, a document, or a conversion
    (``transcode_policy.SEND_VIDEO`` / ``SEND_FILE`` / ``CONVERT``). ``ask``
    means the format needs the user's decision first, and ``format`` is the
    entry that decision is about.
    """

    fmt: str
    format_id: str | None = None
    height: int | None = None
    skipped: tuple[str, str] | None = None
    refusal: str | None = None
    #: ``SEND_VIDEO`` / ``SEND_FILE`` / ``CONVERT`` — what Telegram gets.
    delivery: str = policy.SEND_VIDEO
    #: The user must answer "send as file or convert" first.
    ask: bool = False
    #: The format the question (and its estimate) is about.
    entry: dict | None = None

    @property
    def transcode(self) -> str:
        """The ``transcode`` value the engine expects (``force`` / ``never``)."""
        return "force" if self.delivery == policy.CONVERT else "never"

    @property
    def as_document(self) -> bool:
        """Whether the finished file goes out as a document, not as a video."""
        return self.delivery == policy.SEND_FILE


def plan_choice(
    info: dict, action: str, *, limit_bytes: int | None = None
) -> Choice:
    """The choice that can actually be delivered, not just the one asked for.

    ``best`` no longer leaves the decision to the engine: the engine's best may
    be a file Telegram refuses, and the user would only learn that after a
    download. The highest format whose **known** size fits the ceiling is pinned
    instead, and when that is not the top one the caller is told which was
    skipped. ``mp3`` is treated the same way: an MP3 predicted to be too large
    is refused before the job starts rather than after it fails.

    An explicit format id (a button) is forwarded untouched — the keyboard only
    offers formats that fit — while a format that the limit removed is caught
    above by the offer.

    The delivery is decided here too, from the format's codec and the engine's
    ``transcode.mode``: a playable H.264 file goes straight through, a codec the
    engine cannot convert goes out as a document, and an AV1/VP9 file either
    converts (``always``, or ``auto`` inside the size ceiling) or asks the user
    first. Nothing converts without that decision.
    """
    if action == "mp3":
        offer = download_card.format_offer(info, limit_bytes=limit_bytes)
        if offer.mp3 is None:
            return Choice("audio", refusal=download_card.limit_notice_key(info))
        return Choice("audio")

    if action in ("best", "sendasfile", "convert"):
        return _plan_best(info, limit_bytes=limit_bytes)

    fmt, format_id, height = resolve_choice(info, action)
    entry = find_format(info, format_id) or {}
    decision = policy.decide(
        policy.engine_mode(info),
        entry,
        max_mb=policy.engine_max_mb(info),
    )
    return Choice(
        fmt,
        format_id,
        height,
        delivery=decision,
        ask=decision == policy.ASK,
        entry=entry or None,
    )


def _plan_best(info: dict, *, limit_bytes: int | None = None) -> Choice:
    """The highest sendable format, plus the quality that was skipped."""
    offer = download_card.format_offer(info, limit_bytes=limit_bytes)
    if not offer.formats:
        return Choice("video", refusal=download_card.limit_notice_key(info))

    chosen = offer.formats[0]
    top = video_formats(info)[0]
    skipped = None
    if str(chosen.get("id")) != str(top.get("id")):
        skipped = (
            download_card.format_quality(chosen.get("height"), chosen.get("label")),
            download_card.format_quality(top.get("height"), top.get("label")),
        )
    height = chosen.get("height")
    decision = policy.decide(
        policy.engine_mode(info),
        chosen,
        max_mb=policy.engine_max_mb(info),
    )
    return Choice(
        "video",
        str(chosen.get("id")) if chosen.get("id") is not None else None,
        int(height) if height else None,
        skipped=skipped,
        delivery=decision,
        ask=decision == policy.ASK,
        entry=chosen,
    )


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
        for field_name in ("size_bytes", "filesize", "filesize_approx"):
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

    size = None
    for field_name in ("size_bytes", "filesize", "filesize_approx"):
        size = progress_card.as_number(chosen.get(field_name))
        if size and size > 0:
            break
    if not size or size <= 0:
        return None
    total = float(size)
    audio_size = download_card.format_size_bytes(audio)
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
    lang: str = "en"
    skipped: tuple[str, str] | None = None
    #: ``SEND_VIDEO`` / ``SEND_FILE`` / ``CONVERT`` — see ``transcode_policy``.
    delivery: str = policy.SEND_VIDEO

    @property
    def title(self) -> str:
        return str(self.info.get("title") or "").strip() or "download"

    @property
    def quality(self) -> str:
        return quality_label(self.fmt, self.format_id, self.height, self.info)

    @property
    def transcode_request(self) -> str:
        """The ``transcode`` value the engine needs for this job."""
        return "force" if self.delivery == policy.CONVERT else "never"

    @property
    def limit_note(self) -> str | None:
        """The caption line explaining a quality that was lowered to fit."""
        if not self.skipped:
            return None
        return texts.limit_note(self.skipped[0], self.skipped[1], self.lang)

    @property
    def document_note(self) -> str | None:
        """The caption line explaining why the file arrived as a document.

        Only drawn when the *user* asked for a file on a codec Telegram may not
        play (``delivery == ASK`` once the answer is known): the note is the
        answer to a question they just answered, not a surprise.
        """
        if self.delivery != policy.SEND_FILE:
            return None
        return texts.plain("transcode.document", self.lang)


def fallback_sender(bot, chat_id: int):
    """Build the hook a stage calls when it can no longer edit its message."""

    async def send(text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="MarkdownV2")

    return send


def retry_markup(info: dict, url_hash: str | None, lang: object = None):
    """``markup_factory(message_id)`` for the format keyboard of a failed card.

    A missing hash means there is nothing to rebuild the buttons from, and the
    error message is then sent without a keyboard.
    """
    if not url_hash:
        return None
    return lambda message_id: download_card.build_format_keyboard(
        info, message_id, url_hash, lang=lang
    )


async def ask_transcode(
    stage,
    info: dict,
    choice: Choice,
    *,
    url_hash: str | None,
    lang: object = None,
) -> bool:
    """Show ``[Send as file] [Convert (~4 min)]`` instead of starting a job.

    Called for a format Telegram may not play as a video when the engine's mode
    is ``off`` (the default) or the file is over ``TRANSCODE_MAX_MB`` in
    ``auto``. The download has not started yet, so the answer costs nothing but
    a question.

    The buttons carry the format id and the answer, and the card's session is
    re-homed onto the message that now carries them — exactly like the format
    keyboard after a failure, so a tap on the fresh card is never "expired".

    Returns ``False`` when the question could not be posted (no hash, or the
    stage has gone); the caller then continues as if the user had asked for a
    file, which is always safe and never converts.
    """
    if not url_hash:
        return False
    eta = policy.estimate_seconds(choice.entry)

    def markup(message_id: int):
        return download_card.build_transcode_keyboard(
            choice.entry, message_id, url_hash, lang=lang, eta_seconds=eta
        )

    result = await stage.replace_text(
        download_card.build_transcode_question(info, choice.entry, lang=lang),
        markup_factory=markup,
    )
    if not result.ok:
        return False
    if stage.message_id is not None:
        sessions.rebind(stage.chat_id, stage.message_id, url_hash, stage.message_id)
    return True


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
    tracker=None,
    entry: dict | None = None,
    lang: str | None = None,
    skipped: tuple[str, str] | None = None,
    delivery: str = policy.SEND_VIDEO,
) -> None:
    """Start a job, move it through its stages and finish it with the file.

    ``entry`` is the picker session; it is unlocked in a ``finally`` so a failed
    download gives the format buttons back instead of leaving the card dead.
    ``lang`` is resolved once, by the caller that has the user in hand: a job can
    outlive the message that started it.

    ``delivery`` is what Telegram gets (``transcode_policy``): a video, a
    document, or a conversion. It is decided *before* the job starts, because
    the engine only converts when the request says so.
    """
    if lang is None:
        lang = await language.current(user_id)
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
        lang=lang,
        skipped=skipped,
        delivery=delivery,
    )
    if tracker is None:
        tracker = stages.StageTracker(
            context.bot,
            chat_id,
            status_message_id,
            photo=bool((entry or {}).get("photo", True)),
        )

    try:
        await _advance(job, context, tracker, direct=direct)
    finally:
        if entry is not None:
            entry["job_active"] = False


async def _advance(job: Job, context: ContextTypes.DEFAULT_TYPE, stage, *, direct: bool) -> None:
    """The stages of one download, each replacing the previous message."""

    async def fail(message: str, *, retry_hint: bool = True) -> None:
        """Replace the status with the failure and give the format buttons back."""
        await report_failure(
            stage,
            job.info,
            message,
            job.url_hash,
            retry_hint=retry_hint,
            lang=job.lang,
        )
        # The error card is a new message with its own buttons, so the session
        # has to follow it — otherwise "tap a format to try again" on a card
        # posted a second ago would answer "session expired".
        _rehome_session(job, stage)
        stats.record_error()

    # Stage 1 -> 2: the preview card goes, a progress message arrives. The
    # picker's session stays keyed to the old message, so a tap that was already
    # in flight is still recognised ("already downloading" rather than
    # "expired").
    await stage.replace_text(
        progress_card.build_status_caption(
            job.info, texts.plain("stage.starting", job.lang), lang=job.lang
        )
    )

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
            transcode=job.transcode_request,
        )
    except KoyuraBusy as refusal:
        # A refusal has its own wording: the wait is known and the format
        # buttons come back with the error card, so "tap a format" is noise.
        await fail(progress_card.busy_message(refusal, lang=job.lang), retry_hint=False)
        return
    except KoyuraServiceDown:
        await fail(texts.plain("engine.down", job.lang))
        return
    except KoyuraError as error:
        await fail(str(error))
        return

    job_id = str(accepted.get("job_id") or "")
    if not job_id:
        await fail(texts.plain("download.unexpected", job.lang))
        return

    # A queued job keeps the same message and shows its place; the poll loop
    # below turns the wait into the usual progress as soon as a slot frees up.
    if accepted.get("status") == "queued":
        await stage.edit(
            progress_card.build_queued_caption(
                job.info,
                accepted.get("queue_position"),
                accepted.get("wait_seconds"),
                approximate=bool(accepted.get("queue_estimate_approximate", True)),
                lang=job.lang,
            )
        )

    await _announce_start(job, job_id)

    status = await poll_until_finished(job, job_id, stage)
    if status is None:
        stats.record_error()
        return

    if status.get("status") == "error":
        # A spent cookie session has its own sentence: "add cookies for Reddit"
        # is actionable, the raw yt-dlp line is not. The message is built from
        # the service the engine named, so nothing here knows a site's name.
        if cookie_help.needs_cookies(status):
            detail = cookie_help.message(
                status, user_id=job.user_id, lang=job.lang
            )
        else:
            detail = str(
                status.get("error") or texts.plain("download.unknown_error", job.lang)
            )
        await fail(detail)
        await event_client.send_download_error(
            job_id=job_id,
            error_message=status.get("error", texts.EVENT_UNKNOWN_ERROR),
        )
        return

    await _deliver(job, job_id, stage, context.bot, status, direct=direct)


def _rehome_session(job: Job, stage) -> None:
    """Point the picker session at the message that carries the buttons now."""
    if not job.url_hash or stage.message_id is None:
        return
    sessions.rebind(
        job.chat_id, job.status_message_id, job.url_hash, stage.message_id
    )


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

async def poll_until_finished(job: Job, job_id: str, stage) -> dict | None:
    """Poll the engine every couple of seconds, rewriting the progress message.

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
                stage, job.info, texts.plain("engine.down", job.lang), lang=job.lang
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
            await _show_queue_state(stage, job.info, status, throttle, job.lang)
            continue

        # The engine converts inside the same job (``stage: converting``), so the
        # card only has to change its word and read ffmpeg's percentage from the
        # same ``progress`` dict. Nothing converts unless the user asked for it.
        stage_name = str(status.get("stage") or "downloading")

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
        await stage.edit(
            progress_card.build_progress_caption(
                job.info,
                progress,
                stage=stage_name,
                quality=job.quality,
                elapsed=elapsed,
                lang=job.lang,
            )
        )

    await report_failure(
        stage, job.info, texts.plain("download.timed_out", job.lang), lang=job.lang
    )
    await event_client.send_download_error(
        job_id=job_id, error_message=texts.EVENT_TIMED_OUT
    )
    return None


async def _show_queue_state(stage, info: dict, status: dict, throttle, lang: object = None) -> None:
    """Rewrite the progress message with the job's place in the queue.

    The same throttle as the progress caption applies, so a job moving up the
    queue cannot spam Telegram with edits.
    """
    position = progress_card.as_number(status.get("queue_position"))
    wait = progress_card.as_number(status.get("wait_seconds"))
    if not throttle.should_update(position, wait):
        return
    await stage.edit(
        progress_card.build_queued_caption(
            info,
            position,
            wait,
            approximate=bool(status.get("queue_estimate_approximate", True)),
            lang=lang,
        )
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
    job: Job, job_id: str, stage, bot, status: dict, *, direct: bool
) -> None:
    """Post the finished file as its own message and drop the progress card."""
    local = local_file(status)
    if local is None:
        await report_failure(
            stage,
            job.info,
            texts.plain("download.file_missing", job.lang),
            job.url_hash,
            lang=job.lang,
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
        lang=job.lang,
        note=job.limit_note or job.document_note,
    )

    # Stage 3: the file replaces the progress message. It is uploaded first and
    # the progress message is deleted only after Telegram accepted it, so a
    # failed upload leaves something on screen for the error card.
    document = _goes_out_as_document(job, status)
    delivered = await stage.finish_with_file(
        lambda: _send_file(
            bot,
            job.chat_id,
            local,
            filename=filename,
            fmt=job.fmt,
            info=job.info,
            caption=caption,
            status=status,
            height=height,
            as_document=document,
        )
    )
    if not delivered.ok:
        await report_failure(
            stage,
            job.info,
            texts.plain("download.upload_failed", job.lang),
            job.url_hash,
            lang=job.lang,
        )
        stats.record_error()
        await event_client.send_download_error(
            job_id=job_id, error_message=texts.EVENT_UPLOAD_FAILED
        )
        unlink_quietly(local)
        return

    stats.record_download()
    with contextlib.suppress(Exception):
        await event_client.send_download_done(
            job_id=job_id,
            file_size_bytes=size_bytes or 0,
            duration_seconds=time.time() - job.started_at,
            filename=filename,
        )

    logger.debug("Delivered %s as its own message (progress card retired)", filename)
    unlink_quietly(local)


def _goes_out_as_document(job: Job, status: dict) -> bool:
    """Whether the finished file must be a document instead of a video.

    Telegram does not re-encode, so a VP9/AV1 file uploaded as a video shows a
    broken frame on iOS; as a document it always arrives intact. The engine is
    the authority (``send_as_document`` / ``converted`` on ``/api/status``), and
    the flow's own decision covers an older engine that reports neither:

    * converted or H.264 → a video, whatever the user picked;
    * the user chose ``Send as file`` → a document;
    * an incompatible codec with ``TRANSCODE_MODE=off`` → a document.
    """
    if not isinstance(status, dict):
        return job.delivery == policy.SEND_FILE
    if status.get("converted") or status.get("vcodec") == "h264":
        return False
    if status.get("send_as_document"):
        return True
    if job.fmt == "audio":
        return False
    return job.delivery == policy.SEND_FILE or bool(status.get("vcodec"))


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
    as_document: bool = False,
):
    """Send the file under a human name, retrying once on a transient failure.

    ``as_document`` forces ``send_document`` for a video file whose codec
    Telegram would not play: the same caption, the same name, no broken preview.

    Returns the posted message, or ``None`` when both attempts failed.
    """
    duration = status.get("duration")
    seconds = int(duration) if duration else None
    performer = str(info.get("channel") or info.get("uploader") or "") or None

    for attempt in range(2):
        try:
            with open(local, "rb") as handle:
                suffix = local.suffix.lower()
                if fmt == "audio" or suffix == ".mp3":
                    return await bot.send_audio(
                        chat_id=chat_id,
                        audio=handle,
                        filename=filename,
                        caption=caption,
                        parse_mode="MarkdownV2",
                        title=str(info.get("title") or "") or None,
                        performer=performer,
                        duration=seconds,
                    )
                if suffix == ".mp4" and not as_document:
                    return await bot.send_video(
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
                return await bot.send_document(
                    chat_id=chat_id,
                    document=handle,
                    filename=filename,
                    caption=caption,
                    parse_mode="MarkdownV2",
                )
        except Exception:
            if attempt == 0:
                logger.warning("Upload failed, retrying once")
                await asyncio.sleep(1)
            else:
                logger.exception("Upload failed after retry")
    return None


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

async def report_failure(
    stage,
    info: dict,
    message: str,
    url_hash: str | None = None,
    *,
    retry_hint: bool = True,
    lang: object = None,
) -> None:
    """Replace the status message with the failure and its format buttons."""
    text = progress_card.build_error_caption(
        info, message, retry_hint=retry_hint, lang=lang
    )
    await stage.finish_with_text(
        text, markup_factory=retry_markup(info, url_hash, lang)
    )


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
    *,
    lang: str | None = None,
) -> None:
    """Download without the picker: fetch info, then the usual stages.

    The command's own status message is the first stage. It has no media and no
    buttons, so every replacement is a text message until the file arrives.

    The requested format is resolved **before** the job starts: ``/mp4`` and
    ``/best`` ask for "the best quality", and the best quality may be a file
    Telegram refuses. In that case the highest sendable format is pinned and the
    caption says which one was skipped; when nothing fits at all, the status line
    becomes the explanation instead of a download that cannot succeed.

    A command has no buttons, so a codec Telegram may not play is answered by the
    command itself: it is delivered as a document (``SEND_FILE``) and the caption
    says why. Nothing converts without a tap, which a bare command cannot offer.
    """
    user = update.effective_user
    if user is None:
        return
    if lang is None:
        lang = await language.current(user.id)
    stage = stages.StageTracker(
        context.bot, status_message.chat_id, status_message.message_id
    )
    try:
        info = await get_info(url)
    except KoyuraServiceDown:
        await stage.edit(texts.md("engine.down", lang))
        stats.record_error()
        return
    except KoyuraError as error:
        await stage.edit(texts.md("engine.error", lang, error=error))
        stats.record_error()
        return

    action = "mp3" if fmt == "audio" else (str(format_id) if format_id else "best")
    choice = plan_choice(info, action)
    if choice.refusal:
        await stage.edit(texts.limit_message(choice.refusal, lang))
        return

    delivery = choice.delivery
    if delivery == policy.ASK:
        # No question to ask without buttons: send the file, never convert.
        delivery = policy.SEND_FILE

    await run_download(
        context=context,
        chat_id=status_message.chat_id,
        status_message_id=status_message.message_id,
        url=url,
        info=info,
        fmt=choice.fmt,
        format_id=choice.format_id,
        height=choice.height,
        user_id=user.id,
        username=user.username or str(user.id),
        started_at=time.time(),
        direct=True,
        tracker=stage,
        lang=lang,
        skipped=choice.skipped,
        delivery=delivery,
    )
