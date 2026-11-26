"""Link intake and the preview card.

Covers the two halves of ``url_flow``: what a message's links turn into before
anything is fetched (validation, the five-link cap, the reply-to fallback) and
what the user sees for every outcome of ``/api/info``.
"""
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import url_safety  # noqa: E402
from handlers import config, sessions, url_flow  # noqa: E402
from koyura_client import KoyuraError, KoyuraInfoError, KoyuraServiceDown  # noqa: E402

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


INFO = {
    "title": "Artemis II to the Moon",
    "duration": 635,
    "extractor": "youtube",
    "channel": "NASA",
    "formats": [{"id": "137", "label": "1080p", "height": 1080, "button_label": "1080p"}],
    "audio": {"abr": 129, "size_bytes": 10_271_496},
}


@pytest.fixture(autouse=True)
def _clean_sessions():
    sessions.forget_all()
    yield
    sessions.forget_all()


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Validation is not the thing under test; DNS in CI is not available."""
    def _validate(url):
        if "127.0.0.1" in url or "localhost" in url:
            raise url_safety.UnsafeURLError("URL points to a local or private address")
        return url

    monkeypatch.setattr(url_flow, "validate_url", _validate)


class RecordingBot:
    """The bot behind ``update.get_bot()``: records every stage operation."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, BaseException] = {}
        self._next_id = 100

    def _record(self, name: str, kwargs: dict, *, returns_message: bool = False):
        self.calls.append((name, kwargs))
        error = self.fail.get(name)
        if error is not None:
            raise error
        if returns_message:
            self._next_id += 1
            return MagicMock(message_id=self._next_id)
        return True

    async def send_message(self, **kwargs):
        return self._record("send_message", kwargs, returns_message=True)

    async def send_photo(self, **kwargs):
        return self._record("send_photo", kwargs, returns_message=True)

    async def delete_message(self, **kwargs):
        return self._record("delete_message", kwargs)

    async def edit_message_text(self, **kwargs):
        return self._record("edit_message_text", kwargs)

    async def edit_message_caption(self, **kwargs):
        return self._record("edit_message_caption", kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        return self._record("edit_message_reply_markup", kwargs)

    # -- helpers ----------------------------------------------------------

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def of(self, name: str) -> list[dict]:
        return [kwargs for called, kwargs in self.calls if called == name]

    def written_texts(self) -> list[str]:
        """Everything the user ends up reading, in order."""
        texts = []
        for called, kwargs in self.calls:
            if called in ("send_message", "edit_message_text"):
                texts.append(str(kwargs.get("text") or ""))
            elif called == "edit_message_caption":
                texts.append(str(kwargs.get("caption") or ""))
        return texts


def fake_update(text, *, replied=None):
    """An update whose status message is id 11 and whose bot is a recorder.

    The first "Fetching info..." still comes from ``message.reply_text`` (it is a
    reply to the user's link); every later stage goes through the bot, because a
    replacement is posted as a plain chat message and the old one is deleted.
    """
    bot = RecordingBot()

    status = MagicMock()
    status.message_id = 11
    status.chat_id = 5
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()

    sent_photo = MagicMock()
    sent_photo.message_id = 12

    message = MagicMock()
    message.text = text
    message.caption = None
    message.reply_to_message = replied
    message.message_id = 11
    message.chat_id = 5
    message.reply_text = AsyncMock(return_value=status)
    message.reply_photo = AsyncMock(return_value=sent_photo)
    message.delete = AsyncMock()

    user = MagicMock()
    user.id = 7
    user.username = "tester"

    update = MagicMock()
    update.message = message
    update.effective_user = user
    update.effective_chat = MagicMock(id=5)
    update.get_bot = MagicMock(return_value=bot)
    update.bot = bot
    return update, status


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

class TestExtract:
    def test_ignores_text_without_links(self):
        assert url_flow.extract_urls("just a sentence") == []
        assert url_flow.extract_urls(None) == []

    def test_finds_several_links_in_order(self):
        assert url_flow.extract_urls("a https://x.example/1 b https://y.example/2") == [
            "https://x.example/1",
            "https://y.example/2",
        ]

    def test_command_urls_use_the_command_text(self):
        update, _ = fake_update("/mp3 https://x.example/1")
        assert url_flow.command_urls(update) == ["https://x.example/1"]

    def test_command_urls_fall_back_to_the_reply(self):
        replied = MagicMock()
        replied.text = "https://x.example/replied"
        replied.caption = None
        update, _ = fake_update("/mp3", replied=replied)
        assert url_flow.command_urls(update) == ["https://x.example/replied"]

    def test_command_urls_prefer_the_command_text(self):
        replied = MagicMock()
        replied.text = "https://x.example/replied"
        update, _ = fake_update("/mp3 https://x.example/own", replied=replied)
        assert url_flow.command_urls(update) == ["https://x.example/own"]

    def test_reply_caption_is_used_too(self):
        replied = MagicMock()
        replied.text = None
        replied.caption = "watch https://x.example/caption"
        update, _ = fake_update("/mp3", replied=replied)
        assert url_flow.command_urls(update) == ["https://x.example/caption"]

    def test_no_message_is_no_links(self):
        update, _ = fake_update("/mp3")
        update.message = None
        assert url_flow.command_urls(update) == []


class TestValidation:
    def test_public_links_pass_through(self):
        safe, notice = url_flow.validated_links(["https://x.example/1"])
        assert safe == ["https://x.example/1"]
        assert notice is None

    def test_private_address_is_refused_with_one_notice(self):
        safe, notice = url_flow.validated_links(
            ["http://127.0.0.1/x", "https://x.example/1"]
        )
        assert safe == ["https://x.example/1"]
        assert _plain(notice) == (
            "I can only download from public http(s) links. "
            "That link was skipped because it is not allowed."
        )

    def test_only_the_first_five_links_are_used(self):
        urls = [f"https://x.example/{index}" for index in range(8)]
        safe, notice = url_flow.validated_links(urls)
        assert len(safe) == config.MAX_LINKS_PER_MESSAGE
        assert _plain(notice) == "Only the first 5 links of a message are processed."

    def test_both_notices_share_one_message(self):
        # Seven candidates, the first of them refused: five are examined, four
        # survive, and the cap notice still fires on the total.
        urls = ["http://127.0.0.1/x"] + [f"https://x.example/{i}" for i in range(6)]
        safe, notice = url_flow.validated_links(urls)
        assert len(safe) == config.MAX_LINKS_PER_MESSAGE - 1
        assert notice is not None
        assert _plain(notice).splitlines() == [
            "I can only download from public http(s) links. "
            "That link was skipped because it is not allowed.",
            "Only the first 5 links of a message are processed.",
        ]

    @pytest.mark.asyncio
    async def test_async_wrapper_matches_the_sync_one(self):
        urls = ["http://127.0.0.1/x", "https://x.example/1"]
        assert await url_flow.safe_links(urls) == url_flow.validated_links(urls)


class TestTextHandler:
    @pytest.mark.asyncio
    async def test_text_without_a_link_gets_no_answer(self):
        update, status = fake_update("hello there")
        await url_flow.url_handler(update, MagicMock())

        update.message.reply_text.assert_not_awaited()
        status.edit_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_without_a_message_is_ignored(self):
        update, _ = fake_update("https://x.example/1")
        update.message = None
        await url_flow.url_handler(update, MagicMock())  # must not raise

    @pytest.mark.asyncio
    async def test_a_refused_link_is_announced_and_nothing_is_fetched(self, monkeypatch):
        get_info = AsyncMock()
        monkeypatch.setattr(url_flow, "get_info", get_info)
        update, _ = fake_update("http://127.0.0.1/x")

        await url_flow.url_handler(update, MagicMock())

        assert _plain(update.message.reply_text.await_args.args[0]).startswith(
            "I can only download from public http(s) links."
        )
        get_info.assert_not_awaited()
        assert sessions.live_count() == 0

    @pytest.mark.asyncio
    async def test_one_card_per_link(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        update, _ = fake_update("https://x.example/1 https://x.example/2")

        await url_flow.url_handler(update, MagicMock())

        assert update.message.reply_text.await_count == 2  # one "Fetching info..." each
        assert sessions.live_count() == 2

    @pytest.mark.asyncio
    async def test_stale_sessions_are_dropped_on_intake(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        entry = sessions.remember(
            url="https://x.example/old",
            chat_id=5,
            user_id=7,
            username="tester",
            info=INFO,
            message_id=99,
            photo=True,
        )
        entry["created"] = time.time() - config.STATE_TTL - 1

        update, _ = fake_update("https://x.example/new")
        await url_flow.url_handler(update, MagicMock())

        assert sessions.get(5, 99, sessions.url_hash("https://x.example/old")) is None
        assert sessions.live_count() == 1


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

class TestPreviewCard:
    @pytest.mark.asyncio
    async def test_thumbnail_becomes_the_card(self, monkeypatch):
        info = dict(INFO, thumbnail="https://cdn.example/thumb.jpg")
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=info))
        update, status = fake_update("https://x.example/1")

        await url_flow.url_handler(update, MagicMock())

        bot = update.get_bot()
        # The photo is posted **first** and "Fetching info..." (id 11) is deleted
        # after it, so the chat never shows a link with no card. The keyboard is
        # attached afterwards, because a button must carry its own message id.
        assert bot.names() == [
            "send_photo", "edit_message_reply_markup", "delete_message"
        ]
        assert bot.of("delete_message")[0]["message_id"] == 11

        kwargs = bot.of("send_photo")[0]
        assert kwargs["photo"] == "https://cdn.example/thumb.jpg"
        assert kwargs["parse_mode"] == "MarkdownV2"
        assert "Artemis II to the Moon" in kwargs["caption"]
        # The buttons are attached once the new message has an id, so they
        # address the card that shows them and not the deleted status line.
        markup = bot.of("edit_message_reply_markup")[0]["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        fingerprint = sessions.url_hash("https://x.example/1")
        assert f"dl:101:{fingerprint}:137" in datas
        assert f"dl:101:{fingerprint}:mp3" in datas

        entry = sessions.get(5, 101, sessions.url_hash("https://x.example/1"))
        assert entry is not None
        assert entry["photo"] is True
        assert entry["job_active"] is False

    @pytest.mark.asyncio
    async def test_without_a_thumbnail_the_status_message_becomes_the_card(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        update, status = fake_update("https://x.example/1")

        await url_flow.url_handler(update, MagicMock())

        # No photo to post: the "Fetching info..." message is edited in place, so
        # the link still ends up with exactly one message.
        bot = update.get_bot()
        assert bot.names() == ["edit_message_text"]
        kwargs = bot.of("edit_message_text")[0]
        assert "Artemis II to the Moon" in kwargs["text"]
        assert kwargs["message_id"] == 11
        assert kwargs["parse_mode"] == "MarkdownV2"
        assert kwargs["reply_markup"] is not None
        entry = sessions.get(5, 11, sessions.url_hash("https://x.example/1"))
        assert entry is not None
        assert entry["photo"] is False

    @pytest.mark.asyncio
    async def test_unsendable_thumbnail_falls_back_to_text(self, monkeypatch):
        info = dict(INFO, thumbnail="https://cdn.example/thumb.jpg")
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=info))
        update, status = fake_update("https://x.example/1")
        update.get_bot().fail["send_photo"] = RuntimeError("too big")

        await url_flow.url_handler(update, MagicMock())

        bot = update.get_bot()
        # The photo never arrived, so nothing is deleted and the caption goes
        # into the message that is already on screen.
        assert bot.names() == ["send_photo", "edit_message_text"]
        assert bot.of("delete_message") == []
        assert bot.of("edit_message_text")[0]["message_id"] == 11
        # The buttons travel with that edit, addressing the same message.
        markup = bot.of("edit_message_text")[0]["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert all(data.startswith("dl:11:") for data in datas)
        entry = sessions.get(5, 11, sessions.url_hash("https://x.example/1"))
        assert entry is not None and entry["photo"] is False

    @pytest.mark.asyncio
    async def test_unrenderable_card_says_what_to_do(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        update, status = fake_update("https://x.example/1")
        bot = update.get_bot()
        # The card edit fails for every stage, so the advice is all that is left.
        bot.fail["edit_message_text"] = RuntimeError("no")

        await url_flow.url_handler(update, MagicMock())

        assert _plain(bot.written_texts()[-1]) == (
            "Could not render this link. Try /mp4 or /mp3 instead."
        )
        assert sessions.live_count() == 0

    @pytest.mark.asyncio
    async def test_service_down(self, monkeypatch):
        monkeypatch.setattr(
            url_flow, "get_info", AsyncMock(side_effect=KoyuraServiceDown("down"))
        )
        update, status = fake_update("https://x.example/1")

        await url_flow.url_handler(update, MagicMock())

        # The one failure message replaces "Fetching info...": edit in place.
        assert _plain(update.get_bot().written_texts()[-1]) == (
            "Download service temporarily unavailable."
        )

    @pytest.mark.asyncio
    async def test_info_error_carries_the_engine_message(self, monkeypatch):
        monkeypatch.setattr(
            url_flow, "get_info", AsyncMock(side_effect=KoyuraInfoError("Unsupported URL"))
        )
        update, status = fake_update("https://x.example/1")

        await url_flow.url_handler(update, MagicMock())

        assert _plain(update.get_bot().written_texts()[-1]) == (
            "Failed to fetch info: Unsupported URL"
        )

    @pytest.mark.asyncio
    async def test_generic_error_is_prefixed(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(side_effect=KoyuraError("boom")))
        update, status = fake_update("https://x.example/1")

        await url_flow.url_handler(update, MagicMock())

        assert _plain(update.get_bot().written_texts()[-1]) == "Error: boom"

    @pytest.mark.asyncio
    async def test_update_without_chat_or_user_posts_nothing(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        update, _ = fake_update("https://x.example/1")
        update.effective_chat = None

        await url_flow.url_handler(update, MagicMock())

        update.message.reply_text.assert_not_awaited()
        assert sessions.live_count() == 0

    @pytest.mark.asyncio
    async def test_anonymous_user_is_identified_by_id(self, monkeypatch):
        monkeypatch.setattr(url_flow, "get_info", AsyncMock(return_value=dict(INFO)))
        update, _ = fake_update("https://x.example/1")
        update.effective_user.username = None

        await url_flow.url_handler(update, MagicMock())

        entry = sessions.get(5, 11, sessions.url_hash("https://x.example/1"))
        assert entry is not None and entry["username"] == "7"
