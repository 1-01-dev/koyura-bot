"""Guards for the wave-2 web layer: module split, no inline code, no literals.

The visual contract lives in ``test_ui_design.py``; this module checks the
*structural* choices made when the admin JavaScript and the loader page were
split into small files:

* the dashboard entry is one ES module and it is the only script the pages add,
* every action hook in a template is wired by name in that entry,
* admin actions live in a module and the templates keep using data-hooks,
* nothing is rendered from an HTML string and nothing is an inline handler,
* the loader page carries no inline CSS or JS and keeps the token injection,
* every stylesheet that ships avoids colour literals outside the token block.

Environment must be set BEFORE importing main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from main import create_app  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DASHBOARD_DIR.parent
STATIC = DASHBOARD_DIR / "static"
TEMPLATES = DASHBOARD_DIR / "templates"
MODULES = STATIC / "js"
KOYURA_STATIC = REPO_ROOT / "koyura" / "static"
KOYURA_TEMPLATES = REPO_ROOT / "koyura" / "templates"

app = create_app()

MODULE_EXPORTS = {
    "format.js": ["export function count", "export function percent"],
    "toast.js": ["export function showToast"],
    "kpi.js": ["export function renderKpis"],
    "charts.js": ["export function createCharts"],
    "active.js": ["export function renderActiveDownloads"],
    "admin.js": [
        "export async function deleteSelected",
        "export function openPurgeModal",
        "export function closePurgeModal",
        "export async function confirmPurge",
        "export function fillLimitsPreset",
        "export function initAdminActions",
        "export function initSelectAll",
    ],
    "report.js": ["export function initDashboardPage"],
}

COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(\s*\d|\bhsla?\(\s*\d")


def _csrf_from(html: str) -> str:
    """Pull the CSRF field value out of the rendered form."""
    found = re.search(r'name="csrf_token" value="([^"]*)"', html)
    assert found is not None, "no csrf_token field in the rendered page"
    return found.group(1)


def _login() -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    page = client.get("/login")
    token = _csrf_from(page.text)
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "Koyura-Test-Passw0rd", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


def _without_token_blocks(css: str) -> str:
    return re.sub(r":root[^{]*\{[^{}]*\}", "", css)


# ---------------------------------------------------------------------------
# The dashboard entry point
# ---------------------------------------------------------------------------

def test_pages_load_the_entry_as_a_module():
    """One module script, so the imports resolve without a bundler."""
    html = (_login().get("/")).text
    assert 'type="module" src="/static/dashboard.js"' in html
    # shell.js stays a classic script: it runs before the module graph.
    assert 'src="/static/shell.js"' in html


#: The admin page is four pages; the action wiring has to cover all of them.
ADMIN_PAGES = ("/admin/cookies", "/admin/limits", "/admin/bot", "/admin/system")


def test_the_entry_wires_every_action_the_templates_use():
    entry = (STATIC / "dashboard.js").read_text()
    actions = set()
    for path in ADMIN_PAGES:
        actions |= set(re.findall(r'data-action="([^"]+)"', (_login().get(path)).text))
    assert actions, "the admin pages have no data-action hooks"
    for action in actions:
        assert f"'{action}'" in entry, f"{action} is not wired in dashboard.js"
    assert "initAdminActions" in entry


def test_every_module_exists_and_exports_its_entry_point():
    for name, exports in MODULE_EXPORTS.items():
        source = (MODULES / name).read_text()
        for needle in exports:
            assert needle in source, f"{name} does not export {needle!r}"


def test_modules_are_small_enough_to_read():
    """The split exists so no file is a 700-line scroll; keep each one small."""
    for path in MODULES.glob("*.js"):
        lines = len(path.read_text().splitlines())
        assert lines < 250, f"{path.name} is {lines} lines"


def test_javascript_never_builds_markup_from_strings():
    """Every card, row and toast is built from nodes and textContent."""
    for path in list(STATIC.glob("*.js")) + list(MODULES.glob("*.js")):
        source = path.read_text()
        assert "innerHTML" not in source, f"{path.name} sets innerHTML"
        assert "outerHTML" not in source, f"{path.name} sets outerHTML"
        assert "insertAdjacentHTML" not in source, f"{path.name} inserts HTML"
        assert "document.write" not in source, f"{path.name} writes markup"


def test_templates_carry_no_inline_handlers_or_event_attributes():
    for template in list(TEMPLATES.glob("*.html")) + list(KOYURA_TEMPLATES.glob("*.html")):
        text = template.read_text()
        for attribute in ("onclick=", "onchange=", "onsubmit=", "onload=", "onerror="):
            assert attribute not in text, f"{template.name} still uses {attribute}"


def test_charts_are_rebuilt_on_a_theme_change():
    """Repainting a live Chart.js instance keeps animated colours on the canvas,
    so charts.js destroys and rebuilds instead (see the module docstring)."""
    source = (MODULES / "charts.js").read_text()
    assert "destroy()" in source
    assert "syncTheme" in source
    report = (MODULES / "report.js").read_text()
    assert "koyura:theme" in report


# ---------------------------------------------------------------------------
# Stylesheets: one palette, no literals, no dead aliases
# ---------------------------------------------------------------------------

def test_every_shipped_stylesheet_avoids_colour_literals():
    for path in (
        STATIC / "style.css",
        KOYURA_STATIC / "loader.css",
    ):
        css = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
        found = COLOUR_LITERAL.findall(_without_token_blocks(css))
        assert not found, f"{path.name} hard-codes colours: {found[:5]}"


def test_style_css_keeps_only_the_shell_measurements_in_root():
    """Colours come from koyura.css; style.css may only size the shell."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(), flags=re.S)
    for legacy in ("--bg-primary", "--bg-card", "--text-secondary", "--accent-hover"):
        assert legacy not in css, f"{legacy} is a leftover alias"


def test_toast_leaving_is_a_class_not_an_inline_style():
    css = (STATIC / "style.css").read_text()
    assert ".toast.is-leaving" in css
    toast = (MODULES / "toast.js").read_text()
    assert "is-leaving" in toast
    assert ".style.opacity" not in toast


# ---------------------------------------------------------------------------
# Loader page (engine)
# ---------------------------------------------------------------------------

def test_loader_page_keeps_its_token_injection_and_no_inline_code():
    html = (KOYURA_TEMPLATES / "index.html").read_text()
    assert "{{ api_token|tojson }}" in html, "the token injection must survive"
    # The token is data, not code; the behaviour lives in the static module.
    assert '<script id="api-token" type="application/json">' in html
    assert '<script type="module" src="/static/loader.js">' in html
    assert "<style>" not in html, "page CSS belongs in static/loader.css"
    assert "<script>" not in html, "page JS belongs in static/loader.js"


def test_loader_script_keeps_the_queue_and_download_contract():
    source = (KOYURA_STATIC / "loader.js").read_text()
    for needle in (
        "X-Internal-Token",
        "/api/info",
        "/api/download",
        "/api/queue",
        "/api/status/",
        "/api/file/",
        "In queue",
        "overloaded",
        "slots ",
        "queue ",
    ):
        assert needle in source, f"loader.js lost {needle!r}"


def test_loader_page_keeps_the_ids_and_classes_it_renders():
    html = (KOYURA_TEMPLATES / "index.html").read_text()
    for element_id in ("urls", "goBtn", "cards", "limits-line", "theme-toggle", "api-token"):
        assert f'id="{element_id}"' in html, f"#{element_id} disappeared"
    source = (KOYURA_STATIC / "loader.js").read_text()
    for class_name in (
        "card-thumb",
        "card-body",
        "card-actions",
        "card-status",
        "q-chip",
        "skeleton-line",
        "spin",
        "card-error-msg",
        "dl-all-bar",
    ):
        assert class_name in source, f"loader.js no longer renders .{class_name}"


def test_loader_css_is_loaded_and_linked_once():
    html = (KOYURA_TEMPLATES / "index.html").read_text()
    assert html.count('/static/loader.css') == 1
    assert html.index('/static/koyura.css') < html.index('/static/loader.css')
