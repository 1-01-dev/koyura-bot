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
