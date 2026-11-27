"""The progress switches: the set itself, and how the bot learns it from the panel.

What each switch *draws* is covered by ``test_progress_caption_fields.py``; this
module pins the switch names and defaults, the API body handling, and the
panel-down fallback to title + bar.

Everything here is pure: no Telegram, no engine, no event loop.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_settings  # noqa: E402
import progress_fields  # noqa: E402


# ---------------------------------------------------------------------------
# How the bot learns the switches
# ---------------------------------------------------------------------------

class TestBotSettingsCarriesThem:
    def test_a_panel_body_with_progress_is_read(self):
        settings = bot_settings.normalise_payload({
            "languages": ["en", "ru"],
            "default_language": "en",
            "progress": {"show_percent": True, "show_speed": True},
        })
        assert settings.progress["show_percent"] is True
        assert settings.progress["show_speed"] is True
        assert settings.progress["show_title"] is True
        assert settings.progress["show_eta"] is False

    def test_a_panel_body_without_progress_keeps_the_defaults(self):
        settings = bot_settings.normalise_payload(
            {"languages": ["en"], "default_language": "en"})
        assert settings.progress["show_title"] and settings.progress["show_bar"]
        assert not settings.progress["show_speed"]

    def test_the_built_in_defaults_are_title_and_bar(self):
        settings = bot_settings.defaults()
        assert settings.progress["show_title"] is True
        assert settings.progress["show_bar"] is True
        assert settings.progress.any_shown() is True

    def test_a_panel_that_cannot_answer_falls_back(self, monkeypatch):
        import httpx

        class _Boom:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise httpx.ConnectError("no route to host")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(bot_settings.httpx, "AsyncClient", _Boom)
        settings = __import__("asyncio").run(bot_settings.fetch())
        assert settings.source == "fallback"
        assert settings.progress["show_title"] and settings.progress["show_bar"]
        assert not settings.progress["show_percent"]

    def test_the_api_answer_is_the_switch_names(self):
        """The panel stores and serves the same eight names."""
        import importlib.util
        import pathlib

        path = pathlib.Path(__file__).resolve().parents[2] / "dashboard" / "progress_fields.py"
        if not path.exists():  # the bot suite can run without the dashboard dir
            pytest.skip("dashboard not next to the bot")
        spec = importlib.util.spec_from_file_location("dashboard_progress_fields", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # ``@dataclass`` looks its module up in ``sys.modules`` while building
        # the class, so the module has to be registered before it executes.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        assert tuple(module.FIELD_NAMES) == progress_fields.FIELD_NAMES
        assert dict(module.DEFAULTS) == dict(progress_fields.DEFAULTS)
