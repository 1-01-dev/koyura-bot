"""What the chat says when a download needs cookies.

The engine answers a spent session with ``code: "cookies_required"`` and the
service name it computed from the URL — it never names a site in a sentence, so
nothing here hard-codes one either. This module turns that into the sentence a
person can act on, and decides whether to include the panel link.

Two audiences, one message body:

* **everybody** is told which service wants a session and where to add it
  ("Reddit requires cookies — add them in the panel → Storage");
* **the operator** gets the same sentence plus a link, because they are the one
  who can actually open the panel. "Privileged" is not a new concept: the panel
  address is configuration (``DASHBOARD_PUBLIC_URL``), and only users listed in
  ``PRIVILEGED_USER_IDS`` get the clickable version.

The link is *not* built from ``DASHBOARD_URL``: that one is the internal compose
address (``http://dashboard:8080``), unreachable from a phone. A deployment that
wants the link sets ``DASHBOARD_PUBLIC_URL``; without it nobody sees a link, and
the message still reads correctly.
"""
from __future__ import annotations

import logging
import os

import i18n

logger = logging.getLogger(__name__)

#: Internal compose address; not useful in a chat, kept for the fallback check.
_INTERNAL_MARKERS = ("dashboard:8080", "koyura-engine", "127.0.0.1", "localhost")

#: Where the cookies page lives (the section was renamed from "storage").
COOKIES_PATH = "/admin/cookies"
#: Where the egress rule lives. A blocked address is fixed here, not in Cookies.
PROXY_PATH = "/admin/proxy"


def privileged_ids() -> frozenset[int]:
    """Telegram user ids allowed to see the panel link (``PRIVILEGED_USER_IDS``).

    Comma-separated, whitespace tolerated, unparseable entries dropped: a typo
    in the environment must not make the bot crash on the first download.
    """
    raw = os.environ.get("PRIVILEGED_USER_IDS", "")
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            ids.add(int(text))
        except ValueError:
            logger.warning("PRIVILEGED_USER_IDS: %r is not a number, ignored", text[:20])
    return frozenset(ids)


def is_privileged(user_id: object) -> bool:
    """Whether one Telegram id may be shown the panel link."""
    try:
        return int(str(user_id)) in privileged_ids()
    except (TypeError, ValueError):
        return False


def public_dashboard_url() -> str:
    """The panel address a phone can open, or ``""`` when none is configured.

    ``DASHBOARD_URL`` is the internal one and is deliberately not used: a link
    to ``http://dashboard:8080`` would fail for every user, which is worse than
    no link at all.
    """
    value = os.environ.get("DASHBOARD_PUBLIC_URL", "").strip().rstrip("/")
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        return ""
    lowered = value.lower()
    if any(marker in lowered for marker in _INTERNAL_MARKERS):
        logger.debug("DASHBOARD_PUBLIC_URL looks internal; not linking it in a chat")
        return ""
    return value


def cookies_url() -> str:
    """The Storage page URL for a privileged user, or ``""``."""
    base = public_dashboard_url()
    return f"{base}{COOKIES_PATH}" if base else ""


def proxy_url() -> str:
    """The Proxy page URL for a privileged user, or ``""``."""
    base = public_dashboard_url()
    return f"{base}{PROXY_PATH}" if base else ""


def needs_cookies(status: object) -> bool:
    """Whether an engine status payload asks for a cookie session."""
    if not isinstance(status, dict):
        return False
    return str(status.get("error_code") or "") == "cookies_required"


def is_blocked(status: object) -> bool:
    """Whether the engine said the site refuses this server's address.

    A separate code from ``cookies_required`` on purpose: the two failures look
    alike in a log (both are 403s) and need opposite advice.
    """
    if not isinstance(status, dict):
        return False
    return str(status.get("error_code") or "") == "service_blocked"


def service_of(status: object) -> str:
    """The service a ``cookies_required`` payload names (``""`` when absent)."""
    if not isinstance(status, dict):
        return ""
    return str(status.get("cookies_service") or "").strip()


def _service_label(service: str, lang: object) -> str:
    """``reddit`` → ``Reddit`` for the middle of a sentence.

    Only the capitalisation is ours; the name came from the engine's extractor
    table, so no list of sites lives here. A dotted host (``1tv.ru``) is left
    as it is — capitalising a domain would look like a typo.
    """
    name = (service or "").strip()
    if not name:
        return i18n.t("download.cookies_generic_service", lang)
    if "." in name:
        return name
    return name[:1].upper() + name[1:]


def message(status: object, *, user_id: object = None, lang: object = None) -> str:
    """The sentence to show for a ``cookies_required`` status.

    Falls back to the catalogued generic line when the payload has no service,
    so an older engine (or a truncated answer) still produces something the
    user can read instead of an empty card.
    """
    service = _service_label(service_of(status), lang)
    link = cookies_url() if is_privileged(user_id) else ""

    if link:
        return i18n.t(
            "download.cookies_required_admin",
            lang,
            service=service,
            panel_link=link,
        )
    return i18n.t("download.cookies_required", lang, service=service)


def blocked_message(status: object, *, user_id: object = None,
                    lang: object = None) -> str:
    """The sentence for a ``service_blocked`` status.

    Says the address was refused and that a proxy is the fix — never "add
    cookies", which is the one thing that cannot help here. The panel link
    goes to the Proxy page, not to Cookies, for the same reason.
    """
    service = _service_label(service_of(status), lang)
    link = proxy_url() if is_privileged(user_id) else ""

    if link:
        return i18n.t(
            "download.service_blocked_admin",
            lang,
            service=service,
            panel_link=link,
        )
    return i18n.t("download.service_blocked", lang, service=service)


def message_for_service(service: object, *, user_id: object = None,
                        lang: object = None) -> str:
    """The same sentence for a service named directly (used by the info stage)."""
    return message(
        {"error_code": "cookies_required", "cookies_service": str(service or "")},
        user_id=user_id,
        lang=lang,
    )
