"""Presentation-layer guards for the Washi / Sumi / Shu redesign.

These tests lock in the parts of the visual redesign that are objectively
checkable: a single token source with no hard-coded colours, the strict CSP
that self-hosted fonts allow, the ids and data-hooks the JavaScript needs,
and the form contract (CSRF, actions, filters) that must never drift.

Environment must be set BEFORE importing auth/main/db, like the other
dashboard test modules.
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
# setdefault, not assignment: pytest imports every test module before running
# any of them, and `_db_path()` re-reads DB_PATH on each call. Assigning here
# would silently redirect the other dashboard modules to this tempdir.
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from main import create_app  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DASHBOARD_DIR.parent
STATIC = DASHBOARD_DIR / "static"
TEMPLATES = DASHBOARD_DIR / "templates"
KOYURA_STATIC = REPO_ROOT / "koyura" / "static"
KOYURA_TEMPLATES = REPO_ROOT / "koyura" / "templates"

app = create_app()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _first_group(pattern: str, text: str) -> str:
    """First capture group of `pattern`, or a clear assertion failure."""
    match = re.search(pattern, text)
    assert match is not None, f"pattern not found: {pattern}"
    return match.group(1)


def _csrf_from(html: str) -> str:
    """Pull the CSRF field value out of a rendered form."""
    match = re.search(r'name="csrf_token" value="([^"]*)"', html)
    assert match is not None, "no csrf_token field in the rendered page"
    return match.group(1)


def _login(c: TestClient) -> TestClient:
    page = c.get("/login")
    assert page.status_code == 200, page.text
    token = _csrf_from(page.text)
    resp = c.post(
        "/login",
        data={
            "username": "admin",
            "password": "Koyura-Test-Passw0rd",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return c


def _page(path: str) -> str:
    c = _login(_client())
    resp = c.get(path)
    assert resp.status_code == 200, resp.text
    return resp.text


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _declaration_blocks(css: str) -> list[str]:
    """Return declaration bodies only: everything between { and }."""
    return re.findall(r"\{([^{}]*)\}", css)


# ---------------------------------------------------------------------------
# Tokens: one palette, no hard-coded colours outside the token block
# ---------------------------------------------------------------------------

# Note: no re.X here — in verbose mode '#' starts a comment and the hex
# alternative would silently match the empty string.
COLOUR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\brgba?\(\s*\d|\bhsla?\(\s*\d"
)


def _without_token_blocks(css: str) -> str:
    """Drop the :root blocks, where colour literals are the whole point."""
    return re.sub(r":root[^{]*\{[^{}]*\}", "", css)


def test_koyura_css_defines_the_required_tokens():
    css = (STATIC / "koyura.css").read_text()
    for token in ("--bg:", "--ink:", "--dim:", "--red:", "--line:"):
        assert token in css, f"missing token {token}"
    # Both explicit themes plus the system default must be present.
    assert ":root[data-theme='dark']" in css or ':root[data-theme="dark"]' in css
    assert ":root[data-theme='light']" in css or ':root[data-theme="light"]' in css
    assert "prefers-color-scheme:dark" in css.replace(" ", "")


@pytest.mark.parametrize("path", ["koyura.css", "style.css"])
def test_dashboard_css_has_no_colour_literals_outside_tokens(path: str):
    css = _strip_comments((STATIC / path).read_text())
    outside = _without_token_blocks(css)
    found = COLOUR_LITERAL.findall(outside)
    assert not found, f"{path} hard-codes colours outside the token block: {found[:5]}"


def test_koyura_css_has_no_colour_literals_outside_tokens():
    css = _strip_comments((KOYURA_STATIC / "koyura.css").read_text())
    outside = _without_token_blocks(css)
    found = COLOUR_LITERAL.findall(outside)
    assert not found, f"engine koyura.css hard-codes colours: {found[:5]}"


@pytest.mark.parametrize(
    "path", ["dashboard/static/koyura.css", "dashboard/static/style.css", "koyura/static/koyura.css"]
)
def test_border_radius_is_zero_everywhere_but_the_sun(path: str):
    css = _strip_comments((REPO_ROOT / path).read_text())
    for block in _declaration_blocks(css):
        for decl in block.split(";"):
            if "border-radius" not in decl:
                continue
            value = decl.split(":", 1)[1].strip()
            assert value in {"0", "0px", "50%", "50% !important"}, (
                f"{path} has a rounded value: {decl.strip()}"
            )


def test_koyura_koyura_css_is_a_copy_of_the_dashboard_tokens():
    """The two containers cannot share a volume, so the tokens are duplicated.

    Guard that the palette itself stays identical — a drift here would make
    the loader and the dashboard look like different products.
    """
    def tokens(text: str) -> dict[str, str]:
        out = {}
        for block in re.finditer(r":root[^{]*\{([^{}]*)\}", text):
            for decl in block.group(1).split(";"):
                if "--" in decl and ":" in decl:
                    name, value = decl.split(":", 1)
                    out.setdefault(name.strip(), value.strip())
        return out

    a = tokens(_strip_comments((STATIC / "koyura.css").read_text()))
    b = tokens(_strip_comments((KOYURA_STATIC / "koyura.css").read_text()))
    for name in ("--bg", "--ink", "--dim", "--red", "--line", "--on-red", "--red-ink"):
        assert a.get(name) == b.get(name), f"{name} drifted between the two copies"


# ---------------------------------------------------------------------------
# Favicon: referenced by every page, so it has to actually exist
# ---------------------------------------------------------------------------

def test_favicon_exists_and_is_served():
    """Every page links /static/favicon.svg; a missing file means a 404."""
    path = STATIC / "favicon.svg"
    assert path.is_file(), "dashboard/static/favicon.svg is missing"
    # Small enough to be a favicon, and declared as SVG.
    assert path.stat().st_size < 2048, f"favicon is {path.stat().st_size} bytes"
    assert b"<svg" in path.read_bytes()

    c = _client()
    resp = c.get("/static/favicon.svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers.get("content-type", "")
    assert resp.content.startswith(b"<svg")


@pytest.mark.parametrize(
    "page",
    ["/login", "/", "/history", "/errors", "/admin",
     "/admin/cookies", "/admin/limits", "/admin/bot", "/admin/system"],
)
def test_every_page_links_a_working_favicon(page: str):
    html = _client().get("/login").text if page == "/login" else _page(page)
    match = re.search(r'<link rel="icon" href="([^"]+)"', html)
    assert match is not None, f"{page} has no favicon link"
    href = match.group(1)
    assert href == "/static/favicon.svg"
    assert _client().get(href).status_code == 200, f"{page}: {href} does not resolve"


def test_favicon_follows_the_design_language():
    """Square 32-grid mark: sumi ring, one vermillion sun, no raster or text."""
    svg = (STATIC / "favicon.svg").read_text()
    assert 'viewBox="0 0 32 32"' in svg
    assert "#17140f" in svg, "sumi ink missing"
    assert "#d4361c" in svg, "vermillion accent missing"
    banned_markers = ("<image", "base64", "<text", "data:image", 'href="data:')
    for banned in banned_markers:
        assert banned not in svg, f"favicon must not contain {banned}"
    assert svg.count("<circle") == 1, "exactly one sun disc"


def test_koyura_favicon_matches_the_dashboard_mark():
    """The loader ships its own copy; it must not drift from the dashboard."""
    assert (KOYURA_STATIC / "favicon.svg").is_file()
    assert (KOYURA_STATIC / "favicon.svg").read_bytes() == (STATIC / "favicon.svg").read_bytes()


# ---------------------------------------------------------------------------
# Accessibility: contrast stays AA for text in both themes
# ---------------------------------------------------------------------------

def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _theme_tokens(text: str, selector_prefix: str) -> dict[str, str]:
    """Parse the token block for one theme (system-light, dark override...)."""
    out: dict[str, str] = {}
    for block in re.finditer(re.escape(selector_prefix) + r"[^{]*\{([^{}]*)\}", text):
        for decl in block.group(1).split(";"):
            if decl.strip().startswith("--") and ":" in decl:
                name, value = decl.split(":", 1)
                out[name.strip()] = value.strip()
    return out


@pytest.mark.parametrize(
    "theme,pairs",
    [
        ("light", [("--ink", "--bg"), ("--dim", "--bg"), ("--red-ink", "--bg")]),
        ("dark", [("--ink", "--bg"), ("--dim", "--bg"), ("--red", "--bg")]),
    ],
)
def test_theme_text_pairs_meet_wcag_aa(theme: str, pairs):
    css = _strip_comments((STATIC / "koyura.css").read_text())
    tokens = _theme_tokens(css, ":root")
    if theme == "dark":
        tokens.update(_theme_tokens(css, ":root[data-theme='dark']"))
    for fg, bg in pairs:
        ratio = _contrast(tokens[fg], tokens[bg])
        assert ratio >= 4.5, f"{theme}: {fg} on {bg} is {ratio:.2f}:1, below AA"


def test_filled_accent_cell_uses_a_contrasting_ink():
    css = _strip_comments((STATIC / "koyura.css").read_text())
    light = _theme_tokens(css, ":root")
    dark = dict(light)
    dark.update(_theme_tokens(css, ":root[data-theme='dark']"))
    assert _contrast(light["--on-red"], light["--red"]) >= 4.5
    assert _contrast(dark["--on-red"], dark["--red"]) >= 4.5


# ---------------------------------------------------------------------------
# CSP: fonts self-hosted, no font CDN, Chart.js unchanged
# ---------------------------------------------------------------------------

def test_csp_no_longer_allowlists_google_fonts():
    c = _client()
    csp = c.get("/login").headers["content-security-policy"]
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp
    assert "font-src 'self'" in csp
    # Chart.js delivery is unchanged.
    assert "https://cdn.jsdelivr.net" in csp
    assert "frame-ancestors 'none'" in csp


def test_fonts_are_self_hosted_and_present():
    fonts = STATIC / "fonts"
    for name in (
        "inter-latin-var.woff2",
        "jetbrains-mono-latin-var.woff2",
        "shippori-mincho-700.woff2",
        "shippori-mincho-800.woff2",
    ):
        path = fonts / name
        assert path.is_file(), f"missing font {name}"
        assert path.stat().st_size > 500, f"{name} looks empty"

    # The loader ships its own copy because it cannot share a volume.
    for name in (
        "inter-latin-var.woff2",
        "jetbrains-mono-latin-var.woff2",
        "shippori-mincho-700.woff2",
    ):
        assert (KOYURA_STATIC / "fonts" / name).is_file()

    css = (STATIC / "koyura.css").read_text()
    assert "font-display: swap" in css
    assert "/static/fonts/" in css


def test_font_files_are_served():
    c = _client()
    resp = c.get("/static/fonts/shippori-mincho-700.woff2")
    assert resp.status_code == 200
    assert resp.content[:4] == b"wOF2"


# ---------------------------------------------------------------------------
# Templates: ids, hooks and the form contract survive the restyle
# ---------------------------------------------------------------------------

DASHBOARD_IDS = [
    "hamburger",
    "sidebar",
    "sidebar-overlay",
    "kpi-downloads",
    "kpi-downloads-trend",
    "kpi-users",
    "kpi-users-trend",
    "kpi-disk",
    "disk-bar",
    "kpi-errors",
    "kpi-errors-trend",
    "downloads-chart",
    "platform-chart",
    "top-users",
    "active-downloads",
    "active-downloads-count",
    "active-downloads-body",
    "toast-container",
    "theme-toggle",
]


@pytest.mark.parametrize("element_id", DASHBOARD_IDS)
def test_dashboard_keeps_the_ids_its_javascript_uses(element_id: str):
    html = _page("/")
    assert f'id="{element_id}"' in html, f"#{element_id} disappeared from the dashboard"


#: The four admin sub-pages, in sidebar order. Every one of them is a page of
#: its own now, so the tests below name the page that owns the markup.
ADMIN_PAGES = ("/admin/cookies", "/admin/limits", "/admin/bot", "/admin/system")


def test_admin_uses_data_action_hooks_and_no_inline_handlers():
    html = _page("/admin/system")
    assert "onclick=" not in html, "inline handlers are blocked by the CSP"
    for action in ("delete-selected", "purge-open", "purge-close", "purge-confirm"):
        assert f'data-action="{action}"' in html


def test_no_admin_page_carries_an_inline_handler():
    for path in ADMIN_PAGES:
        html = _page(path)
        assert "onclick=" not in html, f"{path} has an inline handler"
        assert "<script>" not in html, f"{path} has an inline script"


def test_dashboard_js_wires_every_admin_data_action():
    js = (STATIC / "dashboard.js").read_text()
    for action in ("delete-selected", "purge-open", "purge-close", "purge-confirm",
                   "limit-preset"):
        assert f"'{action}'" in js
    assert "initAdminActions" in js


def test_admin_page_has_the_download_limits_contract():
    """The limits section is a plain form: no inline JS, server-side validation,
    a preset button per server size and the detected hardware underneath."""
    html = _page("/admin/limits")
    for field in ("max_concurrent", "per_user_active", "per_user_queued",
                  "max_queue_length", "max_wait_seconds", "estimate_mbps"):
        assert f'name="{field}"' in html, field
    assert 'action="/admin/limits"' in html
    assert 'method="post"' in html
    assert html.count('data-action="limit-preset"') == 4
    assert 'id="hardware-line"' in html
    assert "recommended" in html


def test_admin_page_keeps_its_destructive_confirmations():
    html = _page("/admin/system")
    assert 'id="purge-modal"' in html
    assert 'id="purge-confirm"' in html
    assert 'id="select-all"' in html
    assert 'id="file-table"' in html


def test_login_keeps_form_contract_and_csrf():
    html = _client().get("/login").text
    assert 'method="post" action="/login"' in html
    assert 'name="csrf_token"' in html
    assert 'name="username"' in html
    assert 'name="password"' in html
    # The ensō is decorative and announced as such.
    assert 'id="enso"' in html
    assert 'aria-hidden="true"' in html


def test_login_error_message_stays_visible():
    c = _client()
    page = c.get("/login")
    token = _csrf_from(page.text)
    resp = c.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "Invalid credentials" in resp.text


def test_history_filters_and_pagination_contracts():
    html = _page("/history")
    for field in ("user_filter", "platform", "status", "date_from", "date_to"):
        assert f'name="{field}"' in html
    assert 'action="/history"' in html


def test_every_page_has_theme_toggle_and_footer():
    for path in ("/", "/history", "/errors", *ADMIN_PAGES):
        html = _page(path)
        assert 'id="theme-toggle"' in html, path
        assert "©2026 Koyura" in html, path
        assert "[ self-hosted · MIT ]" in html, path


def test_pages_load_the_shared_stylesheet():
    for path in ("/", "/history", "/errors", *ADMIN_PAGES, "/login"):
        html = _page(path) if path != "/login" else _client().get("/login").text
        assert "/static/koyura.css" in html, path


def test_templates_carry_no_inline_script_handlers():
    for template in TEMPLATES.glob("*.html"):
        text = template.read_text()
        assert "onclick=" not in text, f"{template.name} still uses inline onclick"
        assert "<script>" not in text, f"{template.name} has an inline script block"


# ---------------------------------------------------------------------------
# Motion: reduced-motion is respected by the animated pieces
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Login layout: the page must fill the viewport so flex centring can work
# ---------------------------------------------------------------------------

def _media_blocks(css: str, query: str) -> list[str]:
    """Return the full text of every @media block whose query matches.

    Brace-matched rather than regex-sliced: the stylesheets contain several
    @media blocks with the same query, and a lazy `.*?` happily jumps from one
    into the next.
    """
    blocks = []
    for m in re.finditer(r"@media\s*[^{]+\{", css):
        if query not in m.group(0):
            continue
        depth, i = 1, m.end()
        while i < len(css) and depth:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        blocks.append(css[m.start():i])
    return blocks


def _rule_body(css: str, selector: str) -> str:
    """Return the declaration body of the first exact-selector rule."""
    match = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    assert match is not None, f"no rule for {selector}"
    return match.group(1)


def test_login_body_fills_the_viewport():
    """Without a height on body the card stuck to the top of the page.

    body was a flex container with align-items:center but no min-height, so
    there was nothing to centre inside and the card hugged the top edge.
    """
    css = _strip_comments((STATIC / "style.css").read_text())
    body = _rule_body(css, ".login-body")
    assert "min-height: 100vh" in body, ".login-body needs the 100vh fallback"
    assert "min-height: 100dvh" in body, ".login-body needs the dvh height"
    assert "justify-content: center" in body
    # `safe center` keeps a card taller than the viewport reachable.
    assert "safe center" in body


def test_login_body_is_top_aligned_on_phones():
    """On narrow screens the tall stacked form reads better from the top."""
    css = _strip_comments((STATIC / "style.css").read_text())
    overrides = [
        body
        for block in _media_blocks(css, "max-width: 767px")
        for body in re.findall(r"\.login-body\s*\{([^{}]*)\}", block)
    ]
    assert overrides, "no mobile .login-body override"
    assert any("align-items: flex-start" in body for body in overrides)


def test_login_hero_is_a_backdrop_not_a_panel():
    """The owner read the framed left half as a second window.

    The hero must be a full-viewport background layer: no border of its own,
    no card wrapping both layers, no pointer or focus capture, painted behind
    the form.
    """
    css = _strip_comments((STATIC / "style.css").read_text())
    hero = _rule_body(css, ".login-hero")
    assert "position: fixed" in hero, "the hero must be pinned to the viewport"
    assert "inset: 0" in hero or "inset:0" in hero
    assert "z-index: 0" in hero
    assert "pointer-events: none" in hero, "the backdrop must be inert"
    assert "border: 0" in hero, "the hero must not frame itself"
    # Phones collapse it to a band that scrolls with the page.
    mobile = [
        body
        for block in _media_blocks(css, "max-width: 767px")
        for body in re.findall(r"\.login-hero\s*\{([^{}]*)\}", block)
    ]
    assert any("position: absolute" in b and "height: 200px" in b for b in mobile), (
        "on phones the hero must become a fixed-height band, not a full-screen layer"
    )


def test_login_panel_is_the_single_object():
    css = _strip_comments((STATIC / "style.css").read_text())
    panel = _rule_body(css, ".login-panel")
    assert "position: relative" in panel
    assert "z-index: 1" in panel, "the form must sit above the backdrop"
    assert "border: 1px solid var(--line)" in panel, "hairline frame only"
    assert "box-shadow" not in panel
    width = re.search(r"max-width:\s*(\d+)px", panel)
    assert width and 420 <= int(width.group(1)) <= 460, "panel width out of the 420-460 range"
    # Nothing may put a frame around both the backdrop and the form.
    shell = _rule_body(css, ".login-shell")
    assert "border" not in shell, ".login-shell must not draw a card border"
    assert "background" not in shell


def test_login_hero_is_decorative_and_clipped_to_the_viewport():
    html = _client().get("/login").text
    # The backdrop is announced as decorative and takes no focus stop.
    hero = re.search(r'<section class="hero login-hero"([^>]*)>', html)
    assert hero is not None
    assert 'aria-hidden="true"' in hero.group(1)
    assert 'id="enso"' in html
    enso_tag = re.search(r'<pre[^>]*id="enso"[^>]*>', html)
    assert enso_tag is not None
    assert 'aria-hidden="true"' in enso_tag.group(0)
    # The form is outside the backdrop, so tab order reaches it directly.
    assert html.index('class="hero login-hero"') < html.index('class="login-shell"')

    css = _strip_comments((STATIC / "style.css").read_text())
    hero_rule = _rule_body(css, ".login-hero")
    assert "overflow: hidden" in hero_rule, "the backdrop must be clipped to the viewport"


def test_login_decorative_layers_are_dimmed():
    """Backdrop type must not compete with the form."""
    css = _strip_comments((STATIC / "style.css").read_text())
    for selector in (".login-hero .enso", ".login-hero .hero-mark", ".login-hero .hero-kana"):
        body = _rule_body(css, selector)
        assert "opacity:" in body, f"{selector} needs a backdrop opacity"
        value = float(_first_group(r"opacity:\s*([\d.]+)", body))
        assert value <= 0.8, f"{selector} is too loud for a backdrop ({value})"


def test_reduced_motion_disables_marquee_and_enso_animation():
    css = _strip_comments((STATIC / "koyura.css").read_text())
    reduced = css.split("prefers-reduced-motion", 1)
    assert len(reduced) > 1, "no reduced-motion block"
    block = reduced[1]
    assert "animation: none" in block
    assert ".marq > div" in block

    enso = (STATIC / "enso.js").read_text()
    assert "prefers-reduced-motion: reduce" in enso
    assert "document.hidden" in enso


def test_vanilla_scripts_stay_under_the_budget():
    """Brief: the added JavaScript stays under 5 KB in total.

    shell.js is not counted: it only relocates the hamburger handler that used
    to live inline in base.html, so it is not new behaviour.
    """
    total = sum((STATIC / name).stat().st_size for name in ("theme.js", "enso.js"))
    assert total < 5 * 1024, f"added JS is {total} bytes"
