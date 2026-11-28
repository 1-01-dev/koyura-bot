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
import message_catalog
import message_templates
import proxy_connections

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
    """The bot's operator-set preferences: languages and message templates.

    Read-only and internal: ``bot/bot_settings.py`` polls this once a minute and
    caches the answer, so a change in the panel reaches the chat within that
    window. A request without the shared token is refused exactly like an event
    would be, and the response carries nothing but the public facts: the enabled
    languages, the default one, and the active custom message bodies.

    The ``progress`` switches this body used to carry are gone: which lines the
    live message draws is the ``progress`` template (in ``templates`` below).
    Nothing here reads the retired ``bot_progress.*`` rows, so an upgraded panel
    ignores them without a migration — and an old bot that still expects the
    object simply renders its own built-in default.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stored = await db.get_settings(prefix=bot_languages.SETTING_PREFIX)
    settings = bot_languages.BotLanguages.from_row(stored)
    rows = await db.active_message_templates()
    return {
        "languages": list(settings.languages),
        "default_language": settings.default_language,
        # ``{kind: {lang: body}}``: one active custom body per pair, or nothing
        # at all. The bot renders the built-in default for every kind it does
        # not find here, so an empty object is a valid answer.
        "templates": message_templates.active_bodies(rows),
        # The subscription gate's master switch. Off by default, and the bot
        # needs it even when it never asks /api/subscriptions (a channel list
        # without the switch is not a gate).
        "subscriptions_enabled": await db.subscriptions_enabled(),
    }


# ---------------------------------------------------------------------------
# Subscription gate (internal, read-only)
# ---------------------------------------------------------------------------

@router.get("/api/subscriptions")
async def subscription_gate(request: Request) -> dict[str, Any]:
    """The channels the bot asks a new user to join, and whether the gate is on.

    Internal, like every ``/api/*`` call the bot makes. Only channels that
    **can** be verified travel in ``channels``: an enabled channel has to be one
    where the bot is an administrator of the channel, otherwise
    ``getChatMember`` answers "not a member" for every user and the gate would
    lock the bot instead of opening it. The rows where that check failed are
    reported in ``unusable`` so the panel's page and the API cannot disagree
    about what is in force.

    An empty ``channels`` means "no gate", even with ``enabled: true``: the bot
    reads :meth:`bot.subscriptions.Gate.active` and needs both halves.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    enabled = await db.subscriptions_enabled()
    rows = await db.list_required_channels(enabled_only=True)
    usable = [row for row in rows if row["bot_is_admin"]]
    return {
        "enabled": enabled,
        "channels": [
            {
                "id": int(row["id"]),
                "title": row["title"],
                "username": row["username"],
                "link": row["link"],
                "position": int(row["position"]),
            }
            for row in usable
        ],
        "unusable": [
            {
                "id": int(row["id"]),
                "username": row["username"],
                "check_error": row["check_error"],
            }
            for row in rows
            if not row["bot_is_admin"]
        ],
    }


# ---------------------------------------------------------------------------
# Subscription history (internal, write)
# ---------------------------------------------------------------------------

class SubscriptionEventBody(BaseModel):
    """One membership verdict the bot recorded."""

    user_id: int
    channel_id: int
    state: str


@router.post("/api/subscriptions/events")
async def ingest_subscription_event(request: Request) -> dict[str, Any]:
    """Record one ``subscribed``/``left`` transition reported by the bot.

    The bot is the only thing that can call ``getChatMember``, so it is also the
    only thing that knows a user joined or left. It posts the verdict here and
    the panel writes it — but only when it is a *change*, which is what makes
    the unsubscribe count meaningful and lets the daily check re-post the same
    verdict without inflating anything.

    ``written: false`` therefore means "already the stored state", not an
    error; the bot does not retry it.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid event body")

    state = str(payload.get("state") or "").strip().lower()
    if state not in (db.SUBSCRIBED, db.LEFT):
        raise HTTPException(status_code=400, detail="Invalid state")
    raw_user = payload.get("user_id")
    raw_channel = payload.get("channel_id")
    if raw_user in (None, "") or raw_channel in (None, ""):
        raise HTTPException(
            status_code=400, detail="user_id and channel_id are required"
        )
    try:
        user_id = int(str(raw_user))
        channel_id = int(str(raw_channel))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="user_id and channel_id must be numbers"
        ) from exc

    written = await db.record_subscription(user_id, channel_id, state)
    return {"written": written, "state": state}


@router.get("/api/subscriptions/active-users")
async def active_subscribers(request: Request) -> dict[str, Any]:
    """The ids of users the bot should re-check tonight.

    Internal and ids-only: the nightly sweep in the bot asks ``getChatMember``
    about each one, so nothing else is needed and nothing else is sent. "Active"
    is the same definition the page's KPI uses — last state ``subscribed`` on
    **every** enabled channel — so the number on the page and the length of this
    list cannot disagree.

    A bot that asks while the gate is off gets ``[]``: there is nothing to
    re-check, and the sweep must not spend Telegram's rate limit on a feature
    the owner turned off.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not await db.subscriptions_enabled():
        return {"user_ids": []}
    channels = await db.list_required_channels(enabled_only=True)
    usable = [int(row["id"]) for row in channels if row["bot_is_admin"]]
    if not usable:
        return {"user_ids": []}
    return {"user_ids": await db.subscribed_user_ids(usable)}


# ---------------------------------------------------------------------------
# Egress: the proxy connections the engine walks (internal)
# ---------------------------------------------------------------------------

@router.get("/api/egress")
async def egress_connections(request: Request) -> dict[str, Any]:
    """The outbound connections, in the order the engine tries them.

    ``{connections: [{id, name, kind, url, assignment: {mode, services[]},
    enabled, status}], test_interval}``. This endpoint is internal and behind
    the shared token, and it still carries **no secret**: the URL is stored
    without a login, the password lives in ``EGRESS_PATH/secrets/<id>.txt``
    (0600) which the engine mounts read-only, and ``username``, ``password_ref``
    and ``config_path`` are never part of the answer. ``test_interval`` is the
    panel's re-test period in minutes (0 = never).
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await proxy_connections.engine_payload()


@router.post("/api/egress/test/{connection_id}")
async def test_egress_connection(
    request: Request, connection_id: int
) -> dict[str, Any]:
    """Check one connection through ``https://api.ipify.org`` and store it.

    The probe dials the same URL the engine would (the stored one plus the
    credential from the secrets tree, or the tunnel's SOCKS address) and
    records ``ok``/``fail`` with the external IP or a redacted error text. The
    answer is the public view plus the test fields — a password never appears,
    not even when curl echoes the proxy it could not use.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        row = await proxy_connections.test_connection(connection_id)
    except proxy_connections.ProxyConnectionError as error:
        if error.code == "NotFound":
            raise HTTPException(status_code=404, detail=str(error)) from error
        raise HTTPException(status_code=400, detail=str(error)) from error
    result = proxy_connections.public_view(row)
    result.update(
        {
            "last_test_at": row.get("last_test_at"),
            "last_test_result": row.get("last_test_result"),
        }
    )
    logger.info(
        "Egress check on connection %s (%s): %s",
        row.get("id"),
        row.get("name"),
        row.get("status"),
    )
    return result


# ---------------------------------------------------------------------------
# Cookie registry (internal, read-only)
# ---------------------------------------------------------------------------

@router.get("/api/services-with-cookies")
async def services_with_cookies(request: Request) -> dict[str, Any]:
    """The services that hold cookie jars, and each one's files in order.

    The engine polls this (cached for 60 s) instead of reading the panel's
    database, and the order in ``files`` is the order it tries the jars in.
    Only names travel: no path, no content, no hash. A service with a row but
    no file on disk is reported as it is stored — the engine skips a listed
    file it cannot open, so a stale row degrades to a shorter list rather than
    a failed job.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    rows = await db.list_service_cookies()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["service"]), []).append(str(row["stored_name"]))
    return {
        "services": [
            {"service": name, "files": files}
            for name, files in sorted(grouped.items())
        ],
    }


# ---------------------------------------------------------------------------
# Message templates (internal read + admin preview)
# ---------------------------------------------------------------------------

@router.get("/api/message-templates")
async def message_template_catalogue(request: Request) -> dict[str, Any]:
    """The catalogue and the active bodies, for the bot and for debugging.

    Same shared token as ``/api/bot-settings`` (which already carries the
    ``templates`` object the bot uses); this endpoint adds the kind metadata the
    contract test compares against ``bot/message_kinds.py``.
    """
    if not _internal_token_ok(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    rows = await db.active_message_templates()
    return {
        "kinds": [
            {
                "id": kind.id,
                "category": kind.category,
                "media": kind.media,
                "placeholders": list(kind.placeholders),
            }
            for kind in message_catalog.KINDS
        ],
        "templates": message_templates.active_bodies(rows),
        "coverage": message_templates.coverage(rows),
    }


@router.post("/api/message-templates/preview")
async def preview_message_template(
    request: Request, user: str = Depends(require_admin)
) -> dict[str, Any]:
    """Render one body with the same code the bot uses, for the editor pane.

    A broken body answers with ``error`` rather than a 4xx: the pane shows the
    sentence, and the operator keeps typing. The body comes from the form (the
    textarea may be unsaved), so there is nothing to look up in the database.
    """
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid preview body")

    kind = str(payload.get("kind") or "")
    if message_catalog.get(kind) is None:
        raise HTTPException(status_code=404, detail="Unknown message kind")

    body = payload.get("body")
    return message_templates.preview(
        kind,
        message_templates.normalise_lang(payload.get("lang")),
        None if body is None else str(body),
    )


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
    """Downloads over time, the two top-five lists, and the subscription series.

    One request feeds the whole dashboard: the range switch above the chart is
    the same switch the subscription KPI cells and their series read, so a page
    never has two periods on screen at once. The subscription part carries
    ``visible`` — the gate is on, or there is history to show — which is how the
    template knows not to draw it.
    """
    valid_ranges = {"1D", "7D", "1M", "1Y"}
    if range not in valid_ranges:
        raise HTTPException(status_code=400, detail=f"Invalid range. Must be one of {valid_ranges}")
    payload = await db.get_chart_data(range)
    payload["subscriptions"] = await _subscription_block(range.lower())
    return payload


async def _subscription_block(period: str) -> dict[str, Any]:
    """The dashboard's subscription panel: three cells, two series, a breakdown.

    ``visible`` is the single decision the template and the script branch on:
    the gate is on **or** the history holds at least one transition, ever. A
    gate that is on draws the panel even with zeros — that is the module
    telling the operator it works and nobody has joined yet; a gate that is off
    draws it only while there is history worth reading. Only off-and-empty
    stays out of the page. Everything else in the block is still present (an
    empty series, an empty list) so a caller reading the JSON never has to test
    for a missing key.
    """
    stats = await db.get_subscription_stats(period)
    channels = stats["channels"]
    return {
        "visible": bool(stats["enabled"] or stats["has_history"]),
        "period": stats["period"],
        # Over all time: the breakdown says "No subscriptions yet" only for a
        # table that has never held a row, never for a quiet week.
        "has_history": stats["has_history"],
        "subscribed": stats["subscribed"],
        "left": stats["left"],
        "active": stats["active"],
        "chart": stats["chart"],
        "channels": channels,
        # One channel is a single row that repeats the KPI cells; the panel
        # shows only the total line in that case.
        "single_channel": channels[0] if len(channels) == 1 else None,
    }


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
