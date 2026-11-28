"""The streaming-link substitution: recognition, metadata, search, payload.

Every rule that decides *what* the engine answers for a Spotify (or Apple Music,
or Deezer) link lives in ``koyura/streaming.py``, which is pure apart from two
injected collaborators: the HTTP fetcher and the yt-dlp search runner. Both are
stubbed here, so nothing in this module touches the network or yt-dlp, and the
cases below are the ones a real run showed (a Spotify oEmbed body, an embed page
with the artist in a JSON blob, a YouTube search whose first hit is the official
upload and whose fourth is a "Lyrics" copy).

The JSON shape of the candidates is a contract with the bot (``track_card``), so
it is asserted here rather than left to the chat's tests.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import streaming  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: real shapes, captured once
# ---------------------------------------------------------------------------

#: ``GET https://open.spotify.com/oembed?url=…`` for a real track.
OEMBED = {
    "html": "<iframe …></iframe>",
    "title": "Never Gonna Give You Up",
    "thumbnail_url": "https://image-cdn-ak.spotifycdn.com/image/ab67616d00001e02baf89eb11ec7c657805d2da0",
    "thumbnail_width": 300,
    "thumbnail_height": 300,
    "provider_name": "Spotify",
    "type": "rich",
}

#: The interesting part of ``GET https://open.spotify.com/embed/track/<id>``:
#: the artist lives in a JSON blob, the duration in milliseconds.
EMBED_HTML = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"state":{"data":{"entity":{'
    '"name":"Never Gonna Give You Up","duration":213573,'
    '"artists":[{"name":"Rick Astley","uri":"spotify:artist:0gxyHStUsqpMadRV0Di1Qt"}]'
    '}}}}}}</script>'
)

#: ``GET https://itunes.apple.com/lookup?id=<id>`` for a real song.
ITUNES = {
    "resultCount": 1,
    "results": [
        {
            "wrapperType": "track",
            "kind": "song",
            "trackId": 1624945512,
            "artistName": "Rick Astley",
            "trackName": "Never Gonna Give You Up (2022 Remaster)",
            "trackTimeMillis": 213573,
            "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/x/100x100bb.jpg",
        }
    ],
}

#: ``GET https://api.deezer.com/track/<id>`` for a real track.
DEEZER = {
    "id": 3135556,
    "title": "Harder, Better, Faster, Stronger",
    "duration": 224,
    "artist": {"id": 27, "name": "Daft Punk"},
    "album": {"id": 302127, "title": "Discovery", "cover_xl": "https://cdn/cover.jpg"},
}

TRACK_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"


class FakeHTTP:
    """A fetcher that answers from a table and records what was asked."""

    def __init__(self, json_bodies=None, texts=None):
        self.json_bodies = json_bodies or {}
        self.texts = texts or {}
        self.asked: list[str] = []

    def _lookup(self, table, url):
        self.asked.append(url)
        for needle, value in table.items():
            if needle in url:
                return value
        raise OSError(f"no answer for {url}")

    def json(self, url):
        return self._lookup(self.json_bodies, url)

    def text(self, url):
        return self._lookup(self.texts, url)


class FakeRunner:
    """A search runner that records the providers it was asked for."""

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, provider: str, query: str):
        self.calls.append((provider, query))
        payload = self.answers.get(provider)
        if payload is None:
            raise OSError(f"{provider} is down")
        return payload


def fake_runner(answers: dict[str, dict]) -> FakeRunner:
    """A search runner: ``{"youtube": payload, "soundcloud": payload}``."""
    return FakeRunner(answers)


def ref_of(url: str) -> streaming.TrackRef:
    """The ``TrackRef`` of a link that must parse (keeps the tests readable)."""
    ref = streaming.parse_track_url(url)
    assert ref is not None
    return ref


def entry(title: str, duration: object, url: str = "", **extra) -> dict:
    payload = {"title": title, "duration": duration, "webpage_url": url or "https://youtu.be/x"}
    payload.update(extra)
    return payload


def ytsearch(*entries: dict) -> dict:
    return {"_type": "playlist", "entries": list(entries)}


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    """Every test starts in the default mode; one flips it to ``off``."""
    for name in ("TRACK_SUBSTITUTE", "SPOTIFY_SUBSTITUTE", "APPLE_MUSIC_SUBSTITUTE",
                 "DEEZER_SUBSTITUTE"):
        monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------------------
# 1. Recognition
# ---------------------------------------------------------------------------

class TestRecognition:
    def test_a_spotify_track_link_names_the_track(self):
        ref = streaming.parse_track_url(TRACK_URL)
        assert ref is not None
        assert ref.service == "spotify"
        assert ref.track_id == "4cOdK2wGLETKBW3PvgPWqT"

    def test_a_share_link_with_a_query_and_market_prefix_still_matches(self):
        ref = streaming.parse_track_url(
            "https://open.spotify.com/intl-ru/track/abc123?si=deadbeef&context=x"
        )
        assert ref is not None and ref.track_id == "abc123"

    def test_a_spotify_uri_is_recognised(self):
        ref = streaming.parse_track_url("spotify:track:abc123")
        assert ref is not None and ref.service == "spotify"
        assert ref.track_id == "abc123"

    def test_an_embed_link_is_recognised(self):
        ref = streaming.parse_track_url("https://open.spotify.com/embed/track/abc123")
        assert ref is not None and ref.track_id == "abc123"

    def test_an_album_is_refused_with_its_own_code(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.parse_track_url("https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX")
        assert caught.value.code == "track_unsupported_type"
        assert "album" in caught.value.message

    def test_a_playlist_is_refused_with_its_own_code(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.parse_track_url("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        assert caught.value.code == "track_unsupported_type"
        assert "playlist" in caught.value.message

    def test_an_artist_page_is_refused(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.parse_track_url("https://open.spotify.com/artist/0gxyHStUsqpMadRV0Di1Qt")
        assert caught.value.code == "track_unsupported_type"

    def test_a_bare_spotify_uri_is_refused(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.parse_track_url("spotify:album:abc")
        assert caught.value.code == "track_unsupported_type"

    def test_an_apple_music_song_in_an_album_link_names_the_song(self):
        ref = streaming.parse_track_url(
            "https://music.apple.com/us/album/never-gonna-give-you-up/1624945511?i=1624945512"
        )
        assert ref is not None and ref.service == "apple_music"
        assert ref.track_id == "1624945512"

    def test_an_apple_music_album_without_a_song_id_is_refused(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.parse_track_url("https://music.apple.com/us/album/never/1624945511")
        assert caught.value.code == "track_unsupported_type"

    def test_a_deezer_track_is_recognised(self):
        ref = streaming.parse_track_url("https://www.deezer.com/us/track/3135556")
        assert ref is not None and ref.service == "deezer"
        assert ref.track_id == "3135556"

    def test_a_deezer_album_is_refused(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.parse_track_url("https://www.deezer.com/album/302127")
        assert caught.value.code == "track_unsupported_type"

    @pytest.mark.parametrize(
        "url",
        [
            "https://tidal.com/browse/track/12345",
            "https://music.yandex.ru/album/1/track/2",
        ],
    )
    def test_a_service_that_needs_a_key_says_so(self, url):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.parse_track_url(url)
        assert caught.value.code == "track_unsupported_service"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://soundcloud.com/rick-astley-official/never-gonna-give-you-up",
            "https://www.tiktok.com/@user/video/1",
            "not a url at all",
            "",
        ],
    )
    def test_an_ordinary_link_is_not_a_track(self, url):
        assert streaming.parse_track_url(url) is None


# ---------------------------------------------------------------------------
# 2. Metadata
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_oembed_gives_the_title_and_the_cover(self):
        assert streaming.parse_spotify_oembed(OEMBED) == (
            "Never Gonna Give You Up",
            OEMBED["thumbnail_url"],
        )

    def test_oembed_without_a_title_is_an_empty_pair(self):
        assert streaming.parse_spotify_oembed({}) == ("", "")
        assert streaming.parse_spotify_oembed(None) == ("", "")

    def test_the_embed_page_gives_the_artist_and_the_duration(self):
        artist, duration = streaming.parse_spotify_embed(EMBED_HTML)
        assert artist == "Rick Astley"
        assert duration == pytest.approx(213.6, abs=0.1)

    def test_an_embed_page_with_nothing_useful_gives_empty_values(self):
        assert streaming.parse_spotify_embed("<html></html>") == ("", None)
        assert streaming.parse_spotify_embed("") == ("", None)

    def test_an_escaped_artist_name_is_unescaped(self):
        html = '{"artists": [{"name": "Simon \\u0026 Garfunkel"}]}'
        assert streaming.parse_spotify_embed(html)[0] == "Simon & Garfunkel"

    def test_itunes_lookup_gives_the_song(self):
        meta = streaming.parse_itunes_lookup(ITUNES)
        assert meta is not None
        assert meta.title == "Never Gonna Give You Up (2022 Remaster)"
        assert meta.artist == "Rick Astley"
        assert meta.duration == pytest.approx(213.6, abs=0.1)
        # Apple's 100×100 artwork is up-scaled to something a card can show.
        assert "600x600bb" in meta.thumbnail

    def test_an_empty_itunes_answer_is_none(self):
        assert streaming.parse_itunes_lookup({"resultCount": 0, "results": []}) is None
        assert streaming.parse_itunes_lookup({}) is None

    def test_deezer_gives_the_track_and_the_cover(self):
        meta = streaming.parse_deezer_track(DEEZER)
        assert meta is not None
        assert meta.title == "Harder, Better, Faster, Stronger"
        assert meta.artist == "Daft Punk"
        assert meta.duration == 224
        assert meta.thumbnail == "https://cdn/cover.jpg"

    def test_a_deezer_error_body_is_none(self):
        assert streaming.parse_deezer_track({"error": {"type": "DataException"}}) is None

    def test_spotify_metadata_combines_both_endpoints(self):
        http = FakeHTTP(
            json_bodies={"oembed": OEMBED},
            texts={"embed/track": EMBED_HTML},
        )
        meta = streaming.fetch_metadata(
            ref_of(TRACK_URL), http=http
        )
        assert meta.title == "Never Gonna Give You Up"
        assert meta.artist == "Rick Astley"
        assert meta.duration == pytest.approx(213.6, abs=0.1)
        assert meta.thumbnail == OEMBED["thumbnail_url"]
        assert meta.query == "Rick Astley Never Gonna Give You Up"

    def test_spotify_without_an_artist_searches_by_title(self):
        http = FakeHTTP(json_bodies={"oembed": OEMBED}, texts={})
        meta = streaming.fetch_metadata(
            ref_of(TRACK_URL), http=http
        )
        assert meta.artist == ""
        assert meta.query == "Never Gonna Give You Up"

    def test_spotify_without_a_title_is_a_readable_failure(self):
        http = FakeHTTP(json_bodies={"oembed": {}}, texts={})
        with pytest.raises(streaming.TrackError) as caught:
            streaming.fetch_metadata(ref_of(TRACK_URL), http=http)
        assert caught.value.code == "track_metadata_failed"

    def test_apple_music_metadata_comes_from_the_lookup(self):
        ref = ref_of("https://music.apple.com/song/1624945512")
        meta = streaming.fetch_metadata(ref, http=FakeHTTP(json_bodies={"lookup": ITUNES}))
        assert meta.artist == "Rick Astley"
        assert "Never Gonna Give You Up" in meta.title

    def test_an_unknown_apple_id_is_not_found(self):
        ref = ref_of("https://music.apple.com/song/1")
        with pytest.raises(streaming.TrackError) as caught:
            streaming.fetch_metadata(
                ref, http=FakeHTTP(json_bodies={"lookup": {"results": []}})
            )
        assert caught.value.code == "track_not_found"

    def test_deezer_metadata_comes_from_the_api(self):
        ref = ref_of("https://www.deezer.com/track/3135556")
        meta = streaming.fetch_metadata(ref, http=FakeHTTP(json_bodies={"track/": DEEZER}))
        assert meta.artist == "Daft Punk"
        assert meta.duration == 224


# ---------------------------------------------------------------------------
# 3. The search command
# ---------------------------------------------------------------------------

class TestSearchCommand:
    def test_youtube_search_is_a_flat_playlist_query(self):
        cmd = streaming.search_cmd("youtube", "Rick Astley Never Gonna Give You Up", python="py")
        assert cmd[:2] == ["py", "-m"]
        assert "--flat-playlist" in cmd
        assert cmd[-1] == "ytsearch10:Rick Astley Never Gonna Give You Up"

    def test_soundcloud_uses_scsearch(self):
        cmd = streaming.search_cmd("soundcloud", "a b", count=3)
        assert cmd[-1] == "scsearch3:a b"

    def test_an_unknown_provider_is_a_programming_error(self):
        with pytest.raises(ValueError):
            streaming.search_cmd("vimeo", "x")


class TestPlayerClient:
    """The YouTube client list that keeps "not available on this app" away.

    YouTube serves some videos to none of the clients yt-dlp tries first and
    answers *"The following content is not available on this app"* — measured on
    two of the three IDs that a Spotify search turned up. The ``android`` client
    does not get that refusal, but on its own it only exposes the muxed 360p
    stream, so pinning it alone would cost the video picker its resolutions.
    A *list* is therefore the default: yt-dlp merges the clients' format lists.
    """

    def test_the_default_is_a_list_rather_than_one_client(self):
        assert streaming.player_client() == "default,android"

    def test_the_default_argument_is_the_documented_one(self):
        assert streaming.DEFAULT_YOUTUBE_PLAYER_CLIENT == "default,android"

    def test_the_environment_overrides_it(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_PLAYER_CLIENT", "android")
        assert streaming.player_client() == "android"
        assert streaming.extractor_args() == [
            "--extractor-args", "youtube:player_client=android"
        ]

    def test_an_empty_value_keeps_the_default(self, monkeypatch):
        """An unset or blank variable must not silently restore the failure."""
        monkeypatch.setenv("YOUTUBE_PLAYER_CLIENT", "   ")
        assert streaming.player_client() == "default,android"

    def test_default_is_how_an_operator_opts_out(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_PLAYER_CLIENT", "default")
        assert streaming.extractor_args() == [
            "--extractor-args", "youtube:player_client=default"
        ]

    def test_the_search_passes_it_as_its_own_argv_pair(self):
        cmd = streaming.search_cmd("youtube", "a b", python="py")
        assert cmd[cmd.index("--extractor-args") + 1] == (
            "youtube:player_client=default,android"
        )
        # The query stays last, so yt-dlp cannot read it as the flag's value.
        assert cmd[-1] == "ytsearch10:a b"

    def test_a_non_youtube_provider_gets_no_youtube_flag(self):
        cmd = streaming.search_cmd("soundcloud", "a b", python="py")
        assert "--extractor-args" not in cmd

    def test_an_explicit_client_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("YOUTUBE_PLAYER_CLIENT", "ios")
        assert streaming.extractor_args("android") == [
            "--extractor-args", "youtube:player_client=android"
        ]

    def test_no_client_means_no_flag(self):
        assert streaming.extractor_args("") == []


# ---------------------------------------------------------------------------
# 4. Filtering the hits
# ---------------------------------------------------------------------------

class TestFiltering:
    meta = streaming.TrackMeta(
        service="spotify", track_id="x", title="Never Gonna Give You Up",
        artist="Rick Astley", duration=213.0,
    )

    def test_a_matching_hit_is_kept(self):
        found, drm = streaming.select_candidates(
            [entry("Rick Astley - Never Gonna Give You Up (Official Video)", 213)],
            self.meta,
            "youtube",
        )
        assert len(found) == 1 and drm == 0
        assert found[0].url.startswith("https://")
        assert found[0].provider == "youtube"

    def test_a_different_song_is_dropped(self):
        found, _ = streaming.select_candidates(
            [entry("Daft Punk - Get Lucky", 249)], self.meta, "youtube"
        )
        assert found == []

    def test_a_much_longer_recording_is_dropped(self):
        # Same words, but a 6-minute extended mix is not the same recording.
        found, _ = streaming.select_candidates(
            [entry("Rick Astley - Never Gonna Give You Up (Extended Mix)", 400)],
            self.meta,
            "youtube",
        )
        assert found == []

    def test_the_tolerance_is_fifteen_percent_or_twenty_seconds(self):
        assert streaming.duration_matches(213, 213 + 20)
        assert streaming.duration_matches(213, 213 - 20)
        assert not streaming.duration_matches(213, 213 + 60)
        assert streaming.duration_matches(100, 115)      # 15 %
        assert not streaming.duration_matches(100, 130)

    def test_a_hit_without_a_duration_is_kept_but_ranked_last(self):
        """YouTube's search intermittently omits every duration.

        Dropping those hits made the whole provider look empty and the caller
        fall through to SoundCloud, where a longer cover matched the title — the
        user silently got a different recording. They are offered instead, after
        any hit whose length is known and confirmed.
        """
        found, _ = streaming.select_candidates(
            [
                entry("Rick Astley - Never Gonna Give You Up", None),
                entry("Rick Astley - Never Gonna Give You Up (Official)", 213),
            ],
            self.meta,
            "youtube",
        )
        assert len(found) == 2
        assert found[0].duration == 213, "a measured duration must win"
        assert found[1].duration is None

    def test_an_all_durationless_answer_still_offers_the_track(self):
        """The measured failure: ten hits, none with a duration, no candidates."""
        hits = [entry(f"Rick Astley - Never Gonna Give You Up ({index})", None)
                for index in range(10)]
        found, _ = streaming.select_candidates(hits, self.meta, "youtube")
        assert len(found) == streaming.MAX_CANDIDATES

    def test_a_wrong_duration_is_still_dropped(self):
        """An unknown length is forgivable; a known, wrong one is not."""
        found, _ = streaming.select_candidates(
            [entry("Rick Astley - Never Gonna Give You Up (Extended Mix)", 400)],
            self.meta,
            "youtube",
        )
        assert found == []

    def test_a_drm_hit_is_skipped_and_counted(self):
        found, drm = streaming.select_candidates(
            [
                entry("Rick Astley - Never Gonna Give You Up", 213, has_drm=True),
                entry("Rick Astley - Never Gonna Give You Up (Audio)", 213),
            ],
            self.meta,
            "soundcloud",
        )
        assert drm == 1
        assert len(found) == 1
        assert found[0].title.endswith("(Audio)")

    def test_a_drm_flag_on_one_format_also_skips_the_hit(self):
        payload = entry("Rick Astley - Never Gonna Give You Up", 213)
        payload["formats"] = [{"format_id": "1", "has_drm": True}]
        found, drm = streaming.select_candidates([payload], self.meta, "soundcloud")
        assert (found, drm) == ([], 1)

    def test_the_official_upload_comes_first(self):
        found, _ = streaming.select_candidates(
            [
                entry("Rick Astley - Never Gonna Give You Up (Lyrics)", 214),
                entry("Rick Astley - Never Gonna Give You Up", 213),
            ],
            self.meta,
            "youtube",
        )
        # Same title match, but the exact duration wins on the tie-break.
        assert found[0].title.endswith("Give You Up")

    def test_at_most_eight_alternatives_are_kept(self):
        hits = [
            entry(f"Rick Astley - Never Gonna Give You Up ({index})", 213)
            for index in range(20)
        ]
        found, _ = streaming.select_candidates(hits, self.meta, "youtube")
        assert len(found) == streaming.MAX_CANDIDATES == 8

    def test_the_label_is_a_number_and_a_clock(self):
        found, _ = streaming.select_candidates(
            [entry("Rick Astley - Never Gonna Give You Up", 213)], self.meta, "youtube"
        )
        assert found[0].label(1) == "1 · 3:33"

    def test_a_title_match_ignores_bracketed_qualifiers(self):
        assert streaming.title_match("Never Gonna Give You Up",
                                     "Rick Astley - Never Gonna Give You Up (Official Video)") > 0.9


# ---------------------------------------------------------------------------
# 5. Provider fallback
# ---------------------------------------------------------------------------

class TestFallback:
    meta = streaming.TrackMeta(
        service="spotify", track_id="x", title="Never Gonna Give You Up",
        artist="Rick Astley", duration=213.0,
    )

    def test_youtube_is_asked_first(self):
        runner = fake_runner({"youtube": ytsearch(
            entry("Rick Astley - Never Gonna Give You Up", 213))})
        streaming.find_candidates(self.meta, runner)
        assert runner.calls == [("youtube", "Rick Astley Never Gonna Give You Up")]

    def test_soundcloud_is_asked_when_youtube_has_nothing(self):
        runner = fake_runner({
            "youtube": ytsearch(),
            "soundcloud": ytsearch(entry("Rick Astley - Never Gonna Give You Up", 213)),
        })
        found, _ = streaming.find_candidates(self.meta, runner)
        assert [provider for provider, _ in runner.calls] == ["youtube", "soundcloud"]
        assert found[0].provider == "soundcloud"

    def test_soundcloud_is_asked_when_youtube_fails(self):
        runner = fake_runner({"soundcloud": ytsearch(
            entry("Rick Astley - Never Gonna Give You Up", 213))})
        found, _ = streaming.find_candidates(self.meta, runner)
        assert len(found) == 1

    def test_a_provider_with_only_drm_results_falls_through(self):
        runner = fake_runner({
            "youtube": ytsearch(entry("Rick Astley - Never Gonna Give You Up", 213,
                                     has_drm=True)),
            "soundcloud": ytsearch(entry("Rick Astley - Never Gonna Give You Up", 213)),
        })
        found, drm = streaming.find_candidates(self.meta, runner)
        assert drm == 1
        assert found[0].provider == "soundcloud"

    def test_no_hits_at_all_is_empty(self):
        runner = fake_runner({})
        assert streaming.find_candidates(self.meta, runner) == ([], 0)


# ---------------------------------------------------------------------------
# 6. The whole pipeline
# ---------------------------------------------------------------------------

class TestResolveTrack:
    def _runner(self):
        return fake_runner({"youtube": ytsearch(
            entry("Rick Astley - Never Gonna Give You Up", 213,
                  webpage_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            entry("Rick Astley - Never Gonna Give You Up (Audio)", 214,
                  webpage_url="https://www.youtube.com/watch?v=7FwDP17XPlk"),
        )})

    def _http(self):
        return FakeHTTP(json_bodies={"oembed": OEMBED}, texts={"embed/track": EMBED_HTML})

    def test_a_track_link_answers_with_metadata_and_alternatives(self):
        block = streaming.resolve_track(TRACK_URL, runner=self._runner(), http=self._http())
        assert block is not None
        assert block["service"] == "spotify"
        assert block["service_label"] == "Spotify"
        assert block["artist"] == "Rick Astley"
        assert block["title"] == "Never Gonna Give You Up"
        # 213573 ms from the embed page, rounded to whole seconds.
        assert block["duration"] == 214
        assert block["thumbnail"] == OEMBED["thumbnail_url"]
        # The embed page says 213.573 s, so the 214 s upload is the closer
        # match and comes first; the 213 s one is second. Both stay offered.
        assert [c["label"] for c in block["candidates"]] == ["1 · 3:34", "2 · 3:33"]
        assert block["candidates"][0]["url"] == "https://www.youtube.com/watch?v=7FwDP17XPlk"

    def test_an_ordinary_link_is_not_resolved(self):
        assert streaming.resolve_track(
            "https://www.youtube.com/watch?v=x", runner=self._runner(), http=self._http()
        ) is None

    def test_no_candidates_is_track_not_found(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.resolve_track(
                TRACK_URL, runner=fake_runner({}), http=self._http()
            )
        assert caught.value.code == "track_not_found"

    def test_only_drm_candidates_is_track_drm_only(self):
        runner = fake_runner({
            "youtube": ytsearch(entry("Rick Astley - Never Gonna Give You Up", 213,
                                     has_drm=True)),
            "soundcloud": ytsearch(entry("Rick Astley - Never Gonna Give You Up", 213,
                                        has_drm=True)),
        })
        with pytest.raises(streaming.TrackError) as caught:
            streaming.resolve_track(TRACK_URL, runner=runner, http=self._http())
        assert caught.value.code == "track_drm_only"

    def test_mode_off_refuses_the_link_before_any_search(self):
        os.environ["SPOTIFY_SUBSTITUTE"] = "off"
        runner = fake_runner({})
        with pytest.raises(streaming.TrackError) as caught:
            streaming.resolve_track(TRACK_URL, runner=runner, http=self._http())
        assert caught.value.code == "track_disabled"
        assert runner.calls == []

    def test_the_general_switch_turns_off_every_service(self):
        os.environ["TRACK_SUBSTITUTE"] = "off"
        assert streaming.substitute_mode("spotify") == "off"
        assert streaming.substitute_mode("deezer") == "off"

    def test_a_service_switch_overrides_the_general_one(self, monkeypatch):
        monkeypatch.setenv("TRACK_SUBSTITUTE", "off")
        monkeypatch.setenv("DEEZER_SUBSTITUTE", "auto")
        assert streaming.substitute_mode("deezer") == "auto"
        assert streaming.substitute_mode("spotify") == "off"

    def test_the_default_is_auto(self):
        assert streaming.substitute_mode("spotify") == "auto"

    def test_every_error_carries_a_code_and_a_human_message(self):
        with pytest.raises(streaming.TrackError) as caught:
            streaming.resolve_track(TRACK_URL, runner=fake_runner({}), http=self._http())
        payload = caught.value.payload()
        assert payload["code"] == "track_not_found"
        assert payload["error"] == caught.value.message
        # Never a raw yt-dlp line: no "ERROR:", no "[DRM]", no "yt-dlp".
        assert "ERROR" not in payload["error"]
        assert "yt-dlp" not in payload["error"]
        assert "DRM" not in payload["error"]


class TestTheDurationlessSearchAnswer:
    """The regression that made a substituted track the wrong recording.

    YouTube's flat search sometimes answers with ten hits and no duration on any
    of them (observed once in six runs). Every hit was then rejected, the
    provider looked empty, and the code fell through to SoundCloud — where a
    198 s cover matched the title and won because it was the only candidate.
    """

    meta = streaming.TrackMeta(
        service="spotify", track_id="x", title="Never Gonna Give You Up",
        artist="Rick Astley", duration=214.0,
    )

    def _durationless_youtube(self, count: int = 3) -> dict:
        return {"entries": [
            {"title": f"Rick Astley - Never Gonna Give You Up ({index})",
             "duration": None,
             "url": f"https://www.youtube.com/watch?v=id{index}"}
            for index in range(count)
        ]}

    def test_youtube_without_durations_does_not_fall_back_to_soundcloud(self):
        runner = fake_runner({
            "youtube": self._durationless_youtube(),
            "soundcloud": {"entries": [
                {"title": "Branime Studios - Never Gonna Give You Up (Rick Astley Cover)",
                 "duration": 198.048,
                 "webpage_url": "https://soundcloud.com/x/cover"},
            ]},
        })
        found, _ = streaming.find_candidates(self.meta, runner)
        providers = [provider for provider, _ in runner.calls]
        # YouTube is asked, then asked once more for the durations, and never
        # replaced by SoundCloud: the cover is not the track that was asked for.
        assert providers == ["youtube", "youtube"]
        assert all(candidate.provider == "youtube" for candidate in found)

    def test_one_retry_is_enough_to_recover_the_durations(self):
        calls = []

        def runner(provider, query):
            calls.append(provider)
            if len(calls) == 1:
                return self._durationless_youtube()
            return {"entries": [
                {"title": "Rick Astley - Never Gonna Give You Up", "duration": 214,
                 "webpage_url": "https://www.youtube.com/watch?v=ok"},
            ]}

        found, _ = streaming.find_candidates(self.meta, runner)
        assert calls == ["youtube", "youtube"]
        assert found[0].label(1) == "1 · 3:34"

    def test_no_retry_when_the_durations_are_there(self):
        calls = []

        def runner(provider, query):
            calls.append(provider)
            return {"entries": [
                {"title": "Rick Astley - Never Gonna Give You Up", "duration": 214,
                 "webpage_url": "https://www.youtube.com/watch?v=ok"},
            ]}

        streaming.find_candidates(self.meta, runner)
        assert calls == ["youtube"], "a usable answer must not be asked twice"

    def test_a_complete_youtube_answer_is_still_preferred(self):
        runner = fake_runner({"youtube": {"entries": [
            {"title": "Rick Astley - Never Gonna Give You Up", "duration": 214,
             "webpage_url": "https://www.youtube.com/watch?v=good"},
            {"title": "Rick Astley - Never Gonna Give You Up (HQ)", "duration": None,
             "webpage_url": "https://www.youtube.com/watch?v=unknown"},
        ]}})
        found, _ = streaming.find_candidates(self.meta, runner)
        assert found[0].url == "https://www.youtube.com/watch?v=good"
        assert found[0].duration == 214

    def test_the_ranked_order_is_measured_first(self):
        found, _ = streaming.select_candidates(
            [
                {"title": "Never Gonna Give You Up", "duration": None,
                 "webpage_url": "https://youtu.be/a"},
                {"title": "Never Gonna Give You Up", "duration": 214,
                 "webpage_url": "https://youtu.be/b"},
                {"title": "Never Gonna Give You Up", "duration": 214,
                 "webpage_url": "https://youtu.be/c"},
            ],
            self.meta,
            "youtube",
        )
        assert [c.url for c in found] == ["https://youtu.be/b", "https://youtu.be/c",
                                          "https://youtu.be/a"]
