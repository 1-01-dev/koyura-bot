"""Preview card, inline keyboard and file naming for the download flow.

Everything here is pure: it takes the ``/api/info`` payload and returns text or
``InlineKeyboardMarkup`` objects, so the formatting rules are unit-testable
without Telegram, an engine, or a network.

The filename helpers mirror ``koyura/media_meta.py`` on purpose (like
``url_safety.py``): the engine sanitises the name it records for the dashboard,
the bot sanitises the name it actually uploads, and neither needs the other's
package on its import path.
"""
from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CAPTION_MAX = 1024
# Leave a little headroom: Telegram counts entities and emoji differently.
CAPTION_BUDGET = 1000

# A card with 20 buttons is unusable on a phone; beyond this the list is
# truncated and one "best quality" button is offered instead.
MAX_FORMAT_BUTTONS = 10
BUTTONS_PER_ROW = 2
# Telegram rejects callback_data longer than 64 bytes; checked in the builder.
CALLBACK_DATA_MAX_BYTES = 64

MAX_TITLE_CHARS = 60
_UNSAFE_FILENAME_CHARS = frozenset('/\\:*?"<>|')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


# ---------------------------------------------------------------------------
# Escaping and formatting
# ---------------------------------------------------------------------------

def escape_md(text: object) -> str:
    """Escape MarkdownV2 specials; every caption here uses MarkdownV2."""
    return "".join(f"\\{c}" if c in _MD_SPECIAL else c for c in str(text))


def format_count(count: object) -> str | None:
    """``1.3M`` / ``110.6K`` / ``942``."""
    try:
        value = int(count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    for unit, factor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if value >= factor:
            amount = value / factor
            text = f"{amount:.1f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
    return str(value)


def format_filesize(size: object) -> str | None:
    """``32.1MB`` / ``768KB``; ``None`` when the engine had no size."""
    try:
        value = float(size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    for unit, factor in (("GB", 1024.0 ** 3), ("MB", 1024.0 ** 2), ("KB", 1024.0)):
        if value >= factor:
            amount = value / factor
            digits = 1 if amount < 100 else 0
            return f"{amount:.{digits}f}{unit}"
    return f"{value:.0f}B"


def format_duration(seconds: object) -> str | None:
    """``1:02`` / ``1:02:03``."""
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_quality(height: object, label: object = None) -> str:
    """``1080p`` for a video, ``MP3`` for audio (used in captions and events)."""
    if label:
        return str(label)
    try:
        return f"{int(height)}p"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "best"


def format_upload_date(raw: object) -> str | None:
    """``20141110`` -> ``2014-11-10``; already formatted values pass through."""
    if not isinstance(raw, str):
        return None
    digits = raw.strip()
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 10 and digits[4] == "-" and digits[7] == "-":
        return digits
    return None


# ---------------------------------------------------------------------------
# Preview card
# ---------------------------------------------------------------------------

def video_formats(info: dict) -> list[dict]:
    """Video formats, highest quality first (one per height)."""
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    return sorted(
        formats,
        key=lambda f: (f.get("height") or 0, f.get("fps") or 0),
        reverse=True,
    )


def _video_formats(info: dict) -> list[dict]:
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    return sorted(
        formats,
        key=lambda f: (f.get("height") or 0, f.get("fps") or 0),
        reverse=True,
    )


def card_headline(info: dict) -> str:
    """The card's first line: the title, escaped and bounded."""
    title = str(info.get("title") or "").strip() or "Untitled"
    if len(title) > 300:
        title = title[:299] + "…"
    return f"*{escape_md(title)}*"


def card_meta_lines(info: dict) -> list[str]:
    """Channel / duration / engagement / category / date, minus what's missing."""
    lines: list[str] = []

    channel = str(info.get("channel") or info.get("uploader") or "").strip()
    handle = str(info.get("uploader_id") or "").strip()
    if handle and not handle.startswith("@"):
        handle = handle if handle.startswith("@") else f"@{handle}"
    if channel and handle and handle.lstrip("@").lower() not in channel.lower():
        lines.append(f"{escape_md(channel)} · {escape_md(handle)}")
    elif channel or handle:
        lines.append(escape_md(channel or handle))

    parts = [escape_md(f"{format_duration(info.get('duration'))}")]
    if format_duration(info.get("duration")):
        lines.append(f"Duration: {parts[0]}")

    followers = format_count(info.get("channel_follower_count"))
    if followers:
        lines.append(f"Followers: {escape_md(followers)}")

    engagement = [
        f"{escape_md(format_count(info[key]))} {noun}"
        for key, noun in (
            ("view_count", "views"),
            ("like_count", "likes"),
            ("comment_count", "comments"),
        )
        if format_count(info.get(key))
    ]
    if engagement:
        lines.append(" · ".join(engagement))

    categories = [
        str(c).strip() for c in (info.get("categories") or []) if str(c or "").strip()
    ]
    if categories:
        lines.append(f"Category: {escape_md(', '.join(categories[:2]))}")

    upload_date = format_upload_date(info.get("upload_date"))
    if upload_date:
        lines.append(f"Uploaded: {escape_md(upload_date)}")

    return lines


def _format_lines(info: dict, limit: int | None = None) -> list[str]:
    lines = []
    for fmt in _video_formats(info)[:limit]:
        label = str(fmt.get("button_label") or fmt.get("label") or "").strip()
        if label:
            lines.append(label)
    audio = info.get("audio") or {}
    audio_size = format_filesize(audio.get("size_bytes")) if audio else None
    lines.append(f"MP3 · {audio_size}" if audio_size else "MP3")
    return lines


def build_card(info: dict) -> str:
    """Render the whole preview card as a MarkdownV2 caption (<= 1024 chars)."""
    headline = card_headline(info)
    meta = card_meta_lines(info)

    # Format lines are the only unbounded section, so they are dropped first
    # when the caption would not fit.
    shown = MAX_FORMAT_BUTTONS
    while True:
        formats = _format_lines(info, limit=shown) if shown else []
        total = len(_video_formats(info))
        header = "Formats:" if total <= shown else f"Formats (top {shown}):"
        body = [escape_md(header)] + [escape_md(line) for line in formats]
        blocks = [headline, "\n".join(meta) if meta else "", "\n".join(body)]
        caption = "\n\n".join(block for block in blocks if block)
        if len(caption) <= CAPTION_BUDGET or shown <= 3:
            break
        shown -= 1

    if len(caption) > CAPTION_BUDGET:
        caption = caption[: CAPTION_BUDGET - 1].rstrip() + "…"
    return caption


# ---------------------------------------------------------------------------
# Inline keyboard
# ---------------------------------------------------------------------------

def build_callback_data(msg_id: int, url_hash: str, action: str) -> str:
    """Compact callback data: ``dl:<message_id>:<url_hash>:<action>``.

    Roughly 30 bytes for a 10-digit message id and a 12-char hash, comfortably
    inside Telegram's 64-byte limit even with a long yt-dlp format id.
    """
    return f"dl:{msg_id}:{url_hash}:{action}"


def callback_data_fits(data: str) -> bool:
    return len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES


def build_format_keyboard(info: dict, msg_id: int, url_hash: str) -> InlineKeyboardMarkup:
    """One button per video format plus MP3, two per row, sizes in the labels."""
    formats = _video_formats(info)
    shown = formats[:MAX_FORMAT_BUTTONS]

    buttons = []
    for fmt in shown:
        label = str(fmt.get("button_label") or fmt.get("label") or "video")
        action = str(fmt.get("id") or "best")
        if not callback_data_fits(build_callback_data(msg_id, url_hash, action)):
            # Fall back to the "best" selector rather than sending broken data.
            action = "best"
        buttons.append(
            InlineKeyboardButton(
                label, callback_data=build_callback_data(msg_id, url_hash, action)
            )
        )

    audio = info.get("audio") or {}
    audio_size = format_filesize(audio.get("size_bytes")) if audio else None
    buttons.append(
        InlineKeyboardButton(
            f"MP3 · {audio_size}" if audio_size else "MP3",
            callback_data=build_callback_data(msg_id, url_hash, "mp3"),
        )
    )

    if len(formats) > MAX_FORMAT_BUTTONS:
        buttons.append(
            InlineKeyboardButton(
                "Best quality",
                callback_data=build_callback_data(msg_id, url_hash, "best"),
            )
        )

    rows = [buttons[i : i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

def sanitize_filename(text: object, limit: int = MAX_TITLE_CHARS) -> str:
    """Strip path separators/control chars, keep Unicode, cap the length."""
    name = "".join(ch for ch in str(text or "") if ch not in _UNSAFE_FILENAME_CHARS)
    name = _CONTROL_CHARS.sub(" ", name)
    name = _WHITESPACE.sub(" ", name).strip(" .")
    if len(name) > limit:
        name = name[: limit - 1].rstrip() + "…"
    return name


def _as_int(value: object) -> int | None:
    """Numeric-or-None helper; yt-dlp returns heights as int, str or None."""
    try:
        parsed = int(float(str(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def build_filename(title: object, ext: str, height: object = None) -> str:
    """``"<Title> [1080p].mp4"`` / ``"<Title>.mp3"``, sanitised and bounded."""
    extension = ext if str(ext).startswith(".") else f".{ext}" if ext else ""
    base = sanitize_filename(title) or "koyura"
    pixels = _as_int(height)
    if pixels:
        suffix = f" [{pixels}p]"
        base = f"{sanitize_filename(base, max(8, MAX_TITLE_CHARS - len(suffix)))}{suffix}"
    return f"{base}{extension.lower()}"


# ---------------------------------------------------------------------------
# Caption under the uploaded file
# ---------------------------------------------------------------------------

def build_file_caption(
    info: dict,
    quality: str | None = None,
    size_bytes: int | None = None,
    duration: object = None,
) -> str:
    """Title, size, duration and quality — replaces the old ``Sent: <title>``."""
    title = str(info.get("title") or "").strip() or "Download"
    if len(title) > 300:
        title = title[:299] + "…"
    facts = [
        format_filesize(size_bytes),
        format_duration(duration if duration is not None else info.get("duration")),
        quality,
    ]
    detail = " · ".join(fact for fact in facts if fact)
    caption = f"*{escape_md(title)}*"
    if detail:
        caption += f"\n{escape_md(detail)}"
    caption += f"\n{escape_md('via Koyura')}"
    return caption[:CAPTION_BUDGET]
