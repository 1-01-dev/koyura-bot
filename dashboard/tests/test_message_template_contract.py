"""The contract between the panel's catalogue and the bot's registry.

The two services cannot import each other (separate images, separate
dashboards), so the catalogue exists twice: ``dashboard/message_catalog.py`` is
what the panel lists and documents, ``bot/message_kinds.py`` is what the bot
renders. They are allowed to be two files; they are *not* allowed to disagree
about a message id, its media type, its placeholders or its default text — the
owner would edit one thing and the bot would send another.

This module is the one place that imports both sides. It also renders the same
bodies through both renderers and compares the MarkdownV2 output, so the
preview in the panel cannot drift from the message in the chat.
"""
# ruff: noqa: I001 - the imports below run after sys.path is pointed at both services
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = ROOT / "dashboard"
BOT_DIR = ROOT / "bot"

# The dashboard's own modules (``message_catalog``, the panel engine) import by
# plain name, like the rest of that service; the bot's do too.
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(BOT_DIR))

import message_catalog  # noqa: E402

# The bot's registry lives in the other service: import it by name at runtime so
# a static checker does not try to resolve it from this package.
message_kinds = importlib.import_module("message_kinds")


def _load(alias: str, path: Path):
    """Load one module under a private name (both services ship the same one)."""
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


panel_engine = _load("panel_message_templates", DASHBOARD_DIR / "message_templates.py")
bot_engine = _load("bot_message_templates", BOT_DIR / "message_templates.py")


def test_the_languages_match():
    assert tuple(message_catalog.LANGUAGES) == tuple(message_kinds.LANGUAGES)


def test_every_kind_exists_on_both_sides():
    panel = {kind.id for kind in message_catalog.KINDS}
    bot = {kind.id for kind in message_kinds.KINDS}
    assert panel == bot


@pytest.mark.parametrize("kind_id", [kind.id for kind in message_kinds.KINDS])
def test_each_kind_agrees_on_media_and_placeholders(kind_id: str):
    bot_kind = message_kinds.get(kind_id)
    panel_kind = message_catalog.get(kind_id)
    assert bot_kind is not None and panel_kind is not None
    assert bot_kind.media == panel_kind.media
    assert tuple(bot_kind.placeholders) == tuple(panel_kind.placeholders)
    assert tuple(bot_kind.i18n_keys) == tuple(panel_kind.i18n_keys)


@pytest.mark.parametrize("kind_id", [kind.id for kind in message_kinds.KINDS])
def test_each_kind_has_the_same_default_text(kind_id: str):
    bot_kind = message_kinds.get(kind_id)
    panel_kind = message_catalog.get(kind_id)
    assert bot_kind is not None and panel_kind is not None
    for lang in message_kinds.LANGUAGES:
        assert bot_kind.template(lang) == panel_kind.template(lang), f"{kind_id}/{lang}"


def test_the_limits_match():
    assert message_catalog.LIMITS == bot_engine.LIMITS
    assert message_catalog.BUDGETS == bot_engine.BUDGETS


ESCAPE_CASES = [
    "plain",
    "a.b!c-d",
    "Tom & Jerry (1940)",
    r"already\*escaped",
    "*bold* _italic_ ~strike~ ||spoiler|| `code`",
]


@pytest.mark.parametrize("text", ESCAPE_CASES)
def test_both_escape_the_same_way(text: str):
    # The bot keeps one escaper, in ``i18n``; the panel has its copy.
    assert panel_engine.escape_md(text) == bot_engine.i18n.escape_md(text)


RENDER_CASES = [
    ("*@title*", {"title": "Tom & Jerry"}),
    ("*@title*\nDuration: @duration", {"title": "Clip", "duration": ""}),
    ("@views views · @likes likes", {"views": "", "likes": "8.4K"}),
    ("*unpaired @title", {"title": "Clip"}),
    (r"\*literal\* @title", {"title": "Clip"}),
    ("@a @b @c", {"a": "1", "b": "2", "c": "3"}),
    ("plain text with a dot. and a dash-", {}),
]


@pytest.mark.parametrize("body,values", RENDER_CASES)
def test_both_render_the_same_markdown(body: str, values: dict):
    assert panel_engine.render_body(body, values) == bot_engine.render_body(body, values)


def test_both_refuse_the_same_placeholders():
    for engine in (panel_engine, bot_engine):
        assert engine.unknown_placeholders("@a @b", ("a",)) == ["b"]
        with pytest.raises(engine.TemplateError):
            engine.render_body("@nope", {"a": "1"})
        with pytest.raises(engine.TemplateError):
            engine.render_body("  ", {})


def test_the_panel_renders_the_bots_default_text():
    """A "duplicate" starts as the default: it must render without an error."""
    for kind in message_catalog.KINDS:
        preview = panel_engine.preview(kind.id, "en")
        assert preview["error"] is None, kind.id
        assert str(preview["text"]).strip(), kind.id
