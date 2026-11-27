"""HTTP surface of the Koyura admin panel, exercised end to end.

The app is built once from ``main.create_app()``; every test gets its own
``TestClient`` so cookies, sessions and rate-limit counters never leak between
tests (the previous module-level client made the login tests order-dependent).

Grouped by the contract each block protects:

1. event ingestion — the bot's fire-and-forget POSTs, with and without the
   internal token;
2. read APIs — what the dashboard page polls, and that all of it is behind
   the session;
3. file management — CSRF, path traversal, the purge confirmation;
4. login hardening — CSRF, cookie flags, rate limiting, security headers;
5. download metadata — title/format/quality arrive with the events and reach
   the history table.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

# The environment must be complete BEFORE main/auth/db are imported.
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["DOWNLOADS_PATH"] = _tmpdir
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """Create the file once; ``_conn`` migrates on every call anyway."""
    asyncio.run(db.init_db())


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    """Every TestClient shares one source address, so start each test clean."""
    monkeypatch.setattr(auth, "login_rate_limiter", auth.LoginRateLimiter())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def admin(client: TestClient) -> TestClient:
    """A client that has completed the login form."""
    page = client.get("/login")
    assert page.status_code == 200, page.text
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, f"login failed: {response.status_code} {response.text}"
    return client


def _csrf(client: TestClient) -> str:
    """The signed CSRF token the page set as a cookie (empty before /login)."""
    return str(client.cookies.get("koyura_csrf") or "")


def _delete(client: TestClient, path: str, body: dict):
    """An authenticated DELETE carrying the CSRF header."""
    return client.request(
        "DELETE",
        path,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json", "X-CSRF-Token": _csrf(client)},
    )


def _event(client: TestClient, payload: dict):
    return client.post("/api/events", json=payload)


def _start(client: TestClient, job_id: str, **fields):
    """Send a download_start event, with whatever extra fields a test needs."""
    payload = {
        "type": "download_start",
        "job_id": job_id,
        "user_id": 1,
        "username": "alice",
        "chat_id": 1,
        "url": f"https://example.com/{job_id}.mp4",
        "platform": "youtube",
    }
    payload.update(fields)
    return _event(client, payload)


def _stored(job_id: str):
    row = asyncio.run(db.get_download_by_job_id(job_id))
    assert row is not None, f"{job_id} was not stored"
    return row


# ---------------------------------------------------------------------------
# 1. Event ingestion
# ---------------------------------------------------------------------------

class TestEventIngestion:
    def test_download_start_is_accepted(self, client):
        response = _start(client, "job-start-1", user_id=42, username="alice", chat_id=99)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert _stored("job-start-1")["username"] == "alice"

    def test_older_nested_envelope_still_works(self, client):
        response = _event(client, {
            "type": "download_start",
            "data": {
                "job_id": "job-nested-1",
                "user_id": 7,
                "username": "nested",
                "chat_id": 7,
                "url": "https://example.com/nested.mp4",
            },
        })

        assert response.status_code == 200
        assert _stored("job-nested-1")["username"] == "nested"

    def test_missing_job_id_is_rejected(self, client):
        assert _event(client, {"type": "download_start"}).status_code == 400

    def test_missing_type_is_rejected(self, client):
        assert _event(client, {"job_id": "job-no-type"}).status_code == 400

    def test_non_json_body_is_rejected(self, client):
        response = client.post(
            "/api/events",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400

    def test_progress_is_accepted_for_a_started_job(self, client):
        _start(client, "job-progress-1")
        response = _event(client, {
            "type": "progress",
            "job_id": "job-progress-1",
            "percent": 50.0,
            "speed": 1024,
            "eta": 5,
        })

        assert response.status_code == 200

    def test_done_is_accepted_and_closes_the_job(self, client):
        _start(client, "job-done-1", platform="tiktok")
        response = _event(client, {
            "type": "download_done",
            "job_id": "job-done-1",
            "file_size_bytes": 1024000,
            "duration_seconds": 3.5,
        })

        assert response.status_code == 200
        row = _stored("job-done-1")
        assert row["status"] == "done"
        assert row["file_size_bytes"] == 1024000
        assert abs(row["download_duration_sec"] - 3.5) < 0.01

    def test_error_is_accepted_and_closes_the_job(self, client):
        _start(client, "job-err-1")
        response = _event(client, {
            "type": "download_error",
            "job_id": "job-err-1",
            "error_message": "HTTP 403 forbidden",
        })

        assert response.status_code == 200
        row = _stored("job-err-1")
        assert row["status"] == "error"
        assert row["error_message"] == "HTTP 403 forbidden"

    def test_internal_token_is_enforced_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "token-" + "x" * 24)

        refused = _event(client, {"type": "download_start", "job_id": "job-token-1"})
        assert refused.status_code == 401
        assert asyncio.run(db.get_download_by_job_id("job-token-1")) is None

        accepted = client.post(
            "/api/events",
            json={"type": "download_start", "job_id": "job-token-2"},
            headers={"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]},
        )
        assert accepted.status_code == 200


# ---------------------------------------------------------------------------
# 2. Read APIs (behind the session)
# ---------------------------------------------------------------------------

class TestReadApis:
    def test_dashboard_stats_requires_a_session(self, client):
        assert client.get("/api/dashboard-stats").status_code == 401

    def test_dashboard_stats_returns_the_kpi_block(self, admin):
        response = admin.get("/api/dashboard-stats")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"stats", "disk"}
        assert "downloads_today" in body["stats"]
        assert "active_users_24h" in body["stats"]
        assert "error_rate" in body["stats"]

    def test_chart_data_requires_a_session(self, client):
        assert client.get("/api/chart-data?range=1D").status_code == 401

    @pytest.mark.parametrize("range_key", ["1D", "7D", "1M", "1Y"])
    def test_chart_data_serves_every_range(self, admin, range_key):
        response = admin.get(f"/api/chart-data?range={range_key}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["labels"]) == len(body["values"])

    def test_chart_data_rejects_an_unknown_range(self, admin):
        assert admin.get("/api/chart-data?range=2Y").status_code == 400

    def test_active_downloads_requires_a_session(self, client):
        assert client.get("/api/active-downloads").status_code == 401

    def test_active_downloads_lists_a_running_job(self, client, admin):
        _start(client, "job-active-1", user_id=10, username="eve", platform="instagram")

        response = admin.get("/api/active-downloads")
        assert response.status_code == 200
        assert "job-active-1" in [entry["job_id"] for entry in response.json()]

    def test_a_finished_job_leaves_the_active_list(self, client, admin):
        _start(client, "job-active-2")
        _event(client, {"type": "download_done", "job_id": "job-active-2",
                        "file_size_bytes": 1, "duration_seconds": 1})

        active = admin.get("/api/active-downloads").json()
        assert "job-active-2" not in [entry["job_id"] for entry in active]


# ---------------------------------------------------------------------------
# 3. File management (session + CSRF)
# ---------------------------------------------------------------------------

class TestFileManagement:
    def test_delete_removes_a_file(self, admin):
        target = Path(_tmpdir) / "test_delete_me.mp4"
        target.write_bytes(b"fake video content")

        response = _delete(admin, "/api/files", {"paths": ["test_delete_me.mp4"]})

        assert response.status_code == 200
        assert "test_delete_me.mp4" in response.json()["deleted"]
        assert not target.exists()

    def test_delete_reports_a_missing_file_without_failing(self, admin):
        response = _delete(admin, "/api/files", {"paths": ["never-existed.mp4"]})

        assert response.status_code == 200
        assert response.json()["deleted"] == []
        assert response.json()["errors"][0]["error"] == "not found"

    def test_delete_requires_a_csrf_header(self, admin):
        response = admin.request(
            "DELETE",
            "/api/files",
            content=json.dumps({"paths": ["x.mp4"]}).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 403

    def test_delete_requires_a_session(self, client):
        response = client.request(
            "DELETE",
            "/api/files",
            content=json.dumps({"paths": ["something.mp4"]}).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 401

    def test_delete_ignores_path_traversal(self, admin):
        outside = Path(_tmpdir).parent / "outside-secret.mp4"
        outside.write_bytes(b"do not delete")

        response = _delete(admin, "/api/files", {"paths": ["../../outside-secret.mp4"]})

        # Only the basename is used, and that does not exist in DOWNLOADS_PATH.
        assert response.status_code == 200
        assert outside.exists()
        outside.unlink()

    def test_purge_needs_the_confirmation_word(self, admin):
        assert _delete(admin, "/api/files/all", {}).status_code == 400
        assert _delete(admin, "/api/files/all", {"confirm": "yes"}).status_code == 400

    def test_purge_with_confirmation_clears_the_directory(self, admin):
        target = Path(_tmpdir) / "purge_me.mp4"
        target.write_bytes(b"content to be purged")

        response = _delete(admin, "/api/files/all", {"confirm": "PURGE"})

        assert response.status_code == 200
        assert "deleted_count" in response.json()
        assert not target.exists()

    def test_purge_requires_a_session(self, client):
        response = client.request(
            "DELETE",
            "/api/files/all",
            content=json.dumps({"confirm": "PURGE"}).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# 4. Login hardening
# ---------------------------------------------------------------------------

class TestLogin:
    def test_successful_login_sets_a_hardened_session_cookie(self, client):
        client.get("/login")
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": os.environ["ADMIN_PASSWORD"],
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        cookie = response.headers.get("set-cookie", "")
        assert "koyura_session=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie

    def test_login_without_a_csrf_token_is_refused(self, client):
        client.get("/login")
        response = client.post(
            "/login",
            data={"username": "admin", "password": os.environ["ADMIN_PASSWORD"]},
        )
        assert response.status_code == 403

    def test_wrong_password_is_refused_and_the_page_keeps_the_message(self, client):
        page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "definitely-wrong",
                "csrf_token": _csrf(client),
            },
        )

        assert response.status_code == 401
        assert "Invalid credentials" in response.text
        assert "definitely-wrong" not in response.text, "the password must not be echoed"
        assert page.text.count("csrf_token") >= 1

    def test_repeated_failures_are_rate_limited(self, client):
        client.get("/login")
        for _ in range(5):
            client.post(
                "/login",
                data={"username": "admin", "password": "wrong", "csrf_token": _csrf(client)},
            )

        blocked = client.post(
            "/login",
            data={
                "username": "admin",
                "password": os.environ["ADMIN_PASSWORD"],
                "csrf_token": _csrf(client),
            },
        )
        assert blocked.status_code == 429

    def test_rate_limiter_counts_resets_and_unblocks(self):
        from auth import LoginRateLimiter

        limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
        limiter.record_failure("1.2.3.4")
        assert not limiter.is_blocked("1.2.3.4")

        limiter.record_failure("1.2.3.4")
        assert limiter.is_blocked("1.2.3.4")

        limiter.reset("1.2.3.4")
        assert not limiter.is_blocked("1.2.3.4")

    @pytest.mark.parametrize(
        "path",
        ["/", "/history", "/errors", "/admin", "/admin/storage",
         "/admin/limits", "/admin/bot", "/admin/system"],
    )
    def test_pages_redirect_to_the_login_form(self, client, path):
        response = client.get(path, follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_logged_in_pages_render(self, admin):
        for path in ("/", "/history", "/errors", "/admin", "/admin/storage",
                     "/admin/limits", "/admin/bot", "/admin/system"):
            response = admin.get(path)
            assert response.status_code == 200, path

    def test_logout_ends_the_session(self, admin):
        response = admin.post(
            "/logout", data={"csrf_token": _csrf(admin)}, follow_redirects=False
        )

        assert response.status_code == 303
        assert admin.get("/api/dashboard-stats").status_code == 401

    def test_security_headers_are_present(self, client):
        headers = client.get("/login").headers

        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]


# ---------------------------------------------------------------------------
# 5. Download metadata: events -> history table
# ---------------------------------------------------------------------------

class TestDownloadMetadata:
    def test_start_stores_title_format_and_quality(self, client):
        response = _start(
            client,
            "job-meta-start",
            user_id=55,
            username="meta",
            platform="Youtube",
            title="City walk at sunset",
            format="video",
            quality="1080p",
        )

        assert response.status_code == 200
        row = _stored("job-meta-start")
        assert row["title"] == "City walk at sunset"
        assert row["format"] == "video"
        assert row["quality"] == "1080p"

    def test_start_without_metadata_leaves_the_columns_empty(self, client):
        _start(client, "job-meta-empty", user_id=56, username="meta")

        row = _stored("job-meta-empty")
        assert row["title"] is None
        assert row["format"] is None

    def test_progress_backfills_a_late_title(self, client, admin):
        _start(client, "job-meta-late", user_id=57, username="meta", platform="Tiktok")
        assert _stored("job-meta-late")["title"] is None

        response = _event(client, {
            "type": "progress",
            "job_id": "job-meta-late",
            "percent": 12.5,
            "title": "Late metadata title",
        })

        assert response.status_code == 200
        assert _stored("job-meta-late")["title"] == "Late metadata title"

        active = admin.get("/api/active-downloads").json()
        entry = next(item for item in active if item["job_id"] == "job-meta-late")
        assert entry["title"] == "Late metadata title"

    def test_progress_without_a_title_keeps_the_stored_one(self, client):
        _start(client, "job-meta-keep", title="Keep me")
        _event(client, {"type": "progress", "job_id": "job-meta-keep", "percent": 50})

        assert _stored("job-meta-keep")["title"] == "Keep me"

    def test_history_page_shows_the_metadata(self, admin):
        _start(
            admin,
            "job-meta-history",
            user_id=58,
            username="meta",
            platform="Vimeo",
            title="A visible history title",
            format="audio",
            quality="720p",
        )

        response = admin.get("/history")

        assert response.status_code == 200
        assert "A visible history title" in response.text
        assert "audio" in response.text
        assert "720p" in response.text
        assert ">None<" not in response.text

    def test_history_page_uses_a_placeholder_for_a_missing_title(self, admin):
        _start(admin, "job-meta-notitle", title="")

        response = admin.get("/history", params={"user_filter": ""})

        assert response.status_code == 200
        assert ">None<" not in response.text
        assert "—" in response.text

    def test_history_page_applies_the_user_filter(self, admin):
        _start(admin, "job-filter-match", username="findme", title="Filtered row")
        _start(admin, "job-filter-other", username="someoneelse", title="Hidden row")

        response = admin.get("/history", params={"user_filter": "findme"})

        assert response.status_code == 200
        assert "Filtered row" in response.text
        assert "Hidden row" not in response.text


def test_history_date_today_shortcut_filters_the_day(admin):
    """The KPI cells link to /history?date=today; the link has to mean it."""
    _start(admin, "job-today", title="Today row")
    _start(admin, "job-long-ago", title="Ancient row")
    with sqlite3.connect(os.environ["DB_PATH"]) as connection:
        connection.execute(
            "UPDATE downloads SET started_at = '2020-01-01 10:00:00' WHERE job_id = ?",
            ("job-long-ago",),
        )

    response = admin.get("/history", params={"date": "today"})

    assert response.status_code == 200
    assert "Today row" in response.text
    assert "Ancient row" not in response.text


def test_history_pagination_keeps_the_date_filter(admin):
    """Turning the page must not turn the filter off."""
    for index in range(25):
        _start(admin, f"job-page-{index}", title=f"Paged row {index}")

    first = admin.get("/history", params={"date": "today", "page": 1})
    assert 'page=2' in first.text
    assert "date_from=" in first.text and "date_to=" in first.text


# ---------------------------------------------------------------------------
# CSRF plumbing shared by the forms
# ---------------------------------------------------------------------------

def test_every_rendered_form_carries_a_csrf_token(admin):
    for path in ("/history", "/errors", "/admin", "/admin/limits", "/admin/bot"):
        html = admin.get(path).text
        assert re.search(r'name="csrf_token" value="[^"]+"', html), path
