"""Shared test setup for the bot suite.

Two things every test needs and none of them should have to remember:

* **the package on ``sys.path``** — the bot runs as a flat directory of modules,
  so its own folder is the import root;
* **the panel answer, without the network** — ``bot_settings.get()`` would
  otherwise call the dashboard on every rendered message, which is both slow and
  a hidden dependency. The built-in defaults (``en,ru``, default ``en``) are
  primed into the cache; a test that needs another configuration calls
  ``bot_settings.prime(...)`` itself, and a test of the fallback calls
  ``bot_settings.fetch()`` directly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import bot_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _panel_defaults():
    """No test talks to the dashboard unless it asks to."""
    bot_settings.reset_cache()
    bot_settings.prime(bot_settings.defaults())
    yield
    bot_settings.reset_cache()
