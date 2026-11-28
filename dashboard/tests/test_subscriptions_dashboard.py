"""The subscription statistics on the dashboard: the cells, the series, the gate.

The owner moved these numbers from Admin → Channels to ``/`` because that is
the page they read every day, and a second copy is a second answer. What this
module pins, in that order:

* **the period is the range switch** — ``1D``/``7D``/``1M``/``1Y`` reach both
  the downloads line and the subscription block through one request, so the
  page can never show two periods at once;
* **the three cells count transitions** — subscriptions and unsubscriptions in
  the window, active users right now;
* **the series has both datasets** and lines up with the window it is drawn in;
* **the breakdown is per channel**, and it is the total line only when a single
  channel would just repeat the cells;
* **the block is drawn while the gate is on**, zeros included — that is the
  module saying it works and nobody has joined — **or while the table holds any
  history at all**, even outside the selected window. Both at once (gate off,
  nothing ever recorded) hides it; one or the other is a panel.

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "subs_dashboard.db")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _login() -> TestClient:
    page = client.get("/login")
    assert page.status_code == 200
    token = client.cookies.get("koyura_csrf") or ""
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "Koyura-Test-Passw0rd",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture(scope="module")
def admin() -> TestClient:
    return _login()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No channels, no history, gate off before every test."""
    asyncio.run(db.init_db())

    async def reset() -> None:
        for row in await db.list_required_channels():
            await db.delete_required_channel(int(row["id"]))
        async with db._conn() as conn:  # noqa: SLF001 - the test owns the rows
            await conn.execute("DELETE FROM subscriptions")
            await conn.commit()
        await db.set_subscriptions_enabled(False)

    asyncio.run(reset())
    yield


def _channel(username: str = "koyura_news", *, admin_bot: bool = True) -> int:
    row = asyncio.run(
        db.add_required_channel(username=username, bot_is_admin=admin_bot)
    )
    return int(row["id"])


def _seed_two_channels() -> None:
    """Two working channels, five users; user 5 leaves the first one.

    4 subscriptions, 1 unsubscription, and 4 users still active everywhere.
    """
    first = _channel("koyura_news")
    second = _channel("koyura_chat")

    async def run() -> None:
        for user_id in (1, 2, 3, 4, 5):
            await db.record_subscription(user_id, first, "subscribed")
            await db.record_subscription(user_id, second, "subscribed")
        await db.record_subscription(5, first, "left")
        await db.set_subscriptions_enabled(True)

    asyncio.run(run())


def _stats(admin: TestClient, range_key: str = "1M") -> dict:
    payload = admin.get(f"/api/chart-data?range={range_key}").json()
    return payload["subscriptions"]


# ---------------------------------------------------------------------------
# The request: one window, one call
# ---------------------------------------------------------------------------

class TestOneRequestFeedsBothPanels:
    def test_the_subscription_block_travels_with_the_charts(self, admin: TestClient):
        _seed_two_channels()
        payload = admin.get("/api/chart-data?range=1M").json()
        # The downloads contract is untouched ...
        assert "labels" in payload and "values" in payload
        assert "platforms" in payload and "top_users" in payload
        # ... and one more key carries the subscription panel.
        assert "subscriptions" in payload
        block = payload["subscriptions"]
        for field in ("visible", "period", "subscribed", "left", "active",
                      "chart", "channels", "single_channel"):
            assert field in block, field

    @pytest.mark.parametrize("range_key", ["1D", "7D", "1M", "1Y"])
    def test_every_range_reaches_the_block(self, admin: TestClient, range_key: str):
        _seed_two_channels()
        block = _stats(admin, range_key)
        assert block["visible"] is True
        assert block["period"] == range_key.lower()

    def test_the_api_still_refuses_an_unknown_range(self, admin: TestClient):
        assert admin.get("/api/chart-data?range=2Y").status_code == 400

    def test_the_block_is_behind_the_session(self):
        anonymous = TestClient(app, raise_server_exceptions=True)
        assert anonymous.get("/api/chart-data?range=1M").status_code == 401


# ---------------------------------------------------------------------------
# The three cells
# ---------------------------------------------------------------------------

class TestTheKpiCells:
    @pytest.mark.parametrize("range_key", ["1D", "7D", "1M", "1Y"])
    def test_the_numbers_are_the_same_in_every_window(
        self, admin: TestClient, range_key: str
    ):
        """Every seeded transition just happened, so all four windows see it."""
        _seed_two_channels()
        block = _stats(admin, range_key)
        assert block["subscribed"] == 10  # five users × two channels
        assert block["left"] == 1
        assert block["active"] == 4  # user 5 left one channel

    def test_an_old_transition_leaves_the_short_window(self, admin: TestClient):
        channel = _channel()

        async def run() -> None:
            await db.record_subscription(
                11, channel, "subscribed", changed_at="2020-01-01 10:00:00"
            )
            await db.record_subscription(12, channel, "subscribed")
            await db.set_subscriptions_enabled(True)

        asyncio.run(run())

        assert _stats(admin, "7D")["subscribed"] == 1
        # "1Y" is a twelve-month label axis but the same kind of window: the
        # 2020 row is out of it too, and "all" is the only way to see it.
        assert _stats(admin, "1Y")["subscribed"] == 1
        assert asyncio.run(db.get_subscription_stats("all"))["subscribed"] == 2

    def test_active_needs_every_working_channel(self, admin: TestClient):
        working = _channel("koyura_news")
        _channel("koyura_chat")
        broken = _channel("noadmin", admin_bot=False)

        async def run() -> None:
            await db.record_subscription(21, working, "subscribed")
            # Subscribed to the channel the bot cannot verify, and to nothing
            # else: that must not count as active, and the broken channel must
            # not make "active" impossible to reach either.
            await db.record_subscription(22, broken, "subscribed")
            await db.set_subscriptions_enabled(True)

        asyncio.run(run())
        # User 21 is missing the second working channel; user 22 is only on the
        # broken one. Neither is active.
        assert _stats(admin)["active"] == 0


# ---------------------------------------------------------------------------
# The series
# ---------------------------------------------------------------------------

class TestTheSeries:
    @pytest.mark.parametrize(
        "range_key,expected_buckets",
        [("1D", 24), ("7D", 7), ("1M", 30), ("1Y", 12)],
    )
    def test_the_bucket_count_matches_the_downloads_line(
        self, admin: TestClient, range_key: str, expected_buckets: int
    ):
        _seed_two_channels()
        payload = admin.get(f"/api/chart-data?range={range_key}").json()
        # The two panels draw on the same x axis, label for label.
        assert payload["subscriptions"]["chart"]["labels"] == payload["labels"]
        assert len(payload["subscriptions"]["chart"]["labels"]) == expected_buckets

    @pytest.mark.parametrize("range_key", ["1D", "7D", "1M", "1Y"])
    def test_both_datasets_are_present_and_balanced(
        self, admin: TestClient, range_key: str
    ):
        _seed_two_channels()
        chart = _stats(admin, range_key)["chart"]
        assert len(chart["subscribed"]) == len(chart["labels"])
        assert len(chart["left"]) == len(chart["labels"])
        assert sum(chart["subscribed"]) == 10
        assert sum(chart["left"]) == 1

    def test_a_broken_timestamp_does_not_break_the_chart(self, admin: TestClient):
        channel = _channel()

        async def run() -> None:
            await db.record_subscription(
                31, channel, "subscribed", changed_at="not a timestamp"
            )
            await db.record_subscription(32, channel, "subscribed")
            await db.set_subscriptions_enabled(True)

        asyncio.run(run())
        chart = _stats(admin)["chart"]
        # The malformed row is skipped, never counted into a bucket of its own.
        assert sum(chart["subscribed"]) == 1


# ---------------------------------------------------------------------------
# The breakdown
# ---------------------------------------------------------------------------

class TestTheBreakdown:
    def test_two_channels_get_one_row_each(self, admin: TestClient):
        _seed_two_channels()
        block = _stats(admin)
        assert block["single_channel"] is None
        assert len(block["channels"]) == 2
        first = block["channels"][0]
        assert first["subscribed"] == 5
        assert first["left"] == 1
        assert first["active"] == 4
        assert first["link"] == "https://t.me/koyura_news"

    def test_one_channel_is_the_total_line_only(self, admin: TestClient):
        channel = _channel()

        async def run() -> None:
            await db.record_subscription(41, channel, "subscribed")
            await db.set_subscriptions_enabled(True)

        asyncio.run(run())
        block = _stats(admin)
        assert block["single_channel"] is not None
        assert block["single_channel"]["username"] == "koyura_news"

    def test_a_disabled_channel_leaves_the_breakdown(self, admin: TestClient):
        _seed_two_channels()
        first = asyncio.run(db.find_required_channel("koyura_news"))
        assert first is not None, "the seeded channel is missing"
        asyncio.run(db.set_required_channel_enabled(int(first["id"]), False))
        block = _stats(admin)
        assert [row["username"] for row in block["channels"]] == ["koyura_chat"]


# ---------------------------------------------------------------------------
# Visibility: the gate on, or something to read
# ---------------------------------------------------------------------------

class TestTheBlockHides:
    """Visible when the gate is on, or when the table has any history at all.

    The gate being on is enough: three cells reading 0 are how the module says
    "I work and nobody has joined yet". History is enough on its own: the
    operator switched the gate off, but the numbers are real and worth reading.
    Only both at once — gate off, nothing ever recorded — is not a panel at all.
    """

    def test_the_gate_on_draws_it_even_with_zeros(self, admin: TestClient):
        _channel()
        asyncio.run(db.set_subscriptions_enabled(True))
        block = _stats(admin)
        assert block["visible"] is True
        assert block["subscribed"] == 0
        assert block["left"] == 0
        assert block["active"] == 0
        # An empty window still carries the axis, so the chart can draw "0".
        assert block["chart"]["labels"]
        assert not any(block["chart"]["subscribed"])

    def test_the_gate_on_without_a_channel_draws_it_too(self, admin: TestClient):
        asyncio.run(db.set_subscriptions_enabled(True))
        assert _stats(admin)["visible"] is True

    def test_history_keeps_it_while_the_gate_is_off(self, admin: TestClient):
        channel = _channel()

        async def run() -> None:
            await db.record_subscription(51, channel, "subscribed")
            await db.set_subscriptions_enabled(False)

        asyncio.run(run())
        block = _stats(admin)
        assert block["visible"] is True
        assert block["subscribed"] == 1

    def test_an_off_gate_and_an_empty_table_hide_it(self, admin: TestClient):
        _channel()
        assert _stats(admin)["visible"] is False

    def test_no_channel_and_no_history_hides_it(self, admin: TestClient):
        assert _stats(admin)["visible"] is False

    def test_a_window_without_transitions_still_shows_the_block(
        self, admin: TestClient
    ):
        """The gate is on, and the history is older than the window: 0 and 0.

        That is a real answer for this week, not a missing panel — the numbers
        are still there when the range button moves to 1Y (which also holds
        nothing) or to a window that does.
        """
        channel = _channel()

        async def run() -> None:
            await db.record_subscription(
                61, channel, "subscribed", changed_at="2020-01-01 10:00:00"
            )
            await db.set_subscriptions_enabled(True)

        asyncio.run(run())
        block = _stats(admin, "7D")
        assert block["visible"] is True
        assert block["subscribed"] == 0
        assert block["period"] == "7d"

    def test_an_old_history_shows_up_outside_the_gate(self, admin: TestClient):
        """Gate off, one 2020 transition: the block is drawn, the period is 0."""
        channel = _channel()

        async def run() -> None:
            await db.record_subscription(
                62, channel, "subscribed", changed_at="2020-01-01 10:00:00"
            )
            await db.set_subscriptions_enabled(False)

        asyncio.run(run())
        assert _stats(admin, "7D")["visible"] is True

    def test_the_hidden_block_still_carries_its_shape(self, admin: TestClient):
        """A caller reading the JSON never has to test for a missing key."""
        block = _stats(admin)
        assert block["visible"] is False
        assert block["chart"]["labels"] and block["chart"]["subscribed"]
        assert block["channels"] == []


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

class TestTheDashboardPage:
    def test_the_page_carries_the_block_and_its_hooks(self, admin: TestClient):
        body = admin.get("/").text
        for element in ("subscriptions-block", "kpi-subscribed", "kpi-left",
                        "kpi-active-subscribers", "subscriptions-chart",
                        "subscriptions-by-channel", "subscriptions-breakdown"):
            assert f'id="{element}"' in body, element

    def test_the_block_starts_hidden(self, admin: TestClient):
        """Rendered hidden: a flash of three dash placeholders before the first
        fetch is exactly the empty panel this change removes."""
        body = admin.get("/").text
        start = body.index('id="subscriptions-block"')
        assert "hidden" in body[start - 40:start + 40]

    def test_the_page_never_adds_a_second_period_switch(self, admin: TestClient):
        body = admin.get("/").text
        # The four range buttons of the charts, and nothing else like them.
        assert body.count('class="range-btn') == 4

    def test_the_error_cell_keeps_the_only_filled_accent(self, admin: TestClient):
        """Two filled cells on one page cancel each other out.

        The subscription count leads its row with a wash and a rule instead, so
        the error rate stays the only red fill and keeps meaning "act on this".
        """
        body = admin.get("/").text
        assert body.count("kpi-card--accent") == 1
        assert "kpi-card--lead" in body

    def test_the_module_owns_the_block(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "static" / "js" / "subscriptions.js"
        ).read_text()
        assert "export function createSubscriptionPanel" in source
        assert "kpi-active-subscribers" in source
        # Visibility is the API's decision, not a client-side guess.
        assert "visible" in source
