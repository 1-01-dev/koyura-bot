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

# The one MarkdownV2 escaper of the bot lives in ``i18n``; it is re-exported here
# because the captions in this module and in ``progress_card`` are built from it.
from i18n import escape_md, t

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
# Formatting helpers
# ---------------------------------------------------------------------------

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


def card_headline(info: dict, lang: object = None) -> str:
    """The card's first line: the title, escaped and bounded."""
    title = str(info.get("title") or "").strip() or t("card.untitled", lang)
    if len(title) > 300:
        title = title[:299] + "…"
    return f"*{escape_md(title)}*"


def card_meta_lines(info: dict, lang: object = None) -> list[str]:
    """Channel / duration / engagement / category / date, minus what's missing.

    The lines come back **plain**: ``build_card`` escapes the whole block once,
    so a label and the value next to it cannot be escaped differently. The labels
    themselves (Duration, Followers, Category, Uploaded) come from the catalogue,
    so a Russian card says what a Russian user expects.
    """
    lines: list[str] = []

    channel = str(info.get("channel") or info.get("uploader") or "").strip()
    handle = str(info.get("uploader_id") or "").strip()
    if handle and not handle.startswith("@"):
        handle = handle if handle.startswith("@") else f"@{handle}"
    if channel and handle and handle.lstrip("@").lower() not in channel.lower():
        lines.append(f"{channel} · {handle}")
    elif channel or handle:
        lines.append(channel or handle)

    duration = format_duration(info.get("duration"))
    if duration:
        lines.append(
            t("card.label_value", lang, label=t("card.duration", lang), value=duration)
        )

    followers = format_count(info.get("channel_follower_count"))
    if followers:
        lines.append(
            t("card.label_value", lang, label=t("card.followers", lang), value=followers)
        )

    engagement = [
        f"{format_count(info[key])} {t(noun, lang)}"
        for key, noun in (
            ("view_count", "card.views"),
            ("like_count", "card.likes"),
            ("comment_count", "card.comments"),
        )
        if format_count(info.get(key))
    ]
    if engagement:
        lines.append(" · ".join(engagement))

    categories = [
        str(c).strip() for c in (info.get("categories") or []) if str(c or "").strip()
    ]
    if categories:
        lines.append(
            t(
                "card.label_value",
                lang,
                label=t("card.category", lang),
                value=", ".join(categories[:2]),
            )
        )

    upload_date = format_upload_date(info.get("upload_date"))
    if upload_date:
        lines.append(
            t("card.label_value", lang, label=t("card.uploaded", lang), value=upload_date)
        )

    return lines


def build_card(info: dict, *, lang: object = None) -> str:
    """Render the whole preview card as a MarkdownV2 caption (<= 1024 chars).

    Only metadata is drawn. The formats and their sizes live on the keyboard,
    where they are tappable, so repeating them in the caption spent the caption
    budget on information the user could already see.

    The single escaping pass happens here: the headline is escaped with its bold
    markers, the metadata block is escaped as one string, so a title with ``_``
    and a channel with ``.`` are handled by exactly the same rule.
    """
    headline = card_headline(info, lang)
    meta = escape_md("\n".join(card_meta_lines(info, lang)))
    caption = "\n\n".join(block for block in (headline, meta) if block)
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


def build_format_keyboard(
    info: dict, msg_id: int, url_hash: str, *, lang: object = None
) -> InlineKeyboardMarkup:
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
                t("card.best_quality", lang),
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
    *,
    lang: object = None,
) -> str:
    """Title, size, duration and quality — replaces the old ``Sent: <title>``."""
    title = str(info.get("title") or "").strip() or t("card.download", lang)
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
    caption += f"\n{escape_md(t('card.via', lang))}"
    return caption[:CAPTION_BUDGET]
