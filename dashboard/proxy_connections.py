"""Proxy connections: the list the engine walks, and the secrets behind it.

Before this module the panel could describe exactly one outbound route: a
manual proxy *or* a WireGuard tunnel, stored as ``egress.*`` settings, applied
to every service or to a named subset. One route is not enough — a proxy for
Reddit and a different one for Instagram is an ordinary wish, and so is "try
this one first, then that one".

So a connection is now a row in ``proxy_connections``: a name, a kind
(``socks5``/``socks5h``/``http``/``https``/``wireguard``), a URL, an assignment
rule, an ``enabled`` switch, a ``status`` and a position. The engine reads the
list from ``GET /api/egress`` and, for each service, dials the first row that is
enabled, whose rule covers the service and whose last check did not fail
(``koyura/egress.py`` owns that choice).

Three rules shape the storage:

* **a password never enters the database.** It is written to
  ``EGRESS_PATH/secrets/<id>.txt`` (mode 0600, owner ``koyura2``) and the row
  keeps only ``password_ref`` — the relative path of that file. The engine
  mounts the same tree read-only and composes the URL when it dials; the
  panel's API never carries the login, the password or the path.
* **one WireGuard tunnel, not two.** The config is a single file,
  ``EGRESS_PATH/wg0.conf``, applied by the host's ``koyura-egress.path``
  watcher. A second ``kind=wireguard`` row would name the same file twice, so it
  is refused with ``OnlyOneWireGuard``.
* **the old settings migrate once.** ``egress.mode=manual`` (plus its URL and
  assignment) becomes one connection named ``Default``; ``egress.mode=wireguard``
  becomes a ``wireguard`` row when the config file exists. A marker in
  ``settings`` makes the migration a no-op on the next start.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import db
import egress
import service_catalog

logger = logging.getLogger("koyura.dashboard")

#: The kinds a row may have. For a proxy the URL's scheme *is* the kind.
KINDS: tuple[str, ...] = ("socks5", "socks5h", "http", "https", "wireguard")

WIREGUARD_KIND = "wireguard"

#: What the last check left behind. ``unknown`` is the state of a new row.
STATUSES: tuple[str, ...] = ("ok", "fail", "unknown")

#: The assignment rule, shared with the legacy settings model and the engine.
ASSIGNMENT_MODES: tuple[str, ...] = egress.ASSIGNMENT_MODES

#: A name is a label in a list, not a paragraph.
MAX_NAME_LENGTH = 80

#: ``EGRESS_PATH/secrets`` — one file per connection that needs a login.
SECRETS_DIRNAME = "secrets"

#: The name the migration gives the one connection it creates.
DEFAULT_NAME = "Default"

#: ``settings`` key of the migration flag: the retired single-rule form is
#: read once, turned into rows, and never read again.
MIGRATED_KEY = "egress.migrated"

#: How often the panel re-tests the list, in minutes (0 = never). The page owns
#: this setting and writes ``proxy.test_interval``; ``egress.test_interval`` is
#: the key the first API build used and is still read as a fallback, so an
#: install that set an interval before this page existed keeps it.
TEST_INTERVAL_KEY = "proxy.test_interval"
LEGACY_TEST_INTERVAL_KEY = "egress.test_interval"
DEFAULT_TEST_INTERVAL_MINUTES = 0
MAX_TEST_INTERVAL_MINUTES = 1440

#: The intervals the page offers, in minutes. ``0`` is the off switch, and it
#: is the default: a background probe is an operator decision, not a surprise.
TEST_INTERVAL_CHOICES: tuple[tuple[int, str], ...] = (
    (0, "Off"),
    (5, "Every 5 min"),
    (15, "Every 15 min"),
    (30, "Every 30 min"),
    (60, "Every hour"),
    (180, "Every 3 hours"),
)

#: The environment variable that overrides the stored interval. ``off`` (or
#: ``0``) disables the background tests even when a setting says otherwise —
#: the operator needs one switch that a page cannot undo.
TEST_INTERVAL_ENV = "PROXY_TEST_INTERVAL"


class ProxyConnectionError(ValueError):
    """A refused connection, with a stable ``code`` the routes can branch on.

    ``OnlyOneWireGuard`` is the one code callers are expected to recognise: the
    config is a single file, so a second tunnel row is a mistake, not a value
    to sanitise.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# The draft: one validated submission
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Draft:
    """A connection as the caller asked for it, already validated.

    ``password`` is ``None`` when the caller did not type one: on an update that
    means "keep the stored secret", on a create it means "no login".
    """

    name: str
    kind: str
    url: str = ""
    username: str = ""
    password: str | None = None
    config_path: str = ""
    assignment_mode: str = "all"
    services: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True

    def credential(self) -> str:
        """``user:password`` percent-encoded, or ``""`` when there is no login."""
        return credential_for(self.username, self.password or "")

    def services_json(self) -> str:
        """The ``services`` column: a JSON array, never a comma-joined string."""
        return json.dumps(list(self.services))


def credential_for(username: object, password: object) -> str:
    """The credential string the engine appends to the URL, or ``""``.

    Percent-encoded on purpose: a password may hold ``@``, ``/`` or ``:``, and
    the engine pastes this straight into the URL's authority. ``quote`` leaves
    the benign characters alone, so a simple password is readable in the file
    while an awkward one still dials.
    """
    user = str(username or "")
    secret = str(password or "")
    if not user:
        return ""
    return f"{quote(user, safe='')}:{quote(secret, safe='')}"


def split_credentials(url: object) -> tuple[str, str, str]:
    """``(url without userinfo, username, password)`` for a stored URL.

    The migration and the legacy form both hand us a URL that may still carry
    ``user:pass@``. Splitting it is what keeps the password out of the database:
    the URL column keeps only ``scheme://host:port``.
    """
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text, "", ""
    netloc = parts.netloc
    username = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password else ""
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    clean = f"{parts.scheme}://{netloc}" if parts.scheme else netloc
    return clean, username, password


def kind_for_url(url: object) -> str:
    """The kind a proxy URL implies (``socks5h://…`` → ``socks5h``)."""
    text = str(url or "").strip()
    scheme = text.split("://", 1)[0].strip().lower() if "://" in text else ""
    return scheme if scheme in egress.PROXY_SCHEMES else ""


def parse_draft(
    *,
    name: object,
    kind: object = "",
    url: object = "",
    username: object = "",
    password: object | None = None,
    assignment_mode: object = "all",
    services: list[str] | tuple[str, ...] = (),
    enabled: bool = True,
) -> Draft:
    """Validate one submission and return the draft to store.

    ``password=None`` means "the caller did not type one" — the caller decides
    whether that keeps the stored secret (update) or means "no login" (create).
    A password typed with the URL itself (``socks5h://user:pass@host:port``) is
    split out here, because storing it in the URL is exactly what must not
    happen.
    """
    chosen_name = " ".join(str(name or "").split())
    if not chosen_name:
        raise ProxyConnectionError("MissingName", "Give the connection a name.")
    if len(chosen_name) > MAX_NAME_LENGTH:
        raise ProxyConnectionError(
            "NameTooLong", f"Keep the name under {MAX_NAME_LENGTH} characters."
        )

    chosen_assignment = str(assignment_mode or "all").strip().lower() or "all"
    if chosen_assignment not in ASSIGNMENT_MODES:
        raise ProxyConnectionError(
            "InvalidAssignment",
            "Choose All services, Selected services or All except selected.",
        )
    unique_services = tuple(
        dict.fromkeys(
            str(entry).strip().lower() for entry in services if str(entry).strip()
        )
    )

    chosen_kind = str(kind or "").strip().lower()
    if chosen_kind == WIREGUARD_KIND:
        if str(url or "").strip():
            raise ProxyConnectionError(
                "UnexpectedUrl",
                "A WireGuard connection has no proxy URL. Leave the field empty.",
            )
        return Draft(
            name=chosen_name,
            kind=WIREGUARD_KIND,
            url="",
            config_path=str(egress.wireguard_path()),
            assignment_mode=chosen_assignment,
            services=unique_services,
            enabled=enabled,
        )

    if chosen_kind and chosen_kind not in KINDS:
        raise ProxyConnectionError(
            "InvalidKind", "Choose " + ", ".join(KINDS) + "."
        )

    try:
        clean, url_user, url_password = split_credentials(url)
        clean = egress.normalise_proxy_url(clean)
    except egress.EgressError as error:
        raise ProxyConnectionError("InvalidUrl", str(error)) from error

    scheme = kind_for_url(clean)
    if chosen_kind and chosen_kind not in (scheme, "") and scheme:
        raise ProxyConnectionError(
            "KindMismatch",
            f"The URL starts with {scheme}://, so the kind must be {scheme}.",
        )

    chosen_user = str(username or "").strip() or url_user
    chosen_password: str | None = None
    if password is not None:
        chosen_password = str(password)
    elif url_password:
        chosen_password = url_password
    if chosen_password and not chosen_user:
        raise ProxyConnectionError(
            "MissingUsername",
            "Enter the username that goes with the proxy password.",
        )
    return Draft(
        name=chosen_name,
        kind=scheme,
        url=clean,
        username=chosen_user,
        password=chosen_password,
        assignment_mode=chosen_assignment,
        services=unique_services,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# The secret files
# ---------------------------------------------------------------------------

def secrets_dir() -> Path:
    """``EGRESS_PATH/secrets`` — the only place a password is written."""
    return egress.egress_root() / SECRETS_DIRNAME


def secret_path(connection_id: int | str) -> Path:
    """``EGRESS_PATH/secrets/<id>.txt`` — derived from the id, never stored."""
    return secrets_dir() / f"{int(connection_id)}.txt"


def secret_ref(connection_id: int | str) -> str:
    """The value stored in ``password_ref``: a path relative to ``EGRESS_PATH``.

    Relative on purpose: the same database moved to another host (or mounted at
    another prefix) still points at its own secret tree.
    """
    return f"{SECRETS_DIRNAME}/{int(connection_id)}.txt"


def write_secret(connection_id: int | str, credential: str) -> str:
    """Write the credential as ``secrets/<id>.txt`` (0600) and return its ref."""
    if not credential:
        return ""
    directory = secrets_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ProxyConnectionError(
            "SecretWriteFailed",
            "Could not create the secrets directory. Check the EGRESS_PATH mount.",
        ) from exc
    target = secret_path(connection_id)
    try:
        handle, temp_path = tempfile.mkstemp(
            dir=directory, prefix=".secret-", suffix=".tmp"
        )
    except OSError as exc:
        raise ProxyConnectionError(
            "SecretWriteFailed",
            "Could not write the proxy password. Check the EGRESS_PATH mount.",
        ) from exc
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(credential)
            stream.write("\n")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
    except OSError as exc:
        _unlink_quietly(Path(temp_path))
        raise ProxyConnectionError(
            "SecretWriteFailed",
            "Could not write the proxy password. Check the EGRESS_PATH mount.",
        ) from exc
    os.chmod(target, 0o600)
    return secret_ref(connection_id)


def read_secret(ref: object) -> str:
    """The stored credential for one ``password_ref``, or ``""``."""
    text = str(ref or "").strip()
    if not text:
        return ""
    path = _resolve_ref(text)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def delete_secret(ref: object) -> bool:
    """Remove the secret file; True when it existed."""
    text = str(ref or "").strip()
    if not text:
        return False
    path = _resolve_ref(text)
    return _unlink_quietly(path) if path is not None else False


def _resolve_ref(ref: str) -> Path | None:
    """A ``password_ref`` resolved under ``EGRESS_PATH``, or ``None``.

    The value comes from our own database, but it is still resolved and checked:
    a row edited by hand must not turn into a read of ``/etc/shadow``.
    """
    relative = Path(ref)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return egress.egress_root() / relative


def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The dial URL (a secret in memory, never in a row)
# ---------------------------------------------------------------------------

def _with_credentials(url: str, credential: str) -> str:
    """``socks5h://host:port`` + ``user:pass`` → ``socks5h://user:pass@host:port``."""
    if not credential or "@" in url.split("://", 1)[-1]:
        return url
    scheme, _, remainder = url.partition("://")
    return f"{scheme}://{credential}@{remainder}"


def dial_url(row: dict) -> str:
    """The proxy URL to dial for one row, or ``""`` when it cannot be dialled.

    A WireGuard row has no URL of its own: its proxy is the SOCKS address the
    host watcher reports in ``status.json``, and a tunnel that is not up answers
    ``""`` (a dead tunnel must never become a broken proxy).
    """
    if str(row.get("kind") or "") == WIREGUARD_KIND:
        return egress.read_status().socks_url
    url = str(row.get("url") or "").strip()
    if not url:
        return ""
    return _with_credentials(url, read_secret(row.get("password_ref")))


# ---------------------------------------------------------------------------
# Reading the list
# ---------------------------------------------------------------------------

def services_of(row: dict) -> list[str]:
    """The ``services`` column as a list of names (never a raise)."""
    raw = row.get("services")
    if isinstance(raw, (list, tuple)):
        return [str(name) for name in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(name) for name in parsed]


def public_view(row: dict) -> dict:
    """One connection as ``GET /api/egress`` and the UI may see it.

    No username, no ``password_ref``, no ``config_path``: the engine gets the
    URL without a login and reads the credential from the shared secrets tree
    itself. The URL is masked defensively — it is stored clean, so the mask is
    an identity function on a correct row, and a hard stop on a hand-edited one.
    """
    return {
        "id": int(row["id"]),
        "name": str(row.get("name") or ""),
        "kind": str(row.get("kind") or ""),
        "url": egress.mask_proxy_url(row.get("url")),
        "assignment": {
            "mode": str(row.get("assignment_mode") or "all"),
            "services": services_of(row),
        },
        "enabled": bool(row.get("enabled")),
        "status": str(row.get("status") or "unknown"),
    }


async def list_connections() -> list[dict]:
    """Every connection in the engine's order (raw rows, for the panel)."""
    return await db.list_proxy_connections()


async def engine_payload() -> dict:
    """The body of ``GET /api/egress``: the list and the test interval."""
    rows = await db.list_proxy_connections()
    return {
        "connections": [public_view(row) for row in rows],
        "test_interval": await test_interval_minutes(),
    }


async def test_interval_minutes() -> int:
    """How often the list should be re-tested, in minutes (0 = never)."""
    raw = await db.get_setting(TEST_INTERVAL_KEY)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_TEST_INTERVAL_MINUTES
    return max(0, min(MAX_TEST_INTERVAL_MINUTES, value))


async def set_test_interval(minutes: object) -> int:
    """Validate and store the re-test interval; returns the stored value."""
    try:
        value = int(str(minutes).strip())
    except (TypeError, ValueError) as exc:
        raise ProxyConnectionError(
            "InvalidTestInterval", "The test interval must be a whole number of minutes."
        ) from exc
    value = max(0, min(MAX_TEST_INTERVAL_MINUTES, value))
    await db.save_settings({TEST_INTERVAL_KEY: str(value)})
    return value


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def _refuse_second_wireguard(keep_id: int | None) -> None:
    """Refuse a second ``kind=wireguard`` row (the config is one file)."""
    rows = await db.list_proxy_connections()
    others = [
        row
        for row in rows
        if str(row.get("kind") or "") == WIREGUARD_KIND
        and int(row["id"]) != int(keep_id or 0)
    ]
    if others:
        raise ProxyConnectionError(
            "OnlyOneWireGuard",
            "Only one WireGuard connection is allowed: the tunnel is a single "
            f"config file, already used by {others[0].get('name') or 'another row'}.",
        )


async def create_connection(
    *,
    name: object,
    kind: object = "",
    url: object = "",
    username: object = "",
    password: object | None = None,
    assignment_mode: object = "all",
    services: list[str] | tuple[str, ...] = (),
    enabled: bool = True,
) -> dict:
    """Validate, insert and store the secret; returns the new row."""
    draft = parse_draft(
        name=name,
        kind=kind,
        url=url,
        username=username,
        password=password,
        assignment_mode=assignment_mode,
        services=services,
        enabled=enabled,
    )
    if draft.kind == WIREGUARD_KIND:
        await _refuse_second_wireguard(None)

    row = await db.create_proxy_connection(
        name=draft.name,
        kind=draft.kind,
        url=draft.url,
        username=draft.username or None,
        password_ref=None,
        config_path=draft.config_path or None,
        assignment_mode=draft.assignment_mode,
        services=draft.services_json(),
        enabled=draft.enabled,
    )
    credential = draft.credential()
    if not credential:
        return row
    try:
        ref = write_secret(row["id"], credential)
    except ProxyConnectionError:
        # No half-row: a connection whose password could not be stored would
        # dial without a login and fail every job it was selected for.
        await db.delete_proxy_connection(row["id"])
        raise
    return await db.update_proxy_connection(row["id"], password_ref=ref) or row


async def update_connection(
    connection_id: int,
    *,
    name: object,
    kind: object = "",
    url: object = "",
    username: object = "",
    password: object | None = None,
    assignment_mode: object = "all",
    services: list[str] | tuple[str, ...] = (),
) -> dict:
    """Validate and apply one edit; a blank password keeps the stored secret."""
    row = await db.get_proxy_connection(connection_id)
    if row is None:
        raise ProxyConnectionError("NotFound", "That connection no longer exists.")
    draft = parse_draft(
        name=name,
        kind=kind,
        url=url,
        username=username,
        password=password,
        assignment_mode=assignment_mode,
        services=services,
        enabled=bool(row.get("enabled")),
    )
    if draft.kind == WIREGUARD_KIND:
        await _refuse_second_wireguard(connection_id)

    new_ref: str | None = None
    if draft.kind == WIREGUARD_KIND:
        new_ref = ""
    elif draft.password:
        new_ref = write_secret(connection_id, draft.credential())
    elif draft.password is None and draft.username:
        old_ref = str(row.get("password_ref") or "")
        new_ref = write_secret(
            connection_id,
            _rename_credential(read_secret(old_ref), draft.username),
        ) if old_ref else None
    else:
        new_ref = ""

    if new_ref == "" and row.get("password_ref"):
        delete_secret(row.get("password_ref"))

    return (
        await db.update_proxy_connection(
            connection_id,
            name=draft.name,
            kind=draft.kind,
            url=draft.url,
            username=draft.username or None,
            password_ref=new_ref,
            config_path=draft.config_path or None,
            assignment_mode=draft.assignment_mode,
            services=draft.services_json(),
        )
        or row
    )


def _rename_credential(stored: str, username: str) -> str:
    """The stored credential with a new username and its old password.

    The file holds ``user:password`` percent-encoded; the password half is
    already encoded, so only the username is quoted again. An empty file means
    there is no password to keep — writing ``user:`` would invent one.
    """
    if not stored:
        return ""
    _user, _, password = stored.partition(":")
    return credential_for(username, unquote(password))


async def delete_connection(connection_id: int) -> bool:
    """Delete one row, its position and its secret; True when it existed."""
    row = await db.delete_proxy_connection(connection_id)
    if row is None:
        return False
    delete_secret(row.get("password_ref"))
    return True


async def move_connection(connection_id: int, delta: int) -> bool:
    """Move one row up (``-1``) or down (``+1``) in the engine's order."""
    return await db.move_proxy_connection(connection_id, delta)


async def set_enabled(connection_id: int, enabled: bool) -> bool:
    """Turn one row on or off."""
    return await db.set_proxy_connection_enabled(connection_id, enabled)


# ---------------------------------------------------------------------------
# The check: what does the world see as our IP?
# ---------------------------------------------------------------------------

async def test_connection(connection_id: int) -> dict:
    """Probe one connection, store the verdict and return the updated row.

    The probe itself is a synchronous ``curl`` with a 10 s timeout, so it runs in
    a worker thread: this coroutine is called from a request handler and from the
    background sweep, and blocking the event loop for ten seconds would freeze
    every other page the panel is serving at the time.
    """
    row = await db.get_proxy_connection(connection_id)
    if row is None:
        raise ProxyConnectionError("NotFound", "That connection no longer exists.")
    ok, detail = await asyncio.to_thread(_probe_row, row)
    await db.record_proxy_connection_test(
        connection_id, status="ok" if ok else "fail", result=detail
    )
    return await db.get_proxy_connection(connection_id) or row


def _probe_row(row: dict) -> tuple[bool, str]:
    """The blocking half of :func:`test_connection`: dial and describe.

    Separated so the caller can hand it to a thread, and so the two failure
    shapes — a tunnel that is not up, and a proxy with nothing to dial — stay in
    one place instead of in the middle of an async function.
    """
    url = dial_url(row)
    if url:
        ok, detail = egress.probe_proxy(url)
        return ok, egress.redact(detail, url)
    status = egress.read_status()
    if str(row.get("kind") or "") == WIREGUARD_KIND:
        detail = status.error or (
            "The WireGuard tunnel is not up."
            if status.available
            else "The host watcher has not reported the tunnel yet."
        )
    else:
        detail = "The connection has no proxy URL."
    return False, detail


# ---------------------------------------------------------------------------
# The page: what a row looks like in the table, and in the editor
# ---------------------------------------------------------------------------
#
# Everything the template prints comes from here, never from a raw row. The two
# reasons are a secret and a phone: ``password_ref`` must not reach the HTML, and
# a timestamp from SQLite has to be readable without a Jinja filter that a later
# edit could drop.

KIND_LABELS: dict[str, str] = {
    "socks5": "SOCKS5",
    "socks5h": "SOCKS5H",
    "http": "HTTP",
    "https": "HTTPS",
    "wireguard": "WireGuard",
}

#: ``status`` → the badge a row shows. ``unknown`` is a plain badge: it is the
#: state of a row nobody has checked yet, not a failure.
STATUS_BADGES: dict[str, str] = {
    "ok": "badge badge-success",
    "fail": "badge badge-danger",
    "unknown": "badge",
}

MODE_LABELS: dict[str, str] = {"all": "All services", "selected": "Selected", "except": "All except"}


def kind_label(kind: object) -> str:
    """The protocol as the page spells it (``socks5h`` → ``SOCKS5H``)."""
    text = str(kind or "")
    return KIND_LABELS.get(text, text.upper() or "—")


def format_when(value: object) -> str:
    """``2026-01-31 12:00:03`` → ``2026-01-31 12:00``, or ``—``."""
    text = str(value or "").strip()
    if not text:
        return "—"
    return text[:16]


def assignment_label(row: dict) -> str:
    """The ``Applied to`` cell: the mode, plus the count for the two lists."""
    mode = str(row.get("assignment_mode") or "all")
    names = services_of(row)
    label = MODE_LABELS.get(mode, mode)
    if mode == "all" or not names:
        return label
    return f"{label} · {len(names)}"


def enabled_label(row: dict) -> str:
    """The word the switch says, so a screen reader hears the state."""
    return "Enabled" if row.get("enabled") else "Disabled"


def status_label(row: dict) -> str:
    """The word in the status badge, and the tooltip's reason."""
    return str(row.get("status") or "unknown")


def page_row(row: dict) -> dict:
    """One table row, already formatted, with no secret in it.

    ``has_password`` is the only thing the editor needs to know about the
    secret: whether to say "set" under the empty password field. The value
    itself stays in ``EGRESS_PATH/secrets/<id>.txt`` and never enters the page.

    A ``wireguard`` row is the one exception to "the row only knows its own
    verdict": before anyone presses Test it has no verdict of its own, and the
    honest state of the tunnel is the watcher's report — so its badge reads
    ``status.json`` instead of saying ``unknown`` about a tunnel that is up.
    """
    url = str(row.get("url") or "")
    is_tunnel = str(row.get("kind") or "") == WIREGUARD_KIND
    tunnel = egress.read_status() if is_tunnel else None
    return {
        "id": int(row["id"]),
        "name": str(row.get("name") or ""),
        "kind": str(row.get("kind") or ""),
        "kind_label": kind_label(row.get("kind")),
        "is_wireguard": is_tunnel,
        "url": egress.mask_proxy_url(url),
        "address_label": (
            f"tunnel · {row.get('config_path') or egress.wireguard_path()}"
            if is_tunnel
            else (egress.mask_proxy_url(url) or "no URL")
        ),
        "assignment_mode": str(row.get("assignment_mode") or "all"),
        "assignment_label": assignment_label(row),
        "services": services_of(row),
        "enabled": bool(row.get("enabled")),
        "enabled_label": enabled_label(row),
        "status": tunnel.label() if tunnel else status_label(row),
        "status_badge": (
            f"badge {tunnel.badge()}"
            if tunnel
            else STATUS_BADGES.get(status_label(row), "badge")
        ),
        "last_test_at": format_when(row.get("last_test_at")),
        "last_test_result": str(row.get("last_test_result") or ""),
        "has_password": bool(str(row.get("password_ref") or "")),
        "created_at": format_when(row.get("created_at")),
    }


def connection_json(row: dict) -> dict:
    """The same row for the editor, as the ``data-connection`` JSON payload.

    One payload per row instead of a form per row: the editor fills itself from
    the attribute (``js/proxy.js``) and the row's menu names the id. It carries
    the masked URL, never the secret.
    """
    view = page_row(row)
    return {
        "id": view["id"],
        "name": view["name"],
        "kind": view["kind"],
        "url": str(row.get("url") or ""),
        "url_masked": view["url"],
        "auth": bool(str(row.get("username") or "")),
        "has_password": view["has_password"],
        "mode": view["assignment_mode"],
        "services": view["services"],
        "enabled": view["enabled"],
        "status": view["status"],
    }


def mode_summary(rows: list[dict]) -> str:
    """``Mode: Manual``, ``Mode: WireGuard`` or ``Mode: Off`` for the header.

    It is a reading of the list, not a stored setting: what the engine will do
    is decided by which rows are enabled, so the page derives the word from
    them instead of keeping a fourth source of truth.
    """
    live = [row for row in rows if row.get("enabled")]
    if not live:
        return "Off"
    if any(str(row.get("kind") or "") == WIREGUARD_KIND for row in live):
        return "WireGuard"
    return "Manual"


def interval_choices(stored: int) -> list[dict]:
    """The interval selector: the six options with the stored one marked.

    A value outside the list (an env override, or a hand-edited row) is added
    once so the selector always shows the interval actually in force.
    """
    choices = list(TEST_INTERVAL_CHOICES)
    if stored not in {minutes for minutes, _label in choices}:
        choices.append((stored, f"Every {stored} min"))
    return [
        {
            "minutes": minutes,
            "value": str(minutes),
            "label": label,
            "selected": minutes == stored,
        }
        for minutes, label in choices
    ]


async def page_context() -> dict:
    """Everything ``/admin/proxy`` renders: the rows, the interval, the tunnel.

    The interval comes from the environment first (``PROXY_TEST_INTERVAL``),
    the stored setting second: an operator who disabled the background tests on
    the server must not have them re-armed by a page. The effective value is
    what the selector shows and what the loop schedules against.
    """
    rows = await db.list_proxy_connections()
    stored = await test_interval_minutes()
    interval = await effective_test_interval()
    tunnel = [
        row for row in rows if str(row.get("kind") or "") == WIREGUARD_KIND
    ]
    return {
        "proxy_rows": [page_row(row) for row in rows],
        "proxy_payloads": {
            int(row["id"]): connection_json(row) for row in rows
        },
        "proxy_count": len(rows),
        "proxy_live_count": len([row for row in rows if row.get("enabled")]),
        "proxy_mode": mode_summary(rows),
        "proxy_interval": interval,
        "proxy_interval_stored": stored,
        "proxy_interval_choices": interval_choices(interval),
        "proxy_interval_env": _env_interval(),
        "proxy_has_wireguard": bool(tunnel),
        "proxy_wireguard_name": str(tunnel[0].get("name") or "") if tunnel else "",
        # The tunnel's own report, so the modal can show a real state line
        # instead of a second status panel on the page.
        "proxy_status": egress.read_status(),
        "proxy_wg_present": egress.wireguard_present(),
        "proxy_wg_max_kb": egress.MAX_WG_BYTES // 1024,
        "proxy_wg_path": str(egress.wireguard_path()),
        # The catalogue the "Apply to" picker lists: the same names the Cookies
        # page suggests, so a ticked service is one the engine can compute.
        "proxy_services": list(service_catalog.service_names()),
        "proxy_kinds": [
            {"value": kind, "label": KIND_LABELS[kind]} for kind in KINDS
        ],
        "proxy_assignment_options": [
            {"value": mode, "label": label}
            for mode, label in egress.ASSIGNMENT_LABELS.items()
        ],
    }


# ---------------------------------------------------------------------------
# The re-test interval and its background task
# ---------------------------------------------------------------------------

def _env_interval() -> int | None:
    """``PROXY_TEST_INTERVAL`` as minutes, ``None`` when it is not set.

    ``off``/``0``/an empty string is a deliberate zero: the switch exists so a
    server can pin the tests off. A value that is not a number is ignored (and
    logged once) rather than silently read as "off", because a typo in an env
    file should not look like a decision.
    """
    raw = (os.environ.get(TEST_INTERVAL_ENV) or "").strip().lower()
    if not raw:
        return None
    if raw in {"off", "never", "false", "no"}:
        return 0
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number of minutes; using the stored interval",
            TEST_INTERVAL_ENV,
            raw,
        )
        return None
    return max(0, min(MAX_TEST_INTERVAL_MINUTES, value))


async def effective_test_interval() -> int:
    """The interval the page shows and the loop schedules: env wins over the row."""
    override = _env_interval()
    if override is not None:
        return override
    return await test_interval_minutes()


async def run_due_tests(now: datetime | None = None) -> int:
    """Re-test every enabled row whose last check is older than the interval.

    Run by the background loop and by the tests. "Due" is a comparison against
    ``last_test_at`` rather than a second timer, so a panel that restarts does
    not lose track: a row checked two minutes ago is not checked again because
    the process came up. Returns how many rows were probed.
    """
    interval = await effective_test_interval()
    if interval <= 0:
        return 0
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(minutes=interval)).strftime("%Y-%m-%d %H:%M:%S")
    probed = 0
    for row in await db.list_proxy_connections():
        if not row.get("enabled"):
            continue
        last = str(row.get("last_test_at") or "")
        if last and last > cutoff:
            continue
        try:
            await test_connection(int(row["id"]))
        except ProxyConnectionError as error:
            # A refused row (a deleted tunnel, say) must not stop the sweep.
            logger.info("Egress auto-test skipped %s: %s", row.get("id"), error)
            continue
        except Exception:  # pragma: no cover - a probe must not kill the loop
            logger.exception("Egress auto-test failed for connection %s", row.get("id"))
            continue
        probed += 1
    if probed:
        logger.info("Egress auto-test: checked %d connection(s)", probed)
    return probed


#: How often the loop wakes up. One minute is the granularity of the shortest
#: interval the page offers (5 min), and a wake-up that finds nothing due costs
#: one SQLite read.
AUTO_TEST_TICK_SECONDS = 60


async def auto_test_loop(*, tick_seconds: int = AUTO_TEST_TICK_SECONDS) -> None:
    """Background task: run the due checks once per ``tick_seconds``, forever.

    Cancellation is the only way out (``main.py`` cancels it on shutdown), and a
    failing sweep is logged and then waited out — a broken proxy is the normal
    case this feature exists to report, not a reason to stop reporting it.
    """
    while True:
        try:
            await run_due_tests()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never let one sweep end the loop
            logger.exception("Egress auto-test sweep failed")
        await asyncio.sleep(tick_seconds)


# ---------------------------------------------------------------------------
# The old settings: migrate once
# ---------------------------------------------------------------------------

async def migrate_legacy_settings() -> int:
    """Turn the stored ``egress.*`` settings into connections, once.

    ``manual`` becomes one row named ``Default`` whose kind is the URL's scheme;
    ``wireguard`` becomes one tunnel row *only when its config file exists*,
    because a row pointing at a file nobody wrote would fail every check.
    Everything else is ignored — the key set is retired, not re-interpreted.
    Returns how many rows were created.
    """
    if await db.get_setting(MIGRATED_KEY):
        return 0
    stored = await db.get_settings(prefix=egress.SETTING_PREFIX)
    settings = egress.Egress.from_row(stored)
    created = 0
    try:
        if settings.mode == "manual" and settings.proxy_url:
            clean, username, password = split_credentials(settings.proxy_url)
            await create_connection(
                name=DEFAULT_NAME,
                url=clean,
                username=username,
                password=password or None,
                assignment_mode=settings.assignment_mode,
                services=list(settings.services),
            )
            created = 1
        elif settings.mode == "wireguard" and egress.wireguard_present():
            await create_connection(
                name=DEFAULT_NAME,
                kind=WIREGUARD_KIND,
                assignment_mode=settings.assignment_mode,
                services=list(settings.services),
            )
            created = 1
    except ProxyConnectionError as error:
        # A stored value this build refuses must not stop the panel from
        # starting, and must not be retried on every restart.
        logger.warning("Could not migrate the stored egress settings: %s", error)
    await db.save_settings({MIGRATED_KEY: "1"})
    if created:
        logger.info("Migrated the stored egress settings into %d connection(s)", created)
    return created


async def apply_saved_connections() -> None:
    """Startup: migrate the old settings, then log the list (never a secret)."""
    try:
        await migrate_legacy_settings()
        rows = await db.list_proxy_connections()
        interval = await effective_test_interval()
    except Exception:  # pragma: no cover - a log line must never stop startup
        logger.exception("Could not read the stored proxy connections")
        return
    if not rows:
        logger.info("Egress: no proxy connections (direct connection)")
    else:
        summary = ", ".join(
            f"{row['name']}[{row['kind']},{row['status']},"
            f"{'on' if row['enabled'] else 'off'}]"
            for row in rows
        )
        logger.info("Egress: %d connection(s): %s", len(rows), summary)
    logger.info(
        "Egress auto-tests: %s",
        f"every {interval} min" if interval else "off",
    )


__all__ = [
    "ASSIGNMENT_MODES",
    "AUTO_TEST_TICK_SECONDS",
    "DEFAULT_NAME",
    "DEFAULT_TEST_INTERVAL_MINUTES",
    "Draft",
    "KINDS",
    "KIND_LABELS",
    "MAX_NAME_LENGTH",
    "MAX_TEST_INTERVAL_MINUTES",
    "MIGRATED_KEY",
    "MODE_LABELS",
    "ProxyConnectionError",
    "SECRETS_DIRNAME",
    "STATUSES",
    "STATUS_BADGES",
    "TEST_INTERVAL_CHOICES",
    "TEST_INTERVAL_ENV",
    "TEST_INTERVAL_KEY",
    "WIREGUARD_KIND",
    "apply_saved_connections",
    "assignment_label",
    "auto_test_loop",
    "connection_json",
    "create_connection",
    "credential_for",
    "delete_connection",
    "delete_secret",
    "dial_url",
    "effective_test_interval",
    "enabled_label",
    "engine_payload",
    "format_when",
    "interval_choices",
    "kind_for_url",
    "kind_label",
    "list_connections",
    "migrate_legacy_settings",
    "mode_summary",
    "move_connection",
    "page_context",
    "page_row",
    "parse_draft",
    "public_view",
    "read_secret",
    "run_due_tests",
    "secret_path",
    "secret_ref",
    "services_of",
    "set_enabled",
    "set_test_interval",
    "split_credentials",
    "status_label",
    "test_connection",
    "test_interval_minutes",
    "write_secret",
]
