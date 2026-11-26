"""API contract tests for the queue endpoints and the added status fields.

The queue itself is unit tested in ``test_download_queue.py``; here the point is
the HTTP surface the bot and the web UI consume: status codes, refusal codes,
``retry_after`` and the additive fields that must not break older callers.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ["DOWNLOADS_PATH"] = tempfile.mkdtemp()
os.environ.setdefault("MAX_CONCURRENT_DOWNLOADS", "2")

import pytest  # noqa: E402

import app as koyura_app  # noqa: E402
import download_queue  # noqa: E402

HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}
URL = "https://example.com/v"


@pytest.fixture()
def client():
    koyura_app.app.config.update(TESTING=True)
    with koyura_app.app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test gets an empty job registry and a fresh, predictable queue."""
    koyura_app.jobs.clear()
    koyura_app.job_queue.stop()
    queue = download_queue.JobQueue(
        download_queue.Limits(max_concurrent=1, max_wait_seconds=900),
        lambda job_id: None,
    )
    queue.estimator.configure(default_mbps=1000.0, overhead_seconds=0.0)
    koyura_app.job_queue = queue
    koyura_app.limits = queue.limits
    yield
    koyura_app.jobs.clear()
    koyura_app.job_queue.stop()


def _download(client, **payload):
    body = {"url": URL, **payload}
    return client.post("/api/download", json=body, headers=HEADERS)


# ---------------------------------------------------------------------------
# POST /api/download
# ---------------------------------------------------------------------------

class TestDownloadAdmission:
    def test_accepted_job_reports_downloading(self, client):
        resp = _download(client, user_id=1)
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["job_id"]
        assert body["status"] == "downloading"
        assert body["queue_position"] is None
        # Additive fields exist even when the job did not wait.
        assert body["wait_seconds"] == 0
        assert "queue_estimate_seconds" in body

    def test_queued_job_reports_position_and_wait(self, client):
        assert _download(client, user_id=1).status_code == 202
        resp = _download(client, user_id=2)
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["status"] == "queued"
        assert body["queue_position"] == 1
        assert isinstance(body["wait_seconds"], int)
        assert body["queue_length"] == 1

    def test_refusal_is_429_with_code_and_retry_after(self, client):
        assert _download(client, user_id=1).status_code == 202
        assert _download(client, user_id=2).status_code == 202  # queued

        koyura_app.job_queue.apply_limits(
            koyura_app.job_queue.limits.copy(max_queue_length=1)
        )
        resp = _download(client, user_id=3)

        assert resp.status_code == 429
        body = resp.get_json()
        assert body["code"] == "queue_full"
        assert body["queue_position"] is None
        assert body["retry_after"] >= 1
        assert body["wait_seconds"] is not None
        assert "error" in body
        assert resp.headers["Retry-After"] == str(body["retry_after"])

    def test_second_request_from_the_same_user_is_refused(self, client):
        assert _download(client, user_id=42).status_code == 202
        resp = _download(client, user_id=42)
        assert resp.status_code == 429
        assert resp.get_json()["code"] == "user_busy"

    def test_too_long_refusal_uses_the_wait_budget(self, client):
        koyura_app.job_queue.apply_limits(
            koyura_app.job_queue.limits.copy(max_wait_seconds=60)
        )
        koyura_app.job_queue.estimator.record("video", None, 300.0)
        assert _download(client, user_id=1).status_code == 202
        resp = _download(client, user_id=2)
        assert resp.status_code == 429
        assert resp.get_json()["code"] == "too_long"

    def test_refused_job_is_not_registered(self, client):
        assert _download(client, user_id=1).status_code == 202
        before = set(koyura_app.jobs)
        _download(client, user_id=1)
        assert set(koyura_app.jobs) == before

    def test_old_payload_shape_still_works(self, client):
        """A caller that knows nothing about the queue keeps working."""
        resp = client.post(
            "/api/download",
            json={"url": URL, "format": "video", "title": "x"},
            headers=HEADERS,
        )
        assert resp.status_code == 202
        assert set(resp.get_json()) >= {"job_id", "status"}


# ---------------------------------------------------------------------------
# GET /api/queue
# ---------------------------------------------------------------------------

class TestQueueEndpoint:
    def test_queue_requires_the_internal_token(self, client):
        assert client.get("/api/queue").status_code == 401
        assert client.get("/api/queue?token=wrong").status_code == 401

    def test_queue_snapshot_shape(self, client):
        _download(client, user_id=1)
        _download(client, user_id=2)
        body = client.get("/api/queue", headers=HEADERS).get_json()
        assert set(body) == {"active", "queued", "limits", "counts", "next_wait_seconds", "estimator"}
        assert body["counts"]["active"] == 1
        assert body["counts"]["queued"] == 1
        entry = body["queued"][0]
        assert entry["position"] == 1
        for key in ("job_id", "user_id", "position", "estimate_seconds"):
            assert key in entry
        for key in (
            "max_concurrent",
            "per_user_active",
            "per_user_queued",
            "max_queue_length",
            "max_wait_seconds",
            "estimate_mbps",
        ):
            assert key in body["limits"]


# ---------------------------------------------------------------------------
# GET /api/status/<job_id>
# ---------------------------------------------------------------------------

class TestStatusFields:
    def test_queued_job_status_shows_its_place(self, client):
        _download(client, user_id=1)
        job_id = _download(client, user_id=2).get_json()["job_id"]

        body = client.get(f"/api/status/{job_id}", headers=HEADERS).get_json()
        assert body["status"] == "queued"
        assert body["queue_position"] == 1
        assert isinstance(body["wait_seconds"], int)
        assert body["queue_estimate_seconds"] is not None
        assert body["elapsed"] == 0.0

    def test_downloading_job_has_no_position(self, client):
        job_id = _download(client, user_id=1).get_json()["job_id"]
        body = client.get(f"/api/status/{job_id}", headers=HEADERS).get_json()
        assert body["status"] == "downloading"
        assert body["queue_position"] is None

    def test_status_keeps_the_historical_keys(self, client):
        job_id = _download(client, user_id=1).get_json()["job_id"]
        body = client.get(f"/api/status/{job_id}", headers=HEADERS).get_json()
        for key in (
            "status",
            "error",
            "filename",
            "progress",
            "file_path",
            "file_size_bytes",
            "width",
            "height",
            "duration",
        ):
            assert key in body

    def test_total_bytes_estimate_is_echoed(self, client):
        job_id = _download(client, user_id=1, size_bytes=4096).get_json()["job_id"]
        body = client.get(f"/api/status/{job_id}", headers=HEADERS).get_json()
        assert body["total_bytes_estimate"] == 4096

    def test_queue_expiry_fails_the_job_with_a_reason(self, client):
        clock = [1000.0]
        queue = download_queue.JobQueue(
            download_queue.Limits(max_concurrent=1, max_wait_seconds=900, expiry_seconds=60),
            lambda job_id: None,
            clock=lambda: clock[0],
            on_expire=koyura_app._on_queue_expired,
        )
        koyura_app.job_queue = queue

        assert queue.submit("holder", user_id=1).status == "downloading"
        assert queue.submit("expiring", user_id=9).status == "queued"
        with koyura_app._jobs_lock:
            koyura_app.jobs["expiring"] = {
                "status": "queued",
                "url": URL,
                "created": 1000.0,
                "format": "video",
                "format_id": None,
            }

        clock[0] += 61
        queue.tick()

        body = client.get("/api/status/expiring", headers=HEADERS).get_json()
        assert body["status"] == "error"
        assert "queue" in body["error"].lower()


# ---------------------------------------------------------------------------
# POST /api/settings
# ---------------------------------------------------------------------------

class TestSettingsEndpoint:
    def test_settings_require_the_internal_token(self, client):
        assert client.post("/api/settings", json={"max_concurrent": 2}).status_code == 401

    def test_settings_change_the_limits_at_runtime(self, client):
        resp = client.post(
            "/api/settings",
            json={"max_concurrent": 3, "QUEUE_MAX_WAIT_SECONDS": 200},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        limits = resp.get_json()["limits"]
        assert limits["max_concurrent"] == 3
        assert limits["max_wait_seconds"] == 200
        # The live queue is what actually enforces it.
        assert koyura_app.job_queue.limits.max_concurrent == 3

    def test_settings_reject_out_of_range_values_with_a_sentence(self, client):
        resp = client.post(
            "/api/settings", json={"max_concurrent": 99}, headers=HEADERS
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert "between 1 and 8" in body["error"]
        assert body["errors"]

    def test_settings_accept_env_style_names(self, client):
        resp = client.post(
            "/api/settings",
            json={"PER_USER_MAX_ACTIVE": 2, "MAX_CONCURRENT_DOWNLOADS": 4},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.get_json()["limits"]["per_user_active"] == 2

    def test_settings_do_not_touch_running_jobs(self, client):
        job_id = _download(client, user_id=1).get_json()["job_id"]
        client.post("/api/settings", json={"max_concurrent": 1}, headers=HEADERS)
        body = client.get(f"/api/status/{job_id}", headers=HEADERS).get_json()
        assert body["status"] == "downloading"

    def test_settings_are_reflected_in_the_queue_snapshot(self, client):
        client.post("/api/settings", json={"max_queue_length": 11}, headers=HEADERS)
        body = client.get("/api/queue", headers=HEADERS).get_json()
        assert body["limits"]["max_queue_length"] == 11
