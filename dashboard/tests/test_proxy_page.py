"""The Proxy page: the list, the editor, the row actions and their script.

What these tests pin is the part that only shows up in a browser with the module
loaded — the wiring between the markup and `js/proxy*.js` — plus the three
promises the page makes about a secret:

* **the password is never in the HTML.** A stored credential shows up as
  `has_password: true` and a hint, never as a value; the field is empty so
  "blank keeps it" is the only thing that can happen next.
* **a refusal is shown, not swallowed.** A second WireGuard row answers
  `OnlyOneWireGuard` and the drawer stays open.
* **the table works without the script.** Every row's Test and Enable/Disable are
  real forms that post the same endpoints, so the page is usable before the
  module runs and if it never does.

Environment must be set BEFORE importing auth/main/db.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "proxy-page.db")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ["EGRESS_PATH"] = os.path.join(_tmpdir, "egress")
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import proxy_connections  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
STATIC = DASHBOARD_DIR / "static"
MODULES = STATIC / "js"
TEMPLATES = DASHBOARD_DIR / "templates"

PROXY_PASSWORD = "page-fixture-secret"  # noqa: S105
PROXY_USER = "page-fixture-user"
PROXY_URL = "socks5h://10.0.0.7:1080"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    asyncio.run(db.init_db())


@pytest.fixture(autouse=True)
def _clean():
    async def _reset():
        for row in await db.list_proxy_connections():
            await proxy_connections.delete_connection(int(row["id"]))
        await proxy_connections.set_test_interval(0)

    asyncio.run(_reset())
    yield


def _csrf(client: TestClient) -> str:
    return client.cookies.get("koyura_csrf") or ""


@pytest.fixture()
def admin() -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]*)"', page.text)
    assert match is not None, "the login form has no CSRF field"
    token = match.group(1)
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


def _create(client: TestClient, **overrides) -> dict:
    data = {
        "name": "Primary",
        "kind": "socks5h",
        "url": PROXY_URL,
        "username": "",
        "password": "",
        "assignment_mode": "all",
        "csrf_token": _csrf(client),
    }
    data.update(overrides)
    response = client.post(
        "/admin/proxy/connection",
        data=data,
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. What the page renders
# ---------------------------------------------------------------------------

def test_the_title_and_the_summary(admin):
    body = admin.get("/admin/proxy").text
    assert '<h1 class="page-title">Connections and routing rules</h1>' in body
    assert "0 connection" in body
    assert 'id="proxy-mode"' in body
    assert ">Off<" in body


def test_the_summary_counts_and_names_the_mode(admin):
    _create(admin, name="One")
    _create(admin, name="Two", kind="wireguard", url="")
    body = admin.get("/admin/proxy").text
    assert "2 connections" in body
    # A live tunnel is the stronger claim: the header says WireGuard.
    assert ">WireGuard<" in body


def test_the_card_header_carries_the_help_and_the_menu(admin):
    body = admin.get("/admin/proxy").text
    assert '<h2 class="card-title">Connections</h2>' in body
    assert "Manage proxy and tunnel routes" in body
    assert 'aria-controls="proxy-note"' in body
    assert 'id="proxy-menu-toggle"' in body
    assert 'aria-expanded="false"' in body
    assert "+ Add connection" in body


def test_the_empty_state_says_what_to_do(admin):
    body = admin.get("/admin/proxy").text
    assert "No connections yet" in body
    assert "Add a proxy or WireGuard tunnel to route service traffic." in body
    assert 'aria-hidden="true">空</span>' in body


def test_the_empty_state_does_not_repeat_the_add_button(admin):
    """One "+ Add connection" per screen: the header's.

    The empty state used to carry a second copy of the button, directly under a
    sentence that says the same thing. The header keeps the only one, so the
    page has one way to open the editor and a phone's first view is not two
    identical calls to action.
    """
    body = admin.get("/admin/proxy").text
    assert body.count("+ Add connection") == 1
    empty = body.split('id="proxy-empty"', 1)[1].split("</div>", 1)[0]
    assert "Add connection" not in empty
    assert 'id="proxy-empty-add"' not in body


def test_a_row_carries_the_five_columns_and_the_actions(admin):
    _create(admin, name="Media proxy", assignment_mode="selected",
            services=["reddit", "instagram"])
    body = admin.get("/admin/proxy").text
    for header in ("Name", "Status", "Protocol", "Applied to", "Last test"):
        assert f">{header}<" in body, header
    assert "Media proxy" in body
    assert PROXY_URL in body
    assert "SOCKS5H" in body
    assert "Selected · 2" in body
    for action in ("test", "toggle", "edit", "delete"):
        assert f'data-proxy-action="{action}"' in body, action


def test_the_row_actions_are_real_forms_with_csrf(admin):
    _create(admin, name="Formal")
    body = admin.get("/admin/proxy").text
    row = body.split('data-connection-id="', 1)[1]
    assert 'action="/admin/proxy/connection/' in row
    assert row.count('name="csrf_token"') >= 2
    assert 'method="post"' in row


def test_the_status_badge_matches_the_stored_verdict(admin):
    created = _create(admin)
    asyncio.run(
        db.record_proxy_connection_test(
            created["row"]["id"], status="fail", result="connection refused"
        )
    )
    body = admin.get("/admin/proxy").text
    assert "badge badge-danger" in body
    assert "connection refused" in body


def test_the_wireguard_config_lives_in_the_editor_not_a_second_card(admin):
    """The tunnel is added from the same dialog as a proxy: one upload, one row.

    There used to be a second card (``#wireguard-card``) with its own upload,
    status table, Refresh and Disconnect — a duplicate of what the editor's
    WireGuard fields already do. It is gone; the file field, the path hint and
    the watcher's status are in the dialog.
    """
    body = admin.get("/admin/proxy").text
    assert 'id="wireguard-card"' not in body
    assert 'id="wireguard-form"' not in body
    assert 'id="wg-file"' not in body
    assert 'id="wg-status"' not in body
    assert 'name="wg_file"' in body, "the editor's upload disappeared too"


# ---------------------------------------------------------------------------
# 2. The editor's markup
# ---------------------------------------------------------------------------

def test_the_modal_is_closed_and_labelled(admin):
    body = admin.get("/admin/proxy").text
    modal = body.split('id="proxy-modal"', 1)[0][-400:]
    assert "modal-overlay" in modal
    assert 'role="dialog"' in body
    assert 'aria-modal="true"' in body
    assert 'aria-labelledby="proxy-modal-title"' in body
    assert re.search(r'id="proxy-modal"[^>]*hidden', body)


def test_the_protocol_select_offers_the_five_kinds(admin):
    body = admin.get("/admin/proxy").text
    for value, label in (
        ("socks5", "SOCKS5"),
        ("socks5h", "SOCKS5H"),
        ("http", "HTTP"),
        ("https", "HTTPS"),
        ("wireguard", "WireGuard"),
    ):
        assert f'<option value="{value}">{label}</option>' in body, label


def test_the_tunnel_fields_start_hidden_and_the_proxy_fields_do_not(admin):
    body = admin.get("/admin/proxy").text
    assert re.search(r'id="proxy-wg-fields" hidden', body)
    assert not re.search(r'id="proxy-fields" hidden', body)


def test_the_apply_to_select_offers_the_three_rules(admin):
    body = admin.get("/admin/proxy").text
    assert 'id="proxy-assignment"' in body
    for mode in ("all", "selected", "except"):
        assert f'<option value="{mode}">' in body, mode


def test_the_service_picker_offers_the_catalogue_and_the_bulk_buttons(admin):
    body = admin.get("/admin/proxy").text
    assert 'id="proxy-service-list"' in body
    assert 'name="services"' in body
    assert "Select shown" in body or ">Select shown<" in body
    assert "Clear shown" in body or ">Clear shown<" in body
    # The picker starts hidden: "All services" has no list.
    assert re.search(r'id="proxy-service-picker" hidden', body)


def test_the_editor_has_a_quick_test_and_the_two_actions(admin):
    body = admin.get("/admin/proxy").text
    assert 'id="proxy-quick-test"' in body
    assert 'id="proxy-cancel"' in body
    assert 'id="proxy-save"' in body


def test_the_delete_dialog_asks_for_the_word(admin):
    body = admin.get("/admin/proxy").text
    assert 'id="proxy-delete-modal"' in body
    assert 'id="proxy-delete-confirm"' in body
    assert "Type DELETE to confirm" in body


# ---------------------------------------------------------------------------
# 3. Secrets
# ---------------------------------------------------------------------------

def test_a_stored_password_is_a_flag_not_a_value(admin):
    _create(
        admin,
        name="Secretive",
        url=f"socks5h://{PROXY_USER}:{PROXY_PASSWORD}@10.0.0.7:1080",
        username=PROXY_USER,
        password=PROXY_PASSWORD,
    )
    body = admin.get("/admin/proxy").text
    assert PROXY_PASSWORD not in body
    assert PROXY_USER not in body
    assert '"has_password": true' in body
    # The password field is rendered empty, always.
    assert re.search(r'id="proxy-password"[^>]*>', body)
    assert 'value="' not in body.split('id="proxy-password"', 1)[1][:120]


def test_a_row_without_a_password_says_so_in_the_payload(admin):
    _create(admin)
    body = admin.get("/admin/proxy").text
    assert '"has_password": false' in body


def test_the_payload_carries_the_clean_url(admin):
    """The editor needs the URL to edit; it must not carry a login."""
    _create(
        admin,
        url=f"socks5h://{PROXY_USER}:{PROXY_PASSWORD}@10.0.0.7:1080",
        username=PROXY_USER,
        password=PROXY_PASSWORD,
    )
    body = admin.get("/admin/proxy").text
    payload = re.search(r"data-payload='([^']+)'", body)
    assert payload is not None
    stored = json.loads(payload.group(1).replace("&#39;", "'"))
    assert stored["url"] == PROXY_URL
    assert PROXY_USER not in stored["url"]


# ---------------------------------------------------------------------------
# 4. A second tunnel
# ---------------------------------------------------------------------------

def test_a_second_tunnel_is_refused_into_the_dialog(admin):
    _create(admin, name="Tunnel", kind="wireguard", url="")
    response = admin.post(
        "/admin/proxy/connection",
        data={
            "name": "Tunnel 2",
            "kind": "wireguard",
            "url": "",
            "assignment_mode": "all",
            "csrf_token": _csrf(admin),
        },
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["code"] == "OnlyOneWireGuard"
    assert "Only one WireGuard" in payload["error"]


def test_a_plain_form_refusal_re_renders_the_page(admin):
    """With JavaScript off the same refusal must reach the operator too."""
    _create(admin, name="Tunnel", kind="wireguard", url="")
    response = admin.post(
        "/admin/proxy/connection",
        data={
            "name": "Tunnel 2",
            "kind": "wireguard",
            "url": "",
            "assignment_mode": "all",
            "csrf_token": _csrf(admin),
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Only one WireGuard" in response.text


# ---------------------------------------------------------------------------
# 5. The script's contract with the markup
# ---------------------------------------------------------------------------

def _module(name: str) -> str:
    return (MODULES / name).read_text()


def test_the_entry_wires_the_page():
    entry = (STATIC / "dashboard.js").read_text()
    assert "initProxyPage" in entry
    assert "'./js/proxy.js'" in entry
    assert "proxy-card" in entry


def test_every_id_the_script_reads_is_rendered(admin):
    """A typo in one of these is a dialog that never opens and no error."""
    body = admin.get("/admin/proxy").text
    for element_id in (
        "proxy-card",
        "proxy-rows",
        "proxy-menu",
        "proxy-menu-toggle",
        "proxy-modal",
        "proxy-modal-close",
        "proxy-modal-error",
        "proxy-modal-title",
        "proxy-form",
        "proxy-connection-id",
        "proxy-name",
        "proxy-kind",
        "proxy-fields",
        "proxy-wg-fields",
        "proxy-wg-file",
        "proxy-url",
        "proxy-username",
        "proxy-password",
        "proxy-password-state",
        "proxy-assignment",
        "proxy-service-picker",
        "proxy-service-list",
        "proxy-service-search",
        "proxy-service-count",
        "proxy-select-shown",
        "proxy-clear-shown",
        "proxy-quick-test",
        "proxy-test-result",
        "proxy-cancel",
        "proxy-save",
        "proxy-live",
        "proxy-count",
        "proxy-empty",
        "proxy-delete-modal",
        "proxy-delete-form",
        "proxy-delete-confirm",
        "proxy-delete-cancel",
    ):
        assert f'id="{element_id}"' in body, element_id


def test_the_script_never_builds_markup():
    for name in ("proxy.js", "proxy_rows.js", "proxy_editor.js", "proxy_picker.js"):
        source = _module(name)
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert forbidden not in source, f"{name} uses {forbidden}"


def test_the_script_falls_back_to_the_form():
    """A failed fetch must not strand the operator: the form is the floor."""
    source = _module("proxy.js")
    assert "form.submit()" in source


def test_the_script_patches_the_row_it_changed():
    source = _module("proxy_rows.js")
    assert "export function patchRow" in source
    assert "data-field" in source
    # The row's forms are what a patch has to keep honest.
    assert "toggle" in source


def test_the_script_carries_the_csrf_header():
    source = _module("proxy_rows.js")
    assert "X-CSRF-Token" in source
    assert "X-Requested-With" in source


def test_no_module_is_a_700_line_scroll():
    for path in sorted(MODULES.glob("proxy*.js")):
        assert len(path.read_text().splitlines()) < 250, path.name


def test_the_modules_import_without_a_document():
    """The entry imports them on every page; importing must not touch the DOM."""
    if not _node_available():
        pytest.skip("node is not installed")
    for name in ("proxy_rows.js", "proxy_picker.js", "proxy_editor.js"):
        script = f"import('{(MODULES / name).as_uri()}').then(() => process.stdout.write('ok'))"
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"
        assert result.stdout == "ok", name


def _node_available() -> bool:
    try:
        subprocess.run(["node", "-v"], capture_output=True, check=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# ---------------------------------------------------------------------------
# 6. The stylesheet
# ---------------------------------------------------------------------------

def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(), flags=re.S)


def test_the_narrow_layout_stacks_the_table():
    """At 390px six columns cannot fit; the cells become a stacked list."""
    css = _css()
    narrow = css.split("@media (max-width: 600px)", 1)[1]
    assert ".proxy-table thead" in narrow
    assert "display: block" in narrow


def test_a_long_verdict_cannot_widen_the_page():
    css = _css()
    note = css.split(".proxy-test-note {", 1)[1].split("}", 1)[0]
    assert "overflow-wrap: anywhere" in note
    assert "-webkit-line-clamp" in note


def test_the_row_actions_wrap_inside_their_cell():
    css = _css()
    actions = css.split(".proxy-actions {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in actions


def test_the_dialog_scrolls_instead_of_the_page():
    css = _css()
    card = css.split(".proxy-modal-card {", 1)[1].split("}", 1)[0]
    assert "max-height" in card
    assert "overflow-y: auto" in card


def test_the_interval_menu_is_hidden_until_opened():
    css = _css()
    assert ".menu-pop[hidden]" in css
    assert "display: none" in css.split(".menu-pop[hidden] {", 1)[1].split("}", 1)[0]


def test_the_page_sets_no_colour_literal():
    block = _css().split(".proxy-card-heading", 1)[1]
    for token in ("#fff", "#000", "rgb(", "hsl("):
        assert token not in block, token
