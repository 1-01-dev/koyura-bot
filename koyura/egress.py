"""The engine's side of the outbound proxies the panel configures.

The panel owns the decision — the proxy connections, their order, their
assignment rule and their last check — and publishes the list on
``GET /api/egress`` (internal, ``X-Internal-Token``). This module fetches it,
caches it for 45 s, and turns it into the ``--proxy`` argument a job needs.

Four properties matter more than the request itself:

* **the first usable connection wins.** For a service, the candidates are the
  enabled connections whose rule covers it; the first of those whose last check
  did not fail is dialled. If every candidate failed its check, the first one is
  used anyway — the operator's last chance is better than no proxy at all.
* **a panel that cannot answer is not an outage.** No answer (or an unreadable
  one) means "direct connection", which is the behaviour of every release before
  this feature, and the last good answer is kept for a few minutes so a panel
  restart does not silently reroute traffic mid-job.
* **no secret travels in the answer.** The panel publishes a URL without a
  login; the password lives in ``EGRESS_PATH/secrets/<id>.txt`` (mode 0600) on
  the shared egress tree, mounted read-only here. This module reads that file
  when it dials and never writes it, logs it or returns it.
* **the rule is per service, not per host.** Which extractor a URL belongs to is
  already answered by ``cookies.service_for_url`` (yt-dlp's own table), so the
  assignment rule and the cookie lookup agree on what "reddit" means.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger("koyura.engine.egress")

#: The panel publishes the list; polled at most this often. Under a minute so a
#: change in the panel reaches the next job without a restart.
CACHE_TTL_SECONDS = 45.0

EGRESS_PATH = "/api/egress"

#: A panel that cannot answer in two seconds must not delay a download.
_TIMEOUT_SECONDS = 2.0

#: The schemes a proxy URL may use (the engine refuses anything else, whatever
#: the panel sent).
PROXY_SCHEMES = ("socks5", "socks5h", "http", "https")

#: The kinds a connection may have. For a proxy the scheme *is* the kind.
KINDS: tuple[str, ...] = (*PROXY_SCHEMES, "wireguard")

WIREGUARD_KIND = "wireguard"

ASSIGNMENT_MODES = ("all", "selected", "except")

STATUSES = ("ok", "fail", "unknown")

#: Where the panel's address is read from, in precedence order. ``KOYURA_*`` is
#: this service's own name; ``DASHBOARD_URL`` is the one the bot, compose and
#: every native ``/etc/koyura/koyura.env`` already set. Reading only the first
#: meant a native install (``DASHBOARD_URL=http://127.0.0.1:8090``) left the
#: engine asking ``http://dashboard:8080`` — a host that does not exist outside
#: compose — and silently running with the direct connection.
DASHBOARD_URL_ENV_NAMES = ("KOYURA_DASHBOARD_URL", "DASHBOARD_URL")

#: The compose service name, and the address to fall back to when neither
#: variable is set (compose itself passes nothing else).
DEFAULT_DASHBOARD_URL = "http://dashboard:8080"

#: The egress tree the panel writes and the host watcher applies. The engine
#: mounts it read-only: it reads the tunnel's status and the proxy credentials,
#: and writes neither.
DEFAULT_EGRESS_ROOT = "/var/lib/koyura2/egress"

#: ``EGRESS_PATH/status.json`` (the watcher's report) and
#: ``EGRESS_PATH/secrets/<id>.txt`` (one credential per connection).
STATUS_FILENAME = "status.json"
SECRETS_DIRNAME = "secrets"

#: Consecutive unreadable polls that turn silence into a WARNING. One missed
#: poll is a panel restart; three in a row is "the engine cannot see the panel".
FAILURE_WARN_THRESHOLD = 3


def dashboard_url() -> str:
    """Address of the admin panel (``KOYURA_DASHBOARD_URL``, then ``DASHBOARD_URL``)."""
    return _panel()[0]


def dashboard_source() -> str:
    """Which variable supplied :func:`dashboard_url` (``default`` when neither)."""
    return _panel()[1]


def _panel() -> tuple[str, str]:
    """``(address, variable it came from)``; ``default`` for the compose host."""
    for name in DASHBOARD_URL_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/"), name
    return DEFAULT_DASHBOARD_URL, "default"


# ---------------------------------------------------------------------------
# The egress tree: the tunnel's status and the proxy credentials
# ---------------------------------------------------------------------------

def egress_root() -> str:
    """``EGRESS_PATH`` read per call, so a test can point it at a fixture."""
    return os.environ.get("EGRESS_PATH", "").strip() or DEFAULT_EGRESS_ROOT


def status_path() -> str:
    """``EGRESS_PATH/status.json`` — written by the watcher, only read here."""
    return os.path.join(egress_root(), STATUS_FILENAME)


def secret_path(connection_id: int) -> str:
    """``EGRESS_PATH/secrets/<id>.txt`` — the panel's password file, read-only."""
    return os.path.join(egress_root(), SECRETS_DIRNAME, f"{int(connection_id)}.txt")


def _read_secret(connection_id: object) -> str:
    """The stored credential for one connection, or ``""``.

    One small read per dial, deliberately uncached: a rotated password has to
    apply to the very next job, and a missing file simply means "no login".
    """
    try:
        identifier = int(str(connection_id).strip())
    except (TypeError, ValueError):
        return ""
    try:
        with open(secret_path(identifier), encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _read_wireguard_socks() -> str:
    """The SOCKS address the watcher reports while the tunnel is up."""
    try:
        with open(status_path(), encoding="utf-8", errors="replace") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if str(payload.get("state") or "").strip().lower() != "up":
        return ""
    return _clean_socks(payload.get("socks"))


_socks = ""
_socks_expires = 0.0


def wireguard_socks() -> str:
    """``socks5h://host:port`` for the tunnel, or ``""`` when it is not up.

    Cached with the panel's answer: the file is on a volume the watcher rewrites
    on every reconnect, and a download cannot wait on it.
    """
    global _socks, _socks_expires
    now = time.monotonic()
    with _lock:
        if now < _socks_expires:
            return _socks
    found = _read_wireguard_socks()
    with _lock:
        _socks = found
        _socks_expires = now + CACHE_TTL_SECONDS
        return _socks


# ---------------------------------------------------------------------------
# One connection, as the panel published it
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Connection:
    """One row of ``GET /api/egress`` — never a credential."""

    id: int = 0
    name: str = ""
    kind: str = ""
    url: str = ""
    assignment_mode: str = "all"
    services: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = False
    status: str = "unknown"

    @classmethod
    def from_entry(cls, entry: object) -> Connection | None:
        """Read one entry tolerantly; ``None`` for something unusable.

        A connection the engine cannot name a scheme for is dropped rather than
        passed to yt-dlp: ``--proxy ftp://…`` fails every job it touches, and a
        panel that publishes it has a bug, not a route.
        """
        if not isinstance(entry, dict):
            return None
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in KINDS:
            return None
        assignment = entry.get("assignment")
        if not isinstance(assignment, dict):
            assignment = {}
        assignment_mode = str(assignment.get("mode") or "all").strip().lower()
        if assignment_mode not in ASSIGNMENT_MODES:
            assignment_mode = "all"
        raw_services = assignment.get("services")
        services: tuple[str, ...] = ()
        if isinstance(raw_services, list):
            services = tuple(
                str(name).strip().lower() for name in raw_services if str(name).strip()
            )
        status = str(entry.get("status") or "unknown").strip().lower()
        if status not in STATUSES:
            status = "unknown"
        return cls(
            id=_as_id(entry.get("id")),
            name=str(entry.get("name") or ""),
            kind=kind,
            url="" if kind == WIREGUARD_KIND else _clean_proxy(entry.get("url")),
            assignment_mode=assignment_mode,
            services=services,
            enabled=bool(entry.get("enabled")),
            status=status,
        )

    # -- rule --------------------------------------------------------------

    def applies_to(self, service: object) -> bool:
        """Whether this connection's rule covers ``service``.

        ``all`` covers everything, including a URL no extractor named: the
        operator said "send everything through this one". The two list modes
        need a name, so an unknown service is only covered by ``except`` when it
        is not in the exclusion list.
        """
        name = str(service or "").strip().lower()
        if self.assignment_mode == "all":
            return True
        if self.assignment_mode == "selected":
            return bool(name) and name in self.services
        return not (name and name in self.services)

    def dial_url(self) -> str:
        """The ``--proxy`` value for this connection, or ``""`` if it cannot dial.

        A WireGuard row has no URL of its own: its proxy is the SOCKS address in
        ``status.json``. A tunnel that is not up answers ``""`` — a dead tunnel
        must never become a half-configured proxy that fails every job.
        """
        if self.kind == WIREGUARD_KIND:
            return wireguard_socks()
        if not self.url:
            return ""
        return _with_credential(self.url, self.id)

    # -- serialisation -----------------------------------------------------

    def as_entry(self) -> dict[str, object]:
        """The published shape — the URL as stored, and nothing else."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "assignment": {
                "mode": self.assignment_mode,
                "services": list(self.services),
            },
            "enabled": self.enabled,
            "status": self.status,
        }


def _as_id(value: object) -> int:
    """A connection id, or 0 for an entry that does not carry one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _with_credential(url: str, connection_id: int) -> str:
    """``socks5h://host:port`` + ``EGRESS_PATH/secrets/<id>.txt``.

    The file holds the credential percent-encoded (``user:password``), so it is
    pasted into the authority as it is. No file means no login: the URL is
    returned untouched.
    """
    credential = _read_secret(connection_id)
    if not credential or "@" in url.split("://", 1)[-1]:
        return url
    scheme, _, remainder = url.partition("://")
    return f"{scheme}://{credential}@{remainder}"


def _clean_proxy(value: object) -> str:
    """A proxy URL the engine is willing to hand to yt-dlp, or ``""``."""
    text = str(value or "").strip()
    if not text or len(text) > 512 or any(character.isspace() for character in text):
        return ""
    scheme = text.split("://", 1)[0].lower() if "://" in text else ""
    if scheme not in PROXY_SCHEMES:
        return ""
    return text


def _clean_socks(value: object) -> str:
    """``host:port`` or a full URL from the watcher's ``status.json``."""
    text = str(value or "").strip()
    if not text or len(text) > 255 or any(character.isspace() for character in text):
        return ""
    if "://" in text:
        scheme, _, remainder = text.partition("://")
        if scheme.lower() in PROXY_SCHEMES and remainder:
            return f"{scheme.lower()}://{remainder}"
        return ""
    return f"socks5h://{text}"


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """The panel's connection list, in the order it must be tried."""

    connections: tuple[Connection, ...] = field(default_factory=tuple)
    test_interval: int = 0

    @classmethod
    def off(cls) -> Rule:
        """The direct connection: what an unreachable panel gives us."""
        return cls()

    @classmethod
    def from_payload(cls, payload: object) -> Rule:
        """Read the panel's answer tolerantly; anything odd means direct.

        A payload that cannot be understood is *not* an error: the engine would
        rather download directly than refuse a job because a settings endpoint
        changed shape.
        """
        if not isinstance(payload, dict):
            return cls.off()
        raw = payload.get("connections")
        if not isinstance(raw, list):
            return cls.off()
        found = tuple(
            connection
            for connection in (Connection.from_entry(entry) for entry in raw)
            if connection is not None
        )
        interval = payload.get("test_interval")
        return cls(
            connections=found,
            test_interval=interval if isinstance(interval, int) else 0,
        )

    # -- lookup ------------------------------------------------------------

    def candidates(self, service: object) -> tuple[Connection, ...]:
        """The enabled connections whose rule covers ``service``, in order."""
        return tuple(
            connection
            for connection in self.connections
            if connection.enabled and connection.applies_to(service)
        )

    def applies_to(self, service: object) -> bool:
        """Whether any enabled connection covers ``service``."""
        return bool(self.candidates(service))

    def proxy_for(self, service: object) -> str:
        """The ``--proxy`` value for one service, or ``""`` for a direct call.

        The first candidate whose check did not fail and which can actually be
        dialled wins. When every candidate failed its check the first dialable
        one is still used: a proxy that *might* work beats a guaranteed refusal.
        """
        candidates = self.candidates(service)
        for connection in candidates:
            if connection.status == "fail":
                continue
            url = connection.dial_url()
            if url:
                return url
        for connection in candidates:
            url = connection.dial_url()
            if url:
                return url
        return ""

    def describe(self) -> str:
        """One log-safe line: how many, and how they last checked out."""
        if not self.connections:
            return "off"
        enabled = [connection for connection in self.connections if connection.enabled]
        counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
        for connection in enabled:
            counts[connection.status] = counts.get(connection.status, 0) + 1
        return (
            f"{len(enabled)}/{len(self.connections)} enabled"
            f" (ok:{counts.get('ok', 0)} fail:{counts.get('fail', 0)}"
            f" unknown:{counts.get('unknown', 0)})"
        )

    def as_payload(self) -> dict[str, object]:
        """The body ``GET /api/egress`` answers with (the panel's own shape).

        Round-tripping the parsed list instead of the panel's raw JSON means a
        value this module refused (an unknown kind, an unparseable assignment)
        cannot leak back out of the endpoint a client reads.
        """
        return {
            "connections": [connection.as_entry() for connection in self.connections],
            "test_interval": self.test_interval,
        }


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_rule = Rule.off()
_expires = 0.0

#: Diagnostics, not state: the panel address is named once per process start,
#: and a panel that has never answered is counted so the silence is visible.
_panel_logged = False
_failures = 0


def log_configuration() -> str:
    """Say once, at startup, which panel the connection list is read from.

    The incident this prevents is silent: the engine asked a host that does not
    exist, every fetch failed, and the list stayed empty with nothing in the
    log to explain it. One line naming the address *and* the variable it came
    from settles the question the next time a proxy "does not work".
    """
    global _panel_logged
    url, source = _panel()
    with _lock:
        if _panel_logged:
            return url
        _panel_logged = True
    if source == "default":
        logger.info(
            "Egress panel: %s (default; neither %s is set)",
            url, " nor ".join(DASHBOARD_URL_ENV_NAMES),
        )
    else:
        logger.info("Egress panel: %s (from %s)", url, source)
    return url


def _note_unreadable(reason: object) -> None:
    """Count one unusable poll; warn when it has stopped being an accident."""
    global _failures
    with _lock:
        _failures += 1
        failures = _failures
        current = _rule.describe()
    logger.debug("Egress connections unavailable: %s", reason)
    if failures % FAILURE_WARN_THRESHOLD == 0:
        logger.warning(
            "Egress panel %s could not be read %d times in a row (%s); the "
            "engine keeps using %s. Check that the panel is running and that "
            "%s names it.",
            dashboard_url(), failures, reason, current,
            " / ".join(DASHBOARD_URL_ENV_NAMES),
        )


def _note_readable() -> None:
    """Reset the failure count; say so when it had been alarming."""
    global _failures
    with _lock:
        failures, _failures = _failures, 0
    if failures >= FAILURE_WARN_THRESHOLD:
        logger.info(
            "Egress panel %s answers again after %d unreadable polls",
            dashboard_url(), failures,
        )


def _fetch() -> Rule | None:
    """Fetch the panel's answer; ``None`` when it cannot be used."""
    token = os.environ.get("INTERNAL_API_TOKEN", "").strip()
    headers = {"X-Internal-Token": token} if token else {}
    request_obj = urllib.request.Request(  # noqa: S310 - fixed internal URL
        f"{dashboard_url()}{EGRESS_PATH}",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        _note_unreadable(exc)
        return None
    _note_readable()
    return Rule.from_payload(payload)


def rule() -> Rule:
    """The cached list (45 s, never raises, never blocks a job for long)."""
    global _rule, _expires
    now = time.monotonic()
    with _lock:
        if now < _expires:
            return _rule
    fetched = _fetch()
    with _lock:
        if fetched is not None:
            if fetched.describe() != _rule.describe():
                # One line per change: the count and the checks, never a URL.
                logger.info("Egress connections now %s", fetched.describe())
            _rule = fetched
        # Keep the last good answer: a panel restart must not reroute live jobs.
        _expires = now + CACHE_TTL_SECONDS
        return _rule


def reset_cache() -> None:
    """Forget the cached list and the poll diagnostics (tests)."""
    global _rule, _expires, _failures, _panel_logged, _socks, _socks_expires
    with _lock:
        _rule = Rule.off()
        _expires = 0.0
        _failures = 0
        _panel_logged = False
        _socks = ""
        _socks_expires = 0.0


def with_proxy(cmd: list[str], proxy_url: str) -> list[str]:
    """A copy of ``cmd`` with ``--proxy <url>`` before the trailing URL.

    Same shape as ``cookies.with_cookies``: yt-dlp accepts options after the URL,
    but keeping the flag in the option block reads the way the documentation
    does, and a future argument appended after the URL cannot swallow it.
    """
    if not cmd:
        return ["--proxy", proxy_url]
    return [*cmd[:-1], "--proxy", proxy_url, cmd[-1]]


def proxy_for_url(url: object) -> str:
    """The ``--proxy`` value for a job URL, or ``""``.

    The service is computed by the same extractor table the cookie lookup uses,
    so "reddit" means one thing in this repository. Any failure to decide is a
    direct connection: a download must never fail because the proxy rule could
    not be resolved.
    """
    try:
        from cookies import service_for_url
    except Exception:  # pragma: no cover - cookies is a hard requirement
        return ""
    try:
        service = service_for_url(url)
        return rule().proxy_for(service)
    except Exception:  # pragma: no cover - defensive: never break a job
        logger.debug("Could not resolve the egress rule for a URL", exc_info=True)
        return ""


__all__ = [
    "ASSIGNMENT_MODES",
    "CACHE_TTL_SECONDS",
    "Connection",
    "DASHBOARD_URL_ENV_NAMES",
    "DEFAULT_DASHBOARD_URL",
    "DEFAULT_EGRESS_ROOT",
    "EGRESS_PATH",
    "FAILURE_WARN_THRESHOLD",
    "KINDS",
    "PROXY_SCHEMES",
    "Rule",
    "SECRETS_DIRNAME",
    "STATUSES",
    "STATUS_FILENAME",
    "WIREGUARD_KIND",
    "dashboard_source",
    "dashboard_url",
    "egress_root",
    "log_configuration",
    "proxy_for_url",
    "reset_cache",
    "rule",
    "secret_path",
    "status_path",
    "with_proxy",
    "wireguard_socks",
]
