"""Which lines the bot draws in the live progress message.

The panel owns the choice (Admin → **Progress fields**, eight checkboxes stored
as ``bot_progress.*`` rows in its ``settings`` table); the bot only reads it from
``GET /api/bot-settings``, in the ``progress`` object of the answer:

    {"progress": {"show_title": true, "show_bar": true,
                  "show_percent": false, ...}}

Two properties matter more than the request itself:

* **the bot works with the panel down.** A dashboard that is restarting must not
  take the caption with it, so an unreachable panel (or a payload without the
  object) falls back to :data:`DEFAULTS` — the title and the bar, which is what
  the owner asked for. The fallback is a normal state, logged at DEBUG.
* **an unknown or unreadable switch is the default, never an error.** A row
  somebody edited by hand must not stop a download.

This module only decides *whether* a line is drawn; ``progress_card`` decides
what it looks like.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The switches, in caption order: title line, bar, then the facts line.
FIELD_NAMES: tuple[str, ...] = (
    "show_title",
    "show_bar",
    "show_percent",
    "show_size",
    "show_speed",
    "show_eta",
    "show_elapsed",
    "show_quality",
)

# What a deployment that never saved anything shows: the title and the bar.
DEFAULTS: dict[str, bool] = {
    "show_title": True,
    "show_bar": True,
    "show_percent": False,
    "show_size": False,
    "show_speed": False,
    "show_eta": False,
    "show_elapsed": False,
    "show_quality": False,
}

TRUE_VALUES = frozenset({"true", "1", "on", "yes"})
FALSE_VALUES = frozenset({"false", "0", "off", "no", ""})


def as_bool(value: object, default: bool = False) -> bool:
    """``true``/``"on"``/``"1"`` -> True; anything unparsable -> ``default``."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return default


@dataclass(frozen=True)
class Fields:
    """The eight switches, by name. ``True`` means "draw it"."""

    values: Mapping[str, bool]

    def __getitem__(self, name: str) -> bool:
        return self.values.get(name, DEFAULTS.get(name, False))

    def get(self, name: str) -> bool:
        return self[name]

    def any_shown(self) -> bool:
        """Whether at least one line is enabled (the stage line is always a fallback)."""
        return any(self[name] for name in FIELD_NAMES)

    def describe(self) -> str:
        """One line for the log: ``title,bar`` (or ``none``)."""
        on = [name.removeprefix("show_") for name in FIELD_NAMES if self[name]]
        return ",".join(on) or "none"


DEFAULT_FIELDS = Fields(values=dict(DEFAULTS))


def defaults() -> Fields:
    """The built-in answer: title and bar."""
    return DEFAULT_FIELDS


def normalise(payload: object) -> Fields:
    """Turn the API body into ``Fields``.

    ``payload`` is the whole ``/api/bot-settings`` body; only its ``progress``
    object is read, and a missing one (an older panel) means the defaults. A
    bare mapping of switches is accepted too, so a caller that already unwrapped
    the object still works.
    """
    if not isinstance(payload, Mapping):
        return defaults()
    raw = payload.get("progress", payload)
    if not isinstance(raw, Mapping):
        return defaults()
    return Fields(values={
        name: as_bool(raw.get(name), DEFAULTS[name]) for name in FIELD_NAMES
    })


__all__ = [
    "DEFAULT_FIELDS",
    "DEFAULTS",
    "FIELD_NAMES",
    "Fields",
    "as_bool",
    "defaults",
    "normalise",
]
