"""User-supplied URL validation.

The download engine is reachable from the bot and the dashboard, which both
forward arbitrary user input.  Validating here (and not only in the bot) keeps
the engine from being used as a proxy to localhost / private networks, or to
fetch non-HTTP schemes such as ``file:`` or ``data:``.

Rules enforced:
  * only ``http`` and ``https``
  * bounded length, no credentials in the URL, no control characters
  * hostname must resolve and every resolved address must be globally routable
  * loopback / private / link-local / reserved / multicast targets are refused

Set ``ALLOW_PRIVATE_URLS=true`` only for local development: it skips the DNS
resolution check (hostnames resolving to private addresses) but keeps the
scheme, length and IP-literal rules.
"""
from __future__ import annotations

import ipaddress
import os
import queue
import socket
import threading
from collections.abc import Iterable
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are never resolvable to a public service.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
)

_RESOLVE_TIMEOUT_SECONDS = 5.0


class UnsafeURLError(ValueError):
    """Raised when a user-supplied URL must not be fetched."""


def allow_private_urls() -> bool:
    """Return True when private-address checks are explicitly disabled."""
    return os.environ.get("ALLOW_PRIVATE_URLS", "").strip().lower() in {"1", "true", "yes"}


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to IP strings, bounded by a watchdog timeout."""
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            result.put((True, [info[4][0] for info in infos]))
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            result.put((False, exc))

    thread = threading.Thread(target=_worker, name="koyura-dns", daemon=True)
    thread.start()
    try:
        ok, payload = result.get(timeout=_RESOLVE_TIMEOUT_SECONDS)
    except queue.Empty as exc:
        raise UnsafeURLError("Could not resolve the URL host (timed out)") from exc

    if not ok:
        raise UnsafeURLError("Could not resolve the URL host") from payload  # type: ignore[misc]
    return list(payload)  # type: ignore[arg-type]


def _check_host(host: str) -> None:
    host = host.rstrip(".").lower()
    if not host:
        raise UnsafeURLError("URL has no host")

    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError("URL host is not allowed")

    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None

    if literal is not None:
        if _blocked_ip(literal):
            raise UnsafeURLError("URL points to a local or private address")
        return

    if allow_private_urls():
        return

    for address in _resolve_host(host):
        try:
            ip = ipaddress.ip_address(address.strip("[]"))
        except ValueError as exc:
            raise UnsafeURLError("URL host resolved to an invalid address") from exc
        if _blocked_ip(ip):
            raise UnsafeURLError("URL points to a local or private address")


def validate_url(url: object) -> str:
    """Return the URL if it is safe to fetch, otherwise raise UnsafeURLError."""
    if not isinstance(url, str):
        raise UnsafeURLError("URL must be a string")

    url = url.strip()
    if not url:
        raise UnsafeURLError("URL is empty")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError(f"URL is longer than {MAX_URL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise UnsafeURLError("URL contains whitespace or control characters")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError("URL could not be parsed") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError("Only http and https URLs are supported")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    host = parts.hostname or ""
    if not host:
        raise UnsafeURLError("URL has no host")

    _check_host(host)
    return url


def first_unsafe_reason(url: object) -> str | None:
    """Return a human-readable rejection reason, or None when the URL is safe."""
    try:
        validate_url(url)
    except UnsafeURLError as exc:
        return str(exc)
    return None


def validate_urls(urls: Iterable[object]) -> list[str]:
    """Validate each URL, returning the safe ones (invalid entries are dropped)."""
    safe: list[str] = []
    for candidate in urls:
        try:
            safe.append(validate_url(candidate))
        except UnsafeURLError:
            continue
    return safe
