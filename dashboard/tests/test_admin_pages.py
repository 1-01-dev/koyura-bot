"""Admin split into sub-pages: the route map and the sidebar group.

The page that used to be one long ``/admin`` is four pages now. These tests pin
the two things the split can break and the other suites cannot see:

* the route map — every section renders, ``/admin`` lands on one of them, and
  the query string of an old link follows it to the section that owns it
  (``?saved=1`` → limits, ``?kind=…&lang=…`` → bot);
* the sidebar — Admin is a disclosure group with four children, the current
  child is marked and its parent stays marked, and everything around it
  (Dashboard, History, Errors) is still a plain row.

Environment must be set BEFORE importing auth/main/db, like the other dashboard
test modules.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "admin_pages.db")
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
client = TestClient(app, raise_server_exceptions=True)

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = DASHBOARD_DIR / "templates"
STATIC = DASHBOARD_DIR / "static"

#: Route → the heading the page must carry. Ordered like the sidebar.
SECTIONS = {
    "/admin/cookies": ("Cookies", "cookies"),
    "/admin/limits": ("Limits", "limits"),
    "/admin/bot": ("Bot settings", "bot"),
    "/admin/system": ("System", "system"),
}

#: Where a bare /admin lands, and why: the read-only page.
ADMIN_HOME = "/admin/system"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csrf(c: TestClient | None = None) -> str:
    return (c or client).cookies.get("koyura_csrf") or ""


def _login(c: TestClient | None = None) -> TestClient:
    target = c or client
    page = target.get("/login")
    assert page.status_code == 200, page.text
    resp = target.post(
        "/login",
        data={
            "username": "admin",
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": _csrf(target),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return target


@pytest.fixture(scope="module", autouse=True)
def _schema():
    import asyncio

    asyncio.run(db.init_db())


@pytest.fixture()
def admin() -> TestClient:
    return _login(TestClient(app))


# ---------------------------------------------------------------------------
# The route map
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", list(SECTIONS))
def test_every_section_renders_its_own_page(admin: TestClient, path: str):
    heading, _key = SECTIONS[path]
    response = admin.get(path)
    assert response.status_code == 200, path
    assert f'<h1 class="page-title">{heading}</h1>' in response.text, path


def test_the_pages_no_longer_share_one_body(admin: TestClient):
    """Each section carries only its own panels — that is the point of the split."""
    bodies = {path: admin.get(path).text for path in SECTIONS}
    assert 'id="file-table"' in bodies["/admin/system"]
    assert 'id="file-table"' not in bodies["/admin/limits"]
    assert 'id="limits-card"' in bodies["/admin/limits"]
    assert 'id="limits-card"' not in bodies["/admin/bot"]
    assert 'id="languages-card"' in bodies["/admin/bot"]
    assert 'id="languages-card"' not in bodies["/admin/system"]
    # Nothing ever renders two sections at once.
    for path, body in bodies.items():
        assert body.count('class="panel-stack"') == 1, path


def test_bare_admin_redirects_to_the_read_only_page(admin: TestClient):
    response = admin.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == ADMIN_HOME


def test_an_unknown_section_falls_back_to_the_home_page(admin: TestClient):
    response = admin.get("/admin/nope", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/system"


@pytest.mark.parametrize(
    "query",
    [
        "saved=1",
        "kind=card&lang=en",
        "lang=ru",
        "template=3&kind=card&lang=en",
        "languages_saved=1",
        "progress_saved=1",
        "templates_saved=1",
        "templates_notice=Template%20deleted.",
    ],
)
def test_an_old_admin_link_keeps_working(admin: TestClient, query: str):
    """The parameters pick the section; nothing is dropped on the way."""
    response = admin.get(f"/admin?{query}", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(("/admin/limits", "/admin/bot")), location
    first_name = query.split("=")[0]
    assert f"?{first_name}=" in location or f"&{first_name}=" in location, location

    followed = admin.get(f"/admin?{query}")
    assert followed.status_code == 200


def test_a_limits_link_lands_on_the_limits_page(admin: TestClient):
    response = admin.get("/admin?saved=1", follow_redirects=False)
    assert response.headers["location"] == "/admin/limits?saved=1"
    assert "Limits saved" in admin.get("/admin/limits?saved=1").text


def test_a_template_link_lands_on_the_bot_page(admin: TestClient):
    response = admin.get("/admin?kind=card&lang=ru", follow_redirects=False)
    assert response.headers["location"] == "/admin/bot?kind=card&lang=ru"
    body = admin.get("/admin/bot?kind=card&lang=ru").text
    assert 'data-lang="ru"' in body


def test_an_unknown_parameter_still_lands_on_the_home_page(admin: TestClient):
    response = admin.get("/admin?utm_source=bookmark", follow_redirects=False)
    assert response.headers["location"] == "/admin/system?utm_source=bookmark"


@pytest.mark.parametrize("path", ["/admin", *SECTIONS])
def test_anonymous_visits_go_to_the_login_form(path: str):
    response = TestClient(app).get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# The sidebar group
# ---------------------------------------------------------------------------

def _nav_group(html: str) -> str:
    """The Admin group's markup, so the assertions cannot match another row."""
    start = html.index('<div class="nav-group')
    return html[start : html.index("</div>", start)]


def test_admin_is_a_disclosure_group_not_a_plain_row(admin: TestClient):
    group = _nav_group(admin.get("/admin/system").text)
    assert "<button" in group, "the parent must be operable from the keyboard"
    assert 'aria-expanded="true"' in group
    assert 'aria-controls="nav-admin-children"' in group
    assert 'id="nav-admin-children"' in group
    # A real button: Enter and Space come from the browser, not from a script.
    assert "onclick=" not in group


def test_the_group_opens_when_a_child_is_current(admin: TestClient):
    for path in SECTIONS:
        html = admin.get(path).text
        group = _nav_group(html)
        assert 'aria-expanded="true"' in group, path
        assert 'class="nav-group is-active is-open"' in group, path
        child = re.search(
            rf'<a href="{path}"[^>]*class="nav-item nav-child([^"]*)"', group
        )
        assert child is not None, path
        assert "active" in child.group(1), f"{path} is not highlighted"
        assert 'aria-current="page"' in group.split(f'href="{path}"')[1].split(">")[0]


def test_only_the_current_child_is_highlighted(admin: TestClient):
    html = admin.get("/admin/limits").text
    group = _nav_group(html)
    highlighted = re.findall(r'class="nav-item nav-child active"', group)
    assert len(highlighted) == 1


def test_the_parent_stays_active_while_a_child_is_current(admin: TestClient):
    group = _nav_group(admin.get("/admin/bot").text)
    parent = group.split("</button>")[0]
    assert "nav-group-toggle active" in parent
    assert 'aria-current="true"' in parent


def test_the_group_carries_the_four_children(admin: TestClient):
    group = _nav_group(admin.get("/admin/cookies").text)
    links = re.findall(r'<a href="(/admin/[a-z]+)"', group)
    assert links == ["/admin/cookies", "/admin/limits", "/admin/bot", "/admin/system"]
    for label in ("Cookies", "Limits", "Bot settings", "System"):
        assert label in group


def test_the_group_is_closed_away_from_admin(admin: TestClient):
    group = _nav_group(admin.get("/history").text)
    assert 'class="nav-group"' in group
    assert 'aria-expanded="false"' in group
    assert "is-open" not in group
    assert 'aria-current="false"' in group


def test_the_other_nav_rows_are_untouched(admin: TestClient):
    html = admin.get("/admin/system").text
    for path, label in (("/", "Dashboard"), ("/history", "History"), ("/errors", "Errors")):
        row = re.search(rf'<a href="{path}" class="nav-item([^"]*)"', html)
        assert row is not None, path
        assert "nav-child" not in row.group(1), f"{label} must stay a plain row"
        assert "nav-group" not in row.group(1), label


def test_every_admin_page_carries_the_section_tabs(admin: TestClient):
    """The rail is icon-only on a tablet, so the page keeps the four links."""
    for path in SECTIONS:
        body = admin.get(path).text
        assert 'class="admin-tabs"' in body, path
        current = re.findall(
            r'<a class="admin-tab active"\s+href="([^"]+)"', body
        )
        assert current == [path], f"{path} marks {current} as current"


# ---------------------------------------------------------------------------
# The pages themselves
# ---------------------------------------------------------------------------

def test_cookies_offers_the_search_plus_and_upload_form(admin: TestClient):
    """The add row: a service field with suggestions, a file field and "+"."""
    body = admin.get("/admin/cookies").text
    assert 'id="cookies-card"' in body
    assert 'id="cookie-add-form"' in body
    # The service field is a native search box backed by a datalist, so the
    # dropdown and the filtering work with JavaScript off.
    assert 'name="service"' in body
    assert 'list="service-suggestions"' in body
    assert 'id="service-suggestions"' in body
    assert "<datalist" in body
    # An upload is only an upload with the multipart encoding.
    assert 'enctype="multipart/form-data"' in body
    assert 'type="file"' in body
    assert 'name="cookie_file"' in body
    assert 'data-action="' in body or 'aria-label="Add this cookie file' in body


def test_cookies_starts_with_an_empty_state(admin: TestClient):
    body = admin.get("/admin/cookies").text
    assert 'id="cookies-empty"' in body
    assert "Nothing stored yet" in body


def test_the_old_storage_path_redirects_permanently(admin: TestClient):
    """A bookmark and a chat message still name /admin/storage."""
    response = admin.get("/admin/storage", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/admin/cookies"


def test_the_old_storage_path_keeps_a_query_string(admin: TestClient):
    response = admin.get("/admin/storage?added=1", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/admin/cookies?added=1"


def test_system_owns_the_files_and_the_purge_dialog(admin: TestClient):
    body = admin.get("/admin/system").text
    for element in ("file-table", "select-all", "purge-modal", "purge-confirm"):
        assert f'id="{element}"' in body, element
    assert 'data-action="purge-open"' in body
    assert "System info" in body
    # The files block says what it is about; "Disk management" read like a
    # second storage page next to the Cookies one.
    assert "Disk &amp; files" in body or "Disk & files" in body
    assert "Disk management" not in body


def test_limits_owns_the_form_and_the_presets(admin: TestClient):
    body = admin.get("/admin/limits").text
    assert 'id="limits-form"' in body
    assert 'action="/admin/limits"' in body
    assert 'id="hardware-line"' in body
    assert body.count('data-action="limit-preset"') == 4


def test_bot_owns_the_two_panels_and_their_anchors(admin: TestClient):
    """Languages decide *which* languages exist; the templates say what they say.

    The third panel (``Progress fields``) is retired: the progress line is the
    ``progress`` body in the templates section, so there is nothing left to
    anchor to and no second source of truth for it.
    """
    body = admin.get("/admin/bot").text
    for panel in ("languages-card", "templates-card"):
        assert f'id="{panel}"' in body, panel
        assert f'href="#{panel}"' in body, f"{panel} has no anchor link"
    assert 'id="progress-card"' not in body
    assert 'action="/admin/languages"' in body
    assert 'action="/admin/templates/save"' in body


# ---------------------------------------------------------------------------
# A path is not a stylesheet: the collapse state lives in class + aria only
# ---------------------------------------------------------------------------

def test_the_shell_remembers_the_group_and_mirrors_it_in_aria():
    shell = (STATIC / "shell.js").read_text()
    assert "data-nav-group" in shell
    assert "aria-expanded" in shell
    assert "sessionStorage" in shell
    assert "onclick" not in shell


def test_the_group_needs_no_media_query_to_open():
    """Open/closed is a class, not a second breakpoint: the drawer and the wide
    sidebar share one rule, so a child is never hidden behind a width."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(), flags=re.S)
    assert ".nav-group.is-open .nav-children {\n  display: block;\n}" in css


def test_sub_page_rows_are_thumb_sized():
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(), flags=re.S)
    child = css.split("\n.nav-child {")[1].split("}")[0]
    assert "min-height: 44px" in child
    assert "padding-left: 41px" in child
