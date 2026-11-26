"""API routes for the Koyura admin dashboard."""
from __future__ import annotations

import hmac
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import auth
import bot_languages
import db

logger = logging.getLogger(__name__)

router = APIRouter()

# Live progress for in-flight downloads. Intentionally in-memory: the
# authoritative record is written to SQLite by /api/events.
_active_downloads: dict[str, dict[str, Any]] = {}


def _downloads_path() -> Path:
    return Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

def require_auth(request: Request) -> str:
    user = auth.get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_csrf(request: Request) -> None:
    """Reject state-changing requests that lack a valid CSRF token."""
    submitted = request.headers.get(auth.CSRF_HEADER)
    if not auth.verify_csrf(request, submitted):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def require_admin(request: Request) -> str:
    """Session auth first, then CSRF: unauthenticated callers get 401, not 403."""
    user = require_auth(request)
    require_csrf(request)
    return user


def _internal_token_ok(request: Request) -> bool:
    """Event ingestion is internal; verify the shared token when configured."""
    expected = (os.environ.get("INTERNAL_API_TOKEN") or "").strip()
    if not expected:
        return True
    provided = request.headers.get("X-Internal-Token", "")
    return hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Event ingestion (internal network only)
# ---------------------------------------------------------------------------

@router.post("/api/events")
async def ingest_event(request: Request) -> dict[str, str]:
    """Accept download lifecycle events from the bot.

    The bot sends flat JSON: ``{"type": "download_start", "job_id": ...}``.
    Requests must carry ``X-Internal-Token`` when that variable is configured.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid event body")

    # Accept the documented flat shape and the older nested {"data": {...}} one.
    nested = payload.get("data")
    data: dict[str, Any] = nested if isinstance(nested, dict) else payload
    event_type = data.get("type") or payload.get("type")
    job_id = data.get("job_id")

    if not event_type:
        raise HTTPException(status_code=400, detail="Missing event type")
    if not job_id:
        raise HTTPException(status_code=400, detail="Missing job_id")

    try:
        if event_type in ("download_start", "start"):
            await db.insert_download_start(
                job_id=str(job_id),
                user_id=data.get("user_id"),
                username=data.get("username"),
                chat_id=data.get("chat_id"),
                url=str(data.get("url") or ""),
                platform=data.get("platform"),
                title=data.get("title"),
                format=data.get("format"),
                quality=data.get("quality"),
            )
            _active_downloads[str(job_id)] = {
                "job_id": str(job_id),
                "user_id": data.get("user_id"),
                "username": data.get("username"),
                "url": data.get("url"),
                "platform": data.get("platform"),
                "title": data.get("title"),
                "percent": 0,
                "speed": 0,
                "eta": 0,
            }
        elif event_type in ("progress", "download_progress"):
            # The title is often known only after the metadata probe, so a
            # progress event may backfill it for an already-started job.
            late_title = data.get("title")
            if late_title:
                await db.update_download_title(job_id=str(job_id), title=late_title)

            entry = _active_downloads.get(str(job_id))
            if entry is not None:
                entry.update(
                    {
                        "percent": data.get("percent", 0),
                        "speed": data.get("speed", 0),
                        "eta": data.get("eta", 0),
                        "downloaded_bytes": data.get("downloaded_bytes", 0),
                        "total_bytes": data.get("total_bytes", 0),
                    }
                )
                if late_title:
                    entry["title"] = late_title
        elif event_type in ("download_done", "done"):
            await db.update_download_done(
                job_id=str(job_id),
                file_size_bytes=data.get("file_size_bytes"),
                download_duration_sec=data.get("duration_seconds"),
            )
            _active_downloads.pop(str(job_id), None)
        elif event_type in ("download_error", "error"):
            await db.update_download_error(
                job_id=str(job_id),
                error_message=str(data.get("error_message") or "Unknown error"),
            )
            _active_downloads.pop(str(job_id), None)
        else:
            logger.debug("Ignoring unknown event type %r", event_type)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"Missing field: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - never leak internals to the caller
        logger.exception("Failed to ingest %s event", event_type)
        raise HTTPException(status_code=500, detail="Could not store event") from exc

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Bot settings (internal, read-only)
# ---------------------------------------------------------------------------

@router.get("/api/bot-settings")
async def bot_settings(request: Request) -> dict[str, Any]:
    """The languages the bot offers and its default, for the bot itself.

    Read-only and internal: ``bot/bot_settings.py`` polls this once a minute and
    caches the answer, so a language toggled in the panel reaches the chat within
    that window. A request without the shared token is refused exactly like an
    event would be, and the response carries nothing but the two public facts.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stored = await db.get_settings(prefix=bot_languages.SETTING_PREFIX)
    settings = bot_languages.BotLanguages.from_row(stored)
    return {
        "languages": list(settings.languages),
        "default_language": settings.default_language,
    }


# ---------------------------------------------------------------------------
# Dashboard stats (auth required)
# ---------------------------------------------------------------------------

@router.get("/api/dashboard-stats")
async def dashboard_stats(user: str = Depends(require_auth)) -> dict[str, Any]:
    stats = await db.get_dashboard_stats()
    disk = await db.get_latest_disk_snapshot()
    return {"stats": stats, "disk": disk}


@router.get("/api/chart-data")
async def chart_data(
    range: str = "1D",
    user: str = Depends(require_auth),
) -> dict[str, Any]:
    valid_ranges = {"1D", "7D", "1M", "1Y"}
    if range not in valid_ranges:
        raise HTTPException(status_code=400, detail=f"Invalid range. Must be one of {valid_ranges}")
    return await db.get_chart_data(range)


@router.get("/api/active-downloads")
async def active_downloads(user: str = Depends(require_auth)) -> list[dict[str, Any]]:
    return list(_active_downloads.values())


# ---------------------------------------------------------------------------
# Admin file operations (auth + CSRF required)
# ---------------------------------------------------------------------------

class DeleteFilesBody(BaseModel):
    paths: list[str]


class PurgeBody(BaseModel):
    confirm: str | None = None


@router.delete("/api/files")
async def delete_files(
    body: DeleteFilesBody,
    user: str = Depends(require_admin),
) -> dict[str, Any]:
    """Delete selected files. Uses filename only to prevent path traversal."""
    downloads_path = _downloads_path()
    deleted = []
    errors = []
    for candidate in body.paths:
        # Prevent path traversal: use only the filename component
        safe_path = downloads_path / Path(candidate).name
        try:
            if safe_path.exists() and safe_path.is_file():
                safe_path.unlink()
                deleted.append(safe_path.name)
            else:
                errors.append({"file": candidate, "error": "not found"})
        except OSError as exc:
            errors.append({"file": candidate, "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


@router.delete("/api/files/all")
async def purge_all_files(
    body: PurgeBody,
    user: str = Depends(require_admin),
) -> dict[str, Any]:
    """Purge all files in DOWNLOADS_PATH. Requires confirm='PURGE' in the body."""
    if body.confirm != "PURGE":
        raise HTTPException(status_code=400, detail='Body must contain {"confirm": "PURGE"}')
    downloads_path = _downloads_path()
    deleted_count = 0
    if downloads_path.exists():
        for item in downloads_path.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    deleted_count += 1
                elif item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                    deleted_count += 1
            except OSError:
                logger.warning("Could not purge %s", item)
    return {"deleted_count": deleted_count}
