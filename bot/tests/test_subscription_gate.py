"""The subscription gate: whether it exists, what it says, and when it lets you in.

Four promises the owner described in one paragraph, and each one is a test here:

* **both halves are needed** — the master switch *and* at least one channel the
  bot can verify. Switch off means no gate even with channels stored; on with no
  channels means no gate either, because a bot that blocks everyone behind a list
  it cannot check is a lockout, not a gate;
* **``getChatMember`` decides** — ``member``, ``administrator`` and ``creator``
  count as subscribed, ``left`` and ``kicked`` do not, and a call that *fails* is
  neither: the user is told "try again later" and is not let through;
* **a state transition is recorded once** — the panel is the writer, and the bot
  reports a verdict on every check without knowing whether it is a change, so
  re-posting the same verdict cannot inflate the unsubscribe count;
* **the templates exist in the registry** — the gate's four texts and two button
  labels are catalogued, with ``en``/``ru`` and their placeholders, or the
  message builder would not show them in the panel.

Everything is driven by a fake bot whose ``get_chat_member`` answers from a dict,
so no test talks to Telegram.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import message_kinds
import pytest
import state_store
import subscriptions
from handlers import keyboards, subscription, texts

from tests.test_download_flow import RecordingBot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """A private state database and no cached gate, for every test."""
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(tmp_path / "state.db"))
    state_store.reset_cache()
    state_store.init()
    subscriptions.reset_cache()
    yield
    subscriptions.reset_cache()
    state_store.reset_cache()


def channel(channel_id=1, title="Koyura · news", username="koyura_news"):
    return subscriptions.Channel(
        id=channel_id,
        title=title,
        username=username,
        link=f"https://t.me/{username}",
    )


def prime(*, enabled=True, channels=None):
    """Install a gate without a network round trip."""
    gate = subscriptions.Gate(
        enabled=enabled,
        channels=tuple(
            channels if channels is not None else (channel(),)
        ),
    )
    subscriptions.prime(gate)
    return gate


class MembershipBot(RecordingBot):
    """A recording bot that also answers ``get_chat_member``.

    ``members`` maps a channel handle (``@koyura_news``) to a status, or to an
    exception to raise. A handle that is not in the map raises, which is how a
    channel with no verdict is exercised.
    """

    def __init__(self, members=None):
        super().__init__()
        self.members = dict(members or {})
        self.member_calls: list[dict] = []

    async def get_chat_member(self, **kwargs):
        self.member_calls.append(kwargs)
        answer = self.members.get(str(kwargs.get("chat_id")))
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise RuntimeError("no verdict for this channel")
        return SimpleNamespace(status=answer)


def reported() -> list[dict]:
    """Every subscription verdict the bot tried to report to the panel."""
    return list(report_calls)


report_calls: list[dict] = []


@pytest.fixture(autouse=True)
def _capture_reports(monkeypatch):
    """Swallow the panel call and record what would have been posted."""
    report_calls.clear()

    async def fake_send(*, user_id, channel_id, state):
        report_calls.append(
            {"user_id": user_id, "channel_id": channel_id, "state": state}
        )

    monkeypatch.setattr(subscription.event_client, "send_subscription_event", fake_send)
    yield
    report_calls.clear()


# ---------------------------------------------------------------------------
# 1. Is there a gate at all?
# ---------------------------------------------------------------------------

class TestWhetherThereIsAGate:
    def test_switch_off_means_no_gate_even_with_channels(self):
        prime(enabled=False)
        gate = subscriptions.cached()
        assert gate is not None
        assert gate.channels  # a channel is stored...
        assert not gate.active()  # ...and the gate is still off

    def test_switch_on_without_channels_means_no_gate(self):
        prime(enabled=True, channels=[])
        gate = subscriptions.cached()
        assert gate is not None
        assert not gate.active()

    def test_switch_on_with_a_channel_is_a_gate(self):
        prime(enabled=True, channels=[channel()])
        gate = subscriptions.cached()
        assert gate is not None
        assert gate.active()

    @pytest.mark.asyncio
    async def test_active_answers_none_when_the_switch_is_off(self):
        prime(enabled=False)
        assert await subscription.active() is None

    @pytest.mark.asyncio
    async def test_active_answers_none_when_no_channel_is_enabled(self):
        prime(enabled=True, channels=[])
        assert await subscription.active() is None

    @pytest.mark.asyncio
    async def test_active_answers_the_gate_when_it_is_in_force(self):
        prime(enabled=True, channels=[channel()])
        gate = await subscription.active()
        assert gate is not None and len(gate.channels) == 1

    def test_a_failed_panel_fetch_means_no_gate(self):
        """A panel that cannot answer must never lock the bot."""
        subscriptions.reset_cache()
        payload = {"enabled": True, "channels": [
            {"id": 1, "title": "x", "username": "koyura_news", "link": ""},
        ]}
        gate = subscriptions.normalise_gate(payload)
        assert gate.active()
        # ...but a body we cannot read is "no gate", not "block everyone".
        assert not subscriptions.normalise_gate(None).active()
        assert not subscriptions.normalise_gate("nonsense").active()

    def test_a_channel_without_a_handle_cannot_be_used(self):
        """A row with no username and no link cannot be opened or checked."""
        gate = subscriptions.normalise_gate(
            {"enabled": True, "channels": [{"id": 1, "title": "x"}]}
        )
        assert gate.channels == ()
        assert not gate.active()


# ---------------------------------------------------------------------------
# 2. getChatMember statuses
# ---------------------------------------------------------------------------

class TestMembershipStatuses:
    @pytest.mark.parametrize("status", ["member", "administrator", "creator", "restricted"])
    def test_a_subscribed_status_counts(self, status):
        assert subscriptions.is_subscribed(status)

    @pytest.mark.parametrize("status", ["left", "kicked"])
    def test_an_absent_status_does_not_count(self, status):
        assert not subscriptions.is_subscribed(status)

    @pytest.mark.parametrize("status", [None, "", "nonsense", "MEMBER"])
    def test_an_unknown_status_is_never_a_yes(self, status):
        """``MEMBER`` (upper case) is not a status Telegram sends: normalised."""
        assert subscriptions.is_subscribed(status) == (status == "MEMBER")

    @pytest.mark.asyncio
    async def test_a_member_passes_the_gate(self):
        gate = prime()
        bot = MembershipBot({"@koyura_news": "member"})
        subscribed, failed = await subscription.passed(bot, 42, gate)
        assert subscribed is True
        assert failed is False

    @pytest.mark.asyncio
    async def test_the_check_asks_about_the_configured_channel(self):
        gate = prime()
        bot = MembershipBot({"@koyura_news": "member"})
        await subscription.passed(bot, 42, gate)
        assert bot.member_calls == [{"chat_id": "@koyura_news", "user_id": 42}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["left", "kicked"])
    async def test_a_left_or_kicked_user_does_not_pass(self, status):
        gate = prime()
        bot = MembershipBot({"@koyura_news": status})
        subscribed, failed = await subscription.passed(bot, 42, gate)
        assert subscribed is False
        assert failed is False

    @pytest.mark.asyncio
    async def test_every_channel_has_to_be_subscribed(self):
        gate = prime(channels=[channel(1, username="one"), channel(2, username="two")])
        bot = MembershipBot({"@one": "member", "@two": "left"})
        subscribed, failed = await subscription.passed(bot, 42, gate)
        assert subscribed is False
        assert failed is False

    @pytest.mark.asyncio
    async def test_every_channel_subscribed_passes(self):
        gate = prime(channels=[channel(1, username="one"), channel(2, username="two")])
        bot = MembershipBot({"@one": "member", "@two": "administrator"})
        subscribed, failed = await subscription.passed(bot, 42, gate)
        assert subscribed is True
        assert failed is False

    @pytest.mark.asyncio
    async def test_a_failed_call_is_not_a_refusal(self):
        """A network error says "try later", never "you are not subscribed"."""
        gate = prime()
        bot = MembershipBot({"@koyura_news": RuntimeError("telegram is down")})
        subscribed, failed = await subscription.passed(bot, 42, gate)
        assert subscribed is False
        assert failed is True


# ---------------------------------------------------------------------------
# 3. The transition is reported, and the panel decides whether it is new
# ---------------------------------------------------------------------------

class TestTheTransitionIsReported:
    @pytest.mark.asyncio
    async def test_a_subscription_is_reported(self):
        gate = prime()
        bot = MembershipBot({"@koyura_news": "member"})
        await subscription.passed(bot, 42, gate)
        assert reported() == [
            {"user_id": 42, "channel_id": 1, "state": "subscribed"}
        ]

    @pytest.mark.asyncio
    async def test_a_missing_subscription_is_reported_as_left(self):
        gate = prime()
        bot = MembershipBot({"@koyura_news": "left"})
        await subscription.passed(bot, 42, gate)
        assert reported() == [{"user_id": 42, "channel_id": 1, "state": "left"}]

    @pytest.mark.asyncio
    async def test_a_failed_check_reports_nothing(self):
        """No verdict means nothing to record: a ``left`` would be a guess."""
        gate = prime()
        bot = MembershipBot({"@koyura_news": RuntimeError("down")})
        await subscription.passed(bot, 42, gate)
        assert reported() == []

    @pytest.mark.asyncio
    async def test_one_report_per_channel(self):
        gate = prime(channels=[channel(1, username="one"), channel(2, username="two")])
        bot = MembershipBot({"@one": "member", "@two": "left"})
        await subscription.passed(bot, 42, gate)
        assert reported() == [
            {"user_id": 42, "channel_id": 1, "state": "subscribed"},
            {"user_id": 42, "channel_id": 2, "state": "left"},
        ]


# ---------------------------------------------------------------------------
# 4. The screen and its texts
# ---------------------------------------------------------------------------

class TestTheScreen:
    def test_the_gate_text_lists_every_channel(self):
        gate = prime(channels=[channel(1, title="News"), channel(2, title="Chat", username="koyura_chat")])
        text = subscription.gate_text(gate, "en")
        assert "One more step" in text
        assert "News" in text
        assert "Chat" in text
        # MarkdownV2: the catalogue's punctuation is escaped for Telegram.
        assert "\\." in text

    def test_the_gate_text_is_translated(self):
        gate = prime(channels=[channel(1, title="Новости")])
        assert "Ещё один шаг" in subscription.gate_text(gate, "ru")

    def test_the_refusal_names_what_is_missing(self):
        gate = prime()
        text = subscription.missing_text([gate.channels[0]], "en")
        assert "not subscribed yet" in text
        assert "Koyura · news" in text

    def test_the_error_text_is_a_sentence_not_a_traceback(self):
        assert "try again later" in subscription.error_text("en").lower()
        assert "позже" in subscription.error_text("ru")


# ---------------------------------------------------------------------------
# 5. The keyboard
# ---------------------------------------------------------------------------

class TestTheKeyboard:
    def test_every_channel_gets_a_url_button(self):
        gate = prime(channels=[channel(1, username="one"), channel(2, username="two")])
        markup = keyboards.subscription(gate, "en")
        rows = markup.inline_keyboard
        # One row per channel, then the check button.
        assert len(rows) == 3
        assert rows[0][0].url == "https://t.me/one"
        assert rows[1][0].url == "https://t.me/two"

    def test_the_check_button_is_a_callback(self):
        gate = prime()
        rows = keyboards.subscription(gate, "en").inline_keyboard
        check = rows[-1][0]
        assert check.callback_data == "sub:check"
        assert check.url is None

    def test_the_labels_come_from_the_catalogue(self):
        gate = prime(channels=[channel(1, title="News")])
        rows = keyboards.subscription(gate, "ru").inline_keyboard
        assert rows[-1][0].text == texts.t("button.subscription_check", "ru")
        assert "News" in rows[0][0].text

    def test_parse_subscription_reads_both_shapes(self):
        assert keyboards.parse_subscription("sub:check") == "check"
        assert keyboards.parse_subscription("sub:open:3") == "open"
        assert keyboards.parse_subscription("lang:en") is None
        assert keyboards.parse_subscription(None) is None

    def test_missing_only_narrows_the_list(self):
        gate = prime(channels=[channel(1, title="One", username="one"),
                               channel(2, title="Two", username="two")])
        rows = keyboards.subscription(
            gate, "en", missing_only=[gate.channels[1]]
        ).inline_keyboard
        assert len(rows) == 2  # Two, then Check
        assert "Two" in rows[0][0].text


# ---------------------------------------------------------------------------
# 6. The download path is blocked until the gate is passed
# ---------------------------------------------------------------------------

class TestTheGateBlocksOrdinaryActions:
    @pytest.mark.asyncio
    async def test_no_gate_means_no_interference(self):
        prime(enabled=False)
        message = MagicMock()
        message.get_bot.return_value = MembershipBot()
        assert await subscription.remind(message, 42, "en") is False
        message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unsubscribed_user_is_asked_and_stopped(self):
        prime()
        message = MagicMock()
        message.reply_text = AsyncMock()
        message.get_bot.return_value = MembershipBot({"@koyura_news": "left"})
        assert await subscription.remind(message, 42, "en") is True
        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        assert "One more step" in text

    @pytest.mark.asyncio
    async def test_a_subscribed_user_goes_through_and_is_remembered(self):
        prime()
        message = MagicMock()
        message.get_bot.return_value = MembershipBot({"@koyura_news": "member"})
        assert await subscription.remind(message, 42, "en") is False
        message.reply_text.assert_not_called()
        assert state_store.get_subscription_passed(42) is True

    @pytest.mark.asyncio
    async def test_a_remembered_user_is_not_asked_again(self):
        prime()
        state_store.set_subscription_passed(42, True)
        message = MagicMock()
        bot = MembershipBot({"@koyura_news": "left"})
        message.get_bot.return_value = bot
        assert await subscription.remind(message, 42, "en") is False
        assert bot.member_calls == []  # no round trip at all


# ---------------------------------------------------------------------------
# 7. The templates are in the registry
# ---------------------------------------------------------------------------

class TestTheTemplates:
    @pytest.mark.parametrize(
        "kind_id",
        ["subscription.gate", "subscription.missing", "subscription.error",
         "button.subscription_check", "button.subscription_open"],
    )
    def test_the_kind_is_in_the_registry(self, kind_id):
        assert message_kinds.get(kind_id) is not None

    @pytest.mark.parametrize(
        "kind_id",
        ["subscription.gate", "subscription.missing", "subscription.error"],
    )
    def test_the_gate_texts_are_onboarding(self, kind_id):
        kind = message_kinds.get(kind_id)
        assert kind is not None
        assert kind.category == "onboarding"

    @pytest.mark.parametrize(
        "kind_id", ["button.subscription_check", "button.subscription_open"]
    )
    def test_the_button_labels_are_buttons(self, kind_id):
        kind = message_kinds.get(kind_id)
        assert kind is not None
        assert kind.category == "buttons"

    @pytest.mark.parametrize(
        "kind_id",
        ["subscription.gate", "subscription.missing", "subscription.error",
         "button.subscription_check", "button.subscription_open"],
    )
    def test_every_kind_has_both_languages(self, kind_id):
        kind = message_kinds.get(kind_id)
        assert kind is not None
        for lang in ("en", "ru"):
            assert kind.template(lang).strip(), f"{kind_id}/{lang}"

    def test_the_gate_offers_the_channels_placeholder(self):
        assert "channels" in message_kinds.placeholders_for("subscription.gate")

    def test_the_refusal_offers_the_missing_placeholder(self):
        assert "missing" in message_kinds.placeholders_for("subscription.missing")

    def test_the_i18n_keys_exist_in_both_catalogues(self):
        """The defaults are built from i18n, so the keys must be there."""
        import i18n

        for kind in message_kinds.KINDS:
            if not kind.id.startswith("subscription.") and not kind.id.startswith(
                "button.subscription_"
            ):
                continue
            for key in kind.i18n_keys:
                for lang in ("en", "ru"):
                    assert i18n.has(key, lang), f"{key} missing from {lang}"
