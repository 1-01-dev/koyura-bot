"""Pure helpers that turn a yt-dlp JSON blob into the engine's API shape.

Nothing in this module touches the network, the filesystem or Flask: every
function takes plain data and returns plain data, which keeps ``/api/info``
cheap to test and keeps ``app.py`` focused on the job lifecycle.

Fields yt-dlp does not provide for a given site are simply omitted (``None``),
never invented, so the bot can hide the matching line in the preview card.
"""
from __future__ import annotations

import re

import transcode

# Telegram limits a file name to roughly 60 readable characters; longer names
# are cut with an ellipsis so the extension and quality tag stay visible.
MAX_TITLE_CHARS = 60
_UNSAFE_FILENAME_CHARS = frozenset('/\\:*?"<>|')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

# yt-dlp reports the container as ``ext``; only these can be sent to Telegram.
_VIDEO_EXTS = frozenset({"mp4", "webm", "mkv", "mov"})


# ---------------------------------------------------------------------------
# Human readable scalars
# ---------------------------------------------------------------------------

def format_filesize(size: int | float | None) -> str | None:
    """``32.1MB`` / ``768.4KB`` / ``1.2GB``; ``None`` when the size is unknown."""
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


def format_count(count: int | float | None) -> str | None:
    """``1.3M`` / ``110.6K`` / ``942``; ``None`` when the count is unknown."""
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


def format_duration(seconds: int | float | None) -> str | None:
    """``1:02`` / ``1:02:03``; ``None`` when the duration is unknown."""
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


def format_upload_date(raw: object) -> str | None:
    """``20141110`` (yt-dlp) or ``2014-11-10`` -> ``2014-11-10``."""
    if not isinstance(raw, str):
        return None
    digits = raw.strip()
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 10 and digits[4] == "-" and digits[7] == "-":
        return digits
    return None


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------

def _known_size(fmt: dict) -> int | None:
    for key in ("filesize", "filesize_approx", "filesize_estimate"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _codec_rank(vcodec: object) -> int:
    """Lower is better for Telegram: H.264 plays everywhere, AV1 often does not."""
    codec = str(vcodec or "").lower()
    if codec.startswith(("avc", "h264")):
        return 0
    if codec.startswith(("hev", "hvc", "h265")):
        return 1
    if codec.startswith(("vp9", "vp09")):
        return 2
    if codec.startswith(("av01", "av1")):
        return 3
    return 4


def _is_video_format(fmt: dict) -> bool:
    if str(fmt.get("vcodec") or "none") == "none":
        return False
    if not (fmt.get("height") or 0):
        return False
    # yt-dlp exposes storyboards and other non-media artefacts as formats.
    return str(fmt.get("ext") or "").lower() in _VIDEO_EXTS


def _is_audio_format(fmt: dict) -> bool:
    if str(fmt.get("acodec") or "none") == "none":
        return False
    return str(fmt.get("vcodec") or "none") == "none"


def _quality_label(fmt: dict) -> str:
    height = int(fmt.get("height") or 0)
    label = f"{height}p" if height else (fmt.get("format_note") or "video")
    fps = fmt.get("fps")
    if isinstance(fps, (int, float)) and fps >= 50 and height:
        label += f"{int(fps)}"
    return label


def collect_formats(info: dict, estimate=None) -> list[dict]:
    """Best video format per height, highest quality first.

    One entry per resolution: H.264 wins over any other codec of the same
    height, then a known file size (so the button can show it), then bitrate.
    That is why a card offering "1080p" offers the ``avc1`` variant when one
    exists, and never both 1080p variants side by side.

    Formats that Telegram cannot play as a video are **not** hidden: they keep
    an entry, marked with ``codec``/``codec_label`` and ``compatible: false``,
    so the bot can label the button (``AV1 · 2160p · 2.3GB``) and send the file
    as a document instead of losing the quality altogether.

    ``estimate(size_bytes, duration)`` — the engine's conversion-time estimate —
    is only called for formats that would actually need a conversion, so the
    bot's "Convert (~4 min)" button carries a real number instead of a guess.
    """
    best_by_height: dict[int, dict] = {}
    best_score: dict[int, tuple] = {}

    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or not _is_video_format(fmt):
            continue
        height = int(fmt.get("height") or 0)
        size = _known_size(fmt)
        # H.264 first: Telegram plays it without the costly AV1/VP9 transcode,
        # then a known size (so the button can show it), then bitrate.
        score = (
            _codec_rank(fmt.get("vcodec")),
            0 if size else 1,
            -float(fmt.get("tbr") or fmt.get("vbr") or 0),
        )
        if height not in best_score or score < best_score[height]:
            best_score[height] = score
            best_by_height[height] = fmt

    formats: list[dict] = []
    for height in sorted(best_by_height, reverse=True):
        fmt = best_by_height[height]
        size = _known_size(fmt)
        label = _quality_label(fmt)
        vcodec = fmt.get("vcodec")
        family = transcode.codec_family(vcodec)
        convertible = transcode.needs_conversion(vcodec)
        entry = {
            "id": str(fmt.get("format_id")),
            "label": label,
            "height": height,
            "ext": fmt.get("ext"),
            "vcodec": vcodec,
            "fps": fmt.get("fps"),
            "size_bytes": size,
            "size_label": format_filesize(size),
            # Additive codec facts: the bot decides how to send the file from
            # these, never by guessing from the container.
            "codec": family,
            "codec_label": transcode.CODEC_LABELS.get(family) if family else None,
            "compatible": transcode.is_compatible(vcodec),
            "convertible": convertible,
            "transcode_estimate_seconds": None,
            "transcode_estimate_approximate": None,
        }
        if convertible and estimate is not None:
            seconds, approximate = estimate(size, info.get("duration"))
            entry["transcode_estimate_seconds"] = int(round(seconds))
            entry["transcode_estimate_approximate"] = bool(approximate)
        # ``label`` keeps the historical "1080p" shape; ``button_label`` adds the
        # size for the inline keyboard.
        entry["button_label"] = (
            f"{label} · {entry['size_label']}" if entry["size_label"] else label
        )
        formats.append(entry)
    return formats


def best_audio_format(info: dict) -> dict | None:
    """Highest bitrate audio-only stream, used for the MP3 size estimate."""
    best: dict | None = None
    best_bitrate = -1.0
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or not _is_audio_format(fmt):
            continue
        bitrate = float(fmt.get("abr") or fmt.get("tbr") or 0)
        size = _known_size(fmt) or 0
        score = (bitrate, size)
        if score > (best_bitrate, _known_size(best or {}) or 0):
            best = fmt
            best_bitrate = bitrate
    return best


def describe_audio(info: dict) -> dict | None:
    """MP3 metadata for the card: source bitrate and an estimated output size."""
    fmt = best_audio_format(info)
    duration = info.get("duration")
    size = _known_size(fmt or {})
    bitrate = None
    if fmt:
        raw = fmt.get("abr") or fmt.get("tbr")
        bitrate = int(raw) if isinstance(raw, (int, float)) and raw > 0 else None
    if not size and bitrate and isinstance(duration, (int, float)) and duration > 0:
        # MP3 at the source bitrate: bits/second * seconds / 8.
        size = int(bitrate * 1000 / 8 * float(duration))
    if not fmt and not size:
        return None
    return {
        "id": str(fmt.get("format_id")) if fmt else None,
        "ext": fmt.get("ext") if fmt else None,
        "abr": bitrate,
        "size_bytes": size,
        "size_label": format_filesize(size),
    }


# ---------------------------------------------------------------------------
# /api/info payload
# ---------------------------------------------------------------------------

def build_info_payload(
    info: dict, *, transcode_info: dict | None = None, estimate=None
) -> dict:
    """Assemble the additive ``/api/info`` response from a yt-dlp blob.

    ``transcode_info`` is the engine's conversion policy
    (``{"mode", "max_mb"}``) and ``estimate(size_bytes, duration)`` its
    conversion-time model; both come from ``app.py``, which keeps this module
    pure and unit-testable.
    """
    categories = [str(c) for c in (info.get("categories") or []) if c]
    payload = {
        "title": info.get("title") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "uploader_id": info.get("uploader_id") or "",
        "channel": info.get("channel") or "",
        "channel_follower_count": info.get("channel_follower_count"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "categories": categories,
        "upload_date": format_upload_date(info.get("upload_date")),
        "extractor": info.get("extractor") or "",
        "formats": collect_formats(info, estimate=estimate),
        "audio": describe_audio(info),
        "transcode": transcode_info or {},
        # Only present for a link that was substituted: the streaming service's
        # own metadata plus the alternatives the chat offers as buttons.
        "track": info.get("track") or None,
    }
    # Numeric fields yt-dlp may return as floats/None: keep them JSON friendly.
    for key in ("channel_follower_count", "view_count", "like_count", "comment_count"):
        value = payload[key]
        if isinstance(value, (int, float)):
            payload[key] = int(value)
        elif value is not None:
            payload[key] = None
    return payload


def apply_track_metadata(info: dict, track: dict | None) -> dict:
    """Replace a candidate's metadata with the streaming service's own.

    A track resolved from Spotify/Apple Music/Deezer is downloaded from an
    ordinary link (YouTube, usually), but the user asked for the *track*: the
    card, the file name and the audio tags must all come from the service's
    metadata. The alternative would be a card titled
    ``Daft Punk - Get Lucky (Official Audio) ft. …`` with a channel as the
    artist, which is not what the user sent.

    Everything yt-dlp knows about the *file* (formats, sizes, codecs, duration)
    is kept. The title, the artist and the cover are overwritten; the engagement
    counts and the upload date are dropped rather than passed off as the track's
    own numbers.

    The ``track`` block itself travels with the payload: it carries the
    alternatives the chat offers as ``1 · 3:33`` buttons, which is the only
    place they can come from.
    """
    merged = dict(info or {})
    if not isinstance(track, dict):
        return merged
    title = str(track.get("title") or "").strip()
    artist = str(track.get("artist") or "").strip()
    thumbnail = str(track.get("thumbnail") or "").strip()
    if title:
        merged["title"] = title
    if artist:
        # ``uploader``/``channel`` are what the bot reads, the engine's own MP3
        # tagger uses ``uploader`` too: one artist, one field, both agree.
        merged["uploader"] = artist
        merged["channel"] = artist
        merged["uploader_id"] = ""
    if thumbnail:
        merged["thumbnail"] = thumbnail
    for key in (
        "channel_follower_count", "view_count", "like_count", "comment_count",
        "categories", "upload_date",
    ):
        merged.pop(key, None)
    # The source the file came from is an implementation detail of the
    # substitution; keeping it in the payload would let it reach a card.
    merged["extractor"] = ""
    merged["extractor_key"] = ""
    merged.pop("webpage_url", None)
    merged["track"] = track
    return merged


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

def sanitize_filename(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Strip path separators and control characters, keeping Unicode intact."""
    name = "".join(ch for ch in str(text) if ch not in _UNSAFE_FILENAME_CHARS)
    name = _CONTROL_CHARS.sub(" ", name)
    name = _WHITESPACE.sub(" ", name).strip(" .")
    if len(name) > limit:
        name = name[: limit - 1].rstrip() + "…"
    return name


def build_filename(title: str, ext: str, height: int | None = None) -> str:
    """``"<Title> [1080p].mp4"`` / ``"<Title>.mp3"`` (sanitised, ~60 chars)."""
    extension = ext if ext.startswith(".") else f".{ext}" if ext else ""
    base = sanitize_filename(title or "") or "koyura"
    if height:
        suffix = f" [{int(height)}p]"
        # Keep the quality tag and extension inside the readable budget.
        base = sanitize_filename(base, max(8, MAX_TITLE_CHARS - len(suffix)))
        base = f"{base}{suffix}"
    return f"{base}{extension.lower()}"
