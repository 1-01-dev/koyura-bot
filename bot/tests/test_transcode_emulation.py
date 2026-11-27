"""End-to-end emulation: one real payload, three differring codecs, no network.

This is the scenario the owner asked for, run as a whole rather than as units:

    /api/info with 2160p AV1, 1440p VP9 and 1080p H.264
        -> the keyboard offers all three, H.264 unmarked, the others labelled
        -> the card carries the one-line codec warning
        -> tapping 2160p asks instead of downloading
        -> "Send as file" starts a job with ``transcode=never``
        -> the finished file is uploaded with ``send_document``
        -> "Convert" instead starts the same job with ``transcode=force``
        -> the engine's ``converting`` stage becomes "Converting…" and the
           finished, now-H.264 file is uploaded with ``send_video``

The ``/api/info`` body is produced by the engine's own shaping helpers, so the
field names are the contract and not a hand-written imitation of it.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

BOT_DIR = Path(__file__).resolve().parents[1]
KOYURA_DIR = BOT_DIR.parent / "koyura"
sys.path.insert(0, str(BOT_DIR))

import pytest  # noqa: E402

import download_card  # noqa: E402
import transcode_policy as policy  # noqa: E402
from handlers import callbacks, config, download_flow, sessions  # noqa: E402


def _engine_module():
    """Load the engine's ``media_meta`` by path.

    The bot never imports the engine package in production (they are separate
    services with separate images), so this test loads the one pure module it
    needs the way a reviewer would: straight from its file. That keeps the
    ``/api/info`` field names here the engine's own, not a hand-written copy.

    ``media_meta`` imports the engine's conversion policy (``transcode``), which
    the bot does not ship — the bot has its own ``transcode_policy`` under a
    different name — so that module is loaded first.
    """
    if "transcode" not in sys.modules:
        policy_spec = importlib.util.spec_from_file_location(
            "transcode", KOYURA_DIR / "transcode.py"
        )
        assert policy_spec is not None and policy_spec.loader is not None
        policy_module = importlib.util.module_from_spec(policy_spec)
        sys.modules["transcode"] = policy_module
        policy_spec.loader.exec_module(policy_module)

    path = KOYURA_DIR / "media_meta.py"
    if "koyura_media_meta" not in sys.modules:
        spec = importlib.util.spec_from_file_location("koyura_media_meta", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["koyura_media_meta"] = module
        spec.loader.exec_module(module)
    return sys.modules["koyura_media_meta"]

MB = 1024 * 1024
HASH = "0123456789ab"

#: A trimmed yt-dlp blob: one stream per resolution, three different codecs,
#: sizes that a 2000 MB send limit accepts.
YTDLP_BLOB = {
    "title": "Artemis II to the Moon",
    "duration": 635,
    "extractor": "youtube",
    "channel": "NASA",
    "formats": [
        {"format_id": "399", "ext": "mp4", "vcodec": "av01.0.08M.0",
         "height": 2160, "filesize": 900 * MB},
        {"format_id": "271", "ext": "mp4", "vcodec": "vp09.00.40.0",
         "height": 1440, "filesize": 451 * MB},
        {"format_id": "299", "ext": "mp4", "vcodec": "avc1.64002a",
         "height": 1080, "filesize": 220 * MB},
        {"format_id": "140", "ext": "m4a", "vcodec": "none",
         "acodec": "mp4a.40.2", "abr": 129, "filesize": 9 * MB},
    ],
}


def _engine_info(mode="off", max_mb=300):
    """The ``/api/info`` body the engine would answer with."""
    media_meta = _engine_module()
    return media_meta.build_info_payload(
        YTDLP_BLOB,
        transcode_info={"mode": mode, "max_mb": max_mb},
        estimate=lambda size, duration: (240.0, False),
    )


def _plain(text):
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _clean_sessions():
    sessions.forget_all()
    yield
    sessions.forget_all()


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    real_sleep = download_flow.asyncio.sleep

    async def _fast(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(download_flow.asyncio, "sleep", _fast)


class RecordingBot:
    def __init__(self):
        self.calls = []
        self._next_id = 200

    async def _record(self, name, kwargs, message=False):
        self.calls.append((name, kwargs))
        if message:
            self._next_id += 1
            return MagicMock(message_id=self._next_id)
        return True

    async def send_message(self, **kw):
        return await self._record("send_message", kw, True)

    async def send_photo(self, **kw):
        return await self._record("send_photo", kw, True)

    async def send_video(self, **kw):
        return await self._record("send_video", kw, True)

    async def send_audio(self, **kw):
        return await self._record("send_audio", kw, True)

    async def send_document(self, **kw):
        return await self._record("send_document", kw, True)

    async def delete_message(self, **kw):
        return await self._record("delete_message", kw)

    async def edit_message_text(self, **kw):
        return await self._record("edit_message_text", kw)

    async def edit_message_caption(self, **kw):
        return await self._record("edit_message_caption", kw)

    async def edit_message_reply_markup(self, **kw):
        return await self._record("edit_message_reply_markup", kw)

    def of(self, name):
        return [kw for called, kw in self.calls if called == name]

    def names(self):
        return [name for name, _ in self.calls]


# ---------------------------------------------------------------------------
# 1. The card
# ---------------------------------------------------------------------------

def test_the_keyboard_offers_every_height_and_marks_the_risky_codecs():
    info = _engine_info()
    markup = download_card.build_format_keyboard(info, 11, HASH, lang="en")

    assert _labels(markup) == [
        "AV1 · 2160p · 900MB",
        "VP9 · 1440p · 451MB",
        "1080p · 220MB",     # H.264, unmarked
        "MP3 · 9.0MB",
    ]
    assert _datas(markup)[:3] == [
        f"dl:11:{HASH}:399",
        f"dl:11:{HASH}:271",
        f"dl:11:{HASH}:299",
    ]


def test_h264_is_preferred_when_the_site_offers_it_at_the_same_height():
    """A 1080p avc1 + 1080p vp9 pair yields one button, and it is the avc1."""
    blob = dict(YTDLP_BLOB)
    blob["formats"] = [
        {"format_id": "vp9", "ext": "mp4", "vcodec": "vp09.00.40.0",
         "height": 1080, "filesize": 90 * MB},
        {"format_id": "avc", "ext": "mp4", "vcodec": "avc1.640028",
         "height": 1080, "filesize": 120 * MB},
    ]
    import_media_meta = _engine_module()
    info = import_media_meta.build_info_payload(blob)
    markup = download_card.build_format_keyboard(info, 11, HASH, lang="en")
    assert _labels(markup)[0] == "1080p · 120MB"
    assert _datas(markup)[0] == f"dl:11:{HASH}:avc"


def test_the_card_explains_the_marks_in_both_languages():
    info = _engine_info()
    english = _plain(download_card.build_card(info, lang="en"))
    russian = _plain(download_card.build_card(info, lang="ru"))
    assert "AV1/VP9 may not play on iPhone" in english
    assert "AV1/VP9 может не проигрываться на iPhone" in russian


# ---------------------------------------------------------------------------
# 2. Tapping 2160p asks
# ---------------------------------------------------------------------------

def _card(info, message_id=11):
    entry = sessions.remember(
        url="https://x.example/1", chat_id=5, user_id=7, username="tester",
        info=info, message_id=message_id, photo=False,
    )
    sessions._sessions.pop(
        sessions.key(5, message_id, sessions.url_hash("https://x.example/1"))
    )
    sessions._sessions[sessions.key(5, message_id, HASH)] = entry
    return entry


def _query(data, message_id=11):
    query = MagicMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat.id = 5
    query.message.message_id = message_id
    query.message.photo = None
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


def _context(bot=None):
    bot = bot or MagicMock(
        send_message=AsyncMock(return_value=MagicMock(message_id=77)),
        edit_message_reply_markup=AsyncMock(),
        delete_message=AsyncMock(),
    )
    context = MagicMock()
    context.bot = bot
    return context


@pytest.fixture()
def spawned(monkeypatch):
    jobs = []

    def _capture(coro):
        frame = coro.cr_frame
        jobs.append(dict(frame.f_locals) if frame is not None else {})
        coro.close()

    monkeypatch.setattr(callbacks.asyncio, "create_task", _capture)
    return jobs


@pytest.mark.asyncio
async def test_tapping_2160p_av1_asks_before_downloading(spawned):
    card = _card(_engine_info())
    update, query = _query(f"dl:11:{HASH}:399")
    context = _context()

    await callbacks.on_format_button(update, context)

    assert spawned == [], "no job may start before the user answers"
    assert card["job_active"] is False
    question = _plain(context.bot.send_message.await_args.kwargs["text"])
    assert "2160p" in question and "AV1" in question
    assert "as a file, or convert it?" in question
    markup = context.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert _labels(markup) == ["Send as file", "Convert (~4 min)"]


@pytest.mark.asyncio
async def test_tapping_1080p_h264_downloads_immediately(spawned):
    _card(_engine_info())
    update, query = _query(f"dl:11:{HASH}:299")

    await callbacks.on_format_button(update, _context())

    assert len(spawned) == 1
    assert spawned[0]["delivery"] == policy.SEND_VIDEO
    assert spawned[0]["format_id"] == "299"


# ---------------------------------------------------------------------------
# 3. The two answers, all the way to the upload
# ---------------------------------------------------------------------------

def _status(**extra):
    return {
        "status": "done",
        "filename": "Artemis II to the Moon [2160p].mp4",
        "file_size_bytes": 900 * MB,
        "duration": 635,
        "height": 2160,
        "width": 3840,
        **extra,
    }


async def _finish(monkeypatch, tmp_path, info, status, *, delivery, poll=None):
    """Run the download flow for one answered question and return the bot."""
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    artifact = tmp_path / status["filename"]
    artifact.write_bytes(b"x" * 4096)

    started = AsyncMock(return_value={"job_id": "job1", "status": "downloading"})
    monkeypatch.setattr(download_flow, "start_download", started)
    monkeypatch.setattr(
        download_flow, "poll_status",
        AsyncMock(side_effect=poll) if poll is not None else AsyncMock(return_value=status),
    )
    monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_download_done", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_progress", AsyncMock())

    bot = RecordingBot()
    await download_flow.run_download(
        context=MagicMock(bot=bot),
        chat_id=5,
        status_message_id=11,
        url="https://x.example/1",
        info=info,
        fmt="video",
        format_id="399",
        height=2160,
        user_id=7,
        username="tester",
        started_at=0.0,
        lang="en",
        delivery=delivery,
    )
    return bot, started


@pytest.mark.asyncio
async def test_send_as_file_uploads_a_document(monkeypatch, tmp_path):
    info = _engine_info()
    bot, started = await _finish(
        monkeypatch, tmp_path, info,
        _status(vcodec="vp9", send_as_document=True, converted=False),
        delivery=policy.SEND_FILE,
    )

    call = started.await_args
    assert call is not None and call.kwargs["transcode"] == "never"
    assert bot.of("send_document"), "a VP9 file must arrive as a document"
    assert not bot.of("send_video")
    caption = _plain(bot.of("send_document")[0]["caption"])
    assert "Artemis II to the Moon" in caption
    assert "2160p" in caption


@pytest.mark.asyncio
async def test_convert_shows_the_stage_then_uploads_a_video(monkeypatch, tmp_path):
    info = _engine_info()
    converting = {
        "status": "downloading",
        "stage": "converting",
        "progress": {"percent": 55.0, "stage": "converting"},
    }
    bot, started = await _finish(
        monkeypatch, tmp_path, info,
        _status(vcodec="h264", converted=True, send_as_document=False),
        delivery=policy.CONVERT,
        poll=[converting, _status(vcodec="h264", converted=True)],
    )

    call = started.await_args
    assert call is not None and call.kwargs["transcode"] == "force"
    captions = [
        _plain(kw["text"]) for kw in bot.of("edit_message_text") if kw.get("text")
    ]
    assert any("Converting…" in text for text in captions), captions
    assert bot.of("send_video"), "a converted file is a video again"
    assert not bot.of("send_document")


@pytest.mark.asyncio
async def test_the_stage_never_appears_without_a_conversion(monkeypatch, tmp_path):
    info = _engine_info()
    downloading = {"status": "downloading", "progress": {"percent": 40.0}}
    bot, _ = await _finish(
        monkeypatch, tmp_path, info,
        _status(vcodec="h264"),
        delivery=policy.SEND_VIDEO,
        poll=[downloading, _status(vcodec="h264")],
    )
    for kw in bot.of("edit_message_text"):
        assert "Converting" not in _plain(kw.get("text") or "")


# ---------------------------------------------------------------------------
# 4. auto: converts inside the ceiling, asks above it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_converts_a_small_file_without_asking(spawned):
    info = _engine_info(mode="auto", max_mb=500)   # 1440p VP9 is 451 MB
    _card(info)
    update, query = _query(f"dl:11:{HASH}:271")

    await callbacks.on_format_button(update, _context())

    assert len(spawned) == 1
    assert spawned[0]["delivery"] == policy.CONVERT


@pytest.mark.asyncio
async def test_auto_still_asks_above_the_ceiling(spawned):
    info = _engine_info(mode="auto", max_mb=100)   # 2160p AV1 is 900 MB
    card = _card(info)
    update, query = _query(f"dl:11:{HASH}:399")
    context = _context()

    await callbacks.on_format_button(update, context)

    assert spawned == [], "a file over the ceiling must not convert silently"
    assert card["job_active"] is False
    markup = context.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert _labels(markup) == ["Send as file", "Convert (~4 min)"]


@pytest.mark.asyncio
async def test_off_sends_an_incompatible_file_as_a_document(monkeypatch, tmp_path):
    """The whole point of ``off``: a document, never an encoder run."""
    info = _engine_info(mode="off")
    bot, started = await _finish(
        monkeypatch, tmp_path, info,
        _status(vcodec="av1", send_as_document=True, converted=False),
        delivery=policy.SEND_FILE,
    )
    call = started.await_args
    assert call is not None and call.kwargs["transcode"] == "never"
    assert bot.of("send_document")
