"""Which lines the bot draws in its progress message, as an admin setting.

The progress card used to print everything it knew — title, bar, percentage,
size, speed, ETA, elapsed time, quality — and the owner asked for the opposite:
title and bar only, with the rest opt-in. Which of them a *deployment* shows is
an operator decision, so it lives in the dashboard's ``settings`` table next to
the download limits and the bot languages, and the bot reads it from
``GET /api/bot-settings``.

Three pieces:

* :data:`FIELDS` — the eight switches, in the order the form and the caption
  use them, each with the label and the hint the panel shows. Adding a field is
  a change here *and* in ``bot/progress_card.py``.
* :class:`ProgressFields` — the validated set, with the storage value kept a
  plain ``true``/``false`` string per key (``bot_progress.show_speed = false``).
* :func:`parse` — the form validator. Unchecked boxes are the only signal the
  browser sends, so a missing key is "off", and there is exactly one rule worth
  stating in a message: a caption with nothing on it is not allowed, unless the
  bar or the title carries it — which is why the bot still falls back to the
  stage line (see ``bot/progress_card.py``) instead of rendering an empty
  message. The form therefore accepts an all-off set rather than refusing it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Storage: the same ``settings`` table the limits use, one row per key.
SETTING_PREFIX = "bot_progress."

# Every switch, in caption order: the title on its own line, the bar, then the
# facts line. ``(key, label, hint, default)``.
FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("show_title", "Title",
     "the name of the video, bold, on its own line", True),
    ("show_bar", "Progress bar",
     "12 fixed cells (▓▓▓░░░) — the same width with the percent off", True),
    ("show_percent", "Percent",
     "62% after the bar, or on the facts line when the bar is off", False),
    ("show_size", "Size",
     "30.8 MB / 30.8 MB — hidden when the source reports no exact size", False),
    ("show_speed", "Speed",
     "58.9 MB/s, as the engine measures it", False),
    ("show_eta", "Time left",
     "ETA 0:42 — the engine's number, else remaining bytes over speed", False),
    ("show_elapsed", "Elapsed",
     "0:13 elapsed, counted from the moment the job started", False),
    ("show_quality", "Quality",
     "1440p — the format that was picked, when one was", False),
)

FIELD_NAMES: tuple[str, ...] = tuple(name for name, *_rest in FIELDS)

# A deployment that never saved anything shows the title and the bar.
DEFAULTS: dict[str, bool] = {name: default for name, _label, _hint, default in FIELDS}

TRUE_VALUES = frozenset({"true", "1", "on", "yes"})
FALSE_VALUES = frozenset({"false", "0", "off", "no", ""})


def as_bool(value: object, default: bool = False) -> bool:
    """``"true"``/``"on"``/``"1"`` -> True; anything unparsable -> ``default``.

    A row somebody edited by hand must not take the panel down, so an
    unreadable value is the default rather than an exception.
    """
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
class ProgressFields:
    """The eight switches, by name. ``True`` means "draw it"."""

    values: Mapping[str, bool]

    # -- construction ------------------------------------------------------

    @classmethod
    def defaults(cls) -> ProgressFields:
        return cls(values=dict(DEFAULTS))

    @classmethod
    def from_row(cls, stored: Mapping[str, str]) -> ProgressFields:
        """Read the stored strings, falling back per key to the default."""
        values = {}
        for name, _label, _hint, default in FIELDS:
            raw = stored.get(f"{SETTING_PREFIX}{name}")
            values[name] = default if raw is None else as_bool(raw, default)
        return cls(values=values)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ProgressFields:
        """Read the bot's API body, which carries plain booleans."""
        values = {}
        for name, _label, _hint, default in FIELDS:
            values[name] = as_bool(payload.get(name), default)
        return cls(values=values)

    # -- lookup ------------------------------------------------------------

    def __getitem__(self, name: str) -> bool:
        return self.values.get(name, DEFAULTS.get(name, False))

    def get(self, name: str) -> bool:
        return self[name]

    def any_shown(self) -> bool:
        """Whether at least one line is enabled (the bot always draws the stage)."""
        return any(self[name] for name in FIELD_NAMES)

    # -- serialisation -----------------------------------------------------

    def as_stored(self) -> dict[str, str]:
        """The rows to write: one ``true``/``false`` string per key."""
        return {
            f"{SETTING_PREFIX}{name}": "true" if self[name] else "false"
            for name in FIELD_NAMES
        }

    def as_api(self) -> dict[str, bool]:
        """The body ``GET /api/bot-settings`` adds for the bot."""
        return {name: self[name] for name in FIELD_NAMES}

    def as_form(self) -> dict[str, object]:
        """What the template needs: the checkboxes and their state."""
        return {
            "fields": [
                {
                    "name": name,
                    "label": label,
                    "hint": hint,
                    "enabled": self[name],
                    "default": default,
                }
                for name, label, hint, default in FIELDS
            ]
        }

    def describe(self) -> str:
        """One line for the log: ``title,bar`` (or ``none``)."""
        on = [name.removeprefix("show_") for name in FIELD_NAMES if self[name]]
        return ",".join(on) or "none"


def parse(checked: Iterable[str]) -> tuple[ProgressFields | None, list[str]]:
    """Validate a submitted form: ticked boxes are on, everything else is off.

    There is deliberately no "keep at least one" rule here: the bot falls back
    to the stage line when every line is off, so an all-off set is a valid (if
    spartan) choice and refusing it would need a message that explains a rule
    the operator cannot see in the form.
    """
    chosen = {str(value).strip() for value in checked}
    unknown = sorted(chosen - set(FIELD_NAMES))
    if unknown:
        return None, [f"Unknown progress field: {', '.join(unknown)}."]
    return ProgressFields(values={name: name in chosen for name in FIELD_NAMES}), []


def options() -> list[dict[str, str]]:
    """Every field the form may offer (checked or not)."""
    return [{"name": name, "label": label} for name, label, _hint, _default in FIELDS]


__all__ = [
    "DEFAULTS",
    "FIELDS",
    "FIELD_NAMES",
    "ProgressFields",
    "SETTING_PREFIX",
    "as_bool",
    "options",
    "parse",
]
