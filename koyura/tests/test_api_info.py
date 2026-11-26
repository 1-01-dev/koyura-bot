"""Tests that /api/info forwards the new metadata fields through to the bot.

yt-dlp itself is stubbed: the point is the additive JSON contract, not the
network. The historical keys an older bot reads must keep their shape.
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

import pytest  # noqa: E402

import app as koyura_app  # noqa: E402

HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}

#: The URL the request body carries; the engine only validates its shape here.
SAMPLE_URL = "https://www.youtube.com/watch?v=Ke6XX8FHOHM"

# Metadata captured from a real yt-dlp run rather than invented, so the JSON
# contract is tested against the shape YouTube actually returns. The clip itself
# is in the public domain (NASA: a work of the U.S. federal government, 17
# U.S.C. § 105), captured 2026-11-27 from
# https://www.youtube.com/watch?v=Ke6XX8FHOHM with ``yt-dlp -J``. Nothing is
# fetched at test time: the values below are fixed.
YTDLP_JSON = {
    "title": "Artemis II to the Moon: Launch to Splashdown (NASA Mission Animation)",
    "thumbnail": "https://i.ytimg.com/vi/Ke6XX8FHOHM/maxresdefault.jpg",
    "duration": 465,
    "uploader": "NASA",
    "uploader_id": "@NASA",
    "channel": "NASA",
    "channel_follower_count": 15100000,
    "view_count": 2927499,
    "like_count": 37485,
    "comment_count": None,
    "categories": ["Science & Technology"],
    "upload_date": "20250210",
    "extractor": "youtube",
    "formats": [
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 129.485, "filesize": 7519596},
        {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "height": 1080,
         "fps": 30, "filesize": 77825086},
        {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4D401F", "height": 720,
         "fps": 30, "filesize": 23460805},
    ],
}


@pytest.fixture()
def client():
    koyura_app.app.config.update(TESTING=True)
    with koyura_app.app.test_client() as test_client:
        yield test_client


def _info(client, payload=None):
    """Call /api/info with yt-dlp stubbed to return ``payload``."""
    body = payload if payload is not None else YTDLP_JSON
    with patch.object(koyura_app, "_run_capture",
                      return_value=(0, json.dumps(body), "", False)):
        return client.post(
            "/api/info", json={"url": SAMPLE_URL},
            headers=HEADERS,
        )


class TestInfoPayload:
    def test_new_fields_are_returned(self, client):
        resp = _info(client)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["uploader_id"] == "@NASA"
        assert body["channel"] == "NASA"
        assert body["channel_follower_count"] == 15100000
        assert body["view_count"] == 2927499
        assert body["like_count"] == 37485
        # NASA exposes no comment count; the key is present and null.
        assert body["comment_count"] is None
        assert body["categories"] == ["Science & Technology"]
        assert body["upload_date"] == "2025-02-10"
        assert body["audio"]["abr"] == 129
        assert body["audio"]["size_label"] == "7.2MB"

    def test_formats_carry_sizes(self, client):
        formats = _info(client).get_json()["formats"]
        assert [f["height"] for f in formats] == [1080, 720]
        assert formats[0]["size_label"] == "74.2MB"
        assert formats[0]["button_label"] == "1080p · 74.2MB"
        assert formats[0]["size_bytes"] == 77825086

    def test_historical_keys_are_unchanged(self, client):
        """An older bot must still find every key it knows in the response."""
        body = _info(client).get_json()
        assert body["title"].startswith("Artemis II to the Moon")
        assert body["thumbnail"] == "https://i.ytimg.com/vi/Ke6XX8FHOHM/maxresdefault.jpg"
        assert body["duration"] == 465
        assert body["uploader"] == "NASA"
        assert body["extractor"] == "youtube"
        for fmt in body["formats"]:
            assert {"id", "label", "height"} <= set(fmt)
            assert fmt["label"] == f"{fmt['height']}p"

    def test_missing_fields_do_not_break_the_response(self, client):
        body = _info(client, {"title": "Only", "extractor": "x"}).get_json()
        assert body["title"] == "Only"
        assert body["formats"] == []
        assert body["channel_follower_count"] is None
        assert body["upload_date"] is None
        assert body["audio"] is None

    def test_formats_with_unknown_size_have_no_size_label(self, client):
        payload = dict(YTDLP_JSON, formats=[{
            "format_id": "22", "ext": "mp4", "vcodec": "avc1.4d401f", "height": 720,
        }])
        fmt = _info(client, payload).get_json()["formats"][0]
        assert fmt["size_label"] is None
        assert fmt["button_label"] == "720p"

    def test_unparseable_ytdlp_output_is_a_400(self, client):
        with patch.object(koyura_app, "_run_capture", return_value=(0, "not json", "", False)):
            resp = client.post(
                "/api/info", json={"url": "https://www.youtube.com/watch?v=x"},
                headers=HEADERS,
            )
        assert resp.status_code == 400

    def test_token_is_still_required(self, client):
        with patch.object(koyura_app, "_run_capture",
                          return_value=(0, json.dumps(YTDLP_JSON), "", False)):
            resp = client.post(
                "/api/info", json={"url": "https://www.youtube.com/watch?v=x"},
            )
        assert resp.status_code == 401
