"""The service names the Storage page offers as suggestions.

The list comes from yt-dlp itself: every extractor knows the site it speaks to
through ``IE_NAME`` (``reddit``, ``youtube``, ``1tv.ru``), so the panel can
suggest exactly the names the engine will compute for a URL — no list of sites
to maintain here, and a new yt-dlp release widens the suggestions for free.

Three details keep this cheap and honest:

* **enumerating extractors is not free** (about 1900 classes in a current
  release), so the answer is cached once per process; the panel never pays for
  it per request.
* **generic extractors are dropped.** ``generic`` matches nearly every URL and
  is not a service anybody would add cookies for.
* **the order is alphabetical and stable** so the browser's filter is
  predictable and the test can pin a couple of known entries.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("koyura.dashboard")

#: Names that are not real services (they match everything).
_GENERIC = frozenset({"generic", "url", "fallback"})

_lock = threading.Lock()
_cached: tuple[str, ...] | None = None


def _extractor_names() -> list[str]:
    """Every ``IE_NAME`` yt-dlp knows, lowercased and de-duplicated."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except Exception:
        # yt-dlp missing is not fatal: the operator can still type a name.
        logger.warning("yt-dlp is not importable; the service suggestions are empty")
        return []
    names: set[str] = set()
    try:
        for extractor in gen_extractor_classes():
            raw = str(getattr(extractor, "IE_NAME", "") or "").strip().lower()
            # ``reddit:subreddit`` is the same service as ``reddit``.
            name = raw.split(":")[0].strip()
            if not name or name in _GENERIC:
                continue
            if len(name) > 64:
                continue
            names.add(name)
    except Exception:  # pragma: no cover - a plugin's import can raise
        logger.exception("Could not enumerate the yt-dlp extractors")
        return sorted(names)
    return sorted(names)


def service_names() -> tuple[str, ...]:
    """All suggestion names, computed once and reused for the process."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = tuple(_extractor_names())
            logger.info("Loaded %d service names from yt-dlp", len(_cached))
        return _cached


def reset_cache() -> None:
    """Forget the cached list (tests, and after a package upgrade)."""
    global _cached
    with _lock:
        _cached = None


def suggestions(query: object = "", limit: int = 20) -> list[str]:
    """Names matching ``query`` (substring), longest-prefix-first.

    A browser-side filter is the real interaction — this is what the server
    renders before any JavaScript runs, so the field is usable with JS off.
    """
    names = service_names()
    needle = str(query or "").strip().lower()
    if not needle:
        return list(names[:limit])
    if not limit or limit < 0:
        limit = 20
    exact = [name for name in names if name == needle]
    prefix = [name for name in names if name.startswith(needle) and name != needle]
    inside = [
        name for name in names
        if needle in name and name not in exact and name not in prefix
    ]
    return (exact + prefix + inside)[:limit]
