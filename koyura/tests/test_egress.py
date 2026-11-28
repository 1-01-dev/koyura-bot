"""The engine's egress: the connection list, the cache, ``--proxy`` and `blocked`.

What these tests pin is the part that only shows up in production:

* the panel's answer is read **tolerantly** — an unknown kind, a URL with a
  scheme yt-dlp cannot dial, or no panel at all means "direct", never a failed
  job;
* **the first usable connection wins**: enabled, its rule covers the service,
  and its last check did not fail; when only failed ones are left the first of
  those is the last chance;
* the assignment rule decides **per service**, in all three shapes, using the
  same extractor table the cookie lookup uses;
* a WireGuard row dials the SOCKS address in ``status.json``, and a tunnel that
  is not up is skipped rather than turned into a broken proxy;
* a password never arrives over the wire: it is read from
  ``EGRESS_PATH/secrets/<id>.txt`` when the URL is dialled;
* the ``--proxy`` flag reaches the real yt-dlp argument vector, before the URL;
* a datacentre block (`blocked`, `429`, `captcha`, Reddit's API 403) is
  classified as ``blocked`` — before ``auth``, because another cookie jar
  answers the same 403 — and ends the job with ``service_blocked``.

The panel is never reached: ``KOYURA_DASHBOARD_URL`` points at a closed port.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token-0123456789abcdef")
os.environ["DOWNLOADS_PATH"] = tempfile.mkdtemp()
# ``COOKIES_PATH`` is deliberately not touched: ``test_cookies.py`` owns that
# variable for the whole session, and a second module assigning it would point
# every cookie test at the wrong tree.
# Never let a test reach a real panel.
os.environ["KOYURA_DASHBOARD_URL"] = "http://127.0.0.1:9"

import pytest  # noqa: E402

import app as koyura_app  # noqa: E402
import cookies as cookie_store  # noqa: E402
import egress  # noqa: E402

TOKEN = os.environ["INTERNAL_API_TOKEN"]
HEADERS = {"X-Internal-Token": TOKEN}

#: What the panel publishes: the URL as stored, with no login in it.
PANEL_PROXY = "socks5h://10.0.0.5:1080"
#: The credential the panel keeps in ``secrets/1.txt``, percent-encoded.
CREDENTIAL = "user:secret"
#: What the engine dials once it has read that file.
PROXY = f"socks5h://{CREDENTIAL}@{PANEL_PROXY.split('://', 1)[1]}"
SECOND = "http://10.0.0.9:3128"
SOCKS = "10.8.0.2:11080"

#: The exact strings the task recorded on the server.
REDDIT_BLOCKED = (
    "ERROR: [Reddit] reddit.com:456: Unable to download webpage: "
    "HTTP Error 403: Blocked (caused by HTTPError()); "
    "please report this issue on https://github.com/yt-dlp/yt-dlp/issues "
    "(Your IP address is unable to access the Reddit API)"
)
BLOCKED_403 = "ERROR: [Reddit] Unable to download JSON metadata: HTTP Error 403: Blocked"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path) -> Iterator[Path]:
    """A clean rule, cache, egress tree and job table for every test."""
    root = tmp_path / "egress"
    (root / "secrets").mkdir(parents=True)
    monkeypatch.setenv("EGRESS_PATH", str(root))
    egress.reset_cache()
    cookie_store.reset_registry_cache()
    koyura_app.jobs.clear()
    yield root
    egress.reset_cache()
    cookie_store.reset_registry_cache()
    koyura_app.jobs.clear()


def _secret(root: Path, credential: str = CREDENTIAL, connection_id: int = 1) -> None:
    """Write the panel's password file (0600, like the panel writes it)."""
    path = root / "secrets" / f"{connection_id}.txt"
    path.write_text(credential + "\n")
    path.chmod(0o600)


def _tunnel(root: Path, **payload: object) -> None:
    """Write the watcher's ``status.json``."""
    (root / "status.json").write_text(json.dumps(payload))


def _connection(**overrides: object) -> dict:
    """One entry of the panel's list, with the documented defaults."""
    entry: dict = {
        "id": 1,
        "name": "Primary",
        "kind": "socks5h",
        "url": PANEL_PROXY,
        "assignment": {"mode": "all", "services": []},
        "enabled": True,
        "status": "unknown",
    }
    entry.update(overrides)
    return entry


def _payload(*connections: object, test_interval: int = 0) -> dict:
    return {
        "connections": list(connections) or [_connection()],
        "test_interval": test_interval,
    }


@pytest.fixture()
def panel() -> Iterator:
    """A throwaway panel answering ``/api/egress``; returns its base URL.

    The address comes from the environment, so a test can only prove that the
    *right* variable is read by letting the module dial a real socket: a closed
    port answers ``off``, the stub answers the rule it was given.
    """
    started: list[ThreadingHTTPServer] = []

    def _serve(payload: dict | None = None) -> str:
        answered = payload if payload is not None else _payload()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's own name
                body = json.dumps(answered).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's signature
                """Stay quiet: pytest owns the output here."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield _serve
    for server in started:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Reading the panel's answer
# ---------------------------------------------------------------------------

class TestRuleParsing:
    def test_the_documented_shape_is_read(self):
        rule = egress.Rule.from_payload(
            _payload(
                _connection(
                    assignment={"mode": "selected", "services": ["reddit"]}
                )
            )
        )
        assert len(rule.connections) == 1
        connection = rule.connections[0]
        assert connection.id == 1
        assert connection.kind == "socks5h"
        assert connection.url == PANEL_PROXY
        assert connection.assignment_mode == "selected"
        assert connection.services == ("reddit",)

    def test_an_unreadable_payload_is_a_direct_connection(self):
        for payload in (None, [], "manual", 3, {}, {"connections": "one"}):
            rule = egress.Rule.from_payload(payload)
            assert rule.connections == (), payload
            assert rule.proxy_for("reddit") == ""

    def test_an_unknown_kind_is_dropped(self):
        rule = egress.Rule.from_payload(_payload(_connection(kind="tunnel")))
        assert rule.connections == ()

    def test_an_unknown_scheme_is_dropped(self):
        rule = egress.Rule.from_payload(
            _payload(_connection(kind="http", url="ftp://10.0.0.5:1"))
        )
        assert rule.connections[0].url == ""
        assert rule.proxy_for("reddit") == ""

    def test_a_url_with_whitespace_is_dropped(self):
        rule = egress.Rule.from_payload(
            _payload(_connection(url="socks5h://x y:1"))
        )
        assert rule.connections[0].url == ""

    @pytest.mark.parametrize("scheme", ["socks5", "socks5h", "http", "https"])
    def test_all_four_schemes_survive(self, scheme: str):
        url = f"{scheme}://10.0.0.5:1080"
        rule = egress.Rule.from_payload(
            _payload(_connection(kind=scheme, url=url))
        )
        assert rule.connections[0].url == url

    def test_an_unknown_assignment_mode_becomes_all(self):
        rule = egress.Rule.from_payload(
            _payload(_connection(assignment={"mode": "some", "services": []}))
        )
        assert rule.connections[0].assignment_mode == "all"

    def test_a_missing_assignment_block_becomes_all(self):
        entry = _connection()
        entry.pop("assignment")
        rule = egress.Rule.from_payload(_payload(entry))
        assert rule.connections[0].assignment_mode == "all"

    def test_an_unknown_status_becomes_unknown(self):
        rule = egress.Rule.from_payload(_payload(_connection(status="half-up")))
        assert rule.connections[0].status == "unknown"

    def test_a_disabled_entry_is_read_as_disabled(self):
        rule = egress.Rule.from_payload(_payload(_connection(enabled=False)))
        assert rule.connections[0].enabled is False

    def test_a_wireguard_entry_ignores_any_url(self):
        """Its proxy is the tunnel's SOCKS address, not a stale URL."""
        rule = egress.Rule.from_payload(
            _payload(_connection(kind="wireguard", url=PANEL_PROXY))
        )
        assert rule.connections[0].url == ""

    def test_an_entry_that_is_not_an_object_is_dropped(self):
        rule = egress.Rule.from_payload(_payload("socks5h", _connection()))
        assert len(rule.connections) == 1

    def test_the_test_interval_is_carried_through(self):
        rule = egress.Rule.from_payload(_payload(test_interval=30))
        assert rule.test_interval == 30
        assert rule.as_payload()["test_interval"] == 30


# ---------------------------------------------------------------------------
# The assignment rule
# ---------------------------------------------------------------------------

class TestAssignment:
    def test_all_covers_every_service(self):
        rule = egress.Rule.from_payload(_payload())
        for service in ("reddit", "youtube", "", "anything"):
            assert rule.connections[0].applies_to(service) is True

    def test_selected_covers_only_the_list(self):
        rule = egress.Rule.from_payload(
            _payload(_connection(assignment={"mode": "selected", "services": ["reddit"]}))
        )
        assert rule.connections[0].applies_to("reddit") is True
        assert rule.connections[0].applies_to("youtube") is False
        assert rule.connections[0].applies_to("") is False

    def test_except_covers_everything_but_the_list(self):
        rule = egress.Rule.from_payload(
            _payload(_connection(assignment={"mode": "except", "services": ["youtube"]}))
        )
        assert rule.connections[0].applies_to("reddit") is True
        assert rule.connections[0].applies_to("youtube") is False
        assert rule.connections[0].applies_to("") is True

    def test_no_connections_never_covers_anything(self):
        rule = egress.Rule.off()
        assert rule.applies_to("reddit") is False
        assert rule.proxy_for("reddit") == ""

    def test_the_service_name_is_normalised(self):
        rule = egress.Rule.from_payload(
            _payload(_connection(assignment={"mode": "selected", "services": ["reddit"]}))
        )
        assert rule.connections[0].applies_to("Reddit") is True
        assert rule.connections[0].applies_to(" reddit ") is True

    def test_a_disabled_connection_is_not_a_candidate(self):
        rule = egress.Rule.from_payload(_payload(_connection(enabled=False)))
        assert rule.applies_to("reddit") is False
        assert rule.proxy_for("reddit") == ""


# ---------------------------------------------------------------------------
# Which connection is dialled
# ---------------------------------------------------------------------------

class TestSelection:
    def test_the_first_healthy_connection_wins(self, _fresh):
        _secret(_fresh, "one:pass", 1)
        _secret(_fresh, "two:pass", 2)
        rule = egress.Rule.from_payload(
            _payload(
                _connection(id=1, status="unknown"),
                _connection(id=2, url=SECOND, kind="http", status="ok"),
            )
        )
        assert rule.proxy_for("reddit") == "socks5h://one:pass@10.0.0.5:1080"

    def test_a_failed_connection_is_skipped(self, _fresh):
        _secret(_fresh, "one:pass", 1)
        _secret(_fresh, "two:pass", 2)
        rule = egress.Rule.from_payload(
            _payload(
                _connection(id=1, status="fail"),
                _connection(id=2, url=SECOND, kind="http", status="unknown"),
            )
        )
        assert rule.proxy_for("reddit") == "http://two:pass@10.0.0.9:3128"

    def test_when_only_failed_ones_are_left_the_first_is_the_last_chance(
        self, _fresh
    ):
        _secret(_fresh, "one:pass", 1)
        _secret(_fresh, "two:pass", 2)
        rule = egress.Rule.from_payload(
            _payload(
                _connection(id=1, status="fail"),
                _connection(id=2, url=SECOND, kind="http", status="fail"),
            )
        )
        assert rule.proxy_for("reddit") == "socks5h://one:pass@10.0.0.5:1080"

    def test_a_disabled_connection_is_skipped(self, _fresh):
        _secret(_fresh, "one:pass", 1)
        _secret(_fresh, "two:pass", 2)
        rule = egress.Rule.from_payload(
            _payload(
                _connection(id=1, enabled=False),
                _connection(id=2, url=SECOND, kind="http"),
            )
        )
        assert rule.proxy_for("reddit") == "http://two:pass@10.0.0.9:3128"

    def test_the_rule_decides_which_connection_answers(self, _fresh):
        _secret(_fresh, "one:pass", 1)
        _secret(_fresh, "two:pass", 2)
        rule = egress.Rule.from_payload(
            _payload(
                _connection(
                    id=1, assignment={"mode": "selected", "services": ["youtube"]}
                ),
                _connection(id=2, url=SECOND, kind="http"),
            )
        )
        assert rule.proxy_for("reddit") == "http://two:pass@10.0.0.9:3128"
        assert rule.proxy_for("youtube") == "socks5h://one:pass@10.0.0.5:1080"

    def test_a_connection_with_no_url_is_not_dialled(self):
        rule = egress.Rule.from_payload(
            _payload(_connection(url=""), _connection(id=2, url=SECOND, kind="http"))
        )
        assert rule.proxy_for("reddit") == SECOND

    def test_no_match_means_direct(self):
        rule = egress.Rule.from_payload(
            _payload(
                _connection(assignment={"mode": "selected", "services": ["youtube"]})
            )
        )
        assert rule.proxy_for("reddit") == ""

    def test_the_password_is_read_from_the_secrets_tree(self, _fresh):
        """The panel never sends it: the engine reads ``secrets/<id>.txt``."""
        _secret(_fresh, "user:secret", 1)
        rule = egress.Rule.from_payload(_payload())
        assert rule.proxy_for("reddit") == PROXY
        assert (PANEL_PROXY.split("://", 1)[1]) in rule.proxy_for("reddit")

    def test_no_secret_file_means_no_login(self):
        rule = egress.Rule.from_payload(_payload())
        assert rule.proxy_for("reddit") == PANEL_PROXY

    def test_a_url_that_already_has_a_login_is_not_rewritten(self, _fresh):
        _secret(_fresh, "user:secret", 1)
        rule = egress.Rule.from_payload(
            _payload(_connection(url=PROXY))
        )
        assert rule.proxy_for("reddit") == PROXY

    def test_wireguard_answers_with_the_tunnel(self, _fresh):
        _tunnel(_fresh, state="up", socks=SOCKS)
        egress.reset_cache()
        rule = egress.Rule.from_payload(
            _payload(_connection(kind="wireguard", url=""))
        )
        assert rule.proxy_for("reddit") == f"socks5h://{SOCKS}"

    def test_a_dead_tunnel_is_skipped_for_the_next_connection(self, _fresh):
        """A dead tunnel must never become a half-configured proxy."""
        _tunnel(_fresh, state="down")
        egress.reset_cache()
        rule = egress.Rule.from_payload(
            _payload(
                _connection(id=1, kind="wireguard", url=""),
                _connection(id=2, url=SECOND, kind="http"),
            )
        )
        assert rule.proxy_for("reddit") == SECOND

    def test_a_dead_tunnel_alone_means_direct(self, _fresh):
        _tunnel(_fresh, state="error", error="handshake failed")
        egress.reset_cache()
        rule = egress.Rule.from_payload(
            _payload(_connection(kind="wireguard", url=""))
        )
        assert rule.proxy_for("reddit") == ""

    def test_the_tunnel_status_is_cached(self, _fresh):
        _tunnel(_fresh, state="up", socks=SOCKS)
        egress.reset_cache()
        assert egress.wireguard_socks() == f"socks5h://{SOCKS}"
        # A rewrite inside the TTL is not re-read: 45 s is the contract.
        _tunnel(_fresh, state="down")
        assert egress.wireguard_socks() == f"socks5h://{SOCKS}"

    def test_a_broken_status_file_is_down(self, _fresh):
        (_fresh / "status.json").write_text("{not json")
        egress.reset_cache()
        assert egress.wireguard_socks() == ""


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_an_unreachable_panel_means_direct(self):
        assert egress.rule().connections == ()

    def test_a_fetched_rule_is_cached(self, monkeypatch):
        calls: list[int] = []

        def _fetch():
            calls.append(1)
            return egress.Rule.from_payload(_payload())

        monkeypatch.setattr(egress, "_fetch", _fetch)
        first = egress.rule()
        second = egress.rule()
        assert len(first.connections) == 1
        assert second is first
        assert len(calls) == 1, "a second call inside the TTL must not refetch"

    def test_a_failed_fetch_keeps_the_last_good_rule(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_fetch", lambda: egress.Rule.from_payload(_payload())
        )
        assert len(egress.rule().connections) == 1
        # Expire the cache, then make the panel vanish.
        monkeypatch.setattr(egress, "_expires", 0.0)
        monkeypatch.setattr(egress, "_fetch", lambda: None)
        assert len(egress.rule().connections) == 1, (
            "a panel restart must not reroute jobs"
        )

    def test_a_fetched_list_is_cached_for_the_whole_ttl(self, monkeypatch):
        calls: list[int] = []

        def _fetch():
            calls.append(1)
            return egress.Rule.from_payload(_payload())

        monkeypatch.setattr(egress, "_fetch", _fetch)
        egress.rule()
        egress.rule()
        egress.rule()
        assert len(calls) == 1

    def test_reset_forces_a_refetch(self, monkeypatch):
        """``reset_cache`` drops the answer; the next call asks the panel again.

        It does not pin an empty list: the panel is the authority, so a reset
        followed by a reachable panel must show that panel's list, not a direct
        fallback.
        """
        calls: list[int] = []

        def _fetch():
            calls.append(1)
            return egress.Rule.from_payload(_payload())

        monkeypatch.setattr(egress, "_fetch", _fetch)
        assert len(egress.rule().connections) == 1
        egress.reset_cache()
        assert len(egress.rule().connections) == 1
        assert len(calls) == 2, "the cache was dropped, so the panel was asked again"

    def test_reset_without_a_panel_falls_back_to_direct(self):
        egress.reset_cache()
        assert egress.rule().connections == ()

    def test_the_ttl_is_under_a_minute(self):
        assert 30 <= egress.CACHE_TTL_SECONDS <= 60

    def test_describe_counts_the_states_but_never_the_url(self):
        rule = egress.Rule.from_payload(
            _payload(
                _connection(id=1, status="ok"),
                _connection(id=2, url=SECOND, kind="http", status="fail"),
            )
        )
        described = rule.describe()
        assert "2/2 enabled" in described
        assert "ok:1" in described
        assert "fail:1" in described
        assert "secret" not in described
        assert PANEL_PROXY not in described
        assert SECOND not in described

    def test_describe_of_an_empty_list_says_off(self):
        assert egress.Rule.off().describe() == "off"


# ---------------------------------------------------------------------------
# Which panel is asked
# ---------------------------------------------------------------------------

class TestPanelAddress:
    """The incident: the engine read a name the native install never sets.

    ``KOYURA_DASHBOARD_URL`` is this service's own name and nothing but compose
    could have set it; every ``/etc/koyura/koyura.env`` names the panel
    ``DASHBOARD_URL``. The engine therefore asked ``http://dashboard:8080``,
    failed, and kept ``off`` without a word.
    """

    def test_the_engine_name_wins_over_the_shared_one(self, monkeypatch):
        monkeypatch.setenv("KOYURA_DASHBOARD_URL", "http://127.0.0.1:8090")
        monkeypatch.setenv("DASHBOARD_URL", "http://127.0.0.1:8080")
        assert egress.dashboard_url() == "http://127.0.0.1:8090"
        assert egress.dashboard_source() == "KOYURA_DASHBOARD_URL"

    def test_the_shared_name_is_read_when_the_engine_one_is_absent(self, monkeypatch):
        monkeypatch.delenv("KOYURA_DASHBOARD_URL", raising=False)
        monkeypatch.setenv("DASHBOARD_URL", "http://127.0.0.1:8090/")
        # The trailing slash would double up with ``/api/egress``.
        assert egress.dashboard_url() == "http://127.0.0.1:8090"
        assert egress.dashboard_source() == "DASHBOARD_URL"

    def test_the_compose_host_is_the_last_resort(self, monkeypatch):
        for name in egress.DASHBOARD_URL_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        assert egress.dashboard_url() == "http://dashboard:8080"
        assert egress.dashboard_source() == "default"

    def test_a_native_panel_address_is_actually_used(self, monkeypatch, panel):
        """Only ``DASHBOARD_URL`` set: the list the panel holds must apply."""
        monkeypatch.delenv("KOYURA_DASHBOARD_URL", raising=False)
        monkeypatch.setenv("DASHBOARD_URL", panel())
        egress.reset_cache()
        assert len(egress.rule().connections) == 1
        assert egress.proxy_for_url("https://www.reddit.com/r/x") == PANEL_PROXY

    def test_the_engine_name_still_wins_end_to_end(self, monkeypatch, panel):
        """Both set: the panel at ``KOYURA_DASHBOARD_URL`` is the one dialled."""
        monkeypatch.setenv("KOYURA_DASHBOARD_URL", panel())
        # A closed port, so reading the wrong name cannot look like success.
        monkeypatch.setenv("DASHBOARD_URL", "http://127.0.0.1:9")
        egress.reset_cache()
        assert len(egress.rule().connections) == 1


# ---------------------------------------------------------------------------
# The log lines that make a silent panel visible
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def _lines(self, caplog, marker: str, level: int) -> list[logging.LogRecord]:
        return [
            record for record in caplog.records
            if marker in record.getMessage() and record.levelno >= level
        ]

    def test_the_panel_address_is_named_once_at_startup(self, monkeypatch, caplog):
        monkeypatch.delenv("DASHBOARD_URL", raising=False)
        monkeypatch.setenv("KOYURA_DASHBOARD_URL", "http://127.0.0.1:8090")
        with caplog.at_level(logging.INFO, logger="koyura.engine.egress"):
            egress.log_configuration()
            egress.log_configuration()
        lines = self._lines(caplog, "Egress panel:", logging.INFO)
        assert len(lines) == 1, "one line per process, not one per request"
        assert "http://127.0.0.1:8090" in lines[0].getMessage()
        assert "KOYURA_DASHBOARD_URL" in lines[0].getMessage()

    def test_the_default_address_says_it_is_the_default(self, monkeypatch, caplog):
        for name in egress.DASHBOARD_URL_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        with caplog.at_level(logging.INFO, logger="koyura.engine.egress"):
            egress.log_configuration()
        line = self._lines(caplog, "Egress panel:", logging.INFO)[0].getMessage()
        assert "http://dashboard:8080" in line
        assert "default" in line

    def test_three_unreadable_polls_in_a_row_warn(self, monkeypatch, caplog):
        """Two misses are a restart; three are a panel the engine cannot see."""
        monkeypatch.delenv("DASHBOARD_URL", raising=False)
        monkeypatch.setenv("KOYURA_DASHBOARD_URL", "http://127.0.0.1:9")
        with caplog.at_level(logging.WARNING, logger="koyura.engine.egress"):
            for expected in (0, 0, 1):
                monkeypatch.setattr(egress, "_expires", 0.0)
                assert egress.rule().connections == ()
                warnings = self._lines(caplog, "could not be read", logging.WARNING)
                assert len(warnings) == expected
        message = self._lines(caplog, "could not be read", logging.WARNING)[0].getMessage()
        assert "http://127.0.0.1:9" in message
        assert "KOYURA_DASHBOARD_URL" in message

    def test_a_recovered_panel_is_noted_and_stops_the_warning(
        self, monkeypatch, caplog, panel
    ):
        monkeypatch.delenv("DASHBOARD_URL", raising=False)
        monkeypatch.setenv("KOYURA_DASHBOARD_URL", "http://127.0.0.1:9")
        with caplog.at_level(logging.INFO, logger="koyura.engine.egress"):
            for _ in range(egress.FAILURE_WARN_THRESHOLD):
                monkeypatch.setattr(egress, "_expires", 0.0)
                egress.rule()
            assert len(self._lines(caplog, "could not be read", logging.WARNING)) == 1

            monkeypatch.setenv("KOYURA_DASHBOARD_URL", panel())
            monkeypatch.setattr(egress, "_expires", 0.0)
            assert len(egress.rule().connections) == 1
            assert self._lines(caplog, "answers again", logging.INFO), (
                "a recovered panel has to be visible too"
            )

            # And the counter starts over: no warning on the next miss.
            monkeypatch.setenv("KOYURA_DASHBOARD_URL", "http://127.0.0.1:9")
            monkeypatch.setattr(egress, "_expires", 0.0)
            egress.rule()
            assert len(self._lines(caplog, "could not be read", logging.WARNING)) == 1


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

class TestEndpoint:
    def test_it_answers_the_list(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_fetch", lambda: egress.Rule.from_payload(_payload())
        )
        client = koyura_app.app.test_client()
        response = client.get("/api/egress", headers=HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert set(body) == {"connections", "test_interval"}
        assert body["connections"][0]["url"] == PANEL_PROXY
        assert body["connections"][0]["kind"] == "socks5h"
        assert body["connections"][0]["status"] == "unknown"

    def test_it_never_answers_a_login(self, monkeypatch, _fresh):
        """The credential lives in the secrets tree, not in the answer."""
        _secret(_fresh, "user:topsecret", 1)
        monkeypatch.setattr(
            egress, "_fetch", lambda: egress.Rule.from_payload(_payload())
        )
        body = koyura_app.app.test_client().get(
            "/api/egress", headers=HEADERS
        ).get_json()
        assert "topsecret" not in json.dumps(body)
        assert "user:" not in body["connections"][0]["url"]

    def test_it_needs_the_internal_token(self, monkeypatch):
        monkeypatch.setattr(
            koyura_app, "INTERNAL_API_AUTH_DISABLED", False, raising=False
        )
        client = koyura_app.app.test_client()
        assert client.get("/api/egress").status_code == 401

    def test_an_empty_list_answers_the_documented_shape(self):
        body = koyura_app.app.test_client().get(
            "/api/egress", headers=HEADERS
        ).get_json()
        assert body == {"connections": [], "test_interval": 0}


# ---------------------------------------------------------------------------
# The argument vector
# ---------------------------------------------------------------------------

class TestCommand:
    def test_the_proxy_flag_goes_before_the_url(self):
        cmd = ["python", "-m", "yt_dlp", "--no-playlist", "https://x/y"]
        with_proxy = egress.with_proxy(cmd, PROXY)
        assert with_proxy[-1] == "https://x/y"
        assert with_proxy[-3:] == ["--proxy", PROXY, "https://x/y"]
        assert cmd == ["python", "-m", "yt_dlp", "--no-playlist", "https://x/y"]

    def test_an_empty_command_still_gets_the_flag(self):
        assert egress.with_proxy([], PROXY) == ["--proxy", PROXY]

    def test_the_flag_and_the_cookie_flag_coexist(self):
        """Both are options in the option block, and the URL stays last."""
        cmd = koyura_app._build_download_cmd(
            "job1", "https://www.reddit.com/r/x", "video", None
        )
        cmd = cookie_store.with_cookies(cmd, "/cookies/reddit/a.txt")
        cmd = egress.with_proxy(cmd, PROXY)
        assert cmd[-1] == "https://www.reddit.com/r/x"
        assert "--cookies" in cmd
        assert "--proxy" in cmd
        assert cmd[cmd.index("--proxy") + 1] == PROXY


# ---------------------------------------------------------------------------
# Which URL gets the proxy
# ---------------------------------------------------------------------------

class TestProxyForUrl:
    def test_a_connection_covers_a_reddit_url(self, monkeypatch):
        monkeypatch.setattr(
            egress, "_fetch", lambda: egress.Rule.from_payload(_payload())
        )
        assert egress.proxy_for_url("https://www.reddit.com/r/x") == PANEL_PROXY

    def test_a_selected_rule_skips_an_unlisted_service(self, monkeypatch):
        monkeypatch.setattr(
            egress,
            "_fetch",
            lambda: egress.Rule.from_payload(
                _payload(
                    _connection(
                        assignment={"mode": "selected", "services": ["reddit"]}
                    )
                )
            ),
        )
        assert egress.proxy_for_url("https://www.reddit.com/r/x") != ""
        assert egress.proxy_for_url("https://www.youtube.com/watch?v=x") == ""

    def test_an_except_rule_skips_the_excluded_service(self, monkeypatch):
        monkeypatch.setattr(
            egress,
            "_fetch",
            lambda: egress.Rule.from_payload(
                _payload(
                    _connection(
                        assignment={"mode": "except", "services": ["youtube"]}
                    )
                )
            ),
        )
        assert egress.proxy_for_url("https://www.reddit.com/r/x") != ""
        assert egress.proxy_for_url("https://www.youtube.com/watch?v=x") == ""

    def test_a_failed_connection_is_used_as_the_last_chance(self, monkeypatch):
        monkeypatch.setattr(
            egress,
            "_fetch",
            lambda: egress.Rule.from_payload(_payload(_connection(status="fail"))),
        )
        assert egress.proxy_for_url("https://www.reddit.com/r/x") == PANEL_PROXY

    def test_an_unreachable_panel_means_direct(self):
        assert egress.proxy_for_url("https://www.reddit.com/r/x") == ""

    def test_a_broken_rule_never_breaks_a_download(self, monkeypatch):
        def _boom(_url):
            raise RuntimeError("boom")

        monkeypatch.setattr(cookie_store, "service_for_url", _boom)
        assert egress.proxy_for_url("https://www.reddit.com/r/x") == ""


# ---------------------------------------------------------------------------
# The flag reaching the real vector
# ---------------------------------------------------------------------------

class TestJobCommand:
    def test_the_proxy_reaches_the_real_command(self, monkeypatch):
        captured: dict[str, list[str]] = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            raise RuntimeError("stop before the process runs")

        monkeypatch.setattr(koyura_app.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)

        with pytest.raises(RuntimeError):
            koyura_app._attempt_download(
                "job2", "https://www.reddit.com/r/x", "video", None,
                cookies_path=None, proxy_url=PROXY,
            )

        cmd = captured["cmd"]
        assert "--proxy" in cmd
        assert cmd[cmd.index("--proxy") + 1] == PROXY
        assert cmd[-1] == "https://www.reddit.com/r/x"

    def test_no_proxy_means_no_flag(self, monkeypatch):
        captured: dict[str, list[str]] = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            raise RuntimeError("stop")

        monkeypatch.setattr(koyura_app.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)

        with pytest.raises(RuntimeError):
            koyura_app._attempt_download(
                "job3", "https://www.youtube.com/watch?v=x", "video", None,
                cookies_path=None, proxy_url="",
            )

        assert "--proxy" not in captured["cmd"]

    def test_do_download_passes_the_rule_to_every_attempt(self, monkeypatch):
        seen: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            seen.append(proxy_url)
            return "done"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(koyura_app, "cookie_paths", lambda service: [])
        monkeypatch.setattr(egress, "proxy_for_url", lambda url: PROXY)
        koyura_app._do_download("job4", "https://www.reddit.com/r/x", "video", None)

        assert seen == [PROXY]

    def test_the_proxy_travels_with_each_cookie_retry(self, monkeypatch):
        """Both flags have to be on every attempt, not just the first."""
        seen: list[tuple[str | None, str | None]] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            seen.append((cookies_path, proxy_url))
            return "auth" if str(cookies_path).endswith("one.txt") else "done"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app, "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        monkeypatch.setattr(egress, "proxy_for_url", lambda url: PROXY)
        koyura_app.jobs["job5"] = {"status": "downloading"}
        koyura_app._do_download("job5", "https://www.reddit.com/r/x", "video", None)

        assert seen == [
            ("/cookies/reddit/one.txt", PROXY),
            ("/cookies/reddit/two.txt", PROXY),
        ]


# ---------------------------------------------------------------------------
# blocked vs auth
# ---------------------------------------------------------------------------

class TestBlockedClassification:
    @pytest.mark.parametrize(
        "stderr",
        [
            REDDIT_BLOCKED,
            BLOCKED_403,
            "ERROR: HTTP Error 429: Too Many Requests",
            "ERROR: 429 Too Many Requests",
            "ERROR: [Reddit] blocked",
            "ERROR: Please solve the captcha",
        ],
    )
    def test_a_refused_address_is_blocked(self, stderr: str):
        assert cookie_store.looks_blocked(stderr) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "ERROR: Sign in to confirm your age",
            "ERROR: login required",
            "ERROR: This video is private",
            "ERROR: Video unavailable",
            "",
        ],
    )
    def test_a_session_problem_is_not_blocked(self, stderr: str):
        assert cookie_store.looks_blocked(stderr) is False

    def test_the_real_reddit_block_is_reported_as_a_proxy_problem(self):
        """The task's exact string: the advice has to be "proxy", not "cookies"."""
        message = cookie_store.blocked_message("reddit")
        assert "reddit" in message
        assert "proxy" in message.lower()
        assert "cookies will not help" in message.lower()

    def test_the_blocked_message_names_the_panel_page(self):
        assert "Proxy" in cookie_store.blocked_message("reddit")

    def test_an_unrelated_failure_keeps_the_plain_error(self):
        assert cookie_store.looks_like_auth_failure("ERROR: Video unavailable") is False


class TestBlockedJob:
    def _run(self, monkeypatch, stderr: str) -> str:
        """Run one attempt with a fake yt-dlp that exits 1 with ``stderr``."""

        class _FakeProcess:
            def __init__(self):
                self.stdout = iter(())
                self.stderr = iter([stderr])
                self.returncode = 1

            def wait(self, timeout=None):
                return 1

            def poll(self):
                return 1

        monkeypatch.setattr(
            koyura_app.subprocess, "Popen", lambda *a, **k: _FakeProcess()
        )
        monkeypatch.setattr(koyura_app, "_cleanup_partial_files", lambda job_id: None)
        koyura_app.jobs["job9"] = {"status": "downloading"}
        return koyura_app._attempt_download(
            "job9", "https://www.reddit.com/r/x", "video", None,
            cookies_path="/cookies/reddit/a.txt", proxy_url="",
        )

    def test_a_blocked_address_ends_the_attempt(self, monkeypatch):
        assert self._run(monkeypatch, REDDIT_BLOCKED) == "blocked"

    def test_the_job_carries_the_code_and_the_sentence(self, monkeypatch):
        self._run(monkeypatch, REDDIT_BLOCKED)
        job = koyura_app.jobs["job9"]
        assert job["status"] == "error"
        assert job["error_code"] == "service_blocked"
        assert "proxy" in job["error"].lower()
        assert job["cookies_service"] == "reddit"

    def test_a_blocked_job_does_not_try_the_next_jar(self, monkeypatch):
        """Another jar answers the same 403 — retrying only costs a minute."""
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            attempted.append(cookies_path)
            return "blocked"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app, "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        koyura_app.jobs["job10"] = {"status": "downloading"}
        koyura_app._do_download("job10", "https://www.reddit.com/r/x", "video", None)

        assert attempted == ["/cookies/reddit/one.txt"]

    def test_a_session_problem_still_tries_the_next_jar(self, monkeypatch):
        """The 403 that is *not* a block must keep the cookie retry."""
        attempted: list[str | None] = []

        def fake_attempt(job_id, url, fmt, format_id, *, cookies_path, proxy_url=None):
            attempted.append(cookies_path)
            return "auth"

        monkeypatch.setattr(koyura_app, "_attempt_download", fake_attempt)
        monkeypatch.setattr(
            koyura_app, "cookie_paths",
            lambda service: ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"],
        )
        koyura_app.jobs["job11"] = {"status": "downloading"}
        koyura_app._do_download("job11", "https://www.reddit.com/r/x", "video", None)

        assert attempted == ["/cookies/reddit/one.txt", "/cookies/reddit/two.txt"]
