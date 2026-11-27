"""The engine side of the conversion policy: HTTP fields and the job's choice.

``test_transcode.py`` pins the pure policy; this module pins the three places
where it meets a running engine:

* ``/api/info`` carries the policy and a per-format codec verdict, so the bot
  can build its keyboard without a second request;
* ``/api/download`` accepts ``transcode=force`` (the user's *Convert* tap, which
  works even with ``TRANSCODE_MODE=off``) and ``transcode=never`` (send as a
  file);
* ``/api/status`` reports the ``converting`` stage and whether the delivered
  file must be sent as a document.

ffmpeg is always stubbed: the assertions are about the argument vector and the
job state, never about encoding anything.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ.setdefault("DOWNLOADS_PATH", tempfile.mkdtemp())
os.environ.setdefault("KOYURA_STATE_DIR", tempfile.mkdtemp())
os.environ.pop("TRANSCODE_MODE", None)

import app as koyura_app  # noqa: E402
import transcode  # noqa: E402

HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}
URL = "https://www.youtube.com/watch?v=Ke6XX8FHOHM"

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test gets its own downloads dir, state dir and job registry."""
    monkeypatch.setattr(koyura_app, "DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(koyura_app, "jobs", {})
    monkeypatch.setattr(koyura_app, "validate_url", lambda url: url)
    monkeypatch.setattr(koyura_app, "TRANSCODE_MODE", "off")
    monkeypatch.setattr(koyura_app, "TRANSCODE_MAX_MB", 300)
    monkeypatch.setattr(
        koyura_app, "transcode_stats", transcode.TranscodeStats(None)
    )
    return tmp_path


def _post(client, path, payload):
    return client.post(path, json=payload, headers=HEADERS)


def _fake_media(path, size=2048):
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# /api/info
# ---------------------------------------------------------------------------

INFO_JSON = {
    "title": "Clip",
    "duration": 300,
    "formats": [
        {"format_id": "399", "ext": "mp4", "vcodec": "av01.0.08M.0", "height": 2160,
         "filesize": 2_400 * MB},
        {"format_id": "271", "ext": "mp4", "vcodec": "vp09.00.40.0", "height": 1440,
         "filesize": 451 * MB},
        {"format_id": "299", "ext": "mp4", "vcodec": "avc1.64002a", "height": 1080,
         "filesize": 220 * MB},
    ],
    "formats_note": "kept for readability",
}


def test_info_reports_the_policy_and_the_per_format_codec(monkeypatch):
    monkeypatch.setattr(
        koyura_app, "_run_capture",
        lambda cmd, timeout: (0, json.dumps(INFO_JSON), "", False),
    )
    with koyura_app.app.test_client() as client:
        resp = _post(client, "/api/info", {"url": URL})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["transcode"] == {"mode": "off", "max_mb": 300}
    labels = {fmt["height"]: fmt for fmt in body["formats"]}
    assert labels[2160]["codec_label"] == "AV1"
    assert labels[2160]["compatible"] is False
    assert labels[1440]["codec_label"] == "VP9"
    assert labels[1080]["compatible"] is True
    # The estimate is per format, and only where a conversion could happen.
    assert labels[2160]["transcode_estimate_seconds"] > 0
    assert labels[1080]["transcode_estimate_seconds"] is None


def test_info_reflects_the_configured_mode(monkeypatch):
    monkeypatch.setattr(koyura_app, "TRANSCODE_MODE", "auto")
    monkeypatch.setattr(koyura_app, "TRANSCODE_MAX_MB", 120)
    monkeypatch.setattr(
        koyura_app, "_run_capture",
        lambda cmd, timeout: (0, json.dumps(INFO_JSON), "", False),
    )
    with koyura_app.app.test_client() as client:
        body = _post(client, "/api/info", {"url": URL}).get_json()
    assert body["transcode"] == {"mode": "auto", "max_mb": 120}


def test_the_policy_endpoint_is_readable(monkeypatch):
    monkeypatch.setattr(koyura_app, "TRANSCODE_MODE", "always")
    with koyura_app.app.test_client() as client:
        body = client.get("/api/transcode/policy", headers=HEADERS).get_json()
    assert body["mode"] == "always"
    assert body["video_threads"] == 1
    assert body["preset"] == "veryfast"
    assert body["stats"]["samples"] == 0


# ---------------------------------------------------------------------------
# /api/download: the user's choice travels with the job
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload,expected",
    [
        ({}, "never"),
        ({"transcode": "never"}, "never"),
        ({"transcode": "force"}, "force"),
        ({"transcode": "FORCE"}, "force"),
    ],
)
def test_download_records_the_transcode_choice(payload, expected):
    with koyura_app.app.test_client() as client:
        resp = _post(client, "/api/download", {"url": URL, "title": "T", **payload})
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]
    assert koyura_app.jobs[job_id]["transcode"] == expected


def test_download_refuses_an_unknown_transcode_value():
    with koyura_app.app.test_client() as client:
        resp = _post(client, "/api/download",
                     {"url": URL, "transcode": "sometimes"})
    assert resp.status_code == 400
    assert "transcode" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

def test_status_reports_the_conversion_facts(monkeypatch):
    with koyura_app.app.test_client() as client:
        job_id = _post(client, "/api/download", {"url": URL}).get_json()["job_id"]
    koyura_app._job_update(
        job_id,
        status="downloading",
        stage="converting",
        vcodec="h264",
        converted=True,
        send_as_document=False,
    )
    with koyura_app.app.test_client() as client:
        body = client.get(f"/api/status/{job_id}", headers=HEADERS).get_json()
    assert body["stage"] == "converting"
    assert body["vcodec"] == "h264"
    assert body["converted"] is True
    assert body["send_as_document"] is False


def test_a_finished_job_reports_no_stage(monkeypatch):
    with koyura_app.app.test_client() as client:
        job_id = _post(client, "/api/download", {"url": URL}).get_json()["job_id"]
    koyura_app._job_update(job_id, status="done", stage="converting", vcodec="vp9")
    with koyura_app.app.test_client() as client:
        body = client.get(f"/api/status/{job_id}", headers=HEADERS).get_json()
    assert body["stage"] is None
    assert body["vcodec"] == "vp9"


# ---------------------------------------------------------------------------
# _make_telegram_compatible: the policy meets a real file
# ---------------------------------------------------------------------------

def _stub_tools(monkeypatch, vcodec, *, convert_ok=True):
    """ffprobe answers ``vcodec``; ffmpeg records its vector and succeeds.

    Both ffmpeg paths are stubbed: ``_faststart``/``_convert_untracked`` go
    through ``_run_capture``, while the job-aware conversion uses
    ``subprocess.Popen`` so it can stream progress.
    """
    calls: list[list[str]] = []

    def _write(path, payload):
        with open(path, "wb") as handle:
            handle.write(payload)

    def fake_run(cmd, timeout):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return 0, f"{vcodec}\n", "", False
        if "-c:v" in cmd:
            if not convert_ok:
                return 1, "", "encoder exploded", False
            _write(cmd[-1], b"converted")
            return 0, "", "", False
        _write(cmd[-1], b"faststart")
        return 0, "", "", False

    class _Done:
        returncode = 0
        stdout = iter(())
        stderr = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        if not convert_ok:
            _Done.returncode = 1
        else:
            _Done.returncode = 0
            _write(cmd[-1], b"converted")
        return _Done()

    monkeypatch.setattr(koyura_app, "_run_capture", fake_run)
    monkeypatch.setattr(koyura_app.subprocess, "Popen", fake_popen)
    return calls


def test_off_leaves_a_vp9_file_alone_and_marks_it_a_document(tmp_path, monkeypatch):
    media = _fake_media(tmp_path / "job1.mp4")
    calls = _stub_tools(monkeypatch, "vp9")
    koyura_app.jobs["job1"] = {"format": "video"}

    converted = koyura_app._make_telegram_compatible(str(media), job_id="job1")

    assert converted is False
    assert not any("-c:v" in cmd for cmd in calls), "ffmpeg encoded with mode=off"
    assert not (tmp_path / "job1.mp4.h264.mp4").exists()
    job = koyura_app.jobs["job1"]
    assert job["send_as_document"] is True
    assert job["vcodec"] == "vp9"
    assert job["converted"] is False


def test_off_still_moves_the_moov_atom_without_encoding(tmp_path, monkeypatch):
    media = _fake_media(tmp_path / "job2.mp4")
    calls = _stub_tools(monkeypatch, "h264")
    koyura_app.jobs["job2"] = {"format": "video"}

    converted = koyura_app._make_telegram_compatible(str(media), job_id="job2")

    assert converted is False
    assert [cmd[0] for cmd in calls] == ["ffprobe", "ffmpeg"]
    assert "-c" in calls[-1] and "copy" in calls[-1]
    assert koyura_app.jobs["job2"]["send_as_document"] is False


def test_auto_converts_a_small_incompatible_file(tmp_path, monkeypatch):
    media = _fake_media(tmp_path / "job3.mp4", size=MB)
    calls = _stub_tools(monkeypatch, "vp9")
    koyura_app.jobs["job3"] = {"format": "video"}
    monkeypatch.setattr(koyura_app, "TRANSCODE_MODE", "auto")

    converted = koyura_app._make_telegram_compatible(str(media), job_id="job3")

    assert converted is True
    conversion = [cmd for cmd in calls if "-c:v" in cmd][0]
    assert conversion[conversion.index("-threads") + 1] == "1"
    assert "+faststart" in conversion
    assert media.read_bytes() == b"converted"
    assert koyura_app.jobs["job3"]["converted"] is True
    assert koyura_app.jobs["job3"]["vcodec"] == "h264"
    assert koyura_app.jobs["job3"]["stage"] is None


def test_auto_refuses_a_file_over_the_ceiling(tmp_path, monkeypatch):
    """Over TRANSCODE_MAX_MB the file is sent as a document, not converted."""
    media = _fake_media(tmp_path / "job4.mp4", size=MB)
    calls = _stub_tools(monkeypatch, "av1")
    koyura_app.jobs["job4"] = {"format": "video"}
    monkeypatch.setattr(koyura_app, "TRANSCODE_MODE", "auto")
    monkeypatch.setattr(koyura_app, "TRANSCODE_MAX_MB", 0.0001)  # 100 kB

    converted = koyura_app._make_telegram_compatible(str(media), job_id="job4")

    assert converted is False
    assert not any("-c:v" in cmd for cmd in calls)
    assert koyura_app.jobs["job4"]["send_as_document"] is True
    assert koyura_app.jobs["job4"]["converted"] is False


def test_the_users_tap_converts_even_with_mode_off(tmp_path, monkeypatch):
    media = _fake_media(tmp_path / "job5.mp4", size=8 * MB)
    calls = _stub_tools(monkeypatch, "vp9")
    koyura_app.jobs["job5"] = {"format": "video", "transcode": "force"}

    converted = koyura_app._make_telegram_compatible(
        str(media), job_id="job5", force=True
    )

    assert converted is True
    assert any("-c:v" in cmd for cmd in calls)
    assert koyura_app.jobs["job5"]["converted"] is True


def test_forced_conversion_ignores_the_size_ceiling(tmp_path, monkeypatch):
    """mode=auto + force: the ceiling is a server-safety default, not a wall."""
    media = _fake_media(tmp_path / "job6.mp4", size=900 * MB)
    _stub_tools(monkeypatch, "av1")
    koyura_app.jobs["job6"] = {"format": "video"}
    monkeypatch.setattr(koyura_app, "TRANSCODE_MODE", "auto")

    assert koyura_app._make_telegram_compatible(str(media), job_id="job6", force=True)


def test_a_failed_conversion_keeps_the_original_file(tmp_path, monkeypatch):
    media = _fake_media(tmp_path / "job7.mp4")
    _stub_tools(monkeypatch, "vp9", convert_ok=False)
    koyura_app.jobs["job7"] = {"format": "video"}
    monkeypatch.setattr(koyura_app, "TRANSCODE_MODE", "always")

    converted = koyura_app._make_telegram_compatible(str(media), job_id="job7")

    assert converted is False
    assert media.read_bytes() == b"x" * 2048
    assert not (tmp_path / "job7.mp4.h264.mp4").exists()
    assert koyura_app.jobs["job7"]["stage"] is None
    assert not koyura_app.jobs["job7"].get("converted")


def test_a_conversion_reports_progress_from_ffmpeg(tmp_path, monkeypatch):
    """``-progress pipe:1`` becomes the percentage on /api/status."""
    media = _fake_media(tmp_path / "job8.mp4")
    seen: list[dict] = []

    class FakeStdout:
        def __iter__(self):
            yield "out_time_us=15000000\n"
            yield "progress=continue\n"
            yield "out_time_us=60000000\n"

    class FakeProcess:
        returncode = 0
        stdout = FakeStdout()
        stderr = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(cmd, **kwargs):
        seen.append(kwargs)
        with open(cmd[-1], "wb") as handle:
            handle.write(b"converted")
        return FakeProcess()

    monkeypatch.setattr(koyura_app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(koyura_app, "_probe_media", lambda path: {"duration": 60.0})
    koyura_app.jobs["job8"] = {"format": "video"}

    koyura_app._convert_to_h264("job8", str(media), 60.0)

    job = koyura_app.jobs["job8"]
    assert job["converted"] is True
    assert job["progress"]["percent"] == 100.0


# ---------------------------------------------------------------------------
# POST /api/transcode/<job_id>
# ---------------------------------------------------------------------------

def _finished_job(tmp_path, job_id="jobT", *, vcodec="vp9", size=4 * MB):
    media = _fake_media(tmp_path / f"{job_id}.mp4", size=size)
    koyura_app.jobs[job_id] = {
        "status": "done",
        "format": "video",
        "file": str(media),
        "file_path": str(media),
        "filename": f"{job_id}.mp4",
        "vcodec": vcodec,
        "converted": vcodec == "h264",
        "transcoding": False,
    }
    return media


def test_transcode_converts_a_finished_incompatible_job(tmp_path, monkeypatch):
    media = _finished_job(tmp_path)
    started = {}

    def fake_convert(job_id, path, duration):
        started["job_id"] = job_id
        started["threads"] = transcode.VIDEO_THREADS
        with open(path, "wb") as handle:
            handle.write(b"h264")
        return True

    monkeypatch.setattr(koyura_app, "_convert_to_h264", fake_convert)
    monkeypatch.setattr(koyura_app, "_probe_media", lambda path: {"duration": 12.0})
    monkeypatch.setattr(koyura_app.job_queue, "acquire", lambda job_id, kind=None: True)
    monkeypatch.setattr(koyura_app.job_queue, "finish", lambda job_id, **kw: None)

    with koyura_app.app.test_client() as client:
        resp = client.post("/api/transcode/jobT", headers=HEADERS)
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["stage"] == "converting"
    assert body["estimate_seconds"] > 0

    # The work runs on its own thread; wait for it to settle.
    for _ in range(400):
        if not koyura_app.jobs["jobT"].get("transcoding"):
            break
        time.sleep(0.01)
    assert started.get("job_id") == "jobT"
    assert started.get("threads") == 1
    assert media.read_bytes() == b"h264"
    assert koyura_app.jobs["jobT"]["converted"] is True
    assert koyura_app.jobs["jobT"]["send_as_document"] is False


def test_transcode_refuses_a_playable_file(tmp_path):
    _finished_job(tmp_path, vcodec="h264")
    with koyura_app.app.test_client() as client:
        resp = client.post("/api/transcode/jobT", headers=HEADERS)
    assert resp.status_code == 400
    assert "already playable" in resp.get_json()["error"]


def test_transcode_refuses_an_unknown_job():
    with koyura_app.app.test_client() as client:
        resp = client.post("/api/transcode/nope", headers=HEADERS)
    assert resp.status_code == 404


def test_transcode_refuses_an_audio_job(tmp_path):
    _finished_job(tmp_path, vcodec="vp9")
    koyura_app.jobs["jobT"]["format"] = "audio"
    with koyura_app.app.test_client() as client:
        resp = client.post("/api/transcode/jobT", headers=HEADERS)
    assert resp.status_code == 400


def test_transcode_twice_is_a_conflict(tmp_path):
    _finished_job(tmp_path)
    koyura_app.jobs["jobT"]["transcoding"] = True
    with koyura_app.app.test_client() as client:
        resp = client.post("/api/transcode/jobT", headers=HEADERS)
    assert resp.status_code == 409


def test_transcode_needs_the_internal_token(tmp_path):
    _finished_job(tmp_path)
    with koyura_app.app.test_client() as client:
        resp = client.post("/api/transcode/jobT")
    assert resp.status_code == 401
