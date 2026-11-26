"""Every string the user can see, in one place.

The wording is a product decision, not an implementation detail: a caption that
changes shape between releases is a regression the owner sees in Telegram. So
all of it lives here, next to the list of formats and commands it mentions, and
the rest of the package only ever references names from this module.

Captions built from the ``/api/info`` payload live in ``download_card`` and
``progress_card`` (they are pure formatting over engine data); this module holds
the bot's own voice — answers to commands, status one-liners, refusals.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# /start, /help, /platforms
# ---------------------------------------------------------------------------

START = (
    "Koyura Bot — a quiet downloader.\n\n"
    "Send a video or audio link and the file arrives in this chat.\n\n"
    "Supported platforms: YouTube, TikTok, Instagram, Twitter, Reddit, "
    "and 1000+ more.\n\n"
    "Commands:\n"
    "/help - Help and commands\n"
    "/platforms - Supported platforms\n"
    "/settings - Your preferences\n"
    "/stats - Bot statistics\n"
    "/mp3 <link> - Download directly as MP3\n"
    "/mp4 <link> - Download best quality MP4\n"
)

# MarkdownV2: every special character is escaped in the source, because the
# text is sent with ``parse_mode="MarkdownV2"`` and Telegram rejects unbalanced
# entities. Do not "clean this up".
HELP = (
    "*How to use Koyura Bot:*\n\n"
    "1\\. Send a link \\(YouTube, TikTok, etc\\.\\)\n"
    "2\\. Pick a format \\(MP4 or MP3\\)\n"
    "3\\. Pick quality\n"
    "4\\. The file arrives\n\n"
    "*Shortcuts:*\n"
    "/mp3 \\<link\\> \\- Direct MP3 download\n"
    "/mp4 \\<link\\> \\- Best quality MP4\n"
    "/best \\<link\\> \\- Best available quality\n\n"
    "*Preferences:*\n"
    "/setquality \\<best/720/480\\> \\- Default quality\n"
    "/setformat \\<video/audio\\> \\- Default format\n"
    "/settings \\- View your preferences\n\n"
    "*Other:*\n"
    "/platforms \\- Supported sites\n"
    "/stats \\- Bot stats\n"
    "/cancel \\- Cancel \\(coming soon\\)\n\n"
    "Send several links at once and each one is handled in turn."
)

SUPPORTED_PLATFORMS = [
    "YouTube", "TikTok", "Instagram", "Twitter/X", "Reddit",
    "Facebook", "Vimeo", "Twitch", "Dailymotion", "SoundCloud",
    "Bandcamp", "Bilibili", "Pinterest", "Tumblr", "Threads",
    "LinkedIn", "Loom", "Streamable", "and 1000+ more via yt-dlp",
]

PLATFORMS_HEADER = "Supported platforms:"


def platforms_report() -> str:
    """The /platforms answer: a header and one indented line per platform."""
    return PLATFORMS_HEADER + "\n\n" + "\n".join(f"  {name}" for name in SUPPORTED_PLATFORMS)


# ---------------------------------------------------------------------------
# /stats, /settings, /setquality, /setformat
# ---------------------------------------------------------------------------

def stats_report(
    *,
    uptime_seconds: int,
    downloads: int,
    errors: int,
    cached_files: int,
    disk_mb: float,
    sessions: int,
) -> str:
    """The /stats answer: process uptime and counters, then the volume."""
    hours, remainder = divmod(int(uptime_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"Koyura Bot Stats:\n\n"
        f"  Uptime: {hours}h {minutes}m {seconds}s\n"
        f"  Downloads: {downloads}\n"
        f"  Errors: {errors}\n"
        f"  Cached files: {cached_files}\n"
        f"  Disk usage: {disk_mb:.1f} MB\n"
        f"  Active sessions: {sessions}\n"
    )


def settings_report(quality: str, fmt: str) -> str:
    """The /settings answer: the stored defaults and how to change them."""
    return (
        f"Your preferences:\n\n"
        f"  Default quality: {quality}\n"
        f"  Default format: {fmt}\n\n"
        f"Change:\n"
        f"  /setquality <best/720/480>\n"
        f"  /setformat <video/audio>\n"
    )


# The accepted values of /setquality and /setformat, in the order they are
# offered. ``QUALITY_OPTIONS`` is what the user may set, ``AUDIO``/``VIDEO`` is
# what the engine calls the two output kinds.
QUALITY_OPTIONS = ("best", "1080", "720", "480", "360")
FORMAT_OPTIONS = ("video", "audio")

SETQUALITY_USAGE = "Usage: /setquality <best/1080/720/480/360>"
SETFORMAT_USAGE = "Usage: /setformat <video/audio>"


def invalid_quality() -> str:
    return f"Invalid quality. Options: {', '.join(QUALITY_OPTIONS)}"


def invalid_format() -> str:
    return f"Invalid format. Options: {', '.join(FORMAT_OPTIONS)}"


def quality_saved(quality: str) -> str:
    return f"Default quality set to: {quality}"


def format_saved(fmt: str) -> str:
    return f"Default format set to: {fmt}"


# ---------------------------------------------------------------------------
# /mp3, /mp4, /best
# ---------------------------------------------------------------------------

MP3_USAGE = "Usage: /mp3 <link>\nOr reply to a message containing a link."
MP4_USAGE = "Usage: /mp4 <link>\nOr reply to a message containing a link."
MP3_STARTED = "Downloading MP3..."
MP4_STARTED = "Downloading MP4 (best quality)..."


# ---------------------------------------------------------------------------
# Link intake and the preview card
# ---------------------------------------------------------------------------

FETCHING_INFO = "Fetching info..."
SERVICE_DOWN = "Download service temporarily unavailable."
CARD_RENDER_FAILED = "Could not render this link. Try /mp4 or /mp3 instead."

UNSAFE_LINK = (
    "I can only download from public http(s) links. "
    "That link was skipped because it is not allowed."
)


def too_many_links(limit: int) -> str:
    return f"Only the first {limit} links of a message are processed."


def intake_notice(limit: int, rejected: bool, truncated: bool) -> str | None:
    """The one notice a message gets when links were dropped, or ``None``.

    Both reasons end up in a single reply: two warnings in a row read like two
    different failures.
    """
    parts = []
    if rejected:
        parts.append(UNSAFE_LINK)
    if truncated:
        parts.append(too_many_links(limit))
    return "\n".join(parts) if parts else None


def info_failed(error: object) -> str:
    return f"Failed to fetch info: {error}"


def engine_error(error: object) -> str:
    return f"Error: {error}"


# ---------------------------------------------------------------------------
# Download stages
# ---------------------------------------------------------------------------

SESSION_EXPIRED = "Session expired. Please send the link again."
ALREADY_DOWNLOADING = "Already downloading — wait for the file."
STARTING = "Starting download…"
UNEXPECTED_ANSWER = "Download service returned an unexpected answer."
UNKNOWN_ERROR = "Unknown error"
FILE_MISSING = "File not found after download."
UPLOAD_FAILED = "Failed to upload file to Telegram."
TIMED_OUT = "Download timed out."

# Event messages that are not shown to the user but end up on the dashboard.
EVENT_FILE_MISSING = "File not found after download"
EVENT_UPLOAD_FAILED = "Failed to upload to Telegram"
EVENT_SERVICE_DOWN = "Download service temporarily unavailable"
EVENT_TIMED_OUT = "Download timed out"
EVENT_UNKNOWN_ERROR = "Unknown error"
