"""Outbound egress: a manual proxy or a WireGuard tunnel, and who uses it.

Datacentre IPs are blocked by several sites (Reddit answers ``403 Blocked`` no
matter which cookie jar is uploaded). The owner therefore chooses, in the panel,
*how* the engine leaves the box and *which services* that applies to. This
module is the panel's half of that bargain; the engine reads the result from
``GET /api/egress`` (see ``koyura/egress.py``) and hands yt-dlp ``--proxy``.

Three pieces of state, and they are deliberately different things:

* **the settings** — mode, proxy URL and the assignment rule — live in the
  dashboard's ``settings`` table (``egress.*``), next to the download limits;
* **the WireGuard config** is a *file*, ``EGRESS_PATH/wg0.conf`` (0600), because
  a root-owned watcher outside this container applies it and only a path can be
  watched; the panel never reads it back;
* **the tunnel status** is a *file too*, ``EGRESS_PATH/status.json``, written by
  that watcher: ``{"state": "up|down|error", "egress_ip": ..., "error": ...,
  "socks": "10.8.0.2:11080", "updated_at": ...}``.

Two rules shape everything here:

* **a secret never comes back.** The proxy password is stored (the engine needs
  it) but is rendered as ``***`` in the form, in the page and in every log line;
  the WireGuard private key is never read by this process at all — the panel
  writes the uploaded bytes and forgets them.
* **nothing is guessed.** A proxy URL must name one of four schemes, a
  WireGuard file must look like one (two sections, three required keys, ≤64 KB),
  and an unreadable ``status.json`` is "down", never an exception that takes the
  page with it.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("koyura.dashboard")

#: Storage prefix in the panel's ``settings`` table.
SETTING_PREFIX = "egress."

#: Where the root watcher reads the config and writes the status.
DEFAULT_EGRESS_PATH = "/var/lib/koyura2/egress"

#: The three modes. ``off`` is the shipped default: direct connection, exactly
#: the behaviour of every release before this one.
MODES: tuple[str, ...] = ("off", "manual", "wireguard")

MODE_LABELS: dict[str, str] = {
    "off": "Off",
    "manual": "Manual proxy",
    "wireguard": "WireGuard",
}

#: Which services a configured egress applies to.
ASSIGNMENT_MODES: tuple[str, ...] = ("all", "selected", "except")

ASSIGNMENT_LABELS: dict[str, str] = {
    "all": "All services",
    "selected": "Selected services",
    "except": "All except selected",
}

#: What a proxy URL may be. ``socks5h`` resolves names on the proxy — the only
#: variant that helps when DNS itself is what the site blocks.
PROXY_SCHEMES: tuple[str, ...] = ("socks5", "socks5h", "http", "https")

#: A proxy URL is one line, not a paragraph.
MAX_PROXY_URL_LENGTH = 512

#: A ``.conf`` is a few hundred bytes; 64 KB already means "the wrong file".
MAX_WG_BYTES = 64 * 1024

#: WireGuard sections and the keys each must carry.
WG_SECTIONS: tuple[str, ...] = ("interface", "peer")
WG_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "interface": ("privatekey", "address"),
    "peer": ("endpoint",),
}

#: The states the watcher reports. Anything else is an error, not a state.
STATUS_STATES: tuple[str, ...] = ("up", "down", "error")

#: The address a proxy check asks "what is my IP?" — free, plain text, no key.
PROBE_URL = "https://api.ipify.org"

#: The check is a single request; ten seconds is long enough for a slow tunnel.
PROBE_TIMEOUT_SECONDS = 10.0


class EgressError(ValueError):
    """A refused value, with a sentence the operator can act on."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def egress_root() -> Path:
    """The directory the watcher watches (``EGRESS_PATH``, read per call)."""
    raw = os.environ.get("EGRESS_PATH", "").strip() or DEFAULT_EGRESS_PATH
    return Path(raw)


def wireguard_path() -> Path:
    """``EGRESS_PATH/wg0.conf`` — the only file the panel writes itself."""
    return egress_root() / "wg0.conf"


def status_path() -> Path:
    """``EGRESS_PATH/status.json`` — written by the watcher, only read here."""
    return egress_root() / "status.json"


# ---------------------------------------------------------------------------
# The proxy URL: validation, masking
# ---------------------------------------------------------------------------

def normalise_proxy_url(raw: object) -> str:
    """Validate a proxy URL and return it without surrounding whitespace.

    The four schemes are the ones yt-dlp and curl both understand. A path,
    query or fragment is refused rather than silently dropped: it is how a
    pasted *target* URL (``socks5://host/https://reddit.com``) is recognised
    instead of being stored as a proxy that can never work.
    """
    text = str(raw or "").strip()
    if not text:
        raise EgressError("Enter the proxy URL, for example socks5h://user:pass@1.2.3.4:1080.")
    if len(text) > MAX_PROXY_URL_LENGTH:
        raise EgressError(f"Keep the proxy URL under {MAX_PROXY_URL_LENGTH} characters.")
    if any(character.isspace() for character in text):
        raise EgressError("The proxy URL must not contain spaces.")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise EgressError(f"That is not a valid URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise EgressError(
            "The proxy URL must start with one of "
            + ", ".join(f"{name}://" for name in PROXY_SCHEMES)
            + "."
        )
    if not parts.hostname:
        raise EgressError("The proxy URL has no host.")
    try:
        parts.port  # noqa: B018 - raises for a non-numeric or out-of-range port
    except ValueError as exc:
        raise EgressError("The proxy URL has an invalid port.") from exc
    if parts.path not in ("", "/"):
        raise EgressError("The proxy URL must not carry a path.")
    if parts.query or parts.fragment:
        raise EgressError("The proxy URL must not carry a query or a fragment.")
    if len(parts.netloc) > 255:
        raise EgressError("The proxy URL's host part is too long.")
    return f"{scheme}://{parts.netloc}"


def has_credentials(raw: object) -> bool:
    """Whether the stored proxy URL carries ``user:pass@``."""
    try:
        parts = urlsplit(str(raw or ""))
    except ValueError:
        return False
    return bool(parts.username or parts.password)


def mask_proxy_url(raw: object) -> str:
    """``socks5h://user:s3cret@1.2.3.4:1080`` → ``socks5h://***:***@1.2.3.4:1080``.

    The host and the port stay visible — that is what tells an operator which
    proxy is configured — and every credential part becomes ``***``. The whole
    string is used for display only; the real value never leaves the database
    except to the engine's internal endpoint.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return "***"
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    if parts.username or parts.password:
        netloc = f"***:***@{netloc}"
    return f"{parts.scheme}://{netloc}" if parts.scheme else netloc


def redact(text: object, secret: object = "") -> str:
    """Free text with the proxy URL's credentials taken out.

    A curl error can echo the proxy it used (``Could not resolve proxy:
    user:pass@host``); the operator still needs the *error*, so the credentials
    are replaced in place instead of the whole line being dropped.
    """
    message = str(text or "")
    url = str(secret or "").strip()
    if url:
        message = message.replace(url, mask_proxy_url(url))
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        if parts is not None:
            if parts.password:
                message = message.replace(parts.password, "***")
            if parts.username and parts.password:
                message = message.replace(
                    f"{parts.username}:{parts.password}", "***:***"
                )
    return message


# ---------------------------------------------------------------------------
# The WireGuard config
# ---------------------------------------------------------------------------

def validate_wireguard(data: bytes) -> None:
    """Refuse a file that is not a usable ``wg0.conf``.

    Structural on purpose: two sections, and the three keys a tunnel needs
    (``PrivateKey`` and ``Address`` under ``[Interface]``, ``Endpoint`` under
    ``[Peer]``). The values are not parsed — a key that decrypts to nonsense is
    the watcher's problem to report, and a fixture with fake keys must reach it
    instead of being refused here.
    """
    if not data:
        raise EgressError("The WireGuard config is empty.")
    if len(data) > MAX_WG_BYTES:
        raise EgressError(
            f"The file is larger than {MAX_WG_BYTES // 1024} KB. "
            "A WireGuard config is a few hundred bytes."
        )
    if b"\x00" in data:
        raise EgressError("The file is binary, not a WireGuard config.")

    sections = _parse_sections(data.decode("utf-8", "replace"))
    for name in WG_SECTIONS:
        if name not in sections:
            raise EgressError(
                f"The config has no [{name.title()}] section. "
                "A WireGuard config needs both [Interface] and [Peer]."
            )
    for name, keys in WG_REQUIRED_KEYS.items():
        for key in keys:
            if key not in sections[name]:
                raise EgressError(
                    f"The [{name.title()}] section has no {key.title()} = … line."
                )


def _parse_sections(text: str) -> dict[str, dict[str, str]]:
    """``{[interface]: {privatekey: "…"}}``, keys lowercased, values as written.

    A hand-written config may use ``PrivateKey=value`` without spaces and may
    comment with ``#`` or ``;``; both are accepted because refusing them would
    only be a formatting opinion.
    """
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#")[0].split(";")[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections[current][key.strip().lower()] = value.strip()
    return sections


def write_wireguard(data: bytes) -> Path:
    """Validate and store the config as ``EGRESS_PATH/wg0.conf`` (mode 0600).

    The bytes are written to a temporary file inside the directory and renamed
    into place, so the watcher can never read a half-written config, and the
    mode is 0600 *before* the file has its final name.
    """
    validate_wireguard(data)
    directory = egress_root()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise EgressError(
            "Could not create the egress directory. Check the EGRESS_PATH mount."
        ) from exc
    target = wireguard_path()
    try:
        handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".wg0-", suffix=".tmp")
    except OSError as exc:
        raise EgressError(
            "Could not write the WireGuard config. Check the EGRESS_PATH mount."
        ) from exc
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
    except OSError as exc:
        _unlink_quietly(Path(temp_path))
        raise EgressError(
            "Could not write the WireGuard config. Check the EGRESS_PATH mount."
        ) from exc
    os.chmod(target, 0o600)
    return target


def remove_wireguard() -> bool:
    """Delete the config so the watcher brings the tunnel down; True if it went."""
    return _unlink_quietly(wireguard_path())


def wireguard_present() -> bool:
    """Whether a config is on disk (the panel never reads its bytes)."""
    try:
        return wireguard_path().is_file()
    except OSError:
        return False


def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The watcher's status
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Status:
    """One reading of ``status.json``, tolerant of every in-between state."""

    state: str = "down"
    egress_ip: str = ""
    error: str = ""
    socks: str = ""
    updated_at: str = ""
    #: Whether the watcher has ever written a status file.
    available: bool = False

    @property
    def is_up(self) -> bool:
        return self.state == "up"

    def label(self) -> str:
        """The word the page prints for this state."""
        if not self.available:
            return "no report yet"
        return {"up": "up", "down": "down", "error": "error"}.get(self.state, self.state)

    def badge(self) -> str:
        """Badge class for the state (reuses the table's three badges)."""
        if self.state == "up":
            return "badge-success"
        if self.state == "error":
            return "badge-danger"
        return "badge-warning"

    @property
    def socks_url(self) -> str:
        """``socks5h://10.8.0.2:11080`` for the engine, or ``""``."""
        if not self.is_up or not self.socks:
            return ""
        return socks_url(self.socks)


def socks_url(value: object) -> str:
    """Normalise the watcher's ``host:port`` into a URL yt-dlp accepts."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        scheme, _, remainder = text.partition("://")
        return f"{scheme}://{remainder}" if scheme in PROXY_SCHEMES else ""
    return f"socks5h://{text}"


def parse_status(payload: object, *, available: bool = True) -> Status:
    """Turn the watcher's JSON into a :class:`Status`, or a safe default."""
    if not isinstance(payload, dict):
        return Status(state="error", error="status.json is not a JSON object.", available=available)
    state = str(payload.get("state") or "").strip().lower()
    error = _one_line(payload.get("error"))
    socks = _one_line(payload.get("socks"))
    if state not in STATUS_STATES:
        return Status(
            state="error",
            error=error or f"Unknown state in status.json: {state or '(empty)'}.",
            socks=socks,
            egress_ip=_one_line(payload.get("egress_ip")),
            updated_at=_one_line(payload.get("updated_at")),
            available=available,
        )
    return Status(
        state=state,
        egress_ip=_one_line(payload.get("egress_ip")),
        error=error,
        socks=socks,
        updated_at=_one_line(payload.get("updated_at")),
        available=available,
    )


def _one_line(value: object) -> str:
    """A single, bounded line: a status file is data, not a document."""
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return ""
    return text.splitlines()[0][:200]


def read_status() -> Status:
    """Read ``status.json``; a missing or broken file is "down", never a raise."""
    path = status_path()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return Status(available=False)
    except OSError as exc:
        return Status(
            state="error",
            error=f"Could not read status.json: {exc.strerror or exc}.",
            available=True,
        )
    try:
        payload = json.loads(raw)
    except ValueError:
        return Status(state="error", error="status.json is not valid JSON.", available=True)
    return parse_status(payload, available=True)


# ---------------------------------------------------------------------------
# The settings
# ---------------------------------------------------------------------------

def mode_options(selected: str) -> list[dict[str, object]]:
    """The three radio options, with the chosen one marked."""
    return [
        {"value": value, "label": MODE_LABELS[value], "checked": value == selected}
        for value in MODES
    ]


def assignment_options(selected: str) -> list[dict[str, object]]:
    """The three assignment options, with the chosen one marked."""
    return [
        {
            "value": value,
            "label": ASSIGNMENT_LABELS[value],
            "checked": value == selected,
        }
        for value in ASSIGNMENT_MODES
    ]


@dataclass(frozen=True)
class Egress:
    """Mode, proxy and assignment rule — what the panel stores and the engine reads."""

    mode: str = "off"
    proxy_url: str = ""
    assignment_mode: str = "all"
    services: tuple[str, ...] = field(default_factory=tuple)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_row(cls, stored: dict[str, str]) -> Egress:
        """Read the stored strings, falling back to a direct connection."""
        mode = str(stored.get(f"{SETTING_PREFIX}mode") or "off").strip().lower()
        if mode not in MODES:
            mode = "off"
        assignment = str(
            stored.get(f"{SETTING_PREFIX}assignment") or "all"
        ).strip().lower()
        if assignment not in ASSIGNMENT_MODES:
            assignment = "all"
        raw_services = str(stored.get(f"{SETTING_PREFIX}services") or "")
        services = tuple(
            name for name in (piece.strip().lower() for piece in raw_services.split(","))
            if name
        )
        proxy_url = str(stored.get(f"{SETTING_PREFIX}proxy_url") or "").strip()
        return cls(
            mode=mode,
            proxy_url=proxy_url,
            assignment_mode=assignment,
            services=services,
        )

    # -- rule --------------------------------------------------------------

    def applies_to(self, service: object) -> bool:
        """Whether the egress rule covers ``service``.

        ``all`` covers everything, including a URL no extractor named: the
        operator said "send everything through the proxy". The two list modes
        need a name, so an unknown service is only covered by ``except`` when it
        is not in the exclusion list.
        """
        if self.mode == "off":
            return False
        name = str(service or "").strip().lower()
        if self.assignment_mode == "all":
            return True
        if self.assignment_mode == "selected":
            return bool(name) and name in self.services
        return not (name and name in self.services)

    def proxy_for(self, service: object, status: Status | None = None) -> str:
        """The ``--proxy`` value for one service, or ``""`` for a direct call."""
        if not self.applies_to(service):
            return ""
        if self.mode == "manual":
            return self.proxy_url
        report = status if status is not None else read_status()
        return report.socks_url

    # -- serialisation -----------------------------------------------------

    def as_stored(self) -> dict[str, str]:
        """The rows to write. Only the mode's own value is kept."""
        return {
            f"{SETTING_PREFIX}mode": self.mode,
            f"{SETTING_PREFIX}proxy_url": self.proxy_url if self.mode == "manual" else "",
            f"{SETTING_PREFIX}assignment": self.assignment_mode,
            f"{SETTING_PREFIX}services": ",".join(self.services),
        }

    def as_engine_payload(self, status: Status | None = None) -> dict[str, object]:
        """The body of ``GET /api/egress`` — the only place the real URL travels.

        Internal, over the shared token, to the engine in the same deployment;
        the panel's own pages never render this value. A WireGuard mode sends the
        SOCKS address the watcher reported and the manual URL as an empty string,
        so a dead tunnel cannot leave a stale proxy behind.
        """
        report = status if status is not None else read_status()
        return {
            "mode": self.mode,
            "proxy_url": self.proxy_url if self.mode == "manual" else "",
            "wg_socks": report.socks if (self.mode == "wireguard" and report.is_up) else "",
            "assignment": {
                "mode": self.assignment_mode,
                "services": list(self.services),
            },
        }

    def describe(self) -> str:
        """One log line: the mode, who it applies to, and nothing secret."""
        if self.mode == "off":
            return "off (direct connection)"
        target = mask_proxy_url(self.proxy_url) if self.mode == "manual" else "WireGuard"
        if self.assignment_mode == "all":
            scope = "all services"
        elif self.assignment_mode == "selected":
            scope = "only " + (", ".join(self.services) or "nothing selected")
        else:
            scope = "all except " + (", ".join(self.services) or "nothing")
        return f"{self.mode} via {target}, {scope}"

    def as_form(self, services: list[str] | None = None) -> dict[str, object]:
        """What the template needs, with the proxy URL already masked."""
        names = list(services or self.services)
        return {
            "mode": self.mode,
            "mode_options": mode_options(self.mode),
            "proxy_configured": bool(self.proxy_url),
            "proxy_display": mask_proxy_url(self.proxy_url),
            "assignment_mode": self.assignment_mode,
            "assignment_options": assignment_options(self.assignment_mode),
            "services": [
                {"name": name, "checked": name in self.services} for name in names
            ],
            "selected_count": len(self.services),
        }


def parse_settings(
    *,
    mode: object,
    proxy_url: object,
    assignment_mode: object,
    services: list[str],
    stored: Egress,
) -> tuple[Egress | None, list[str]]:
    """Validate one submission of the mode + assignment form.

    A blank proxy field means "keep the stored URL": the page never renders the
    password back, so an empty field is the normal case once a proxy is saved.
    The mode still has to be one of three and a first-time manual save has to
    carry a URL, which is the only way to fail this form.
    """
    chosen_mode = str(mode or "").strip().lower()
    if chosen_mode not in MODES:
        return None, ["Choose Off, Manual proxy or WireGuard."]
    chosen_assignment = str(assignment_mode or "").strip().lower() or "all"
    if chosen_assignment not in ASSIGNMENT_MODES:
        return None, ["Choose All services, Selected services or All except selected."]

    kept_url = stored.proxy_url
    typed_url = str(proxy_url or "").strip()
    if chosen_mode == "manual":
        if typed_url:
            try:
                typed_url = normalise_proxy_url(typed_url)
            except EgressError as error:
                return None, [str(error)]
            kept_url = typed_url
        elif not kept_url:
            return None, [
                "Enter the proxy URL, for example socks5h://user:pass@1.2.3.4:1080."
            ]
    elif typed_url:
        # A URL typed while switching to WireGuard is a typo, not a wish: keeping
        # it would silently arm a proxy the operator did not select.
        return None, [
            "Clear the proxy URL, or choose Manual proxy to use it."
        ]

    unique = tuple(dict.fromkeys(
        name.strip().lower() for name in services if name.strip()
    ))
    return Egress(
        mode=chosen_mode,
        proxy_url=kept_url,
        assignment_mode=chosen_assignment,
        services=unique,
    ), []


# ---------------------------------------------------------------------------
# The "Check" action: what does the world see as our IP?
# ---------------------------------------------------------------------------

def curl_command(proxy_url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> list[str]:
    """The argument vector used to test a proxy (fixed, never shell-parsed)."""
    return [
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        f"{int(timeout)}",
        "--proxy",
        proxy_url,
        PROBE_URL,
    ]


def probe_proxy(proxy_url: object, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Ask ``api.ipify.org`` through ``proxy_url``; return ``(ok, text)``.

    ``ok`` is True only when curl exited 0 *and* the answer is a bare IP — a
    proxy that returns its own error page is a failed check, not a success. The
    detail line is redacted before it is returned or logged, because curl echoes
    the proxy it could not use.
    """
    try:
        url = normalise_proxy_url(proxy_url)
    except EgressError as error:
        return False, str(error)

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument vector
            curl_command(url, timeout),
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.info("Proxy check: no answer within %ss", int(timeout))
        return False, f"No answer within {int(timeout)} s."
    except OSError as exc:
        # ``FileNotFoundError`` is an ``OSError``: the dashboard image ships
        # curl, a bare-python dev box may not, so a missing binary falls back to
        # asking the same question through urllib and the same proxy.
        if isinstance(exc, FileNotFoundError):
            return _probe_with_urllib(url, timeout)
        logger.info("Proxy check could not start curl: %s", exc.strerror or exc)
        return False, "Could not run the check on this server."

    body = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = redact(_last_line(completed.stderr) or "curl failed", url)
        logger.info("Proxy check failed (curl exit %s): %s", completed.returncode, detail[:160])
        return False, detail
    try:
        ipaddress.ip_address(body)
    except ValueError:
        detail = redact(_last_line(completed.stderr) or "the answer was not an IP address", url)
        logger.info("Proxy check: unexpected answer (%s)", detail[:160])
        return False, detail
    logger.info("Proxy check succeeded")
    return True, body


def _probe_with_urllib(url: str, timeout: float) -> tuple[bool, str]:
    """``probe_proxy`` without curl: one GET through a proxy handler."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})
    )
    try:
        with opener.open(PROBE_URL, timeout=timeout) as response:  # noqa: S310 - fixed URL
            body = response.read(64).decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        detail = redact(str(getattr(exc, "reason", exc)) or "the request failed", url)
        logger.info("Proxy check failed: %s", detail[:160])
        return False, detail
    try:
        ipaddress.ip_address(body)
    except ValueError:
        return False, "The answer was not an IP address."
    logger.info("Proxy check succeeded")
    return True, body


def _last_line(text: object) -> str:
    for line in reversed(str(text or "").splitlines()):
        if line.strip():
            return line.strip()[:200]
    return ""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def apply_saved_egress() -> None:
    """Log what is stored, so a restart shows which egress is in force.

    Nothing is pushed: the engine polls ``GET /api/egress`` and applies the
    answer to the next job, so there is no state to re-apply — only a line for
    the operator who reads the journal after a reboot.
    """
    try:
        import db  # imported here: this module is also used without a database

        stored = await db.get_settings(prefix=SETTING_PREFIX)
        settings = Egress.from_row(stored)
        status = read_status()
        logger.info(
            "Egress: %s (tunnel %s, config %s)",
            settings.describe(),
            status.label(),
            "present" if wireguard_present() else "absent",
        )
    except Exception:  # pragma: no cover - a log line must never stop startup
        logger.exception("Could not read the stored egress settings")


__all__ = [
    "ASSIGNMENT_LABELS",
    "ASSIGNMENT_MODES",
    "assignment_options",
    "DEFAULT_EGRESS_PATH",
    "Egress",
    "EgressError",
    "MAX_WG_BYTES",
    "MODES",
    "MODE_LABELS",
    "PROBE_URL",
    "PROXY_SCHEMES",
    "SETTING_PREFIX",
    "STATUS_STATES",
    "Status",
    "apply_saved_egress",
    "curl_command",
    "egress_root",
    "has_credentials",
    "mask_proxy_url",
    "mode_options",
    "normalise_proxy_url",
    "parse_settings",
    "parse_status",
    "probe_proxy",
    "read_status",
    "redact",
    "remove_wireguard",
    "socks_url",
    "status_path",
    "validate_wireguard",
    "wireguard_path",
    "wireguard_present",
    "write_wireguard",
]
