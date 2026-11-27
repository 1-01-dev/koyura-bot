"""``/api/info`` (and ``/api/download``) for a streaming-service track.

``streaming`` is unit-tested on its own; this module pins the HTTP contract the
bot depends on: the status codes, the ``code`` field of every refusal, the shape
of the ``track`` block, and the fact that a download job is pointed at the
candidate the chat picked rather than at the un-downloadable link.

yt-dlp, the search runner and the metadata endpoints are all stubbed: nothing
here touches the network or spawns a process, and the assertions are about the
JSON, not about yt-dlp's behaviour.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ.setdefault("DOWNLOADS_PATH", tempfile.mkdtemp())
os.environ.setdefault("MAX_CONCURRENT_DOWNLOADS", "3")
# Never let a test reach a real panel: the egress rule and the cookie registry
# would both dial it. A closed port answers "direct" immediately.
os.environ.setdefault("KOYURA_DASHBOARD_URL", "http://127.0.0.1:9")

import pytest  # noqa: E402

import app as koyura_app  # noqa: E402
import streaming  # noqa: E402

HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}

TRACK_URL = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"

TRACK_BLOCK = {
    "service": "spotify",
    "service_label": "Spotify",
    "title": "Never Gonna Give You Up",
    "artist": "Rick Astley",
    "duration": 214,
    "thumbnail": "https://image-cdn-ak.spotifycdn.com/image/ab67616d00001e02baf8",
    "candidates": [
        {"index": 1, "label": "1 · 3:34", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
         "duration": 214},
        {"index": 2, "label": "2 · 3:48", "url": "https://www.youtube.com/watch?v=6PLatPMoxGw",
         "duration": 228},
    ],
}

#: One real YouTube extraction, trimmed to what the payload uses. The title and
#: the channel are deliberately *not* the track's: the substitution must win.
YTDLP_INFO = {
    "id": "dQw4w9WgXcQ",
    "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
    "uploader": "Rick Astley",
    "uploader_id": "@RickAstleyYT",
    "channel": "Rick Astley",
    "channel_follower_count": 4200000,
    "view_count": 1500000000,
    "like_count": 17000000,
    "duration": 213,
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    "extractor": "youtube",
    "categories": ["Music"],
    "upload_date": "20091025",
    "formats": [
        {
            "format_id": "140", "ext": "m4a", "vcodec": "none",
            "acodec": "mp4a.40.2", "abr": 129, "filesize": 3400000,
        },
        {
            "format_id": "137", "ext": "mp4", "vcodec": "avc1.640028",
            "acodec": "none", "height": 1080, "filesize": 61000000,
        },
    ],
}

TRACK_JSON = json.dumps(TRACK_BLOCK)
YTDLP_JSON = json.dumps(YTDLP_INFO)


@pytest.fixture(autouse=True)
def client():
    """A test client per test, with the track cache emptied between them."""
    koyura_app._track_cache.clear()
    for name in ("TRACK_SUBSTITUTE", "SPOTIFY_SUBSTITUTE"):
        os.environ.pop(name, None)
    with koyura_app.app.test_client() as test_client:
        yield test_client


def post_info(client, url: str = TRACK_URL):
    return client.post("/api/info", json={"url": url}, headers=HEADERS)


class TestTrackInfo:
    def test_a_track_link_answers_with_the_track_block(self, client):
        with patch.object(koyura_app, "_track_payload", return_value=TRACK_BLOCK), \
             patch.object(koyura_app, "_run_capture", return_value=(0, YTDLP_JSON, "", False)):
            response = post_info(client)
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["track"] == TRACK_BLOCK
        assert payload["track"]["candidates"][0]["label"] == "1 · 3:34"

    def test_the_metadata_is_the_services_not_the_files(self, client):
        with patch.object(koyura_app, "_track_payload", return_value=TRACK_BLOCK), \
             patch.object(koyura_app, "_run_capture", return_value=(0, YTDLP_JSON, "", False)):
            payload = post_info(client).get_json()
        assert payload["title"] == "Never Gonna Give You Up"
        # The artist, in both fields the bot reads, and never the uploader id.
        assert payload["uploader"] == "Rick Astley"
        assert payload["channel"] == "Rick Astley"
        assert payload["uploader_id"] == ""
        # The service's cover, not the video's frame.
        assert payload["thumbnail"] == TRACK_BLOCK["thumbnail"]
        assert payload["duration"] == 213       # the file's own duration

    def test_the_engagement_numbers_are_dropped_not_relabelled(self, client):
        with patch.object(koyura_app, "_track_payload", return_value=TRACK_BLOCK), \
             patch.object(koyura_app, "_run_capture", return_value=(0, YTDLP_JSON, "", False)):
            payload = post_info(client).get_json()
        for key in ("view_count", "like_count", "channel_follower_count", "comment_count"):
            assert payload[key] is None, key
        assert payload["categories"] == []
        assert payload["upload_date"] is None

    def test_the_source_site_is_not_reported(self, client):
        """The candidate URLs must be downloadable, so the *site name* is not
        what the payload may never mention: it is the extractor, the title and
        the channel, the three fields a card or a tag is built from."""
        with patch.object(koyura_app, "_track_payload", return_value=TRACK_BLOCK), \
             patch.object(koyura_app, "_run_capture", return_value=(0, YTDLP_JSON, "", False)):
            payload = post_info(client).get_json()
        assert payload["extractor"] == ""
        for key in ("title", "uploader", "channel", "uploader_id"):
            assert "youtube" not in str(payload[key]).lower(), key

    def test_the_formats_are_still_the_files_own(self, client):
        with patch.object(koyura_app, "_track_payload", return_value=TRACK_BLOCK), \
             patch.object(koyura_app, "_run_capture", return_value=(0, YTDLP_JSON, "", False)):
            payload = post_info(client).get_json()
        assert [fmt["label"] for fmt in payload["formats"]] == ["1080p"]
        assert payload["audio"]["abr"] == 129

    def test_an_ordinary_link_has_no_track_block(self, client):
        info = dict(YTDLP_INFO)
        with patch.object(koyura_app, "_run_capture", return_value=(0, json.dumps(info), "", False)):
            payload = post_info(
                client, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            ).get_json()
        assert payload["track"] is None
        assert payload["title"].startswith("Rick Astley - Never Gonna Give You Up")
        assert payload["view_count"] == 1500000000


class TestTrackRefusals:
    """Every refusal is a 400 with a ``code`` the chat turns into a sentence."""

    @pytest.mark.parametrize(
        "code",
        [
            "track_not_found",
            "track_unsupported_type",
            "track_drm_only",
            "track_disabled",
            "track_metadata_failed",
            "track_unsupported_service",
        ],
    )
    def test_each_code_is_forwarded_with_its_message(self, client, code):
        error = streaming.TrackError(code, "A readable sentence.", service="Spotify")
        with patch.object(koyura_app, "_track_payload", side_effect=error):
            response = post_info(client)
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["code"] == code
        assert payload["service"] == "Spotify"
        assert payload["error"] == "A readable sentence."

    def test_an_album_is_refused_before_any_search(self, client):
        with patch.object(koyura_app, "_run_search") as search:
            response = post_info(client, "https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX")
        assert response.status_code == 400
        assert response.get_json()["code"] == "track_unsupported_type"
        search.assert_not_called()

    def test_mode_off_answers_track_disabled(self, client, monkeypatch):
        monkeypatch.setenv("SPOTIFY_SUBSTITUTE", "off")
        response = post_info(client)
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["code"] == "track_disabled"
        # The historical wording: "not supported", never a DRM lecture.
        assert "not supported" in payload["error"].lower()

    def test_a_provider_that_returns_nothing_is_track_not_found(self, client):
        with patch.object(koyura_app, "_track_payload",
                          side_effect=streaming.TrackError("track_not_found", "nothing")):
            response = post_info(client)
        assert response.get_json()["code"] == "track_not_found"


class TestTrackCache:
    def test_one_link_resolves_once(self, client):
        with patch.object(koyura_app, "streaming") as module:
            module.parse_track_url.return_value = streaming.TrackRef(
                "spotify", "x", TRACK_URL
            )
            module.resolve_track.return_value = TRACK_BLOCK
            with patch.object(koyura_app, "_run_capture",
                              return_value=(0, YTDLP_JSON, "", False)):
                post_info(client)
                post_info(client)
        assert module.resolve_track.call_count == 1

    def test_the_cache_can_be_disabled(self, client, monkeypatch):
        monkeypatch.setattr(koyura_app, "TRACK_CACHE_TTL_SECONDS", 0)
        with patch.object(koyura_app, "streaming") as module:
            module.parse_track_url.return_value = streaming.TrackRef(
                "spotify", "x", TRACK_URL
            )
            module.resolve_track.return_value = TRACK_BLOCK
            with patch.object(koyura_app, "_run_capture",
                              return_value=(0, YTDLP_JSON, "", False)):
                post_info(client)
                post_info(client)
        assert module.resolve_track.call_count == 2


class TestDownloadTarget:
    def test_a_track_job_is_pointed_at_the_picked_candidate(self, client):
        submitted = {}
        with patch.object(koyura_app, "_track_payload", return_value=TRACK_BLOCK), \
             patch.object(koyura_app.job_queue, "submit") as submit:
            submit.return_value = koyura_app.download_queue.Admission(
                accepted=True, status="queued", position=1, wait_seconds=1,
                estimate_seconds=2.0, queue_length=1,
            )
            response = client.post(
                "/api/download",
                json={
                    "url": TRACK_URL,
                    "format": "audio",
                    "format_id": None,
                    "title": "Never Gonna Give You Up",
                    "candidate_url": TRACK_BLOCK["candidates"][1]["url"],
                },
                headers=HEADERS,
            )
            job_id = response.get_json()["job_id"]
            submitted = dict(koyura_app.jobs[job_id])
        assert response.status_code == 202
        # The job fetches the picked alternative; the source link is recorded.
        assert submitted["url"] == TRACK_BLOCK["candidates"][1]["url"]
        assert submitted["source_url"] == TRACK_URL

    def test_a_candidate_the_engine_did_not_offer_is_refused(self, client):
        with patch.object(koyura_app, "_track_payload", return_value=TRACK_BLOCK):
            response = client.post(
                "/api/download",
                json={
                    "url": TRACK_URL,
                    "format": "audio",
                    "candidate_url": "https://example.com/aaa.mp3",
                },
                headers=HEADERS,
            )
        assert response.status_code == 400
        assert "not one of the offered tracks" in response.get_json()["error"]

    def test_an_ordinary_link_is_untouched(self, client):
        with patch.object(koyura_app, "_download_target", return_value="x") as target, \
             patch.object(koyura_app.job_queue, "submit") as submit:
            submit.return_value = koyura_app.download_queue.Admission(
                accepted=True, status="queued", position=1, wait_seconds=1,
                estimate_seconds=2.0, queue_length=1,
            )
            client.post(
                "/api/download",
                json={
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "format": "video",
                    "title": "x",
                },
                headers=HEADERS,
            )
        target.assert_called_once()
        assert target.call_args.args[1] is None


class TestMediaMeta:
    def test_applying_a_track_keeps_the_files_formats(self):
        import media_meta

        merged = media_meta.apply_track_metadata(YTDLP_INFO, TRACK_BLOCK)
        assert merged["title"] == "Never Gonna Give You Up"
        assert merged["uploader"] == "Rick Astley"
        assert merged["formats"] == YTDLP_INFO["formats"]
        assert merged["track"] is TRACK_BLOCK

    def test_applying_nothing_changes_nothing(self):
        import media_meta

        assert media_meta.apply_track_metadata(YTDLP_INFO, None) == YTDLP_INFO

    def test_the_payload_carries_the_track_only_when_there_is_one(self):
        import media_meta

        assert media_meta.build_info_payload(YTDLP_INFO)["track"] is None
        merged = media_meta.apply_track_metadata(YTDLP_INFO, TRACK_BLOCK)
        assert media_meta.build_info_payload(merged)["track"] == TRACK_BLOCK
