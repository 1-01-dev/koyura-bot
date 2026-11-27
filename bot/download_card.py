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
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import message_templates
import send_limit
import transcode_policy as policy

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
    """Video formats, highest quality first (one per height).

    A raw payload may carry several codecs at the same height (``1080p avc1``
    and ``1080p vp9``). Telegram plays H.264 everywhere, so that variant wins
    and the other is dropped here — the card shows **one** 1080p button, and it
    leads to the file that needs no conversion. The engine already collapses
    these (``media_meta.collect_formats``); doing it again keeps the bot correct
    against an older or a stubbed engine, and neither layer hides a whole
    resolution.
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    best: dict[object, dict] = {}
    for fmt in formats:
        height = fmt.get("height") or 0
        current = best.get(height)
        if current is None or _preference(fmt) < _preference(current):
            best[height] = fmt
    return sorted(
        best.values(),
        key=lambda f: (f.get("height") or 0, f.get("fps") or 0),
        reverse=True,
    )


def _preference(fmt: dict) -> tuple:
    """Sort key: playable codec, then a known size, then frame rate."""
    family = policy.codec_family(fmt)
    rank = 0 if policy.is_playable(fmt) else (2 if family else 1)
    return (
        rank,
        0 if policy.size_bytes(fmt) else 1,
        -(fmt.get("fps") or 0),
    )


# ``video_formats`` used to exist twice, under this private alias as well; keep
# the alias so older importers (and the tests that patch it) keep working.
_video_formats = video_formats


def format_size_bytes(fmt: object) -> int | None:
    """A format's reported size in bytes; ``None`` when the engine had none."""
    return send_limit.known_size(fmt)


def audio_size_bytes(info: dict) -> int | None:
    """The MP3 the bot would send: reported size, else bitrate × duration."""
    return send_limit.audio_size(info)


def fits_send_limit(fmt: object, limit_bytes: int | None = None) -> bool:
    """Whether a format's **known** size is inside the Telegram ceiling.

    An unknown size returns ``True``: the engine refuses an oversized file with
    its own ``--max-filesize``, and hiding it here would take away a choice the
    user may well still have (a format's size is often reported only after the
    download starts).
    """
    return send_limit.fits(format_size_bytes(fmt), limit_bytes)


@dataclass(frozen=True)
class FormatOffer:
    """What the keyboard may offer for one ``/api/info`` payload.

    ``formats`` are the sendable video formats, highest quality first;
    ``mp3`` is the audio entry (``None`` when its size is known to be over the
    limit) and ``hidden_videos`` counts what the limit removed, so a caller can
    tell "nothing was offered because everything is too big" apart from "this
    link never had video" without re-doing the filtering.
    """

    formats: list[dict]
    mp3: dict | None
    hidden_videos: int = 0

    @property
    def empty(self) -> bool:
        """Nothing at all can be sent: no video format and no MP3."""
        return not self.formats and self.mp3 is None

    @property
    def all_videos_hidden(self) -> bool:
        """There were video formats, and the limit removed every one of them."""
        return not self.formats and self.hidden_videos > 0

    @property
    def truncated(self) -> bool:
        return len(self.formats) > MAX_FORMAT_BUTTONS


def format_offer(info: dict, *, limit_bytes: int | None = None) -> FormatOffer:
    """Split the engine's formats into what may be offered and what may not.

    The filter runs **before** the ``MAX_FORMAT_BUTTONS`` truncation, so a
    format the bot cannot send never takes a slot away from one it can.
    """
    formats = video_formats(info)
    visible = [fmt for fmt in formats if fits_send_limit(fmt, limit_bytes)]
    audio: dict | None = {"size_bytes": audio_size_bytes(info)}
    if not send_limit.fits(audio["size_bytes"], limit_bytes):
        audio = None
    return FormatOffer(
        formats=visible,
        mp3=audio,
        hidden_videos=len(formats) - len(visible),
    )


def limit_notice_key(info: dict, *, limit_bytes: int | None = None) -> str:
    """Which "this cannot be sent" line explains an empty offer.

    * nothing fits and the MP3 is still available → *try another link or MP3*;
    * no video fits and the MP3 does not either → *this video is too large*;
    * the link had no video at all and the audio is too large → *this audio is
      too large*.
    """
    offer = format_offer(info, limit_bytes=limit_bytes)
    if offer.mp3 is not None:
        return "limit.all_too_big"
    if offer.hidden_videos:
        return "limit.video_too_big"
    return "limit.audio_too_big"


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


def card_values(info: dict, *, url: str | None = None, lang: object = None) -> dict[str, str]:
    """The placeholders a custom ``card`` template may use, as plain strings.

    Values stay **unescaped**: ``message_templates`` escapes them as it
    substitutes, so ``@title`` is safe inside bold markers and a channel name
    with a dot cannot break the caption.
    """
    channel = str(info.get("channel") or info.get("uploader") or "").strip()
    handle = str(info.get("uploader_id") or "").strip()
    if handle and not handle.startswith("@"):
        handle = f"@{handle}"
    if channel and handle and handle.lstrip("@").lower() not in channel.lower():
        channel_line = f"{channel} · {handle}"
    else:
        channel_line = channel or handle
    categories = [
        str(c).strip() for c in (info.get("categories") or []) if str(c or "").strip()
    ]
    source = str(info.get("extractor_key") or info.get("extractor") or "").strip()
    if not source and url:
        source = str(url).split("://", 1)[-1].split("/", 1)[0]
    return {
        "title": str(info.get("title") or "").strip() or t("card.untitled", lang),
        "uploader": str(info.get("uploader") or info.get("channel") or "").strip(),
        "channel": channel_line,
        "followers": format_count(info.get("channel_follower_count")) or "",
        "duration": format_duration(info.get("duration")) or "",
        "views": format_count(info.get("view_count")) or "",
        "likes": format_count(info.get("like_count")) or "",
        "comments": format_count(info.get("comment_count")) or "",
        "category": ", ".join(categories[:2]),
        "uploaded": format_upload_date(info.get("upload_date")) or "",
        "source": source,
        "url": str(url or info.get("webpage_url") or ""),
        "meta": "\n".join(card_meta_lines(info, lang)),
    }


def build_card(info: dict, *, lang: object = None, url: str | None = None) -> str:
    """Render the whole preview card as a MarkdownV2 caption (<= 1024 chars).

    Only metadata is drawn. The formats and their sizes live on the keyboard,
    where they are tappable, so repeating them in the caption spent the caption
    budget on information the user could already see.

    The one exception is the codec warning: when any offered format is one
    Telegram may not play (AV1/VP9), a single explanatory line is added, so a
    button marked ``AV1 ·`` is not a mystery. The line goes last, so it is the
    first thing lost if the caption ever has to be cut.

    The single escaping pass happens here: the headline is escaped with its bold
    markers, the metadata block is escaped as one string, so a title with ``_``
    and a channel with ``.`` are handled by exactly the same rule.
    """
    headline = card_headline(info, lang)
    meta = escape_md("\n".join(card_meta_lines(info, lang)))
    blocks = [headline, meta]
    if has_incompatible_formats(info):
        blocks.append(escape_md(t(policy.note_key(None), lang)))
    caption = "\n\n".join(block for block in blocks if block)
    if len(caption) > CAPTION_BUDGET:
        caption = caption[: CAPTION_BUDGET - 1].rstrip() + "…"
    return message_templates.render(
        "card", lang, card_values(info, url=url, lang=lang), caption, media="caption"
    )


def has_incompatible_formats(info: dict, *, limit_bytes: int | None = None) -> bool:
    """Whether any **sendable** format is a codec Telegram may not play."""
    offer = format_offer(info, limit_bytes=limit_bytes)
    return any(not policy.is_playable(fmt) for fmt in offer.formats)


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
    info: dict,
    msg_id: int,
    url_hash: str,
    *,
    lang: object = None,
    limit_bytes: int | None = None,
) -> InlineKeyboardMarkup | None:
    """One button per **sendable** video format plus MP3, two per row.

    A format Telegram may not play is still offered: it is labelled with its
    codec (``AV1 · 2160p · 2.3GB``, see ``transcode_policy.button_label``) and
    the flow sends it as a document or asks before converting. Nothing is hidden
    to make the keyboard look simple.

    ``None`` means "nothing can be offered": the caller then explains why with
    ``limit_notice_key`` instead of drawing an empty keyboard, which Telegram
    would reject.
    """
    offer = format_offer(info, limit_bytes=limit_bytes)

    buttons = []
    for fmt in offer.formats[:MAX_FORMAT_BUTTONS]:
        label = policy.button_label(fmt)
        action = str(fmt.get("id") or "best")
        if not callback_data_fits(build_callback_data(msg_id, url_hash, action)):
            # Fall back to the "best" selector rather than sending broken data.
            action = "best"
        buttons.append(
            InlineKeyboardButton(
                label, callback_data=build_callback_data(msg_id, url_hash, action)
            )
        )

    if offer.mp3 is not None:
        audio_size = format_filesize(offer.mp3.get("size_bytes"))
        buttons.append(
            InlineKeyboardButton(
                f"MP3 · {audio_size}" if audio_size else "MP3",
                callback_data=build_callback_data(msg_id, url_hash, "mp3"),
            )
        )

    if offer.truncated:
        buttons.append(
            InlineKeyboardButton(
                message_templates.render_line(
                    "card.best_quality", lang, t("card.best_quality", lang)
                ),
                callback_data=build_callback_data(msg_id, url_hash, "best"),
            )
        )

    if not buttons:
        return None

    rows = [buttons[i : i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(rows)


def build_transcode_keyboard(
    fmt: object,
    msg_id: int,
    url_hash: str,
    *,
    lang: object = None,
    eta_seconds: object = None,
) -> InlineKeyboardMarkup:
    """``[Send as file] [Convert (~4 min)]`` for a codec Telegram may not play.

    The download has **not** started when this keyboard is shown: the format's
    codec is known from ``/api/info``, so asking costs nothing. Each button
    carries the format id and the answer in its own ``callback_data``::

        dl:<message_id>:<url_hash>:file:<format_id>
        dl:<message_id>:<url_hash>:conv:<format_id>

    Four fields, no session lookup needed for the action — the session only has
    to confirm the card is still live. A yt-dlp format id is short (``399``,
    ``137``) and the whole string stays far inside Telegram's 64-byte limit; an
    id that would not fit is replaced by ``best`` rather than truncating the
    action, exactly like the format keyboard does.
    """
    entry = fmt if isinstance(fmt, dict) else {}
    action = str(entry.get("id") or "best")
    eta = policy.format_eta(eta_seconds, lang) if eta_seconds else None
    convert_label = (
        message_templates.render_line(
            "transcode.convert", lang, t("transcode.convert", lang, eta=eta), eta=eta
        )
        if eta
        else t("transcode.convert_no_eta", lang)
    )

    buttons = []
    for verb, label in (
        (
            "file",
            message_templates.render_line(
                "transcode.send_as_file", lang, t("transcode.send_as_file", lang)
            ),
        ),
        ("conv", convert_label),
    ):
        data = build_callback_data(msg_id, url_hash, f"{verb}:{action}")
        if not callback_data_fits(data):
            data = build_callback_data(msg_id, url_hash, f"{verb}:best")
        buttons.append(InlineKeyboardButton(label, callback_data=data))
    return InlineKeyboardMarkup([buttons])


def build_transcode_question(
    info: dict,
    fmt: object,
    *,
    lang: object = None,
) -> str:
    """The card caption that asks the question the two buttons answer."""
    entry = fmt if isinstance(fmt, dict) else {}
    headline = card_headline(info, lang)
    question = t(
        "transcode.ask",
        lang,
        quality=policy.quality_label(entry),
        codec=policy.codec_label(entry) or "?",
    )
    note = t(policy.note_key(entry), lang)
    default = _bound("\n\n".join((headline, escape_md(question), escape_md(note))))
    return message_templates.render(
        "transcode.ask",
        lang,
        {
            "title": str(info.get("title") or "").strip() or t("card.untitled", lang),
            "question": question,
            "quality": policy.quality_label(entry),
            "codec": policy.codec_label(entry) or "?",
            "note": note,
        },
        default,
        media="caption",
    )


def _bound(caption: str) -> str:
    if len(caption) > CAPTION_BUDGET:
        return caption[: CAPTION_BUDGET - 1].rstrip() + "…"
    return caption


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
    note: str | None = None,
) -> str:
    """Title, size, duration and quality — replaces the old ``Sent: <title>``.

    ``note`` is an extra line the flow has to explain: today only "the best
    quality did not fit the send limit, so a smaller one was chosen".
    """
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
    if note:
        caption += f"\n{escape_md(note)}"
    caption += f"\n{escape_md(t('card.via', lang))}"
    caption = caption[:CAPTION_BUDGET]
    return message_templates.render(
        "done",
        lang,
        {
            "title": title,
            "quality": quality or "",
            "size": format_filesize(size_bytes) or "",
            "duration": format_duration(
                duration if duration is not None else info.get("duration")
            )
            or "",
            "source": t("card.via", lang),
        },
        caption,
        media="caption",
    )
