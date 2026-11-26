"""Tests for dashboard authentication, session cookies and CSRF."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")

import auth  # noqa: E402

GOOD_SECRET = "a" * 40
CREDENTIAL = "Koyura-Test-" + "Passw0rd"  # fixture value, not a real secret


def _request(path: str = "/", scheme: str = "http", headers: dict | None = None):
    from starlette.requests import Request

    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("dashboard", 8080),
        "root_path": "",
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Startup validation: no insecure defaults
# ---------------------------------------------------------------------------

def test_missing_secret_key_fails(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", CREDENTIAL)
    with pytest.raises(RuntimeError, match="SECRET_KEY is required"):
        auth.validate_config()


def test_short_secret_key_fails(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "too-short")
    monkeypatch.setenv("ADMIN_PASSWORD", CREDENTIAL)
    with pytest.raises(RuntimeError, match="too short"):
        auth.validate_config()


def test_placeholder_secret_key_fails(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
    monkeypatch.setenv("ADMIN_PASSWORD", CREDENTIAL)
    with pytest.raises(RuntimeError, match="placeholder"):
        auth.validate_config()


def test_missing_password_fails(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        auth.validate_config()


def test_short_password_fails(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    with pytest.raises(RuntimeError, match="too short"):
        auth.validate_config()


def test_placeholder_password_fails(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    with pytest.raises(RuntimeError, match="placeholder"):
        auth.validate_config()


def test_valid_plain_password_passes(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", CREDENTIAL)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    auth.validate_config()  # must not raise


# ---------------------------------------------------------------------------
# Password verification
# ---------------------------------------------------------------------------

def test_plain_password_verification(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", CREDENTIAL)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    assert auth.verify_credentials("admin", CREDENTIAL)
    assert not auth.verify_credentials("admin", "wrong-password")
    assert not auth.verify_credentials("root", CREDENTIAL)


def test_password_hash_verification(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password(CREDENTIAL))
    assert auth.verify_credentials("admin", CREDENTIAL)
    assert not auth.verify_credentials("admin", "nope")


def test_malformed_password_hash_fails_closed(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "not-a-valid-hash")
    assert auth.verify_password(CREDENTIAL) is False


# ---------------------------------------------------------------------------
# Session cookies
# ---------------------------------------------------------------------------

def _set_cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


def test_session_cookie_is_hardened(monkeypatch):
    from fastapi.responses import Response

    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", CREDENTIAL)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "auto")
    response = Response()
    auth.create_session_cookie(response, "admin", _request(scheme="https"))
    header = _set_cookie_header(response)
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Secure" in header
    assert "path=/" in header.lower()


def test_session_cookie_not_secure_over_http(monkeypatch):
    from fastapi.responses import Response

    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "auto")
    response = Response()
    auth.create_session_cookie(response, "admin", _request(scheme="http"))
    assert "Secure" not in _set_cookie_header(response)


def test_session_cookie_respects_forwarded_proto(monkeypatch):
    from fastapi.responses import Response

    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "auto")
    response = Response()
    auth.create_session_cookie(
        response, "admin", _request(scheme="http", headers={"x-forwarded-proto": "https"})
    )
    assert "Secure" in _set_cookie_header(response)


def test_session_roundtrip(monkeypatch):
    from fastapi.responses import Response

    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("SESSION_MAX_AGE_SECONDS", "3600")
    response = Response()
    auth.create_session_cookie(response, "admin", _request())
    header = _set_cookie_header(response)
    token = header.split("koyura_session=")[1].split(";")[0]

    request = _request(headers={"cookie": f"koyura_session={token}"})
    assert auth.get_current_user(request) == "admin"

    tampered = _request(headers={"cookie": "koyura_session=not-a-signed-token"})
    assert auth.get_current_user(tampered) is None
    assert auth.get_current_user(_request()) is None


def test_session_max_age_is_clamped(monkeypatch):
    monkeypatch.setenv("SESSION_MAX_AGE_SECONDS", "1")
    assert auth._max_age_seconds() == 300

    monkeypatch.setenv("SESSION_MAX_AGE_SECONDS", "not-a-number")
    assert auth._max_age_seconds() == 43200


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def test_csrf_token_roundtrip(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    token = auth.new_csrf_token()
    request = _request(headers={"cookie": f"koyura_csrf={token}"})
    assert auth.verify_csrf(request, token)
    assert not auth.verify_csrf(request, "forged")
    assert not auth.verify_csrf(request, None)


def test_csrf_rejects_unsigned_cookie(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    request = _request(headers={"cookie": "koyura_csrf=attacker-value"})
    assert not auth.verify_csrf(request, "attacker-value")


# ---------------------------------------------------------------------------
# Rate limiter / client key
# ---------------------------------------------------------------------------

def test_rate_limiter_window():
    limiter = auth.LoginRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(3):
        limiter.record_failure("10.0.0.1")
    assert limiter.is_blocked("10.0.0.1")
    assert not limiter.is_blocked("10.0.0.2")
    assert limiter.retry_after("10.0.0.1") > 0


def test_client_key_ignores_forwarded_header_by_default(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    request = _request(headers={"x-forwarded-for": "8.8.8.8"})
    assert auth.client_key(request) == "127.0.0.1"


def test_client_key_uses_forwarded_header_when_trusted(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    request = _request(headers={"x-forwarded-for": "8.8.8.8, 10.0.0.1"})
    assert auth.client_key(request) == "8.8.8.8"
