"""Channel references: what ``https://t.me/x`` and ``@x`` mean, and how to check one.

The operator types a link (``https://t.me/koyura_news``) or a handle
(``@koyura_news``) into the Channels form. Both mean the same channel, and the
bot needs a **username** to call ``getChatMember`` with, so the parsing lives
here — one place, one answer — instead of in the route.

Three kinds of reference are accepted, all pointing at a public channel:

    https://t.me/<name>       https://telegram.me/<name>    @<name>    <name>

What is *not* accepted is deliberate: a private invite link
(``https://t.me/+AbCdEf``) and a joinchat link carry no username, so the bot
cannot check membership of the channel they name, and a channel the gate cannot
verify must never be added. ``parse_reference`` answers ``None`` for those.

The checks themselves (:func:`check_channel`) are separated from the parsing so
the unit tests can pin the "bot is not an administrator → this channel does not
work" rule without a network.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: A Telegram public-channel username: 5–32 word characters, starting with a
#: letter. Telegram's own rule; a shorter or dashed name cannot exist.
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

#: The hosts a ``t.me`` link may use, and the ones that carry no username.
LINK_HOSTS = frozenset({"t.me", "telegram.me", "www.t.me", "www.telegram.me"})

#: Path prefixes that name a *message* inside a channel (``t.me/x/17``), which
#: is the same channel as ``t.me/x`` and therefore allowed.
_NUMERIC_SEGMENT = re.compile(r"^\d+$")

#: The verdicts :func:`check_channel` reports, as stored in ``check_error``.
OK = ""
NOT_ADMIN = "not_admin"
NOT_FOUND = "not_found"

#: The Telegram Bot API answers membership with these two statuses for a bot
#: that is not in the channel at all; anything else means the call worked.
_ADMIN_STATUSES = frozenset({"administrator", "creator"})


def channel_link(username: str) -> str:
    """``koyura_news`` -> ``https://t.me/koyura_news``."""
    name = str(username or "").strip().lstrip("@")
    return f"https://t.me/{name}" if name else ""


def parse_reference(raw: object) -> str | None:
    """The username a typed reference names, or ``None`` when there is none.

    ``https://t.me/koyura_news``, ``t.me/koyura_news/17``, ``@koyura_news`` and
    ``koyura_news`` all answer ``koyura_news``. A private invite
    (``t.me/+abc``), a ``joinchat`` link, another host and a malformed name
    answer ``None``: none of them can be checked with ``getChatMember``.
    """
    text = str(raw or "").strip()
    if not text:
        return None

    lowered = text.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break

    # A link: split off the host and take the first path segment that is not a
    # message id.
    head, _, tail = text.partition("/")
    host = head.lower().split(":", 1)[0]
    if host in LINK_HOSTS:
        for segment in tail.split("/"):
            piece = segment.strip()
            if not piece:
                continue
            if piece.startswith(("+", "joinchat", "s/", "c/")):
                return None
            if _NUMERIC_SEGMENT.match(piece):
                continue
            return _clean(piece)
        return None
    if "." in host:
        # Another host entirely (a channel's own site, a search result): the
        # username cannot be recovered from it.
        return None

    # A bare handle, with or without the ``@``.
    return _clean(text.split("/")[0])


def _clean(candidate: str) -> str | None:
    """Validate one candidate username (without the ``@``)."""
    name = str(candidate or "").strip().lstrip("@")
    return name if USERNAME_PATTERN.match(name) else None


def channel_title(title: object, username: str) -> str:
    """The name to show: the one Telegram gave, else the username."""
    text = str(title or "").strip()
    return text or (f"@{username}" if username else "")


def check_error_label(error: object) -> str:
    """The sentence the table shows for a stored verdict."""
    value = str(error or "")
    if value == NOT_ADMIN:
        return "bot is not an administrator"
    if value == NOT_FOUND:
        return "not found"
    return "ok"


# ---------------------------------------------------------------------------
# The Telegram call
# ---------------------------------------------------------------------------

def _bot_token() -> str:
    return os.environ.get("BOT_TOKEN", "").strip()


def _api_base() -> str:
    """The Bot API the bot itself talks to (the self-hosted one in compose)."""
    configured = os.environ.get("TELEGRAM_BOT_API_URL", "").strip()
    return (configured or "https://api.telegram.org").rstrip("/")


def _api_url(method: str) -> str:
    return f"{_api_base()}/bot{_bot_token()}/{method}"


async def check_channel(username: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Ask Telegram about one channel and about the bot's own membership.

    ``getChat`` gives the title (and confirms the channel exists), then
    ``getChatMember`` for the bot's own id answers whether it is an
    administrator there. Both are needed: a bot that is not an administrator
    cannot read a user's membership, so the channel is stored but reported as
    **not working** — the page says so in those words.

    Never raises: a network failure is a ``not_found``-style verdict with the
    reason in ``detail``, because the operator pressed a button and expects an
    answer, not a 500.
    """
    token = _bot_token()
    name = str(username or "").strip().lstrip("@")
    if not token:
        return {
            "ok": False,
            "error": NOT_FOUND,
            "detail": "BOT_TOKEN is not configured on the panel",
            "title": "",
            "username": name,
        }
    if not USERNAME_PATTERN.match(name):
        return {
            "ok": False,
            "error": NOT_FOUND,
            "detail": "not a public channel username",
            "title": "",
            "username": name,
        }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                _api_url("getChat"), params={"chat_id": f"@{name}"}
            )
            payload = _json(response)
            chat = payload.get("result") if payload.get("ok") else None
            if not isinstance(chat, dict):
                return {
                    "ok": False,
                    "error": NOT_FOUND,
                    "detail": _description(payload) or "channel not found",
                    "title": "",
                    "username": name,
                }
            title = str(chat.get("title") or "").strip()
            resolved = str(chat.get("username") or name).lstrip("@")

            bot_id = token.split(":", 1)[0]
            response = await client.get(
                _api_url("getChatMember"),
                params={"chat_id": f"@{resolved}", "user_id": bot_id},
            )
            member = _json(response)
            member_status = ""
            result = member.get("result")
            if member.get("ok") and isinstance(result, dict):
                member_status = str(result.get("status") or "")
    except httpx.HTTPError as exc:
        # Never the exception object itself: an httpx error message carries the
        # request URL, and the URL carries the bot token.
        logger.debug("Channel check for @%s failed: %s", name, type(exc).__name__)
        return {
            "ok": False,
            "error": NOT_FOUND,
            "detail": "Telegram did not answer",
            "title": "",
            "username": name,
        }

    is_admin = member_status in _ADMIN_STATUSES
    return {
        "ok": is_admin,
        "error": OK if is_admin else NOT_ADMIN,
        "detail": "" if is_admin else (
            "the bot must be an administrator of the channel, otherwise the "
            "subscription check cannot work"
        ),
        "title": title,
        "username": resolved,
        "status": member_status,
    }


def _json(response: httpx.Response) -> dict[str, Any]:
    """The API's JSON body, or an empty mapping when it is not one."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _description(payload: dict[str, Any]) -> str:
    """Telegram's own error sentence (never the token, never the URL)."""
    return str(payload.get("description") or "").strip()


__all__ = [
    "LINK_HOSTS",
    "NOT_ADMIN",
    "NOT_FOUND",
    "OK",
    "USERNAME_PATTERN",
    "channel_link",
    "channel_title",
    "check_channel",
    "check_error_label",
    "parse_reference",
]
