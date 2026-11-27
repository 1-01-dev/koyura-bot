"""When — and whether — a downloaded video is turned into H.264.

Telegram never re-encodes what the bot uploads, so what the *site* serves is
what the phone has to play: only MP4/H.264+AAC is guaranteed to work
(tdlib#3054), VP9 in MP4 shows a blurred frame with sound on iOS
(Telegram-iOS#811) and AV1 is passed through untouched (forkgram#487).
Converting on the server is the only way to make those playable — and on two
shared vCPUs it costs ~180% CPU and minutes of waiting, which is exactly the
kind of work that must not happen by default.

So conversion is a **policy**, not a hard-coded step:

``TRANSCODE_MODE=off`` (the default)
    Never convert on the engine's own initiative. The bot sends an incompatible
    file as a document, which always arrives intact.

``TRANSCODE_MODE=auto``
    Convert an incompatible file only when it is small enough to be worth it
    (``TRANSCODE_MAX_MB``, default 300 MB). Above that the file is delivered as
    a document instead of pinning the CPU for ten minutes.

``TRANSCODE_MODE=always``
    The historical behaviour: every AV1/VP9/VP8 file is converted.

A ``force`` request (the user pressed *Convert* in the chat) overrides all three
modes: that is explicit consent, and the caller has already been told what it
costs.

CPU budget
----------
The conversion runs inside the job's queue slot (never in parallel with another
job's transfer *or* another conversion: ``_CONVERSION_LOCK`` serialises them),
with ``-threads 1 -preset veryfast``. x264 at that setting uses one core plus a
little of a second, so a 2 vCPU host stays inside a 90% average-load limit even
while a download runs next to it — see ``docs/configuration.md``.

Nothing here touches the network. ``TranscodeStats`` is the only stateful piece:
an EWMA of measured conversion time, persisted next to the queue EWMA so the
"Convert (~4 min)" button does not have to guess twice.
"""
from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger("koyura.engine.transcode")

MODES = ("off", "auto", "always")
DEFAULT_MODE = "off"
DEFAULT_MAX_MB = 300

MB = 1024.0 * 1024.0

#: One encoder thread. ``veryfast`` at CRF 23 is the knee of the quality/CPU
#: curve for this job: ~1.3 cores, so two concurrent conversions cannot happen
#: (the lock forbids it) and one never eats both vCPUs on its own.
VIDEO_THREADS = 1
FRAME_PRESET = "veryfast"
FRAME_CRF = "23"
AUDIO_BITRATE = "128k"

#: Wall-clock seconds per second of video, used until something was measured.
#: Roughly "half realtime" on two shared vCPUs.
FALLBACK_SPEED_FACTOR = 0.5
#: The owner's rough formula when only the size is known: ~8 MB per minute.
FALLBACK_MB_PER_MINUTE = 8.0

ESTIMATE_MIN_SECONDS = 20.0
ESTIMATE_MAX_SECONDS = 3600.0

#: Why the codec names are grouped: yt-dlp reports ``avc1.640028``,
#: ``vp09.00.40.0``, ``av01.0.08M.0`` and friends, never a bare family name.
CODEC_LABELS = {
    "h264": "H.264",
    "hevc": "HEVC",
    "vp9": "VP9",
    "vp8": "VP8",
    "av1": "AV1",
}

#: Codecs Telegram plays everywhere. Everything else is "send as a document or
#: convert" territory. HEVC is deliberately *not* listed: Android and desktop
#: play it, iOS does not, which is the same trap as VP9.
COMPATIBLE_CODECS = frozenset({"h264"})

#: Codecs the bot is allowed to convert to H.264 (the engine's own encoder).
CONVERTIBLE_CODECS = frozenset({"vp8", "vp9", "av1"})

#: One conversion at a time in this process, whatever the queue's slot count is.
_CONVERSION_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------

def codec_family(vcodec: object) -> str | None:
    """``"avc1.640028"`` -> ``"h264"``; ``None`` when the codec is unknown."""
    codec = str(vcodec or "").strip().lower()
    if not codec or codec == "none":
        return None
    if codec.startswith(("avc", "h264")):
        return "h264"
    if codec.startswith(("hev", "hvc", "h265")):
        return "hevc"
    if codec.startswith(("vp9", "vp09")):
        return "vp9"
    if codec.startswith(("vp8", "vp08")):
        return "vp8"
    if codec.startswith(("av01", "av1")):
        return "av1"
    return None


def codec_label(vcodec: object) -> str | None:
    """``"VP9"`` / ``"AV1"`` — the short name a button can carry."""
    family = codec_family(vcodec)
    return CODEC_LABELS.get(family) if family else None


def is_compatible(vcodec: object) -> bool:
    """Whether Telegram plays this codec as a video everywhere.

    An **unknown** codec counts as compatible: the engine refuses to burn CPU
    on a guess, and the bot keeps sending the file as a video as it always did.
    """
    family = codec_family(vcodec)
    return family is None or family in COMPATIBLE_CODECS


def needs_conversion(vcodec: object) -> bool:
    """Whether converting this codec to H.264 would change anything."""
    return codec_family(vcodec) in CONVERTIBLE_CODECS


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def normalize_mode(raw: object) -> str:
    """One of ``off``/``auto``/``always``; anything else is ``off``.

    A typo must not start a conversion nobody asked for, so the fallback is the
    safe mode rather than a warning-then-convert.
    """
    mode = str(raw or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def mode_from_env() -> str:
    return normalize_mode(os.environ.get("TRANSCODE_MODE", DEFAULT_MODE))


def max_mb_from_env() -> int:
    """``TRANSCODE_MAX_MB`` as a positive whole number of megabytes."""
    raw = os.environ.get("TRANSCODE_MAX_MB", "")
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_MAX_MB
    return value if value > 0 else DEFAULT_MAX_MB


def should_convert(
    vcodec: object,
    *,
    mode: str,
    size_bytes: int | float | None = None,
    max_mb: int = DEFAULT_MAX_MB,
    forced: bool = False,
) -> bool:
    """Whether this file is converted to H.264.

    ``forced`` is the user's explicit "Convert" tap and wins over every mode
    (including ``off``): the button only exists where the cost was explained.
    """
    if not needs_conversion(vcodec):
        return False
    if forced:
        return True
    mode = normalize_mode(mode)
    if mode == "off":
        return False
    if mode == "always":
        return True
    # auto: only when the file is small enough to be worth the CPU.
    if not size_bytes or size_bytes <= 0:
        return False
    return float(size_bytes) <= float(max_mb) * MB


def conversion_reason(
    vcodec: object,
    *,
    mode: str,
    size_bytes: int | float | None = None,
    max_mb: int = DEFAULT_MAX_MB,
    forced: bool = False,
) -> str | None:
    """``None`` when the file converts, otherwise why it is sent as a document."""
    if forced or should_convert(
        vcodec, mode=mode, size_bytes=size_bytes, max_mb=max_mb, forced=forced
    ):
        return None
    if not needs_conversion(vcodec):
        return "compatible"
    if normalize_mode(mode) == "auto" and size_bytes:
        return "too_big"
    return "mode_off"


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def ffmpeg_command(src: str, dst: str) -> list[str]:
    """The exact conversion command.

    ``-threads 1`` and ``-preset veryfast`` are the CPU-budget contract: one
    core per conversion, so a conversion next to a running download stays under
    the host's 90% average-load limit. ``-progress pipe:1 -nostats`` reports
    machine-readable progress to stdout (``out_time_us=…``) instead of the
    human line that would have to be scraped with a regex.
    """
    return [
        "ffmpeg", "-y", "-nostdin",
        "-i", src,
        "-c:v", "libx264",
        "-preset", FRAME_PRESET,
        "-crf", FRAME_CRF,
        "-threads", str(VIDEO_THREADS),
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        dst,
    ]


def parse_progress_line(line: str) -> dict | None:
    """One line of ``-progress pipe:1`` -> ``{"out_time_us": …}`` or ``None``.

    ffmpeg reports ``out_time_us`` on modern builds and ``out_time_ms`` on
    older ones (the latter is microseconds too, a long-standing quirk), so both
    are accepted.
    """
    text = (line or "").strip()
    if "=" not in text:
        return None
    key, _, value = text.partition("=")
    if key not in ("out_time_us", "out_time_ms"):
        return None
    try:
        micros = int(float(value.strip()))
    except (TypeError, ValueError):
        return None
    if micros < 0:
        return None
    return {"out_time_us": micros}


def percent_from(out_time_us: object, duration: object) -> float | None:
    """Conversion percentage from ffmpeg's output position and the duration."""
    try:
        micros = float(out_time_us)  # type: ignore[arg-type]
        total = float(duration)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if micros <= 0 or total <= 0:
        return None
    return max(0.0, min(round(micros / 1_000_000.0 / total * 100.0, 1), 100.0))


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------

def clamp_estimate(seconds: float) -> float:
    return max(ESTIMATE_MIN_SECONDS, min(ESTIMATE_MAX_SECONDS, float(seconds)))


def default_stats_path() -> str:
    """Where the conversion EWMA lives (never inside ``DOWNLOADS_PATH``)."""
    explicit = os.environ.get("TRANSCODE_STATS_PATH", "").strip()
    if explicit:
        return explicit
    state_dir = os.environ.get("KOYURA_STATE_DIR", "").strip() or "/var/lib/koyura"
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        logger.warning(
            "Conversion stats directory %s is not writable; estimates stay in memory",
            state_dir,
        )
        return ""
    return os.path.join(state_dir, "koyura_transcode_stats.json")


class TranscodeStats:
    """EWMA of measured conversion time, like the queue's throughput estimate.

    The stored number is a **speed factor**: wall-clock seconds spent per second
    of video. Multiplying by a clip's duration is the honest estimate — a 4K AV1
    file and a 720p VP9 file of the same length cost about the same, because the
    encoder is the bottleneck, not the file size.

    With no sample yet the owner's rough formula is used instead
    (``size_MB / 8`` minutes), and the flag stays *approximate*.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        alpha: float = 0.3,
        clock=None,
    ) -> None:
        self.path = path
        self.alpha = float(alpha)
        self._lock = threading.Lock()
        self._factor: float | None = None
        self._samples = 0
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            logger.debug("Could not read conversion stats %s", self.path, exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        factor = raw.get("seconds_per_video_second")
        samples = raw.get("samples")
        with self._lock:
            if isinstance(factor, (int, float)) and factor > 0:
                self._factor = float(factor)
            if isinstance(samples, int) and samples > 0:
                self._samples = samples

    def _save_locked(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "seconds_per_video_second": self._factor,
                        "samples": self._samples,
                    },
                    handle,
                    sort_keys=True,
                )
            os.replace(tmp, self.path)
        except OSError:
            logger.debug("Could not persist conversion stats", exc_info=True)

    # -- estimation --------------------------------------------------------

    def estimate(self, size_bytes: object = None, duration: object = None) -> tuple[float, bool]:
        """``(seconds, approximate)`` for converting one file."""
        with self._lock:
            factor = self._factor
            samples = self._samples

        seconds: float | None = None
        if factor is not None:
            try:
                video_seconds = float(duration)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                video_seconds = 0.0
            if video_seconds > 0:
                seconds = factor * video_seconds
        if seconds is None:
            seconds = _fallback_seconds(size_bytes, duration)
            approximate = True
        else:
            approximate = samples < 2
        return clamp_estimate(seconds), approximate

    def record(self, wall_seconds: float, duration: object = None) -> None:
        """Fold one finished conversion into the EWMA (skips nonsense samples)."""
        try:
            wall = float(wall_seconds)
            video = float(duration)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if wall <= 0 or video <= 0:
            return
        sample = wall / video
        with self._lock:
            if self._factor is None:
                self._factor = sample
            else:
                self._factor = self._factor * (1.0 - self.alpha) + sample * self.alpha
            self._samples += 1
            self._save_locked()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "seconds_per_video_second": self._factor,
                "samples": self._samples,
            }


def _fallback_seconds(size_bytes: object, duration: object) -> float:
    """The owner's rough formula: ~8 MB per minute, else half realtime."""
    try:
        size = float(size_bytes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        size = 0.0
    if size > 0:
        return size / MB / FALLBACK_MB_PER_MINUTE * 60.0
    try:
        video = float(duration)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        video = 0.0
    if video > 0:
        return video * FALLBACK_SPEED_FACTOR
    return clamp_estimate(240.0)


def conversion_lock() -> threading.Lock:
    """The process-wide "one conversion at a time" lock."""
    return _CONVERSION_LOCK


__all__ = [
    "AUDIO_BITRATE",
    "CODEC_LABELS",
    "COMPATIBLE_CODECS",
    "CONVERTIBLE_CODECS",
    "DEFAULT_MAX_MB",
    "DEFAULT_MODE",
    "ESTIMATE_MAX_SECONDS",
    "ESTIMATE_MIN_SECONDS",
    "FALLBACK_MB_PER_MINUTE",
    "FALLBACK_SPEED_FACTOR",
    "FRAME_CRF",
    "FRAME_PRESET",
    "MODES",
    "TranscodeStats",
    "VIDEO_THREADS",
    "clamp_estimate",
    "codec_family",
    "codec_label",
    "conversion_lock",
    "conversion_reason",
    "default_stats_path",
    "ffmpeg_command",
    "is_compatible",
    "max_mb_from_env",
    "mode_from_env",
    "needs_conversion",
    "normalize_mode",
    "parse_progress_line",
    "percent_from",
    "should_convert",
]
