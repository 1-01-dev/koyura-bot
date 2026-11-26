"""Behaviour of the bot's media-engine client.

Everything below runs against a stubbed transport: the client is the only place
that knows the engine's HTTP surface, so the tests pin what the bot sends, how
each failure is classified, and that a refusal (429) keeps the engine's own
wording and wait estimate.

The engine is reached through ``koyura_client._client()``; replacing that one
seam keeps every assertion below about real client code instead of mocks.
"""
from __future__ import annotations

import contextlib
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import koyura_client
from koyura_client import (
    KoyuraBusy,
    KoyuraDownloadError,
    KoyuraError,
    KoyuraInfoError,
    KoyuraServiceDown,
    auth_headers,
    get_info,
    poll_status,
    start_download,
    validate_startup_config,
)

URL = "https://www.youtube.com/watch?v=KOYURA00001"


# ---------------------------------------------------------------------------
# Stubbed transport
# ---------------------------------------------------------------------------

def _response(status: int = 200, body: Any = None) -> MagicMock:
    """A stand-in httpx.Response that raises like the real one on 4xx/5xx."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json.return_value = {} if body is None else body
    if status >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


class _Transport:
    """Records what the client asked for and answers with a fixed result."""

    def __init__(self, result: MagicMock | Exception | None) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple, dict]] = []

    async def _answer(self, verb: str, args: tuple, kwargs: dict) -> MagicMock:
        self.calls.append((verb, args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        assert self.result is not None, f"{verb} was not expected"
        return self.result

    async def post(self, *args, **kwargs):
        return await self._answer("post", args, kwargs)

    async def get(self, *args, **kwargs):
        return await self._answer("get", args, kwargs)

    async def __aenter__(self) -> _Transport:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


@contextlib.contextmanager
def transport(result: MagicMock | Exception | None = None):
    """Patch the client seam and hand back the recorder."""
    fake = _Transport(result)
    with patch.object(koyura_client, "_client", lambda: fake):
        yield fake


def _single_call(fake: _Transport) -> dict:
    assert len(fake.calls) == 1, fake.calls
    return fake.calls[0][2]


# ---------------------------------------------------------------------------
# get_info
# ---------------------------------------------------------------------------

class TestGetInfo:
    @pytest.mark.asyncio
    async def test_returns_the_engine_payload(self):
        body = {
            "title": "Test Video",
            "thumbnail": "https://example.com/thumb.jpg",
            "duration": 120,
            "uploader": "TestUser",
            "extractor": "youtube",
            "formats": [{"id": "22", "label": "720p", "height": 720}],
        }
        with transport(_response(200, body)) as fake:
            info = await get_info(URL)

        assert info["title"] == "Test Video"
        assert info["extractor"] == "youtube"
        assert info["formats"] == body["formats"]
        call = _single_call(fake)
        assert fake.calls[0][1][0] == "/api/info"
        assert call["json"] == {"url": URL}
        # Metadata probing is slow on the engine side; the client waits a minute.
        assert call["timeout"] == 60.0

    @pytest.mark.asyncio
    async def test_unreachable_engine_is_a_service_failure(self):
        with transport(httpx.ConnectError("refused")), pytest.raises(KoyuraServiceDown):
            await get_info(URL)

    @pytest.mark.asyncio
    async def test_timeout_is_reported_as_an_info_failure(self):
        with transport(httpx.TimeoutException("timeout")), pytest.raises(
            KoyuraInfoError, match="timed out"
        ):
            await get_info(URL)

    @pytest.mark.asyncio
    async def test_koyura_error_message_is_forwarded(self):
        with transport(_response(400, {"error": "Unsupported URL"})), pytest.raises(
            KoyuraInfoError, match="Unsupported URL"
        ):
            await get_info("https://invalid-site.com/nope")

    @pytest.mark.asyncio
    async def test_body_without_error_field_falls_back_to_the_status(self):
        with transport(_response(500, {})), pytest.raises(KoyuraInfoError, match="500"):
            await get_info(URL)

    @pytest.mark.asyncio
    async def test_rejected_token_is_not_reported_as_a_bad_url(self):
        """A 401/403 is a deployment problem, not a link the user can fix."""
        with transport(_response(401, {"error": "Unauthorized"})), pytest.raises(
            KoyuraServiceDown, match="authentication"
        ):
            await get_info(URL)

    @pytest.mark.asyncio
    async def test_busy_engine_keeps_its_code_and_wait_estimate(self):
        refusal = _response(
            429, {"error": "All slots are busy", "code": "busy", "retry_after": 45}
        )
        with transport(refusal), pytest.raises(KoyuraBusy) as caught:
            await get_info(URL)

        assert caught.value.code == "busy"
        assert caught.value.retry_after == 45
        assert caught.value.wait_seconds == 45
        assert str(caught.value) == "All slots are busy"


# ---------------------------------------------------------------------------
# start_download
# ---------------------------------------------------------------------------

class TestStartDownload:
    @pytest.mark.asyncio
    async def test_returns_the_acceptance_payload(self):
        accepted = {"job_id": "abc1234567", "status": "queued", "queue_position": 2}
        with transport(_response(200, accepted)) as fake:
            result = await start_download(URL, "video", "22", "Test")

        assert result["job_id"] == "abc1234567"
        assert result["queue_position"] == 2
        assert fake.calls[0][1][0] == "/api/download"

    @pytest.mark.asyncio
    async def test_forwards_uploader_height_and_user_hints(self):
        """The engine needs these for the human file name and the per-user limits."""
        with transport(_response(200, {"job_id": "abc1234567"})) as fake:
            await start_download(
                URL, "video", "137", "Test",
                uploader="NASA", height=1080,
                user_id=4242, size_bytes=123456789,
            )

        payload = _single_call(fake)["json"]
        assert payload["format_id"] == "137"
        assert payload["uploader"] == "NASA"
        assert payload["height"] == 1080
        assert payload["user_id"] == 4242
        assert payload["size_bytes"] == 123456789

    @pytest.mark.asyncio
    async def test_omits_empty_hints(self):
        """An older engine must keep receiving the historical payload shape."""
        with transport(_response(200, {"job_id": "abc1234567"})) as fake:
            await start_download(URL, "audio", None, "Test")

        payload = _single_call(fake)["json"]
        assert set(payload) == {"url", "format", "title"}
        assert payload["format"] == "audio"

    @pytest.mark.asyncio
    async def test_unreachable_engine_is_a_service_failure(self):
        with transport(httpx.ConnectError("refused")), pytest.raises(KoyuraServiceDown):
            await start_download(URL, "video", None, "Test")

    @pytest.mark.asyncio
    async def test_timeout_is_a_download_failure(self):
        with transport(httpx.TimeoutException("timeout")), pytest.raises(
            KoyuraDownloadError, match="timed out"
        ):
            await start_download(URL, "video", None, "Test")

    @pytest.mark.asyncio
    async def test_answer_without_a_job_id_is_rejected(self):
        """A 200 with the wrong body must not turn into a polling loop."""
        with transport(_response(200, {"detail": "accepted"})), pytest.raises(
            KoyuraDownloadError, match="no job_id"
        ):
            await start_download(URL, "video", None, "Test")

    @pytest.mark.asyncio
    async def test_koyura_refusal_is_busy_not_a_plain_error(self):
        refusal = _response(429, {"error": "Queue is full", "code": "queue_full"})
        with transport(refusal), pytest.raises(KoyuraBusy) as caught:
            await start_download(URL, "video", None, "Test")

        assert caught.value.code == "queue_full"
        # The card can still say something useful when no estimate is given.
        assert caught.value.retry_after is None

    @pytest.mark.asyncio
    async def test_koyura_rejection_uses_the_engine_wording(self):
        with transport(
            _response(400, {"error": "Video is longer than allowed"})
        ), pytest.raises(KoyuraDownloadError, match="longer than allowed"):
            await start_download(URL, "video", None, "Test")

    @pytest.mark.asyncio
    async def test_rejected_token_is_a_service_failure(self):
        with transport(_response(403, {})), pytest.raises(
            KoyuraServiceDown, match="authentication"
        ):
            await start_download(URL, "video", None, "Test")


# ---------------------------------------------------------------------------
# poll_status
# ---------------------------------------------------------------------------

class TestPollStatus:
    @pytest.mark.asyncio
    async def test_finished_job_carries_the_file_metadata(self):
        body = {
            "status": "done",
            "filename": "test.mp4",
            "file_path": "/downloads/abc1234567.mp4",
            "progress": None,
        }
        with transport(_response(200, body)) as fake:
            result = await poll_status("abc1234567")

        assert result["status"] == "done"
        assert result["file_path"] == "/downloads/abc1234567.mp4"
        assert fake.calls[0][1][0] == "/api/status/abc1234567"
        assert fake.calls[0][2]["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_running_job_keeps_the_progress_block(self):
        body = {
            "status": "downloading",
            "progress": {
                "percent": 45.2,
                "downloaded_bytes": 23000000,
                "total_bytes": 51000000,
            },
        }
        with transport(_response(200, body)):
            result = await poll_status("abc1234567")

        assert result["status"] == "downloading"
        assert result["progress"]["percent"] == 45.2

    @pytest.mark.asyncio
    async def test_failed_job_keeps_the_error_text(self):
        body = {"status": "error", "error": "Download timed out (5 min limit)"}
        with transport(_response(200, body)):
            result = await poll_status("abc1234567")

        assert result["status"] == "error"
        assert result["error"] == "Download timed out (5 min limit)"

    @pytest.mark.asyncio
    async def test_unknown_job_is_a_recoverable_error(self):
        """The engine forgets jobs on restart; the bot must stop polling."""
        with transport(_response(404, {"error": "unknown job"})), pytest.raises(
            KoyuraError, match="no longer known"
        ):
            await poll_status("abc1234567")

    @pytest.mark.asyncio
    async def test_unreachable_engine_is_a_service_failure(self):
        with transport(httpx.ConnectError("refused")), pytest.raises(KoyuraServiceDown):
            await poll_status("abc1234567")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_auth_header_carries_the_internal_token(monkeypatch):
    monkeypatch.setattr(koyura_client, "INTERNAL_API_TOKEN", "secret-token")
    assert auth_headers() == {"X-Internal-Token": "secret-token"}


def test_auth_header_is_empty_without_a_token(monkeypatch):
    monkeypatch.setattr(koyura_client, "INTERNAL_API_TOKEN", "")
    assert auth_headers() == {}


def test_startup_refuses_to_run_without_a_token(monkeypatch):
    monkeypatch.setattr(koyura_client, "INTERNAL_API_TOKEN", "")
    monkeypatch.setattr(koyura_client, "INTERNAL_API_AUTH_DISABLED", False)
    with pytest.raises(SystemExit, match="INTERNAL_API_TOKEN"):
        validate_startup_config()


def test_startup_allows_the_documented_sandbox_escape_hatch(monkeypatch):
    monkeypatch.setattr(koyura_client, "INTERNAL_API_TOKEN", "")
    monkeypatch.setattr(koyura_client, "INTERNAL_API_AUTH_DISABLED", True)
    validate_startup_config()  # warns, does not raise


# ---------------------------------------------------------------------------
# Engine address
# ---------------------------------------------------------------------------

def test_the_address_defaults_to_the_compose_service(monkeypatch):
    monkeypatch.delenv("KOYURA_ENGINE_URL", raising=False)
    assert koyura_client.engine_url() == "http://koyura-engine:8899"


def test_the_address_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("KOYURA_ENGINE_URL", "http://elsewhere:8899")
    assert koyura_client.engine_url() == "http://elsewhere:8899"


@pytest.mark.asyncio
async def test_the_http_client_targets_that_address(monkeypatch):
    monkeypatch.setenv("KOYURA_ENGINE_URL", "http://elsewhere:8899")
    async with koyura_client._client() as client:
        assert str(client.base_url).rstrip("/") == "http://elsewhere:8899"
