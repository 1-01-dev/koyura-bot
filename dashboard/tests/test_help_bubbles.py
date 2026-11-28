"""The help bubble: one "?" button that hides a long explanation.

The owner's complaint was that long paragraphs pushed the panels apart, so the
contract pinned here is deliberately narrow: the *long* prose moves behind a
button, the *short* captions a field needs while typing stay on the page, and
the hidden text is really hidden — not merely dimmed or clipped.

What is checked:

* every page that used to print one of these notes renders the button and its
  panel, with ``aria-expanded``, ``aria-controls`` and an ``aria-label`` that
  says which note it opens;
* the panel starts ``hidden`` and no old ``<p class="limits-note" id="…">``
  copy of the same text is left behind, so nothing is said twice;
* the toggle is a real ``<button>`` (Enter and Space come from the element, not
  from a key handler that could fire the click twice);
* ``js/help.js`` owns Escape, restores the focus and closes the other bubbles
  in its group, and the markup carries no inline handler;
* the short captions stay visible: the unit/range line under a limits field and
  the "no restart" line next to the save button are outside every panel.

The pages come from the same app the other dashboard design tests use, so the
environment block is identical.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("ADMIN_PASSWORD", "Koyura-Test-Passw0rd")
os.environ.setdefault("SECRET_KEY", "koyura-test-secret-key-0123456789abcdef")
os.environ.setdefault("DB_PATH", os.path.join(_tmpdir, "help_bubbles.db"))
os.environ.setdefault("DOWNLOADS_PATH", _tmpdir)
os.environ.pop("INTERNAL_API_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from main import create_app  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = DASHBOARD_DIR / "templates"
STATIC = DASHBOARD_DIR / "static"
MODULES = STATIC / "js"
HELP_TEMPLATE = TEMPLATES / "_help.html"
HELP_MODULE = MODULES / "help.js"

app = create_app()

#: Admin page -> the help panel ids it must carry. The proxy page is the one
#: the owner pointed at; the rest are the paragraphs that shared its shape.
EXPECTED_PANELS = {
    "/admin/proxy": ["proxy-note", "proxy-assignment-hint", "wireguard-note"],
    "/admin/cookies": ["cookies-note"],
    "/admin/limits": ["per-user-hint"],
    "/admin/bot": ["languages-note", "templates-note"],
    "/admin/channels": ["gate-switch-help"],
}

#: The text each panel hides. Asserted so a future edit cannot empty a panel
#: while keeping the button: a "?" that opens nothing is worse than the
#: paragraph it replaced.
PANEL_TEXT = {
    "proxy-note": "Some sites refuse a datacentre IP",
    "proxy-assignment-hint": "It is the same catalogue",
    "wireguard-note": "this panel only writes the file",
    "cookies-note": "Netscape cookie",
    "languages-note": "The bot offers only the languages enabled here",
    "templates-note": "Один активный шаблон",
    "per-user-hint": "the fairest choice when more than one person uses the bot",
    "gate-switch-help": "behaves as if this page did not exist",
}


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


def _toggles(html: str) -> list[dict[str, str]]:
    """Every help toggle on the page, as its attributes."""
    found = []
    for match in re.finditer(r"<button\b[^>]*>", html, re.S):
        tag = match.group(0)
        if "help-toggle" not in tag:
            continue
        attrs = dict(re.findall(r'([a-z-]+)="([^"]*)"', tag))
        found.append(attrs)
    return found


def _panels(html: str) -> dict[str, str]:
    """Help panel id -> the markup between its tags.

    Brace-free but tag-aware: a panel carries `<span class="mono">` inside it,
    so the closing tag is found by counting nested spans rather than by taking
    the first `</span>` the regex meets.
    """
    panels: dict[str, str] = {}
    for start in re.finditer(r'<span class="help-panel" id="([^"]+)"', html):
        panel_id = start.group(1)
        depth = 0
        index = start.start()
        while index < len(html):
            opens = html.find("<span", index)
            closes = html.find("</span>", index)
            if closes == -1:
                break
            if opens != -1 and opens < closes:
                depth += 1
                index = opens + 5
                continue
            depth -= 1
            index = closes + 7
            if depth == 0:
                break
        panels[panel_id] = html[start.start():index]
    return panels


# ---------------------------------------------------------------------------
# 1. The markup the pages render
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,panel_ids", EXPECTED_PANELS.items())
def test_the_page_carries_a_toggle_and_a_panel_per_note(path: str, panel_ids: list[str]):
    html = _page(path)
    toggles = {toggle.get("aria-controls"): toggle for toggle in _toggles(html)}
    panels = _panels(html)

    for panel_id in panel_ids:
        assert panel_id in toggles, f"{path} has no toggle for #{panel_id}"
        assert panel_id in panels, f"{path} has no panel #{panel_id}"

        toggle = toggles[panel_id]
        assert toggle.get("aria-expanded") == "false", panel_id
        assert toggle.get("type") == "button", f"#{panel_id} is not a plain button"
        assert toggle.get("aria-label"), f"#{panel_id} has no accessible name"
        assert toggle.get("class", "").startswith("help-toggle"), panel_id
        # The glyph is a question mark, not an image or an emoji font.
        assert "?" in html.split(f'aria-controls="{panel_id}"', 1)[1][:400]


@pytest.mark.parametrize("path", EXPECTED_PANELS)
def test_the_toggle_names_the_note_it_opens(path: str):
    """Three buttons called "Help" are indistinguishable in a control list."""
    html = _page(path)
    labels = [toggle.get("aria-label") for toggle in _toggles(html)]
    assert labels, path
    assert all(label and label.strip() for label in labels), labels
    assert len(set(labels)) == len(labels), f"{path} repeats an aria-label: {labels}"


def test_the_panel_is_hidden_until_it_is_asked_for():
    """`hidden`, not a class and not a style: the server says closed."""
    for path in EXPECTED_PANELS:
        html = _page(path)
        for panel_id, markup in _panels(html).items():
            assert re.search(r"\shidden(\s|>)", markup), (
                f"{path}: #{panel_id} would be visible before the click"
            )


def _collapse(markup: str) -> str:
    """One line, one space: the templates wrap prose across source lines."""
    return re.sub(r"\s+", " ", markup)


def test_the_hidden_text_is_still_in_the_page_and_still_says_something():
    """Hiding the note must not delete it — the button has to open an answer."""
    for path, panel_ids in EXPECTED_PANELS.items():
        html = _page(path)
        panels = _panels(html)
        for panel_id in panel_ids:
            marker = _collapse(PANEL_TEXT[panel_id])
            assert marker in _collapse(panels[panel_id]), f"#{panel_id} lost its text"

    # The owner's example: the sentence that was on /admin/proxy in full.
    panels = _panels(_page("/admin/proxy"))
    assert PANEL_TEXT["proxy-note"] in _collapse(panels["proxy-note"])


@pytest.mark.parametrize("legacy_id", list(PANEL_TEXT))
def test_the_old_paragraph_is_not_rendered_a_second_time(legacy_id: str):
    """One copy of the text: the note moved into the panel, it was not copied."""
    for path in EXPECTED_PANELS:
        html = _page(path)
        if legacy_id not in html:
            continue
        para = re.search(
            r'<p[^>]*id="' + re.escape(legacy_id) + r'"[^>]*>', html
        )
        assert para is None, f"{path} still prints the note as a paragraph"


def test_every_panel_id_is_unique_and_resolves():
    for path in EXPECTED_PANELS:
        html = _page(path)
        ids = re.findall(r'id="([^"]+)"', html)
        assert len(ids) == len(set(ids)), f"{path} repeats an element id"
        for toggle in _toggles(html):
            assert toggle.get("aria-controls") in ids, path


def test_the_headings_stay_visible():
    """The "?" sits next to the title, it does not replace it."""
    proxy = _page("/admin/proxy")
    # The page became a connection list; the headings say what the two cards
    # hold now, and both keep their "?" beside the title.
    assert '<h2 class="card-title">Connections</h2>' in proxy
    assert '<h2 class="card-title">WireGuard config</h2>' in proxy
    assert '<h2 class="card-title">Cookie files</h2>' in _page("/admin/cookies")
    assert '<h2 class="card-title">Bot languages</h2>' in _page("/admin/bot")


# ---------------------------------------------------------------------------
# 2. Short captions stay on the page
# ---------------------------------------------------------------------------

def test_the_limits_fields_keep_their_unit_and_range_visible():
    """A caption read while typing is not an explanation: it stays."""
    html = _page("/admin/limits")
    panels = "".join(_panels(html).values())
    for visible in ("slots · 1…8", "slots · 1…", "MB/s ·"):
        if visible not in html:
            continue
        assert visible in html.replace(panels, ""), (
            f"{visible!r} belongs under the field, not behind the question mark"
        )


def test_the_save_line_stays_visible():
    for path, text in (
        ("/admin/limits", "applied to the engine immediately, no restart"),
        ("/admin/proxy", "the engine picks it up within a minute"),
        ("/admin/bot", "the bot picks this up within a minute, no restart"),
    ):
        html = _page(path)
        visible = html
        for markup in _panels(html).values():
            visible = visible.replace(markup, "")
        assert text in visible, f"{path} hid a caption that belongs on the page"


def test_the_hardware_line_stays_on_the_page():
    """It is data (this server's CPU and RAM), not an explanation."""
    html = _page("/admin/limits")
    visible = html
    for markup in _panels(html).values():
        visible = visible.replace(markup, "")
    assert 'id="hardware-line"' in visible
    assert "vCPU" in visible


# ---------------------------------------------------------------------------
# 3. Behaviour: no inline script, keyboard, focus
# ---------------------------------------------------------------------------

def test_the_component_is_one_shared_macro_file():
    source = HELP_TEMPLATE.read_text()
    assert "{% macro help_toggle(" in source
    assert "{% macro help_panel(" in source
    # aria-expanded starts closed and the panel starts hidden, in one place.
    assert 'aria-expanded="false"' in source
    assert "hidden" in source
    imported = [
        path.name
        for path in TEMPLATES.glob("*.html")
        if "_help.html" in path.read_text()
    ]
    assert len(imported) >= 4, f"the macro is not shared: {imported}"


@pytest.mark.parametrize("path", ["/admin/cookies", "/admin/proxy", "/admin/limits", "/admin/bot"])
def test_the_page_has_no_inline_handler_or_script(path: str):
    html = _page(path)
    for attribute in ("onclick=", "onkeydown=", "onchange=", "onsubmit="):
        assert attribute not in html, f"{path} still uses {attribute}"
    assert "<script>" not in html, f"{path} has an inline script"


def test_the_toggle_is_a_button_so_enter_and_space_come_from_the_element():
    """`<button>` already activates on Enter and Space.

    A keydown handler for those keys would fire the click *and* the key, so the
    module must not have one; this test fails if one is added.
    """
    source = HELP_MODULE.read_text()
    assert "'Enter'" not in source
    assert '"Enter"' not in source
    assert "' '" not in source
    assert "'Spacebar'" not in source
    assert _page("/admin/proxy").count('type="button"') >= 1


def test_escape_closes_the_bubble_and_returns_the_focus():
    source = HELP_MODULE.read_text()
    # Escape is handled once, for the page: a second listener on the button
    # would close twice and land the focus on a hidden panel.
    assert source.count("addEventListener('keydown'") == 1
    handler = source.split("function onPageKeydown", 1)[1]
    assert "'Escape'" in handler
    assert "focus()" in handler
    assert "setOpen(open, false)" in handler
    # The button itself has no key handler at all: `<button>` activates on
    # Enter and Space already, and a click handler on top of that would toggle
    # twice for one key press.
    button_handler = source.split("toggle.addEventListener('click'", 1)[1]
    button_handler = button_handler.split("\n  });", 1)[0]
    assert "addEventListener" not in button_handler


def test_opening_one_bubble_closes_the_others_in_its_group():
    source = HELP_MODULE.read_text()
    assert "data-help-group" in source
    assert "closeSiblings" in source
    assert "closest('[data-help-group]')" in source
    # The groups exist in the markup, otherwise the JS silently does nothing.
    for path in EXPECTED_PANELS:
        assert "data-help-group" in _page(path), path


def test_the_group_is_the_page_not_an_individual_card():
    """A group per header would never close anything.

    /admin/proxy carries three bubbles in three different headers; grouping each
    one on its own header (the first version of this markup) left all three open
    on a phone at once. The container is the group.
    """
    for path, container in (
        ("/admin/proxy", "panel-stack"),
        ("/admin/cookies", "panel-stack"),
        ("/admin/limits", "panel-stack"),
        ("/admin/bot", "panel-stack"),
    ):
        html = _page(path)
        assert re.search(
            r'class="' + container + r'" data-help-group="[a-z]+"', html
        ), f"{path}: the help group is not on the page container"
        headers = re.findall(r'<div class="card-header panel-head"([^>]*)>', html)
        for attrs in headers:
            assert "data-help-group" not in attrs, (
                f"{path}: a card header owns its own group, so bubbles never close"
            )


def test_the_module_only_flips_hidden_and_aria_expanded():
    """The state lives in two attributes; nothing else is mutated."""
    source = HELP_MODULE.read_text()
    assert "panel.hidden = !open" in source
    assert "setAttribute('aria-expanded'" in source
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "style.display"):
        assert forbidden not in source, forbidden


def test_the_module_is_wired_by_the_page_entry():
    entry = (STATIC / "dashboard.js").read_text()
    assert "initHelpBubbles" in entry
    assert "'./js/help.js'" in entry


# ---------------------------------------------------------------------------
# 4. The stylesheet: hidden means gone, and nothing widens a 390px page
# ---------------------------------------------------------------------------

def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", (STATIC / "style.css").read_text(), flags=re.S)


def _rule(selector: str) -> str:
    css = _css()
    match = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    assert match is not None, f"no rule for {selector}"
    return match.group(1)


def test_the_hidden_panel_is_display_none():
    assert "display: none" in _rule(".help-panel[hidden]")


def test_an_open_panel_is_a_block_so_max_width_applies():
    """A span is inline: the first build rendered the note 68ch wide anyway."""
    assert "display: block" in _rule(".help-panel")


def test_the_button_is_a_one_pixel_mono_square_in_the_panel_tokens():
    body = _rule(".help-toggle")
    assert "border: 1px solid var(--line)" in body
    assert "font-family: var(--font-mono)" in body
    # A square, a hairline — and no rounded corner anywhere in the design.
    assert "22px" in body
    assert "border-radius" not in body


def test_a_long_hint_cannot_widen_the_page():
    """A pasted key or a URL in a hint must wrap inside its cell."""
    body = _rule(".help-panel")
    assert "max-width" in body
    assert "overflow-wrap: break-word" in body
    assert "word-break: break-word" in body


def test_the_bubble_stacks_in_a_narrow_cell():
    """At 390px the button sits in a wrapping row, never in a fixed column."""
    body = _rule(".help-row")
    assert "display: inline-flex" in body
    assert "min-width: 0" in body
    assert "flex-wrap" not in body.split("gap")[0]  # inline row, not a grid
    # The coarse-pointer block gives a thumb its 24px target.
    assert "pointer: coarse" in _css()
    assert re.search(
        r"@media \(pointer: coarse\)\s*\{[^}]*\.help-toggle\s*\{[^}]*width: 28px",
        _css(),
        re.S,
    )


# ---------------------------------------------------------------------------
# 5. The module loads without a DOM (and exports its entry point)
# ---------------------------------------------------------------------------

def _node_available() -> bool:
    try:
        subprocess.run(["node", "-v"], capture_output=True, check=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


@pytest.mark.skipif(not _node_available(), reason="node is not installed")
def test_the_module_imports_without_a_document_and_exports_its_entry():
    """Loading it must not touch the DOM: the page entry imports it everywhere."""
    script = (
        "import('" + HELP_MODULE.as_uri() + "')"
        ".then((m) => process.stdout.write(typeof m.initHelpBubbles))"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "function"
