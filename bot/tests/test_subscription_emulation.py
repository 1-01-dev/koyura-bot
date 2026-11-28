"""Emulate the gate end to end: language → gate → check → welcome.

The owner asked for two live sequences, and this module is how they are proved
without a Telegram account:

    a new user, gate on, subscribes
        /start  → "Choose the language"
                → tap lang:ru
                → the gate screen in Russian, one link button per channel
                → tap sub:check, Telegram says "member"
                → the welcome screen in Russian

    the same user, still not subscribed
        … tap sub:check, Telegram says "left"
                → an alert naming the channel, and the gate screen again

Run it with ``-s`` to read the call log quoted in the pull request:

    cd bot && python -m pytest tests/test_subscription_emulation.py -s -q
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import state_store
import subscriptions
from handlers import onboarding, subscription

from tests.test_download_flow import RecordingBot
from tests.test_onboarding import fake_query, fake_update
from tests.test_stage_emulation import _log


class GateBot(RecordingBot):
    """A recording bot whose ``get_chat_member`` answer can change mid-test."""

    def __init__(self, status: object = "member"):
        super().__init__()
        self.status = status
        self.member_calls: list[dict] = []

    async def get_chat_member(self, **kwargs):
        self.member_calls.append(kwargs)
        if isinstance(self.status, BaseException):
            raise self.status
        return SimpleNamespace(status=self.status)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(tmp_path / "state.db"))
    state_store.reset_cache()
    state_store.init()
    subscriptions.reset_cache()

    async def fake_send(*, user_id, channel_id, state):
        return None

    monkeypatch.setattr(subscription.event_client, "send_subscription_event", fake_send)
    yield
    subscriptions.reset_cache()
    state_store.reset_cache()


def save_russian() -> None:
    """The user picked Russian already, so the answers arrive in Russian.

    ``language.current`` reads the saved choice; ``fake_query`` does not set one,
    and a test that asserts a Russian sentence has to say so explicitly.
    """
    state_store.set_language(7, "ru")


def prime_gate():
    gate = subscriptions.Gate(
        enabled=True,
        channels=(
            subscriptions.Channel(
                id=1,
                title="Koyura · news",
                username="koyura_news",
                link="https://t.me/koyura_news",
            ),
        ),
    )
    subscriptions.prime(gate)
    return gate


@pytest.mark.asyncio
async def test_language_then_gate_then_check_then_welcome():
    """The subscribed branch: the gate is shown, passed, and replaced."""
    prime_gate()
    bot = GateBot(status="member")
    context = MagicMock(bot=bot)

    # 1. The language tap answers with the gate, not the welcome.
    update, _query = fake_query(bot, "lang:ru")
    await onboarding.on_language_button(update, context)

    posted = bot.of("send_message")
    assert len(posted) == 1, _log("gate", bot)
    gate_text = posted[0]["text"]
    assert "Ещё один шаг" in gate_text
    assert "Koyura · news" in gate_text
    buttons = [
        button
        for row in posted[0]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert [button.url for button in buttons if button.url == "https://t.me/koyura_news"]
    assert any(button.callback_data == "sub:check" for button in buttons)
    print("\n" + _log("ru → гейт подписки", bot))

    # 2. The check passes: Telegram says "member", so the welcome replaces it.
    bot.calls.clear()
    update, _query = fake_query(bot, "sub:check")
    await subscription.on_subscription_button(update, context)

    assert bot.member_calls == [{"chat_id": "@koyura_news", "user_id": 7}]
    welcomes = [
        kwargs for kwargs in bot.of("send_message")
        if "тихий загрузчик" in str(kwargs.get("text"))
    ]
    assert welcomes, _log("welcome after the gate", bot)
    # The gate's own message is retired: one screen at a time.
    assert "delete_message" in bot.names()
    assert state_store.get_subscription_passed(7) is True
    print("\n" + _log("подписка подтверждена → приветствие", bot))


@pytest.mark.asyncio
async def test_still_not_subscribed_keeps_the_gate():
    """The "not yet" branch: an alert, the same buttons, no access."""
    prime_gate()
    save_russian()
    bot = GateBot(status="left")
    context = MagicMock(bot=bot)

    update, query = fake_query(bot, "sub:check")
    await subscription.on_subscription_button(update, context)

    # A toast naming the channel is the answer to the tap.
    query.answer.assert_awaited()
    alert = query.answer.await_args
    assert alert is not None
    assert alert.kwargs.get("show_alert") is True
    assert "Koyura · news" in alert.args[0]

    # The screen is rewritten in place (an edit, not a new message) and keeps
    # the check button, so the user can retry without sending the link again.
    assert state_store.get_subscription_passed(7) is False
    edited = bot.of("edit_message_text") or bot.of("edit_message_caption")
    assert edited, _log("still not subscribed", bot)
    assert "не подписаны" in str(edited[0].get("text") or edited[0].get("caption"))
    print("\n" + _log("подписки нет → тот же экран", bot))


@pytest.mark.asyncio
async def test_a_failed_check_says_try_again_and_keeps_the_screen():
    """Telegram unreachable is not a refusal: "try again later", not "no"."""
    prime_gate()
    save_russian()
    bot = GateBot(status=RuntimeError("telegram is down"))
    context = MagicMock(bot=bot)

    update, query = fake_query(bot, "sub:check")
    await subscription.on_subscription_button(update, context)

    query.answer.assert_awaited()
    alert = query.answer.await_args
    assert alert is not None
    assert "позже" in alert.args[0].lower()
    assert state_store.get_subscription_passed(7) is False
    print("\n" + _log("проверка не удалась → попробуйте позже", bot))


@pytest.mark.asyncio
async def test_start_with_a_saved_language_shows_the_gate_first():
    """``/start`` from a returning user lands on the gate, not the welcome."""
    prime_gate()
    save_russian()
    bot = GateBot(status="member")
    context = MagicMock(bot=bot)

    update = fake_update(bot)
    await onboarding.start_command(update, context)

    posted = bot.of("send_message")
    assert len(posted) == 1, _log("start → gate", bot)
    assert "Ещё один шаг" in posted[0]["text"]
    assert any(
        button.callback_data == "sub:check"
        for row in posted[0]["reply_markup"].inline_keyboard
        for button in row
    )
    print("\n" + _log("start с сохранённым языком → сразу гейт", bot))


@pytest.mark.asyncio
async def test_start_goes_straight_to_the_welcome_when_the_gate_is_passed():
    prime_gate()
    save_russian()
    state_store.set_subscription_passed(7, True)
    bot = GateBot(status="member")
    context = MagicMock(bot=bot)

    update = fake_update(bot)
    await onboarding.start_command(update, context)

    posted = bot.of("send_message")
    assert len(posted) == 1
    assert "тихий загрузчик" in posted[0]["text"]
    print("\n" + _log("start после пройденного гейта → приветствие", bot))
