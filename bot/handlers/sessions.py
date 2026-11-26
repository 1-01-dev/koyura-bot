"""The picker sessions behind a preview card, and the buttons that address them.

A session records what the bot already knows about one Telegram message: the
link, the ``/api/info`` payload and whether the card is a photo or plain text.
It lives in this process only — it describes a chat message, not a user — so a
restart simply means the user sends the link again. ``config.STATE_TTL`` bounds
how long a card stays tappable.

The same module owns the addressing scheme, because a session key is also the
contract with buttons already sitting in old chats:

    "<chat_id>:<message_id>:<url_hash>"     the session key
    "<scheme>:<message_id>:<url_hash>:<action>"   the callback_data

``parse_callback_data`` accepts the current ``dl:`` scheme and the historical
``fmt:``/``qty:`` ones, so a year-old keyboard keeps working.
"""
from __future__ import annotations

import hashlib
import time

from . import config

# Sessions are keyed by chat + message + url, so the same link sent twice in one
# chat is two independent cards.
_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    """Short, stable fingerprint of a link (12 hex chars, 48 bits)."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def key(chat_id: int, message_id: int, fingerprint: str) -> str:
    return f"{chat_id}:{message_id}:{fingerprint}"


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def remember(
    *,
    url: str,
    chat_id: int,
    user_id: int,
    username: str,
    info: dict,
    message_id: int,
    photo: bool,
) -> dict:
    """Record a freshly posted preview card and return its session."""
    entry = {
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id,
        "username": username,
        "info": info,
        "message_id": message_id,
        "photo": photo,
        # One card runs one job: a second format on the same message would race
        # for the same caption and upload the wrong file.
        "job_active": False,
        "created": time.time(),
    }
    _sessions[key(chat_id, message_id, url_hash(url))] = entry
    return entry


def get(chat_id: int, message_id: int, fingerprint: str) -> dict | None:
    return _sessions.get(key(chat_id, message_id, fingerprint))


def live_count() -> int:
    return len(_sessions)


def forget_all() -> None:
    """Drop every session (tests; also a clean slate after a config change)."""
    _sessions.clear()


def rebind(
    chat_id: int, message_id: int, fingerprint: str, new_message_id: int
) -> dict | None:
    """Move a session to the message its keyboard now lives on.

    Every stage posts a new message, and a failed job re-posts the format
    keyboard under the error card. The session has to follow, otherwise the
    buttons a user can actually see would answer "session expired". Re-keying
    keeps the entry (and its ``created`` stamp) so the ten-minute window is not
    extended by a retry.
    """
    old_key = key(chat_id, message_id, fingerprint)
    entry = _sessions.get(old_key)
    if entry is None:
        return None
    entry["message_id"] = new_message_id
    _sessions[key(chat_id, new_message_id, fingerprint)] = entry
    if old_key != key(chat_id, new_message_id, fingerprint):
        del _sessions[old_key]
    return entry


def evict_stale(now: float | None = None) -> int:
    """Drop sessions older than ``STATE_TTL``; returns how many went away.

    Called on every link and every button, so an idle bot cleans up lazily
    instead of running a timer.
    """
    moment = time.time() if now is None else now
    expired = [
        session_key
        for session_key, entry in _sessions.items()
        if moment - entry["created"] > config.STATE_TTL
    ]
    for session_key in expired:
        del _sessions[session_key]
    return len(expired)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

def parse_callback_data(data: str) -> tuple[int, str, str] | None:
    """``"dl:42:<hash>:137"`` -> ``(42, "<hash>", "137")``.

    The action is the last part and the hash the one before it, which is what
    makes a yt-dlp format id (no colons) and a long id both fit. Anything that
    does not parse returns ``None``: a broken button is answered, not chased.
    """
    parts = (data or "").split(":")
    if len(parts) < 4:
        return None
    try:
        message_id = int(parts[1])
    except ValueError:
        return None
    return message_id, parts[-2], parts[-1]


def candidate_chat_ids(query, message_id: int) -> list[int]:
    """Chats worth probing for a session, the message's own chat first.

    ``query.message`` is optional in the types and may be an inaccessible
    message, so every attribute access is defensive; the stored sessions are the
    fallback that keeps an old card working.
    """
    candidates: list[int] = []
    chat = getattr(getattr(query, "message", None), "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id:
        candidates.append(int(chat_id))
    for entry in list(_sessions.values()):
        if entry.get("message_id") == message_id and entry.get("chat_id"):
            candidates.append(int(entry["chat_id"]))
    ordered: list[int] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def find(query, message_id: int, fingerprint: str) -> dict | None:
    """The session a button points at, or ``None`` when it has expired.

    Both the id from the callback data and the id Telegram reports are tried:
    forwards and copies change the latter, the data keeps the original.
    """
    current_id = getattr(getattr(query, "message", None), "message_id", None)
    for chat_id in candidate_chat_ids(query, message_id):
        for candidate_id in (message_id, current_id):
            if not candidate_id:
                continue
            entry = get(chat_id, candidate_id, fingerprint)
            if entry is not None:
                return entry
    return None
