"""The bot's half of the no-transcode policy: labels, the question, delivery.

Three owner-visible rules are pinned here:

* **H.264 is preferred, never hidden elsewhere.** A card with ``1080p avc1`` and
  ``2160p av1`` shows both, one labelled ``AV1 · 2160p · 2.3GB``, and a tap on
  the unmarked one leads to H.264.
* **nothing converts by itself.** With ``TRANSCODE_MODE=off`` (the default) a
  codec Telegram may not play becomes a document or a question, never an
  encoder run; ``auto`` converts only inside ``TRANSCODE_MAX_MB``; ``always``
  converts. The card shows ``Converting…`` only when a conversion is running.
* **the question costs one tap, not a download.** ``Send as file`` and
  ``Convert (~4 min)`` both start the same job with a different ``transcode``
  request, and both fit in ``callback_data``.
"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import bot_settings  # noqa: E402
import download_card  # noqa: E402
import i18n  # noqa: E402
import progress_card  # noqa: E402
import transcode_policy as policy  # noqa: E402
from handlers import callbacks, config, download_flow, sessions  # noqa: E402

MB = 1024 * 1024
HASH = "abcdef123456"


# ---------------------------------------------------------------------------
# The card the owner sees: 2160p AV1, 1440p VP9, 1080p H.264
# ---------------------------------------------------------------------------

INFO = {
    "title": "Artemis II to the Moon",
    "duration": 635,
    "extractor": "youtube",
    "channel": "NASA",
    "transcode": {"mode": "off", "max_mb": 300},
    "formats": [
        {"id": "399", "label": "2160p", "height": 2160, "button_label": "2160p · 900MB",
         "vcodec": "av01.0.08M.0", "codec": "av1", "codec_label": "AV1",
         "compatible": False, "convertible": True, "size_bytes": 900 * MB,
         "transcode_estimate_seconds": 1800},
        {"id": "271", "label": "1440p", "height": 1440, "button_label": "1440p · 451MB",
         "vcodec": "vp09.00.40.0", "codec": "vp9", "codec_label": "VP9",
         "compatible": False, "convertible": True, "size_bytes": 451 * MB,
         "transcode_estimate_seconds": 240},
        {"id": "299", "label": "1080p", "height": 1080, "button_label": "1080p · 220MB",
         "vcodec": "avc1.64002a", "codec": "h264", "codec_label": "H.264",
         "compatible": True, "convertible": False, "size_bytes": 220 * MB},
    ],
    "audio": {"abr": 129, "size_bytes": 4 * MB},
}


def _plain(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _stage_texts(bot) -> list[str]:
    """Every caption/text the flow wrote onto a card, unescaped."""
    texts: list[str] = []
    for name in ("edit_message_text", "edit_message_caption", "send_message"):
        for kwargs in bot.of(name):
            value = kwargs.get("text") or kwargs.get("caption")
            if value:
                texts.append(_plain(value))
    return texts


def _datas(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _clean_sessions():
    sessions.forget_all()
    yield
    sessions.forget_all()


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

class TestKeyboardLabels:
    def test_a_codec_telegram_may_not_play_is_marked(self):
        assert policy.button_label(INFO["formats"][0]) == "AV1 · 2160p · 900MB"
        assert policy.button_label(INFO["formats"][1]) == "VP9 · 1440p · 451MB"

    def test_a_playable_format_keeps_its_plain_label(self):
        assert policy.button_label(INFO["formats"][2]) == "1080p · 220MB"

    def test_the_mark_comes_from_the_ytdlp_codec_when_the_engine_did_not_say(self):
        entry = {"button_label": "1440p · 451MB", "vcodec": "vp09.00.40.0"}
        assert policy.button_label(entry) == "VP9 · 1440p · 451MB"

    def test_an_unknown_codec_is_not_marked(self):
        entry = {"button_label": "480p", "vcodec": "theora"}
        assert policy.button_label(entry) == "480p"

    def test_h264_and_av1_of_the_same_height_collapse_to_one_button(self):
        """The engine sends one entry per height, H.264 winning."""
        info = {
            "formats": [
                {"id": "vp9", "label": "1080p", "height": 1080,
                 "vcodec": "vp09.00.40.0", "size_bytes": 90 * MB},
                {"id": "avc", "label": "1080p", "height": 1080,
                 "vcodec": "avc1.640028", "size_bytes": 120 * MB},
            ]
        }
        markup = download_card.build_format_keyboard(info, 11, HASH)
        assert "1080p" in _labels(markup)
        assert len([label for label in _labels(markup) if "1080p" in label]) == 1
        assert _datas(markup)[0] == f"dl:11:{HASH}:avc"

    def test_the_keyboard_shows_every_height_and_only_one_per_height(self):
        markup = download_card.build_format_keyboard(INFO, 11, HASH)
        assert _labels(markup)[:3] == [
            "AV1 · 2160p · 900MB",
            "VP9 · 1440p · 451MB",
            "1080p · 220MB",       # the H.264 variant, unmarked
        ]

    def test_every_button_fits_inside_the_callback_limit(self):
        markup = download_card.build_format_keyboard(INFO, 11, HASH)
        for data in _datas(markup):
            assert len(data.encode("utf-8")) <= download_card.CALLBACK_DATA_MAX_BYTES


class TestCardNote:
    def test_the_card_explains_the_av1_vp9_mark_in_english(self):
        caption = _plain(download_card.build_card(INFO, lang="en"))
        assert "AV1/VP9 may not play on iPhone" in caption
        assert "converted to H.264" in caption

    def test_the_card_explains_the_mark_in_russian(self):
        caption = _plain(download_card.build_card(INFO, lang="ru"))
        assert "AV1/VP9 может не проигрываться на iPhone" in caption
        assert "H.264" in caption

    def test_a_card_of_playable_formats_has_no_note(self):
        info = {"title": "T", "formats": [INFO["formats"][2]]}
        caption = _plain(download_card.build_card(info, lang="en"))
        assert "iPhone" not in caption

    def test_the_note_is_the_same_line_in_both_languages(self):
        assert i18n.has("transcode.note", "en")
        assert i18n.has("transcode.note", "ru")
        assert i18n.placeholders("transcode.note", "en") == set()
        assert i18n.placeholders("transcode.note", "ru") == set()


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

class TestDecision:
    def test_off_asks_instead_of_converting(self):
        assert policy.decide("off", INFO["formats"][0]) == policy.ASK
        assert policy.decide("off", INFO["formats"][1]) == policy.ASK

    def test_off_sends_h264_straight_through(self):
        assert policy.decide("off", INFO["formats"][2]) == policy.SEND_VIDEO

    def test_auto_converts_only_inside_the_size_ceiling(self):
        # 1440p VP9 is 451 MB: inside a 500 MB ceiling, outside a 300 MB one.
        assert policy.decide("auto", INFO["formats"][1], max_mb=500) == policy.CONVERT
        assert policy.decide("auto", INFO["formats"][1], max_mb=300) == policy.ASK
        assert policy.decide("auto", INFO["formats"][0], max_mb=300) == policy.ASK

    def test_always_converts(self):
        assert policy.decide("always", INFO["formats"][0]) == policy.CONVERT

    def test_a_missing_or_unknown_mode_is_off(self):
        assert policy.normalize_mode(None) == "off"
        assert policy.normalize_mode("sometimes") == "off"
        assert policy.decide("sometimes", INFO["formats"][0]) == policy.ASK

    def test_hevc_is_sent_as_a_file_because_it_cannot_be_converted(self):
        entry = {"label": "1080p", "vcodec": "hev1.1.6.L93.B0", "compatible": False,
                 "convertible": False}
        assert policy.decide("always", entry) == policy.SEND_FILE

    def test_the_engine_mode_and_ceiling_are_read_from_info(self):
        assert policy.engine_mode(INFO) == "off"
        assert policy.engine_max_mb(INFO) == 300
        assert policy.engine_mode({}) == "off"
        assert policy.engine_max_mb({}) == 300


# ---------------------------------------------------------------------------
# The estimate and its button
# ---------------------------------------------------------------------------

class TestEstimate:
    @pytest.mark.parametrize(
        "seconds,english,russian",
        [
            (40, "~40 s", "~40 с"),
            (240, "~4 min", "~4 мин"),
            (1500, "~25 min", "~25 мин"),
            (100, "~2 min", "~2 мин"),
        ],
    )
    def test_the_eta_is_coarse_and_rounds_minutes_up(self, seconds, english, russian):
        assert policy.format_eta(seconds, "en") == english
        assert policy.format_eta(seconds, "ru") == russian

    def test_no_estimate_means_no_number(self):
        assert policy.format_eta(None) is None
        assert policy.format_eta(0) is None

    def test_the_button_carries_the_estimate(self):
        markup = download_card.build_transcode_keyboard(
            INFO["formats"][1], 11, HASH, lang="en", eta_seconds=240
        )
        assert _labels(markup) == ["Send as file", "Convert (~4 min)"]

    def test_the_button_says_convert_in_russian(self):
        markup = download_card.build_transcode_keyboard(
            INFO["formats"][1], 11, HASH, lang="ru", eta_seconds=240
        )
        assert _labels(markup) == ["Отправить файлом", "Конвертировать (~4 мин)"]

    def test_without_an_estimate_the_button_is_just_convert(self):
        markup = download_card.build_transcode_keyboard(
            {"id": "399", "label": "2160p"}, 11, HASH, lang="en", eta_seconds=None
        )
        assert _labels(markup) == ["Send as file", "Convert"]

    def test_both_answers_fit_the_callback_limit(self):
        markup = download_card.build_transcode_keyboard(
            INFO["formats"][0], 9999999999, HASH, lang="en", eta_seconds=3600
        )
        assert _datas(markup) == [
            f"dl:9999999999:{HASH}:file:399",
            f"dl:9999999999:{HASH}:conv:399",
        ]
        for data in _datas(markup):
            assert len(data.encode("utf-8")) <= download_card.CALLBACK_DATA_MAX_BYTES

    def test_an_unfittable_format_id_falls_back_to_best(self):
        markup = download_card.build_transcode_keyboard(
            {"id": "x" * 60, "label": "2160p"}, 11, HASH, lang="en"
        )
        assert _datas(markup)[1].endswith(":conv:best")

    def test_the_question_names_the_quality_and_the_codec(self):
        caption = _plain(
            download_card.build_transcode_question(INFO, INFO["formats"][0], lang="en")
        )
        assert "2160p" in caption
        assert "AV1" in caption
        assert "as a file, or convert it?" in caption

    def test_the_question_is_translated(self):
        caption = _plain(
            download_card.build_transcode_question(INFO, INFO["formats"][0], lang="ru")
        )
        assert "файлом или конвертировать" in caption


# ---------------------------------------------------------------------------
# The question, routed
# ---------------------------------------------------------------------------

def _card(info=INFO, message_id=11):
    entry = sessions.remember(
        url="https://x.example/1",
        chat_id=5,
        user_id=7,
        username="tester",
        info=info,
        message_id=message_id,
        photo=False,
    )
    sessions._sessions.pop(
        sessions.key(5, message_id, sessions.url_hash("https://x.example/1"))
    )
    sessions._sessions[sessions.key(5, message_id, HASH)] = entry
    return entry


@pytest.fixture(name="card")
def _card_fixture():
    """A live session as the preview card would have recorded it."""
    return _card()


def _query(data, *, chat_id=5, message_id=11, photo=None):
    query = MagicMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat.id = chat_id
    query.message.message_id = message_id
    query.message.photo = photo
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


def _context():
    """A context whose bot can actually post the question (an async send)."""
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=77))
    bot.edit_message_text = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.delete_message = AsyncMock()
    context = MagicMock()
    context.bot = bot
    return context


@pytest.fixture()
def spawned(monkeypatch):
    """Capture the download task instead of running it."""
    jobs = []

    def _capture(coro):
        frame = coro.cr_frame
        jobs.append(dict(frame.f_locals) if frame is not None else {})
        coro.close()

    monkeypatch.setattr(callbacks.asyncio, "create_task", _capture)
    return jobs


class TestAskingInTheChat:
    @pytest.mark.asyncio
    async def test_tapping_av1_asks_instead_of_downloading(self, card, spawned):
        update, query = _query(f"dl:11:{HASH}:399")
        context = _context()
        await callbacks.on_format_button(update, context)

        assert spawned == []
        query.answer.assert_awaited_once()
        # The question replaces the card (a new message), the keyboard goes with it.
        sent = context.bot.send_message.await_args.kwargs
        assert sent["text"].startswith("*Artemis")
        assert "AV1" in _plain(sent["text"])
        # The keyboard goes on once the new card has an id, so the buttons
        # address the message they sit on.
        markup = context.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        assert context.bot.edit_message_reply_markup.await_args.kwargs["message_id"] == 77
        assert _labels(markup) == ["Send as file", "Convert (~30 min)"]
        assert card["job_active"] is False

    @pytest.mark.asyncio
    async def test_the_question_replaces_the_preview_message(self, card, spawned):
        update, query = _query(f"dl:11:{HASH}:399")
        context = _context()
        await callbacks.on_format_button(update, context)
        # One card at a time: the preview is deleted once the question is up.
        context.bot.delete_message.assert_awaited_once()
        assert context.bot.delete_message.await_args.kwargs["message_id"] == 11

    @pytest.mark.asyncio
    async def test_the_session_follows_the_question(self, card, spawned):
        """A tap on the fresh question must not answer "session expired"."""
        update, query = _query(f"dl:11:{HASH}:399")
        await callbacks.on_format_button(update, _context())

        assert card["message_id"] == 77
        assert sessions.get(5, 77, HASH) is card

    @pytest.mark.asyncio
    async def test_send_as_file_starts_the_job_without_conversion(self, card, spawned):
        update, query = _query(f"dl:11:{HASH}:file:399")
        await callbacks.on_format_button(update, MagicMock())

        assert len(spawned) == 1
        assert spawned[0]["delivery"] == policy.SEND_FILE
        assert card["job_active"] is True
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    @pytest.mark.asyncio
    async def test_convert_starts_the_job_with_a_forced_conversion(self, card, spawned):
        update, query = _query(f"dl:11:{HASH}:conv:399")
        await callbacks.on_format_button(update, MagicMock())

        assert len(spawned) == 1
        assert spawned[0]["delivery"] == policy.CONVERT

    @pytest.mark.asyncio
    async def test_a_tap_on_h264_still_starts_at_once(self, card, spawned):
        update, query = _query(f"dl:11:{HASH}:299")
        await callbacks.on_format_button(update, MagicMock())

        assert len(spawned) == 1
        assert spawned[0]["delivery"] == policy.SEND_VIDEO

    @pytest.mark.asyncio
    async def test_a_second_answer_while_a_job_runs_is_refused(self, card, spawned):
        card["job_active"] = True
        update, query = _query(f"dl:11:{HASH}:conv:399")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned == []
        assert query.answer.await_args.args == ("Already downloading — wait for the file.",)

    @pytest.mark.asyncio
    async def test_an_unknown_format_id_on_the_question_is_forwarded(self, card, spawned):
        update, query = _query(f"dl:11:{HASH}:file:999")
        await callbacks.on_format_button(update, MagicMock())

        assert spawned[0]["format_id"] == "999"


# ---------------------------------------------------------------------------
# The plan: what the engine is asked for
# ---------------------------------------------------------------------------

class TestPlanChoice:
    def test_off_asks_for_an_incompatible_format(self):
        assert download_flow.plan_choice(INFO, "399").ask is True
        assert download_flow.plan_choice(INFO, "399").delivery == policy.ASK

    def test_an_h264_format_goes_straight_through(self):
        choice = download_flow.plan_choice(INFO, "299")
        assert (choice.ask, choice.delivery) == (False, policy.SEND_VIDEO)
        assert choice.transcode == "never"

    def test_auto_inside_the_ceiling_converts_without_asking(self):
        info = dict(INFO, transcode={"mode": "auto", "max_mb": 500})
        choice = download_flow.plan_choice(info, "271")
        assert (choice.ask, choice.delivery) == (False, policy.CONVERT)
        assert choice.transcode == "force"

    def test_auto_over_the_ceiling_still_asks(self):
        info = dict(INFO, transcode={"mode": "auto", "max_mb": 100})
        choice = download_flow.plan_choice(info, "271")
        assert choice.ask is True

    def test_always_converts_without_asking(self):
        info = dict(INFO, transcode={"mode": "always", "max_mb": 300})
        choice = download_flow.plan_choice(info, "399")
        assert (choice.ask, choice.delivery) == (False, policy.CONVERT)

    def test_best_picks_the_highest_sendable_format(self):
        choice = download_flow.plan_choice(INFO, "best")
        assert choice.format_id == "399"      # 2160p is the highest that fits
        assert choice.ask is True

    def test_best_skips_a_format_over_the_send_limit(self):
        info = dict(
            INFO,
            formats=[
                dict(INFO["formats"][0], size_bytes=2400 * MB),
                INFO["formats"][1],
                INFO["formats"][2],
            ],
        )
        # 2400 MB is above the 2000 MB send limit, so 1440p is the best offer.
        choice = download_flow.plan_choice(info, "best")
        assert choice.format_id == "271"
        assert choice.skipped == ("1440p", "2160p")

    def test_mp3_is_never_asked_about(self):
        choice = download_flow.plan_choice(INFO, "mp3")
        assert (choice.fmt, choice.ask, choice.delivery) == ("audio", False, policy.SEND_VIDEO)

    def test_hevc_is_planned_as_a_file(self):
        info = {
            "transcode": {"mode": "always", "max_mb": 300},
            "formats": [{"id": "h", "label": "1080p", "height": 1080,
                         "vcodec": "hev1.1.6.L93.B0", "compatible": False,
                         "convertible": False, "size_bytes": 100 * MB}],
            "audio": {"abr": 128},
        }
        choice = download_flow.plan_choice(info, "h")
        assert choice.delivery == policy.SEND_FILE
        assert choice.ask is False


# ---------------------------------------------------------------------------
# Delivery: a document, a conversion, a video
# ---------------------------------------------------------------------------

class RecordingBot:
    """A fake bot recording uploads and stage edits."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 100

    async def _record(self, name, kwargs, returns_message=False):
        self.calls.append((name, kwargs))
        if returns_message:
            self._next_id += 1
            return MagicMock(message_id=self._next_id)
        return True

    async def send_message(self, **kwargs):
        return await self._record("send_message", kwargs, True)

    async def send_photo(self, **kwargs):
        return await self._record("send_photo", kwargs, True)

    async def send_video(self, **kwargs):
        return await self._record("send_video", kwargs, True)

    async def send_audio(self, **kwargs):
        return await self._record("send_audio", kwargs, True)

    async def send_document(self, **kwargs):
        return await self._record("send_document", kwargs, True)

    async def delete_message(self, **kwargs):
        return await self._record("delete_message", kwargs)

    async def edit_message_text(self, **kwargs):
        return await self._record("edit_message_text", kwargs)

    async def edit_message_caption(self, **kwargs):
        return await self._record("edit_message_caption", kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        return await self._record("edit_message_reply_markup", kwargs)

    def names(self):
        return [name for name, _ in self.calls]

    def of(self, name):
        return [kwargs for called, kwargs in self.calls if called == name]


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    real_sleep = download_flow.asyncio.sleep

    async def _fast(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(download_flow.asyncio, "sleep", _fast)


@pytest.fixture()
def media_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    return tmp_path


def _status(name="job.mp4", **extra):
    return {"status": "done", "filename": name, "file_size_bytes": 2048,
            "duration": 635, "height": 2160, "width": 3840, **extra}


async def _run(monkeypatch, bot, info, status, *, delivery, format_id="399",
               height=2160, poll=None):
    target = Path(config.DOWNLOADS_PATH) / status["filename"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * 2048)
    started = AsyncMock(return_value={"job_id": "job1", "status": "downloading"})
    monkeypatch.setattr(download_flow, "start_download", started)
    monkeypatch.setattr(
        download_flow,
        "poll_status",
        AsyncMock(side_effect=poll) if poll is not None else AsyncMock(return_value=status),
    )
    monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_download_done", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_progress", AsyncMock())

    context = MagicMock()
    context.bot = bot
    await download_flow.run_download(
        context=context,
        chat_id=1,
        status_message_id=2,
        url="https://youtube.com/watch?v=x",
        info=info,
        fmt="video",
        format_id=format_id,
        height=height,
        user_id=3,
        username="tester",
        started_at=0.0,
        lang="en",
        delivery=delivery,
    )
    return started


class TestDelivery:
    @pytest.mark.asyncio
    async def test_send_as_file_uploads_a_document_with_the_same_caption(
        self, media_file, monkeypatch
    ):
        bot = RecordingBot()
        await _run(monkeypatch, bot, INFO, _status(), delivery=policy.SEND_FILE)

        assert "send_video" not in bot.names()
        documents = bot.of("send_document")
        assert len(documents) == 1
        caption = _plain(documents[0]["caption"])
        # Title, quality, size and the reason — the same information as a video.
        assert "Artemis II to the Moon" in caption
        assert "2160p" in caption
        assert "2.0KB" in caption
        assert "Sent as a file" in caption

    @pytest.mark.asyncio
    async def test_off_with_an_incompatible_codec_reports_a_document(
        self, media_file, monkeypatch
    ):
        """The engine's own verdict (``send_as_document``) is honoured too."""
        bot = RecordingBot()
        status = _status(vcodec="vp9", send_as_document=True, converted=False)
        await _run(monkeypatch, bot, INFO, status, delivery=policy.ASK)

        assert bot.of("send_document"), "a VP9 file must not go out as a video"
        assert not bot.of("send_video")

    @pytest.mark.asyncio
    async def test_a_converted_file_goes_out_as_a_video(
        self, media_file, monkeypatch
    ):
        bot = RecordingBot()
        status = _status(vcodec="h264", converted=True, send_as_document=False)
        await _run(monkeypatch, bot, INFO, status, delivery=policy.CONVERT)

        assert bot.of("send_video")
        assert not bot.of("send_document")

    @pytest.mark.asyncio
    async def test_h264_is_a_video(self, media_file, monkeypatch):
        bot = RecordingBot()
        await _run(monkeypatch, bot, INFO, _status(vcodec="h264"),
                   delivery=policy.SEND_VIDEO)
        assert bot.of("send_video")

    @pytest.mark.asyncio
    async def test_the_job_asks_the_engine_to_convert_only_when_told(
        self, media_file, monkeypatch
    ):
        bot = RecordingBot()
        started = await _run(monkeypatch, bot, INFO, _status(), delivery=policy.CONVERT)
        call = started.await_args
        assert call is not None and call.kwargs["transcode"] == "force"

    @pytest.mark.asyncio
    async def test_send_as_file_tells_the_engine_never(self, media_file, monkeypatch):
        bot = RecordingBot()
        started = await _run(monkeypatch, bot, INFO, _status(), delivery=policy.SEND_FILE)
        call = started.await_args
        assert call is not None and call.kwargs["transcode"] == "never"

    @pytest.mark.asyncio
    async def test_an_mp3_is_never_a_document(self, media_file, monkeypatch):
        """The document rule is about video codecs; an MP3 is always audio."""
        job = download_flow.Job(
            url="u", info=dict(INFO), fmt="audio", format_id=None, height=None,
            user_id=1, username="u", chat_id=1, status_message_id=2,
            started_at=0.0, delivery=policy.SEND_FILE,
        )
        assert download_flow._goes_out_as_document(job, _status(vcodec="vp9")) is False


# ---------------------------------------------------------------------------
# The Converting… stage
# ---------------------------------------------------------------------------

class TestConvertingStage:
    def test_the_stage_exists_in_both_languages(self):
        assert i18n.t("stage.converting", "en") == "Converting…"
        assert i18n.t("stage.converting", "ru") == "Конвертирую…"

    def test_the_caption_shows_converting_with_a_bar(self):
        caption = _plain(
            progress_card.build_progress_caption(
                INFO, {"percent": 42.0, "stage": "converting"},
                stage="converting", quality="2160p", lang="en",
            )
        )
        assert "Converting…" in caption
        # The bar is drawn from the same percentage ffmpeg reports and stays 12
        # cells wide; whether the number sits next to it is a template choice.
        assert progress_card.progress_bar(42.0, show_percent=False) in caption

    def test_the_percentage_is_shown_when_the_template_asks_for_it(
        self, monkeypatch
    ):
        """The panel's own text decides: ``@bar @percent`` prints the number."""
        base = bot_settings.defaults()
        monkeypatch.setattr(
            bot_settings, "cached",
            lambda: bot_settings.Settings(
                languages=base.languages,
                default_language=base.default_language,
                source="panel",
                templates={"progress": {"en": "*@title*\n@stage\n@bar @percent"}},
            ),
        )
        caption = _plain(
            progress_card.build_progress_caption(
                INFO, {"percent": 42.0}, stage="converting", lang="en",
            )
        )
        assert "Converting…" in caption
        assert "42%" in caption

    def test_the_russian_caption_says_konvertiruyu(self):
        caption = _plain(
            progress_card.build_progress_caption(
                INFO, {"percent": 10.0}, stage="converting", lang="ru"
            )
        )
        assert "Конвертирую…" in caption

    @pytest.mark.asyncio
    async def test_the_engine_stage_drives_the_card(self, media_file, monkeypatch):
        """A poll mid-conversion rewrites the caption to ``Converting…``."""
        bot = RecordingBot()
        converting = {
            "status": "downloading",
            "stage": "converting",
            "progress": {"percent": 55.0, "stage": "converting"},
        }
        await _run(
            monkeypatch, bot, INFO, _status(),
            delivery=policy.CONVERT,
            poll=[converting, _status(vcodec="h264", converted=True)],
        )
        edits = _stage_texts(bot)
        assert any("Converting…" in text for text in edits)
        assert not any("Downloading…" in text for text in edits)

    @pytest.mark.asyncio
    async def test_without_a_conversion_the_stage_never_appears(
        self, media_file, monkeypatch
    ):
        bot = RecordingBot()
        download = {
            "status": "downloading",
            "progress": {"percent": 40.0, "downloaded_bytes": 100},
        }
        await _run(
            monkeypatch, bot, INFO, _status(vcodec="h264"),
            delivery=policy.SEND_VIDEO,
            poll=[download, _status(vcodec="h264")],
        )
        for text in _stage_texts(bot):
            assert "Converting" not in text


# ---------------------------------------------------------------------------
# Files: names and callbacks
# ---------------------------------------------------------------------------

class TestConversionCallbacks:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (f"dl:11:{HASH}:file:399", (11, HASH, "file", "399")),
            (f"dl:11:{HASH}:conv:399", (11, HASH, "conv", "399")),
            (f"fmt:11:{HASH}:conv:best", (11, HASH, "conv", "best")),
        ],
    )
    def test_the_question_shape_parses(self, data, expected):
        assert sessions.parse_conversion_data(data) == expected

    @pytest.mark.parametrize(
        "data",
        [
            f"dl:11:{HASH}:137",
            f"dl:11:{HASH}:mp3",
            f"dl:11:{HASH}:file",
            f"dl:11:{HASH}:nope:399",
            f"dl:notanint:{HASH}:conv:399",
            "",
        ],
    )
    def test_a_plain_button_is_not_a_question(self, data):
        assert sessions.parse_conversion_data(data) is None

    def test_a_plain_button_still_parses(self):
        assert download_card.build_transcode_keyboard({"label": "720p"}, 1, "h")
        # Four fields is a format button; five is the question.
        assert sessions.parse_callback_data(f"dl:11:{HASH}:137") == (11, HASH, "137")
        assert sessions.parse_conversion_data(f"dl:11:{HASH}:conv:399") == (
            11, HASH, "conv", "399"
        )
