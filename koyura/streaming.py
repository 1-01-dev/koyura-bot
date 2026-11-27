"""Turn a streaming-service link into a track this engine can download.

Spotify, Apple Music and Deezer are DRM-only: ``yt-dlp`` refuses all three by
design (``ERROR: [DRM] The requested site is known to use DRM protection``), so
the link can never be the source of the file. What *is* available without a key
is the metadata — the title, the artist, the duration and the cover — and that
is enough to find the same recording somewhere that can be downloaded.

The module is therefore three pure steps plus two network calls:

1. :func:`parse_track_url` — recognise the link and name the service and the
   track id, or say why the link cannot be handled (an album, a playlist, a
   service that needs a token);
2. :func:`fetch_metadata` — read title/artist/duration/cover from the service's
   own public endpoint (oEmbed, the embed page, the iTunes lookup API, the Deezer
   API), never from the file that will eventually be downloaded;
3. :func:`find_candidates` — search ``ytsearch`` and, when that yields nothing,
   ``scsearch``; keep the results whose title matches and whose duration is
   within tolerance, skip DRM-protected ones, and label them ``1 · 3:33`` so the
   chat can offer a choice without ever naming where the audio comes from.

Nothing here talks to Flask or to the chat. The caller (``app.py``) injects the
yt-dlp runner and the HTTP fetcher, which is what makes every rule above
testable without a network.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

#: How many search hits one provider may return before filtering.
SEARCH_COUNT = 10

#: How many alternatives the card offers. More buttons than this do not fit a
#: phone keyboard, and the first few are the ones a human would have picked.
MAX_CANDIDATES = 8

#: A candidate must not be much longer or shorter than the track it stands in
#: for: a 3:33 song and a 5:59 "extended mix" are not the same recording.
DURATION_TOLERANCE_RATIO = 0.15
DURATION_TOLERANCE_SECONDS = 20

#: Share of the track title's words that must appear in a candidate's title.
MIN_TITLE_MATCH = 0.6

#: A link budget, so one slow service cannot hold the info request forever.
HTTP_TIMEOUT_SECONDS = 10.0

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SERVICE_LABELS: dict[str, str] = {
    "spotify": "Spotify",
    "apple_music": "Apple Music",
    "deezer": "Deezer",
    "tidal": "Tidal",
    "yandex_music": "Яндекс Музыка",
    "amazon_music": "Amazon Music",
}

#: Services whose catalogue is behind a token or an app-only API. Saying so is
#: the polite answer; guessing at an undocumented endpoint would not be.
UNSUPPORTED_SERVICES = frozenset({"tidal", "yandex_music", "amazon_music"})

#: ``<service>: <env>`` overrides of ``TRACK_SUBSTITUTE``.
SERVICE_ENV: dict[str, str] = {
    "spotify": "SPOTIFY_SUBSTITUTE",
    "apple_music": "APPLE_MUSIC_SUBSTITUTE",
    "deezer": "DEEZER_SUBSTITUTE",
}

#: The yt-dlp search prefixes, in the order they are tried. SoundCloud is the
#: fallback: YouTube is the better match, but it is not always there.
SEARCH_PREFIXES: dict[str, str] = {"youtube": "ytsearch", "soundcloud": "scsearch"}

#: Words that carry no identifying meaning in a track title. They are dropped
#: from both sides before comparing, so "(Official Audio)" in a search hit does
#: not stop it from matching the title it is actually a recording of.
_NOISE_WORDS = frozenset(
    {
        "official", "video", "videoclip", "audio", "lyrics", "lyric", "hd", "hq",
        "4k", "mv", "m", "v", "music", "clip", "visualizer", "visualiser",
        "remaster", "remastered", "explicit", "clean", "version", "edition",
        "deluxe", "feat", "ft", "featuring", "prod", "topic", "the",
    }
)
_WORD_RE = re.compile(r"[0-9a-z\u0400-\u04ff]+")
_BRACKETED_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_DURATION_RE = re.compile(r'"duration"\s*:\s*(\d+)')
_ARTISTS_RE = re.compile(r'"artists"\s*:\s*\[\s*\{\s*"name"\s*:\s*"((?:[^"\\]|\\.)*)"')


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TrackError(Exception):
    """A track link that cannot become a download, with a code the chat knows.

    ``code`` is the stable identifier the bot turns into a sentence
    (``track_not_found``, ``track_unsupported_type``, …); ``message`` is the
    English explanation for a log line or a caller with no catalogue; and
    ``service`` is the human service name when one is worth naming.
    """

    def __init__(self, code: str, message: str, *, service: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.service = service

    def payload(self) -> dict[str, str]:
        """The JSON body ``/api/info`` answers with (additive, never a stack)."""
        body = {"error": self.message, "code": self.code}
        if self.service:
            body["service"] = self.service
        return body


# ---------------------------------------------------------------------------
# 1. Recognition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackRef:
    """One track on a streaming service: what the link named, nothing more."""

    service: str
    track_id: str
    url: str

    @property
    def service_label(self) -> str:
        return SERVICE_LABELS.get(self.service, self.service)


def _segments(parts) -> list[str]:
    return [segment for segment in parts.path.split("/") if segment]


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


#: How to name an unsupported Spotify link kind in a sentence ("not an album").
_KIND_ARTICLE = {"album": "an album", "playlist": "a playlist", "artist": "an artist",
                 "show": "a podcast", "episode": "a podcast episode",
                 "audiobook": "an audiobook", "user": "a profile"}


def _kind_phrase(kind: str) -> str:
    """``album`` -> ``an album``; an unknown kind keeps its own name."""
    return _KIND_ARTICLE.get(kind, f"a {kind}")


def _spotify_ref(kind: str, track_id: str) -> TrackRef:
    """One Spotify segment pair, or the reason it cannot be used."""
    label = SERVICE_LABELS["spotify"]
    if kind != "track":
        raise TrackError(
            "track_unsupported_type",
            f"Only single {label} tracks are supported, not {_kind_phrase(kind)}.",
            service=label,
        )
    return TrackRef("spotify", track_id, f"https://open.spotify.com/track/{track_id}")


def _parse_spotify(host: str, parts, text: str) -> TrackRef | None:
    if text.lower().startswith("spotify:"):
        # spotify:track:<id> (and the historical spotify:user:<id>:playlist:<id>)
        segments = [segment for segment in text.split(":") if segment]
        if len(segments) < 3:
            raise TrackError(
                "track_unsupported_type",
                "This Spotify link does not name a single track.",
                service=SERVICE_LABELS["spotify"],
            )
        return _spotify_ref(segments[1].lower(), segments[2])

    if host not in ("open.spotify.com", "play.spotify.com", "embed.spotify.com"):
        return None
    segments = _segments(parts)
    # A share link may carry a market prefix (``/intl-ru/track/…``) or an embed
    # path (``/embed/track/…``); both name the same track.
    segments = [s for s in segments if not s.startswith("intl-")]
    if segments and segments[0] in ("embed", "embed-podcast"):
        segments = segments[1:]
    if not segments:
        raise TrackError(
            "track_unsupported_type",
            "This Spotify link does not name a single track.",
            service=SERVICE_LABELS["spotify"],
        )
    track_id = segments[1] if len(segments) > 1 else ""
    if not track_id:
        raise TrackError(
            "track_unsupported_type",
            "This Spotify link does not name a single track.",
            service=SERVICE_LABELS["spotify"],
        )
    return _spotify_ref(segments[0].lower(), track_id)


def _apple_id(parts) -> tuple[str, str]:
    """``(kind, id)`` for one Apple Music link: a song id or an album id."""
    segments = _segments(parts)
    if segments and re.fullmatch(r"[a-z]{2}", segments[0]):
        segments = segments[1:]          # storefront code: /us/album/…
    if not segments:
        return "", ""
    kind = segments[0].lower()
    query = parse_qs(parts.query)
    if kind == "album":
        # A song page is an album page with ``?i=<track id>``; without it the
        # link is the album, which has no single duration to match against.
        song_id = (query.get("i") or [""])[0]
        return kind, song_id
    if kind in ("song", "music-video"):
        return kind, segments[-1]
    return kind, ""


def _parse_apple_music(host: str, parts) -> TrackRef | None:
    if not host.endswith(("music.apple.com", "itunes.apple.com")):
        return None
    label = SERVICE_LABELS["apple_music"]
    kind, track_id = _apple_id(parts)
    if kind == "album" and not track_id:
        raise TrackError(
            "track_unsupported_type",
            f"Only single {label} songs are supported, not an album.",
            service=label,
        )
    if kind == "artist" or kind == "playlist":
        raise TrackError(
            "track_unsupported_type",
            f"Only single {label} songs are supported, not {_kind_phrase(kind)}.",
            service=label,
        )
    if not track_id:
        raise TrackError(
            "track_unsupported_type",
            f"This {label} link does not name a single song.",
            service=label,
        )
    return TrackRef("apple_music", track_id, f"https://music.apple.com/song/{track_id}")


def _parse_deezer(host: str, parts) -> TrackRef | None:
    if host != "deezer.com" and not host.endswith(".deezer.com"):
        return None
    label = SERVICE_LABELS["deezer"]
    segments = _segments(parts)
    if segments and re.fullmatch(r"[a-z]{2}", segments[0]):
        segments = segments[1:]          # market prefix: /us/track/…
    if len(segments) < 2:
        raise TrackError(
            "track_unsupported_type",
            f"This {label} link does not name a single track.",
            service=label,
        )
    kind, track_id = segments[0].lower(), segments[1]
    if kind != "track":
        raise TrackError(
            "track_unsupported_type",
            f"Only single {label} tracks are supported, not {_kind_phrase(kind)}.",
            service=label,
        )
    return TrackRef("deezer", track_id, f"https://www.deezer.com/track/{track_id}")


def _unsupported_service(host: str) -> str | None:
    """The service name for a host whose catalogue needs a key, if any."""
    if host.endswith("tidal.com"):
        return "tidal"
    if host.startswith("music.yandex.") or host.endswith("music.yandex.ru"):
        return "yandex_music"
    if host.startswith("music.amazon."):
        return "amazon_music"
    return None


def parse_track_url(url: object) -> TrackRef | None:
    """The track a streaming link names, or ``None`` for an ordinary link.

    Raises :class:`TrackError` when the link *is* from a supported service but
    cannot be used: an album, a playlist, an artist page, or a service whose
    catalogue is behind a key. Those are answers, not failures — the chat shows
    them instead of a raw yt-dlp DRM line.
    """
    text = str(url or "").strip()
    if not text:
        return None
    if text.lower().startswith("spotify:"):
        return _parse_spotify("", None, text)

    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = _strip_www((parts.hostname or "").rstrip(".").lower())
    if not host:
        return None

    unsupported = _unsupported_service(host)
    if unsupported:
        label = SERVICE_LABELS[unsupported]
        raise TrackError(
            "track_unsupported_service",
            f"{label} needs an API key, so this service is not supported.",
            service=label,
        )

    for ref in (
        _parse_spotify(host, parts, text),
        _parse_deezer(host, parts),
        _parse_apple_music(host, parts),
    ):
        if ref is not None:
            return ref
    return None


# ---------------------------------------------------------------------------
# 2. Metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackMeta:
    """What the streaming service knows about the track the link named."""

    service: str
    track_id: str
    title: str
    artist: str = ""
    duration: float | None = None
    thumbnail: str = ""

    @property
    def service_label(self) -> str:
        return SERVICE_LABELS.get(self.service, self.service)

    @property
    def query(self) -> str:
        """The search phrase: ``"<artist> <title>"``, or the title alone."""
        return " ".join(part for part in (self.artist, self.title) if part).strip()


class Fetcher:
    """The two HTTP shapes the metadata endpoints need, over ``urllib``.

    A class rather than two functions so a test can pass its own: nothing else
    in this module reaches the network.
    """

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def text(self, url: str) -> str:
        request = urllib.request.Request(  # noqa: S310 - fixed service URL
            url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, "replace")

    def json(self, url: str) -> dict:
        """A JSON object body; a non-object answer raises ``ValueError``."""
        try:
            payload = json.loads(self.text(url) or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Service answer is not JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Service answer is not a JSON object")
        return payload


def _unescape_json_string(raw: str) -> str:
    """``Rick \\u0026 Astley`` -> ``Rick & Astley`` (a JSON string body)."""
    try:
        return json.loads(f'"{raw}"')
    except (json.JSONDecodeError, ValueError):
        return raw


def parse_spotify_oembed(payload: object) -> tuple[str, str]:
    """``(title, thumbnail)`` from an oEmbed body (either may be empty)."""
    if not isinstance(payload, dict):
        return "", ""
    title = str(payload.get("title") or "").strip()
    thumbnail = str(payload.get("thumbnail_url") or "").strip()
    return title, thumbnail


def parse_spotify_embed(html: str) -> tuple[str, float | None]:
    """``(artist, duration_seconds)`` from the embed page, best effort.

    The page carries the artist in a JSON blob and the duration in
    milliseconds; both are optional, because the page's shape is not a contract.
    A track with no artist is still searchable by its title alone.
    """
    text = str(html or "")
    artist = ""
    match = _ARTISTS_RE.search(text)
    if match:
        artist = _unescape_json_string(match.group(1)).strip()
    duration = None
    match = _DURATION_RE.search(text)
    if match:
        milliseconds = int(match.group(1))
        if milliseconds > 0:
            duration = round(milliseconds / 1000.0, 1)
    return artist, duration


def parse_itunes_lookup(payload: dict) -> TrackMeta | None:
    """The song out of an ``itunes.apple.com/lookup`` answer, or ``None``."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return None
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("wrapperType") or "") not in ("track", ""):
            continue
        if entry.get("kind") not in (None, "song"):
            continue
        title = str(entry.get("trackName") or "").strip()
        if not title:
            continue
        milliseconds = entry.get("trackTimeMillis")
        duration = None
        if isinstance(milliseconds, (int, float)) and milliseconds > 0:
            duration = round(float(milliseconds) / 1000.0, 1)
        artwork = str(entry.get("artworkUrl100") or "").strip()
        return TrackMeta(
            service="apple_music",
            track_id=str(entry.get("trackId") or ""),
            title=title,
            artist=str(entry.get("artistName") or "").strip(),
            duration=duration,
            thumbnail=_apple_artwork(artwork),
        )
    return None


def _apple_artwork(url: str) -> str:
    """Up-scale Apple's 100×100 artwork to something a card can use."""
    return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", url) if url else ""


def parse_deezer_track(payload: dict) -> TrackMeta | None:
    """The track out of a Deezer ``/track/<id>`` answer, or ``None``."""
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    track_id = payload.get("id")
    if track_id is None:
        return None
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    raw_artist = payload.get("artist")
    raw_album = payload.get("album")
    artist: dict = raw_artist if isinstance(raw_artist, dict) else {}
    album: dict = raw_album if isinstance(raw_album, dict) else {}
    duration = payload.get("duration")
    return TrackMeta(
        service="deezer",
        track_id=str(track_id),
        title=title,
        artist=str(artist.get("name") or "").strip(),
        duration=float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        thumbnail=str(album.get("cover_xl") or album.get("cover_big") or "").strip(),
    )


def _spotify_metadata(ref: TrackRef, http) -> TrackMeta:
    label = ref.service_label
    title, thumbnail = "", ""
    try:
        title, thumbnail = parse_spotify_oembed(
            http.json(f"https://open.spotify.com/oembed?url={ref.url}")
        )
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        title, thumbnail = "", ""
    artist, duration = "", None
    if title:
        try:
            artist, duration = parse_spotify_embed(
                http.text(f"https://open.spotify.com/embed/track/{ref.track_id}")
            )
        except (urllib.error.URLError, OSError, ValueError):
            artist, duration = "", None
    if not title:
        raise TrackError(
            "track_metadata_failed",
            f"Could not read this {label} track's details.",
            service=label,
        )
    return TrackMeta(
        service=ref.service,
        track_id=ref.track_id,
        title=title,
        artist=artist,
        duration=duration,
        thumbnail=thumbnail,
    )


def _apple_metadata(ref: TrackRef, http) -> TrackMeta:
    label = ref.service_label
    try:
        payload = http.json(f"https://itunes.apple.com/lookup?id={ref.track_id}&entity=song")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise TrackError(
            "track_metadata_failed",
            f"Could not read this {label} track's details.",
            service=label,
        ) from exc
    meta = parse_itunes_lookup(payload)
    if meta is None:
        raise TrackError(
            "track_not_found",
            f"{label} does not know a song with this id.",
            service=label,
        )
    return TrackMeta(
        service=ref.service, track_id=ref.track_id, title=meta.title,
        artist=meta.artist, duration=meta.duration, thumbnail=meta.thumbnail,
    )


def _deezer_metadata(ref: TrackRef, http) -> TrackMeta:
    label = ref.service_label
    try:
        payload = http.json(f"https://api.deezer.com/track/{ref.track_id}")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise TrackError(
            "track_metadata_failed",
            f"Could not read this {label} track's details.",
            service=label,
        ) from exc
    meta = parse_deezer_track(payload)
    if meta is None:
        raise TrackError(
            "track_not_found",
            f"{label} does not know a track with this id.",
            service=label,
        )
    return meta


_METADATA_READERS: dict[str, Callable[[TrackRef, object], TrackMeta]] = {
    "spotify": _spotify_metadata,
    "apple_music": _apple_metadata,
    "deezer": _deezer_metadata,
}


def fetch_metadata(ref: TrackRef, http=None) -> TrackMeta:
    """Read title/artist/duration/cover from the service that owns the link."""
    reader = _METADATA_READERS.get(ref.service)
    if reader is None:
        raise TrackError(
            "track_unsupported_service",
            f"{ref.service_label} is not supported.",
            service=ref.service_label,
        )
    return reader(ref, http if http is not None else Fetcher())


# ---------------------------------------------------------------------------
# 3. Search
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One downloadable recording that may be the track that was asked for."""

    url: str
    title: str
    duration: float | None
    provider: str

    def label(self, index: int) -> str:
        """``1 · 3:33`` — the button text, deliberately without a channel."""
        return format_track_label(index, self.duration)


def format_track_label(index: int, duration: object) -> str:
    """``1 · 3:33`` / ``2 · ?`` — the neutral button label."""
    seconds = duration_seconds(duration)
    return f"{index} · {format_clock(seconds)}" if seconds else f"{index} · ?"


def duration_seconds(value: object) -> int | None:
    """Coerce a duration to whole seconds (``None`` when it is unknown)."""
    try:
        seconds = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def format_clock(value: object) -> str:
    """``3:33`` / ``1:02:03``; an unknown duration is an empty string."""
    seconds = duration_seconds(value)
    if seconds is None:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def search_cmd(
    provider: str, query: str, count: int = SEARCH_COUNT, *, python: str | None = None
) -> list[str]:
    """The yt-dlp argv for one provider's search (``ytsearchN:`` / ``scsearchN:``).

    ``--flat-playlist`` is what makes this cheap: the search itself already
    returns the titles and durations, and extracting every hit fully would cost
    one network round trip per candidate.
    """
    prefix = SEARCH_PREFIXES.get(provider)
    if prefix is None:
        raise ValueError(f"Unknown search provider: {provider}")
    executable = python or sys.executable
    return [
        executable, "-m", "yt_dlp",
        "-J", "--flat-playlist", "--no-warnings",
        f"{prefix}{int(count)}:{query}",
    ]


def _tokens(text: object) -> set[str]:
    """Identifying words of a title, lower-cased and stripped of filler."""
    cleaned = _BRACKETED_RE.sub(" ", str(text or "").lower())
    return {
        word
        for word in _WORD_RE.findall(cleaned)
        if word not in _NOISE_WORDS and len(word) > 1 or word.isdigit()
    }


def title_match(reference: object, candidate: object) -> float:
    """Share of the reference title's words the candidate carries (0..1).

    Bracketed qualifiers are dropped first, so ``Never Gonna Give You Up
    (Official Video)`` and ``Never Gonna Give You Up (2022 Remaster)`` both
    match ``Never Gonna Give You Up``; a remix or a cover keeps its own words
    and scores lower.
    """
    wanted = _tokens(reference)
    if not wanted:
        return 0.0
    return len(wanted & _tokens(candidate)) / len(wanted)


def duration_matches(reference: object, candidate: object) -> bool:
    """Whether a candidate is within ``±15 %`` or ``±20 s`` of the reference."""
    target = duration_seconds(reference)
    if target is None:
        return True                      # nothing to compare against
    found = duration_seconds(candidate)
    if found is None:
        return False                     # a hit with no duration cannot be trusted
    tolerance = max(DURATION_TOLERANCE_SECONDS, target * DURATION_TOLERANCE_RATIO)
    return abs(found - target) <= tolerance


def _entry_url(entry: dict) -> str:
    """The page a search hit points at (never the internal API URL)."""
    for key in ("webpage_url", "original_url", "url"):
        value = str(entry.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _is_drm(entry: dict) -> bool:
    if entry.get("has_drm"):
        return True
    for fmt in entry.get("formats") or []:
        if isinstance(fmt, dict) and fmt.get("has_drm"):
            return True
    return False


def select_candidates(
    entries: Iterable[object], meta: TrackMeta, provider: str, limit: int = MAX_CANDIDATES
) -> tuple[list[Candidate], int]:
    """Keep the search hits that could be this track, best match first.

    Returns the candidates plus how many hits were dropped for DRM, so the
    caller can tell "nothing matched" apart from "everything matched was
    protected".
    """
    scored: list[tuple[float, float, int, Candidate]] = []
    drm_skipped = 0
    for position, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            continue
        if _is_drm(entry):
            drm_skipped += 1
            continue
        url = _entry_url(entry)
        if not url:
            continue
        title = str(entry.get("title") or "").strip()
        score = title_match(meta.title, title)
        if score < MIN_TITLE_MATCH:
            continue
        if not duration_matches(meta.duration, entry.get("duration")):
            continue
        delta = abs(
            (duration_seconds(entry.get("duration")) or 0)
            - (duration_seconds(meta.duration) or 0)
        )
        scored.append((
            -score,
            delta,
            position,
            Candidate(url=url, title=title, duration=entry.get("duration"), provider=provider),
        ))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored[:limit]], drm_skipped


SearchRunner = Callable[[str, str], object]


def search_entries(provider: str, query: str, runner: SearchRunner) -> list[dict]:
    """Ask one provider and return its flat-playlist entries (never raises)."""
    try:
        payload = runner(provider, query)
    except Exception:  # noqa: BLE001 - a provider that fails simply has no hits
        return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if entries is None and payload.get("title"):
        entries = [payload]          # a single hit may come back unwrapped
    return [entry for entry in (entries or []) if isinstance(entry, dict)]


def find_candidates(
    meta: TrackMeta,
    runner: SearchRunner,
    *,
    providers: Iterable[str] = ("youtube", "soundcloud"),
) -> tuple[list[Candidate], int]:
    """The alternatives for ``meta``: YouTube first, SoundCloud when it fails."""
    query = meta.query
    if not query:
        return [], 0
    drm_skipped = 0
    for provider in providers:
        entries = search_entries(provider, query, runner)
        if not entries:
            continue
        candidates, skipped = select_candidates(entries, meta, provider)
        drm_skipped += skipped
        if candidates:
            return candidates, drm_skipped
    return [], drm_skipped


# ---------------------------------------------------------------------------
# Configuration and the whole pipeline
# ---------------------------------------------------------------------------

def substitute_mode(service: str) -> str:
    """``auto`` (resolve the track) or ``off`` (say the site is unsupported).

    ``TRACK_SUBSTITUTE`` sets it for every service and ``SPOTIFY_SUBSTITUTE``
    (``APPLE_MUSIC_SUBSTITUTE``, ``DEEZER_SUBSTITUTE``) overrides it for one.
    Read at call time, so a test can flip it with an environment variable.
    """
    specific = os.environ.get(SERVICE_ENV.get(service, ""), "").strip().lower()
    general = os.environ.get("TRACK_SUBSTITUTE", "").strip().lower()
    return "off" if (specific or general) == "off" else "auto"


def _candidate_payload(candidate: Candidate, index: int) -> dict:
    """One candidate as the chat sees it: a label, a duration and a link."""
    return {
        "index": index,
        "label": candidate.label(index),
        "url": candidate.url,
        "duration": duration_seconds(candidate.duration),
    }


def build_payload(meta: TrackMeta, candidates: list[Candidate]) -> dict:
    """The ``track`` block of ``/api/info``: metadata plus the alternatives."""
    return {
        "service": meta.service,
        "service_label": meta.service_label,
        "title": meta.title,
        "artist": meta.artist,
        "duration": duration_seconds(meta.duration),
        "thumbnail": meta.thumbnail,
        "candidates": [
            _candidate_payload(candidate, index)
            for index, candidate in enumerate(candidates, start=1)
        ],
    }


def resolve_track(
    url: object,
    *,
    runner: SearchRunner,
    http=None,
) -> dict | None:
    """Metadata and alternatives for a streaming link, or ``None`` for any other.

    Returns the ``track`` block ``/api/info`` adds to its payload; raises
    :class:`TrackError` with the code the chat needs for every link that cannot
    be substituted.
    """
    ref = parse_track_url(url)
    if ref is None:
        return None
    if substitute_mode(ref.service) == "off":
        raise TrackError(
            "track_disabled",
            f"{ref.service_label} is not supported.",
            service=ref.service_label,
        )
    if ref.service in UNSUPPORTED_SERVICES:
        raise TrackError(
            "track_unsupported_service",
            f"{ref.service_label} needs an API key, so this service is not supported.",
            service=ref.service_label,
        )
    meta = fetch_metadata(ref, http)
    candidates, drm_skipped = find_candidates(meta, runner)
    if not candidates:
        if drm_skipped:
            raise TrackError(
                "track_drm_only",
                "Every match for this track is DRM protected.",
                service=meta.service_label,
            )
        raise TrackError(
            "track_not_found",
            "No matching track was found.",
            service=meta.service_label,
        )
    return build_payload(meta, candidates)


__all__ = [
    "BROWSER_UA",
    "Candidate",
    "DURATION_TOLERANCE_RATIO",
    "DURATION_TOLERANCE_SECONDS",
    "Fetcher",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_CANDIDATES",
    "MIN_TITLE_MATCH",
    "SERVICE_ENV",
    "SERVICE_LABELS",
    "SEARCH_COUNT",
    "SEARCH_PREFIXES",
    "TrackError",
    "TrackMeta",
    "TrackRef",
    "UNSUPPORTED_SERVICES",
    "build_payload",
    "duration_matches",
    "duration_seconds",
    "fetch_metadata",
    "find_candidates",
    "format_clock",
    "format_track_label",
    "parse_deezer_track",
    "parse_itunes_lookup",
    "parse_spotify_embed",
    "parse_spotify_oembed",
    "parse_track_url",
    "resolve_track",
    "search_cmd",
    "search_entries",
    "select_candidates",
    "substitute_mode",
    "title_match",
]
