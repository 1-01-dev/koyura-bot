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
from main import create_app  # noqa: E402

app = create_app()
DASHBOARD_DIR = Path(__file__).resolve().parents[1]

#: The value that must never appear in a page, a JSON body or a log line. Named
#: as a fixture value, not a credential: it is deliberately fake.
PROXY_PASSWORD = "fixture-proxy-secret"  # noqa: S105
# Deliberately not "koyura": that word appears in every stylesheet link.
PROXY_USER = "egress-user"

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

class TestPage:
    def test_the_page_needs_a_session(self):
        response = TestClient(app).get("/admin/proxy", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_page_renders_every_control(self, admin):
        body = admin.get("/admin/proxy").text
        assert 'id="proxy-card"' in body
        assert 'id="wireguard-card"' in body
        for value in ("off", "manual", "wireguard"):
            assert f'id="mode-{value}"' in body
        for value in ("all", "selected", "except"):
            assert f'id="assign-{value}"' in body
        assert 'name="proxy_url"' in body
        assert 'name="wg_file"' in body
        assert 'name="services"' in body

    def test_the_form_carries_csrf_and_is_multipart_for_the_upload(self, admin):
        body = admin.get("/admin/proxy").text
        assert body.count('name="csrf_token"') >= 4
        assert 'enctype="multipart/form-data"' in body

    def test_the_page_mentions_the_supported_schemes(self, admin):
        body = admin.get("/admin/proxy").text
        for scheme in ("socks5://", "socks5h://", "http://", "https://"):
            assert scheme in body

    def test_saving_manual_stores_the_rule(self, admin):
        resp = _save(admin)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/proxy?egress_saved=1"
        stored = asyncio.run(db.get_settings(prefix=egress.SETTING_PREFIX))
        assert stored["egress.mode"] == "manual"
        assert stored["egress.proxy_url"] == PROXY_URL

    def test_saving_off_returns_to_a_direct_connection(self, admin):
        _save(admin)
        _save(admin, mode="off", proxy_url="")
        stored = asyncio.run(db.get_settings(prefix=egress.SETTING_PREFIX))
        assert stored["egress.mode"] == "off"
        assert stored["egress.proxy_url"] == ""

    def test_saving_wireguard_stores_the_mode_without_a_url(self, admin):
        _save(admin, mode="wireguard", proxy_url="")
        stored = asyncio.run(db.get_settings(prefix=egress.SETTING_PREFIX))
        assert stored["egress.mode"] == "wireguard"
        assert stored["egress.proxy_url"] == ""

    def test_saving_selected_stores_the_chosen_services(self, admin):
        _save(admin, assignment_mode="selected", services=["reddit", "instagram"])
        stored = asyncio.run(db.get_settings(prefix=egress.SETTING_PREFIX))
        assert stored["egress.assignment"] == "selected"
        assert stored["egress.services"] == "reddit,instagram"

    def test_an_invalid_url_renders_the_error_and_stores_nothing(self, admin):
        resp = _save(admin, proxy_url="ftp://10.0.0.5:1")
        assert resp.status_code == 400
        assert "socks5" in resp.text
        stored = asyncio.run(db.get_settings(prefix=egress.SETTING_PREFIX))
        assert stored.get("egress.mode") is None

    def test_a_rejected_field_keeps_what_was_typed(self, admin):
        resp = _save(
            admin, assignment_mode="selected", services=["reddit"],
            proxy_url="ftp://nope",
        )
        assert resp.status_code == 400
        body = resp.text
        # The two radios the operator chose are still selected.
        assert 'id="mode-manual"' in body
        assert 'id="assign-selected"' in body
        # A validation error must never echo the password into the page.
        assert PROXY_PASSWORD not in body

    def test_a_missing_csrf_is_refused_everywhere(self, admin):
        for action in (
            "/admin/proxy",
            "/admin/proxy/wireguard",
            "/admin/proxy/wireguard/remove",
            "/admin/proxy/status",
            "/admin/proxy/check",
        ):
            response = admin.post(action, data={"mode": "manual"})
            assert response.status_code == 403, action

    def test_the_endpoints_need_a_session(self):
        client = TestClient(app)
        for action in ("/admin/proxy", "/admin/proxy/check"):
            response = client.post(action, data={}, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"

    def test_the_old_admin_link_lands_on_the_proxy_page(self, admin):
        response = admin.get("/admin?egress_saved=1", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/proxy?egress_saved=1"


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
        assert resp.status_code == 400
        assert "[Peer]" in resp.text
        assert not (_root() / "wg0.conf").exists()

    def test_an_oversized_upload_is_refused(self, admin):
        resp = self._upload(admin, b"x" * (egress.MAX_WG_BYTES + 1))
        assert resp.status_code == 400
        assert "64 KB" in resp.text

    def test_an_empty_upload_is_refused(self, admin):
        resp = self._upload(admin, b"")
        assert resp.status_code == 400

    def test_no_file_at_all_is_refused(self, admin):
        resp = admin.post(
            "/admin/proxy/wireguard",
            data={"csrf_token": _csrf(admin)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "Choose a WireGuard" in resp.text

    def test_disconnect_deletes_the_config(self, admin):
        self._upload(admin, WG_CONFIG)
        resp = admin.post(
            "/admin/proxy/wireguard/remove",
            data={"csrf_token": _csrf(admin)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/proxy?wg_removed=1"
        assert not (_root() / "wg0.conf").exists()

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


class TestCheckEndpoint:
    def test_a_failed_check_is_shown_on_the_page(self, admin):
        class _Failed:
            returncode = 5
            stdout = ""
            stderr = "curl: (5) Could not resolve proxy\n"

        import egress as module

        original = module.subprocess.run
        module.subprocess.run = lambda *a, **k: _Failed()
        try:
            resp = admin.post(
                "/admin/proxy/check",
                data={"csrf_token": _csrf(admin), "proxy_url": PROXY_URL},
            )
        finally:
            module.subprocess.run = original
        assert resp.status_code == 200
        assert "Check failed" in resp.text
        assert PROXY_PASSWORD not in resp.text

    def test_a_check_in_the_query_string_is_never_used(self, admin):
        """The URL under test must not travel in a URL: it can hold a password."""
        resp = admin.get("/admin/proxy?proxy_url=leak")
        assert resp.status_code == 200
        assert PROXY_PASSWORD not in resp.text


# ---------------------------------------------------------------------------
# The internal endpoint the engine reads
# ---------------------------------------------------------------------------

class TestInternalEndpoint:
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
        _save(admin, assignment_mode="selected", services=["reddit"])
        response = TestClient(app).get(
            "/api/egress",
            headers={"X-Internal-Token": "test-token-0123456789abcdef"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"connections", "test_interval"}
        connection = payload["connections"][0]
        assert connection["name"] == "Default"
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
        _save(admin, mode="wireguard", proxy_url="")
        payload = TestClient(app).get(
            "/api/egress",
            headers={"X-Internal-Token": "test-token-0123456789abcdef"},
        ).json()
        assert payload["connections"][0]["kind"] == "wireguard"
        assert payload["connections"][0]["url"] == ""


# ---------------------------------------------------------------------------
# Secrecy
# ---------------------------------------------------------------------------

class TestSecrecy:
    def test_the_page_never_renders_the_password(self, admin):
        _save(admin)
        for path in ("/admin/proxy", "/admin/proxy?egress_saved=1"):
            body = admin.get(path).text
            assert PROXY_PASSWORD not in body, path
            assert MASKED in body or "configured" in body, path

    def test_the_masked_url_is_what_the_page_shows(self, admin):
        _save(admin)
        body = admin.get("/admin/proxy").text
        assert MASKED in body
        assert PROXY_USER not in body

    def test_no_log_line_carries_the_password(self, admin, caplog):
        with caplog.at_level(logging.DEBUG, logger="koyura.dashboard"):
            _save(admin)
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

    def test_the_database_stores_the_url_because_the_engine_needs_it(self, admin):
        """The password is *stored* (it must be dialled); it is never *rendered*."""
        _save(admin)
        stored = asyncio.run(db.get_settings(prefix=egress.SETTING_PREFIX))
        assert stored["egress.proxy_url"] == PROXY_URL
        body = admin.get("/admin/proxy").text
        assert PROXY_PASSWORD not in body

    def test_the_answered_json_never_reaches_a_page(self, admin, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token-0123456789abcdef")
        _save(admin)
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
        for selector in (".proxy-choice", ".proxy-input", ".proxy-check .btn"):
            assert selector in css, selector
        assert "min-height: 44px" in css

    def test_the_proxy_block_sets_no_colour_literal(self):
        css = (DASHBOARD_DIR / "static" / "style.css").read_text()
        block = css.split("/* ----- Proxy (admin → proxy)", 1)[1]
        for token in ("#fff", "#000", "rgb(", "hsl("):
            assert token not in block, token
