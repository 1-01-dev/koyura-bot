"""The mandatory-subscription gate: which channels, and is this user in them.

The panel owns the channels (``required_channels`` in its database) and the
master switch (``settings`` row ``subscriptions.enabled``); the bot only reads
them from the internal API:

    GET {DASHBOARD_URL}/api/subscriptions
    X-Internal-Token: ****

    {"enabled": true,
     "channels": [{"id": 3, "title": "Koyura · news", "username": "koyura_news",
                   "link": "https://t.me/koyura_news", "position": 0}]}

Three rules decide whether the gate exists at all, and all three are answered
here rather than at the call sites:

* **the master switch is off** — there is no gate, even with channels stored.
  The owner asked for one toggle that turns the whole feature off, and "off"
  must mean the bot behaves exactly like it did before the feature existed;
* **the switch is on but no channel is enabled** — still no gate: a bot that
  would block every user behind a list of zero channels is a lockout;
* **a channel the bot cannot verify** (it is not an administrator there) is
  reported by the panel as such and never counted as a working one. Blocking a
  user behind a check that can only answer "no" would make the bot unusable, so
  the panel drops those rows before answering and the bot only sees the usable
  ones.

Nothing here talks to Telegram. The ``get_chat_member`` call is in
``handlers.subscription``, next to the screen that needs it.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_DASHBOARD_URL = "http://dashboard:8080"
SUBSCRIPTIONS_PATH = "/api/subscriptions"

# Same window as the language settings: the owner flips the switch, the bot
# notices within a minute without a restart.
TTL_SECONDS = 45.0

_TIMEOUT_SECONDS = 2.0

#: The membership statuses Telegram reports that mean "this user is subscribed".
#: ``restricted`` counts: a restricted member is a member whose account was
#: limited, which is not the same thing as having left.
SUBSCRIBED_STATUSES: frozenset[str] = frozenset(
    {"member", "administrator", "creator", "restricted"}
)

#: The statuses that mean "this user is not subscribed".
ABSENT_STATUSES: frozenset[str] = frozenset({"left", "kicked"})


@dataclass(frozen=True)
class Channel:
    """One channel the gate sends the user to."""

    id: int
    title: str
    username: str
    link: str
    #: ``@name`` when the channel has a username, else the stored link. This is
    #: what ``get_chat_member`` accepts and what the screen shows.
    handle: str = ""

    def __post_init__(self) -> None:
        if not self.handle:
            object.__setattr__(
                self, "handle", f"@{self.username}" if self.username else self.link
            )

    def label(self) -> str:
        """How the channel is named in the list and in the refusal."""
        return self.title or self.handle or self.link


@dataclass(frozen=True)
class Gate:
    """The answer to "is there a gate, and what does it ask for?"."""

    enabled: bool
    channels: tuple[Channel, ...] = ()

    def active(self) -> bool:
        """Whether the gate is actually in force.

        Both halves are needed: the switch the owner set **and** at least one
        channel that can be verified. Anything else answers ``False`` so a
        caller can ask this one question instead of repeating the rule.
        """
        return bool(self.enabled and self.channels)


def _dashboard_url() -> str:
    return os.environ.get("DASHBOARD_URL", "").strip() or DEFAULT_DASHBOARD_URL


def _headers() -> dict[str, str]:
    token = os.environ.get("INTERNAL_API_TOKEN", "").strip()
    return {"X-Internal-Token": token} if token else {}


def normalise_channel(raw: object, *, position: int = 0) -> Channel | None:
    """One channel from an API body, or ``None`` when it cannot be used.

    A row without a username and without a link cannot be opened and cannot be
    checked, so it is dropped instead of blocking the user behind a dead entry.
    """
    if not isinstance(raw, Mapping):
        return None
    username = str(raw.get("username") or "").strip().lstrip("@")
    link = str(raw.get("link") or "").strip()
    if not username and not link:
        return None
    try:
        identifier = int(str(raw.get("id") or "0"))
    except ValueError:
        identifier = 0
    return Channel(
        id=identifier,
        title=str(raw.get("title") or "").strip(),
        username=username,
        link=link,
        handle=f"@{username}" if username else link,
    )


def normalise_gate(payload: object) -> Gate:
    """Turn ``GET /api/subscriptions`` into a :class:`Gate`; never raises.

    A body we cannot read means "no gate": the alternative is a bot that stops
    answering because a panel is restarting, which is exactly the failure the
    settings fetch is designed to avoid.
    """
    if not isinstance(payload, Mapping):
        return Gate(enabled=False)
    enabled = bool(payload.get("enabled"))
    raw = payload.get("channels")
    channels: list[Channel] = []
    if isinstance(raw, (list, tuple)):
        for position, entry in enumerate(raw):
            channel = normalise_channel(entry, position=position)
            if channel is not None:
                channels.append(channel)
    return Gate(enabled=enabled, channels=tuple(channels))


def defaults() -> Gate:
    """No gate: the state of a bot that has never reached the panel."""
    return Gate(enabled=False)


async def fetch() -> Gate:
    """Ask the panel once; never raises."""
    url = f"{_dashboard_url()}{SUBSCRIPTIONS_PATH}"
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS, headers=_headers()
        ) as client:
            response = await client.get(url)
        if response.status_code != 200:
            logger.debug("Subscriptions unavailable (HTTP %s)", response.status_code)
            return defaults()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - the panel must never stop the bot
        logger.debug("Subscriptions unavailable (%s); no gate", exc)
        return defaults()
    gate = normalise_gate(payload)
    logger.debug(
        "Subscription gate %s with %d channel(s)",
        "on" if gate.enabled else "off",
        len(gate.channels),
    )
    return gate


_cache: Gate | None = None
_cache_at: float = 0.0


async def get(now: float | None = None) -> Gate:
    """The current gate, from cache when it is fresh enough."""
    global _cache, _cache_at
    moment = time.monotonic() if now is None else float(now)
    if _cache is not None and moment - _cache_at < TTL_SECONDS:
        return _cache
    gate = await fetch()
    _cache = gate
    _cache_at = moment
    return gate


def cached() -> Gate | None:
    """The last answer without a round trip (``None`` before the first fetch)."""
    return _cache


def reset_cache() -> None:
    """Test helper: forget the cached answer."""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def prime(gate: Gate, *, now: float | None = None) -> None:
    """Test helper: install an answer without a network round trip."""
    global _cache, _cache_at
    _cache = gate
    _cache_at = time.monotonic() if now is None else float(now)


def is_subscribed(status: object) -> bool:
    """Whether one ``get_chat_member`` status counts as a subscription.

    ``member``, ``administrator`` and ``creator`` (and a restricted member) are
    subscribed; ``left`` and ``kicked`` are not. An unknown status is treated as
    "not subscribed" rather than "yes": the gate exists to be passed on purpose.
    """
    return str(getattr(status, "value", status) or "").strip().lower() in SUBSCRIBED_STATUSES


__all__ = [
    "ABSENT_STATUSES",
    "SUBSCRIPTIONS_PATH",
    "SUBSCRIBED_STATUSES",
    "TTL_SECONDS",
    "Channel",
    "Gate",
    "cached",
    "defaults",
    "fetch",
    "get",
    "is_subscribed",
    "normalise_channel",
    "normalise_gate",
    "prime",
    "reset_cache",
]
