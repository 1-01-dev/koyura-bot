"""Koyura media engine — Flask API used by the Telegram bot and dashboard.

The engine is not published to the host: it only listens on the internal
compose network (or loopback for native runs) and every ``/api/*`` request must
carry the shared ``X-Internal-Token`` header unless ``INTERNAL_API_AUTH_DISABLED``
is explicitly set.

Its address is ``KOYURA_ENGINE_URL`` and its queue state lives in
``KOYURA_STATE_DIR``; see [docs/configuration.md](../docs/configuration.md).
"""
from __future__ import annotations

import contextlib
import glob
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request, send_file

import download_queue
import egress
import media_meta
import streaming
import transcode
from cookies import (
    blocked_message,
    blocked_payload,
    cookie_paths,
    cookie_required_payload,
    looks_blocked,
    looks_like_auth_failure,
    required_message,
    service_for_url,
    with_cookies,
)
from url_safety import UnsafeURLError, validate_url

logger = logging.getLogger("koyura.engine")
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Configuration (all overridable via environment, nothing hard-coded)
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s is below the minimum %s, using %s", name, value, minimum, minimum)
        return minimum
    return value


DOWNLOAD_DIR = os.environ.get(
    "DOWNLOADS_PATH", os.path.join(os.path.dirname(__file__), "downloads")
)
try:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
except OSError as exc:
    raise SystemExit(f"DOWNLOADS_PATH {DOWNLOAD_DIR!r} is not writable: {exc}") from exc

DOWNLOAD_TIMEOUT = _env_int("DOWNLOAD_TIMEOUT", 900, minimum=30)
INFO_TIMEOUT = _env_int("INFO_TIMEOUT", 60, minimum=5)
MAX_FILESIZE_MB = _env_int("MAX_FILESIZE_MB", 2000, minimum=0)
MAX_DURATION_SECONDS = _env_int("MAX_DURATION_SECONDS", 7200, minimum=0)
JOB_TTL_SECONDS = _env_int("JOB_TTL_SECONDS", 3600, minimum=60)

INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()
INTERNAL_API_AUTH_DISABLED = os.environ.get(
    "INTERNAL_API_AUTH_DISABLED", ""
).strip().lower() in {"1", "true", "yes", "on"}

# How long a resolved track (metadata + its alternatives) is reused. One card is
# usually re-rendered once — the bot asks for /api/info, then a job starts
# minutes later — so a short TTL removes the repeat search without ever serving
# a stale choice. Set 0 to disable the cache entirely.
TRACK_CACHE_TTL_SECONDS = _env_int("TRACK_CACHE_TTL_SECONDS", 600, minimum=0)

# Runtime limits: env first, the admin panel may override them through
# ``POST /api/settings`` without a restart (the queue applies them live).
limits = download_queue.Limits.from_env()
estimator = download_queue.Estimator(
    download_queue.default_stats_path(DOWNLOAD_DIR),
    default_mbps=limits.estimate_mbps,
    overhead_seconds=limits.estimate_overhead_seconds,
)

# HLS downloader used for m3u8 sources (which is how YouTube serves video).
#
# ``native`` is the default because it is the only one that reports progress:
# the external ffmpeg downloader never emits progress lines, so a job served
# this way shows no percentage at all for its whole runtime (measured: 1 line
# versus 885 for the same download).
#
# ``hls:ffmpeg`` stays available because it was added to work around X/Twitter
# stalling on fragmented ``twimg`` responses; set ``HLS_DOWNLOADER=ffmpeg`` to
# get that behaviour back at the cost of live progress.
HLS_DOWNLOADER = os.environ.get("HLS_DOWNLOADER", "native").strip().lower()

# Conversion policy: see ``transcode.py``. ``off`` is the default because a
# VP9/AV1 -> H.264 pass costs ~180% CPU on a two-vCPU host and the bot can send
# the same file as a document instead, which arrives intact.
TRANSCODE_MODE = transcode.mode_from_env()
TRANSCODE_MAX_MB = transcode.max_mb_from_env()
transcode_stats = transcode.TranscodeStats(transcode.default_stats_path())

# In-memory job registry. The engine keeps no state on disk; the bot treats a lost
# job (404) as a failed download and the dashboard marks it interrupted.
jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

def _on_queue_expired(job_id: str, message: str) -> None:
    """A job sat in the queue too long: fail it with a readable reason.

    The bot polls ``/api/status`` and forwards the failure to the dashboard as
    a ``download_error`` event, so the panel and the chat agree on the reason.
    """
    _cleanup_partial_files(job_id)
    _job_update(job_id, status="error", error=message, queue_position=None)
    logger.info("Queue job %s expired: %s", job_id, message)


job_queue = download_queue.JobQueue(
    limits,
    lambda job_id: _run_queued_job(job_id),
    estimator=estimator,
    on_expire=_on_queue_expired,
)
# The dispatcher moves queued jobs into freed slots; it is a plain daemon
# thread so nothing has to be shut down explicitly on exit.
job_queue.start()

# The progress line is JSON so the bot can read it without a parser of its own.
# Every field is wrapped in a quoted string with a fallback: yt-dlp renders an
# absent field as the literal ``NA`` (unquoted, which is *not* valid JSON), and
# for HLS/DASH some fields are simply never set. Strings can carry anything, so
# each value is coerced in ``_parse_progress_line``.
#
# ``fragment_index``/``fragment_count`` matter most: YouTube serves video as HLS
# with no ``total_bytes``, and ``total_bytes_estimate`` jumps around wildly while
# the fragment counter only ever goes up.
# The template is passed as ``--progress-template "download:<json>"``. yt-dlp
# treats the ``download:`` part as a *type selector* and consumes it, so the
# lines that actually arrive are the bare JSON objects. ``PROGRESS_PREFIX`` is
# therefore optional when reading: an older deploy may still emit it.
PROGRESS_PREFIX = "download:"
PROGRESS_TEMPLATE = (
    'download:{"downloaded_bytes":"%(progress.downloaded_bytes|)s",'
    '"total_bytes":"%(progress.total_bytes|)s",'
    '"total_bytes_estimate":"%(progress.total_bytes_estimate|)s",'
    '"fragment_index":"%(progress.fragment_index|)s",'
    '"fragment_count":"%(progress.fragment_count|)s",'
    '"speed":"%(progress.speed|)s",'
    '"eta":"%(progress.eta|)s",'
    '"percent_str":"%(progress._percent_str|)s",'
    '"eta_str":"%(progress._eta_str|)s",'
    '"speed_str":"%(progress._speed_str|)s"}'
)

_SAFE_FORMAT_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-.")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_loopback(remote_addr: str | None) -> bool:
    """True when the request comes from the local host (native/loopback use)."""
    if not remote_addr:
        return False
    candidate = remote_addr.strip("[]")
    if candidate.startswith("::ffff:"):
        candidate = candidate[len("::ffff:"):]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _token_matches(provided: str | None) -> bool:
    if not INTERNAL_API_TOKEN:
        return False
    return hmac.compare_digest(provided or "", INTERNAL_API_TOKEN)


def _auth_ok() -> bool:
    if INTERNAL_API_AUTH_DISABLED:
        return True
    if not INTERNAL_API_TOKEN:
        # Fail closed: an unconfigured token must never mean "open to everyone".
        return False
    if _token_matches(request.headers.get("X-Internal-Token")):
        return True
    # The loopback web UI downloads files through a plain <a href>, which cannot
    # carry a header. Only a local operator may pass the token in the query.
    return _is_loopback(request.remote_addr) and _token_matches(
        request.args.get("token")
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the whole process group (yt-dlp + ffmpeg children)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        with contextlib.suppress(OSError):
            proc.kill()


def _run_capture(cmd: list[str], timeout: int) -> tuple[int, str, str, bool]:
    """Run ``cmd`` in its own session, returning (rc, stdout, stderr, timed_out)."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = threading.Event()

    def _on_timeout() -> None:
        timed_out.set()
        _kill_process_group(proc)

    timer = threading.Timer(timeout, _on_timeout)
    timer.daemon = True
    timer.start()
    try:
        stdout, stderr = proc.communicate()
    finally:
        timer.cancel()
        if proc.poll() is None:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Process %s did not exit after SIGKILL", proc.pid)
    return proc.returncode, stdout or "", stderr or "", timed_out.is_set()


def _remove_quietly(path: str) -> None:
    """Best-effort unlink; a missing or locked file must never break a job."""
    try:
        os.remove(path)
    except OSError:
        logger.debug("Could not remove %s", path)


def _cleanup_partial_files(job_id: str) -> None:
    """Remove every partial artifact belonging to a failed job."""
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
        _remove_quietly(path)


def _prune_old_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with _jobs_lock:
        for job_id in [jid for jid, job in jobs.items() if job.get("created", 0) < cutoff]:
            jobs.pop(job_id, None)


def _job_update(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _record_progress(job_id: str, snapshot: dict) -> None:
    """Store a progress snapshot, keeping the percentage monotonic.

    One job can download more than one file: a video+audio job fetches the
    video stream to 100%, then the audio stream starts again at 0%. Reporting
    that reset makes the card jump backwards, so the highest percentage seen
    for the job is kept. ``downloaded_bytes`` and ``speed`` are always the
    latest real values.
    """
    with _jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        percent = snapshot.get("percent")
        previous = (job.get("progress") or {}).get("percent")
        if percent is not None and previous is not None:
            snapshot["percent"] = max(percent, previous)
        elif percent is not None:
            snapshot["percent"] = max(0.0, min(percent, 100.0))
        job["progress"] = snapshot


# Guards the "first malformed progress line" decision, which both the stdout
# and the stderr reader can reach at the same time.
_BAD_LINE_LOCK = threading.Lock()


def _consume_progress_line(job_id: str, line: str, bad_lines: list[int]) -> bool:
    """Record one line of yt-dlp output as progress, if it is progress.

    Returns True when the line was a progress line (usable or not), so the
    caller can decide whether to log it. ``bad_lines`` is a one-element counter
    shared by the stdout and stderr readers, so a malformed line is reported
    once per job instead of once per fragment.
    """
    if not _looks_like_progress(line):
        return False
    payload = _parse_progress_line(line)
    if payload is None:
        with _BAD_LINE_LOCK:
            bad_lines[0] += 1
            first = bad_lines[0] == 1
        if first:
            logger.warning(
                "Job %s: unparsable progress line, progress may be incomplete: %s",
                job_id,
                line[:200],
            )
        else:
            logger.debug("Job %s: another unparsable progress line", job_id)
        return True
    _record_progress(
        job_id,
        _progress_snapshot(payload, elapsed=_job_elapsed(job_id)),
    )
    return True


def _job_elapsed(job_id: str) -> float | None:
    """Seconds since the job started; ``None`` for an unknown job.

    The engine owns this number: yt-dlp's own ``elapsed`` restarts for every
    download inside a job and is missing entirely when ffmpeg does the transfer.
    ``started_at`` is set when a queue slot is granted (so queue wait time is
    not counted as download time); ``created`` covers a job that has not been
    dispatched yet.
    """
    with _jobs_lock:
        job = jobs.get(job_id)
        started = (job.get("started_at") or job.get("created")) if job else None
    if not started:
        return None
    return round(max(0.0, time.time() - float(started)), 1)


def _progress_number(value: object) -> float | int | None:
    """Coerce a yt-dlp progress field to a number (``"NA"``/``None`` -> None).

    yt-dlp renders an absent field as the literal string ``NA``; older progress
    templates also emitted that ``NA`` unquoted, which is not valid JSON. Both
    forms end up here, so anything unparsable is treated as "unknown" instead of
    reaching the bot as a fake value.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL", "UNKNOWN"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


# yt-dlp writes the raw percent as a string like ``"  62.4%"``.
_PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
# ``"3.24MiB/s"`` / ``"  830.81B/s"`` / ``"Unknown"``.
_SPEED_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*([KMGTP]?)i?B/s", re.IGNORECASE
)
_SPEED_FACTORS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4,
                  "P": 1024 ** 5}


def _progress_percent_str(value: object) -> float | None:
    """``"  62.4%"`` -> ``62.4``; ``"NA"``/``"Unknown"`` -> ``None``."""
    if value is None:
        return None
    match = _PERCENT_RE.search(str(value))
    return float(match.group(1)) if match else None


def _progress_speed_str(value: object) -> float | None:
    """``"3.24MiB/s"`` -> bytes per second; anything else -> ``None``."""
    if value is None:
        return None
    match = _SPEED_RE.search(str(value))
    if not match:
        return None
    factor = _SPEED_FACTORS.get(match.group(2).upper() or "", 1)
    return float(match.group(1)) * factor


def _progress_percent(
    *,
    downloaded: float | None,
    total: float | None,
    estimate: float | None,
    fragment_index: float | None,
    fragment_count: float | None,
    percent_str: float | None,
) -> float | None:
    """Best available download percentage, in order of trustworthiness.

    Priority, measured against real yt-dlp runs:

    1. ``total_bytes`` — an exact size; minutes-precise.
    2. ``fragment_index / fragment_count`` — what HLS/DASH (i.e. most YouTube
       video) actually reports. It only ever goes up.
    3. ``total_bytes_estimate`` — a running estimate that *jumps* (observed
       6752208 -> 1688052 on one YouTube HLS job), so a percentage from it goes
       backwards. Used only when nothing better exists.
    4. yt-dlp's own ``_percent_str`` — same estimate caveat, kept as a fallback.

    ``None`` means "no percentage is known": the caller must not invent one.
    """
    if total and downloaded is not None and total > 0:
        return round(min(downloaded / total, 1.0) * 100, 1)
    if fragment_index is not None and fragment_count and fragment_count > 0:
        return round(min(fragment_index / fragment_count, 1.0) * 100, 1)
    if estimate and downloaded is not None and estimate > 0:
        return round(min(downloaded / estimate, 1.0) * 100, 1)
    if percent_str is not None:
        return round(min(max(percent_str, 0.0), 100.0), 1)
    return None


def _looks_like_progress(line: str) -> bool:
    """True for a progress line, with or without the ``download:`` prefix.

    yt-dlp consumes ``download:`` as the template's *type selector* and prints
    the bare JSON object, so matching on the prefix alone discarded every
    progress line and left ``/api/status`` with ``progress: null`` forever.
    """
    probe = line.strip()
    if probe.startswith(PROGRESS_PREFIX):
        probe = probe[len(PROGRESS_PREFIX):].lstrip()
    return probe.startswith("{")


def _parse_progress_line(line: str) -> dict | None:
    """Parse one ``download:`` progress line from yt-dlp.

    Returns ``None`` when the line is not usable. A malformed line (an older
    template that emitted a bare ``NA``, a truncated pipe read) is a normal
    event during a download, so the caller logs it once per job instead of on
    every occurrence.
    """
    payload_text = line.strip()
    if payload_text.startswith(PROGRESS_PREFIX):
        payload_text = payload_text[len(PROGRESS_PREFIX):].strip()
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, ValueError):
        # Last resort: a template that wrote unquoted ``NA`` values. Quote them
        # so the rest of the line (the byte counts) is not thrown away.
        repaired = _NA_TOKEN_RE.sub("null", payload_text)
        if repaired == payload_text:
            return None
        try:
            payload = json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            return None
    return payload if isinstance(payload, dict) else None


# Matches a bare ``NA`` used as a JSON value, e.g. ``"a":NA,`` or ``[NA]``.
# Only the ``NA`` token itself is replaced, so the surrounding punctuation and
# the rest of the line (byte counts, speed) survive.
_NA_TOKEN_RE = re.compile(r"(?<=[:\[,])\s*NA\s*(?=[,}\]])")


def _progress_snapshot(payload: dict, *, elapsed: float | None) -> dict:
    """Turn a parsed yt-dlp progress payload into the engine's progress dict."""
    downloaded = _progress_number(payload.get("downloaded_bytes"))
    total = _progress_number(payload.get("total_bytes"))
    estimate = _progress_number(payload.get("total_bytes_estimate"))
    fragment_index = _progress_number(payload.get("fragment_index"))
    fragment_count = _progress_number(payload.get("fragment_count"))

    speed = _progress_number(payload.get("speed"))
    if speed is None:
        speed = _progress_speed_str(payload.get("speed_str"))
    percent_str = payload.get("percent_str")
    percent = _progress_percent(
        downloaded=downloaded,
        total=total,
        estimate=estimate,
        fragment_index=fragment_index,
        fragment_count=fragment_count,
        percent_str=_progress_percent_str(percent_str),
    )
    # ``elapsed`` is tracked by the engine from the job's start time: yt-dlp's
    # own elapsed resets per download and is absent for ffmpeg-driven transfers.
    return {
        "percent": percent,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "total_bytes_estimate": estimate,
        "speed": speed,
        "eta": _progress_number(payload.get("eta")),
        "elapsed": elapsed,
    }


def _safe_format_id(value: object) -> str | None:
    """Accept only a yt-dlp format id (never an arbitrary CLI argument)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("format_id must be a string")
    value = value.strip()
    if not value or len(value) > 64:
        raise ValueError("invalid format_id")
    if any(ch not in _SAFE_FORMAT_ID for ch in value):
        raise ValueError("invalid format_id")
    return value


def _yt_dlp_cmd(*args: str) -> list[str]:
    """Run yt-dlp through the interpreter that is running this service.

    ``python -m yt_dlp`` does not depend on ``PATH`` containing the virtualenv's
    ``bin`` directory. systemd units start with a minimal PATH, so a bare
    ``yt-dlp`` (or ``ffmpeg`` wrapper installed by the yt-dlp package) is not
    found there even though it works in an interactive shell.
    """
    return [sys.executable, "-m", "yt_dlp", *args]


def _build_info_cmd(
    url: str,
    *,
    cookies_path: str | None = None,
    proxy_url: str = "",
    youtube_client: str | None = None,
) -> list[str]:
    """yt-dlp's argument vector for one metadata lookup.

    The flags are the ones the download path uses, for the same reasons: a
    ``--cookies`` jar when the service has one, and ``--proxy`` when the
    panel's egress rule covers this service. Both are options in the option
    block, so the URL stays last. With neither the command is the plain one
    this endpoint always ran.

    ``youtube_client`` pins the YouTube player clients for a substituted track;
    ``None`` keeps every other lookup on yt-dlp's own choice.
    """
    cmd = _yt_dlp_cmd("--no-playlist", "-j", "--no-warnings")
    if youtube_client:
        cmd += streaming.extractor_args(youtube_client)
    cmd.append(url)
    if cookies_path:
        cmd = with_cookies(cmd, cookies_path)
    if proxy_url:
        cmd = egress.with_proxy(cmd, proxy_url)
    return cmd


# ---------------------------------------------------------------------------
# Streaming links (Spotify, Apple Music, Deezer)
# ---------------------------------------------------------------------------

#: Resolved tracks, keyed by the link that was asked for: ``{url: (at, block)}``.
#: Re-rendering one card otherwise costs another search per format button.
_track_cache: dict[str, tuple[float, dict]] = {}
_track_cache_lock = threading.Lock()


def _track_cached(url: str) -> dict | None:
    """A still-valid resolved track for ``url``, or ``None``."""
    if TRACK_CACHE_TTL_SECONDS <= 0:
        return None
    with _track_cache_lock:
        entry = _track_cache.get(url)
        if entry is None:
            return None
        stored_at, block = entry
        if time.time() - stored_at > TRACK_CACHE_TTL_SECONDS:
            _track_cache.pop(url, None)
            return None
        return block


def _track_remember(url: str, block: dict) -> None:
    """Keep a resolved track for the next card, bounded to avoid unbounded growth."""
    if TRACK_CACHE_TTL_SECONDS <= 0:
        return
    with _track_cache_lock:
        now = time.time()
        for stale in [key for key, (at, _) in _track_cache.items()
                      if now - at > TRACK_CACHE_TTL_SECONDS]:
            _track_cache.pop(stale, None)
        if len(_track_cache) >= 256:
            _track_cache.pop(next(iter(_track_cache)), None)
        _track_cache[url] = (now, block)


def _run_search(provider: str, query: str) -> object:
    """Run one provider search and return its JSON payload (``{}`` on failure).

    A search is a plain ``yt-dlp`` call like every other one this engine makes:
    same interpreter, same option block. ``--flat-playlist`` keeps it to the
    metadata the search already carries, so ten hits cost one request, not ten.
    Timeouts and failures are the caller's business: a provider with no answer
    simply has no candidates.
    """
    cmd = streaming.search_cmd(provider, query, python=sys.executable)
    rc, stdout, stderr, timed_out = _run_capture(cmd, timeout=INFO_TIMEOUT)
    if timed_out or rc != 0 or not stdout.strip():
        logger.info(
            "Track search on %s returned nothing (rc=%s, timed_out=%s): %s",
            provider, rc, timed_out, (stderr or "").strip().splitlines()[-1:],
        )
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        logger.info("Track search on %s returned unparsable JSON", provider)
        return {}


def _track_payload(url: str) -> dict:
    """Metadata and alternatives for a streaming link (cached per link)."""
    cached = _track_cached(url)
    if cached is not None:
        return cached
    block = streaming.resolve_track(url, runner=_run_search)
    if block is None:  # pragma: no cover - the caller checked the link first
        raise streaming.TrackError("track_not_found", "No track was found.")
    _track_remember(url, block)
    return block


def _track_info(url: str) -> dict:
    """The yt-dlp-shaped info blob for the track a candidate points at.

    The candidate is an ordinary link, so the ordinary extraction runs; what
    changes is that the *metadata* is then replaced with the streaming
    service's. The title, the artist and the cover come from Spotify (or Apple
    Music, or Deezer), the formats and the duration from the file — the user
    asked for a track, and the card must describe the track, not the upload that
    happens to carry it.
    """
    track = _track_payload(url)
    candidates = track.get("candidates") or []
    if not candidates:
        raise streaming.TrackError(
            "track_not_found",
            "No matching track was found.",
            service=track.get("service_label") or "",
        )
    chosen = candidates[0]
    target = str(chosen.get("url") or "")

    rc, stdout, stderr, timed_out = _run_capture(
        _build_info_cmd(target, youtube_client=streaming.player_client()),
        timeout=INFO_TIMEOUT,
    )
    if timed_out or rc != 0 or not stdout.strip():
        logger.info(
            "Track %s: the first candidate did not answer (rc=%s, timed_out=%s)",
            track.get("service"), rc, timed_out,
        )
        raise streaming.TrackError(
            "track_not_found",
            "The matching track could not be read.",
            service=track.get("service_label") or "",
        )
    try:
        info = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise streaming.TrackError(
            "track_not_found",
            "The matching track could not be read.",
            service=track.get("service_label") or "",
        ) from exc
    if not isinstance(info, dict):
        raise streaming.TrackError(
            "track_not_found",
            "The matching track could not be read.",
            service=track.get("service_label") or "",
        )
    return media_meta.apply_track_metadata(info, track)


def _download_target(url: str, candidate_url: object = None) -> str:
    """The link a download job should actually fetch.

    A streaming link is never downloadable, so a job for one resolves to the
    candidate the chat picked (``candidate_url``) or the best match. An ordinary
    link is returned untouched.
    """
    if streaming.parse_track_url(url) is None:
        return url
    if candidate_url:
        candidate = validate_url(candidate_url)
        # Only a link this engine itself proposed may be substituted: otherwise
        # a caller could have /api/download fetch any address by naming it.
        known = {
            str(entry.get("url"))
            for entry in (_track_payload(url).get("candidates") or [])
        }
        if candidate not in known:
            raise ValueError("candidate is not one of the offered tracks")
        return candidate
    candidates = _track_payload(url).get("candidates") or []
    if not candidates:
        raise ValueError("no matching track was found")
    return str(candidates[0].get("url"))


def _build_download_cmd(
    job_id: str,
    url: str,
    format_choice: str,
    format_id: str | None,
    *,
    youtube_client: str | None = None,
) -> list[str]:
    """Build the yt-dlp argument vector for a job.

    yt-dlp is invoked as ``<sys.executable> -m yt_dlp`` so the call works in a
    container and in a venv/systemd deployment without relying on PATH.

    ``youtube_client`` pins the YouTube player clients for this one job; a
    substituted track passes the same list its search used. ``None`` keeps the
    plain command every other job gets, so nothing outside the substitution path
    changes behaviour.
    """
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    cmd = _yt_dlp_cmd(
        "--no-playlist",
        "-o", out_template,
        "--progress-template", PROGRESS_TEMPLATE,
        "--force-ipv4",
        "--concurrent-fragments", "4",
        "--socket-timeout", "20",
        "--retries", "5",
        "--fragment-retries", "10",
        "--throttled-rate", "50K",
        "--newline",
    )
    if youtube_client:
        cmd += streaming.extractor_args(youtube_client)
    if HLS_DOWNLOADER and HLS_DOWNLOADER != "native":
        cmd += ["--downloader", f"hls:{HLS_DOWNLOADER}"]

    # Size and duration limits are enforced by yt-dlp itself so oversized
    # downloads are refused before they fill the shared volume.
    if MAX_FILESIZE_MB > 0:
        cmd += ["--max-filesize", f"{MAX_FILESIZE_MB}M"]
        cmd += ["--match-filter", f"filesize < {MAX_FILESIZE_MB}M"]
    if MAX_DURATION_SECONDS > 0:
        cmd += ["--match-filter", f"duration < {MAX_DURATION_SECONDS}"]

    if format_choice == "audio":
        # Without an explicit ``-f``, yt-dlp falls back to ``bv*+ba/b`` and would
        # download the whole video stream before ffmpeg strips the audio: a 4K
        # source meant hundreds of megabytes and minutes of waiting for a file
        # that only needs ~10 MB of audio.
        cmd += [
            "-f", "bestaudio/best",
            "-x", "--audio-format", "mp3",
            "--write-thumbnail", "--convert-thumbnails", "jpg",
        ]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += [
            "-f",
            "bv*[vcodec~='^(avc|h264)']+ba/b[vcodec~='^(avc|h264)']/bv*+ba/b",
            "--merge-output-format", "mp4",
        ]

    cmd.append(url)
    return cmd


# ---------------------------------------------------------------------------
# Download worker
# ---------------------------------------------------------------------------

def run_download(
    job_id: str,
    url: str,
    format_choice: str,
    format_id: str | None,
    *,
    size_hint: int | None = None,
) -> None:
    """Run one job to completion and hand the slot back to the queue.

    ``size_hint`` is only used to sharpen the EWMA sample of a job whose real
    size yt-dlp never reported (``MAX_FILESIZE_MB=0``, or a failed merge).
    """
    started = time.time()
    succeeded = False
    size_bytes: int | None = None
    try:
        _do_download(job_id, url, format_choice, format_id)
        job = jobs.get(job_id) or {}
        succeeded = job.get("status") == "done"
        size_bytes = job.get("file_size_bytes") or size_hint
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        logger.exception("Download job %s crashed", job_id)
        _job_update(job_id, status="error", error=f"Internal error: {exc}")
        _cleanup_partial_files(job_id)
    finally:
        job_queue.finish(
            job_id,
            size_bytes=size_bytes,
            seconds=time.time() - started,
            succeeded=succeeded,
        )


def _run_queued_job(job_id: str) -> None:
    """Queue runner: the job's metadata is already registered in ``jobs``."""
    job = jobs.get(job_id) or {}
    # The dir mtime is a cheap upper bound for the transfer size, which makes
    # the estimate concrete for sites that do not report a filesize up front.
    hint = _dir_size_hint(job_id)
    _job_update(
        job_id,
        status="downloading",
        queue_position=None,
        started_at=time.time(),
        elapsed=0.0,
        progress={},
    )
    run_download(
        job_id,
        str(job.get("url") or ""),
        str(job.get("format") or "video"),
        job.get("format_id"),
        size_hint=hint,
    )


def _dir_size_hint(job_id: str) -> int | None:
    """Bytes of the partial file(s) already on disk for a job (best effort)."""
    total = 0
    try:
        for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
            try:
                if os.path.isfile(path):
                    total += os.path.getsize(path)
            except OSError:
                continue
    except OSError:
        return None
    return total or None


def _do_download(job_id: str, url: str, format_choice: str, format_id: str | None) -> None:
    """Download a job, trying its service's cookie jars in order.

    Without cookies this is one plain attempt, exactly as before. With cookies
    (the operator uploaded at least one jar for this URL's service) the first
    jar is tried, and only an *authorization* failure — yt-dlp saying "sign in
    to confirm", a 403, a private video — moves on to the second. Any other
    failure is final on the first attempt: re-running a broken URL would only
    burn time and confuse the error the user gets.

    The egress rule is resolved once, here, and travels with every attempt: the
    panel's assignment decides whether this job leaves through a proxy, and it
    is the same ``service`` the cookie lookup just used.
    """
    service = service_for_url(url)
    proxy_url = egress.proxy_for_url(url)
    paths = cookie_paths(service) if service else []
    if not paths:
        _attempt_download(
            job_id, url, format_choice, format_id,
            cookies_path=None, proxy_url=proxy_url,
        )
        return

    result = "failed"
    for index, path in enumerate(paths):
        # The jar path is a name, never its content: safe to log.
        logger.info(
            "Job %s: attempt %d/%d with cookies for %s",
            job_id, index + 1, len(paths), service,
        )
        result = _attempt_download(
            job_id, url, format_choice, format_id,
            cookies_path=path, proxy_url=proxy_url,
        )
        if result != "auth":
            return

    # Every jar was refused (or the list turned out empty at the last moment).
    _cleanup_partial_files(job_id)
    _job_update(
        job_id,
        status="error",
        error=required_message(service),
        error_code="cookies_required",
        cookies_service=service,
        cookies_tried=len(paths),
    )
    logger.info(
        "Job %s: cookies_required for %s after %d file(s)",
        job_id, service, len(paths),
    )


def _attempt_download(
    job_id: str,
    url: str,
    format_choice: str,
    format_id: str | None,
    *,
    cookies_path: str | None,
    proxy_url: str | None = None,
) -> str:
    """Run yt-dlp once and return ``done``, ``failed`` or ``auth``.

    ``auth`` means the failure looks like a refused session, so the caller may
    try the next cookie jar; everything else is terminal and already recorded
    on the job. ``proxy_url`` is the egress rule's answer for this service —
    empty for a direct connection — and the URL itself is never logged.

    The YouTube player clients come from the job registry rather than a
    parameter: only a substituted track sets ``youtube_client`` on its job, so
    reading it here keeps every other job on yt-dlp's own choice without
    threading a new argument through four call sites (the ``transcode`` field
    already travels the same way).
    """
    youtube_client = (jobs.get(job_id) or {}).get("youtube_client")
    cmd = _build_download_cmd(
        job_id, url, format_choice, format_id, youtube_client=youtube_client
    )
    if cookies_path:
        cmd = with_cookies(cmd, cookies_path)
    if proxy_url:
        cmd = egress.with_proxy(cmd, proxy_url)
        logger.info("Job %s: leaving through the configured proxy", job_id)
    logger.info("Job %s: starting download", job_id)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        _kill_process_group(process)

    deadline_timer = threading.Timer(DOWNLOAD_TIMEOUT, _kill_on_timeout)
    deadline_timer.daemon = True
    deadline_timer.start()

    stderr_lines: list[str] = []
    stderr_lock = threading.Lock()
    bad_lines = [0]  # one-element counter shared by both reader threads

    def _handle_progress_line(line: str) -> bool:
        """Record one progress line; returns True when it was one."""
        return _consume_progress_line(job_id, line, bad_lines)

    def _read_stderr() -> None:
        """Collect yt-dlp's diagnostics; warnings are also progress candidates."""
        assert process.stderr is not None
        for raw_line in process.stderr:
            line = raw_line.rstrip("\n")
            with stderr_lock:
                stderr_lines.append(line)
                if len(stderr_lines) > 200:
                    del stderr_lines[:100]
            _handle_progress_line(line)

    def _read_stdout() -> None:
        """Read the stream yt-dlp actually writes progress to.

        yt-dlp prints ``[download] ...`` and the progress template to *stdout*;
        stderr only carries warnings and errors. Draining stdout therefore threw
        away every progress update, which is why ``/api/status`` reported
        ``progress: null`` for whole jobs even when the template was correct.
        """
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if not _handle_progress_line(line):
                logger.debug("Job %s: %s", job_id, line[:200])

    stderr_reader = threading.Thread(target=_read_stderr, name=f"ytdlp-err-{job_id}", daemon=True)
    stderr_reader.start()

    stdout_reader = threading.Thread(target=_read_stdout, name=f"ytdlp-out-{job_id}", daemon=True)
    stdout_reader.start()

    try:
        returncode = process.wait()
    finally:
        deadline_timer.cancel()
        if process.poll() is None:
            _kill_process_group(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("Job %s: process ignored SIGKILL", job_id)
        stderr_reader.join(timeout=5)
        stdout_reader.join(timeout=5)

    if timed_out.is_set():
        _cleanup_partial_files(job_id)
        _job_update(
            job_id,
            status="error",
            error=f"Download timed out ({DOWNLOAD_TIMEOUT // 60} min limit)",
        )
        logger.info("Job %s: timed out after %ss", job_id, DOWNLOAD_TIMEOUT)
        return "failed"

    with stderr_lock:
        last_stderr = stderr_lines[-1] if stderr_lines else ""

    if returncode != 0:
        _cleanup_partial_files(job_id)
        # A *blocked address* is checked first and is terminal: the same 403
        # would greet the next cookie jar, so retrying only costs a minute and
        # ends in the wrong advice ("add cookies") for a problem cookies cannot
        # fix. The job carries its own code and sentence instead.
        joined = "\n".join(stderr_lines[-20:])
        if looks_blocked(joined):
            service = service_for_url(url)
            _job_update(
                job_id,
                status="error",
                error=blocked_message(service),
                error_code="service_blocked",
                cookies_service=service,
            )
            logger.info(
                "Job %s: %s is refusing this IP (proxy needed)",
                job_id, service or "the site",
            )
            return "blocked"
        # A refused session is the one failure worth retrying with another jar;
        # the job stays ``downloading`` so the next attempt owns the outcome.
        if cookies_path and looks_like_auth_failure(joined):
            logger.info("Job %s: cookie file refused, trying the next one", job_id)
            return "auth"
        _job_update(job_id, status="error", error=last_stderr or "Unknown error")
        logger.info("Job %s: yt-dlp exited with %s", job_id, returncode)
        return "failed"

    files = sorted(glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")))
    if not files:
        _job_update(job_id, status="error", error="Download completed but no file was found")
        return "failed"

    if format_choice == "audio":
        target = [f for f in files if f.endswith(".mp3")]
    else:
        target = [f for f in files if f.endswith(".mp4")]
    chosen = target[0] if target else files[0]

    # The thumbnail sidecar is input for ``_tag_audio``, not a leftover.
    cover = next(
        (path for path in (
            os.path.splitext(chosen)[0] + ".jpg",
            os.path.splitext(chosen)[0] + ".webp",
            os.path.splitext(chosen)[0] + ".png",
        ) if os.path.exists(path)),
        None,
    ) if format_choice == "audio" else None

    for other in files:
        if other not in (chosen, cover):
            _remove_quietly(other)

    if MAX_FILESIZE_MB > 0:
        try:
            size = os.path.getsize(chosen)
        except OSError:
            size = 0
        if size > MAX_FILESIZE_MB * 1024 * 1024:
            _cleanup_partial_files(job_id)
            _job_update(
                job_id,
                status="error",
                error=f"File is larger than the {MAX_FILESIZE_MB} MB limit",
            )
            return "failed"

    if chosen.endswith(".mp4"):
        job = jobs.get(job_id, {}) or {}
        _make_telegram_compatible(
            chosen,
            job_id=job_id,
            force=str(job.get("transcode") or "") == "force",
        )
    elif chosen.endswith(".mp3"):
        _tag_audio(chosen, jobs.get(job_id, {}) or {})

    probe: dict = {}
    try:
        probe = _probe_media(chosen)
        _job_update(
            job_id,
            width=probe.get("width"),
            height=probe.get("height"),
            duration=probe.get("duration"),
        )
    except Exception:
        logger.debug("Job %s: media probe failed", job_id, exc_info=True)

    ext = os.path.splitext(chosen)[1]
    job = jobs.get(job_id, {}) or {}
    title = str(job.get("title") or "").strip()
    if title:
        # An MP3's probed "height" is the cover art, not a video resolution.
        height = probe.get("height") if chosen.endswith(".mp4") else None
        filename = media_meta.build_filename(title, ext, height)
    else:
        filename = os.path.basename(chosen)

    try:
        final_size = os.path.getsize(chosen)
    except OSError:
        final_size = None

    _job_update(
        job_id,
        status="done",
        file=chosen,
        file_path=os.path.abspath(chosen),
        filename=filename,
        file_size_bytes=final_size,
        error=None,
    )
    logger.info("Job %s: finished (%s)", job_id, filename)
    return "done"


def _probe_media(path: str) -> dict:
    rc, stdout, _, _ = _run_capture(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", path,
        ],
        timeout=15,
    )
    if rc != 0:
        return {}
    try:
        info = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {}
    stream = (info.get("streams") or [{}])[0]
    fmt = info.get("format") or {}
    duration = fmt.get("duration")
    return {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "duration": float(duration) if duration else None,
    }


def _tag_audio(chosen: str, job: dict) -> None:
    """Write title/artist/cover tags into the extracted MP3.

    yt-dlp already tags whatever the site provides, but its ``title`` is often
    the site's internal slug and only the bot knows the channel name, so the
    caller-supplied values win. The cover is the ``--write-thumbnail``
    sidecar, which is unlinked afterwards.
    """
    title = str(job.get("title") or "").strip()
    artist = str(job.get("uploader") or job.get("channel") or "").strip()
    stem = os.path.splitext(chosen)[0]
    cover = next(
        (path for path in (stem + ".jpg", stem + ".webp", stem + ".png")
         if os.path.exists(path)),
        None,
    )
    if not title and not artist and not cover:
        return

    tagged = chosen + ".tagged.mp3"
    cmd = ["ffmpeg", "-y", "-i", chosen]
    if cover:
        cmd += ["-i", cover, "-map", "0:a", "-map", "1:v"]
        cmd += ["-c:a", "copy", "-c:v", "mjpeg", "-id3v2_version", "3"]
        cmd += ["-disposition:v", "attached_pic"]
        cmd += ["-metadata:s:v", "title=Album cover"]
        cmd += ["-metadata:s:v", "comment=Cover (front)"]
    else:
        cmd += ["-map", "0:a", "-c:a", "copy", "-id3v2_version", "3"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}", "-metadata", f"album_artist={artist}"]
    cmd.append(tagged)

    rc, _, _, timed_out = _run_capture(cmd, timeout=120)
    if timed_out:
        logger.warning("Tagging timed out for %s", chosen)
    if rc == 0 and os.path.exists(tagged) and os.path.getsize(tagged) > 0:
        os.replace(tagged, chosen)
    elif os.path.exists(tagged):
        _remove_quietly(tagged)
    if cover:
        _remove_quietly(cover)


def _probe_vcodec(path: str) -> str:
    """The first video stream's codec name (``h264``/``vp9``/``av1``), or ""."""
    rc, stdout, _, _ = _run_capture(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ],
        timeout=15,
    )
    return (stdout or "").strip().lower() if rc == 0 else ""


def _convert_to_h264(job_id: str, chosen: str, duration: float | None) -> bool:
    """Run the one CPU-bounded x264 pass; True when it replaced the original.

    Serialised by ``transcode.conversion_lock`` so two jobs can never convert at
    the same time whatever the queue's slot count is, and ``-threads 1`` keeps
    the pass inside a single core — the host's load average must stay under its
    90% limit even while another download runs.

    ``-progress pipe:1`` writes ``out_time_us=…`` lines to stdout, which is how
    ``/api/status`` reports the ``converting`` stage's percentage; stderr is
    drained by the same call so ffmpeg can never block on a full pipe.
    """
    transcoded = chosen + ".h264.mp4"
    cmd = transcode.ffmpeg_command(chosen, transcoded)
    started = time.time()
    percent = 0.0

    with transcode.conversion_lock():
        _job_update(job_id, stage="converting", stage_updated_at=started)
        logger.info("Job %s: converting to H.264 (threads=%s)",
                    job_id, transcode.VIDEO_THREADS)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            _kill_process_group(process)

        # The conversion shares the job's hard deadline: a stuck encoder must
        # not hold the queue slot for ever.
        timer = threading.Timer(max(120, DOWNLOAD_TIMEOUT // 3), _kill_on_timeout)
        timer.daemon = True
        timer.start()

        try:
            if process.stdout is None:
                process.wait()
            else:
                for raw_line in process.stdout:
                    payload = transcode.parse_progress_line(raw_line)
                    if payload is None:
                        logger.debug("Job %s: ffmpeg %s", job_id, raw_line.strip()[:200])
                        continue
                    current = transcode.percent_from(payload["out_time_us"], duration)
                    if current is not None and current >= percent:
                        percent = current
                        _job_update(
                            job_id,
                            progress={
                                "percent": percent,
                                "stage": "converting",
                                "elapsed": round(time.time() - started, 1),
                            },
                        )
                process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                _kill_process_group(process)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            if process.stderr is not None:
                with contextlib.suppress(Exception):
                    process.stderr.read()

        elapsed = time.time() - started
        if timed_out.is_set():
            logger.warning("Job %s: conversion timed out", job_id)
        if process.returncode == 0 and os.path.exists(transcoded) and os.path.getsize(transcoded) > 0:
            os.replace(transcoded, chosen)
            transcode_stats.record(elapsed, duration)
            _job_update(
                job_id,
                # The delivered bytes are H.264 now, whatever the user picked:
                # the bot must send a video, not a document.
                vcodec="h264",
                converted=True,
                send_as_document=False,
                stage=None,
                stage_updated_at=None,
            )
            return True

    if os.path.exists(transcoded):
        _remove_quietly(transcoded)
    _job_update(job_id, converted=False, stage=None, stage_updated_at=None)
    logger.warning("Job %s: conversion failed, keeping the original file", job_id)
    return False


def _faststart(chosen: str) -> None:
    """Move the moov atom to the front so Telegram can stream the MP4."""
    faststart_tmp = chosen + ".fs.mp4"
    rc, _, _, _ = _run_capture(
        ["ffmpeg", "-y", "-i", chosen, "-c", "copy", "-movflags", "+faststart", faststart_tmp],
        timeout=120,
    )
    if rc == 0 and os.path.exists(faststart_tmp) and os.path.getsize(faststart_tmp) > 0:
        os.replace(faststart_tmp, chosen)
    elif os.path.exists(faststart_tmp):
        _remove_quietly(faststart_tmp)


def _make_telegram_compatible(
    chosen: str,
    *,
    job_id: str | None = None,
    force: bool = False,
    duration: float | None = None,
) -> bool:
    """Apply the conversion policy, otherwise just move the moov atom.

    Returns True when the file was converted (so the caller can record that the
    delivered bytes are H.264 whatever the user picked). ``force`` is the user's
    explicit *Convert* tap and overrides ``TRANSCODE_MODE``; without it the mode
    decides, and ``off`` (the default) never converts on its own.

    A conversion that the policy does **not** ask for is not silent: the job
    keeps its codec so ``/api/status`` can tell the bot to send a document
    instead of a video with a broken preview.
    """
    vcodec = _probe_vcodec(chosen)
    try:
        size = os.path.getsize(chosen)
    except OSError:
        size = None

    if transcode.should_convert(
        vcodec,
        mode=TRANSCODE_MODE,
        size_bytes=size,
        max_mb=TRANSCODE_MAX_MB,
        forced=force,
    ):
        if job_id is None:
            logger.info(
                "Converting %s outside a job (no progress reported)",
                os.path.basename(chosen),
            )
            return _convert_untracked(chosen, duration)
        if duration is None:
            # ffmpeg's ``out_time_us`` only becomes a percentage with a total.
            duration = _probe_media(chosen).get("duration")
        return _convert_to_h264(job_id, chosen, duration)

    reason = transcode.conversion_reason(
        vcodec,
        mode=TRANSCODE_MODE,
        size_bytes=size,
        max_mb=TRANSCODE_MAX_MB,
        forced=force,
    )
    if job_id:
        _job_update(
            job_id,
            vcodec=transcode.codec_family(vcodec) or vcodec or None,
            converted=False,
            send_as_document=bool(reason and reason != "compatible"),
        )
    if reason and reason != "compatible":
        logger.info(
            "Leaving %s as %s (%s, mode=%s, %s MB)",
            os.path.basename(chosen), vcodec or "unknown", reason,
            TRANSCODE_MODE, round((size or 0) / transcode.MB, 1),
        )

    _faststart(chosen)
    return False


def _convert_untracked(chosen: str, duration: float | None) -> bool:
    """Convert a file that belongs to no job (the loader's direct path)."""
    transcoded = chosen + ".h264.mp4"
    started = time.time()
    with transcode.conversion_lock():
        rc, _, _, timed_out = _run_capture(
            transcode.ffmpeg_command(chosen, transcoded),
            timeout=max(120, DOWNLOAD_TIMEOUT // 3),
        )
    if timed_out:
        logger.warning("Conversion timed out for %s", chosen)
    if rc == 0 and os.path.exists(transcoded) and os.path.getsize(transcoded) > 0:
        os.replace(transcoded, chosen)
        transcode_stats.record(time.time() - started, duration)
        return True
    if os.path.exists(transcoded):
        _remove_quietly(transcoded)
    return False


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

@app.before_request
def _enforce_internal_auth():
    """Reject unauthenticated API calls (401) before they reach a view."""
    if not request.path.startswith("/api/"):
        return None
    if _auth_ok():
        return None
    if not INTERNAL_API_TOKEN and not INTERNAL_API_AUTH_DISABLED:
        return jsonify({"error": "Internal API token is not configured"}), 503
    return jsonify({"error": "Unauthorized"}), 401


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    # Only a loopback operator (native run or published 127.0.0.1 port) gets the
    # token injected into the page; other containers receive an empty value.
    ui_token = ""
    if not INTERNAL_API_AUTH_DISABLED and _is_loopback(request.remote_addr):
        ui_token = INTERNAL_API_TOKEN
    return render_template("index.html", api_token=ui_token)


@app.route("/api/info", methods=["POST"])
def get_info():
    """Metadata for one URL, trying the service's cookie jars in order.

    The download path has always retried with the next jar; the preview did
    not, so a service that answers "account authentication is required" before
    any download starts (Reddit) failed on the card the user never got, with a
    cookie file sitting in the panel. The jars, their order and the retry rule
    are therefore the same here: the panel's first file first, and only an
    authorization failure moves on to the second. The egress rule travels with
    every attempt, exactly as it does for a download.
    """
    data = request.get_json(silent=True) or {}
    try:
        url = validate_url(data.get("url"))
    except UnsafeURLError as exc:
        return jsonify({"error": str(exc)}), 400

    # A streaming link is not a download source at all — yt-dlp refuses those
    # sites by design — so it takes a different path: read the track's metadata
    # from the service, find the same recording where it *can* be fetched, and
    # answer with the ordinary payload shaped around that recording.
    try:
        if streaming.parse_track_url(url) is not None:
            info = _track_info(url)
            return jsonify(
                media_meta.build_info_payload(
                    info,
                    transcode_info={"mode": TRANSCODE_MODE, "max_mb": TRANSCODE_MAX_MB},
                    estimate=transcode_stats.estimate,
                )
            )
    except streaming.TrackError as exc:
        logger.info("Info: %s for %s (%s)", exc.code, exc.service or "track", exc.message)
        return jsonify(exc.payload()), 400
    except UnsafeURLError as exc:
        return jsonify({"error": str(exc)}), 400

    service = service_for_url(url)
    proxy_url = egress.proxy_for_url(url)
    paths = cookie_paths(service) if service else []
    # With no jar the endpoint makes one plain attempt, as it always did; the
    # loop shape only exists so the retry order has a single owner.
    attempts: list[str | None] = list(paths) or [None]
    tried = 0
    rc, stdout, stderr = 0, "", ""
    for cookies_path in attempts:
        if cookies_path:
            tried += 1
            # The jar path is a name, never its content: safe to log.
            logger.info(
                "Info: attempt %d/%d with cookies for %s",
                tried, len(paths), service,
            )
        rc, stdout, stderr, timed_out = _run_capture(
            _build_info_cmd(url, cookies_path=cookies_path, proxy_url=proxy_url),
            timeout=INFO_TIMEOUT,
        )
        if timed_out:
            return jsonify({"error": "Timed out fetching video info"}), 400
        if rc == 0:
            break
        # A refused *address* is terminal here too: another jar answers the
        # same 403, and "add cookies" would be the wrong advice for it.
        if service and looks_blocked(stderr):
            logger.info("Info: %s is refusing this IP (proxy needed)", service)
            return jsonify(blocked_payload(service)), 400
        # Only a refused session is worth the next jar.
        if not (cookies_path and looks_like_auth_failure(stderr)):
            break
    else:
        # Every jar was refused: the operator has to add a working one, and the
        # bot turns the code into "this service needs cookies".
        logger.info("Info: cookies_required for %s after %d file(s)", service, tried)
        return jsonify(cookie_required_payload(service, tried)), 400

    if rc != 0:
        # A service with no jar at all gets the same answer — add one — instead
        # of a raw yt-dlp line: the operator can fix it, and the bot says so.
        if service and looks_like_auth_failure(stderr):
            return jsonify(cookie_required_payload(service, tried)), 400
        message = (stderr or "").strip().split("\n")[-1] if stderr else "Unknown error"
        return jsonify({"error": message}), 400

    try:
        info = json.loads(stdout)
    except json.JSONDecodeError:
        return jsonify({"error": "Could not parse video info"}), 400

    # The response shape lives in media_meta so it can be unit-tested and so
    # the bot's card and the engine agree on every field name. The conversion
    # policy and its estimate travel with it: the bot only has to render them.
    return jsonify(
        media_meta.build_info_payload(
            info,
            transcode_info={"mode": TRANSCODE_MODE, "max_mb": TRANSCODE_MAX_MB},
            estimate=transcode_stats.estimate,
        )
    )


def _refusal(admission: download_queue.Admission):
    """HTTP 429 body for a refused job (additive: old keys are unchanged)."""
    retry_after = int(admission.wait_seconds or 1)
    payload = {
        "error": admission.error,
        "code": admission.code,
        "queue_position": None,
        "wait_seconds": admission.wait_seconds,
        "retry_after": retry_after,
        "queue_length": admission.queue_length,
    }
    response = jsonify(payload)
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    try:
        url = validate_url(data.get("url"))
    except UnsafeURLError as exc:
        return jsonify({"error": str(exc)}), 400

    format_choice = data.get("format", "video")
    if format_choice not in ("video", "audio"):
        return jsonify({"error": "format must be 'video' or 'audio'"}), 400

    try:
        format_id = _safe_format_id(data.get("format_id"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    title = str(data.get("title") or "")[:300]
    uploader = str(data.get("uploader") or data.get("channel") or "")[:200]
    height_hint = data.get("height")
    try:
        height_hint = int(height_hint) if height_hint else None
    except (TypeError, ValueError):
        height_hint = None

    # A streaming link is never the file: the job is pointed at the candidate
    # the chat picked instead. The source link is kept for the record, so the
    # dashboard shows what the user actually sent.
    source_url = url
    is_track = streaming.parse_track_url(url) is not None
    try:
        url = _download_target(url, data.get("candidate_url"))
    except streaming.TrackError as exc:
        return jsonify(exc.payload()), 400
    except (ValueError, UnsafeURLError) as exc:
        return jsonify({"error": str(exc)}), 400

    user_id = _optional_int(data.get("user_id"))
    size_hint = _optional_int(
        data.get("size_bytes") or data.get("filesize") or data.get("filesize_approx")
    )

    # The chat's explicit "Convert" tap. It overrides ``TRANSCODE_MODE`` (which
    # may be ``off``): the button is only drawn where the cost was explained.
    transcode_choice = str(data.get("transcode") or "").strip().lower()
    if transcode_choice not in ("", "force", "never"):
        return jsonify({"error": "transcode must be 'force' or 'never'"}), 400
    force_transcode = transcode_choice == "force"

    _prune_old_jobs()

    job_id = uuid.uuid4().hex[:10]
    now = time.time()
    with _jobs_lock:
        jobs[job_id] = {
            # ``queued`` until the queue grants a slot and the runner flips it
            # to ``downloading``; both states answer on /api/status.
            "status": "queued",
            "url": url,
            "source_url": source_url,
            # A substituted track is fetched with the same YouTube player
            # clients its search used, so the alternative the user chose is the
            # alternative that downloads. ``None`` for every other job: yt-dlp
            # keeps its own choice there.
            "youtube_client": streaming.player_client() if is_track else None,
            "title": title,
            "uploader": uploader,
            "height_hint": height_hint,
            "format": format_choice,
            "format_id": format_id,
            "transcode": "force" if force_transcode else "never",
            "user_id": user_id,
            "created": now,
            "started_at": None,
            "queue_position": None,
            "wait_seconds": None,
            "queue_estimate_seconds": None,
            "queue_estimate_approximate": True,
            "total_bytes_estimate": size_hint,
            "elapsed": 0.0,
        }

    admission = job_queue.submit(
        job_id,
        user_id=user_id,
        kind=format_choice,
        size_bytes=size_hint,
    )

    if not admission.accepted:
        with _jobs_lock:
            jobs.pop(job_id, None)
        logger.info(
            "Job %s refused (%s): wait=%ss queue=%d",
            job_id,
            admission.code,
            admission.wait_seconds,
            admission.queue_length,
        )
        return _refusal(admission)

    _job_update(
        job_id,
        status=admission.status,
        queue_position=admission.position,
        wait_seconds=admission.wait_seconds,
        queue_estimate_seconds=admission.estimate_seconds,
        queue_estimate_approximate=admission.estimate_approximate,
        enqueued_at=now,
    )

    payload = {
        "job_id": job_id,
        "status": admission.status,
        "queue_position": admission.position,
        "wait_seconds": admission.wait_seconds,
        "queue_estimate_seconds": admission.estimate_seconds,
        "queue_estimate_approximate": admission.estimate_approximate,
        "queue_length": admission.queue_length,
    }
    logger.info(
        "Job %s accepted as %s (position=%s, wait=%ss)",
        job_id,
        admission.status,
        admission.position,
        admission.wait_seconds,
    )
    return jsonify(payload), 202


def _optional_int(value: object) -> int | None:
    """Parse an optional integer from JSON without trusting its type."""
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@app.route("/api/queue")
def queue_status():
    """Live queue for the web UI and the admin panel (internal API)."""
    return jsonify(job_queue.snapshot())


@app.route("/api/cookies/<service>")
def service_cookies(service: str):
    """The ordered cookie files for one service (paths only, never contents).

    The panel uploads jars and owns their order; the engine only reads the
    paths here — the same tree, mounted read-only. An unknown or maliciously
    named service answers ``[]`` rather than an error: the caller's question is
    "what do I try?" and "nothing" is a valid answer to it. The path is
    sanitised by ``cookies.cookie_paths``, so ``../../etc`` cannot escape.
    """
    paths = cookie_paths(service)
    return jsonify({
        "service": service,
        # Ordered: the panel's priority order is the retry order.
        "paths": paths,
        "files": [os.path.basename(path) for path in paths],
        "count": len(paths),
    })


@app.route("/api/egress")
def egress_rule():
    """The outbound connections the panel configured (internal, read-only).

    ``{connections: [{id, name, kind, url, assignment: {mode, services[]},
    enabled, status}], test_interval}``. The engine polls this and caches it for
    45 s; the panel is the authority on the order and on the rule, so this
    endpoint only republishes the parsed list. Every URL here is stored without
    a login — a password lives in ``EGRESS_PATH/secrets/<id>.txt`` on the shared
    egress tree, which is why nothing secret crosses this endpoint and nothing
    secret is ever logged.
    """
    return jsonify(egress.rule().as_payload())


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Apply download limits at runtime (the panel posts here after saving)."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400
    updated, errors = download_queue.Limits.parse(data, base=job_queue.limits)
    if errors:
        return jsonify({"error": "; ".join(errors), "errors": errors}), 400
    new_limits = job_queue.apply_limits(updated)
    global limits
    limits = new_limits
    return jsonify({"status": "ok", "limits": new_limits.as_dict()})


@app.route("/api/status/<job_id>")
def check_status(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        snapshot = dict(job)

    # Queue facts are read live so the card can count down while the job waits;
    # ``None`` for a job the queue no longer tracks (finished or failed).
    position = job_queue.position_of(job_id)
    wait_seconds = job_queue.wait_for(job_id)
    status = snapshot.get("status")
    stage = snapshot.get("stage")
    if status in ("done", "error"):
        position = None
        wait_seconds = None
        stage = None
    entry = job_queue.entry(job_id)
    started_at = snapshot.get("started_at")
    elapsed = snapshot.get("elapsed")
    if started_at:
        elapsed = round(max(0.0, time.time() - started_at), 1)
    elif snapshot.get("enqueued_at"):
        elapsed = 0.0

    return jsonify({
        "status": status,
        "error": snapshot.get("error"),
        "filename": snapshot.get("filename"),
        "progress": snapshot.get("progress"),
        "file_path": snapshot.get("file_path"),
        "file_size_bytes": snapshot.get("file_size_bytes"),
        "width": snapshot.get("width"),
        "height": snapshot.get("height"),
        "duration": snapshot.get("duration"),
        # Additive queue fields (the bot's card and the web UI read them).
        "queue_position": position,
        "wait_seconds": wait_seconds,
        "queue_estimate_seconds": (
            round(entry.estimate_seconds, 1) if entry else snapshot.get("queue_estimate_seconds")
        ),
        "queue_estimate_approximate": (
            entry.approximate if entry else snapshot.get("queue_estimate_approximate")
        ),
        "total_bytes_estimate": snapshot.get("total_bytes_estimate"),
        "elapsed": elapsed,
        "queue_length": job_queue.queued_count(),
        # Conversion facts (additive): the ``converting`` stage, the codec the
        # delivered file actually has, and whether it must go out as a
        # document. An older bot ignores them and behaves as before.
        "stage": stage,
        "vcodec": snapshot.get("vcodec"),
        "converted": bool(snapshot.get("converted")),
        "send_as_document": bool(snapshot.get("send_as_document")),
        # Cookie facts (additive): ``error_code == "cookies_required"`` tells the
        # bot to say "add cookies for this service" instead of showing a raw
        # yt-dlp line, and ``cookies_service`` names the service to add. An
        # older bot ignores both and keeps the plain error text.
        "error_code": snapshot.get("error_code"),
        "cookies_service": snapshot.get("cookies_service"),
    })


@app.route("/api/transcode/policy")
def transcode_policy():
    """The conversion policy and the measured estimate (additive endpoint).

    The bot renders its "Convert (~4 min)" button from this, so a change of
    ``TRANSCODE_MODE`` or a new EWMA sample is reflected without redeploying the
    bot. Nothing here is a secret: a mode name, a size ceiling and a duration.
    """
    minutes = transcode.FALLBACK_MB_PER_MINUTE
    return jsonify(
        {
            "mode": TRANSCODE_MODE,
            "max_mb": TRANSCODE_MAX_MB,
            "video_threads": transcode.VIDEO_THREADS,
            "preset": transcode.FRAME_PRESET,
            "fallback_mb_per_minute": minutes,
            "fallback_speed_factor": transcode.FALLBACK_SPEED_FACTOR,
            "stats": transcode_stats.snapshot(),
        }
    )


def _format_of(job_id: str) -> str | None:
    """The ``video``/``audio`` kind of a finished job, or its file extension."""
    with _jobs_lock:
        job = dict(jobs.get(job_id) or {})
    if job:
        return str(job.get("format") or "video")
    return None


def _wait_for_slot(job_id: str, timeout: float = 900.0) -> bool:
    """Wait until the queue grants ``job_id`` a slot (polling, not blocking)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job_queue.acquire(job_id, kind="transcode"):
            return True
        time.sleep(1.0)
    return False


@app.route("/api/transcode/<job_id>", methods=["POST"])
def start_transcode(job_id: str):
    """Convert an already-finished job's file to H.264 (explicit request only).

    This is what the chat's *Convert* button calls when the download itself was
    started with ``transcode=never`` (send as file): the user saw the file, then
    asked for a playable copy. The work runs in a background thread so the call
    answers immediately with the same ``/api/status`` vocabulary the bot already
    polls — a new stage (``converting``), not a new client protocol.

    The conversion takes a queue slot (``JobQueue.acquire``) and the process-wide
    ``transcode.conversion_lock``, so it can never run next to a second conversion
    and never next to more downloads than the slot budget allows.
    """
    with _jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        source = job.get("file")
        fmt = str(job.get("format") or "video")
        vcodec = job.get("vcodec")
        already = bool(job.get("converted"))
        in_flight = bool(job.get("transcoding"))

    if fmt != "video":
        return jsonify({"error": "Only a video job can be converted"}), 400
    if in_flight:
        return jsonify({"error": "A conversion is already running", "stage": "converting"}), 409
    if already or not transcode.needs_conversion(vcodec):
        return jsonify({"error": "This file is already playable", "vcodec": vcodec}), 400
    if not source or not os.path.exists(source):
        return jsonify({"error": "File not ready"}), 404

    try:
        size = os.path.getsize(source)
    except OSError:
        size = None

    estimate, approximate = transcode_stats.estimate(size)
    _job_update(
        job_id,
        transcoding=True,
        stage="converting",
        stage_updated_at=time.time(),
        progress={"percent": 0.0, "stage": "converting", "elapsed": 0.0},
    )

    def _run() -> None:
        acquired = _wait_for_slot(job_id)
        try:
            if not acquired:
                _job_update(
                    job_id,
                    status="error",
                    error="The server is busy; the conversion did not start",
                    stage=None,
                    transcoding=False,
                )
                return
            duration = _probe_media(source).get("duration")
            converted = _convert_to_h264(job_id, source, duration)
            if not converted:
                _job_update(job_id, transcoding=False, stage=None, stage_updated_at=None)
                return
            try:
                final_size = os.path.getsize(source)
            except OSError:
                final_size = None
            _job_update(
                job_id,
                transcoding=False,
                vcodec="h264",
                converted=True,
                send_as_document=False,
                file_size_bytes=final_size,
                stage=None,
                stage_updated_at=None,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced on /api/status
            logger.exception("Conversion for job %s crashed", job_id)
            _job_update(
                job_id,
                status="error",
                error=f"Conversion failed: {exc}",
                stage=None,
                transcoding=False,
            )
        finally:
            if acquired:
                job_queue.finish(job_id, succeeded=True)

    thread = threading.Thread(target=_run, name=f"transcode-{job_id}", daemon=True)
    thread.start()
    return jsonify(
        {
            "job_id": job_id,
            "stage": "converting",
            "estimate_seconds": int(round(estimate)),
            "estimate_approximate": bool(approximate),
            "mode": TRANSCODE_MODE,
        }
    ), 202


@app.route("/api/file/<job_id>")
def download_file(job_id: str):
    with _jobs_lock:
        job = dict(jobs.get(job_id) or {})
    if not job or job.get("status") != "done" or not job.get("file"):
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job.get("filename"))


def validate_startup_config() -> None:
    """Fail fast with a clear message when the engine is misconfigured."""
    if INTERNAL_API_AUTH_DISABLED:
        logger.warning(
            "INTERNAL_API_AUTH_DISABLED is set: the internal API accepts "
            "requests without a token. Never do this on a shared network."
        )
        return
    if not INTERNAL_API_TOKEN:
        raise SystemExit(
            "INTERNAL_API_TOKEN is required. Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and set "
            "it for the engine, the bot and the dashboard. Set "
            "INTERNAL_API_AUTH_DISABLED=true only for local development."
        )
    if len(INTERNAL_API_TOKEN) < 16:
        raise SystemExit(
            "INTERNAL_API_TOKEN is too short (minimum 16 characters). "
            "Use at least 32 random characters."
        )


if __name__ == "__main__":
    validate_startup_config()
    # One INFO line naming the panel the egress rule comes from: the failure the
    # operator hit was that this address was silently wrong.
    egress.log_configuration()
    host = os.environ.get("HOST", "127.0.0.1")
    port = _env_int("PORT", 8899, minimum=1)
    app.run(host=host, port=port, threaded=True)
