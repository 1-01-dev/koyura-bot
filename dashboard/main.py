"""FastAPI application factory for the Koyura admin dashboard."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import db
import download_limits as limits_settings
from routes.api import router as api_router
from routes.pages import router as pages_router

logger = logging.getLogger("koyura.dashboard")

_STATIC_DIR = Path(__file__).parent / "static"

# Applied to every response. The dashboard ships its own CSS/JS and now
# self-hosts Inter, JetBrains Mono and Shippori Mincho, so no font CDN is
# needed; Chart.js is the only third-party origin (script). '#unsafe-inline'
# remains in style-src for the inline style attributes the templates emit
# (disk-bar width, hidden sections).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


async def _disk_snapshot_loop() -> None:
    """Background task: scan DOWNLOADS_PATH every 5 minutes and record a snapshot."""
    while True:
        try:
            downloads_path = Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))
            total_bytes = 0
            file_count = 0
            if downloads_path.exists():
                for item in downloads_path.rglob("*"):
                    if item.is_file():
                        try:
                            total_bytes += item.stat().st_size
                            file_count += 1
                        except OSError:
                            continue
            await db.insert_disk_snapshot(total_bytes=total_bytes, file_count=file_count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Disk snapshot failed")
        await asyncio.sleep(300)  # 5 minutes


async def _drop_retired_progress_rows() -> None:
    """Delete the ``bot_progress.*`` rows the retired Progress fields wrote.

    The section is gone and nothing reads these keys any more, so the rows are
    dead weight. The delete is best effort and additive-only as far as the schema
    is concerned: no table is touched, a missing table simply matches nothing,
    and any failure is logged and swallowed — a panel that starts with a few
    stale preference rows is fine, one that refuses to start is not.
    """
    try:
        removed = await db.delete_settings(
            prefix="bot_progress.",
        )
    except Exception:
        logger.exception("Could not remove the retired progress-field rows")
        return
    if removed:
        logger.info("Removed %d retired progress-field setting(s)", removed)


async def _apply_saved_limits() -> None:
    """Push the limits stored in the panel to the engine on startup.

    The environment is only the fallback: whatever the admin saved last is what
    the engine should run with after a restart. A failure here is logged, never
    fatal — the panel keeps its values and the next save retries.
    """
    try:
        stored = await db.get_settings(prefix=limits_settings.SETTING_PREFIX)
        if not stored:
            return
        limits = limits_settings.Limits.from_row(
            {key.removeprefix(limits_settings.SETTING_PREFIX): value
             for key, value in stored.items()}
        )
        ok, detail = limits_settings.apply_to_engine(limits, timeout=5.0)
        if ok:
            logger.info("Applied saved download limits to the engine")
        else:
            logger.warning("Could not apply saved download limits: %s", detail)
    except Exception:
        logger.exception("Could not apply the saved download limits")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await db.init_db()
    if os.environ.get("MARK_STALE_ON_STARTUP", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }:
        try:
            updated = await db.mark_interrupted_downloads()
            if updated:
                logger.info("Marked %d interrupted download(s) as failed", updated)
        except Exception:
            logger.exception("Could not mark interrupted downloads")
    await _apply_saved_limits()
    await _drop_retired_progress_rows()
    task = asyncio.create_task(_disk_snapshot_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    """Application factory."""
    # Refuse to start with missing/placeholder secrets.
    auth.validate_config()

    app = FastAPI(title="Koyura Admin", lifespan=_lifespan)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception):
        # Log without request bodies/credentials and return a generic error.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(api_router)
    app.include_router(pages_router)

    return app


app = create_app()
