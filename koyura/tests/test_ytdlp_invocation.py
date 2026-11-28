"""yt-dlp / ffmpeg invocation tests.

systemd units start with a minimal PATH, so the engine must run yt-dlp through
the interpreter of its own virtualenv (``sys.executable -m yt_dlp``) instead of
relying on a ``yt-dlp`` binary being on PATH.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ["DOWNLOADS_PATH"] = tempfile.mkdtemp()

import app as koyura_app  # noqa: E402

TOKEN = os.environ["INTERNAL_API_TOKEN"]
HEADERS = {"X-Internal-Token": TOKEN}


def test_download_command_uses_current_interpreter():
    cmd = koyura_app._build_download_cmd(
        "job1", "https://example.com/video", "video", None
    )
    assert cmd[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "yt-dlp" not in cmd, "the engine must not rely on a yt-dlp binary on PATH"
    assert cmd[-1] == "https://example.com/video"


def test_download_command_audio_variant():
    cmd = koyura_app._build_download_cmd(
        "job2", "https://example.com/audio", "audio", None
    )
    assert cmd[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "--audio-format" in cmd


def test_audio_download_asks_for_the_audio_stream_only():
    """Audio jobs must not fetch the video stream first.

    Without an explicit ``-f``, yt-dlp falls back to ``bv*+ba/b``: a 4K source
    downloaded hundreds of megabytes of video before ffmpeg stripped the audio.
    """
    cmd = koyura_app._build_download_cmd(
        "job3", "https://example.com/audio", "audio", None
    )
    assert "-f" in cmd, "audio jobs must pin the format selector"
    selector = cmd[cmd.index("-f") + 1]
    assert "bestaudio" in selector
    # ``-x`` extracts the audio; merging into an mp4 is a video-only concept.
    assert "-x" in cmd
    assert "--merge-output-format" not in cmd


def test_audio_selector_ignores_a_stray_format_id():
    """A format id belongs to the video path; audio stays audio-only."""
    cmd = koyura_app._build_download_cmd(
        "job4", "https://example.com/audio", "audio", "137"
    )
    selector = cmd[cmd.index("-f") + 1]
    assert selector == "bestaudio/best"
    assert "137" not in selector


def test_video_download_keeps_its_format_selector():
    """The audio fix must not change the video command."""
    cmd = koyura_app._build_download_cmd(
        "job5", "https://example.com/video", "video", None
    )
    selector = cmd[cmd.index("-f") + 1]
    assert selector != "bestaudio/best"
    assert "bv*" in selector
    assert "--merge-output-format" in cmd
    assert "-x" not in cmd


def test_explicit_video_format_id_still_merges_audio():
    cmd = koyura_app._build_download_cmd(
        "job6", "https://example.com/video", "video", "137"
    )
    assert cmd[cmd.index("-f") + 1] == "137+bestaudio/best"
    assert "--merge-output-format" in cmd


def test_info_endpoint_uses_current_interpreter(monkeypatch):
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, timeout):
        captured["cmd"] = cmd
        return 0, '{"title": "Example", "formats": []}', "", False

    monkeypatch.setattr(koyura_app, "_run_capture", fake_run)
    monkeypatch.setattr(koyura_app, "validate_url", lambda url: url)

    with koyura_app.app.test_client() as client:
        resp = client.post(
            "/api/info",
            json={"url": "https://example.com/video"},
            headers=HEADERS,
        )

    assert resp.status_code == 200
    assert captured["cmd"][:3] == [sys.executable, "-m", "yt_dlp"]
    assert "yt-dlp" not in captured["cmd"]


def test_ffmpeg_and_ffprobe_are_called_by_name(tmp_path, monkeypatch):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"not really a video")
    calls: list[str] = []

    def fake_run(cmd, timeout):
        calls.append(cmd[0])
        if cmd[0] == "ffprobe":
            return 0, "vp9\n", "", False
        return 1, "", "transcode failed", False

    monkeypatch.setattr(koyura_app, "_run_capture", fake_run)
    koyura_app._make_telegram_compatible(str(media))

    assert calls, "no ffmpeg tooling was invoked"
    assert calls[0] == "ffprobe"
    assert "ffmpeg" in calls


def test_probe_media_uses_ffprobe(tmp_path, monkeypatch):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x")
    calls: list[str] = []

    def fake_run(cmd, timeout):
        calls.append(cmd[0])
        return 0, '{"streams": [{"width": 1920, "height": 1080}], "format": {"duration": "12.5"}}', "", False

    monkeypatch.setattr(koyura_app, "_run_capture", fake_run)
    probe = koyura_app._probe_media(str(media))

    assert calls == ["ffprobe"]
    assert probe["width"] == 1920
    assert probe["duration"] == 12.5


# ---------------------------------------------------------------------------
# A substituted track: audio only, and the player client that gets there
# ---------------------------------------------------------------------------

#: Every selector that would make yt-dlp fetch a *video* stream.
VIDEO_SELECTOR_MARKERS = ("bv*", "bestvideo", "vcodec", "+bestaudio", "bestvideo+")


def _track_audio_cmd(client="default,android"):
    return koyura_app._build_download_cmd(
        "track1",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "audio",
        None,
        youtube_client=client,
    )


def test_a_track_job_has_no_video_selector():
    """The whole promise of the substitution is one MP3, so no video stream may
    be selected: a ``bv*``/``bestvideo`` selector would download the picture too
    and a ``+bestaudio`` merge would mean a second stream."""
    cmd = _track_audio_cmd()
    selector = cmd[cmd.index("-f") + 1]
    assert selector == "bestaudio/best"
    for marker in VIDEO_SELECTOR_MARKERS:
        assert marker not in selector, marker


def test_a_track_job_does_not_merge_output():
    """``--merge-output-format`` is a video-only concept; with it in the command
    the output could never be an MP3."""
    cmd = _track_audio_cmd()
    assert "--merge-output-format" not in cmd
    assert "mp4" not in cmd


def test_a_track_job_extracts_mp3():
    cmd = _track_audio_cmd()
    assert "-x" in cmd
    assert cmd[cmd.index("--audio-format") + 1] == "mp3"


def test_a_track_job_asks_for_the_cover_and_the_tags():
    """The MP3 is tagged from the streaming metadata; the cover has to be
    fetched for that to be possible."""
    cmd = _track_audio_cmd()
    assert "--write-thumbnail" in cmd
    assert "--convert-thumbnails" in cmd


def test_a_track_job_pins_the_youtube_player_clients():
    cmd = _track_audio_cmd()
    assert "--extractor-args" in cmd
    value = cmd[cmd.index("--extractor-args") + 1]
    assert value == "youtube:player_client=default,android"


def test_an_ordinary_job_gets_no_extractor_args():
    """Nothing outside the substitution path changes behaviour."""
    assert "--extractor-args" not in koyura_app._build_download_cmd(
        "plain", "https://example.com/v", "audio", None
    )
    assert "--extractor-args" not in koyura_app._build_download_cmd(
        "plain", "https://example.com/v", "video", None
    )


def test_the_client_is_configurable(monkeypatch):
    monkeypatch.setenv("YOUTUBE_PLAYER_CLIENT", "android")
    cmd = _track_audio_cmd(koyura_app.streaming.player_client())
    assert cmd[cmd.index("--extractor-args") + 1] == "youtube:player_client=android"


def test_the_search_and_the_download_agree_on_the_client():
    """The hit the user is offered must be the hit that downloads, so the search
    and the extraction are pinned to the same client list."""
    import streaming

    client = streaming.player_client()
    search = streaming.search_cmd("youtube", "a b", python="py", client=client)
    assert search[search.index("--extractor-args") + 1] == (
        f"youtube:player_client={client}"
    )
    assert client in _track_audio_cmd()[_track_audio_cmd().index("--extractor-args") + 1]


def test_the_info_lookup_for_a_candidate_pins_the_client(monkeypatch):
    captured = {}

    def fake_run(cmd, timeout):
        captured["cmd"] = cmd
        return 0, '{"title": "x", "formats": []}', "", False

    monkeypatch.setattr(koyura_app, "_run_capture", fake_run)
    cmd = koyura_app._build_info_cmd(
        "https://www.youtube.com/watch?v=x",
        youtube_client=koyura_app.streaming.player_client(),
    )
    assert "--extractor-args" in cmd
    # The URL stays last: an option after it could be swallowed by it.
    assert cmd[-1] == "https://www.youtube.com/watch?v=x"


def test_a_job_for_a_track_carries_the_client_but_a_plain_one_does_not(monkeypatch):
    """``/api/download`` records the client on a track job only."""
    import streaming

    monkeypatch.setattr(
        koyura_app.job_queue, "submit", lambda *a, **k: _accepted()
    )
    # The engine must not dial Spotify for this: the block is what matters.
    monkeypatch.setattr(koyura_app, "_track_payload", lambda url: {
        "service": "spotify", "service_label": "Spotify", "title": "x",
        "artist": "y", "duration": 200, "thumbnail": "",
        "candidates": [
            {"index": 1, "label": "1 · 3:20", "duration": 200,
             "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        ],
    })

    with koyura_app.app.test_client() as client:
        plain = client.post(
            "/api/download",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                  "format": "audio", "title": "x"},
            headers=HEADERS,
        ).get_json()
        track_job = client.post(
            "/api/download",
            json={"url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
                  "format": "audio", "title": "x",
                  "candidate_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            headers=HEADERS,
        ).get_json()

    plain_job = koyura_app.jobs[plain["job_id"]]
    track_entry = koyura_app.jobs[track_job["job_id"]]
    assert plain_job["youtube_client"] is None
    assert track_entry["youtube_client"] == streaming.player_client()


def _accepted():
    return koyura_app.download_queue.Admission(
        accepted=True, status="queued", position=1, wait_seconds=1,
        estimate_seconds=2.0, queue_length=1,
    )
