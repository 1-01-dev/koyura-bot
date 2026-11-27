"""The engine's side of the outbound proxy the panel configures.

The panel owns the decision — manual proxy, WireGuard tunnel, or off — and which
services it applies to. It publishes the answer on ``GET /api/egress`` (internal,
``X-Internal-Token``); this module fetches it, caches it, and turns it into the
``--proxy`` argument a job needs.

Three properties matter more than the request itself:

* **a panel that cannot answer is not an outage.** No answer (or an unreadable
  one) means "direct connection", which is the behaviour of every release before
  this one, and the last good answer is kept for a few minutes so a panel restart
  does not silently reroute traffic mid-job.
* **the secret stays in memory.** The panel sends the proxy URL over the internal
  token; nothing here writes it to disk or to a log line, and the service name is
  the only thing a log line names.
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

logger = logging.getLogger("koyura.engine.egress")

#: The panel publishes the rule; polled at most this often. Under a minute so a
#: change in the panel reaches the next job without a restart.
CACHE_TTL_SECONDS = 45.0

EGRESS_PATH = "/api/egress"

#: A panel that cannot answer in two seconds must not delay a download.
_TIMEOUT_SECONDS = 2.0

#: The schemes a proxy URL may use (the engine refuses anything else, whatever
#: the panel sent).
PROXY_SCHEMES = ("socks5", "socks5h", "http", "https")

ASSIGNMENT_MODES = ("all", "selected", "except")

MODES = ("off", "manual", "wireguard")

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
# The rule
# ---------------------------------------------------------------------------

class Rule:
    """``mode`` + ``proxy_url`` + ``assignment`` as the engine reads them."""

    __slots__ = ("mode", "proxy_url", "wg_socks", "assignment_mode", "services")

    def __init__(
        self,
        mode: str = "off",
        proxy_url: str = "",
        wg_socks: str = "",
        assignment_mode: str = "all",
        services: tuple[str, ...] = (),
    ) -> None:
        self.mode = mode
        self.proxy_url = proxy_url
        self.wg_socks = wg_socks
        self.assignment_mode = assignment_mode
        self.services = tuple(services)

    # -- construction ------------------------------------------------------

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
        mode = str(payload.get("mode") or "off").strip().lower()
        if mode not in MODES:
            return cls.off()
        assignment = payload.get("assignment")
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
        return cls(
            mode=mode,
            proxy_url=_clean_proxy(payload.get("proxy_url")),
            wg_socks=_clean_socks(payload.get("wg_socks")),
            assignment_mode=assignment_mode,
            services=services,
        )

    # -- lookup ------------------------------------------------------------

    def applies_to(self, service: object) -> bool:
        """Whether this rule covers ``service`` (mirrors the panel's method)."""
        if self.mode == "off":
            return False
        name = str(service or "").strip().lower()
        if self.assignment_mode == "all":
            return True
        if self.assignment_mode == "selected":
            return bool(name) and name in self.services
        return not (name and name in self.services)

    def proxy_for(self, service: object) -> str:
        """The ``--proxy`` value for one service, or ``""`` for a direct call.

        A WireGuard mode with no SOCKS address is a dead tunnel: it answers ``""``
        (direct) rather than a half-configured proxy that would fail every job.
        """
        if not self.applies_to(service):
            return ""
        if self.mode == "manual":
            return self.proxy_url
        return self.wg_socks

    def describe(self) -> str:
        """One log-safe line: the mode and the scope, never the URL."""
        if self.mode == "off":
            return "off"
        scope = self.assignment_mode
        if scope != "all":
            scope = f"{scope}:{','.join(self.services) or '-'}"
        return f"{self.mode} ({scope})"

    def as_payload(self) -> dict[str, object]:
        """The body ``GET /api/egress`` answers with (the panel's own shape).

        Round-tripping the parsed rule instead of the panel's raw JSON means a
        value this module refused (an unknown scheme, an empty SOCKS address)
        cannot leak back out of the endpoint a client reads.
        """
        return {
            "mode": self.mode,
            "proxy_url": self.proxy_url,
            "wg_socks": self.wg_socks,
            "assignment": {
                "mode": self.assignment_mode,
                "services": list(self.services),
            },
        }


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
    """Say once, at startup, which panel the egress rule is read from.

    The incident this prevents is silent: the engine asked a host that does not
    exist, every fetch failed, and the rule stayed ``off`` with nothing in the
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
    logger.debug("Egress rule unavailable: %s", reason)
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
    """The cached rule (45 s, never raises, never blocks a job for long)."""
    global _rule, _expires
    now = time.monotonic()
    with _lock:
        if now < _expires:
            return _rule
    fetched = _fetch()
    with _lock:
        if fetched is not None:
            if fetched.mode != _rule.mode or fetched.services != _rule.services:
                # One line per change: the mode and the scope, never the URL.
                logger.info("Egress rule now %s", fetched.describe())
            _rule = fetched
        # Keep the last good answer: a panel restart must not reroute live jobs.
        _expires = now + CACHE_TTL_SECONDS
        return _rule


def reset_cache() -> None:
    """Forget the cached rule and the poll diagnostics (tests)."""
    global _rule, _expires, _failures, _panel_logged
    with _lock:
        _rule = Rule.off()
        _expires = 0.0
        _failures = 0
        _panel_logged = False


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
    "DASHBOARD_URL_ENV_NAMES",
    "DEFAULT_DASHBOARD_URL",
    "EGRESS_PATH",
    "FAILURE_WARN_THRESHOLD",
    "MODES",
    "PROXY_SCHEMES",
    "Rule",
    "dashboard_source",
    "dashboard_url",
    "log_configuration",
    "proxy_for_url",
    "reset_cache",
    "rule",
    "with_proxy",
]
