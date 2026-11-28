"""Proxy connections: the table, the migration, the secrets and the endpoint.

What these tests pin is the part that only shows up on a real server:

* **the old settings migrate once** — ``egress.mode=manual`` becomes one
  ``Default`` connection (kind from the URL's scheme), ``egress.mode=wireguard``
  becomes a tunnel row only when its config file exists, and a second start
  duplicates nothing;
* **only one WireGuard row** — the tunnel is one config file, so a second
  ``kind=wireguard`` answers ``OnlyOneWireGuard``;
* **a password is a file, not a column** — it lands in
  ``EGRESS_PATH/secrets/<id>.txt`` with mode 0600, the row keeps only
  ``password_ref``, and neither ``GET /api/egress`` nor a connection update ever
  renders it back;
* **a check is stored** — ``POST /api/egress/test/<id>`` writes ``status``,
  ``last_test_at`` and a ``last_test_result`` with the proxy's password taken
  out of curl's own error text.

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
os.environ["DB_PATH"] = os.path.join(_tmpdir, "proxy-connections.db")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ["EGRESS_PATH"] = os.path.join(_tmpdir, "egress")
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import egress  # noqa: E402
import proxy_connections  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()

#: The value that must never appear in a JSON body, a log line or a row.
PROXY_PASSWORD = "fixture-proxy-secret"  # noqa: S105
PROXY_USER = "egress-user"
PROXY_URL = "socks5h://10.0.0.5:1080"
PROXY_WITH_CREDENTIALS = f"socks5h://{PROXY_USER}:{PROXY_PASSWORD}@10.0.0.5:1080"
TOKEN = "test-token-0123456789abcdef"  # noqa: S105
HEADERS = {"X-Internal-Token": TOKEN}

WG_CONFIG = b"""\
[Interface]
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Address = 10.8.0.2/32

[Peer]
PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
Endpoint = 198.51.100.7:51820
AllowedIPs = 0.0.0.0/0
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """A fresh database, egress directory and token for every test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "connections.db"))
    monkeypatch.setenv("EGRESS_PATH", str(tmp_path / "egress"))
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN)
    asyncio.run(db.init_db())
    yield


@pytest.fixture()
def admin() -> TestClient:
    client = TestClient(app)
    page = client.get("/login")
    assert page.status_code == 200, page.text
    token = client.cookies.get("koyura_csrf") or ""
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


def _root() -> Path:
    return Path(os.environ["EGRESS_PATH"])


def _run(coroutine):
    return asyncio.run(coroutine)


def _create(**overrides):
    fields = {
        "name": "Primary",
        "kind": "socks5h",
        "url": PROXY_URL,
        "assignment_mode": "all",
        "services": [],
    }
    fields.update(overrides)
    return _run(proxy_connections.create_connection(**fields))


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

class TestMigration:
    def _settings(self, **values) -> None:
        _run(db.save_settings({f"egress.{key}": value for key, value in values.items()}))

    def test_a_manual_rule_becomes_one_default_connection(self):
        self._settings(
            mode="manual",
            proxy_url=PROXY_WITH_CREDENTIALS,
            assignment="selected",
            services="reddit,instagram",
        )
        created = _run(proxy_connections.migrate_legacy_settings())
        assert created == 1
        rows = _run(db.list_proxy_connections())
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "Default"
        assert row["kind"] == "socks5h"
        assert row["url"] == PROXY_URL
        assert row["username"] == PROXY_USER
        assert row["assignment_mode"] == "selected"
        assert proxy_connections.services_of(row) == ["reddit", "instagram"]
        assert row["enabled"] == 1
        assert row["status"] == "unknown"
        assert row["position"] == 0

    def test_the_password_lands_in_a_0600_file_not_in_the_row(self):
        self._settings(mode="manual", proxy_url=PROXY_WITH_CREDENTIALS)
        _run(proxy_connections.migrate_legacy_settings())
        row = _run(db.list_proxy_connections())[0]
        ref = row["password_ref"]
        assert ref == "secrets/1.txt"
        assert PROXY_PASSWORD not in json.dumps(dict(row))
        path = _root() / ref
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert PROXY_PASSWORD in path.read_text()
        assert stat.S_IMODE((_root() / "secrets").stat().st_mode) == 0o700

    def test_a_url_without_a_login_stores_no_secret(self):
        self._settings(mode="manual", proxy_url=PROXY_URL)
        _run(proxy_connections.migrate_legacy_settings())
        row = _run(db.list_proxy_connections())[0]
        assert row["username"] is None
        assert row["password_ref"] is None
        assert not (_root() / "secrets" / "1.txt").exists()

    def test_a_wireguard_rule_becomes_a_tunnel_row_when_the_file_exists(self):
        self._settings(mode="wireguard", proxy_url="", assignment="all")
        _root().mkdir(parents=True, exist_ok=True)
        egress.write_wireguard(WG_CONFIG)
        _run(proxy_connections.migrate_legacy_settings())
        row = _run(db.list_proxy_connections())[0]
        assert row["kind"] == "wireguard"
        assert row["url"] == ""
        assert row["config_path"] == str(egress.wireguard_path())

    def test_a_wireguard_rule_without_a_file_creates_nothing(self):
        """A row pointing at a file nobody wrote would fail every check."""
        self._settings(mode="wireguard")
        assert _run(proxy_connections.migrate_legacy_settings()) == 0
        assert _run(db.list_proxy_connections()) == []

    def test_off_or_empty_creates_nothing(self):
        self._settings(mode="off")
        assert _run(proxy_connections.migrate_legacy_settings()) == 0
        _run(db.delete_settings("egress."))
        assert _run(proxy_connections.migrate_legacy_settings()) == 0
        assert _run(db.list_proxy_connections()) == []

    def test_a_second_run_does_not_duplicate(self):
        self._settings(mode="manual", proxy_url=PROXY_WITH_CREDENTIALS)
        assert _run(proxy_connections.migrate_legacy_settings()) == 1
        assert _run(proxy_connections.migrate_legacy_settings()) == 0
        assert len(_run(db.list_proxy_connections())) == 1
        assert _run(db.get_setting(proxy_connections.MIGRATED_KEY)) == "1"

    def test_a_later_edit_of_the_old_settings_is_ignored(self):
        self._settings(mode="manual", proxy_url=PROXY_URL)
        _run(proxy_connections.migrate_legacy_settings())
        self._settings(mode="manual", proxy_url="http://10.0.0.9:3128")
        _run(proxy_connections.migrate_legacy_settings())
        rows = _run(db.list_proxy_connections())
        assert len(rows) == 1
        assert rows[0]["url"] == PROXY_URL

    def test_a_refused_legacy_url_is_logged_and_not_retried(self, caplog):
        self._settings(mode="manual", proxy_url="ftp://10.0.0.5:1")
        with caplog.at_level(logging.WARNING, logger="koyura.dashboard"):
            assert _run(proxy_connections.migrate_legacy_settings()) == 0
        assert _run(db.get_setting(proxy_connections.MIGRATED_KEY)) == "1"
        assert any("migrate" in record.getMessage().lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# Only one WireGuard
# ---------------------------------------------------------------------------

class TestWireGuardUniqueness:
    def test_a_second_tunnel_is_refused(self):
        _create(name="Tunnel", kind="wireguard", url="")
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _create(name="Another", kind="wireguard", url="")
        assert raised.value.code == "OnlyOneWireGuard"
        assert len(_run(db.list_proxy_connections())) == 1

    def test_a_proxy_next_to_the_tunnel_is_allowed(self):
        _create(name="Tunnel", kind="wireguard", url="")
        _create(name="Primary")
        assert len(_run(db.list_proxy_connections())) == 2

    def test_updating_the_tunnel_itself_is_allowed(self):
        row = _create(name="Tunnel", kind="wireguard", url="")
        updated = _run(
            proxy_connections.update_connection(
                row["id"],
                name="Renamed tunnel",
                kind="wireguard",
                url="",
                assignment_mode="all",
                services=[],
            )
        )
        assert updated["name"] == "Renamed tunnel"

    def test_turning_a_proxy_into_a_second_tunnel_is_refused(self):
        _create(name="Tunnel", kind="wireguard", url="")
        proxy = _create(name="Primary")
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _run(
                proxy_connections.update_connection(
                    proxy["id"],
                    name="Primary",
                    kind="wireguard",
                    url="",
                    assignment_mode="all",
                    services=[],
                )
            )
        assert raised.value.code == "OnlyOneWireGuard"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestCrud:
    def test_create_stores_the_url_without_the_login(self):
        row = _create(
            url=PROXY_WITH_CREDENTIALS,
            username="",
            password=None,
        )
        assert row["url"] == PROXY_URL
        assert row["username"] == PROXY_USER
        assert row["password_ref"] == "secrets/1.txt"  # noqa: S105
        assert (_root() / "secrets" / "1.txt").read_text().strip() == (
            f"{PROXY_USER}:{PROXY_PASSWORD}"
        )

    def test_the_kind_follows_the_url_scheme(self):
        row = _create(kind="", url="http://10.0.0.9:3128")
        assert row["kind"] == "http"

    def test_a_mismatched_kind_is_refused(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _create(kind="http", url=PROXY_URL)
        assert raised.value.code == "KindMismatch"

    def test_a_password_without_a_username_is_refused(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _create(username="", password="secret")  # noqa: S106
        assert raised.value.code == "MissingUsername"

    def test_a_url_scheme_the_engine_cannot_dial_is_refused(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _create(kind="", url="ftp://10.0.0.5:1")
        assert raised.value.code == "InvalidUrl"

    def test_a_nameless_connection_is_refused(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _create(name="   ")
        assert raised.value.code == "MissingName"

    def test_a_wireguard_row_may_not_carry_a_url(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _create(kind="wireguard", url=PROXY_URL)
        assert raised.value.code == "UnexpectedUrl"

    def test_services_are_deduplicated_and_lowercased(self):
        row = _create(
            assignment_mode="selected", services=["Reddit", "reddit", " YouTube "]
        )
        assert proxy_connections.services_of(row) == ["reddit", "youtube"]

    def test_the_order_is_dense_and_a_move_changes_it(self):
        first = _create(name="First")
        second = _create(name="Second")
        third = _create(name="Third")
        assert [row["position"] for row in _run(db.list_proxy_connections())] == [0, 1, 2]
        assert _run(proxy_connections.move_connection(third["id"], -2)) is True
        names = [row["name"] for row in _run(db.list_proxy_connections())]
        assert names == ["Third", "First", "Second"]
        assert [row["position"] for row in _run(db.list_proxy_connections())] == [0, 1, 2]
        assert first["id"] != second["id"]

    def test_a_move_at_the_edge_is_a_no_op(self):
        row = _create()
        assert _run(proxy_connections.move_connection(row["id"], -1)) is True
        assert _run(db.list_proxy_connections())[0]["position"] == 0

    def test_deleting_renumbers_the_rest(self):
        _create(name="First")
        second = _create(name="Second")
        _create(name="Third")
        assert _run(proxy_connections.delete_connection(second["id"])) is True
        rows = _run(db.list_proxy_connections())
        assert [row["name"] for row in rows] == ["First", "Third"]
        assert [row["position"] for row in rows] == [0, 1]

    def test_deleting_removes_the_secret(self):
        row = _create(password=PROXY_PASSWORD, username=PROXY_USER)
        path = _root() / str(row["password_ref"])
        assert path.exists()
        assert _run(proxy_connections.delete_connection(row["id"])) is True
        assert not path.exists()

    def test_enabling_and_disabling(self):
        row = _create()
        assert _run(proxy_connections.set_enabled(row["id"], False)) is True
        assert (_run(db.get_proxy_connection(row["id"])) or {})["enabled"] == 0
        assert _run(proxy_connections.set_enabled(row["id"], True)) is True
        assert (_run(db.get_proxy_connection(row["id"])) or {})["enabled"] == 1

    def test_a_blank_password_on_update_keeps_the_stored_one(self):
        row = _create(username=PROXY_USER, password=PROXY_PASSWORD)
        updated = _run(
            proxy_connections.update_connection(
                row["id"],
                name="Renamed",
                kind="socks5h",
                url=PROXY_URL,
                username=PROXY_USER,
                password=None,
                assignment_mode="all",
                services=[],
            )
        )
        assert updated["name"] == "Renamed"
        assert updated["password_ref"] == row["password_ref"]
        assert PROXY_PASSWORD in (_root() / "secrets" / "1.txt").read_text()

    def test_a_new_username_with_a_blank_password_reuses_the_password(self):
        row = _create(username=PROXY_USER, password=PROXY_PASSWORD)
        _run(
            proxy_connections.update_connection(
                row["id"],
                name="Primary",
                kind="socks5h",
                url=PROXY_URL,
                username="second-user",
                password=None,
                assignment_mode="all",
                services=[],
            )
        )
        stored = (_root() / "secrets" / "1.txt").read_text().strip()
        assert stored == f"second-user:{PROXY_PASSWORD}"

    def test_clearing_the_username_removes_the_secret(self):
        row = _create(username=PROXY_USER, password=PROXY_PASSWORD)
        updated = _run(
            proxy_connections.update_connection(
                row["id"],
                name="Primary",
                kind="socks5h",
                url=PROXY_URL,
                username="",
                password=None,
                assignment_mode="all",
                services=[],
            )
        )
        assert updated["password_ref"] is None
        assert not (_root() / "secrets" / "1.txt").exists()

    def test_a_missing_connection_is_an_error_not_a_row(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            _run(
                proxy_connections.update_connection(
                    999,
                    name="Ghost",
                    kind="socks5h",
                    url=PROXY_URL,
                    assignment_mode="all",
                    services=[],
                )
            )
        assert raised.value.code == "NotFound"


# ---------------------------------------------------------------------------
# The endpoint the engine reads
# ---------------------------------------------------------------------------

def _get(path: str) -> dict:
    return TestClient(app).get(path, headers=HEADERS).json()


class TestEnginePayload:
    def test_the_documented_shape(self):
        _create(name="Primary", assignment_mode="selected", services=["reddit"])
        payload = _get("/api/egress")
        assert set(payload) == {"connections", "test_interval"}
        connection = payload["connections"][0]
        assert set(connection) == {
            "id", "name", "kind", "url", "assignment", "enabled", "status",
        }
        assert connection["assignment"] == {"mode": "selected", "services": ["reddit"]}
        assert connection["url"] == PROXY_URL
        assert connection["enabled"] is True
        assert connection["status"] == "unknown"

    def test_no_login_and_no_path_is_ever_published(self):
        _create(username=PROXY_USER, password=PROXY_PASSWORD)
        _create(name="Tunnel", kind="wireguard", url="")
        body = json.dumps(_get("/api/egress"))
        assert PROXY_PASSWORD not in body
        assert PROXY_USER not in body
        assert "password_ref" not in body
        assert "config_path" not in body
        assert "secrets/" not in body

    def test_a_url_that_still_carries_a_login_is_masked(self):
        """A hand-edited row must not leak through the endpoint either."""
        row = _create()
        _run(
            db.update_proxy_connection(
                row["id"], url=PROXY_WITH_CREDENTIALS
            )
        )
        payload = _get("/api/egress")
        assert PROXY_PASSWORD not in json.dumps(payload)
        assert "***:***" in payload["connections"][0]["url"]

    def test_the_order_is_the_engine_order(self):
        _create(name="Second")
        row = _create(name="First")
        _run(proxy_connections.move_connection(row["id"], -1))
        names = [entry["name"] for entry in _get("/api/egress")["connections"]]
        assert names == ["First", "Second"]

    def test_the_internal_token_is_required(self):
        client = TestClient(app)
        assert client.get("/api/egress").status_code == 401
        assert client.get(
            "/api/egress", headers={"X-Internal-Token": "wrong"}
        ).status_code == 401

    def test_a_fresh_install_answers_an_empty_list(self):
        assert _get("/api/egress") == {"connections": [], "test_interval": 0}


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

class _Done:
    returncode = 0
    stdout = "203.0.113.9\n"
    stderr = ""


class _Failed:
    returncode = 5
    stdout = ""
    stderr = "curl: (5) Could not resolve proxy: {url}\n"


class TestCheck:
    def test_a_successful_check_stores_the_ip(self, monkeypatch):
        row = _create()
        monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: _Done())
        response = TestClient(app).post(
            f"/api/egress/test/{row['id']}", headers=HEADERS
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["last_test_result"] == "203.0.113.9"
        assert body["last_test_at"]
        stored = _run(db.get_proxy_connection(row["id"])) or {}
        assert stored["status"] == "ok"
        assert stored["last_test_result"] == "203.0.113.9"

    def test_the_probe_dials_the_url_with_the_stored_credential(self, monkeypatch):
        _create(username=PROXY_USER, password=PROXY_PASSWORD)
        seen: list[list[str]] = []

        def _run_curl(cmd, **kwargs):
            seen.append(cmd)
            return _Done()

        monkeypatch.setattr(egress.subprocess, "run", _run_curl)
        TestClient(app).post("/api/egress/test/1", headers=HEADERS)
        assert seen[0][seen[0].index("--proxy") + 1] == PROXY_WITH_CREDENTIALS

    def test_a_failed_check_masks_the_password(self, monkeypatch):
        row = _create(username=PROXY_USER, password=PROXY_PASSWORD)

        def _run_curl(cmd, **kwargs):
            failed = _Failed()
            failed.stderr = f"curl: (5) Could not resolve proxy: {cmd[-3]}\n"
            return failed

        monkeypatch.setattr(egress.subprocess, "run", _run_curl)
        body = TestClient(app).post(
            f"/api/egress/test/{row['id']}", headers=HEADERS
        ).json()
        assert body["status"] == "fail"
        assert PROXY_PASSWORD not in body["last_test_result"]
        assert "Could not resolve proxy" in body["last_test_result"]

    def test_a_dead_tunnel_is_a_failure_with_the_watchers_reason(self):
        row = _create(name="Tunnel", kind="wireguard", url="")
        _root().mkdir(parents=True, exist_ok=True)
        (_root() / "status.json").write_text(
            json.dumps({"state": "error", "error": "handshake failed"})
        )
        body = TestClient(app).post(
            f"/api/egress/test/{row['id']}", headers=HEADERS
        ).json()
        assert body["status"] == "fail"
        assert "handshake failed" in body["last_test_result"]

    def test_a_tunnel_without_a_report_is_a_failure(self):
        row = _create(name="Tunnel", kind="wireguard", url="")
        body = TestClient(app).post(
            f"/api/egress/test/{row['id']}", headers=HEADERS
        ).json()
        assert body["status"] == "fail"
        assert "watcher" in body["last_test_result"]

    def test_the_check_needs_the_internal_token(self):
        row = _create()
        assert TestClient(app).post(f"/api/egress/test/{row['id']}").status_code == 401

    def test_an_unknown_connection_is_404(self):
        response = TestClient(app).post("/api/egress/test/999", headers=HEADERS)
        assert response.status_code == 404

    def test_no_log_line_carries_the_password(self, monkeypatch, caplog):
        _create(username=PROXY_USER, password=PROXY_PASSWORD)
        monkeypatch.setattr(egress.subprocess, "run", lambda *a, **k: _Done())
        with caplog.at_level(logging.DEBUG, logger="koyura.dashboard"):
            TestClient(app).post("/api/egress/test/1", headers=HEADERS)
            _get("/api/egress")
        for record in caplog.records:
            assert PROXY_PASSWORD not in record.getMessage()


# ---------------------------------------------------------------------------
# The re-test interval
# ---------------------------------------------------------------------------

class TestInterval:
    def test_the_default_is_never(self):
        assert _run(proxy_connections.test_interval_minutes()) == 0

    def test_a_stored_value_is_read(self):
        _run(proxy_connections.set_test_interval(30))
        assert _run(proxy_connections.test_interval_minutes()) == 30
        assert _get("/api/egress")["test_interval"] == 30

    def test_a_nonsense_value_falls_back_to_never(self):
        _run(db.save_settings({proxy_connections.TEST_INTERVAL_KEY: "soon"}))
        assert _run(proxy_connections.test_interval_minutes()) == 0

    def test_it_is_clamped(self):
        assert _run(proxy_connections.set_test_interval(-5)) == 0
        assert (
            _run(proxy_connections.set_test_interval(10_000))
            == proxy_connections.MAX_TEST_INTERVAL_MINUTES
        )


# ---------------------------------------------------------------------------
# The editor's endpoints, and the retired form
# ---------------------------------------------------------------------------
#
# The page owns the list now, so the endpoints the editor posts to are part of
# the contract these tests pin: a save is a save, a second tunnel is refused with
# a code the dialog can show, and the single-rule form that predated the list is
# gone rather than silently half-working.

class TestAddress:
    """Host + port in the editor, one canonical URL in the row.

    The protocol select decides the scheme, so the address is two fields. What
    is stored must be exactly what the old single URL box stored — the engine
    and the internal endpoint read that column — so these tests are about the
    assembly, not about a second format.
    """

    def test_host_and_port_become_the_schemes_url(self):
        assert proxy_connections.proxy_url_for("http", "138.124.89.104", "3128") == (
            "http://138.124.89.104:3128"
        )
        assert proxy_connections.proxy_url_for("socks5h", "proxy.example.com", 1080) == (
            "socks5h://proxy.example.com:1080"
        )

    def test_the_port_is_normalised_to_a_number(self):
        assert proxy_connections.proxy_url_for("https", "h.example", " 0443 ") == (
            "https://h.example:443"
        )

    def test_an_ipv6_host_keeps_its_brackets(self):
        assert proxy_connections.proxy_url_for("socks5", "[2a01:4f8::1]", "1080") == (
            "socks5://[2a01:4f8::1]:1080"
        )

    def test_an_ipv6_host_without_brackets_is_a_sentence(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            proxy_connections.proxy_url_for("socks5", "2a01:4f8::1", "1080")
        assert raised.value.code == "InvalidHost"
        assert "brackets" in str(raised.value)

    def test_a_host_typed_with_its_own_port_is_accepted(self):
        """The single-bar habit: ``host:port`` in the host field still works."""
        assert proxy_connections.proxy_url_for("http", "10.0.0.5:8080", "") == (
            "http://10.0.0.5:8080"
        )
        # And a port typed in the field wins over the one in the host.
        assert proxy_connections.proxy_url_for("http", "10.0.0.5:8080", "9090") == (
            "http://10.0.0.5:9090"
        )

    @pytest.mark.parametrize(
        "host, port, code",
        (
            ("", "1080", "MissingHost"),
            ("10.0.0.5", "", "MissingPort"),
            ("10.0.0.5", "0", "InvalidPort"),
            ("10.0.0.5", "65536", "InvalidPort"),
            ("10.0.0.5", "ten", "InvalidPort"),
            ("http://10.0.0.5", "1080", "InvalidHost"),
            ("10.0.0.5/path", "1080", "InvalidHost"),
            ("10.0.0.5?x", "1080", "InvalidHost"),
            ("user@10.0.0.5", "1080", "InvalidHost"),
            ("10.0.0 5", "1080", "InvalidHost"),
            ("[not-a-v6]", "1080", "InvalidHost"),
        ),
    )
    def test_a_bad_address_is_a_sentence_with_a_code(self, host, port, code):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            proxy_connections.proxy_url_for("socks5h", host, port)
        assert raised.value.code == code
        assert str(raised.value).strip().endswith(".")

    def test_the_scheme_comes_from_the_protocol_not_the_host(self):
        with pytest.raises(proxy_connections.ProxyConnectionError) as raised:
            proxy_connections.proxy_url_for("" , "10.0.0.5", "1080")
        assert raised.value.code == "InvalidKind"

    def test_split_address_is_the_editor_s_two_fields(self):
        assert proxy_connections.split_address("socks5h://10.0.0.5:1080") == (
            "socks5h", "10.0.0.5", "1080"
        )
        assert proxy_connections.split_address("socks5://[2a01:4f8::1]:1080") == (
            "socks5", "[2a01:4f8::1]", "1080"
        )
        assert proxy_connections.split_address("") == ("", "", "")
        assert proxy_connections.split_address("not a url")[1] == ""

    def test_a_round_trip_keeps_the_url(self):
        for url in (
            "socks5h://10.0.0.5:1080",
            "http://proxy.example.com:3128",
            "https://[2a01:4f8::1]:443",
        ):
            scheme, host, port = proxy_connections.split_address(url)
            assert proxy_connections.proxy_url_for(scheme, host, port) == url

    def test_the_editor_creates_the_row_from_two_fields(self, admin):
        response = admin.post(
            "/admin/proxy/connection",
            data={
                "name": "Split",
                "kind": "http",
                "host": "138.124.89.104",
                "port": "3128",
                "username": "",
                "password": "",
                "assignment_mode": "all",
                "csrf_token": admin.cookies.get("koyura_csrf") or "",
            },
            headers={"X-Requested-With": "fetch"},
        )
        assert response.status_code == 201, response.text
        # What the engine reads is the canonical URL, unchanged by the split.
        assert _get("/api/egress")["connections"][0]["url"] == (
            "http://138.124.89.104:3128"
        )

    def test_a_bad_port_is_refused_inside_the_dialog(self, admin):
        response = admin.post(
            "/admin/proxy/connection",
            data={
                "name": "Split",
                "kind": "http",
                "host": "10.0.0.5",
                "port": "70000",
                "assignment_mode": "all",
                "csrf_token": admin.cookies.get("koyura_csrf") or "",
            },
            headers={"X-Requested-With": "fetch"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "InvalidPort"
        assert "1 and 65535" in response.json()["error"]


class TestEditorEndpoints:
    def _post(self, client: TestClient, action: str, **overrides):
        data = {
            "name": "Primary",
            "kind": "socks5h",
            "url": PROXY_URL,
            "username": "",
            "password": "",
            "assignment_mode": "all",
            "csrf_token": client.cookies.get("koyura_csrf") or "",
        }
        data.update(overrides)
        return client.post(
            action, data=data, headers={"X-Requested-With": "fetch"}
        )

    def test_a_save_creates_the_connection_the_engine_reads(self, admin):
        response = self._post(
            admin, "/admin/proxy/connection", url=PROXY_WITH_CREDENTIALS
        )
        assert response.status_code == 201
        payload = _get("/api/egress")
        assert len(payload["connections"]) == 1
        assert payload["connections"][0]["url"] == PROXY_URL
        assert PROXY_PASSWORD not in json.dumps(payload)

    def test_a_save_answers_the_row_the_table_patches(self, admin):
        body = self._post(admin, "/admin/proxy/connection").json()
        assert body["ok"] is True
        row = body["row"]
        assert row["name"] == "Primary"
        assert row["kind_label"] == "SOCKS5H"
        assert row["status_badge"] == "badge"
        assert row["assignment_label"] == "All services"

    def test_an_edit_updates_instead_of_duplicating(self, admin):
        created = self._post(admin, "/admin/proxy/connection").json()["row"]
        self._post(
            admin,
            f"/admin/proxy/connection/{created['id']}",
            assignment_mode="selected",
            services=["reddit"],
        )
        payload = _get("/api/egress")
        assert len(payload["connections"]) == 1
        assert payload["connections"][0]["assignment"] == {
            "mode": "selected",
            "services": ["reddit"],
        }

    def test_a_second_tunnel_is_refused_with_a_code(self, admin):
        self._post(admin, "/admin/proxy/connection", name="T1", kind="wireguard", url="")
        response = self._post(
            admin, "/admin/proxy/connection", name="T2", kind="wireguard", url=""
        )
        assert response.status_code == 400
        assert response.json()["code"] == "OnlyOneWireGuard"

    def test_a_plain_form_gets_a_redirect_not_json(self, admin):
        response = admin.post(
            "/admin/proxy/connection",
            data={
                "name": "Plain",
                "kind": "socks5h",
                "url": PROXY_URL,
                "assignment_mode": "all",
                "csrf_token": admin.cookies.get("koyura_csrf") or "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin/proxy")

    def test_the_retired_rule_form_no_longer_answers(self, admin):
        response = admin.post(
            "/admin/proxy",
            data={
                "mode": "manual",
                "proxy_url": PROXY_WITH_CREDENTIALS,
                "csrf_token": admin.cookies.get("koyura_csrf") or "",
            },
            follow_redirects=False,
        )
        assert response.status_code in (404, 405)
        assert _get("/api/egress")["connections"] == []

    def test_removing_the_config_forgets_the_tunnel_row(self, admin):
        egress.write_wireguard(WG_CONFIG)
        self._post(admin, "/admin/proxy/connection", name="T", kind="wireguard", url="")
        response = admin.post(
            "/admin/proxy/wireguard/remove",
            data={"csrf_token": admin.cookies.get("koyura_csrf") or ""},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert _get("/api/egress")["connections"] == []


# ---------------------------------------------------------------------------
# The automatic tests
# ---------------------------------------------------------------------------

class TestAutoTests:
    def _enabled(self):
        return _run(
            proxy_connections.create_connection(
                name="Sweep", kind="socks5h", url=PROXY_URL,
                assignment_mode="all", services=[],
            )
        )

    def _recorder(self, monkeypatch, calls: list[int]) -> None:
        """A probe that records the row instead of opening a socket."""

        async def _probe(connection_id):
            calls.append(connection_id)

        monkeypatch.setattr(proxy_connections, "test_connection", _probe)

    def test_off_never_probes(self, monkeypatch):
        self._enabled()
        calls: list[int] = []
        self._recorder(monkeypatch, calls)
        assert _run(proxy_connections.run_due_tests()) == 0
        assert calls == []

    def test_a_due_row_is_probed_and_a_fresh_one_is_not(self, monkeypatch):
        row = self._enabled()
        calls: list[int] = []
        self._recorder(monkeypatch, calls)
        _run(proxy_connections.set_test_interval(30))
        assert _run(proxy_connections.run_due_tests()) == 1
        assert calls == [row["id"]]

        # Now the row has just been tested: the same sweep leaves it alone.
        calls.clear()
        _run(
            db.record_proxy_connection_test(
                row["id"], status="ok", result="203.0.113.9"
            )
        )
        assert _run(proxy_connections.run_due_tests()) == 0
        assert calls == []

    def test_an_old_verdict_comes_due_again(self, monkeypatch):
        row = self._enabled()
        _run(proxy_connections.set_test_interval(5))
        _run(
            db.record_proxy_connection_test(
                row["id"],
                status="ok",
                result="203.0.113.9",
                tested_at="2020-01-01 00:00:00",
            )
        )
        self._recorder(monkeypatch, [])
        assert _run(proxy_connections.run_due_tests()) == 1

    def test_a_disabled_row_is_never_swept(self, monkeypatch):
        row = self._enabled()
        _run(proxy_connections.set_enabled(row["id"], False))
        _run(proxy_connections.set_test_interval(5))
        self._recorder(monkeypatch, [])
        assert _run(proxy_connections.run_due_tests()) == 0

    def test_the_environment_pins_the_interval_and_off_wins(self, monkeypatch):
        _run(proxy_connections.set_test_interval(60))
        monkeypatch.setenv(proxy_connections.TEST_INTERVAL_ENV, "off")
        assert _run(proxy_connections.effective_test_interval()) == 0
        assert _run(proxy_connections.run_due_tests()) == 0

    def test_the_environment_can_shorten_the_interval(self, monkeypatch):
        _run(proxy_connections.set_test_interval(180))
        monkeypatch.setenv(proxy_connections.TEST_INTERVAL_ENV, "5")
        assert _run(proxy_connections.effective_test_interval()) == 5

    def test_a_nonsense_environment_value_falls_back_to_the_row(self, monkeypatch):
        _run(proxy_connections.set_test_interval(15))
        monkeypatch.setenv(proxy_connections.TEST_INTERVAL_ENV, "soon")
        assert _run(proxy_connections.effective_test_interval()) == 15

    def test_the_loop_sweeps_then_sleeps_for_the_tick(self, monkeypatch):
        """One turn of the loop: a sweep, then a tick — and cancelling is clean."""
        sweeps: list[int] = []
        ticks: list[float] = []

        async def _sweep():
            sweeps.append(1)
            return 1

        async def _sleep(seconds):
            ticks.append(seconds)
            # Stand in for the cancellation a shutdown delivers: the loop has
            # done one full turn, which is all this test asserts.
            raise asyncio.CancelledError

        monkeypatch.setattr(proxy_connections, "run_due_tests", _sweep)
        monkeypatch.setattr(proxy_connections.asyncio, "sleep", _sleep)

        async def _once():
            with pytest.raises(asyncio.CancelledError):
                await proxy_connections.auto_test_loop(tick_seconds=42)

        asyncio.run(_once())
        assert sweeps == [1]
        assert ticks == [42]
