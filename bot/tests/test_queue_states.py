"""Tests for the bot's queue and refusal states on the one card.

The engine decides; the bot only has to say the truth about it:

* queued  -> "In queue №3 · ~45 s" on the same card, no buttons;
* refused -> "the service is overloaded — try again in ~2 min" (or the
  per-user variant) with the format keyboard still available;
* both then fall through to the usual progress and file stages.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import progress_card  # noqa: E402
from handlers import config, download_flow  # noqa: E402
from koyura_client import KoyuraBusy  # noqa: E402


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch):
    real_sleep = download_flow.asyncio.sleep

    async def _fast(delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(download_flow.asyncio, "sleep", _fast)


def _plain(text: str) -> str:
    """Drop MarkdownV2 backslashes so assertions read like the caption."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


INFO = {
    "title": "Artemis II to the Moon",
    "duration": 635,
    "extractor": "youtube",
    "channel": "NASA",
    "formats": [
        {"id": "137", "label": "1080p", "height": 1080, "button_label": "1080p · 32.1MB"},
    ],
    "audio": {"abr": 129, "size_bytes": 10_271_496},
}


@pytest.fixture()
def downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOADS_PATH", str(tmp_path))
    # The sleeps are patched to zero, so the edit throttle would otherwise
    # swallow the stage changes; the throttle has its own tests.
    monkeypatch.setattr(config, "PROGRESS_EDIT_INTERVAL", 0.0)
    monkeypatch.setattr(download_flow.event_client, "send_download_start", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_download_done", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_download_error", AsyncMock())
    monkeypatch.setattr(download_flow.event_client, "send_progress", AsyncMock())
    return tmp_path


def _bot():
    bot = MagicMock()
    bot.send_video = AsyncMock()
    bot.send_audio = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_message = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    bot.edit_message_media = AsyncMock()
    return bot


def _context(bot):
    context = MagicMock()
    context.bot = bot
    return context


async def _run(bot, *, fmt="video", format_id=None, height=None, url_hash="abcdef123456"):
    await download_flow.run_download(
        context=_context(bot),
        chat_id=1,
        status_message_id=2,
        url="https://youtube.com/watch?v=x",
        info=INFO,
        fmt=fmt,
        format_id=format_id,
        height=height,
        user_id=3,
        username="tester",
        started_at=0.0,
        url_hash=url_hash,
    )


def _captions(bot) -> list[str]:
    return [_plain(c.kwargs["caption"]) for c in bot.edit_message_caption.await_args_list]


# ---------------------------------------------------------------------------
# Queued
# ---------------------------------------------------------------------------

class TestQueuedCard:
    @pytest.mark.asyncio
    async def test_accepted_queue_position_shows_on_the_card(self, downloads, monkeypatch):
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(return_value={
                "job_id": "j1",
                "status": "queued",
                "queue_position": 3,
                "wait_seconds": 45,
                "queue_estimate_approximate": True,
            }),
        )
        monkeypatch.setattr(
            download_flow,
            "poll_status",
            AsyncMock(return_value={
                "status": "queued",
                "queue_position": 2,
                "wait_seconds": 30,
            }),
        )
        bot = _bot()
        await _run(bot)

        captions = _captions(bot)
        assert any("In queue №3 · ~45 s" in caption for caption in captions)
        # The poll loop keeps the card current as the position improves.
        assert any("In queue №2 · ~30 s" in caption for caption in captions)
        # No buttons while waiting: the card is busy.
        for call in bot.edit_message_caption.await_args_list:
            assert call.kwargs.get("reply_markup") is None

    @pytest.mark.asyncio
    async def test_queue_caption_is_marked_as_an_estimate(self, downloads, monkeypatch):
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(return_value={"job_id": "j1", "status": "queued",
                                    "queue_position": 1, "wait_seconds": 20}),
        )
        monkeypatch.setattr(
            download_flow,
            "poll_status",
            AsyncMock(return_value={"status": "error", "error": "boom"}),
        )
        bot = _bot()
        await _run(bot)
        assert any("Estimated wait" in caption for caption in _captions(bot))

    @pytest.mark.asyncio
    async def test_queued_job_becomes_a_progress_card(self, downloads, monkeypatch):
        """After waiting, the same card shows progress and then the file."""
        queued = {"status": "queued", "queue_position": 1, "wait_seconds": 10}
        downloading = {"status": "downloading", "progress": {"percent": 42.0}}
        done = {"status": "done", "filename": "job.mp4", "file_size_bytes": 2048,
                "duration": 635, "height": 720, "width": 1280}
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(return_value={"job_id": "j1", **queued}),
        )
        monkeypatch.setattr(
            download_flow,
            "poll_status",
            AsyncMock(side_effect=[queued, downloading, done]),
        )
        (downloads / "job.mp4").write_bytes(b"x" * 2048)

        bot = _bot()
        await _run(bot, height=720)

        captions = _captions(bot)
        assert any("In queue" in caption for caption in captions)
        assert any("42%" in caption for caption in captions)
        assert bot.edit_message_media.await_count == 1

    @pytest.mark.asyncio
    async def test_queue_state_without_numbers_is_still_readable(self, downloads, monkeypatch):
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(return_value={"job_id": "j1", "status": "queued"}),
        )
        monkeypatch.setattr(
            download_flow,
            "poll_status",
            AsyncMock(return_value={"status": "error", "error": "boom"}),
        )
        bot = _bot()
        await _run(bot)
        assert any("In queue" in caption for caption in _captions(bot))


# ---------------------------------------------------------------------------
# Refused
# ---------------------------------------------------------------------------

class TestRefusalCard:
    @pytest.mark.asyncio
    async def test_busy_refusal_says_when_to_retry(self, downloads, monkeypatch):
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(side_effect=KoyuraBusy("engine busy", code="busy", retry_after=145)),
        )
        bot = _bot()
        await _run(bot)

        caption = _captions(bot)[-1]
        assert "overloaded" in caption.lower()
        assert "~3 min" in caption
        # The buttons are still on screen; no "tap a format" hint needed.
        assert "Tap a format" not in caption
        markup = bot.edit_message_caption.await_args.kwargs["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "dl:2:abcdef123456:137" in datas
        assert bot.send_video.call_count == 0

    @pytest.mark.asyncio
    async def test_per_user_refusal_has_its_own_wording(self, downloads, monkeypatch):
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(side_effect=KoyuraBusy("busy", code="user_busy", retry_after=20)),
        )
        bot = _bot()
        await _run(bot)
        caption = _captions(bot)[-1]
        assert "already have a download" in caption.lower()
        assert "~20 s" in caption

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["busy", "queue_full", "too_long"])
    async def test_every_refusal_code_shows_the_same_friendly_text(self, downloads, monkeypatch, code):
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(side_effect=KoyuraBusy("busy", code=code, retry_after=60)),
        )
        bot = _bot()
        await _run(bot)
        assert "overloaded" in _captions(bot)[-1].lower()

    @pytest.mark.asyncio
    async def test_refusal_without_a_wait_still_reads_well(self, downloads, monkeypatch):
        monkeypatch.setattr(download_flow, "start_download", AsyncMock(side_effect=KoyuraBusy("busy")))
        bot = _bot()
        await _run(bot)
        assert "~2 min" in _captions(bot)[-1]

    @pytest.mark.asyncio
    async def test_an_old_engine_without_codes_still_works(self, downloads, monkeypatch):
        """A pre-queue engine raises KoyuraBusy with no code and no retry_after."""
        monkeypatch.setattr(
            download_flow,
            "start_download",
            AsyncMock(side_effect=KoyuraBusy("Server is busy: 2 downloads are already running")),
        )
        bot = _bot()
        await _run(bot)
        caption = _captions(bot)[-1]
        assert "overloaded" in caption.lower()
        assert "Download failed" in caption


# ---------------------------------------------------------------------------
# Caption helpers
# ---------------------------------------------------------------------------

class TestQueueCaptionHelpers:
    def test_wait_seconds_are_rounded_to_five(self):
        assert progress_card.format_queue_wait(43) == "~45 s"
        assert progress_card.format_queue_wait(2) == "~5 s"

    def test_minutes_round_up(self):
        # A 2m25s wait must not be announced as 2 min and then run longer.
        assert progress_card.format_queue_wait(145) == "~3 min"
        assert progress_card.format_queue_wait(90) == "~2 min"

    def test_missing_wait_draws_nothing(self):
        assert progress_card.format_queue_wait(None) is None
        assert progress_card.format_queue_wait(0) is None
        assert progress_card.format_queue_wait("NA") is None

    def test_queued_caption_has_the_position(self):
        caption = _plain(progress_card.build_queued_caption(INFO, 3, 45))
        assert "Artemis II to the Moon" in caption
        assert "In queue №3 · ~45 s" in caption

    def test_queued_caption_without_a_position(self):
        caption = _plain(progress_card.build_queued_caption(INFO, None, None))
        assert "In queue" in caption
        assert "№" not in caption

    def test_busy_message_prefers_the_engine_wait(self):
        error = KoyuraBusy("x", code="busy", retry_after=150)
        assert "~3 min" in progress_card.busy_message(error)

    def test_busy_message_never_empty(self):
        assert progress_card.busy_message("") == progress_card.busy_message("anything")
        assert "overloaded" in progress_card.busy_message("plain string").lower()
