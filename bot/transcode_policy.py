"""What Telegram will do with a format, and what the chat should do about it.

Telegram never re-encodes an upload, so the container the site serves is the
container the phone has to play:

* MP4/H.264 (+AAC) plays everywhere — that is the only guaranteed combination;
* VP9 inside MP4 shows a blurred first frame with sound on iOS, and AV1 is
  passed through untouched, so a phone that cannot decode it shows nothing.

The engine reports those facts per format (``codec``, ``codec_label``,
``compatible``, ``convertible`` and a conversion estimate); this module is the
bot's pure reading of them:

* :func:`is_playable` — may the file go out as a video;
* :func:`button_label` — how the keyboard marks it (``AV1 · 2160p · 2.3GB``);
* :func:`decide` — send as a video, convert, or ask the user first;
* :func:`format_eta` — ``~4 min`` for the "Convert (~4 min)" button.

Everything here is pure and language-aware only through ``i18n``, so the rules
are unit-testable without Telegram, an engine or a clock.
"""
from __future__ import annotations

from i18n import t

# Conversion modes, mirroring ``koyura/transcode.py`` (the engine owns the
# real policy; the bot only needs to know what the operator asked for).
MODE_OFF = "off"
MODE_AUTO = "auto"
MODE_ALWAYS = "always"
MODES = (MODE_OFF, MODE_AUTO, MODE_ALWAYS)
DEFAULT_MODE = MODE_OFF
DEFAULT_MAX_MB = 300

MB = 1024 * 1024

#: What the download flow does with the chosen format.
SEND_VIDEO = "video"   # H.264: straight through as a video message
SEND_FILE = "file"     # a codec the engine cannot convert: a document, no question
CONVERT = "convert"    # the operator allowed it (always / auto inside the limit)
ASK = "ask"            # show "Send as file" / "Convert (~N min)" and wait

#: Short codec names for a button; anything unknown prints no mark at all.
CODEC_LABELS = {
    "h264": "H.264",
    "hevc": "HEVC",
    "vp9": "VP9",
    "vp8": "VP8",
    "av1": "AV1",
}

#: yt-dlp reports ``avc1.640028`` and friends, never a bare family name.
_PREFIXES = (
    ("h264", ("avc", "h264")),
    ("hevc", ("hev", "hvc", "h265")),
    ("vp9", ("vp9", "vp09")),
    ("vp8", ("vp8", "vp08")),
    ("av1", ("av01", "av1")),
)


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------

def codec_family(entry: object) -> str | None:
    """``"av01.0.08M.0"`` -> ``"av1"``; ``None`` for an unknown or absent codec.

    An engine that already answered with ``codec`` wins: the bot never guesses
    twice about the same string.
    """
    if not isinstance(entry, dict):
        return None
    reported = str(entry.get("codec") or "").strip().lower()
    if reported in CODEC_LABELS:
        return reported
    raw = str(entry.get("vcodec") or "").strip().lower()
    if not raw or raw == "none":
        return None
    for family, prefixes in _PREFIXES:
        if raw.startswith(prefixes):
            return family
    return None


def codec_label(entry: object) -> str | None:
    """``AV1`` / ``VP9`` / ``H.264``; ``None`` when the codec is unknown."""
    if isinstance(entry, dict):
        reported = str(entry.get("codec_label") or "").strip()
        if reported:
            return reported
    family = codec_family(entry)
    return CODEC_LABELS.get(family) if family else None


def is_playable(entry: object) -> bool:
    """Whether Telegram plays this as a video everywhere it matters.

    An **unknown** codec counts as playable: the bot keeps sending it as a video
    as it always did rather than inventing a limitation. Only a codec the engine
    positively identified as incompatible changes the flow.
    """
    if not isinstance(entry, dict):
        return True
    reported = entry.get("compatible")
    if isinstance(reported, bool):
        return reported
    family = codec_family(entry)
    return family is None or family == "h264"


def can_convert(entry: object) -> bool:
    """Whether the engine can re-encode this codec into H.264 at all."""
    if not isinstance(entry, dict):
        return False
    reported = entry.get("convertible")
    if isinstance(reported, bool):
        return reported
    return codec_family(entry) in ("vp8", "vp9", "av1")


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------

def estimate_seconds(entry: object) -> int | None:
    """The engine's conversion estimate for this format, in whole seconds."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("transcode_estimate_seconds")
    if isinstance(raw, bool):
        return None
    try:
        seconds = int(float(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def format_eta(seconds: object, lang: object = None) -> str | None:
    """``~40 s`` / ``~4 min`` / ``~25 min`` — coarse, like the queue's wait.

    Minutes round **up**: a conversion that is really 3m20s must not be
    announced as "3 min" and then overrun the promise.
    """
    try:
        total = int(round(float(seconds)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    if total < 90:
        rounded = max(5, int(round(total / 5.0) * 5))
        return t("transcode.eta_seconds", lang, seconds=rounded)
    minutes = max(2, -(-total // 60))
    return t("transcode.eta_minutes", lang, minutes=minutes)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def normalize_mode(raw: object) -> str:
    """One of ``off``/``auto``/``always``; anything unknown is ``off``.

    A missing or misspelt mode must never start an encoder: the server-side
    default is the safe one.
    """
    mode = str(raw or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def engine_mode(info: object) -> str:
    """The mode the engine reported in ``/api/info`` (``transcode.mode``)."""
    if not isinstance(info, dict):
        return DEFAULT_MODE
    policy = info.get("transcode")
    if isinstance(policy, dict):
        return normalize_mode(policy.get("mode"))
    return DEFAULT_MODE


def engine_max_mb(info: object) -> int:
    """``TRANSCODE_MAX_MB`` as the engine reported it, else the documented 300."""
    if isinstance(info, dict):
        policy = info.get("transcode")
        if isinstance(policy, dict):
            try:
                value = int(float(str(policy.get("max_mb")).strip()))
            except (TypeError, ValueError):
                value = DEFAULT_MAX_MB
            if value > 0:
                return value
    return DEFAULT_MAX_MB


def size_bytes(entry: object) -> int | None:
    """The format's reported size, or ``None`` when the engine had no number."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("size_bytes")
    if raw is None:
        raw = entry.get("filesize") or entry.get("filesize_approx")
    try:
        value = int(float(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def decide(
    mode: object,
    entry: object,
    *,
    max_mb: int = DEFAULT_MAX_MB,
    size: object = None,
) -> str:
    """What the flow does with one chosen format.

    * a playable codec (or an entry that is not a format at all) →
      :data:`SEND_VIDEO`, nothing to do;
    * a codec the engine cannot re-encode (HEVC) → :data:`SEND_FILE`: the file
      is delivered as a document instead of as a video with a broken preview;
    * ``always`` → :data:`CONVERT`;
    * ``auto`` and a known size inside ``max_mb`` → :data:`CONVERT`;
    * everything else, including the default ``off`` → :data:`ASK`, because a
      conversion costs a whole core for minutes and only the user may spend it.
    """
    if not isinstance(entry, dict) or is_playable(entry):
        return SEND_VIDEO
    if not can_convert(entry):
        return SEND_FILE

    mode = normalize_mode(mode)
    if mode == MODE_ALWAYS:
        return CONVERT
    if mode == MODE_AUTO:
        known = size_bytes(entry) if size is None else _positive(size)
        if known is not None and known <= float(max_mb) * MB:
            return CONVERT
    return ASK


def _positive(value: object) -> int | None:
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def wants_conversion(mode: object, entry: object, *, max_mb: int = DEFAULT_MAX_MB) -> bool:
    """Whether the engine should convert without asking (``CONVERT``)."""
    return decide(mode, entry, max_mb=max_mb) == CONVERT


def engine_choice(decision: object) -> str:
    """The ``transcode`` value ``/api/download`` expects for a decision."""
    return "force" if decision == CONVERT else "never"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def button_label(entry: object) -> str:
    """The keyboard label, with the codec in front when it matters.

    ``AV1 · 2160p · 2.3GB`` for a format that may not play, ``1080p · 32.1MB``
    for the ones Telegram handles — the mark is a warning, not decoration, so a
    playable H.264 variant keeps the label it always had.
    """
    if not isinstance(entry, dict):
        return "video"
    base = str(entry.get("button_label") or entry.get("label") or "video").strip()
    if is_playable(entry):
        return base
    mark = codec_label(entry)
    if not mark:
        return base
    return f"{mark} · {base}"


def quality_label(entry: object) -> str:
    """``2160p`` for a caption; falls back to the format's own label."""
    if not isinstance(entry, dict):
        return "video"
    label = str(entry.get("label") or "").strip()
    if label:
        return label
    height = _positive(entry.get("height"))
    return f"{height}p" if height else "video"


def note_key(entry: object) -> str:
    """The catalogue key of the one-line explanation for this format."""
    return "transcode.note"


__all__ = [
    "ASK",
    "CODEC_LABELS",
    "CONVERT",
    "DEFAULT_MAX_MB",
    "DEFAULT_MODE",
    "MODE_ALWAYS",
    "MODE_AUTO",
    "MODE_OFF",
    "MODES",
    "SEND_FILE",
    "SEND_VIDEO",
    "button_label",
    "can_convert",
    "codec_family",
    "codec_label",
    "decide",
    "engine_choice",
    "engine_max_mb",
    "engine_mode",
    "estimate_seconds",
    "format_eta",
    "is_playable",
    "note_key",
    "normalize_mode",
    "quality_label",
    "size_bytes",
    "wants_conversion",
]
