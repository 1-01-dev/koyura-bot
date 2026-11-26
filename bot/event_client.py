"""Fire-and-forget download telemetry for the admin dashboard.

The dashboard never talks to the bot: the bot pushes one JSON object per
lifecycle step to ``POST /api/events`` and forgets about it. Four event types
exist and their field names are a public contract shared with
``dashboard/routes/api.py``:

    download_start   job_id user_id username chat_id url platform format
                     quality title
    progress         job_id percent speed eta downloaded_bytes total_bytes
                     [title]
    download_done    job_id file_size_bytes duration_seconds filename
    download_error   job_id error_message

Reporting is best effort by design. A panel that is down, slow or restarting
must not turn a finished download into a failure for the user, so every call
returns ``None`` and any exception is demoted to a DEBUG line. Requests carry
``X-Internal-Token`` when the shared secret is configured; the dashboard
rejects them with 401 otherwise.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Read once at import so a deployment only has to set the environment before
# the process starts; tests swap the module attribute instead of the env.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()

# A dashboard that cannot answer within two seconds is not worth waiting for.
_TIMEOUT_SECONDS = 2.0
_EVENTS_PATH = "/api/events"


def _headers() -> dict[str, str]:
    """Auth header for the internal dashboard API (empty when unconfigured)."""
    if INTERNAL_API_TOKEN:
        return {"X-Internal-Token": INTERNAL_API_TOKEN}
    return {}


def _timestamp() -> str:
    """Event time in UTC, ISO 8601, as the dashboard stores it."""
    return datetime.now(timezone.utc).isoformat()


def _events_url() -> str:
    return f"{DASHBOARD_URL}{_EVENTS_PATH}"


async def _emit(event_type: str, fields: dict[str, Any]) -> None:
    """POST one event and swallow everything that can go wrong.

    ``fields`` is spread as-is: the dashboard reads the same key names, so an
    extra key is ignored rather than rejected.
    """
    body: dict[str, Any] = {"type": event_type, "ts": _timestamp()}
    body.update(fields)
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS, headers=_headers()
        ) as client:
            await client.post(_events_url(), json=body)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a download
        logger.debug("%s event dropped: %s", event_type, exc)


async def send_download_start(
    job_id: str,
    user_id: int,
    username: str,
    chat_id: int,
    url: str,
    platform: str,
    format: str,
    quality: str,
    title: str,
) -> None:
    """Announce a job the engine has just accepted."""
    await _emit(
        "download_start",
        {
            "job_id": job_id,
            "user_id": user_id,
            "username": username,
            "chat_id": chat_id,
            "url": url,
            "platform": platform,
            "format": format,
            "quality": quality,
            "title": title,
        },
    )


async def send_progress(
    job_id: str,
    percent: float | None,
    speed: float | None,
    eta: float | None,
    downloaded_bytes: int | None,
    total_bytes: int | None,
) -> None:
    """Report one progress tick for an in-flight job."""
    await _emit(
        "progress",
        {
            "job_id": job_id,
            "percent": percent,
            "speed": speed,
            "eta": eta,
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
        },
    )


async def send_download_done(
    job_id: str,
    file_size_bytes: int,
    duration_seconds: float,
    filename: str,
) -> None:
    """Mark a job finished and give the panel the numbers for its KPIs."""
    await _emit(
        "download_done",
        {
            "job_id": job_id,
            "file_size_bytes": file_size_bytes,
            "duration_seconds": duration_seconds,
            "filename": filename,
        },
    )


async def send_download_error(
    job_id: str,
    error_message: str,
) -> None:
    """Mark a job failed with the message the user was shown."""
    await _emit(
        "download_error",
        {"job_id": job_id, "error_message": error_message},
    )
