"""Background cleanup of the shared downloads volume.

Two independent limits are enforced:

* ``CLEANUP_MAX_AGE_HOURS``  — delete finished files older than this
* ``CLEANUP_MAX_DISK_MB``    — delete the oldest files until usage fits

Files touched within the last ``CLEANUP_ACTIVE_GRACE_SECONDS`` seconds are never
deleted: yt-dlp writes progress into the file it is currently downloading, so a
recent mtime means the file is still active.  The task is filesystem-only and
therefore keeps working with an empty, broken or missing database.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DOWNLOADS_PATH = Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "300"))
CLEANUP_MAX_AGE_HOURS = float(os.environ.get("CLEANUP_MAX_AGE_HOURS", "1"))
CLEANUP_MAX_DISK_MB = int(os.environ.get("CLEANUP_MAX_DISK_MB", "5000"))
CLEANUP_ACTIVE_GRACE_SECONDS = int(os.environ.get("CLEANUP_ACTIVE_GRACE_SECONDS", "300"))


async def cleanup_loop() -> None:
    logger.info(
        "Cleanup task started: path=%s interval=%ds max_age=%sh max_disk=%dMB grace=%ds",
        DOWNLOADS_PATH,
        CLEANUP_INTERVAL_SECONDS,
        CLEANUP_MAX_AGE_HOURS,
        CLEANUP_MAX_DISK_MB,
        CLEANUP_ACTIVE_GRACE_SECONDS,
    )
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            _run_cleanup()
        except Exception:
            logger.exception("Cleanup cycle failed")


def _active(stat_result: os.stat_result, now: float) -> bool:
    """True when the file was modified too recently to be considered finished."""
    return (now - stat_result.st_mtime) < CLEANUP_ACTIVE_GRACE_SECONDS


def _iter_files():
    """Yield (path, stat) for regular files directly inside DOWNLOADS_PATH."""
    try:
        if not DOWNLOADS_PATH.exists():
            return
        entries = list(DOWNLOADS_PATH.iterdir())
    except OSError:
        logger.warning("Cannot list %s", DOWNLOADS_PATH)
        return

    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            yield entry, entry.stat()
        except OSError:
            logger.debug("Skipping unreadable entry %s", entry)


def _run_cleanup() -> None:
    now = time.time()
    max_age_sec = CLEANUP_MAX_AGE_HOURS * 3600
    deleted = 0

    for entry, stat_result in _iter_files():
        if _active(stat_result, now):
            continue
        try:
            age = now - stat_result.st_mtime
            if age > max_age_sec:
                entry.unlink()
                deleted += 1
                logger.info("Deleted (age): %s (%.0fs old)", entry.name, age)
        except PermissionError:
            logger.warning("Permission denied: %s", entry.name)
        except OSError:
            logger.exception("Error checking file: %s", entry.name)

    deleted += _enforce_disk_limit()
    if deleted:
        logger.info("Cleanup pass removed %d file(s)", deleted)


def _enforce_disk_limit() -> int:
    now = time.time()
    files = []
    total_bytes = 0
    for entry, stat_result in _iter_files():
        files.append((entry, stat_result.st_mtime, stat_result.st_size))
        total_bytes += stat_result.st_size

    max_bytes = CLEANUP_MAX_DISK_MB * 1024 * 1024
    if total_bytes <= max_bytes:
        return 0

    files.sort(key=lambda item: item[1])  # oldest first
    deleted = 0
    for path, mtime, size in files:
        if total_bytes <= max_bytes:
            break
        if (now - mtime) < CLEANUP_ACTIVE_GRACE_SECONDS:
            # Everything left is still being written; stop instead of deleting
            # a download that is in flight.
            break
        try:
            path.unlink()
            total_bytes -= size
            deleted += 1
            logger.info("Deleted (disk limit): %s (freed %d bytes)", path.name, size)
        except PermissionError:
            logger.warning("Permission denied: %s", path.name)
        except OSError:
            logger.exception("Error deleting: %s", path.name)

    if deleted == 0:
        logger.warning(
            "Disk limit of %d MB exceeded but every remaining file is active",
            CLEANUP_MAX_DISK_MB,
        )
    return deleted
