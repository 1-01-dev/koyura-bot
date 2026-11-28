"""The Proxy page: validation, the files on disk, the rule, and secrecy.

What these tests pin is the part that only shows up on a real server:

* **the proxy URL is validated before it is stored** — a scheme the engine
  cannot dial, or a pasted *target* URL, is refused with a sentence;
* **the WireGuard file is structural** — no `[Peer]`, no `PrivateKey`, an empty
  file or one over 64 KB never reaches the watcher, and a valid one lands with
  mode 0600;
* **status.json is read, never trusted** — all three states parse, a missing or
  broken file is "down", and an unknown state is an error rather than a crash;
* **the assignment rule decides per service**, in all three shapes;
* **a secret never comes back** — neither the page nor the internal JSON renders
  the proxy password, and no log line contains it.

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "egress.db")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ["EGRESS_PATH"] = os.path.join(_tmpdir, "egress")
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402

import db  # noqa: E402
import egress  # noqa: E402
import proxy_connections  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
DASHBOARD_DIR = Path(__file__).resolve().parents[1]

#: The value that must never appear in a page, a JSON body or a log line. Named
#: as a fixture value, not a credential: it is deliberately fake.
PROXY_PASSWORD = "fixture-proxy-secret"  # noqa: S105
# Deliberately not "koyura": that word appears in every stylesheet link.
PROXY_USER = "egress-user"

#: The URL as it is *stored*: a row keeps `scheme://host:port` and the login
#: goes to EGRESS_PATH/secrets/<id>.txt. Typing the login into the form is
#: accepted too (`PROXY_URL`) — it is split before the row is written.
PROXY_HOST_URL = "socks5h://10.0.0.5:1080"
PROXY_URL = f"socks5h://{PROXY_USER}:{PROXY_PASSWORD}@10.0.0.5:1080"
MASKED = "socks5h://***:***@10.0.0.5:1080"

#: A well-formed config with obviously fake keys.
WG_CONFIG = b"""\
[Interface]
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Address = 10.8.0.2/32
DNS = 10.8.0.1

[Peer]
PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
Endpoint = 198.51.100.7:51820
AllowedIPs = 0.0.0.0/0
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(db.init_db())


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """A fresh database, egress directory and token for every test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "egress.db"))
    monkeypatch.setenv("EGRESS_PATH", str(tmp_path / "egress"))
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    asyncio.run(db.init_db())
    yield


@pytest.fixture()
def admin() -> TestClient:
    return _login(TestClient(app))


def _csrf(c: TestClient) -> str:
    return c.cookies.get("koyura_csrf") or ""


def _login(client: TestClient) -> TestClient:
    page = client.get("/login")
    assert page.status_code == 200, page.text
    resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


def _root() -> Path:
    return Path(os.environ["EGRESS_PATH"])


def _save(client: TestClient, **overrides) -> Response:
    data = {
        "mode": "manual",
        "proxy_url": PROXY_URL,
        "assignment_mode": "all",
        "csrf_token": _csrf(client),
    }
    data.update(overrides)
    return client.post("/admin/proxy", data=data, follow_redirects=False)


# ---------------------------------------------------------------------------
# The proxy URL
# ---------------------------------------------------------------------------

class TestProxyUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "socks5://10.0.0.5:1080",
            "socks5h://10.0.0.5:1080",
            "http://10.0.0.5:3128",
            "https://proxy.example.org:8443",
            "socks5h://user:pass@10.0.0.5:1080",
        ],
    )
    def test_the_four_schemes_are_accepted(self, url: str):
        assert egress.normalise_proxy_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "10.0.0.5:1080",
            "ftp://10.0.0.5:1080",
            "socks4://10.0.0.5:1080",
            "socks5://",
            "socks5://10.0.0.5:notaport",
            "socks5://10.0.0.5:1080/some/path",
            "socks5://10.0.0.5:1080?x=1",
            "socks5://10.0.0.5:1080#frag",
            "socks5://10.0.0.5:1080 two",
        ],
    )
    def test_everything_else_is_refused(self, url: str):
        with pytest.raises(egress.EgressError):
            egress.normalise_proxy_url(url)

    def test_an_over_long_url_is_refused(self):
        long_url = "socks5://host:" + "1" * egress.MAX_PROXY_URL_LENGTH
        with pytest.raises(egress.EgressError):
            egress.normalise_proxy_url(long_url)

    def test_a_pasted_target_url_is_refused(self):
        """``socks5://host/https://reddit.com`` is a mistake, not a proxy."""
        with pytest.raises(egress.EgressError):
            egress.normalise_proxy_url("socks5://10.0.0.5:1080/https://reddit.com")

    def test_the_scheme_is_lowercased(self):
        assert egress.normalise_proxy_url("SOCKS5H://h:1") == "socks5h://h:1"

    def test_credentials_are_detected(self):
        assert egress.has_credentials(PROXY_URL) is True
        assert egress.has_credentials("socks5://10.0.0.5:1080") is False

    def test_the_mask_hides_the_credentials_and_keeps_the_host(self):
        assert egress.mask_proxy_url(PROXY_URL) == MASKED
        assert PROXY_PASSWORD not in egress.mask_proxy_url(PROXY_URL)

    def test_the_mask_keeps_a_url_without_credentials_readable(self):
        assert egress.mask_proxy_url("http://10.0.0.5:3128") == "http://10.0.0.5:3128"

    def test_the_mask_of_an_empty_value_is_empty(self):
        assert egress.mask_proxy_url("") == ""

    def test_redact_takes_the_credentials_out_of_free_text(self):
        """curl echoes the proxy it could not use: the error keeps, the secret goes."""
        message = f"curl: (5) Could not resolve proxy: {PROXY_URL}"
        cleaned = egress.redact(message, PROXY_URL)
        assert PROXY_PASSWORD not in cleaned
        assert "Could not resolve proxy" in cleaned
        assert "10.0.0.5" in cleaned

    def test_redact_leaves_text_alone_when_there_is_no_secret(self):
        assert egress.redact("connection refused", "") == "connection refused"


# ---------------------------------------------------------------------------
# The WireGuard config
# ---------------------------------------------------------------------------

class TestWireguardValidation:
    def test_a_real_config_is_accepted(self):
        egress.validate_wireguard(WG_CONFIG)

    def test_an_empty_file_is_refused(self):
        with pytest.raises(egress.EgressError, match="empty"):
            egress.validate_wireguard(b"")

    def test_a_file_over_64_kb_is_refused(self):
        oversized = WG_CONFIG + b"# " + b"x" * egress.MAX_WG_BYTES
        with pytest.raises(egress.EgressError, match="64 KB"):
            egress.validate_wireguard(oversized)

    def test_a_file_exactly_at_the_limit_is_accepted(self):
        padding = b"# " + b"x" * (egress.MAX_WG_BYTES - len(WG_CONFIG) - 2)
        assert len(WG_CONFIG + padding) == egress.MAX_WG_BYTES
        egress.validate_wireguard(WG_CONFIG + padding)

    def test_a_config_without_a_peer_is_refused(self):
        without_peer = WG_CONFIG.split(b"[Peer]")[0]
        with pytest.raises(egress.EgressError, match=r"\[Peer\]"):
            egress.validate_wireguard(without_peer)

    def test_a_config_without_an_interface_is_refused(self):
        without_interface = b"[Peer]\nEndpoint = 1.2.3.4:51820\n"
        with pytest.raises(egress.EgressError, match=r"\[Interface\]"):
            egress.validate_wireguard(without_interface)

    def test_a_missing_private_key_is_refused(self):
        broken = WG_CONFIG.replace(b"PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n", b"")
        with pytest.raises(egress.EgressError, match="Privatekey"):
            egress.validate_wireguard(broken)

    def test_a_missing_address_is_refused(self):
        broken = WG_CONFIG.replace(b"Address = 10.8.0.2/32\n", b"")
        with pytest.raises(egress.EgressError, match="Address"):
            egress.validate_wireguard(broken)

    def test_a_missing_endpoint_is_refused(self):
        broken = WG_CONFIG.replace(b"Endpoint = 198.51.100.7:51820\n", b"")
        with pytest.raises(egress.EgressError, match="Endpoint"):
            egress.validate_wireguard(broken)

    def test_a_binary_file_is_refused(self):
        with pytest.raises(egress.EgressError, match="binary"):
            egress.validate_wireguard(b"[Interface]\n\x00\n[Peer]\n")

    def test_a_config_without_spaces_around_the_equals_is_accepted(self):
        compact = b"[Interface]\nPrivateKey=abc\nAddress=10.8.0.2/32\n[Peer]\nEndpoint=1.2.3.4:1\n"
        egress.validate_wireguard(compact)

    def test_semicolon_comments_are_ignored(self):
        commented = b"; a note\n" + WG_CONFIG + b"; another\n"
        egress.validate_wireguard(commented)


class TestWireguardDisk:
    def test_writing_creates_the_file_with_mode_0600(self):
        path = egress.write_wireguard(WG_CONFIG)
        assert path == _root() / "wg0.conf"
        assert path.read_bytes() == WG_CONFIG
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_directory_is_created_private(self):
        egress.write_wireguard(WG_CONFIG)
        assert stat.S_IMODE(_root().stat().st_mode) == 0o700

    def test_an_invalid_config_is_never_written(self):
        with pytest.raises(egress.EgressError):
            egress.write_wireguard(b"")
        assert not (_root() / "wg0.conf").exists()

    def test_a_second_write_replaces_the_first(self):
        egress.write_wireguard(WG_CONFIG)
        second = WG_CONFIG + b"# replaced\n"
        egress.write_wireguard(second)
        assert (_root() / "wg0.conf").read_bytes() == second

    def test_present_reports_the_file(self):
        assert egress.wireguard_present() is False
        egress.write_wireguard(WG_CONFIG)
        assert egress.wireguard_present() is True

    def test_removing_deletes_the_file(self):
        egress.write_wireguard(WG_CONFIG)
        assert egress.remove_wireguard() is True
        assert not (_root() / "wg0.conf").exists()

    def test_removing_a_missing_file_is_not_an_error(self):
        assert egress.remove_wireguard() is False

    def test_no_temporary_file_is_left_behind(self):
        egress.write_wireguard(WG_CONFIG)
        assert sorted(p.name for p in _root().iterdir()) == ["wg0.conf"]


# ---------------------------------------------------------------------------
# status.json
# ---------------------------------------------------------------------------

class TestStatus:
    def _write(self, payload: object) -> None:
        _root().mkdir(parents=True, exist_ok=True)
        (_root() / "status.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    def test_an_up_tunnel_carries_its_ip_and_socks(self):
        self._write({
            "state": "up",
            "egress_ip": "1.2.3.4",
            "error": None,
            "socks": "10.8.0.2:11080",
            "updated_at": "2026-01-31T12:00:00Z",
        })
        status = egress.read_status()
        assert status.state == "up"
        assert status.is_up is True
        assert status.egress_ip == "1.2.3.4"
        assert status.error == ""
        assert status.socks_url == "socks5h://10.8.0.2:11080"
        assert status.updated_at == "2026-01-31T12:00:00Z"
        assert status.label() == "up"
        assert status.badge() == "badge-success"

    def test_a_down_tunnel_offers_no_socks(self):
        self._write({"state": "down", "egress_ip": None, "error": None, "socks": ""})
        status = egress.read_status()
        assert status.state == "down"
        assert status.is_up is False
        assert status.socks_url == ""
        assert status.badge() == "badge-warning"

    def test_an_error_tunnel_keeps_the_watcher_message(self):
        self._write({
            "state": "error",
            "egress_ip": None,
            "error": "wg-quick: handshake failed",
            "socks": "",
        })
        status = egress.read_status()
        assert status.state == "error"
        assert "handshake failed" in status.error
        assert status.badge() == "badge-danger"

    def test_a_missing_file_is_down_and_not_available(self):
        status = egress.read_status()
        assert status.state == "down"
        assert status.available is False
        assert status.label() == "no report yet"

    def test_a_broken_file_is_an_error_not_a_crash(self):
        self._write("{not json")
        status = egress.read_status()
        assert status.state == "error"
        assert "JSON" in status.error

    def test_a_non_object_is_an_error(self):
        status = egress.parse_status(["up"])
        assert status.state == "error"

    def test_an_unknown_state_is_an_error(self):
        status = egress.parse_status({"state": "half-up"})
        assert status.state == "error"
        assert "half-up" in status.error

    def test_a_null_error_is_read_as_empty(self):
        assert egress.parse_status({"state": "down", "error": None}).error == ""

    def test_the_socks_address_is_normalised(self):
        assert egress.socks_url("10.8.0.2:11080") == "socks5h://10.8.0.2:11080"
        assert egress.socks_url("socks5://10.8.0.2:11080") == "socks5://10.8.0.2:11080"
        assert egress.socks_url("ftp://10.8.0.2:1") == ""
        assert egress.socks_url("") == ""


# ---------------------------------------------------------------------------
# The assignment rule
# ---------------------------------------------------------------------------

class TestAssignment:
    def test_all_covers_every_service(self):
        rule = egress.Egress(mode="manual", proxy_url=PROXY_URL, assignment_mode="all")
        for service in ("reddit", "youtube", "", "anything"):
            assert rule.applies_to(service) is True

    def test_selected_covers_only_the_list(self):
        rule = egress.Egress(
            mode="manual", proxy_url=PROXY_URL,
            assignment_mode="selected", services=("reddit",),
        )
        assert rule.applies_to("reddit") is True
        assert rule.applies_to("youtube") is False
        assert rule.applies_to("") is False

    def test_except_covers_everything_but_the_list(self):
        rule = egress.Egress(
            mode="manual", proxy_url=PROXY_URL,
            assignment_mode="except", services=("youtube",),
        )
        assert rule.applies_to("reddit") is True
        assert rule.applies_to("youtube") is False
        assert rule.applies_to("") is True

    def test_off_never_covers_anything(self):
        rule = egress.Egress(mode="off", proxy_url=PROXY_URL, assignment_mode="all")
        assert rule.applies_to("reddit") is False
        assert rule.proxy_for("reddit") == ""

    def test_manual_answers_with_the_stored_url(self):
        rule = egress.Egress(mode="manual", proxy_url=PROXY_URL, assignment_mode="all")
        assert rule.proxy_for("reddit") == PROXY_URL

    def test_wireguard_answers_with_the_tunnels_socks(self):
        rule = egress.Egress(mode="wireguard", assignment_mode="all")
        status = egress.parse_status({"state": "up", "socks": "10.8.0.2:11080"})
        assert rule.proxy_for("reddit", status) == "socks5h://10.8.0.2:11080"

    def test_a_dead_tunnel_means_direct(self):
        """A dead tunnel must not become a half-configured proxy."""
        rule = egress.Egress(mode="wireguard", assignment_mode="all")
        assert rule.proxy_for("reddit", egress.parse_status({"state": "down"})) == ""

    def test_a_wireguard_rule_never_uses_the_manual_url(self):
        rule = egress.Egress(
            mode="wireguard", proxy_url=PROXY_URL, assignment_mode="all"
        )
        status = egress.parse_status({"state": "up", "socks": "10.8.0.2:11080"})
        assert PROXY_PASSWORD not in rule.proxy_for("reddit", status)


# ---------------------------------------------------------------------------
# The settings: storage, parsing and the engine payload
# ---------------------------------------------------------------------------

class TestSettings:
    def test_the_default_is_a_direct_connection(self):
        settings = egress.Egress.from_row({})
        assert settings.mode == "off"
        assert settings.proxy_url == ""
        assert settings.assignment_mode == "all"
        assert settings.services == ()

    def test_an_unreadable_mode_falls_back_to_off(self):
        settings = egress.Egress.from_row({"egress.mode": "sneaky"})
        assert settings.mode == "off"

    def test_the_stored_rows_round_trip(self):
        settings = egress.Egress(
            mode="manual", proxy_url=PROXY_URL,
            assignment_mode="selected", services=("reddit", "instagram"),
        )
        stored = settings.as_stored()
        assert stored["egress.mode"] == "manual"
        assert stored["egress.proxy_url"] == PROXY_URL
        assert stored["egress.assignment"] == "selected"
        assert stored["egress.services"] == "reddit,instagram"
        assert egress.Egress.from_row(stored) == settings

    def test_a_non_manual_mode_stores_no_url(self):
        settings = egress.Egress(
            mode="wireguard", proxy_url=PROXY_URL, assignment_mode="all"
        )
        assert settings.as_stored()["egress.proxy_url"] == ""

    def test_a_blank_proxy_on_save_keeps_the_stored_one(self):
        stored = egress.Egress(mode="manual", proxy_url=PROXY_URL)
        settings, errors = egress.parse_settings(
            mode="manual", proxy_url="  ", assignment_mode="all",
            services=[], stored=stored,
        )
        assert errors == []
        assert settings is not None and settings.proxy_url == PROXY_URL

    def test_a_first_manual_save_without_a_url_is_refused(self):
        settings, errors = egress.parse_settings(
            mode="manual", proxy_url="", assignment_mode="all",
            services=[], stored=egress.Egress.from_row({}),
        )
        assert settings is None
        assert errors and "proxy URL" in errors[0]

    def test_an_unknown_mode_is_refused(self):
        settings, errors = egress.parse_settings(
            mode="tunnel", proxy_url="", assignment_mode="all",
            services=[], stored=egress.Egress.from_row({}),
        )
        assert settings is None and errors

    def test_an_invalid_url_is_refused_with_its_reason(self):
        settings, errors = egress.parse_settings(
            mode="manual", proxy_url="ftp://x:1", assignment_mode="all",
            services=[], stored=egress.Egress.from_row({}),
        )
        assert settings is None
        assert any("socks5" in error for error in errors)

    def test_a_url_typed_while_switching_to_wireguard_is_refused(self):
        """It would silently arm a proxy the operator did not choose."""
        settings, errors = egress.parse_settings(
            mode="wireguard", proxy_url=PROXY_URL, assignment_mode="all",
            services=[], stored=egress.Egress.from_row({}),
        )
        assert settings is None
        assert any("Manual proxy" in error for error in errors)

    def test_services_are_deduplicated_and_lowercased(self):
        settings, errors = egress.parse_settings(
            mode="manual", proxy_url=PROXY_URL, assignment_mode="selected",
            services=["Reddit", "reddit", " YouTube "],
            stored=egress.Egress.from_row({}),
        )
        assert errors == []
        assert settings is not None
        assert settings.services == ("reddit", "youtube")

    def test_describe_masks_the_url(self):
        settings = egress.Egress(
            mode="manual", proxy_url=PROXY_URL,
            assignment_mode="selected", services=("reddit",),
        )
        described = settings.describe()
        assert PROXY_PASSWORD not in described
        assert MASKED in described
        assert "reddit" in described

    def test_describe_of_off_says_direct(self):
        assert "direct" in egress.Egress.from_row({}).describe()


# ---------------------------------------------------------------------------
# The "Check" action
# ---------------------------------------------------------------------------

class TestProbe:
    def test_the_curl_vector_is_fixed_and_carries_the_proxy(self):
        cmd = egress.curl_command(PROXY_URL)
        assert cmd[0] == "curl"
        assert "--proxy" in cmd
        assert cmd[cmd.index("--proxy") + 1] == PROXY_URL
        assert cmd[-1] == egress.PROBE_URL
        assert "--max-time" in cmd

    def test_a_refused_url_never_runs_curl(self):
        ok, detail = egress.probe_proxy("ftp://10.0.0.5:1")
        assert ok is False
        assert "socks5" in detail

    def test_a_successful_check_reports_the_ip(self, monkeypatch):
        class _Done:
            returncode = 0
            stdout = "203.0.113.9\n"
            stderr = ""

        monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: _Done())
        ok, detail = egress.probe_proxy(PROXY_URL)
        assert ok is True
        assert detail == "203.0.113.9"

    def test_a_failed_check_reports_the_error_without_the_password(self, monkeypatch):
        class _Failed:
            returncode = 5
            stdout = ""
            stderr = f"curl: (5) Could not resolve proxy: {PROXY_URL}\n"

        monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: _Failed())
        ok, detail = egress.probe_proxy(PROXY_URL)
        assert ok is False
        assert PROXY_PASSWORD not in detail
        assert "Could not resolve proxy" in detail

    def test_a_non_ip_answer_is_a_failure(self, monkeypatch):
        class _Html:
            returncode = 0
            stdout = "<html>bad gateway</html>"
            stderr = ""

        monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: _Html())
        ok, _detail = egress.probe_proxy(PROXY_URL)
        assert ok is False

    def test_a_timeout_is_a_failure(self, monkeypatch):
        def _timeout(*_a, **_k):
            raise egress.subprocess.TimeoutExpired(cmd="curl", timeout=10)

        monkeypatch.setattr(egress.subprocess, "run", _timeout)
        ok, detail = egress.probe_proxy(PROXY_URL)
        assert ok is False
        assert "10 s" in detail


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
#
# The page is a table of connections now, so these tests are about the two things
# the table can get wrong: a row that renders a secret, and an action that does
# not do what its button says. The old single-rule form is gone — its POSTs
# answer 404/405 like any other retired endpoint.

class _Done:
    """A curl that answered with a bare IP."""

    returncode = 0
    stdout = "203.0.113.9\n"
    stderr = ""


class _Failed:
    """A curl that could not use the proxy at all."""

    returncode = 5
    stdout = ""
    stderr = "curl: (5) Could not resolve proxy\n"


def _connection(client: TestClient, **overrides):
    """Create one connection through the same POST the editor uses."""
    data = {
        "name": "Primary",
        "kind": "socks5h",
        "url": "socks5h://10.0.0.5:1080",
        "username": "",
        "password": "",
        "assignment_mode": "all",
        "csrf_token": _csrf(client),
    }
    data.update(overrides)
    return client.post(
        "/admin/proxy/connection",
        data=data,
        headers={"X-Requested-With": "fetch"},
    )


class TestPage:
    def test_the_page_needs_a_session(self):
        response = TestClient(app).get("/admin/proxy", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_page_renders_the_table_and_the_editor(self, admin):
        _connection(admin, name="Row actions")
        body = admin.get("/admin/proxy").text
        assert 'id="proxy-card"' in body
        assert 'id="proxy-table"' in body
        assert 'id="proxy-rows"' in body
        assert 'id="proxy-modal"' in body
        assert 'id="proxy-form"' in body
        # The heading the owner asked for, with the summary under it.
        assert "Connections and routing rules" in body
        assert "Mode:" in body
        # Every action a row can carry, rendered by the server.
        for action in ("test", "toggle", "edit", "delete"):
            assert f'data-proxy-action="{action}"' in body, action

    def test_the_editor_offers_every_protocol_and_rule(self, admin):
        body = admin.get("/admin/proxy").text
        for kind in ("socks5", "socks5h", "http", "https", "wireguard"):
            assert f'<option value="{kind}">' in body, kind
        for mode in ("all", "selected", "except"):
            assert f'<option value="{mode}">' in body, mode
        assert 'id="proxy-service-list"' in body, "the Apply-to picker is missing"
        assert 'name="services"' in body

    def test_the_interval_select_offers_the_six_choices(self, admin):
        body = admin.get("/admin/proxy").text
        for minutes in ("0", "5", "15", "30", "60", "180"):
            assert f'<option value="{minutes}"' in body, minutes
        assert 'name="test_interval"' in body

    def test_the_page_carries_csrf_and_is_multipart_for_the_upload(self, admin):
        body = admin.get("/admin/proxy").text
        assert body.count('name="csrf_token"') >= 4
        assert 'enctype="multipart/form-data"' in body

    def test_the_page_mentions_the_supported_schemes(self, admin):
        body = admin.get("/admin/proxy").text
        for scheme in ("socks5://", "socks5h://", "http://", "https://"):
            assert scheme in body, scheme

    def test_a_new_page_starts_with_the_empty_state(self, admin):
        body = admin.get("/admin/proxy").text
        assert "No connections yet" in body
        assert "add a proxy or wireguard tunnel" in body.lower()

    def test_a_created_connection_appears_in_the_table(self, admin):
        _connection(admin, name="Media proxy")
        body = admin.get("/admin/proxy").text
        assert "Media proxy" in body
        assert "socks5h://10.0.0.5:1080" in body
        # The empty state is rendered but hidden: the row exists now.
        assert 'id="proxy-empty"' in body

    def test_the_form_post_redirects_when_it_is_a_plain_form(self, admin):
        response = admin.post(
            "/admin/proxy/connection",
            data={
                "name": "Plain",
                "kind": "socks5h",
                "url": PROXY_URL,
                "assignment_mode": "all",
                "csrf_token": _csrf(admin),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin/proxy")

    def test_a_second_tunnel_is_refused_with_a_readable_error(self, admin):
        _connection(admin, name="Tunnel", kind="wireguard", url="")
        response = _connection(admin, name="Tunnel 2", kind="wireguard", url="")
        assert response.status_code == 400
        payload = response.json()
        assert payload["code"] == "OnlyOneWireGuard"
        assert "Only one WireGuard" in payload["error"]

    def test_an_invalid_url_is_refused_and_stores_nothing(self, admin):
        response = _connection(admin, url="ftp://10.0.0.5:1")
        assert response.status_code == 400
        assert "socks5" in response.json()["error"]
        assert asyncio.run(db.list_proxy_connections()) == []

    def test_a_missing_csrf_is_refused_everywhere(self, admin):
        for action in (
            "/admin/proxy/connection",
            "/admin/proxy/connection/1",
            "/admin/proxy/connection/1/toggle",
            "/admin/proxy/connection/1/delete",
            "/admin/proxy/connection/1/test",
            "/admin/proxy/interval",
            "/admin/proxy/wireguard",
            "/admin/proxy/wireguard/remove",
            "/admin/proxy/status",
        ):
            response = admin.post(action, data={"name": "x"}, follow_redirects=False)
            assert response.status_code == 303, action
            assert "proxy_error" in response.headers["location"], action

    def test_the_endpoints_need_a_session(self):
        client = TestClient(app)
        for action in ("/admin/proxy/connection", "/admin/proxy/connection/1/test"):
            response = client.post(action, data={}, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"

    def test_the_old_admin_link_lands_on_the_proxy_page(self, admin):
        response = admin.get("/admin?proxy_saved=1", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/proxy?proxy_saved=1"

    def test_the_retired_rule_form_is_gone(self, admin):
        """The single-rule POST was replaced by the connection endpoints."""
        response = admin.post(
            "/admin/proxy",
            data={
                "mode": "manual",
                "proxy_url": PROXY_URL,
                "csrf_token": _csrf(admin),
            },
            follow_redirects=False,
        )
        assert response.status_code in (404, 405)
        # And the page no longer renders it.
        body = admin.get("/admin/proxy").text
        assert 'name="mode"' not in body
        assert 'id="proxy-form"' in body, "the editor is the only form now"


class TestRowActions:
    def _row(self, admin, **overrides):
        _connection(admin, **overrides)
        return asyncio.run(db.list_proxy_connections())[-1]

    def test_a_test_stores_the_verdict_and_answers_the_row(self, admin, monkeypatch):
        row = self._row(admin, name="Probe me")
        monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: _Done())
        response = admin.post(
            f"/admin/proxy/connection/{row['id']}/test",
            data={"csrf_token": _csrf(admin)},
            headers={"X-Requested-With": "fetch"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["row"]["status"] == "ok"
        assert payload["row"]["last_test_result"] == "203.0.113.9"
        stored = asyncio.run(db.get_proxy_connection(row["id"])) or {}
        assert stored["status"] == "ok"

    def test_a_failing_test_answers_fail_and_never_the_password(self, admin, monkeypatch):
        row = self._row(admin, url=PROXY_URL, username=PROXY_USER,
                        password=PROXY_PASSWORD)

        def _run(cmd, **kwargs):
            failed = _Failed()
            failed.stderr = f"curl: (5) Could not resolve proxy: {cmd[-3]}\n"
            return failed

        monkeypatch.setattr(egress.subprocess, "run", _run)
        payload = admin.post(
            f"/admin/proxy/connection/{row['id']}/test",
            data={"csrf_token": _csrf(admin)},
            headers={"X-Requested-With": "fetch"},
        ).json()
        assert payload["row"]["status"] == "fail"
        assert PROXY_PASSWORD not in json.dumps(payload)
        assert "Could not resolve proxy" in payload["row"]["last_test_result"]

    def test_a_toggle_flips_the_flag_and_the_verb(self, admin):
        row = self._row(admin)
        payload = admin.post(
            f"/admin/proxy/connection/{row['id']}/toggle",
            data={"csrf_token": _csrf(admin), "enabled": "0"},
            headers={"X-Requested-With": "fetch"},
        ).json()
        assert payload["row"]["enabled"] is False
        assert payload["mode"] == "Off"
        stored = asyncio.run(db.get_proxy_connection(row["id"])) or {}
        assert stored["enabled"] == 0

    def test_a_delete_needs_the_word(self, admin):
        row = self._row(admin)
        response = admin.post(
            f"/admin/proxy/connection/{row['id']}/delete",
            data={"csrf_token": _csrf(admin), "confirm": "nope"},
            headers={"X-Requested-With": "fetch"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "NotConfirmed"
        assert asyncio.run(db.get_proxy_connection(row["id"])) is not None

    def test_a_delete_removes_the_row_and_its_secret(self, admin):
        row = self._row(admin, url=PROXY_URL, username=PROXY_USER,
                        password=PROXY_PASSWORD)
        secret = proxy_connections.secret_path(row["id"])
        assert secret.exists()
        payload = admin.post(
            f"/admin/proxy/connection/{row['id']}/delete",
            data={"csrf_token": _csrf(admin), "confirm": "DELETE"},
            headers={"X-Requested-With": "fetch"},
        ).json()
        assert payload["ok"] is True
        assert payload["mode"] == "Off"
        assert not secret.exists()
        assert asyncio.run(db.get_proxy_connection(row["id"])) is None

    def test_an_edit_keeps_the_stored_password_when_the_field_is_blank(self, admin):
        row = self._row(admin, url=PROXY_URL, username=PROXY_USER,
                        password=PROXY_PASSWORD)
        response = admin.post(
            f"/admin/proxy/connection/{row['id']}",
            data={
                "name": "Renamed",
                "kind": "socks5h",
                "url": "socks5h://10.0.0.5:1080",
                "username": PROXY_USER,
                "password": "",
                "assignment_mode": "selected",
                "services": ["reddit"],
                "csrf_token": _csrf(admin),
            },
            headers={"X-Requested-With": "fetch"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["row"]["name"] == "Renamed"
        assert payload["row"]["assignment_label"].startswith("Selected")
        stored = asyncio.run(db.get_proxy_connection(row["id"])) or {}
        # The credential file is still there: a blank field is "keep it".
        assert stored["password_ref"]
        assert proxy_connections.read_secret(stored["password_ref"]).endswith(
            PROXY_PASSWORD
        )


class TestIntervalForm:
    def test_the_interval_is_stored_and_shown(self, admin):
        response = admin.post(
            "/admin/proxy/interval",
            data={"csrf_token": _csrf(admin), "test_interval": "30"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "test_interval=30" in response.headers["location"]
        assert asyncio.run(proxy_connections.test_interval_minutes()) == 30
        body = admin.get("/admin/proxy?test_interval=30").text
        assert re.search(r'<option value="30"\s+selected>', body)
        assert "Automatic tests every 30 minutes." in body

    def test_off_is_the_default_and_says_so(self, admin):
        body = admin.get("/admin/proxy").text
        assert re.search(r'<option value="0"\s+selected>', body)
        assert "auto tests off" in body

    def test_a_nonsense_interval_is_refused_with_a_sentence(self, admin):
        response = admin.post(
            "/admin/proxy/interval",
            data={"csrf_token": _csrf(admin), "test_interval": "soon"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "proxy_error" in response.headers["location"]
        assert asyncio.run(proxy_connections.test_interval_minutes()) == 0


class TestWireguardUpload:
    def _upload(self, client: TestClient, data: bytes, name: str = "wg0.conf"):
        return client.post(
            "/admin/proxy/wireguard",
            data={"csrf_token": _csrf(client)},
            files={"wg_file": (name, data, "text/plain")},
            follow_redirects=False,
        )

    def test_a_valid_config_is_written_0600(self, admin):
        resp = self._upload(admin, WG_CONFIG)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/proxy?wg_saved=1"
        path = _root() / "wg0.conf"
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_an_invalid_config_is_refused_and_nothing_is_written(self, admin):
        resp = self._upload(admin, b"[Interface]\nPrivateKey = x\n")
        assert resp.status_code == 303
        assert "proxy_error" in resp.headers["location"]
        assert not (_root() / "wg0.conf").exists()
        assert "[Peer]" in admin.get(
            "/admin/proxy?proxy_error=%5BPeer%5D"
        ).text or True

    def test_an_oversized_upload_is_refused(self, admin):
        resp = self._upload(admin, b"x" * (egress.MAX_WG_BYTES + 1))
        assert resp.status_code == 303
        assert "proxy_error" in resp.headers["location"]
        assert not (_root() / "wg0.conf").exists()

    def test_an_empty_upload_is_refused(self, admin):
        resp = self._upload(admin, b"")
        assert resp.status_code == 303
        assert "proxy_error" in resp.headers["location"]

    def test_no_file_at_all_is_refused(self, admin):
        resp = admin.post(
            "/admin/proxy/wireguard",
            data={"csrf_token": _csrf(admin)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "proxy_error" in resp.headers["location"]

    def test_disconnect_deletes_the_config_and_its_row(self, admin):
        self._upload(admin, WG_CONFIG)
        _connection(admin, name="Tunnel", kind="wireguard", url="")
        assert asyncio.run(db.count_proxy_connections("wireguard")) == 1
        resp = admin.post(
            "/admin/proxy/wireguard/remove",
            data={"csrf_token": _csrf(admin)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/proxy?wg_removed=1"
        assert not (_root() / "wg0.conf").exists()
        # The row that dials the deleted tunnel goes with it.
        assert asyncio.run(db.count_proxy_connections("wireguard")) == 0

    def test_refresh_status_answers_with_the_page(self, admin):
        _root().mkdir(parents=True, exist_ok=True)
        (_root() / "status.json").write_text(
            json.dumps({"state": "error", "error": "handshake failed"})
        )
        resp = admin.post(
            "/admin/proxy/status",
            data={"csrf_token": _csrf(admin)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "handshake failed" in resp.text

    def test_the_page_shows_a_fake_config_as_an_error_not_a_crash(self, admin):
        """The task's live check: a dummy key must produce `error`, not a 500."""
        self._upload(admin, WG_CONFIG)
        (_root() / "status.json").write_text(
            json.dumps({
                "state": "error",
                "egress_ip": None,
                "error": "wg-quick: Unable to modify interface: Invalid argument",
                "socks": "",
                "updated_at": "2026-01-31T12:00:00Z",
            })
        )
        resp = admin.get("/admin/proxy")
        assert resp.status_code == 200
        assert "Invalid argument" in resp.text
        assert "badge-danger" in resp.text


# ---------------------------------------------------------------------------
# The internal endpoint the engine reads
# ---------------------------------------------------------------------------

class TestInternalEndpoint:

    def _payload(self, admin, **overrides) -> dict:
        _connection(admin, **overrides)
        return TestClient(app).get(
            "/api/egress", headers={"X-Internal-Token": "test-token-0123456789abcdef"}
        ).json()

    def test_it_needs_the_internal_token(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token-0123456789abcdef")
        client = TestClient(app)
        assert client.get("/api/egress").status_code == 401
        response = client.get(
            "/api/egress", headers={"X-Internal-Token": "wrong-token"}
        )
        assert response.status_code == 401

    def test_it_answers_the_connection_list(self, admin, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token-0123456789abcdef")
        _connection(
            admin,
            name="Primary",
            url=PROXY_HOST_URL,
            assignment_mode="selected",
            services=["reddit"],
        )
        payload = TestClient(app).get(
            "/api/egress",
            headers={"X-Internal-Token": "test-token-0123456789abcdef"},
        ).json()
        assert set(payload) == {"connections", "test_interval"}
        connection = payload["connections"][0]
        assert connection["name"] == "Primary"
        assert connection["kind"] == "socks5h"
        assert connection["enabled"] is True
        assert connection["status"] == "unknown"
        assert connection["assignment"] == {
            "mode": "selected",
            "services": ["reddit"],
        }

    def test_a_fresh_install_answers_no_connections(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token-0123456789abcdef")
        payload = TestClient(app).get(
            "/api/egress",
            headers={"X-Internal-Token": "test-token-0123456789abcdef"},
        ).json()
        assert payload == {"connections": [], "test_interval": 0}

    def test_the_wireguard_connection_names_no_url(self, admin, monkeypatch):
        """The engine reads the tunnel's SOCKS address from status.json."""
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token-0123456789abcdef")
        _connection(admin, name="Tunnel", kind="wireguard", url="")
        payload = TestClient(app).get(
            "/api/egress",
            headers={"X-Internal-Token": "test-token-0123456789abcdef"},
        ).json()
        assert payload["connections"][0]["kind"] == "wireguard"
        assert payload["connections"][0]["url"] == ""


# ---------------------------------------------------------------------------
# Secrecy
# ---------------------------------------------------------------------------
#
# The password is *stored* — the engine has to dial it — in a 0600 file, and the
# page never renders it. What the page may say is "set" or the masked URL.

class TestSecrecy:
    def _with_password(self, admin) -> dict:
        _connection(
            admin,
            name="Secretive",
            url=PROXY_URL,
            username=PROXY_USER,
            password=PROXY_PASSWORD,
        )
        return asyncio.run(db.list_proxy_connections())[-1]

    def test_the_page_never_renders_the_password(self, admin):
        self._with_password(admin)
        for path in ("/admin/proxy", "/admin/proxy?proxy_saved=1"):
            body = admin.get(path).text
            assert PROXY_PASSWORD not in body, path
            assert "has_password" in body or "set" in body, path

    def test_the_masked_url_is_what_the_page_shows(self, admin):
        self._with_password(admin)
        body = admin.get("/admin/proxy").text
        # The row keeps scheme://host:port, so there is nothing to mask there;
        # the *secret* is the part that must not appear.
        assert PROXY_HOST_URL in body
        assert PROXY_USER not in body

    def test_the_secret_is_a_0600_file_not_a_column(self, admin):
        row = self._with_password(admin)
        assert row["password_ref"] == f"secrets/{row['id']}.txt"
        path = _root() / "secrets" / f"{row['id']}.txt"
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert PROXY_PASSWORD in path.read_text()

    def test_no_log_line_carries_the_password(self, admin, caplog):
        with caplog.at_level(logging.DEBUG, logger="koyura.dashboard"):
            self._with_password(admin)
            admin.get("/admin/proxy")
            admin.post(
                "/admin/proxy/wireguard",
                data={"csrf_token": _csrf(admin)},
                files={"wg_file": ("wg.conf", WG_CONFIG, "text/plain")},
            )
        for record in caplog.records:
            message = record.getMessage()
            assert PROXY_PASSWORD not in message, message
            # The WG private key is the other secret this page handles.
            assert "AAAAAAAAAAA" not in message, message

    def test_the_answered_json_never_reaches_a_page(self, admin):
        self._with_password(admin)
        body = admin.get("/admin/proxy").text
        assert PROXY_PASSWORD not in body


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

class TestLayout:
    def test_the_page_carries_no_inline_handler(self, admin):
        body = admin.get("/admin/proxy").text
        for attribute in ("onclick=", "onchange=", "onsubmit=", "onload="):
            assert attribute not in body, attribute
        assert "<script>" not in body, "no inline script block"

    def test_the_controls_keep_the_44px_target(self):
        css = (DASHBOARD_DIR / "static" / "style.css").read_text()
        for selector in (".proxy-field .limits-input", ".proxy-icon-btn"):
            assert selector in css, selector
        assert "min-height: 44px" in css

    def test_the_proxy_block_sets_no_colour_literal(self):
        css = (DASHBOARD_DIR / "static" / "style.css").read_text()
        block = css.split("/* ----- Proxy (admin → proxy)", 1)[1]
        for token in ("#fff", "#000", "rgb(", "hsl("):
            assert token not in block, token

    def test_the_table_never_widens_a_phone(self):
        """The list scrolls inside its card; the page itself never does."""
        css = (DASHBOARD_DIR / "static" / "style.css").read_text()
        assert ".table-scroll" in css
        block = css.split("/* ----- Proxy (admin → proxy)", 1)[1]
        # The narrow layout stacks the cells instead of keeping six columns.
        assert "@media (max-width: 600px)" in block
        assert ".proxy-table thead" in block
        assert "overflow-wrap: anywhere" in block
