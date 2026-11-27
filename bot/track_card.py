"""The preview card and keyboard for a streaming-service track.

Spotify, Apple Music and Deezer cannot be downloaded (DRM), so a link to one of
them is turned into a track that can be: the engine reads the metadata from the
service, searches for the same recording elsewhere and answers with a list of
matching versions. This module renders that list — and nothing else.

Two rules shape the wording, and both are product decisions:

* **the card says nothing about where the audio comes from.** The user sent a
  track; the card shows the artist, the title, the duration and the service's own
  cover. A channel name, a view count or a "found on …" line would break that.
* **every alternative looks the same.** The buttons are numbered and carry the
  duration only (``1 · 3:33``), because a list of channel names is a list of
  leaks: it tells the user which upload each version is.

Pure formatting: an ``/api/info`` payload in, a caption and an
``InlineKeyboardMarkup`` out, so the rules above are unit-testable without
Telegram or a network.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import download_card
import message_templates
from i18n import escape_md, t

CAPTION_BUDGET = download_card.CAPTION_BUDGET
BUTTONS_PER_ROW = download_card.BUTTONS_PER_ROW
CALLBACK_DATA_MAX_BYTES = download_card.CALLBACK_DATA_MAX_BYTES

#: The action prefix of a candidate button: ``dl:<msg>:<hash>:t3`` picks the
#: third alternative. Short on purpose — a 10-digit message id, a 12-char hash
#: and ``t8`` stay far inside Telegram's 64-byte callback limit.
CANDIDATE_PREFIX = "t"


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------

def track_block(info: object) -> dict | None:
    """The ``track`` block of an ``/api/info`` payload, or ``None``.

    ``None`` means the link was not a streaming link: the caller then renders the
    ordinary card. A block with no candidates is *not* ``None`` — it is a track
    that could not be substituted, which is a different thing to say.
    """
    if not isinstance(info, dict):
        return None
    block = info.get("track")
    return block if isinstance(block, dict) else None


def candidates(info: object) -> list[dict]:
    """The alternatives in the payload, in the order the engine ranked them."""
    block = track_block(info) or {}
    entries = block.get("candidates")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("url")]


def is_track(info: object) -> bool:
    """Whether this payload describes a substituted streaming track."""
    return track_block(info) is not None


def artist_of(info: dict) -> str:
    """The track's artist, from the track block (never from the file's channel)."""
    block = track_block(info) or {}
    artist = str(block.get("artist") or "").strip()
    if artist:
        return artist
    return str(info.get("uploader") or info.get("channel") or "").strip()


def title_of(info: dict) -> str:
    """The track's title, from the track block."""
    block = track_block(info) or {}
    return str(block.get("title") or info.get("title") or "").strip()


def duration_of(info: dict) -> object:
    """The track's duration: the service's own figure, else the file's."""
    block = track_block(info) or {}
    return block.get("duration") or info.get("duration")


def candidate_choice(action: object) -> int | None:
    """``"t3"`` -> ``3`` (1-based); ``None`` for any other action."""
    text = str(action or "")
    if not text.startswith(CANDIDATE_PREFIX):
        return None
    try:
        index = int(text[len(CANDIDATE_PREFIX):])
    except ValueError:
        return None
    return index if index >= 1 else None


def candidate_action(index: int) -> str:
    """``3`` -> ``"t3"`` — the callback action for one alternative."""
    return f"{CANDIDATE_PREFIX}{int(index)}"


def pick_candidate(info: dict, index: object) -> dict | None:
    """The alternative a button names, or ``None`` when it is gone."""
    try:
        wanted = int(index)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for position, entry in enumerate(candidates(info), start=1):
        if position == wanted:
            return entry
    return None


def candidate_url(info: dict, index: object) -> str | None:
    """The link the engine must download for one button, or ``None``."""
    entry = pick_candidate(info, index)
    return str(entry.get("url")) if entry else None


# ---------------------------------------------------------------------------
# Caption
# ---------------------------------------------------------------------------

def track_line(info: dict, lang: object = None) -> str:
    """``Artist — Title``, or whichever of the two the service gave.

    One line rather than two: the card is a track, and ``Artist — Title`` is how
    a track is written. A missing artist degrades to the title alone instead of
    leaving a dangling dash.
    """
    artist = artist_of(info)
    title = title_of(info) or t("card.untitled", lang)
    if artist:
        return t("card.track", lang, artist=artist, title=title)
    return title


def card_lines(info: dict, lang: object = None) -> list[str]:
    """The plain (unescaped) lines of the card: the track, then its duration.

    Deliberately minimal. A substituted track has no meaningful view count, no
    channel and no upload date — printing the ones the *file* has would be a
    lie about where the number came from, so they are simply not drawn.
    """
    lines = [track_line(info, lang)]
    duration = download_card.format_duration(duration_of(info))
    if duration:
        lines.append(duration)
    return lines


def build_card(info: dict, *, lang: object = None) -> str:
    """The preview caption for a streaming track (MarkdownV2, <= 1024 chars).

    ``Artist — Title`` in bold, then the running time, and nothing else. The
    service's cover is the photo above it, so the caption never repeats it.

    Rendered through the ``card`` message kind, so an operator who reworded the
    preview card rewords this one too; ``@uploader`` is the artist and
    ``@duration`` the running time, and every other placeholder is empty so its
    line disappears from a custom body.
    """
    artist = artist_of(info)
    title = title_of(info)
    duration = download_card.format_duration(duration_of(info)) or ""
    headline = escape_md(track_line(info, lang))
    lines = [f"*{headline}*"]
    if duration:
        lines.append(escape_md(duration))
    caption = "\n\n".join(lines)
    if len(caption) > CAPTION_BUDGET:
        caption = caption[: CAPTION_BUDGET - 1].rstrip() + "…"
    return message_templates.render(
        "card",
        lang,
        {
            "title": title or t("card.untitled", lang),
            "uploader": artist,
            "channel": artist,
            "duration": duration,
            "followers": "",
            "views": "",
            "likes": "",
            "comments": "",
            "category": "",
            "uploaded": "",
            "source": "",
            "url": "",
            "meta": "\n".join(card_lines(info, lang)[1:]),
        },
        caption,
        media="caption",
    )


def picker_caption(info: dict, *, lang: object = None) -> str:
    """The card that asks which version to download: the track plus the prompt."""
    caption = build_card(info, lang=lang)
    prompt = t("card.pick_track", lang)
    if not prompt:
        return caption
    combined = f"{caption}\n\n{escape_md(prompt)}"
    if len(combined) > CAPTION_BUDGET:
        return caption
    return combined


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

def build_candidate_keyboard(
    info: dict,
    msg_id: int,
    url_hash: str,
    *,
    lang: object = None,
) -> InlineKeyboardMarkup | None:
    """One button per alternative: ``1 · 3:33``, two per row.

    The label is built from the engine's own pre-formatted ``label`` when it has
    one, so the chat and the engine cannot disagree about what ``3:33`` means.
    A candidate without a duration is still offered — as ``2 · ?`` — because a
    hit with no reported length may still be the right recording.

    ``None`` means there is nothing to offer: the caller explains the failure
    instead of drawing an empty keyboard, which Telegram would reject.
    """
    entries = candidates(info)
    if not entries:
        return None
    buttons = []
    for position, entry in enumerate(entries, start=1):
        label = str(entry.get("label") or "").strip() or str(position)
        action = candidate_action(position)
        data = download_card.build_callback_data(msg_id, url_hash, action)
        if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
            # Never send broken callback data: the first alternative still works.
            action = candidate_action(1)
            data = download_card.build_callback_data(msg_id, url_hash, action)
        buttons.append(InlineKeyboardButton(label, callback_data=data))
    rows = [buttons[i : i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(rows)


def candidate_count(info: object) -> int:
    """How many alternatives the payload carries."""
    return len(candidates(info))


__all__ = [
    "CANDIDATE_PREFIX",
    "artist_of",
    "build_candidate_keyboard",
    "build_card",
    "candidate_action",
    "candidate_choice",
    "candidate_count",
    "candidate_url",
    "candidates",
    "card_lines",
    "duration_of",
    "is_track",
    "pick_candidate",
    "picker_caption",
    "title_of",
    "track_block",
    "track_line",
]
