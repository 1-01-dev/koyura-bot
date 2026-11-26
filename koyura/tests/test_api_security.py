"""API-level tests for the media engine's internal token and input validation."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ["DOWNLOADS_PATH"] = tempfile.mkdtemp()
os.environ.setdefault("MAX_CONCURRENT_DOWNLOADS", "3")

import pytest  # noqa: E402

import app as koyura_app  # noqa: E402
import download_queue  # noqa: E402

TOKEN = os.environ["INTERNAL_API_TOKEN"]
HEADERS = {"X-Internal-Token": TOKEN}


@pytest.fixture()
def client():
    koyura_app.app.config.update(TESTING=True)
    with koyura_app.app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_jobs():
    koyura_app.jobs.clear()
    yield
    koyura_app.jobs.clear()


# ---------------------------------------------------------------------------
# Health endpoint stays public
# ---------------------------------------------------------------------------

def test_healthz_needs_no_token(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Internal token enforcement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/status/does-not-exist"),
        ("post", "/api/info"),
        ("post", "/api/download"),
        ("get", "/api/file/does-not-exist"),
    ],
)
def test_api_rejects_missing_token(client, method, path):
    resp = getattr(client, method)(path, json={"url": "https://example.com/v"})
    assert resp.status_code == 401


def test_api_rejects_wrong_token(client):
    resp = client.post(
        "/api/info",
        json={"url": "https://example.com/v"},
        headers={"X-Internal-Token": "wrong-token"},
    )
    assert resp.status_code == 401


def test_api_accepts_correct_token(client):
    resp = client.get("/api/status/unknown", headers=HEADERS)
    assert resp.status_code == 404


def test_api_fails_closed_without_configured_token(client, monkeypatch):
    monkeypatch.setattr(koyura_app, "INTERNAL_API_TOKEN", "")
    resp = client.get("/api/status/unknown")
    assert resp.status_code == 503


def test_auth_can_be_disabled_explicitly(client, monkeypatch):
    monkeypatch.setattr(koyura_app, "INTERNAL_API_TOKEN", "")
    monkeypatch.setattr(koyura_app, "INTERNAL_API_AUTH_DISABLED", True)
    resp = client.get("/api/status/unknown")
    assert resp.status_code == 404


def test_loopback_ui_download_link_accepts_query_token(client):
    resp = client.get(f"/api/status/unknown?token={TOKEN}")
    # test_client remote_addr is 127.0.0.1, so the query token is honoured.
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/plain;base64,aGk=",
        "",
        "ftp://example.com/v",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/video",
    ],
)
def test_download_rejects_unsafe_urls(client, url):
    resp = client.post("/api/download", json={"url": url}, headers=HEADERS)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_info_rejects_unsafe_urls(client):
    resp = client.post("/api/info", json={"url": "file:///etc/passwd"}, headers=HEADERS)
    assert resp.status_code == 400


def test_download_rejects_unknown_format(client):
    resp = client.post(
        "/api/download",
        json={"url": "https://example.com/v", "format": "exe"},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_download_rejects_injected_format_id(client):
    resp = client.post(
        "/api/download",
        json={"url": "https://example.com/v", "format_id": "22; rm -rf /"},
        headers=HEADERS,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Concurrency limit
#
# The semaphore is gone: admission control now belongs to the queue, which
# starts jobs FIFO and refuses them up front instead of parking the request.
# These tests keep the old externally visible contract (429 + "busy") while
# the queue is saturated.
# ---------------------------------------------------------------------------

@pytest.fixture()
def saturated_queue(monkeypatch):
    """A queue with one slot, no room to wait and a no-op runner."""
    limits = download_queue.Limits(max_concurrent=1, max_queue_length=0)
    queue = download_queue.JobQueue(limits, lambda job_id: None)
    queue.estimator.configure(default_mbps=1000.0, overhead_seconds=0.0)
    monkeypatch.setattr(koyura_app, "job_queue", queue)
    assert queue.submit("holder", user_id=None).accepted
    return queue


def test_download_refuses_when_all_slots_are_busy(client, saturated_queue):
    resp = client.post(
        "/api/download",
        json={"url": "https://example.com/v"},
        headers=HEADERS,
    )

    assert resp.status_code == 429
    body = resp.get_json()
    assert "busy" in body["error"].lower()
    assert body["code"] == "busy"
    assert body["retry_after"] >= 1


def test_download_returns_job_id_when_a_slot_is_free(client, monkeypatch):
    queue = download_queue.JobQueue(
        download_queue.Limits(max_concurrent=1), lambda job_id: None
    )
    monkeypatch.setattr(koyura_app, "job_queue", queue)
    resp = client.post(
        "/api/download",
        json={"url": "https://example.com/v"},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "downloading"
    assert body["job_id"]


def test_unknown_job_returns_404(client):
    resp = client.get("/api/status/missing-job", headers=HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Startup configuration
# ---------------------------------------------------------------------------

def test_startup_requires_token(monkeypatch):
    monkeypatch.setattr(koyura_app, "INTERNAL_API_TOKEN", "")
    monkeypatch.setattr(koyura_app, "INTERNAL_API_AUTH_DISABLED", False)
    with pytest.raises(SystemExit) as excinfo:
        koyura_app.validate_startup_config()
    assert "INTERNAL_API_TOKEN" in str(excinfo.value)


def test_startup_rejects_short_token(monkeypatch):
    monkeypatch.setattr(koyura_app, "INTERNAL_API_TOKEN", "short")
    with pytest.raises(SystemExit) as excinfo:
        koyura_app.validate_startup_config()
    assert "too short" in str(excinfo.value)
