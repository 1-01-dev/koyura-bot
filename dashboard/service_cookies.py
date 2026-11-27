"""Cookie files for services that need a logged-in session.

A service (``reddit``, ``instagram``, …) may hold **several** Netscape cookie
files; the order is the priority order the engine tries them in, so a second
file is only used when the first one is refused. The panel owns storage, the
engine only reads the files:

    /var/lib/koyura2/cookies/<service>/<id>.txt       mode 0600

Three rules shape this module:

* **Cookies are a secret.** Nothing here logs a line of a file, writes it to the
  database or returns it over HTTP: the row keeps the name, the size and the
  SHA-256, never the content. The files live outside ``DOWNLOADS_PATH`` so the
  bot's cleanup and the panel's purge cannot delete them.
* **The service name is a path component.** It is sanitised down to
  ``[a-z0-9._-]`` before it is ever joined to the directory, so ``../../etc``
  cannot leave the root even if a caller forgets to validate.
* **The format is not guessed.** A file is a Netscape cookie jar (the format
  every browser extension exports and yt-dlp reads) or it is refused with a
  sentence that says what was expected.

The engine mirrors :func:`sanitise_service` in ``koyura/cookies.py``; both
modules are pinned by a shared table of cases in their tests.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

#: The largest cookie jar a browser exports is a few kilobytes; 1 MB is already
#: absurd, and it keeps a bad upload from filling the state volume.
MAX_COOKIE_BYTES = 1024 * 1024

#: ``COOKIES_PATH`` is the only name the code reads. The default is the path the
#: deployment documents; compose and the systemd units mount it for both the
#: panel (write) and the engine (read).
DEFAULT_COOKIES_PATH = "/var/lib/koyura2/cookies"

#: A service name doubles as a directory name: one path component, no dots-only.
MAX_SERVICE_LENGTH = 64
_SERVICE_ALLOWED = re.compile(r"[a-z0-9._-]+")
#: A run of separators becomes one dash, and a ``..`` never survives as a path
#: component: ``abc/../x`` → ``abc-x``, ``reddit;rm -rf /`` → ``reddit-rm-rf``.
_SEPARATOR_RUN = re.compile(r"[-._]{2,}")
_SERVICE_STRIP = re.compile(r"[^a-z0-9._-]+")

#: Stored file names are generated, never taken from an upload.
_STORED_NAME = re.compile(r"[a-z0-9]{8,32}\.txt")

#: Every Netscape jar starts with one of these two comment lines (browser
#: extensions differ in the second word).
NETSCAPE_HEADERS = (
    "# netscape http cookie file",
    "# http cookie file",
)

#: A data line is exactly seven tab-separated fields.
NETSCAPE_FIELDS = 7


class CookieError(ValueError):
    """A refused upload, with a sentence the operator can act on."""


# ---------------------------------------------------------------------------
# Paths and names
# ---------------------------------------------------------------------------

def cookies_root() -> Path:
    """Root of the cookie tree (``COOKIES_PATH``, read per call)."""
    raw = os.environ.get("COOKIES_PATH", "").strip() or DEFAULT_COOKIES_PATH
    return Path(raw)


def sanitise_service(raw: object) -> str:
    """Lowercase ``raw`` into one safe path component.

    ``Reddit`` → ``reddit``, ``abc/../x`` → ``abc-x``, ``1tv.ru`` stays as is.
    An empty result (or a name that is only dots) is refused rather than
    silently turned into something else.
    """
    text = str(raw or "").strip().lower()
    text = _SERVICE_STRIP.sub("-", text)
    # Collapsing the runs is what keeps a traversal attempt from becoming a
    # dotted name: ``abc/../x`` is ``abc-x``, not ``abc-..-x``.
    text = _SEPARATOR_RUN.sub("-", text)
    text = text.strip("-.")
    if not text:
        raise CookieError("Enter a service name (letters, digits, dot, dash).")
    if len(text) > MAX_SERVICE_LENGTH:
        raise CookieError(
            f"Keep the service name under {MAX_SERVICE_LENGTH} characters."
        )
    if ".." in text or not _SERVICE_ALLOWED.fullmatch(text):
        raise CookieError("A service name may only contain a-z, 0-9, dot, dash, underscore.")
    return text


def service_dir(service: object) -> Path:
    """``<root>/<service>`` — the only directory a service may own."""
    return cookies_root() / sanitise_service(service)


def new_stored_name() -> str:
    """A fresh, unguessable file name for one jar (``<id>.txt``)."""
    return f"{secrets.token_hex(8)}.txt"


def cookie_path(service: object, stored_name: object) -> Path:
    """Resolve one stored jar, refusing anything that is not a generated name."""
    name = str(stored_name or "")
    if not _STORED_NAME.fullmatch(name):
        raise CookieError("Unknown cookie file.")
    return service_dir(service) / name


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_payload(data: bytes) -> None:
    """Refuse an upload that is not a Netscape cookie jar, or is too large.

    The check is deliberately structural — a header line *or* data lines with
    seven tab-separated fields — because every browser extension writes the
    header and every hand-made jar still has the rows.
    """
    if not data:
        raise CookieError("The file is empty.")
    if len(data) > MAX_COOKIE_BYTES:
        raise CookieError(
            f"The file is larger than {MAX_COOKIE_BYTES // 1024} KB. "
            "A cookie jar is a few kilobytes; export it again."
        )
    if b"\x00" in data:
        raise CookieError("The file is binary, not a Netscape cookie file.")

    text = data.decode("utf-8", "replace")
    lines = [line.strip("\r\n") for line in text.splitlines()]
    if not lines:
        raise CookieError("The file is empty.")

    # The first meaningful line decides: a header is authoritative, and a jar
    # without one is still accepted when its rows have the right shape.
    first = next((line for line in lines if line.strip()), "")
    if first.strip().lower().startswith(NETSCAPE_HEADERS):
        rows = [
            line for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not rows:
            raise CookieError("The cookie file has a header but no cookies.")
        _validate_rows(rows)
        return

    rows = [
        line for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not rows:
        raise CookieError(
            "This is not a Netscape cookie file: expected "
            "'# Netscape HTTP Cookie File' or rows of seven tab-separated fields."
        )
    _validate_rows(rows)


def _validate_rows(rows: list[str]) -> None:
    """Every non-comment line must be a seven-field Netscape row."""
    for index, line in enumerate(rows, start=1):
        if len(line.split("\t")) != NETSCAPE_FIELDS:
            raise CookieError(
                "This is not a Netscape cookie file: line "
                f"{index} does not have {NETSCAPE_FIELDS} tab-separated fields "
                "(domain, flag, path, secure, expiry, name, value)."
            )


def looks_like_cookies(data: bytes) -> bool:
    """``validate_payload`` as a predicate (used by the tests and the UI copy)."""
    try:
        validate_payload(data)
    except CookieError:
        return False
    return True


# ---------------------------------------------------------------------------
# Files on disk
# ---------------------------------------------------------------------------

def write_cookie_file(service: object, data: bytes, stored_name: str | None = None) -> tuple[str, str, int]:
    """Store one validated jar; return ``(stored_name, sha256, size)``.

    The directory is created on first use (mode 0700), the file is written to a
    temporary name inside it and renamed into place, and its mode is 0600 from
    the moment it exists — an interrupted save therefore never leaves a
    world-readable half file behind.
    """
    validate_payload(data)
    name = stored_name or new_stored_name()
    directory = service_dir(service)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / name

    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".upload-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
    except BaseException:
        _unlink_quietly(Path(temp_path))
        raise
    os.chmod(target, 0o600)
    return name, hashlib.sha256(data).hexdigest(), len(data)


def remove_cookie_file(service: object, stored_name: object) -> bool:
    """Delete one jar (and the service directory when it becomes empty)."""
    path = cookie_path(service, stored_name)
    removed = _unlink_quietly(path)
    _prune_empty_dir(path.parent)
    return removed


def service_files(service: object) -> list[str]:
    """Every ``<id>.txt`` currently on disk for one service, name-sorted."""
    directory = service_dir(service)
    try:
        return sorted(
            entry.name for entry in directory.iterdir()
            if entry.is_file() and _STORED_NAME.fullmatch(entry.name)
        )
    except OSError:
        return []


def _unlink_quietly(path: Path) -> bool:
    """Remove a file; ``True`` when it is gone afterwards."""
    try:
        path.unlink()
        return True
    except OSError:
        # Missing (already deleted) and locked (a concurrent reader) are both
        # "not there for us"; the caller only needs to know it is not.
        return False


def _prune_empty_dir(directory: Path) -> None:
    # Not empty, or already gone: both are fine, the caller only tidies up.
    with contextlib.suppress(OSError):
        directory.rmdir()


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def human_size(size: object) -> str:
    """``1536`` → ``1.5 KB`` (the table prints sizes, never contents)."""
    try:
        value = int(str(size))
    except (TypeError, ValueError):
        return "—"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


#: A check result as the table shows it: a word and a badge class.
CHECK_STATUS_LABELS = {
    "ok": ("ok", "badge-success"),
    "auth": ("needs sign-in", "badge-warning"),
    # ``blocked`` is not a spent session: the site refused the *IP*, so no cookie
    # file can fix it and the hint under the badge says so.
    "blocked": ("blocked · needs another IP", "badge-danger"),
    "error": ("error", "badge-danger"),
}


def check_label(status: object) -> tuple[str, str]:
    """``(text, badge class)`` for a stored ``check_status``."""
    return CHECK_STATUS_LABELS.get(str(status or ""), ("not checked", "badge"))


def default_probe_url(service: object) -> str:
    """The URL a check tries when the operator typed none.

    A service named like a host (``1tv.ru``) gets its own site; a bare extractor
    key (``reddit``) has no domain to infer, so the form asks for one instead of
    guessing a URL that would fail for the wrong reason.
    """
    name = str(service or "").strip().lower()
    if "." in name and not name.startswith("."):
        return f"https://{name}/"
    return ""


# ---------------------------------------------------------------------------
# The "Check" action
# ---------------------------------------------------------------------------

#: ``yt-dlp --simulate`` against a test URL is the only honest way to say whether
#: a jar still works: it makes the same request a download would, without
#: fetching a byte of media. Short timeout, no playlist walk.
PROBE_TIMEOUT_SECONDS = 45.0

#: Substrings yt-dlp (or the site) emits when the session is not good enough.
#: Mirrors ``koyura/cookies.py``; a jar that fails this way is "needs sign-in",
#: which is a different sentence from "the URL was wrong".
AUTH_MARKERS = (
    "sign in to confirm",
    "login required",
    "log in to confirm",
    "private video",
    "this video is private",
    "http error 403",
    "cookies",
    "age-restricted",
    "age restricted",
    "confirm your age",
    "unauthorized",
)

#: The site refused the *address*: no cookie file can fix this, only another
#: outbound IP. The list is what a datacentre block actually looks like — a
#: Reddit 403 from the API, Cloudflare's page, a rate limit, a captcha wall.
#: Checked first, because "HTTP Error 403: Blocked" also matches the 403 above
#: and calling that "needs sign-in" would send the operator to the wrong page.
BLOCKED_MARKERS = (
    "unable to access the reddit api",
    "http error 403: blocked",
    "blocked",
    "too many requests",
    "429",
    "captcha",
)


def classify_probe(returncode: int, stderr: str) -> str:
    """``ok`` / ``blocked`` / ``auth`` / ``error`` for one simulate run.

    ``blocked`` is decided before ``auth``: a datacentre block produces a 403
    too, and telling the operator to upload cookies for it is advice that
    cannot work. ``auth`` keeps only the genuine sign-in markers.
    """
    if returncode == 0:
        return "ok"
    text = (stderr or "").lower()
    if any(marker in text for marker in BLOCKED_MARKERS):
        return "blocked"
    if any(marker in text for marker in AUTH_MARKERS):
        return "auth"
    return "error"


def check_probe_url(url: object) -> str:
    """Refuse a test URL that is not a plain public http(s) address.

    The panel is the only caller and the URL comes from the operator's own
    form, but the value still reaches a subprocess: the same rules the rest of
    the project applies to a link hold here — http/https only, a real host, no
    credentials in the address, a bounded length. Deliberately not a copy of
    ``koyura/url_safety.py`` (services do not import across directories): this
    is the small subset the check needs.
    """
    text = str(url or "").strip()
    if not text:
        raise CookieError("Enter the URL to test this service with.")
    if len(text) > 2048:
        raise CookieError("That URL is too long.")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise CookieError("The test URL must start with http:// or https:// .")
    if not parts.hostname:
        raise CookieError("The test URL has no host.")
    if parts.username or parts.password:
        raise CookieError("The test URL must not carry a username or a password.")
    return text


def probe_cookie(path: Path, url: str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Run ``yt-dlp --simulate`` with one jar; return ``(status, detail)``.

    The detail line is the last non-empty stderr line, trimmed — it is shown to
    the operator, who is the only person who should ever see a yt-dlp message
    about their own cookie file. It never contains the file's contents (yt-dlp
    does not echo a cookie jar), but it is truncated anyway.
    """
    import subprocess
    import sys

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--simulate", "--skip-download", "--no-playlist",
        "--no-warnings", "--cookies", str(path), url,
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument vector
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "error", f"yt-dlp did not answer within {int(timeout)} s"
    except OSError as exc:
        return "error", f"Could not run yt-dlp: {exc}"

    status = classify_probe(completed.returncode, completed.stderr)
    detail = ""
    for line in reversed((completed.stderr or "").splitlines()):
        stripped = line.strip()
        if stripped:
            detail = stripped[:200]
            break
    return status, detail
