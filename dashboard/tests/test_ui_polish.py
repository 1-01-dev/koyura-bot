"""Guards for the UI-polish wave: the link dialog, the hidden bulk bars, the
redraw on a format switch, the mobile drawer trigger and the panel stack.

Everything here is checkable without a browser:

* the markup the pages render (``role="dialog"``, ``data-*`` hooks, no inline
  handlers, no literal colours outside the token block),
* the source of the modules that have to wire those hooks,
* the Node-testable size filter the loader uses for its quality chips.

The rendered pages come from the same app the other design tests use, so the
environment block is identical.
"""
from __future__ import annotations

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
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from main import create_app  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DASHBOARD_DIR.parent
STATIC = DASHBOARD_DIR / "static"
MODULES = STATIC / "js"
TEMPLATES = DASHBOARD_DIR / "templates"
KOYURA_STATIC = REPO_ROOT / "koyura" / "static"
KOYURA_TEMPLATES = REPO_ROOT / "koyura" / "templates"

app = create_app()

COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(\s*\d|\bhsla?\(\s*\d")


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]*)"', html)
    assert match is not None, "no csrf_token field in the rendered page"
    return match.group(1)


def _client() -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    token = _csrf_from(client.get("/login").text)
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "Koyura-Test-Passw0rd", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


def _page(path: str) -> str:
    return _client().get(path).text


def _seed_history_row() -> None:
    """One row with a long URL, so the tables have a cell to render.

    The shared test database starts empty and other modules truncate it, so
    the row is inserted right before the page is fetched and keyed by a unique
    job id: re-insertion is a no-op.
    """
    import asyncio

    import db as dashboard_db

    asyncio.run(dashboard_db.init_db())
    asyncio.run(
        dashboard_db.insert_download_start(
            job_id="ui-polish-long-url",
            user_id=1,
            username="tester",
            chat_id=1,
            url=(
                "https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=PLQnHfyKFzHRk0Xw"
                "JjSaMlhQ&index=12&t=1184s&si=EnSIkaIECMiOmarE&pp=iAQB8AUB"
            ),
            platform="youtube",
            title="Big Buck Bunny",
            format="video",
            quality="1080p",
        )
    )
    asyncio.run(
        dashboard_db.update_download_error(
            job_id="ui-polish-long-url", error_message="Sign in to confirm you are not a bot"
        )
    )


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _without_token_blocks(css: str) -> str:
    """Drop the `:root` palette, the one place literals are allowed."""
    return re.sub(r":root[^{]*\{[^{}]*\}", "", css)


# ---------------------------------------------------------------------------
# 1. The link dialog
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ["/history", "/errors", "/",
     "/admin/cookies", "/admin/channels", "/admin/limits", "/admin/bot",
     "/admin/system"],
)
def test_every_page_carries_the_link_dialog(path: str):
    """One dialog in base.html: the tables share it, so it must exist
    everywhere the base template renders, not only on the two tables."""
    html = _page(path)
    assert 'id="link-modal"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="link-modal-title"' in html
    assert 'id="link-modal-title"' in html


def test_the_link_dialog_has_its_hooks_and_starts_hidden():
    html = _page("/history")
    for hook in ("data-link-text", "data-link-status", "data-link-copy", "data-link-close"):
        assert hook in html, hook
    # `hidden` is the contract: a dialog that starts visible would cover the
    # table on load, and the stylesheet relies on the attribute.
    dialog = re.search(r'<div class="modal-overlay" id="link-modal".*?>', html, re.S)
    assert dialog is not None
    assert "hidden" in dialog.group(0)


def test_urls_render_as_a_link_button_not_as_the_url():
    """The URL itself moves into a data attribute: a long query string must
    not be able to decide a column's width."""
    _seed_history_row()
    for path in ("/history", "/errors"):
        html = _client().get(path).text
        assert "data-link-url=" in html
        assert 'class="url-cell"' not in html, f"{path} still renders the URL cell"
        assert 'aria-haspopup="dialog"' in html
        assert 'aria-controls="link-modal"' in html


def test_the_link_module_is_wired_from_the_entry():
    entry = (STATIC / "dashboard.js").read_text()
    assert "initLinkModal" in entry
    assert "./js/linkmodal.js" in entry

    module = (MODULES / "linkmodal.js").read_text()
    # Closing paths the brief names: button, scrim, Escape.
    assert "data-link-close" in module or "data-link-copy" in module
    assert "'Escape'" in module
    assert "addEventListener('click'" in module
    # Focus returns to the button that opened the dialog.
    assert "focus()" in module
    # The clipboard needs a fallback for a non-secure context.
    assert "navigator.clipboard" in module
    assert "execCommand" in module


def test_the_link_module_never_builds_markup_from_strings():
    module = (MODULES / "linkmodal.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in module


# ---------------------------------------------------------------------------
# 2. Bulk actions disappear with their content
# ---------------------------------------------------------------------------

def test_active_downloads_card_is_hidden_by_default():
    html = _page("/")
    card = re.search(r'<div class="card active-card" id="active-downloads"[^>]*>', html)
    assert card is not None, "the active-downloads card changed shape"
    assert "hidden" in card.group(0)


def test_active_module_clears_and_hides_the_card():
    module = (MODULES / "active.js").read_text()
    # Hiding is the requirement; clearing the rows is what keeps a stale row
    # from flashing when the next batch starts.
    assert "section.hidden" in module
    assert "replaceChildren" in module


def test_the_loader_bulk_bar_is_a_function_of_the_cards():
    source = (KOYURA_STATIC / "loader.js").read_text()
    assert "function renderDownloadAll()" in source
    assert "existing.remove()" in source
    # Called after every card repaint, so a finished card takes the bar away.
    assert "renderDownloadAll();" in source.split("function renderDownloadAll()")[1]
    assert "ready < 2" in source


# ---------------------------------------------------------------------------
# 3. A format switch redraws the cards
# ---------------------------------------------------------------------------

def test_set_format_repaints_every_card():
    source = (KOYURA_STATIC / "loader.js").read_text()
    set_format = source.split("function setFormat(")[1].split("\nasync function")[0]
    assert "state.format = pill.dataset.format" in set_format
    # Without the redraw a finished card kept the previous format's label.
    assert "renderCard(index)" in set_format
    assert "state.cards.forEach" in set_format


def test_done_card_labels_the_file_that_exists():
    source = (KOYURA_STATIC / "loader.js").read_text()
    assert "savedLabel(card)" in source
    assert "card.fileSizeLabel" in source


# ---------------------------------------------------------------------------
# 4. The drawer trigger on a phone
# ---------------------------------------------------------------------------

def _rule(css: str, selector: str) -> str:
    """The declaration block of one selector, whitespace-normalised.

    Split on the exact selector followed by `{`: a bare `.hamburger` would also
    match `.hamburger span`, and `.sidebar-open .hamburger` must not pick up
    the `:first-child` rules below it.
    """
    marker = selector + " {"
    assert marker in css, f"no rule for {selector!r}"
    body = css.split(marker, 1)[1].split("}", 1)[0]
    return " ".join(body.split())


def test_the_hamburger_moves_into_the_open_drawer():
    css = _strip_comments((STATIC / "style.css").read_text())

    # A 44x44 tap target that eases horizontally.
    hamburger = _rule(css, ".hamburger")
    assert "width: 44px" in hamburger
    assert "height: 44px" in hamburger
    assert "transition: left" in hamburger

    # Open: the button sits at the drawer's RIGHT edge, keeping the same 12px
    # inset it has on the closed page (44px target + 12px = 56px), and the
    # drawer's own left safe-area padding is subtracted so it stays on the
    # content edge of a notched device.
    opened = _rule(css, ".sidebar-open .hamburger")
    left = opened.split("left:", 1)[1].split(";", 1)[0]
    assert "var(--sidebar-width)" in left
    assert "56px" in left
    assert "var(--safe-left)" in left


def test_the_open_icon_is_a_cross_not_a_chevron():
    """Every bar shares the centre and only rotates, so the open state is a
    symmetric X. A `translateY(...) rotate(...)` mix pushes the bars along a
    slanted axis and reads as the "—>" the owner reported."""
    css = _strip_comments((STATIC / "style.css").read_text())

    bar = _rule(css, ".hamburger span")
    assert "position: absolute" in bar
    # Centred, not stacked in a flex column.
    assert "top: calc(50% - 0.5px)" in bar
    assert "left: calc(50% - 9px)" in bar

    first = _rule(css, ".sidebar-open .hamburger span:first-child")
    last = _rule(css, ".sidebar-open .hamburger span:last-child")
    middle = _rule(css, ".sidebar-open .hamburger span:nth-child(2)")

    assert first == "transform: translateY(0) rotate(45deg);"
    assert last == "transform: translateY(0) rotate(-45deg);"
    assert "opacity: 0" in middle
    # No stray offset: a non-zero translate is what bent the cross.
    assert "translateY(5px)" not in first and "translateY(5px)" not in last

    # Closed: the same two bars offset by 5px, middle visible.
    assert "translateY(-5px)" in _rule(css, ".hamburger span:first-child")
    assert "translateY(5px)" in _rule(css, ".hamburger span:last-child")


def test_the_hamburger_icon_does_not_animate_its_shape():
    """The rotation is instant so no snapshot can catch a half-turned bar.
    Only the colour eases."""
    css = _strip_comments((STATIC / "style.css").read_text())
    bar = _rule(css, ".hamburger span")
    assert "transition: background" in bar
    assert "transform" not in bar.split("transition:", 1)[1].split(";", 1)[0]
    assert "opacity" not in bar.split("transition:", 1)[1].split(";", 1)[0]


def test_the_drawer_transition_is_disabled_for_reduced_motion():
    """koyura.css is the one place that turns every transition off; this keeps
    the drawer's slide covered by it rather than adding a second copy."""
    tokens = (STATIC / "koyura.css").read_text()
    reduced = tokens.split("prefers-reduced-motion", 1)
    assert len(reduced) > 1, "no reduced-motion block"
    assert "transition: none !important" in reduced[1]



def test_the_shell_toggles_the_root_class_and_the_label():
    shell = (STATIC / "shell.js").read_text()
    assert "sidebar-open" in shell
    assert "document.documentElement" in shell
    assert "aria-expanded" in shell
    assert "aria-label" in shell


def test_the_hamburger_keeps_its_id_and_control_attributes():
    html = _page("/history")
    button = re.search(r"<button class=\"hamburger\" id=\"hamburger\"[^>]*>", html)
    assert button is not None
    tag = button.group(0)
    assert 'aria-controls="sidebar"' in tag
    assert 'aria-expanded="false"' in tag


# ---------------------------------------------------------------------------
# 5. /admin: one panel per block, one page per section
# ---------------------------------------------------------------------------

def test_admin_groups_its_blocks_into_separate_panels():
    """Every admin page is a stack of the same ``card panel``, and a panel
    belongs to exactly one page — that is what the split bought."""
    pages = (
        "/admin/cookies", "/admin/channels", "/admin/limits", "/admin/bot",
        "/admin/system",
    )
    for path in pages:
        html = _page(path)
        assert 'class="panel-stack"' in html, path
        assert html.count('class="card panel"') >= 1, path
        assert html.count('class="card-header panel-head"') >= 1, path

    system = _page("/admin/system")
    for title in ("Disk &amp; files", "System info"):
        assert title in system
    assert 'id="file-table"' in system
    assert _page("/admin/limits").count('class="card panel" id="limits-card"') == 1
    assert _page("/admin/bot").count('class="card panel" id="languages-card"') == 1
    assert 'id="progress-card"' not in _page("/admin/bot")
    assert 'id="templates-card"' in _page("/admin/bot")
    # The disk panels and the limits form are not on the same page any more.
    assert "Download limits" not in system
    assert "Disk management" not in _page("/admin/limits")


def test_admin_keeps_the_limits_form_contract():
    html = _page("/admin/limits")
    assert 'action="/admin/limits"' in html
    assert html.count('name="csrf_token"') >= 1
    assert html.count('data-action="limit-preset"') == 4
    assert 'id="limits-form"' in html
    assert 'id="limits-card"' in html
    assert 'id="hardware-line"' in html


def test_panels_are_separated_by_a_gap_not_by_child_margins():
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(), flags=re.S)
    stack = css.split(".panel-stack {")[1].split("}")[0]
    assert "display: flex" in stack
    assert "flex-direction: column" in stack
    assert "gap:" in stack


# ---------------------------------------------------------------------------
# 6. Tokens only, no inline script
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [STATIC / "style.css", KOYURA_STATIC / "loader.css"])
def test_the_new_rules_add_no_colour_literals(path: Path):
    css = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    found = COLOUR_LITERAL.findall(_without_token_blocks(css))
    assert not found, f"{path.name} hard-codes colours: {found[:5]}"


def test_no_template_gained_an_inline_handler():
    for template in list(TEMPLATES.glob("*.html")) + list(KOYURA_TEMPLATES.glob("*.html")):
        text = template.read_text()
        for attribute in ("onclick=", "onchange=", "onsubmit=", "onload=", "onerror="):
            assert attribute not in text, f"{template.name} still uses {attribute}"


def test_the_dialog_uses_the_shared_modal_skin():
    css = (STATIC / "style.css").read_text()
    assert ".modal-overlay[hidden]" in css
    assert ".link-modal-url" in css
    assert "word-break: break-all" in css.split(".link-modal-url {")[1].split("}")[0]
    assert "max-height" in css.split(".link-modal-url {")[1].split("}")[0]


# ---------------------------------------------------------------------------
# 7. The loader's send-size filter
# ---------------------------------------------------------------------------

NODE = "node"


def _node_available() -> bool:
    try:
        subprocess.run([NODE, "-v"], capture_output=True, check=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _run_filter(formats: list[dict], max_mb: float) -> dict:
    """Call the shipped ES module in Node, so the test covers real code."""
    script = (
        "import('" + (KOYURA_STATIC / "format_list.js").as_uri() + "')"
        ".then((m) => process.stdout.write(JSON.stringify("
        f"m.pickFormats({json.dumps(formats)}, {max_mb}))))"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(not _node_available(), reason="node is not installed")
def test_formats_above_the_limit_are_withheld():
    formats = [
        {"id": "4k", "label": "2160p", "size_bytes": 3 * 1024 ** 3},
        {"id": "hd", "label": "720p", "size_bytes": 100 * 1024 ** 2},
        {"id": "auto", "label": "auto"},
    ]
    picked = _run_filter(formats, 2000)
    assert [f["id"] for f in picked["formats"]] == ["hd", "auto"]
    assert picked["filtered"] is True


@pytest.mark.skipif(not _node_available(), reason="node is not installed")
def test_a_list_with_nothing_fitting_keeps_every_chip_marked():
    formats = [{"id": "4k", "label": "2160p", "size_bytes": 5 * 1024 ** 3}]
    picked = _run_filter(formats, 2000)
    assert len(picked["formats"]) == 1
    assert picked["formats"][0]["too_large"] is True
    assert picked["filtered"] is False


@pytest.mark.skipif(not _node_available(), reason="node is not installed")
def test_an_unknown_size_is_offered_and_a_zero_limit_means_unlimited():
    formats = [{"id": "x", "size_bytes": None}, {"id": "y", "size_bytes": 9 * 1024 ** 3}]
    assert _run_filter(formats, 2000)["formats"][0]["id"] == "x"
    assert len(_run_filter(formats, 0)["formats"]) == 2


def test_the_loader_reads_the_ceiling_from_the_page():
    html = (KOYURA_TEMPLATES / "index.html").read_text()
    assert 'id="api-config"' in html
    assert "max_send_mb" in html
    source = (KOYURA_STATIC / "loader.js").read_text()
    assert "from './format_list.js'" in source
    assert "pickFormats(" in source
    assert "q-chip--too-large" in source
