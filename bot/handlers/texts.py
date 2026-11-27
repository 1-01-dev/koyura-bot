"""The bot's voice: thin builders over ``i18n``, plus dashboard-facing constants.

Every word a user reads comes from ``bot/i18n.py``. This module only decides
*how the pieces are assembled* — which lines belong to ``/help``, in what order
``/stats`` prints its counters, how a label and a value are joined.

Two forms exist, and picking the wrong one is the only mistake to avoid here:

* :func:`md` — one catalogued line, **escaped for MarkdownV2**. Use it whenever
  the text is sent with ``parse_mode="MarkdownV2"`` (every message the bot posts,
  including the stage messages).
* :func:`t` — the same lookup **without** escaping. Use it for the card captions
  (``progress_card``/``download_card`` escape their inputs themselves) and for
  plain-text emitters (``query.answer`` toasts, a plain ``edit_message_text``).

The composite builders below (help, welcome, settings, stats, platforms) already
return escaped MarkdownV2, because they are complete messages.

Two things here are deliberately **not** translated:

* :data:`EVENT_*` — the error strings sent to the dashboard. The panel is
  English-only, so these are stable English constants that the operator reads in
  the history table.
* :data:`QUALITY_OPTIONS`, :data:`FORMAT_OPTIONS`, :data:`SUPPORTED_PLATFORMS` —
  protocol values and proper nouns. ``best``/``720``/``video`` are what the
  engine and the commands accept; a translated value would stop working.
"""
from __future__ import annotations

import bot_settings
import i18n
import message_templates
import send_limit
from i18n import DEFAULT_LANGUAGE, escape_md
from i18n import t as _t

# What ``@version`` says in the welcome/help/stats screens. Kept here rather
# than in an environment variable: it is a fact about this build, not a
# deployment knob.
VERSION = "1.0"

# ---------------------------------------------------------------------------
# Protocol values (never translated)
# ---------------------------------------------------------------------------

# The accepted values of /setquality and /setformat, in the order they are
# offered. ``QUALITY_OPTIONS`` is what the user may set, ``AUDIO``/``VIDEO`` is
# what the engine calls the two output kinds.
QUALITY_OPTIONS = ("best", "1080", "720", "480", "360")
FORMAT_OPTIONS = ("video", "audio")

# Site names are proper nouns. The intro line and the "and 1000+ more" tail are
# translated (``platforms.header`` / ``platforms.more``).
SUPPORTED_PLATFORMS = [
    "YouTube", "TikTok", "Instagram", "Twitter/X", "Reddit",
    "Facebook", "Vimeo", "Twitch", "Dailymotion", "SoundCloud",
    "Bandcamp", "Bilibili", "Pinterest", "Tumblr", "Threads",
    "LinkedIn", "Loom", "Streamable",
]


# ---------------------------------------------------------------------------
# Dashboard events (English only — the panel is not translated)
# ---------------------------------------------------------------------------

EVENT_FILE_MISSING = "File not found after download"
EVENT_UPLOAD_FAILED = "Failed to upload to Telegram"
EVENT_SERVICE_DOWN = "Download service temporarily unavailable"
EVENT_TIMED_OUT = "Download timed out"
EVENT_UNKNOWN_ERROR = "Unknown error"


# ---------------------------------------------------------------------------
# Catalogue access
# ---------------------------------------------------------------------------

def t(key: str, lang: object = None, /, **values: object) -> str:
    """A catalogued line as plain text (cards, plain emitters).

    Deliberately **not** templated: the same key is often a label inside a
    larger caption (``card.views``, ``format.video``), and a custom body there
    would be rendered inside another template's output.
    """
    return _t(key, lang, **values)


def md(key: str, lang: object = None, /, **values: object) -> str:
    """A catalogued line escaped for ``parse_mode="MarkdownV2"``.

    A one-line message the owner can override in the panel (the registry keys
    ``message_kinds`` by this i18n key): the custom body wins when it renders,
    the catalogued line is the default.
    """
    return message_templates.render_line(
        key, lang, escape_md(_t(key, lang, **values)), **values
    )


def plain(key: str, lang: object = None, /, **values: object) -> str:
    """A catalogued line for a plain-text emitter (a ``query.answer`` toast)."""
    return message_templates.render_plain_line(
        key, lang, _t(key, lang, **values), **values
    )


def button(key: str, lang: object = None) -> str:
    """A button label, custom template first (``buttons`` category)."""
    return message_templates.render_line(key, lang, _t(key, lang))


# ---------------------------------------------------------------------------
# Names of things
# ---------------------------------------------------------------------------

def language_name(code: object) -> str:
    """``en`` -> ``English``, in the user's own language."""
    key = f"language.{i18n.normalise(code)}"
    return t(key, code)


def format_label(fmt: object, lang: object = None) -> str:
    """``video``/``audio`` as the user reads them; unknown values pass through."""
    name = str(fmt or "")
    key = f"format.{name}"
    return t(key, lang) if i18n.has(key, lang) else name


def mode_label(fmt: object, lang: object = None) -> str:
    """``audio`` -> ``MP3``, ``video`` -> ``MP4`` (the mode armed by a button)."""
    return t("mode.mp3" if str(fmt) == "audio" else "mode.mp4", lang)


# ---------------------------------------------------------------------------
# The welcome screen and the language screen
# ---------------------------------------------------------------------------

def enabled_language_names(lang: object = None) -> str:
    """``English, Русский`` — the languages this deployment offers."""
    settings = bot_settings.cached()
    codes = settings.languages if settings is not None else i18n.LANGUAGES
    return ", ".join(language_name(code) for code in codes)


def platform_names() -> str:
    """``YouTube, TikTok, …`` — the proper nouns, comma separated."""
    return ", ".join(SUPPORTED_PLATFORMS)


def welcome_text(lang: object = None) -> str:
    """The owner's welcome, verbatim, in the user's language."""
    default = escape_md("\n\n".join(
        t(key, lang) for key in ("welcome.title", "welcome.body", "welcome.platforms")
    ))
    return message_templates.render(
        "welcome",
        lang,
        {
            "languages": enabled_language_names(lang),
            "platforms": platform_names(),
            "version": VERSION,
        },
        default,
    )


def language_prompt(lang: object = None) -> str:
    """The first-contact screen: one line asking for the language."""
    default = escape_md(t("language.prompt", lang))
    return message_templates.render(
        "language.prompt", lang, {"languages": enabled_language_names(lang)}, default
    )


def mode_armed(fmt: str, lang: object = None) -> str:
    """The hint after tapping MP3/MP4: what will happen to the next link."""
    return md("mode.armed", lang, mode=mode_label(fmt, lang))


# ---------------------------------------------------------------------------
# The send limit
# ---------------------------------------------------------------------------

def send_limit_label(lang: object = None) -> str:
    """The ceiling as the user reads it: ``2000 MB`` / ``2000 МБ``.

    The unit is catalogued (``limit.unit``) so a Russian user does not read an
    English abbreviation inside a Russian sentence.
    """
    return t("limit.unit", lang, mb=send_limit.MAX_SEND_MB)


def limit_message(key: str, lang: object = None) -> str:
    """A "this cannot be sent" line, MarkdownV2-escaped, with the limit filled in.

    ``key`` is one of ``limit.all_too_big`` / ``limit.video_too_big`` /
    ``limit.audio_too_big``; ``download_card.limit_notice_key`` picks it.
    """
    return md(key, lang, limit=send_limit_label(lang))


def limit_note(quality: str, skipped: str, lang: object = None) -> str:
    """The caption line explaining a quality that was lowered to fit.

    Plain text: the ``done`` caption escapes it as one of its own lines, and the
    owner can override the sentence in the panel.
    """
    return plain(
        "limit.quality_selected", lang, quality=quality, skipped=skipped
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

def _bullets(lang: object, *keys: str) -> str:
    return "\n".join(escape_md(t(key, lang)) for key in keys)


def _heading(lang: object, key: str) -> str:
    return f"*{escape_md(t(key, lang))}*"


def help_text(lang: object = None) -> str:
    """The /help answer, assembled from catalogued lines."""
    return message_templates.render(
        "help",
        lang,
        {"platforms": platform_names(), "version": VERSION},
        _help_default(lang),
    )


def _help_default(lang: object = None) -> str:
    blocks = [
        _heading(lang, "help.title"),
        _bullets(lang, "help.step1", "help.step2", "help.step3", "help.step4"),
        _heading(lang, "help.shortcuts"),
        _bullets(lang, "help.mp3", "help.mp4", "help.best"),
        _heading(lang, "help.preferences"),
        _bullets(lang, "help.setquality", "help.setformat", "help.settings"),
        _heading(lang, "help.other"),
        _bullets(lang, "help.platforms", "help.stats", "help.cancel"),
        escape_md(t("help.footer", lang)),
    ]
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# /platforms
# ---------------------------------------------------------------------------

def platforms_report(lang: object = None) -> str:
    """The /platforms answer: a header and one indented line per platform."""
    lines = [t("platforms.header", lang), ""]
    lines.extend(f"  {name}" for name in SUPPORTED_PLATFORMS)
    lines.append(f"  {t('platforms.more', lang)}")
    default = escape_md("\n".join(lines))
    listing = "\n".join(f"  {name}" for name in SUPPORTED_PLATFORMS)
    return message_templates.render(
        "platforms",
        lang,
        {"platforms": listing, "count": len(SUPPORTED_PLATFORMS)},
        default,
    )


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

def stats_report(
    *,
    uptime_seconds: int,
    downloads: int,
    errors: int,
    cached_files: int,
    disk_mb: float,
    sessions: int,
    lang: object = None,
) -> str:
    """The /stats answer: process uptime and counters, then the volume."""
    hours, remainder = divmod(int(uptime_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime = t(
        "stats.uptime_value", lang, hours=hours, minutes=minutes, seconds=seconds
    )
    rows = (
        ("stats.uptime", uptime),
        ("stats.downloads", downloads),
        ("stats.errors", errors),
        ("stats.cached", cached_files),
        ("stats.disk", f"{disk_mb:.1f} MB"),
        ("stats.sessions", sessions),
    )
    lines = [t("stats.title", lang), ""]
    lines.extend(
        t("stats.line", lang, label=t(label_key, lang), value=value)
        for label_key, value in rows
    )
    default = escape_md("\n".join(lines))
    return message_templates.render(
        "stats",
        lang,
        {
            "uptime": uptime,
            "downloads": downloads,
            "errors": errors,
            "cached": cached_files,
            "disk": f"{disk_mb:.1f} MB",
            "sessions": sessions,
            "version": VERSION,
        },
        default,
    )


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------

def settings_report(
    *,
    quality: str,
    fmt: str,
    language: str,
    mode: str | None = None,
    lang: object = None,
) -> str:
    """The /settings answer: stored defaults, the current language, the mode.

    ``mode`` is the format armed from the welcome keyboard (``audio``/``video``)
    or ``None``; it is shown so a user who tapped MP3 an hour ago can see why the
    next link came back as an MP3.
    """
    next_link = mode_label(mode, lang) if mode else t("settings.mode_none", lang)
    rows = (
        ("settings.quality", quality),
        ("settings.format", format_label(fmt, lang)),
        ("settings.language", language_name(language)),
        ("settings.mode", next_link),
    )
    lines = [t("settings.title", lang), ""]
    lines.extend(
        t("stats.line", lang, label=t(label_key, lang), value=value)
        for label_key, value in rows
    )
    lines.extend([
        "",
        t("settings.change", lang),
        f"  {t('settings.setquality', lang)}",
        f"  {t('settings.setformat', lang)}",
        "",
        t("settings.language_hint", lang),
    ])
    default = escape_md("\n".join(lines))
    return message_templates.render(
        "settings",
        lang,
        {
            "quality": quality,
            "format": format_label(fmt, lang),
            "language": language_name(language),
            "mode": next_link,
        },
        default,
    )


# ---------------------------------------------------------------------------
# Intake notices (one per message, never two in a row)
# ---------------------------------------------------------------------------

def intake_notice(
    limit: int, rejected: bool, truncated: bool, lang: object = None
) -> str | None:
    """The one notice a message gets when links were dropped, or ``None``.

    Both reasons end up in a single reply: two warnings in a row read like two
    different failures.
    """
    parts = []
    if rejected:
        parts.append(md("intake.unsafe", lang))
    if truncated:
        parts.append(md("intake.too_many", lang, limit=limit))
    return "\n".join(parts) if parts else None


__all__ = [
    "DEFAULT_LANGUAGE",
    "EVENT_FILE_MISSING",
    "EVENT_SERVICE_DOWN",
    "EVENT_TIMED_OUT",
    "EVENT_UNKNOWN_ERROR",
    "EVENT_UPLOAD_FAILED",
    "FORMAT_OPTIONS",
    "QUALITY_OPTIONS",
    "SUPPORTED_PLATFORMS",
    "VERSION",
    "button",
    "enabled_language_names",
    "escape_md",
    "format_label",
    "help_text",
    "intake_notice",
    "language_name",
    "language_prompt",
    "limit_message",
    "limit_note",
    "md",
    "mode_armed",
    "mode_label",
    "plain",
    "platform_names",
    "platforms_report",
    "send_limit_label",
    "settings_report",
    "stats_report",
    "t",
    "welcome_text",
]
