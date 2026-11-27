"""Progress and error captions for the one-message download card.

A photo message cannot be turned into a text message (``editMessageText`` does
not work on media), so the card stays a media message for its whole life:

    preview (photo + keyboard) -> progress (same photo, live caption)
    -> file (``editMessageMedia``)   |   error (caption + keyboard again)

Everything here is pure: given the ``/api/info`` payload and the engine's
``progress`` dict it returns a MarkdownV2 caption. No Telegram, no clock, no
network, so the formatting and throttling rules are unit-testable.

*What* the live message says is the operator's ``progress`` template (Admin →
**Message templates**), rendered by ``message_templates`` with the values this
module derives: ``@title``, ``@stage``, ``@bar``, ``@percent``, ``@size``,
``@speed``, ``@eta``, ``@elapsed``, ``@quality`` and the pre-joined ``@facts``
line. There is no second set of switches: the template *is* the choice, so the
panel preview and the chat agree.

Two rules survive any template: the stage line is always a value (so
"Downloading…" is never missing from the words on offer) and a body that is
empty, broken or unavailable falls back to the built-in caption, never to an
empty message.
"""
from __future__ import annotations

import logging
import math
import time

import download_card
import message_kinds
import message_templates
from i18n import escape_md, t

logger = logging.getLogger(__name__)

CAPTION_MAX = download_card.CAPTION_MAX
CAPTION_BUDGET = download_card.CAPTION_BUDGET

# Fixed-width text bar: clients are not monospaced, but a constant cell count
# keeps the caption from re-wrapping ("jumping") between edits.
BAR_WIDTH = 12
BAR_FULL = "▓"
BAR_EMPTY = "░"

# Progress labels come from the catalogue (``stage.starting``, ``stage.downloading``,
# ``stage.processing``, ``stage.uploading``, ``stage.searching``); this is the one
# used when the engine reports a stage the bot does not know yet.
DEFAULT_STAGE = "downloading"


def stage_label(stage: object, lang: object = None) -> str:
    """The caption line for one stage, in the user's language.

    An unknown stage falls back to "Downloading…" rather than showing a bare key:
    the engine may grow a stage before the bot learns its word for it.
    """
    name = str(stage or DEFAULT_STAGE)
    key = f"stage.{name}"
    if t(key, lang) == key:
        key = f"stage.{DEFAULT_STAGE}"
    return t(key, lang)


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

def as_number(value: object) -> float | None:
    """``None`` for missing values, ``"NA"`` (yt-dlp) and anything unparsable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return number


def format_size(size: object) -> str | None:
    """``12.4 MB`` / ``768 KB`` / ``1.2 GB`` (space before the unit)."""
    value = as_number(size)
    if value is None or value <= 0:
        return None
    for unit, factor in (("GB", 1024.0 ** 3), ("MB", 1024.0 ** 2), ("KB", 1024.0)):
        if value >= factor:
            amount = value / factor
            digits = 1 if amount < 100 else 0
            return f"{amount:.{digits}f} {unit}"
    return f"{value:.0f} B"


def format_speed(speed: object) -> str | None:
    """``3.2 MB/s`` — bytes per second, as yt-dlp reports it."""
    size = format_size(speed)
    return f"{size}/s" if size else None


def format_eta(seconds: object) -> str | None:
    """``0:42`` / ``12:05`` / ``1:02:03``; also used for the elapsed time."""
    value = as_number(seconds)
    if value is None or value < 0:
        return None
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_bar(
    percent: object, width: int = BAR_WIDTH, *, show_percent: bool = True
) -> str:
    """``▓▓▓▓▓▓▓░░░░░ 62%`` — always ``width`` cells, optionally the percentage.

    The cell count never depends on ``show_percent``: the bar is the one line
    that is always the same physical size, so hiding the number must not make
    the caption re-wrap between edits. The percentage is part of the string on
    purpose: clients render the bar in a proportional font, so the glyphs alone
    can look misleading.
    """
    value = as_number(percent)
    if value is None:
        return ""
    value = max(0.0, min(100.0, value))
    filled = int(value * width / 100.0 + 0.5)
    bar = BAR_FULL * filled + BAR_EMPTY * (width - filled)
    return f"{bar} {value:.0f}%" if show_percent else bar


def _caption_title(info: dict, lang: object = None) -> str:
    title = str(info.get("title") or "").strip() or t("card.download", lang)
    if len(title) > 300:
        title = title[:299] + "…"
    return f"*{escape_md(title)}*"


def _bounded(lines: list[str]) -> str:
    caption = "\n".join(lines)
    if len(caption) > CAPTION_BUDGET:
        caption = caption[: CAPTION_BUDGET - 1].rstrip() + "…"
    return caption


def derive_eta(progress: dict) -> float | None:
    """Engine ETA when it has one, otherwise ``remaining / speed``."""
    eta = as_number(progress.get("eta"))
    if eta is not None and eta >= 0:
        return eta
    speed = as_number(progress.get("speed"))
    total = _total_bytes(progress)
    downloaded = as_number(progress.get("downloaded_bytes"))
    if speed and speed > 0 and total is not None and downloaded is not None:
        return max(0.0, (total - downloaded) / speed)
    return None


def derive_percent(progress: dict) -> float | None:
    """Engine percent when known, else computed from bytes (estimate included)."""
    percent = as_number(progress.get("percent"))
    if percent is not None:
        return percent
    total = _total_bytes(progress)
    downloaded = as_number(progress.get("downloaded_bytes"))
    if total and downloaded is not None and total > 0:
        return round(downloaded / total * 100.0, 1)
    return None


def _total_bytes(progress: dict) -> float | None:
    """Best guess at the download size, for ETA maths only.

    The estimate is good enough to divide by, but it is not a number to *show*:
    see ``_exact_total_bytes``.
    """
    total = as_number(progress.get("total_bytes"))
    if total is not None and total > 0:
        return total
    estimate = as_number(progress.get("total_bytes_estimate"))
    if estimate is not None and estimate > 0:
        return estimate
    return None


def _exact_total_bytes(progress: dict) -> float | None:
    """The real size, when the source actually reported one.

    ``total_bytes_estimate`` drifts as a fragment stream is measured (a captured
    YouTube job went 340 MB -> 504 MB), so printing it as a denominator would
    make the caption jump backwards. Only an exact total is displayed.
    """
    total = as_number(progress.get("total_bytes"))
    return total if total is not None and total > 0 else None


# ---------------------------------------------------------------------------
# Progress caption
# ---------------------------------------------------------------------------

def _normalised_stage(stage: str, percent: float | None) -> str:
    """The stage the caption talks about, with the 99.5% merge folded in."""
    if stage in ("download", ""):
        return "downloading"
    if stage == "downloading" and percent is not None and percent >= 99.5:
        # yt-dlp is finished; the engine is still merging/transcoding.
        return "processing"
    return stage


def _title_text(info: dict, lang: object = None) -> str:
    """The video's own name, unescaped: the template escapes what it inserts."""
    title = str(info.get("title") or "").strip() or t("card.download", lang)
    if len(title) > 300:
        title = title[:299] + "…"
    return title


def _size_fact(progress: dict) -> str | None:
    """``30.8 MB / 30.8 MB``, or the downloaded amount when no exact total."""
    downloaded = as_number(progress.get("downloaded_bytes"))
    total = _exact_total_bytes(progress)
    if downloaded is not None and total:
        return f"{format_size(downloaded)} / {format_size(total)}"
    if downloaded is not None and downloaded > 0:
        # No exact size (HLS/DASH): show how much arrived, never a made-up total.
        return format_size(downloaded)
    return None


def _speed_fact(progress: dict) -> str | None:
    return format_speed(progress.get("speed"))


def _eta_value(progress: dict, percent: float | None) -> str | None:
    """``0:42`` — the engine's estimate, else remaining bytes over speed.

    Nothing is left to report at 100%: yt-dlp reports ``eta 0`` there, and a
    "0:00 left" line would sit in the caption after the work is done.
    """
    if percent is not None and percent >= 100:
        return None
    return format_eta(derive_eta(progress))


def _eta_fact(progress: dict, percent: float | None, lang: object) -> str | None:
    eta = _eta_value(progress, percent)
    return t("progress.eta", lang, eta=eta) if eta else None


def _elapsed_fact(elapsed: float | None, lang: object) -> str | None:
    text = format_eta(elapsed) if elapsed is not None else None
    return t("progress.elapsed", lang, elapsed=text) if text else None


def _facts_line(
    progress: dict,
    *,
    percent: float | None,
    quality: str | None,
    elapsed: float | None,
    lang: object,
) -> str:
    """The ``a · b · c`` line: every fact the engine actually knows.

    This is ``@facts``, for an operator who wants the old one-line summary. The
    percentage comes first and is the same number ``@bar`` prints, so a body
    that uses both shows it twice — write one or the other.
    """
    candidates = (
        f"{percent:.0f}%" if percent is not None else None,
        str(quality) if quality else None,
        _size_fact(progress),
        _speed_fact(progress),
        _eta_fact(progress, percent, lang),
        _elapsed_fact(elapsed, lang),
    )
    return " · ".join(text for text in candidates if text)


def progress_values(
    info: dict,
    progress: dict | None = None,
    *,
    stage: str = "downloading",
    quality: str | None = None,
    elapsed: float | None = None,
    lang: object = None,
) -> dict[str, str]:
    """Every value the ``progress`` template can use, already formatted.

    Unescaped on purpose: ``message_templates`` escapes a value when it inserts
    it and leaves the operator's prose alone. An unknown number is the empty
    string, which makes its line disappear instead of printing a zero.

    ``@bar`` is the 12 cells alone and ``@percent`` the number: the operator
    writes ``@bar @percent`` for the old "bar with a percentage after it", and
    the bar stays exactly 12 cells wide either way so the caption cannot
    re-wrap between edits.
    """
    payload = progress if isinstance(progress, dict) else {}
    percent = derive_percent(payload)
    label = stage_label(_normalised_stage(stage, percent), lang)
    return {
        "title": _title_text(info, lang),
        "stage": label,
        "quality": str(quality or ""),
        "bar": (
            progress_bar(percent, show_percent=False)
            if percent is not None
            else ""
        ),
        "percent": f"{percent:.0f}%" if percent is not None else "",
        "size": _size_fact(payload) or "",
        "speed": _speed_fact(payload) or "",
        "eta": _eta_value(payload, percent) or "",
        "elapsed": format_eta(elapsed) or "" if elapsed is not None else "",
        "facts": _facts_line(
            payload, percent=percent, quality=quality, elapsed=elapsed, lang=lang
        ),
    }


def _progress_body(lang: object) -> str | None:
    """The active body: the operator's, else the catalogue's built-in one."""
    trial = message_templates.custom_body("progress", lang)
    return trial or message_kinds.template("progress", lang)


def _built_in_caption(values: dict[str, str], lang: object) -> str:
    """The floor: the stage line, so a broken template never sends nothing."""
    lines = [f"*{escape_md(values['title'])}*"] if values["title"] else []
    lines.append(escape_md(values["stage"] or stage_label(DEFAULT_STAGE, lang)))
    for key in ("bar", "facts"):
        if values[key]:
            lines.append(escape_md(values[key]))
    return _bounded(lines)


def build_progress_caption(
    info: dict,
    progress: dict | None = None,
    *,
    stage: str = "downloading",
    quality: str | None = None,
    elapsed: float | None = None,
    lang: object = None,
) -> str:
    """The card caption while a job runs, rendered from the ``progress`` template.

    The default body is ``*@title*`` / ``@stage`` / ``@bar`` — the title, what is
    happening, and the 12-cell bar — which is the caption this bot has always
    sent plus the stage line the panel's Default entry shows. Only real data is
    drawn: with no exact total size the caption shows the downloaded amount and
    the speed, never an invented percentage, and ``@bar`` is dropped for a stage
    that reports nothing (starting / processing / uploading).

    A custom body that cannot be rendered (unknown placeholder, unpaired marker,
    blank) is logged at DEBUG and the built-in text is sent instead, exactly like
    every other message kind.
    """
    values = progress_values(
        info, progress, stage=stage, quality=quality, elapsed=elapsed, lang=lang
    )
    body = _progress_body(lang)
    if body:
        try:
            return message_templates.render_body(body, values, media="caption")
        except message_templates.TemplateError as error:
            logger.debug(
                "Template progress/%s is unusable (%s); sending the default",
                lang,
                error,
            )
    return _built_in_caption(values, lang)


# ---------------------------------------------------------------------------
# Error caption
# ---------------------------------------------------------------------------

ERROR_DETAIL_CHARS = 180


def build_error_caption(
    info: dict, error: object, *, retry_hint: bool = True, lang: object = None
) -> str:
    """The card caption when the job failed; the keyboard comes back with it."""
    detail = str(error or "").strip().splitlines()
    detail = detail[0].strip() if detail else t("download.unknown_error", lang)
    if len(detail) > ERROR_DETAIL_CHARS:
        detail = detail[: ERROR_DETAIL_CHARS - 1].rstrip() + "…"
    lines = [
        _caption_title(info, lang),
        escape_md(t("download.failed", lang)),
        escape_md(detail),
    ]
    if retry_hint:
        lines.append(escape_md(t("download.retry_hint", lang)))
    return message_templates.render(
        "error",
        lang,
        {
            "title": str(info.get("title") or "").strip() or t("card.download", lang),
            "error": detail,
            "retry_hint": t("download.retry_hint", lang) if retry_hint else "",
            "url": "",
            "limit": "",
        },
        _bounded(lines),
        media="caption",
    )


def build_status_caption(info: dict, text: str, *, lang: object = None) -> str:
    """A short one-off caption (starting, busy, service down)."""
    return _bounded([_caption_title(info, lang), escape_md(text)])


# ---------------------------------------------------------------------------
# Queue captions
# ---------------------------------------------------------------------------

QUEUE_HINT_KEY = "queue.hint"


def format_queue_wait(seconds: object, lang: object = None) -> str | None:
    """``~45 s`` / ``~2 min`` — coarse on purpose: the estimate is a guess.

    Minutes round **up**: a wait that is really 2m25s must not be announced as
    "2 min" and then turn out to be longer.
    """
    value = as_number(seconds)
    if value is None or value <= 0:
        return None
    if value < 90:
        rounded = int(round(value / 5.0) * 5) or 5
        return t("queue.wait_seconds", lang, seconds=rounded)
    return t("queue.wait_minutes", lang, minutes=max(2, math.ceil(value / 60.0)))


def build_queued_caption(
    info: dict,
    position: object,
    wait_seconds: object = None,
    *,
    approximate: bool = True,
    lang: object = None,
) -> str:
    """The card caption while the job waits for a free slot.

    ``Queue #3 · ~45 s`` — the position comes from the engine, and a missing
    number is simply not drawn rather than filled in with a made-up guess.
    """
    lines = [_caption_title(info, lang)]
    place = as_number(position)
    head = t("queue.head", lang)
    if place is not None and place >= 1:
        head = t("queue.head_number", lang, position=int(place))
    wait = format_queue_wait(wait_seconds, lang)
    if wait:
        head = f"{head} \u00b7 {wait}"
    lines.append(escape_md(head))
    if approximate and wait:
        lines.append(escape_md(t("queue.estimated", lang)))
    lines.append(escape_md(t(QUEUE_HINT_KEY, lang)))
    return message_templates.render(
        "queued",
        lang,
        {
            "title": str(info.get("title") or "").strip() or t("card.download", lang),
            "position": head,
            "wait": wait or "",
            "hint": t(QUEUE_HINT_KEY, lang),
        },
        _bounded(lines),
        media="caption",
    )


def busy_message(
    error: BaseException | str, *, seconds: object = None, lang: object = None
) -> str:
    """User-facing text for a refusal from the engine.

    The engine's own message is usually English prose about internal limits; a
    single sentence the user can act on is better than forwarding it, and the
    wait is rounded to something a human can plan around.
    """
    wait = None
    code = None
    if not isinstance(error, str):
        wait = getattr(error, "retry_after", None) or getattr(error, "wait_seconds", None)
        code = getattr(error, "code", None)
    if seconds is not None:
        wait = seconds
    when = format_queue_wait(wait, lang) or t("download.wait_default", lang)
    kind = "download.busy_user" if code == "user_busy" else "download.busy_service"
    # Plain: the caller escapes the sentence with the caption around it.
    return message_templates.render_plain_line(
        kind, lang, t(kind, lang, when=when), when=when
    )


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

class ProgressThrottle:
    """Decide whether a progress edit may be sent now.

    Telegram rate-limits message edits hard, so an update is only allowed when
    it is both **old enough** (``min_interval``) and **different enough**
    (``min_percent_delta`` or a noticeable ETA drop). The first update always
    goes through.
    """

    def __init__(
        self,
        min_interval: float = 3.0,
        min_percent_delta: float = 1.5,
        min_eta_drop: float = 5.0,
    ) -> None:
        self.min_interval = float(min_interval)
        self.min_percent_delta = float(min_percent_delta)
        self.min_eta_drop = float(min_eta_drop)
        self._last_at: float | None = None
        self._last_percent: float | None = None
        self._last_eta: float | None = None

    def should_update(
        self,
        percent: float | None,
        eta: float | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        """Return True when this update should be rendered, and remember it."""
        moment = time.monotonic() if now is None else float(now)
        if self._last_at is not None and moment - self._last_at < self.min_interval:
            return False

        if self._last_at is None or percent is None or self._last_percent is None:
            changed = True
        else:
            changed = abs(percent - self._last_percent) >= self.min_percent_delta
            if not changed and eta is not None and self._last_eta is not None:
                changed = (self._last_eta - eta) >= self.min_eta_drop

        if not changed:
            return False
        self._last_at = moment
        self._last_percent = percent
        self._last_eta = eta
        return True

    def force(self, percent: float | None = None, eta: float | None = None,
              *, now: float | None = None) -> None:
        """Record a mandatory edit (stage change) so the next one is compared with it."""
        self._last_at = time.monotonic() if now is None else float(now)
        self._last_percent = percent
        self._last_eta = eta
