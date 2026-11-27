"""Async HTTP client for the Koyura media engine.

The engine's HTTP API is internal: every request carries the shared
``X-Internal-Token`` header unless ``INTERNAL_API_AUTH_DISABLED`` is set for
local development.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEFAULT_KOYURA_ENGINE_URL = "http://koyura-engine:8899"

INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "").strip()
INTERNAL_API_AUTH_DISABLED = os.environ.get(
    "INTERNAL_API_AUTH_DISABLED", ""
).strip().lower() in {"1", "true", "yes", "on"}


class KoyuraError(Exception):
    pass


class KoyuraInfoError(KoyuraError):
    pass


class KoyuraDownloadError(KoyuraError):
    pass


class KoyuraServiceDown(KoyuraError):
    pass


class KoyuraBusy(KoyuraError):
    """Raised when the engine refuses a job.

    ``code`` is the engine's reason (``busy``, ``queue_full``, ``too_long``,
    ``user_busy``) and ``retry_after`` its estimate of how long the user should
    wait, so the card can say "try again in about 2 min" instead of a vague
    "busy".
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry_after: int | None = None,
        wait_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.wait_seconds = wait_seconds if wait_seconds is not None else retry_after


def engine_url() -> str:
    """Address of the media engine (``KOYURA_ENGINE_URL``, else the compose one)."""
    return os.environ.get("KOYURA_ENGINE_URL", "").strip() or DEFAULT_KOYURA_ENGINE_URL


def auth_headers() -> dict[str, str]:
    """Headers required by the internal engine API."""
    if INTERNAL_API_TOKEN:
        return {"X-Internal-Token": INTERNAL_API_TOKEN}
    return {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=engine_url(), headers=auth_headers())


def _error_message(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return fallback
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return fallback


def _busy(response: httpx.Response, fallback: str) -> KoyuraBusy:
    """Build the refusal exception from a 429 body (additive fields, tolerant)."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    retry_after = payload.get("retry_after")
    try:
        retry_after = int(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    code = payload.get("code")
    return KoyuraBusy(
        str(payload.get("error") or fallback),
        code=str(code) if code else None,
        retry_after=retry_after,
    )


async def get_info(url: str) -> dict:
    try:
        async with _client() as client:
            resp = await client.post("/api/info", json={"url": url}, timeout=60.0)
            if resp.status_code == 429:
                raise _busy(resp, "The server is busy, try again later")
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as e:
        raise KoyuraServiceDown("Cannot reach the media engine") from e
    except httpx.TimeoutException as e:
        raise KoyuraInfoError("Info request timed out") from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            logger.error("Media engine rejected the internal token (HTTP %s)", e.response.status_code)
            raise KoyuraServiceDown("Media engine authentication failed") from e
        raise KoyuraInfoError(
            _error_message(e.response, f"Info request failed: {e.response.status_code}")
        ) from e
    except KoyuraError:
        raise
    except Exception as e:
        raise KoyuraInfoError(f"Info request failed: {e}") from e


async def start_download(
    url: str,
    format: str,
    format_id: str | None,
    title: str,
    uploader: str = "",
    height: int | None = None,
    *,
    user_id: int | None = None,
    size_bytes: int | None = None,
    transcode: str | None = None,
) -> dict:
    """Ask the engine for a job and return its full acceptance payload.

    The payload is the engine's JSON as-is: ``job_id`` plus the additive queue
    fields (``status``, ``queue_position``, ``wait_seconds``, ...). Callers that
    only need the id read ``payload["job_id"]``. ``user_id`` lets the engine
    enforce the per-user limits; ``size_bytes`` sharpens the wait estimate.

    ``transcode`` is ``"force"`` when the chat decided to convert and
    ``"never"`` when the file is to be delivered as-is: the engine never
    converts on its own initiative, so the decision travels with the job.
    """
    payload: dict[str, object] = {"url": url, "format": format, "title": title}
    if format_id:
        payload["format_id"] = format_id
    # Additive hints: the engine uses them for the human file name and the MP3
    # tags. An older engine ignores unknown keys, so this stays compatible.
    if uploader:
        payload["uploader"] = uploader
    if height:
        payload["height"] = int(height)
    if user_id is not None:
        payload["user_id"] = int(user_id)
    if size_bytes:
        payload["size_bytes"] = int(size_bytes)
    if transcode in ("force", "never"):
        payload["transcode"] = transcode
    try:
        async with _client() as client:
            resp = await client.post("/api/download", json=payload, timeout=60.0)
            if resp.status_code == 429:
                raise _busy(
                    resp,
                    "The server is busy with other downloads. Please try again in a minute.",
                )
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict) or not body.get("job_id"):
                raise KoyuraDownloadError("Malformed response: no job_id")
            return body
    except httpx.ConnectError as e:
        raise KoyuraServiceDown("Cannot reach the media engine") from e
    except httpx.TimeoutException as e:
        raise KoyuraDownloadError("Download request timed out") from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            logger.error("Media engine rejected the internal token (HTTP %s)", e.response.status_code)
            raise KoyuraServiceDown("Media engine authentication failed") from e
        raise KoyuraDownloadError(
            _error_message(e.response, f"Download request failed: {e.response.status_code}")
        ) from e
    except (KeyError, ValueError) as e:
        raise KoyuraDownloadError(f"Malformed response: {e}") from e
    except KoyuraError:
        raise
    except Exception as e:
        raise KoyuraDownloadError(f"Download request failed: {e}") from e


async def start_transcode(job_id: str) -> dict:
    """Ask the engine to convert a finished job's file to H.264.

    Used when the user first asked for the file (``Send as file``) and then
    pressed *Convert*: the bytes are already on the volume, so only the encoder
    has to run. The answer carries the stage and the estimate the card shows; a
    refusal (already playable, conversion running, unknown job) arrives as a
    :class:`KoyuraError` that the flow renders as the usual error card.
    """
    try:
        async with _client() as client:
            resp = await client.post(f"/api/transcode/{job_id}", timeout=30.0)
            if resp.status_code == 429:
                raise _busy(resp, "The server is busy, try again later")
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                raise KoyuraError("Malformed response: expected an object")
            return body
    except httpx.ConnectError as e:
        raise KoyuraServiceDown("Cannot reach the media engine") from e
    except httpx.TimeoutException as e:
        raise KoyuraError("Conversion request timed out") from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            logger.error(
                "Media engine rejected the internal token (HTTP %s)",
                e.response.status_code,
            )
            raise KoyuraServiceDown("Media engine authentication failed") from e
        raise KoyuraError(
            _error_message(e.response, f"Conversion failed: {e.response.status_code}")
        ) from e
    except KoyuraError:
        raise
    except Exception as e:
        raise KoyuraError(f"Conversion request failed: {e}") from e


async def poll_status(job_id: str) -> dict:
    try:
        async with _client() as client:
            resp = await client.get(f"/api/status/{job_id}", timeout=10.0)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as e:
        raise KoyuraServiceDown("Cannot reach the media engine") from e
    except httpx.TimeoutException as e:
        raise KoyuraError("Status request timed out") from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise KoyuraError("Download job is no longer known to the engine") from e
        if e.response.status_code in (401, 403):
            logger.error("Media engine rejected the internal token (HTTP %s)", e.response.status_code)
            raise KoyuraServiceDown("Media engine authentication failed") from e
        raise KoyuraError(f"Status request failed: {e.response.status_code}") from e
    except KoyuraError:
        raise
    except Exception as e:
        raise KoyuraError(f"Status request failed: {e}") from e


def validate_startup_config() -> None:
    """Fail fast when the internal token is missing and checks are not disabled."""
    if INTERNAL_API_AUTH_DISABLED:
        logger.warning(
            "INTERNAL_API_AUTH_DISABLED is set: calling the engine without a token. "
            "Never do this on a shared network."
        )
        return
    if not INTERNAL_API_TOKEN:
        raise SystemExit(
            "INTERNAL_API_TOKEN is required (must match the media engine). "
            "Set INTERNAL_API_AUTH_DISABLED=true only for local development."
        )
