"""Authentication, sessions and CSRF protection for the Koyura dashboard.

Secrets are never baked into the image or the repository: the dashboard refuses
to start when ``SECRET_KEY``/``ADMIN_PASSWORD`` are missing, too short, or still
set to a well-known placeholder.

Supported password configuration (set exactly one):
  * ``ADMIN_PASSWORD``      — plain password from the environment
  * ``ADMIN_PASSWORD_HASH`` — ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``
    (generate with ``python -m dashboard.hash_password`` or the snippet printed
    by the startup error)

``ADMIN_USER`` defaults to ``admin``; it is only a login name, not a secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

COOKIE_NAME = "koyura_session"
CSRF_COOKIE_NAME = "koyura_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

MIN_SECRET_KEY_LENGTH = 32
MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 260_000

_PLACEHOLDER_SECRETS = {
    "change-me",
    "change-me-in-production",
    "changeme",
    "secret",
    "secret-key",
    "insecure-default-key-change-me",
    "test-secret",
    "your-secret-key",
}

_PLACEHOLDER_PASSWORDS = {
    "admin",
    "admin123",
    "password",
    "changeme",
    "change-me",
    "change-me-in-production",
    "testpass",
    "testpass123",
    "koyura",
}


def _env_flag(name: str, default: bool | None = None) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "" or raw.strip().lower() == "auto":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _secret_key() -> str:
    key = (os.environ.get("SECRET_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "SECRET_KEY is required: set a random value with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
        )
    if key.lower() in _PLACEHOLDER_SECRETS:
        raise RuntimeError(
            "SECRET_KEY is still a placeholder value; generate a unique random secret."
        )
    if len(key) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY is too short ({len(key)} chars); use at least "
            f"{MIN_SECRET_KEY_LENGTH} random characters."
        )
    return key


def admin_username() -> str:
    """Configured login name (not a secret; defaults to ``admin``)."""
    return (os.environ.get("ADMIN_USER") or "admin").strip() or "admin"


def _plain_password() -> str:
    return os.environ.get("ADMIN_PASSWORD") or ""


def _password_hash() -> str:
    return (os.environ.get("ADMIN_PASSWORD_HASH") or "").strip()


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a salted PBKDF2-SHA256 hash string for ``password``."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str) -> bool:
    """Constant-time password check against the configured hash or plaintext."""
    if not isinstance(password, str):
        return False

    stored_hash = _password_hash()
    if stored_hash:
        try:
            scheme, iterations, salt_b64, hash_b64 = stored_hash.split("$")
            if scheme != "pbkdf2_sha256":
                logger.error("Unsupported ADMIN_PASSWORD_HASH scheme: %s", scheme)
                return False
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), salt, int(iterations)
            )
            return hmac.compare_digest(candidate, expected)
        except (ValueError, TypeError):
            logger.error("ADMIN_PASSWORD_HASH is malformed")
            return False

    plain = _plain_password()
    if not plain:
        # Fail closed; validate_config() normally prevents this.
        return False
    return hmac.compare_digest(password.encode(), plain.encode())


def verify_credentials(username: str, password: str) -> bool:
    """Return True only when both the user name and the password match."""
    username_ok = hmac.compare_digest(
        (username or "").encode(), admin_username().encode()
    )
    password_ok = verify_password(password or "")
    # Evaluate both branches so timing does not reveal which one failed.
    return username_ok and password_ok


def validate_config() -> None:
    """Fail fast (at startup) when authentication is misconfigured."""
    _secret_key()

    has_hash = bool(_password_hash())
    has_plain = bool(_plain_password())
    if not has_hash and not has_plain:
        raise RuntimeError(
            "ADMIN_PASSWORD or ADMIN_PASSWORD_HASH is required. "
            "Generate a hash with `python -m dashboard.hash_password`."
        )
    if has_hash and has_plain:
        logger.warning(
            "Both ADMIN_PASSWORD and ADMIN_PASSWORD_HASH are set; the hash wins. "
            "Remove ADMIN_PASSWORD from the environment."
        )
    if has_plain and not has_hash:
        if _plain_password().strip().lower() in _PLACEHOLDER_PASSWORDS:
            raise RuntimeError(
                "ADMIN_PASSWORD is a well-known placeholder; choose a unique password."
            )
        if len(_plain_password()) < MIN_PASSWORD_LENGTH:
            raise RuntimeError(
                f"ADMIN_PASSWORD is too short; use at least {MIN_PASSWORD_LENGTH} characters."
            )
    if has_hash and not _password_hash().startswith("pbkdf2_sha256$"):
        raise RuntimeError("ADMIN_PASSWORD_HASH must use the pbkdf2_sha256$ format")


def _max_age_seconds() -> int:
    try:
        return max(300, int(os.environ.get("SESSION_MAX_AGE_SECONDS", "43200")))
    except ValueError:
        return 43200


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt="koyura-session")


def _cookie_secure(request: Request | None = None) -> bool:
    """Secure cookies by default whenever the request is served over HTTPS."""
    override = _env_flag("SESSION_COOKIE_SECURE")
    if override is not None:
        return override
    if request is not None:
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        scheme = forwarded or request.url.scheme
        return scheme == "https"
    return False


def create_session_cookie(
    response: Response, username: str, request: Request | None = None
) -> None:
    """Sign and set the session cookie on the response."""
    token = _serializer().dumps(username)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def get_current_user(request: Request) -> str | None:
    """Read the session cookie and return the username, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        username = _serializer().loads(token, max_age=_max_age_seconds())
    except (BadSignature, SignatureExpired):
        return None
    except RuntimeError:
        return None
    return username if isinstance(username, str) else None


# ---------------------------------------------------------------------------
# CSRF (double submit cookie + HMAC signature)
# ---------------------------------------------------------------------------

def _csrf_signature(value: str) -> str:
    return hmac.new(
        _secret_key().encode(), f"csrf:{value}".encode(), hashlib.sha256
    ).hexdigest()


def _valid_csrf_token(token: str) -> bool:
    try:
        value, signature = token.rsplit(".", 1)
    except (AttributeError, ValueError):
        return False
    if not value or not signature:
        return False
    return hmac.compare_digest(signature, _csrf_signature(value))


def new_csrf_token() -> str:
    value = secrets.token_urlsafe(24)
    return f"{value}.{_csrf_signature(value)}"


def existing_csrf_token(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE_NAME) or ""
    return token if _valid_csrf_token(token) else ""


def set_csrf_cookie(response: Response, token: str, request: Request | None = None) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=_max_age_seconds(),
        httponly=False,  # the dashboard JS reads it to set the request header
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def verify_csrf(request: Request, submitted: str | None) -> bool:
    """Validate a form/header token against the signed double-submit cookie."""
    cookie = existing_csrf_token(request)
    if not cookie or not submitted:
        return False
    if not _valid_csrf_token(submitted):
        return False
    return hmac.compare_digest(cookie, submitted)


# ---------------------------------------------------------------------------
# Login rate limiting (in-memory, per client address)
# ---------------------------------------------------------------------------

class LoginRateLimiter:
    """Sliding-window limiter for failed login attempts."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self.max_attempts = max(1, max_attempts)
        self.window_seconds = max(1, window_seconds)
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        attempts = [ts for ts in self._failures.get(key, []) if now - ts < self.window_seconds]
        if attempts:
            self._failures[key] = attempts
        else:
            self._failures.pop(key, None)
        return attempts

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.time())) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.time()
            if len(self._failures) > 4096:
                # Keep the limiter's memory bounded under address sweeps.
                for stale in [
                    candidate
                    for candidate in self._failures
                    if not self._recent(candidate, now)
                ]:
                    self._failures.pop(stale, None)
            attempts = self._recent(key, now)
            attempts.append(now)
            self._failures[key] = attempts

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def retry_after(self, key: str) -> int:
        with self._lock:
            attempts = self._recent(key, time.time())
        if not attempts:
            return 0
        oldest = min(attempts)
        return max(1, int(self.window_seconds - (time.time() - oldest)))


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


login_rate_limiter = LoginRateLimiter(
    max_attempts=_int_env("LOGIN_RATE_LIMIT_ATTEMPTS", 5),
    window_seconds=_int_env("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300),
)


def client_key(request: Request) -> str:
    """Rate-limit key for the caller (proxy headers only when explicitly trusted)."""
    if _env_flag("TRUST_PROXY_HEADERS", False):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"
