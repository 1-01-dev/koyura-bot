"""The service names the Cookies page offers as suggestions.

Two sources, in this order:

1. **yt-dlp**, when the panel's environment has it: every extractor knows the
   site it speaks to through ``IE_NAME`` (``reddit``, ``youtube``, ``1tv.ru``),
   so a suggestion is exactly the string the engine computes for a URL.
2. **a built-in catalogue** of well-known services, always present.

The catalogue is not a nicety. The dashboard image deliberately does **not**
install yt-dlp (the engine owns it), so relying on the import alone left the
suggestions empty in every Docker deployment and in CI: the operator had a
dropdown with nothing in it and no way to discover a name.

The two lists are merged rather than switched, so a yt-dlp that is present
only *widens* the answer: a popular service stays suggested even if an
upstream release renames or drops its extractor, and a yt-dlp that is absent
still gives a usable list.

Every name in the catalogue is a real yt-dlp ``IE_NAME`` (the tests check the
ones that can be checked without the package). That matters: the engine maps a
URL to a *service* through the same extractor table, so a name the engine never
computes would be a folder it never looks in — ``odysee`` is ``lbry`` upstream,
which is why ``lbry`` is the entry here and not the brand.

Three details keep this cheap and honest:

* **enumerating extractors is not free** (about 1900 classes in a current
  release), so the answer is cached once per process.
* **generic extractors are dropped.** ``generic`` matches nearly every URL and
  is not a service anybody would add cookies for.
* **the order is alphabetical and stable** so the browser's filter is
  predictable and the tests can pin a couple of known entries.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("koyura.dashboard")

#: Names that are not real services (they match everything).
_GENERIC = frozenset({"generic", "url", "fallback"})

#: Well-known services, alphabetically. Every entry is a real ``IE_NAME`` in a
#: current yt-dlp, which is what the engine keys its cookie folders by: ``lbry``
#: is the extractor behind odysee.com, ``kick`` behind kick.com, and so on. The
#: list is deliberately kept to services people actually download from and
#: would plausibly need a session for (age gates, private posts, region locks).
FALLBACK_SERVICES: tuple[str, ...] = (
    "9gag",
    "abc.net.au",
    "acast",
    "adultswim",
    "archive.org",
    "atresplayer",
    "audiomack",
    "bandcamp",
    "bbc",
    "bilibili",
    "bitchute",
    "bloomberg",
    "brightcove",
    "buzzfeed",
    "cbs",
    "cbsnews",
    "ccma",
    "ceskatelevize",
    "cnn",
    "comedycentral",
    "dailymotion",
    "dlive",
    "dw",
    "espn",
    "facebook",
    "flickr",
    "fox",
    "foxnews",
    "gamejolt",
    "giantbomb",
    "globo",
    "godtube",
    "googledrive",
    "hbo",
    "hidive",
    "history",
    "huya",
    "imdb",
    "imgur",
    "instagram",
    "iqiyi",
    "jeuxvideo",
    "jwplatform",
    "kick",
    "kika",
    "lbry",
    "linkedin",
    "mixcloud",
    "mlb",
    "mtv",
    "nba",
    "nbc",
    "ndr",
    "newgrounds",
    "niconico",
    "nicovideo",
    "npo",
    "npr",
    "nrk",
    "nytimes",
    "patreon",
    "pbs",
    "pinterest",
    "podomatic",
    "reddit",
    "rtbf",
    "rumble",
    "rutube",
    "sciencechannel",
    "soundcloud",
    "substack",
    "tbs",
    "tiktok",
    "tmz",
    "tumblr",
    "tv2",
    "tv4",
    "tvp",
    "twitcasting",
    "twitch",
    "twitter",
    "udemy",
    "ustream",
    "vbox7",
    "vevo",
    "vice",
    "vimeo",
    "vk",
    "washingtonpost",
    "weibo",
    "wwe",
    "xhamster",
    "youku",
    "youtube",
    "zdf",
    "zoom",
)

_lock = threading.Lock()
_cached: tuple[str, ...] | None = None


def _extractor_names() -> list[str]:
    """Every ``IE_NAME`` yt-dlp knows, lowercased and de-duplicated.

    Returns an empty list when yt-dlp is not installed, which is the normal
    case for the dashboard image: the caller then answers from the built-in
    catalogue. Nothing here may raise, or the page would 500 on a missing
    optional package.
    """
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except Exception:
        logger.info("yt-dlp is not installed; suggestions come from the built-in list")
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


def _merged_names() -> tuple[str, ...]:
    """yt-dlp's extractors plus the built-in catalogue, alphabetical."""
    merged = set(FALLBACK_SERVICES) | set(_extractor_names())
    return tuple(sorted(merged))


def service_names() -> tuple[str, ...]:
    """All suggestion names, computed once and reused for the process."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = _merged_names()
            logger.info("Loaded %d service names", len(_cached))
        return _cached


def reset_cache() -> None:
    """Forget the cached list (tests, and after a package upgrade)."""
    global _cached
    with _lock:
        _cached = None


def suggestions(query: object = "", limit: int = 20) -> list[str]:
    """Names matching ``query`` (substring), exact and prefix matches first.

    A browser-side filter is the real interaction — this is what the server
    renders before any JavaScript runs, so the field is usable with JS off.
    The answer is never empty: the built-in catalogue backs it.
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
