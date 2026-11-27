"""The Telegram send limit: what the bot offers, and what it explains.

Telegram refuses a file over its own ceiling (2 GB on the self-hosted Bot API,
20 MB on the public one), so a format the bot cannot deliver must not be in the
keyboard at all: the user would tap it, wait for the download and only then read
that the file is too big.

Three behaviours are pinned here:

* filtering — a **known** size over ``MAX_SEND_MB`` is hidden, an unknown size
  stays (the engine's ``--max-filesize`` still refuses it, but the choice
  remains), and the filter runs before the button cap so a hidden format never
  takes a slot from a sendable one;
* the MP3 estimate — the audio summary's size, or bitrate × duration when the
  engine did not report one;
* what is left when nothing fits — a sentence with the limit in it, in the
  language of the chat, instead of a keyboard.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import download_card  # noqa: E402
import send_limit  # noqa: E402
from handlers import config, download_flow, sessions, texts, url_flow  # noqa: E402

# 2.3 GB / 696 MB / 301 MB — the capture the owner tested with.
BIG_INFO = {
    "title": "Big video",
    "duration": 600,
    "extractor": "youtube",
    "channel": "NASA",
    "formats": [
        {"id": "313", "label": "2160p", "height": 2160,
         "size_bytes": 2_469_606_195, "button_label": "2160p · 2.3GB"},
        {"id": "308", "label": "1440p", "height": 1440,
         "size_bytes": 729_808_896, "button_label": "1440p · 696MB"},
        {"id": "137", "label": "1080p", "height": 1080,
         "size_bytes": 315_621_376, "button_label": "1080p · 301MB"},
    ],
    "audio": {"abr": 129, "size_bytes": 9_675_000},
}


def _plain(text: object) -> str:
    """Drop MarkdownV2 backslashes so assertions read like the message."""
    text = str(text or "")
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
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.fixture(autouse=True)
def _limit(monkeypatch):
    """2000 MB, whatever the machine running the tests has configured."""
    monkeypatch.setattr(send_limit, "MAX_SEND_MB", 2000)


@pytest.fixture(autouse=True)
def _clean_sessions():
    sessions.forget_all()
    yield
    sessions.forget_all()


# ---------------------------------------------------------------------------
# The knob
# ---------------------------------------------------------------------------

class TestTheCeiling:
    def test_default_is_the_self_hosted_bot_api(self, monkeypatch):
        monkeypatch.delenv("MAX_SEND_MB", raising=False)
        assert send_limit._read_mb("MAX_SEND_MB", 2000) == 2000
        assert send_limit.DEFAULT_MAX_SEND_MB == 2000

    def test_environment_overrides_it(self, monkeypatch):
        monkeypatch.setenv("MAX_SEND_MB", "20")
        assert send_limit._read_mb("MAX_SEND_MB", 2000) == 20

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
    def test_a_bad_value_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("MAX_SEND_MB", raw)
        assert send_limit._read_mb("MAX_SEND_MB", 2000) == 2000

    def test_bytes_are_read_at_call_time(self, monkeypatch):
        monkeypatch.setattr(send_limit, "MAX_SEND_MB", 20)
        assert send_limit.max_send_bytes() == 20 * 1024 * 1024
        monkeypatch.setattr(send_limit, "MAX_SEND_MB", 2000)
        assert send_limit.max_send_bytes() == 2000 * 1024 * 1024

    def test_the_boundary_is_exact(self, monkeypatch):
        monkeypatch.setattr(send_limit, "MAX_SEND_MB", 2000)
        limit = 2000 * 1024 * 1024
        assert send_limit.fits(limit)
        assert not send_limit.fits(limit + 1)
        assert send_limit.fits(None), "an unknown size is never hidden"
        assert not send_limit.fits(limit + 1, limit_bytes=limit)

    def test_known_size_accepts_the_engines_names(self):
        assert send_limit.known_size({"size_bytes": 5}) == 5
        assert send_limit.known_size({"filesize": 7}) == 7
        assert send_limit.known_size({"filesize_approx": 9}) == 9
        assert send_limit.known_size({"size_bytes": 0}) is None
        assert send_limit.known_size({"size_bytes": "NA"}) is None
        assert send_limit.known_size(None) is None


class TestAudioEstimate:
    def test_reported_size_wins(self):
        assert send_limit.audio_size(BIG_INFO) == 9_675_000

    def test_bitrate_and_duration_are_multiplied(self):
        info = {"duration": 100, "audio": {"abr": 128}}
        # 128 kbit/s × 100 s / 8 = 1 600 000 bytes
        assert send_limit.audio_size(info) == 1_600_000

    def test_no_data_means_no_estimate(self):
        assert send_limit.audio_size({"audio": {"abr": 128}}) is None
        assert send_limit.audio_size({"duration": 100, "audio": {}}) is None
        assert send_limit.audio_size({}) is None


# ---------------------------------------------------------------------------
# The offer
# ---------------------------------------------------------------------------

class TestFormatOffer:
    def test_oversized_formats_are_not_offered(self):
        """2160p (2.3 GB) is hidden; 1440p, 1080p and MP3 remain."""
        markup = download_card.build_format_keyboard(BIG_INFO, 11, "abcdef123456")

        labels = _labels(markup)
        assert labels == ["1440p · 696MB", "1080p · 301MB", "MP3 · 9.2MB"]
        assert not any("2160" in label for label in labels)
        assert not any(data.endswith(":313") for data in _callbacks(markup))

    def test_hidden_formats_do_not_take_button_slots(self, monkeypatch):
        """Twelve 4K formats over the limit, one 720p under it: one button."""
        huge = 3 * 1024 ** 3
        info = {
            "formats": [
                {"id": f"big{i}", "label": f"{2000 + i}p", "height": 2000 + i,
                 "size_bytes": huge}
                for i in range(card_max())
            ]
            + [{"id": "22", "label": "720p", "height": 720, "size_bytes": 10}],
            "audio": {"size_bytes": 10},
        }
        markup = download_card.build_format_keyboard(info, 11, "abcdef123456")

        labels = _labels(markup)
        assert "720p" in labels
        assert download_card.t("card.best_quality") not in labels
        assert not any("big" in data for data in _callbacks(markup))

    def test_unknown_size_is_kept(self):
        info = {
            "formats": [
                {"id": "313", "label": "2160p", "height": 2160,
                 "size_bytes": 2_469_606_195},
                {"id": "18", "label": "360p", "height": 360},
            ],
            "audio": {},
        }
        offer = download_card.format_offer(info)

        assert [fmt["id"] for fmt in offer.formats] == ["18"]
        assert offer.hidden_videos == 1

    def test_mp3_is_hidden_when_the_estimate_is_over_the_limit(self):
        info = {
            "duration": 7200,
            "formats": [{"id": "137", "label": "1080p", "height": 1080}],
            "audio": {"abr": 320},
        }
        # 320 kbit/s × 7200 s = 288 000 000 bytes — well inside 2 GB; raise the
        # duration and the same audio no longer fits.
        assert download_card.format_offer(info).mp3 is not None
        long_info = dict(info, duration=72_000)
        assert download_card.format_offer(long_info).mp3 is None

    def test_nothing_sendable_means_no_keyboard(self):
        info = {
            "formats": [{"id": "313", "label": "2160p", "height": 2160,
                         "size_bytes": 2_469_606_195}],
            "audio": {"size_bytes": 3 * 1024 ** 3},
        }
        assert download_card.build_format_keyboard(info, 11, "abcdef123456") is None

    def test_an_mp3_only_link_still_gets_its_button(self):
        """No video formats at all is not "too big" — the MP3 button is the card."""
        markup = download_card.build_format_keyboard({"formats": []}, 11, "abcdef123456")
        assert _labels(markup) == ["MP3"]

    def test_the_notice_key_names_the_reason(self):
        all_hidden_mp3_fits = {
            "formats": [{"id": "313", "label": "2160p", "height": 2160,
                         "size_bytes": 2_469_606_195}],
            "audio": {"size_bytes": 10},
        }
        assert download_card.limit_notice_key(all_hidden_mp3_fits) == "limit.all_too_big"

        nothing_fits = dict(all_hidden_mp3_fits, audio={"size_bytes": 3 * 1024 ** 3})
        assert download_card.limit_notice_key(nothing_fits) == "limit.video_too_big"

        audio_only = {"formats": [], "audio": {"size_bytes": 3 * 1024 ** 3}}
        assert download_card.limit_notice_key(audio_only) == "limit.audio_too_big"


def card_max() -> int:
    return download_card.MAX_FORMAT_BUTTONS + 2


# ---------------------------------------------------------------------------
# The wording, in both languages
# ---------------------------------------------------------------------------

class TestWording:
    def test_the_limit_is_readable_in_both_languages(self):
        assert texts.send_limit_label("en") == "2000 MB"
        assert texts.send_limit_label("ru") == "2000 МБ"

    def test_all_too_big(self):
        assert _plain(texts.limit_message("limit.all_too_big", "en")) == (
            "All available formats are over the send limit (2000 MB). "
            "Try another link or MP3."
        )
        assert _plain(texts.limit_message("limit.all_too_big", "ru")) == (
            "Все доступные форматы больше лимита отправки (2000 МБ). "
            "Попробуйте другую ссылку или MP3."
        )

    def test_video_too_big(self):
        assert _plain(texts.limit_message("limit.video_too_big", "en")) == (
            "This video is too large to send (limit 2000 MB)."
        )
        assert _plain(texts.limit_message("limit.video_too_big", "ru")) == (
            "Это видео слишком большое для отправки (лимит 2000 МБ)."
        )

    def test_the_quality_note(self):
        assert _plain(texts.limit_note("1440p", "2160p", "en")) == (
            "Selected 1440p: 2160p does not fit the send limit"
        )
        assert _plain(texts.limit_note("1440p", "2160p", "ru")) == (
            "Выбрано 1440p: 2160p не влезает в лимит"
        )


# ---------------------------------------------------------------------------
# The choice behind a tap or a command
# ---------------------------------------------------------------------------

class TestPlanChoice:
    def test_best_is_pinned_to_the_highest_sendable_format(self):
        choice = download_flow.plan_choice(BIG_INFO, "best")

        assert (choice.fmt, choice.format_id, choice.height) == ("video", "308", 1440)
        assert choice.skipped == ("1440p", "2160p")
        assert choice.refusal is None

    def test_best_without_a_skipped_format_has_no_note(self):
        info = {
            "formats": [
                {"id": "137", "label": "1080p", "height": 1080, "size_bytes": 10},
                {"id": "22", "label": "720p", "height": 720, "size_bytes": 5},
            ],
            "audio": {"size_bytes": 10},
        }
        choice = download_flow.plan_choice(info, "best")

        assert choice.format_id == "137"
        assert choice.skipped is None

    def test_an_explicit_format_is_forwarded_untouched(self):
        choice = download_flow.plan_choice(BIG_INFO, "137")
        assert (choice.fmt, choice.format_id, choice.height) == ("video", "137", 1080)
        assert choice.skipped is None

    def test_nothing_fits_is_refused_with_a_reason(self):
        info = dict(
            BIG_INFO,
            formats=BIG_INFO["formats"][:1],
            audio={"size_bytes": 3 * 1024 ** 3},
        )
        choice = download_flow.plan_choice(info, "best")
        assert choice.refusal == "limit.video_too_big"

        mp3_only = dict(BIG_INFO, formats=[], audio={"size_bytes": 10})
        # No video at all: an MP3-only link is not "too big".
        assert download_flow.plan_choice(mp3_only, "best").refusal == "limit.all_too_big"

    def test_an_oversized_mp3_is_refused_before_the_job(self):
        info = dict(BIG_INFO, audio={"size_bytes": 3 * 1024 ** 3})
        assert download_flow.plan_choice(info, "mp3").refusal == "limit.video_too_big"
        assert download_flow.plan_choice(BIG_INFO, "mp3").refusal is None


# ---------------------------------------------------------------------------
# The preview card the owner sees
# ---------------------------------------------------------------------------

def _fake_update(text: str):
    """An update whose "Fetching info..." message is id 11, plus a recorder bot."""
    from tests.test_handlers_url_flow import fake_update

    return fake_update(text)


class TestPreviewCard:
    @pytest.fixture(autouse=True)
    def _no_dns(self, monkeypatch):
        """Validation touches DNS; only the card is under test here."""
        monkeypatch.setattr(url_flow, "validate_url", lambda url: url)

    @pytest.mark.asyncio
    async def test_the_keyboard_offers_only_formats_that_fit(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=dict(BIG_INFO)))
        update, _ = _fake_update("https://x.example/1")

        await url_flow.url_handler(update, MagicMock())

        bot = update.get_bot()
        markup = bot.of("edit_message_text")[0]["reply_markup"]
        labels = _labels(markup)
        assert labels == ["1440p · 696MB", "1080p · 301MB", "MP3 · 9.2MB"]
        assert not any("2160" in label for label in labels)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lang, expected",
        [
            (
                "en",
                (
                    "All available formats are over the send limit (2000 MB). "
                    "Try another link or MP3."
                ),
            ),
            (
                "ru",
                (
                    "Все доступные форматы больше лимита отправки (2000 МБ). "
                    "Попробуйте другую ссылку или MP3."
                ),
            ),
        ],
    )
    async def test_nothing_left_is_explained_instead_of_a_keyboard(
        self, monkeypatch, lang, expected
    ):
        info = dict(BIG_INFO, formats=BIG_INFO["formats"][:1])  # only 2160p
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=info))
        update, _ = _fake_update("https://x.example/1")

        await url_flow.send_preview(update, "https://x.example/1", lang=lang)

        bot = update.get_bot()
        text = _plain(bot.written_texts()[-1])
        assert expected in text
        assert "Big video" in text  # the title stays, so links stay tellable apart
        assert bot.of("edit_message_reply_markup") == []
        assert sessions.live_count() == 0, "no dead card is remembered"

    @pytest.mark.asyncio
    async def test_an_audio_only_link_that_is_too_big_says_audio(self, monkeypatch):
        info = {"title": "Huge audio", "formats": [],
                "audio": {"size_bytes": 3 * 1024 ** 3}}
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=info))
        update, _ = _fake_update("https://x.example/1")

        await url_flow.url_handler(update, MagicMock())

        text = _plain(update.get_bot().written_texts()[-1])
        assert "This audio is too large to send (limit 2000 MB)." in text


# ---------------------------------------------------------------------------
# /best end to end
# ---------------------------------------------------------------------------

class RecordingBot:
    """Just enough Telegram: the sends a finished download makes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 100

    def _record(self, name: str, kwargs: dict):
        self.calls.append((name, kwargs))
        self._next_id += 1
        return MagicMock(message_id=self._next_id)

    async def send_message(self, **kwargs):
        return self._record("send_message", kwargs)

    async def send_video(self, **kwargs):
        return self._record("send_video", kwargs)

    async def send_audio(self, **kwargs):
        return self._record("send_audio", kwargs)

    async def send_document(self, **kwargs):
        return self._record("send_document", kwargs)

    async def delete_message(self, **kwargs):
        return self._record("delete_message", kwargs)

    async def edit_message_text(self, **kwargs):
        return self._record("edit_message_text", kwargs)

    async def edit_message_caption(self, **kwargs):
        return self._record("edit_message_caption", kwargs)

    def of(self, name: str) -> list[dict]:
        return [kwargs for called, kwargs in self.calls if called == name]


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """No 2 s poll delay, no dashboard, and a downloads directory of our own."""
    real_sleep = download_flow.asyncio.sleep

    async def _fast(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(download_flow.asyncio, "sleep", _fast)
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    for name in ("send_download_start", "send_download_done",
                 "send_download_error", "send_progress"):
        monkeypatch.setattr(download_flow.event_client, name, AsyncMock())
    return tmp_path


def _status(bot):
    status = MagicMock()
    status.message_id = 11
    status.chat_id = 5
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()

    message = MagicMock()
    message.message_id = 11
    message.chat_id = 5
    message.reply_text = AsyncMock(return_value=status)

    user = MagicMock()
    user.id = 7
    user.username = "tester"

    update = MagicMock()
    update.message = message
    update.effective_user = user
    update.effective_chat = MagicMock(id=5)
    update.get_bot = MagicMock(return_value=bot)
    update.bot = bot

    context = MagicMock()
    context.bot = bot
    return update, context, status


class TestBestCommand:
    @pytest.mark.asyncio
    async def test_best_picks_the_highest_format_that_fits(self, engine, monkeypatch):
        monkeypatch.setattr(download_flow, "get_info", AsyncMock(return_value=dict(BIG_INFO)))
        started: list[tuple[tuple, dict]] = []

        async def _start(*args, **kwargs):
            started.append((args, kwargs))
            return {"job_id": "j1", "status": "downloading"}

        monkeypatch.setattr(download_flow, "start_download", _start)
        monkeypatch.setattr(
            download_flow, "poll_status",
            AsyncMock(return_value={
                "status": "done", "filename": "job.mp4", "file_size_bytes": 2048,
                "duration": 600, "height": 1440, "width": 2560,
            }),
        )
        (engine / "job.mp4").write_bytes(b"x" * 2048)

        bot = RecordingBot()
        update, context, status = _status(bot)
        await download_flow.run_direct(
            update, context, status, "https://a.example/1", "video", None, lang="en"
        )

        # The engine is asked for 1440p, not for "whatever is best".
        (args, kwargs) = started[0]
        assert args[2] == "308"
        assert kwargs["height"] == 1440

        caption = _plain(bot.of("send_video")[0]["caption"])
        assert "1440p" in caption
        assert "Selected 1440p: 2160p does not fit the send limit" in caption

    @pytest.mark.asyncio
    async def test_best_says_nothing_when_the_best_quality_fits(self, engine, monkeypatch):
        info = {
            "title": "Small video",
            "duration": 60,
            "formats": [{"id": "137", "label": "1080p", "height": 1080,
                         "size_bytes": 10}],
            "audio": {"size_bytes": 10},
        }
        monkeypatch.setattr(download_flow, "get_info", AsyncMock(return_value=dict(info)))
        monkeypatch.setattr(
            download_flow, "start_download",
            AsyncMock(return_value={"job_id": "j1", "status": "downloading"}),
        )
        monkeypatch.setattr(
            download_flow, "poll_status",
            AsyncMock(return_value={
                "status": "done", "filename": "job.mp4", "file_size_bytes": 2048,
                "duration": 60, "height": 1080, "width": 1920,
            }),
        )
        (engine / "job.mp4").write_bytes(b"x" * 2048)

        bot = RecordingBot()
        update, context, status = _status(bot)
        await download_flow.run_direct(
            update, context, status, "https://a.example/1", "video", None, lang="en"
        )

        caption = _plain(bot.of("send_video")[0]["caption"])
        assert "1080p" in caption
        assert "does not fit" not in caption

    @pytest.mark.asyncio
    async def test_nothing_fits_stops_before_the_engine(self, engine, monkeypatch):
        info = {
            "title": "Too big",
            "duration": 600,
            "formats": [{"id": "313", "label": "2160p", "height": 2160,
                         "size_bytes": 2_469_606_195}],
            "audio": {"size_bytes": 3 * 1024 ** 3},
        }
        monkeypatch.setattr(download_flow, "get_info", AsyncMock(return_value=dict(info)))
        started = AsyncMock()
        monkeypatch.setattr(download_flow, "start_download", started)

        bot = RecordingBot()
        update, context, status = _status(bot)
        await download_flow.run_direct(
            update, context, status, "https://a.example/1", "video", None, lang="en"
        )

        started.assert_not_awaited()
        assert "This video is too large to send (limit 2000 MB)." in _plain(
            bot.of("edit_message_text")[-1]["text"]
        )
