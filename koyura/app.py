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
import media_meta
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


def _build_download_cmd(job_id: str, url: str, format_choice: str, format_id: str | None) -> list[str]:
    """Build the yt-dlp argument vector for a job.

    yt-dlp is invoked as ``<sys.executable> -m yt_dlp`` so the call works in a
    container and in a venv/systemd deployment without relying on PATH.
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
    cmd = _build_download_cmd(job_id, url, format_choice, format_id)
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
        return

    with stderr_lock:
        last_stderr = stderr_lines[-1] if stderr_lines else ""

    if returncode != 0:
        _cleanup_partial_files(job_id)
        _job_update(job_id, status="error", error=last_stderr or "Unknown error")
        logger.info("Job %s: yt-dlp exited with %s", job_id, returncode)
        return

    files = sorted(glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")))
    if not files:
        _job_update(job_id, status="error", error="Download completed but no file was found")
        return

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
            return

    if chosen.endswith(".mp4"):
        _make_telegram_compatible(chosen)
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


def _make_telegram_compatible(chosen: str) -> None:
    """Transcode AV1/VP9 to H.264, otherwise move the moov atom to the front."""
    rc, stdout, _, _ = _run_capture(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1", chosen,
        ],
        timeout=15,
    )
    vcodec = (stdout or "").strip().lower() if rc == 0 else ""

    if vcodec in ("av1", "vp9", "vp8"):
        transcoded = chosen + ".h264.mp4"
        rc, _, _, timed_out = _run_capture(
            [
                "ffmpeg", "-y", "-i", chosen,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                transcoded,
            ],
            timeout=600,
        )
        if timed_out:
            logger.warning("Transcode timed out for %s", chosen)
        if rc == 0 and os.path.exists(transcoded) and os.path.getsize(transcoded) > 0:
            os.replace(transcoded, chosen)
        elif os.path.exists(transcoded):
            _remove_quietly(transcoded)
        return

    faststart_tmp = chosen + ".fs.mp4"
    rc, _, _, _ = _run_capture(
        ["ffmpeg", "-y", "-i", chosen, "-c", "copy", "-movflags", "+faststart", faststart_tmp],
        timeout=120,
    )
    if rc == 0 and os.path.exists(faststart_tmp) and os.path.getsize(faststart_tmp) > 0:
        os.replace(faststart_tmp, chosen)
    elif os.path.exists(faststart_tmp):
        _remove_quietly(faststart_tmp)


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
    data = request.get_json(silent=True) or {}
    try:
        url = validate_url(data.get("url"))
    except UnsafeURLError as exc:
        return jsonify({"error": str(exc)}), 400

    rc, stdout, stderr, timed_out = _run_capture(
        _yt_dlp_cmd("--no-playlist", "-j", "--no-warnings", url),
        timeout=INFO_TIMEOUT,
    )
    if timed_out:
        return jsonify({"error": "Timed out fetching video info"}), 400
    if rc != 0:
        message = (stderr or "").strip().split("\n")[-1] if stderr else "Unknown error"
        return jsonify({"error": message}), 400

    try:
        info = json.loads(stdout)
    except json.JSONDecodeError:
        return jsonify({"error": "Could not parse video info"}), 400

    # The response shape lives in media_meta so it can be unit-tested and so
    # the bot's card and the engine agree on every field name.
    return jsonify(media_meta.build_info_payload(info))


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

    user_id = _optional_int(data.get("user_id"))
    size_hint = _optional_int(
        data.get("size_bytes") or data.get("filesize") or data.get("filesize_approx")
    )

    _prune_old_jobs()

    job_id = uuid.uuid4().hex[:10]
    now = time.time()
    with _jobs_lock:
        jobs[job_id] = {
            # ``queued`` until the queue grants a slot and the runner flips it
            # to ``downloading``; both states answer on /api/status.
            "status": "queued",
            "url": url,
            "title": title,
            "uploader": uploader,
            "height_hint": height_hint,
            "format": format_choice,
            "format_id": format_id,
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
    if status in ("done", "error"):
        position = None
        wait_seconds = None
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
    })


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
    host = os.environ.get("HOST", "127.0.0.1")
    port = _env_int("PORT", 8899, minimum=1)
    app.run(host=host, port=port, threaded=True)
