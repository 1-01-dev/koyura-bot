"""The Channels page: the master switch, the honest "not an admin" verdict, the numbers.

Four things the owner asked for, and each one is a test here:

* **the switch is first and it means what it says** — off is the default, off
  means "no gate even with channels stored", and the state is readable as words
  next to a real ``role="switch"`` control (no inline script: the CSP forbids it);
* **a channel the bot cannot verify is not a working channel** — it is stored,
  reported as "bot is not an administrator", and the page says in those words
  that the subscription check is impossible there;
* **a transition is written once** — the same verdict twice writes one row, and a
  ``left`` without a subscription before it is not a transition at all;
* **the period moves the numbers** — the KPI cells, the per-channel table and the
  chart all read the same window.

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "channels.db")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402

import db  # noqa: E402
import subscriptions  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csrf(c: TestClient | None = None) -> str:
    return (c or client).cookies.get("koyura_csrf") or ""


def _login(c: TestClient | None = None) -> TestClient:
    target = c or client
    page = target.get("/login")
    assert page.status_code == 200, page.text
    token = _csrf(target)
    response = target.post(
        "/login",
        data={
            "username": "admin",
            "password": "Koyura-Test-Passw0rd",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    target.get("/admin/channels")
    return target


@pytest.fixture(scope="module")
def admin() -> TestClient:
    return _login(client)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """An empty channel table and the gate off before every test."""
    asyncio.run(db.init_db())

    async def reset() -> None:
        # Channels *and* the history: the numbers below are counts of rows, so a
        # row left behind by an earlier test would be counted again.
        for row in await db.list_required_channels():
            await db.delete_required_channel(int(row["id"]))
        import aiosqlite

        async with aiosqlite.connect(os.environ["DB_PATH"]) as conn:
            await conn.execute("DELETE FROM subscriptions")
            await conn.commit()
        await db.set_subscriptions_enabled(False)

    asyncio.run(reset())
    # A channel check must never touch Telegram in a test. The route module
    # holds its own reference to the module, so patch the attribute it reads.
    monkeypatch.setattr(subscriptions, "check_channel", _fake_check)
    yield


async def _fake_check(username: str, *, timeout: float = 10.0) -> dict:
    """A stub probe: ``noadmin`` is the "bot is not an administrator" branch."""
    name = str(username).lstrip("@")
    if name == "missing":
        return {
            "ok": False,
            "error": subscriptions.NOT_FOUND,
            "detail": "channel not found",
            "title": "",
            "username": name,
        }
    is_admin = name != "noadmin"
    return {
        "ok": is_admin,
        "error": subscriptions.OK if is_admin else subscriptions.NOT_ADMIN,
        "detail": "" if is_admin else "the bot must be an administrator",
        "title": f"Channel {name}",
        "username": name,
    }


def _post(path: str, data: dict) -> Response:
    payload = dict(data)
    payload.setdefault("csrf_token", _csrf())
    return client.post(path, data=payload, follow_redirects=False)


def _add(reference: str) -> None:
    response = _post("/admin/channels/add", {"reference": reference})
    assert response.status_code == 303, response.text


def _channel_id(username: str) -> int:
    row = asyncio.run(db.find_required_channel(username))
    assert row is not None, f"@{username} was not stored"
    return int(row["id"])


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

class TestTheMasterSwitch:
    def test_the_gate_is_off_by_default(self):
        assert asyncio.run(db.subscriptions_enabled()) is False

    def test_the_switch_is_the_first_element_of_the_page(self, admin: TestClient):
        body = admin.get("/admin/channels").text
        assert body.index('id="gate-card"') < body.index('id="channel-add-card"')
        assert body.index('id="gate-card"') < body.index('id="channels-card"')

    def test_the_switch_is_a_real_switch_not_a_checkbox(self, admin: TestClient):
        body = admin.get("/admin/channels").text
        assert 'role="switch"' in body
        assert 'aria-checked="false"' in body
        assert 'id="gate-switch"' in body
        # A checkbox-* styled control would also render as <input type="checkbox">.
        switch = body.split('id="gate-switch"')[0].rsplit("<", 1)[-1]
        assert "input" not in switch

    def test_the_switch_state_is_spelled_out_in_words(self, admin: TestClient):
        body = admin.get("/admin/channels").text
        assert "Subscription gate is off" in body
        assert ">off<" in body

    def test_the_switch_needs_no_script(self, admin: TestClient):
        """Space/Enter come from the button; the CSP forbids an inline handler."""
        body = admin.get("/admin/channels").text
        assert "onclick=" not in body
        assert "<script>" not in body

    def test_turning_it_on_stores_the_setting(self, admin: TestClient):
        response = _post("/admin/channels/toggle", {"enabled": "1"})
        assert response.status_code == 303
        assert asyncio.run(db.subscriptions_enabled()) is True
        body = admin.get("/admin/channels").text
        assert 'aria-checked="true"' in body
        assert "Subscription gate is on" in body

    def test_turning_it_off_stores_the_setting(self, admin: TestClient):
        asyncio.run(db.set_subscriptions_enabled(True))
        response = _post("/admin/channels/toggle", {"enabled": "0"})
        assert response.status_code == 303
        assert asyncio.run(db.subscriptions_enabled()) is False

    def test_off_keeps_the_channel_list_usable(self, admin: TestClient):
        """An operator prepares the configuration before flipping the switch."""
        _add("https://t.me/koyura_news")
        body = admin.get("/admin/channels").text
        assert "Channel koyura_news" in body
        # ...and each row says it is inactive because the gate is off.
        assert "inactive" in body.lower()

    def test_switching_on_without_a_working_channel_says_so(self, admin: TestClient):
        response = _post("/admin/channels/toggle", {"enabled": "1"})
        assert response.status_code == 303
        body = admin.get("/admin/channels").text
        assert "not" in body.lower() and "in force" in body.lower()


# ---------------------------------------------------------------------------
# Adding and checking a channel
# ---------------------------------------------------------------------------

class TestAddingAChannel:
    def test_a_link_is_parsed_and_stored(self, admin: TestClient):
        _add("https://t.me/koyura_news")
        row = asyncio.run(db.find_required_channel("koyura_news"))
        assert row is not None
        assert row["title"] == "Channel koyura_news"
        assert row["link"] == "https://t.me/koyura_news"
        assert row["bot_is_admin"] is True

    def test_a_handle_is_accepted_too(self, admin: TestClient):
        _add("@koyura_chat")
        assert asyncio.run(db.find_required_channel("koyura_chat")) is not None

    def test_a_private_invite_link_is_refused(self, admin: TestClient):
        """A link that carries no username cannot be checked, so it is refused."""
        response = _post("/admin/channels/add", {"reference": "https://t.me/+AbCdEf"})
        assert response.status_code == 303
        body = admin.get(response.headers["location"]).text
        assert "not a public channel link" in body
        assert asyncio.run(db.list_required_channels()) == []

    def test_a_duplicate_is_refused(self, admin: TestClient):
        _add("https://t.me/koyura_news")
        response = _post("/admin/channels/add", {"reference": "@koyura_news"})
        body = admin.get(response.headers["location"]).text
        assert "already in the list" in body
        assert len(asyncio.run(db.list_required_channels())) == 1

    def test_the_new_channel_appears_in_the_table(self, admin: TestClient):
        _add("https://t.me/koyura_news")
        body = admin.get("/admin/channels").text
        assert "Channel koyura_news" in body
        assert "https://t.me/koyura_news" in body
        assert 'id="channel-table"' in body


class TestTheNotAnAdminVerdict:
    def test_a_channel_where_the_bot_is_not_admin_is_refused_in_words(
        self, admin: TestClient
    ):
        """The owner's rule: say it honestly, and do not count it as working."""
        _add("https://t.me/noadmin")
        row = asyncio.run(db.find_required_channel("noadmin"))
        assert row is not None
        assert row["bot_is_admin"] is False
        assert row["check_error"] == subscriptions.NOT_ADMIN

        body = admin.get("/admin/channels").text
        assert "bot is not an administrator" in body
        # The rule is spelled out on the page, not implied.
        assert "must be an administrator of the channel" in body

    def test_such_a_channel_is_never_reported_as_working(self):
        asyncio.run(
            db.add_required_channel(
                username="noadmin",
                link="https://t.me/noadmin",
                bot_is_admin=False,
                check_error=subscriptions.NOT_ADMIN,
            )
        )
        rows = asyncio.run(db.list_required_channels(enabled_only=True))
        assert [row["bot_is_admin"] for row in rows] == [False]

    def test_enabling_it_is_refused(self, admin: TestClient):
        _add("https://t.me/noadmin")
        channel_id = _channel_id("noadmin")
        response = _post(
            "/admin/channels/toggle-channel",
            {"channel_id": str(channel_id), "enabled": "0"},
        )
        assert response.status_code == 303
        # Now try to switch it back on: it cannot verify anyone.
        response = _post(
            "/admin/channels/toggle-channel",
            {"channel_id": str(channel_id), "enabled": "1"},
        )
        body = admin.get(response.headers["location"]).text
        assert "cannot be enabled" in body
        row = asyncio.run(db.get_required_channel(channel_id))
        assert row is not None and row["enabled"] is False

    def test_a_check_can_turn_a_channel_working(self, admin: TestClient):
        """Make the bot an admin in Telegram, then press Check: it goes green."""
        _add("https://t.me/noadmin")
        channel_id = _channel_id("noadmin")
        stored = asyncio.run(db.get_required_channel(channel_id))
        assert stored is not None
        assert stored["bot_is_admin"] is False

        async def now_admin(username: str, *, timeout: float = 10.0) -> dict:
            return {
                "ok": True,
                "error": subscriptions.OK,
                "detail": "",
                "title": "Now works",
                "username": str(username).lstrip("@"),
            }

        original = subscriptions.check_channel
        subscriptions.check_channel = now_admin
        try:
            _post("/admin/channels/check", {"channel_id": str(channel_id)})
        finally:
            subscriptions.check_channel = original

        row = asyncio.run(db.get_required_channel(channel_id))
        assert row is not None
        assert row["bot_is_admin"] is True
        assert row["check_error"] == ""


# ---------------------------------------------------------------------------
# The transition is written once
# ---------------------------------------------------------------------------

class TestTheHistoryIsIdempotent:
    def test_the_same_verdict_twice_writes_one_row(self):
        channel = asyncio.run(
            db.add_required_channel(username="koyura_news", bot_is_admin=True)
        )

        async def run() -> list[bool]:
            first = await db.record_subscription(42, channel["id"], "subscribed")
            second = await db.record_subscription(42, channel["id"], "subscribed")
            return [first, second]

        assert asyncio.run(run()) == [True, False]

    def test_a_left_needs_a_subscription_before_it(self):
        """A user nobody ever saw subscribed cannot "unsubscribe"."""
        channel = asyncio.run(
            db.add_required_channel(username="koyura_news", bot_is_admin=True)
        )
        assert asyncio.run(db.record_subscription(9, channel["id"], "left")) is False

    def test_a_real_transition_is_recorded_once(self):
        channel = asyncio.run(
            db.add_required_channel(username="koyura_news", bot_is_admin=True)
        )
        channel_id = int(channel["id"])

        async def run() -> None:
            await db.record_subscription(7, channel_id, "subscribed")
            await db.record_subscription(7, channel_id, "left")
            # The sweep re-posted the same verdict: not a second transition.
            await db.record_subscription(7, channel_id, "left")

        asyncio.run(run())
        stats = asyncio.run(db.get_subscription_stats("7d"))
        assert stats["subscribed"] == 1
        assert stats["left"] == 1

    def test_subscribe_again_after_leaving_is_a_new_transition(self):
        channel = asyncio.run(
            db.add_required_channel(username="koyura_news", bot_is_admin=True)
        )
        channel_id = int(channel["id"])

        async def run() -> None:
            await db.record_subscription(7, channel_id, "subscribed")
            await db.record_subscription(7, channel_id, "left")
            await db.record_subscription(7, channel_id, "subscribed")

        asyncio.run(run())
        stats = asyncio.run(db.get_subscription_stats("7d"))
        assert stats["subscribed"] == 2
        assert stats["left"] == 1

    def test_an_unknown_state_is_refused(self):
        with pytest.raises(ValueError):
            asyncio.run(db.record_subscription(1, 1, "maybe"))


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------

class TestTheStatistics:
    def _seed(self) -> int:
        """One working channel and four users: 3 subscribed, 1 of them left."""
        channel = asyncio.run(
            db.add_required_channel(username="koyura_news", bot_is_admin=True)
        )
        channel_id = int(channel["id"])

        async def run() -> None:
            for user_id in (1, 2, 3):
                await db.record_subscription(user_id, channel_id, "subscribed")
            await db.record_subscription(3, channel_id, "left")
            await db.record_subscription(4, channel_id, "subscribed")

        asyncio.run(run())
        return channel_id

    def test_the_kpi_cells_count_transitions(self, admin: TestClient):
        self._seed()
        stats = asyncio.run(db.get_subscription_stats("30d"))
        assert stats["subscribed"] == 4
        assert stats["left"] == 1
        # Users 1, 2 and 4 are still subscribed; 3 left.
        assert stats["active"] == 3

    def test_the_breakdown_is_per_channel(self, admin: TestClient):
        self._seed()
        stats = asyncio.run(db.get_subscription_stats("30d"))
        rows = stats["channels"]
        assert len(rows) == 1
        assert rows[0]["subscribed"] == 4
        assert rows[0]["left"] == 1
        assert rows[0]["active"] == 3
        assert rows[0]["link"] == "https://t.me/koyura_news"

    def test_the_page_renders_the_kpis_and_the_table(self, admin: TestClient):
        self._seed()
        body = admin.get("/admin/channels").text
        for element in ("kpi-subscribed", "kpi-left", "kpi-active",
                        "channel-stats-table", "subscriptions-chart"):
            assert f'id="{element}"' in body, element
        assert "https://t.me/koyura_news" in body

    def test_the_period_selector_offers_three_windows(self, admin: TestClient):
        body = admin.get("/admin/channels").text
        for key in ("7d", "30d", "all"):
            assert f"channel_period={key}" in body, key

    def test_the_period_changes_the_numbers(self, admin: TestClient):
        """A transition older than 7 days is in "all" but not in "7d"."""
        channel = asyncio.run(
            db.add_required_channel(username="koyura_news", bot_is_admin=True)
        )
        channel_id = int(channel["id"])

        async def run() -> None:
            await db.record_subscription(
                11, channel_id, "subscribed", changed_at="2020-01-01 10:00:00"
            )
            await db.record_subscription(12, channel_id, "subscribed")
            await db.record_subscription(12, channel_id, "left")

        asyncio.run(run())

        recent = asyncio.run(db.get_subscription_stats("7d"))
        everything = asyncio.run(db.get_subscription_stats("all"))
        assert recent["subscribed"] == 1
        assert everything["subscribed"] == 2
        assert recent["subscribed"] < everything["subscribed"]

    def test_the_period_reaches_the_page(self, admin: TestClient):
        body = admin.get("/admin/channels?channel_period=7d").text
        assert "Subscribed · 7d" in body
        assert 'aria-pressed="true"' in body

    def test_an_unknown_period_falls_back_to_thirty_days(self, admin: TestClient):
        body = admin.get("/admin/channels?channel_period=nonsense").text
        assert "Subscribed · 30d" in body

    def test_the_chart_series_has_both_datasets(self, admin: TestClient):
        self._seed()
        stats = asyncio.run(db.get_subscription_stats("7d"))
        chart = stats["chart"]
        assert len(chart["labels"]) == 7
        assert len(chart["subscribed"]) == 7
        assert len(chart["left"]) == 7
        assert sum(chart["subscribed"]) == 4
        assert sum(chart["left"]) == 1

    def test_a_channel_that_left_does_not_count_as_active(self, admin: TestClient):
        self._seed()
        stats = asyncio.run(db.get_subscription_stats("30d"))
        active_ids = asyncio.run(db.subscribed_user_ids(
            [int(row["id"]) for row in stats["channels"]]
        ))
        assert 3 not in active_ids
        assert {1, 2, 4} <= set(active_ids)

    def test_a_disabled_channel_is_out_of_the_breakdown(self, admin: TestClient):
        channel_id = self._seed()
        asyncio.run(db.set_required_channel_enabled(channel_id, False))
        stats = asyncio.run(db.get_subscription_stats("30d"))
        assert stats["channels"] == []


# ---------------------------------------------------------------------------
# Deleting and ordering
# ---------------------------------------------------------------------------

class TestOrderingAndDeleting:
    def test_a_new_channel_goes_last(self, admin: TestClient):
        _add("@channel_one")
        _add("@channel_two")
        rows = asyncio.run(db.list_required_channels())
        assert [row["username"] for row in rows] == ["channel_one", "channel_two"]
        assert [row["position"] for row in rows] == [0, 1]

    def test_a_move_renumbers_the_list(self, admin: TestClient):
        _add("@channel_one")
        _add("@channel_two")
        second = _channel_id("channel_two")
        _post("/admin/channels/move", {"channel_id": str(second), "direction": "up"})
        rows = asyncio.run(db.list_required_channels())
        assert [row["username"] for row in rows] == ["channel_two", "channel_one"]

    def test_a_move_at_the_edge_is_a_no_op(self, admin: TestClient):
        _add("@channel_one")
        first = _channel_id("channel_one")
        _post("/admin/channels/move", {"channel_id": str(first), "direction": "up"})
        rows = asyncio.run(db.list_required_channels())
        assert [row["username"] for row in rows] == ["channel_one"]

    def test_deleting_removes_the_channel(self, admin: TestClient):
        _add("@channel_one")
        channel_id = _channel_id("channel_one")
        _post("/admin/channels/delete", {"channel_id": str(channel_id)})
        assert asyncio.run(db.find_required_channel("channel_one")) is None

    def test_deleting_renumbers_what_is_left(self, admin: TestClient):
        _add("@channel_one")
        _add("@channel_two")
        first = _channel_id("channel_one")
        _post("/admin/channels/delete", {"channel_id": str(first)})
        rows = asyncio.run(db.list_required_channels())
        assert [row["position"] for row in rows] == [0]


# ---------------------------------------------------------------------------
# The gate the bot reads
# ---------------------------------------------------------------------------

class TestTheGateTheBotReads:
    def test_the_switch_travels_in_bot_settings(self, admin: TestClient):
        asyncio.run(db.set_subscriptions_enabled(True))
        payload = admin.get("/api/bot-settings").json()
        assert payload["subscriptions_enabled"] is True

    def test_bot_settings_says_off_by_default(self, admin: TestClient):
        payload = admin.get("/api/bot-settings").json()
        assert payload["subscriptions_enabled"] is False

    def test_only_a_verifiable_channel_travels(self, admin: TestClient):
        asyncio.run(
            db.add_required_channel(username="good", link="https://t.me/good",
                                    bot_is_admin=True)
        )
        asyncio.run(
            db.add_required_channel(username="noadmin", link="https://t.me/noadmin",
                                    bot_is_admin=False, check_error="not_admin")
        )
        asyncio.run(db.set_subscriptions_enabled(True))
        payload = admin.get("/api/subscriptions").json()
        assert payload["enabled"] is True
        assert [row["username"] for row in payload["channels"]] == ["good"]
        assert [row["username"] for row in payload["unusable"]] == ["noadmin"]

    def test_an_internal_endpoint_refuses_a_wrong_token(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "the-real-token")
        response = client.get(
            "/api/subscriptions", headers={"X-Internal-Token": "wrong"}
        )
        assert response.status_code == 401

    def test_the_event_endpoint_writes_a_transition(self, admin: TestClient):
        channel = asyncio.run(
            db.add_required_channel(username="good", bot_is_admin=True)
        )
        first = admin.post(
            "/api/subscriptions/events",
            json={"user_id": 5, "channel_id": int(channel["id"]), "state": "subscribed"},
        )
        assert first.status_code == 200
        assert first.json()["written"] is True
        second = admin.post(
            "/api/subscriptions/events",
            json={"user_id": 5, "channel_id": int(channel["id"]), "state": "subscribed"},
        )
        assert second.json()["written"] is False

    def test_the_event_endpoint_refuses_an_unknown_state(self, admin: TestClient):
        response = admin.post(
            "/api/subscriptions/events",
            json={"user_id": 5, "channel_id": 1, "state": "maybe"},
        )
        assert response.status_code == 400

    def test_the_active_users_list_is_ids_only(self, admin: TestClient):
        channel = asyncio.run(
            db.add_required_channel(username="good", bot_is_admin=True)
        )
        asyncio.run(db.set_subscriptions_enabled(True))
        asyncio.run(db.record_subscription(21, channel["id"], "subscribed"))
        payload = admin.get("/api/subscriptions/active-users").json()
        assert payload["user_ids"] == [21]

    def test_the_active_list_is_empty_when_the_gate_is_off(self, admin: TestClient):
        channel = asyncio.run(
            db.add_required_channel(username="good", bot_is_admin=True)
        )
        asyncio.run(db.record_subscription(21, channel["id"], "subscribed"))
        payload = admin.get("/api/subscriptions/active-users").json()
        assert payload["user_ids"] == []


# ---------------------------------------------------------------------------
# Parsing a channel reference
# ---------------------------------------------------------------------------

class TestParsingAReference:
    @pytest.mark.parametrize(
        "reference",
        [
            "https://t.me/koyura_news",
            "http://t.me/koyura_news",
            "t.me/koyura_news",
            "https://telegram.me/koyura_news",
            "https://t.me/koyura_news/17",
            "@koyura_news",
            "koyura_news",
            "  @Koyura_News  ",
        ],
    )
    def test_a_public_reference_gives_the_username(self, reference: str):
        assert subscriptions.parse_reference(reference) == (
            reference.strip().lstrip("@").replace("https://t.me/", "")
            .replace("http://t.me/", "").replace("t.me/", "")
            .replace("https://telegram.me/", "").split("/")[0]
        )

    @pytest.mark.parametrize(
        "reference",
        [
            "",
            "   ",
            "https://t.me/+AbCdEf",
            "https://t.me/joinchat/AbCdEf",
            "https://example.com/koyura_news",
            "@ab",
            "a b c",
        ],
    )
    def test_an_unusable_reference_gives_nothing(self, reference: str):
        assert subscriptions.parse_reference(reference) is None

    def test_the_link_is_canonical(self):
        assert subscriptions.channel_link("koyura_news") == (
            "https://t.me/koyura_news"
        )
        assert subscriptions.channel_link("@koyura_news") == (
            "https://t.me/koyura_news"
        )

    def test_the_verdict_has_a_sentence(self):
        assert subscriptions.check_error_label("") == "ok"
        assert "administrator" in subscriptions.check_error_label("not_admin")
        assert "not found" in subscriptions.check_error_label("not_found")


# ---------------------------------------------------------------------------
# The panel holds BOT_TOKEN: it must never reach a log line
# ---------------------------------------------------------------------------

class TestTheTokenStaysOutOfTheLogs:
    """The Channels page is the only dashboard code that talks to Telegram.

    ``getChat``/``getChatMember`` are called on a URL that carries the bot
    token, and ``httpx`` logs every request as that whole URL. The two guards
    are pinned here because losing either one puts the secret in journald.
    """

    def test_a_bot_api_url_is_redacted(self, monkeypatch):
        import logging

        import logging_setup

        monkeypatch.setenv(
            "BOT_TOKEN", "123456789:AAFakeSecretTokenValueThatIsLongEnough"
        )
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            (
                "HTTP Request: GET https://api.telegram.org/bot"
                "123456789:AAFakeSecretTokenValueThatIsLongEnough/getChat"
            ),
            (),
            None,
        )
        assert logging_setup.RedactSecrets().filter(record) is True
        assert "AAFakeSecretTokenValueThatIsLongEnough" not in record.getMessage()
        assert logging_setup.REDACTED in record.getMessage()

    def test_a_secret_passed_as_an_argument_is_redacted(self, monkeypatch):
        import logging

        import logging_setup

        monkeypatch.setenv(
            "BOT_TOKEN", "123456789:AAFakeSecretTokenValueThatIsLongEnough"
        )
        record = logging.LogRecord(
            "koyura.dashboard",
            logging.INFO,
            __file__,
            1,
            "checking channel with %s",
            ("123456789:AAFakeSecretTokenValueThatIsLongEnough",),
            None,
        )
        logging_setup.RedactSecrets().filter(record)
        assert "AAFakeSecretTokenValueThatIsLongEnough" not in record.getMessage()

    def test_the_noisy_loggers_are_pinned(self):
        import logging

        import logging_setup

        logging_setup.configure_logging(force=True)
        for name in ("httpx", "httpcore"):
            assert logging.getLogger(name).level >= logging.WARNING

    def test_the_panel_never_logs_the_exception_message_of_a_call(self):
        """An httpx error message carries the URL, and the URL the token."""
        source = (Path(__file__).resolve().parents[1] / "subscriptions.py").read_text()
        assert 'type(exc).__name__' in source
        assert 'logger.debug("Channel check for @%s failed: %s", name, exc)' not in source
