"""Progress and error captions for the one-message download card.

A photo message cannot be turned into a text message (``editMessageText`` does
not work on media), so the card stays a media message for its whole life:

    preview (photo + keyboard) -> progress (same photo, live caption)
    -> file (``editMessageMedia``)   |   error (caption + keyboard again)

Everything here is pure: given the ``/api/info`` payload and the engine's
``progress`` dict it returns a MarkdownV2 caption. No Telegram, no clock, no
network, so the formatting and throttling rules are unit-testable.
"""
from __future__ import annotations

import math
import time

import download_card

CAPTION_MAX = download_card.CAPTION_MAX
CAPTION_BUDGET = download_card.CAPTION_BUDGET

# Fixed-width text bar: clients are not monospaced, but a constant cell count
# keeps the caption from re-wrapping ("jumping") between edits.
BAR_WIDTH = 12
BAR_FULL = "▓"
BAR_EMPTY = "░"

STAGE_LABELS = {
    "starting": "Starting…",
    "downloading": "Downloading…",
    "processing": "Processing…",
    "uploading": "Uploading…",
}


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


def progress_bar(percent: object, width: int = BAR_WIDTH) -> str:
    """``▓▓▓▓▓▓▓░░░░░ 62%`` — always ``width`` cells plus the percentage.

    The percentage is part of the string on purpose: clients render the bar in
    a proportional font, so the glyphs alone can look misleading.
    """
    value = as_number(percent)
    if value is None:
        return ""
    value = max(0.0, min(100.0, value))
    filled = int(value * width / 100.0 + 0.5)
    bar = BAR_FULL * filled + BAR_EMPTY * (width - filled)
    return f"{bar} {value:.0f}%"


def _caption_title(info: dict) -> str:
    title = str(info.get("title") or "").strip() or "Download"
    if len(title) > 300:
        title = title[:299] + "…"
    return f"*{download_card.escape_md(title)}*"


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

def build_progress_caption(
    info: dict,
    progress: dict | None = None,
    *,
    stage: str = "downloading",
    quality: str | None = None,
    elapsed: float | None = None,
) -> str:
    """The card caption while a job runs.

    Only real data is drawn: when the engine reports no total size the caption
    shows the downloaded amount and the speed, never an invented percentage.
    """
    progress = progress if isinstance(progress, dict) else {}
    percent = derive_percent(progress)
    if stage == "download" or stage == "":
        stage = "downloading"
    elif stage == "downloading" and percent is not None and percent >= 99.5:
        # yt-dlp is finished; the engine is still merging/transcoding.
        stage = "processing"

    lines = [_caption_title(info)]

    label = STAGE_LABELS.get(stage, "Downloading…")
    if percent is None or stage in ("starting", "processing", "uploading"):
        lines.append(download_card.escape_md(label))
    if percent is not None:
        lines.append(download_card.escape_md(progress_bar(percent)))

    facts: list[str] = []
    if quality:
        facts.append(str(quality))
    downloaded = as_number(progress.get("downloaded_bytes"))
    total = _exact_total_bytes(progress)
    if downloaded is not None and total:
        facts.append(f"{format_size(downloaded)} / {format_size(total)}")
    elif downloaded is not None and downloaded > 0:
        # No exact size (HLS/DASH): show how much arrived, never a made-up total.
        facts.append(format_size(downloaded) or "")
    speed = format_speed(progress.get("speed"))
    if speed:
        facts.append(speed)
    eta = derive_eta(progress)
    if eta is not None and percent is not None and percent < 100:
        facts.append(f"ETA {format_eta(eta)}")
    elapsed_text = format_eta(elapsed) if elapsed is not None else None
    if elapsed_text:
        facts.append(f"{elapsed_text} elapsed")

    facts = [fact for fact in facts if fact]
    if facts:
        lines.append(download_card.escape_md(" · ".join(facts)))
    return _bounded(lines)


# ---------------------------------------------------------------------------
# Error caption
# ---------------------------------------------------------------------------

ERROR_DETAIL_CHARS = 180


def build_error_caption(info: dict, error: object, *, retry_hint: bool = True) -> str:
    """The card caption when the job failed; the keyboard comes back with it."""
    detail = str(error or "").strip().splitlines()
    detail = detail[0].strip() if detail else "Unknown error"
    if len(detail) > ERROR_DETAIL_CHARS:
        detail = detail[: ERROR_DETAIL_CHARS - 1].rstrip() + "…"
    lines = [
        _caption_title(info),
        download_card.escape_md("Download failed"),
        download_card.escape_md(detail),
    ]
    if retry_hint:
        lines.append(download_card.escape_md("Tap a format to try again."))
    return _bounded(lines)


def build_status_caption(info: dict, text: str) -> str:
    """A short one-off caption (starting, busy, service down)."""
    return _bounded([_caption_title(info), download_card.escape_md(text)])


# ---------------------------------------------------------------------------
# Queue captions
# ---------------------------------------------------------------------------

QUEUE_HINT = "You can leave this chat — the file arrives here."


def format_queue_wait(seconds: object) -> str | None:
    """``~45 s`` / ``~2 min`` — coarse on purpose: the estimate is a guess.

    Minutes round **up**: a wait that is really 2m25s must not be announced as
    "2 min" and then turn out to be longer.
    """
    value = as_number(seconds)
    if value is None or value <= 0:
        return None
    if value < 90:
        rounded = int(round(value / 5.0) * 5) or 5
        return f"~{rounded} s"
    return f"~{max(2, math.ceil(value / 60.0))} min"


def build_queued_caption(
    info: dict,
    position: object,
    wait_seconds: object = None,
    *,
    approximate: bool = True,
) -> str:
    """The card caption while the job waits for a free slot.

    ``Queue #3 · ~45 s`` — the position comes from the engine, and a missing
    number is simply not drawn rather than filled in with a made-up guess.
    """
    lines = [_caption_title(info)]
    place = as_number(position)
    head = "In queue"
    if place is not None and place >= 1:
        head = f"In queue \u2116{int(place)}"
    wait = format_queue_wait(wait_seconds)
    if wait:
        head = f"{head} \u00b7 {wait}"
    lines.append(download_card.escape_md(head))
    if approximate and wait:
        lines.append(download_card.escape_md("Estimated wait"))
    lines.append(download_card.escape_md(QUEUE_HINT))
    return _bounded(lines)


def busy_message(error: BaseException | str, *, seconds: object = None) -> str:
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
    when = format_queue_wait(wait) or "~2 min"
    if code == "user_busy":
        return f"You already have a download running. Try again in {when}."
    return f"The service is overloaded right now — try again in {when}."


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
