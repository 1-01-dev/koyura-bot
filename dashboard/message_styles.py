"""The preset message styles the owner can apply in one click.

A *style* is one coherent voice for the whole bot — every kind in the
catalogue, in every language — shipped as a YAML file in
``bot/template_styles/``. The panel reads those files, validates them exactly
like a hand-typed body (known placeholders, Telegram's limit for the kind) and
writes the accepted ones as *active* templates, so from the bot's point of view
a style is indistinguishable from 112 bodies the owner saved by hand: both
arrive through ``GET /api/bot-settings`` in the same ``templates`` object.

Why a file and not a table: the texts are a product decision that goes through
review like code, and a style is a whole set — a half-imported one would read as
a bug in the chat. Keeping them as YAML in the repository means the owner can
diff two voices, and it costs the panel one read per apply.

Three properties this module guarantees:

* **Nothing is invented.** A style may only carry kinds the catalogue knows; an
  unknown kind in a file is ignored (the bot has no such message to render),
  and a file that names one is reported, never merged silently.
* **A bad body never reaches the bot.** Every body is validated before it is
  written, and an invalid one is *skipped*, not stored: the panel then says
  "applied 54 of 56, skipped: …". A style that is partly unusable still applies
  its good half, because the alternative (all or nothing) leaves the operator
  with no way to see which body was wrong.
* **The active style is one setting.** ``templates.style`` in the ``settings``
  table holds ``01-quiet-premium`` … ``04-friendly``, ``default`` (factory
  texts, no overrides at all) or ``custom`` (something was edited by hand after
  a style was applied). The panel only highlights buttons from it; the texts
  themselves live in ``message_templates``.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import message_catalog
import message_templates

#: Where the active style is recorded (``settings.key``).
SETTING_KEY = "templates.style"

#: Factory texts: no stored override at all, the bot renders the built-ins.
DEFAULT_STYLE = "default"

#: The set was edited by hand after a style was applied (or was never one).
CUSTOM_STYLE = "custom"

#: The languages a style file must carry for every kind it names.
LANGUAGES: tuple[str, ...] = message_catalog.LANGUAGES

#: The directory ``bot/template_styles`` looks like from the panel and from a
#: native install. ``TEMPLATE_STYLES_PATH`` overrides it for anything else.
_ENV_PATH = "TEMPLATE_STYLES_PATH"
_RELATIVE_CANDIDATES: tuple[str, ...] = (
    "template_styles",  # baked into the image, or mounted beside the panel
    "../bot/template_styles",  # native install and the repository checkout
)

#: Field names the panel reads out of a file; everything else (``builtin_en``,
#: ``customised``) documents the file for a human and is not imported.
_TEXT_FIELDS: tuple[str, ...] = LANGUAGES


class StyleError(ValueError):
    """A style file that cannot be read or is not the shape we expect."""


@dataclass(frozen=True)
class StylePreset:
    """One button in the panel, and the file behind it."""

    id: str
    label: str
    filename: str
    tagline: str


#: The presets, in the order the row of buttons shows them. ``id`` is the
#: settings value, so renaming one is a migration, not a copy edit.
STYLES: tuple[StylePreset, ...] = (
    StylePreset(
        id="01-quiet-premium",
        label="Quiet premium",
        filename="01-quiet-premium.yaml",
        tagline="Calm and restrained: short lines, no exclamation marks.",
    ),
    StylePreset(
        id="02-terminal",
        label="Terminal",
        filename="02-terminal.yaml",
        tagline="Mono, imperative, machine-like: a status line per event.",
    ),
    StylePreset(
        id="03-media-studio",
        label="Media studio",
        filename="03-media-studio.yaml",
        tagline="Editorial: the asset, its source and its specs, then the action.",
    ),
    StylePreset(
        id="04-friendly",
        label="Friendly",
        filename="04-friendly.yaml",
        tagline="Warm and conversational, with the user addressed as a person.",
    ),
)

BY_ID: dict[str, StylePreset] = {preset.id: preset for preset in STYLES}


@dataclass(frozen=True)
class Skipped:
    """One kind that was not written, and why — the panel prints this list."""

    kind: str
    reason: str

    def as_text(self) -> str:
        return f"{self.kind} ({self.reason})"


@dataclass
class Plan:
    """What an apply would write: the accepted bodies plus the refusals."""

    style: StylePreset
    bodies: dict[str, dict[str, str]] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)
    #: Kinds in the file the catalogue does not have (ignored, but reported).
    unknown: list[str] = field(default_factory=list)

    @property
    def applied(self) -> int:
        """How many kinds will be written (what "N of 56" counts)."""
        return len(self.bodies)

    @property
    def total(self) -> int:
        """The catalogue size, so the panel can say "N of 56"."""
        return len(message_catalog.KINDS)


# ---------------------------------------------------------------------------
# Where the files are
# ---------------------------------------------------------------------------

def styles_dir() -> Path:
    """The first candidate directory that holds style files.

    Resolved per call rather than at import: the tests, the image and a native
    install see different paths, and a restart must not need a code change.
    """
    override = (os.environ.get(_ENV_PATH) or "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    for candidate in _RELATIVE_CANDIDATES:
        path = (here / candidate).resolve()
        if path.is_dir():
            return path
    # Report the repository location so the notice names a real path.
    return (here / _RELATIVE_CANDIDATES[-1]).resolve()


def path_for(style_id: str) -> Path:
    preset = BY_ID.get(str(style_id))
    if preset is None:
        raise StyleError(f"Unknown style: {style_id}.")
    return styles_dir() / preset.filename


def installed() -> dict[str, bool]:
    """``{style_id: file is on disk}`` — the panel greys out what is missing."""
    return {preset.id: path_for(preset.id).is_file() for preset in STYLES}


def normalise_style(value: object) -> str:
    """A settings value that means something: a preset, ``default`` or ``custom``."""
    name = str(value or "").strip()
    if name in BY_ID or name in (DEFAULT_STYLE, CUSTOM_STYLE):
        return name
    return DEFAULT_STYLE


def label_of(style_id: object) -> str:
    """The button label for a stored value."""
    name = normalise_style(style_id)
    if name == DEFAULT_STYLE:
        return "Default"
    if name == CUSTOM_STYLE:
        return "Custom"
    return BY_ID[name].label


# ---------------------------------------------------------------------------
# Reading a file
# ---------------------------------------------------------------------------

def _flatten(payload: object, path: Path) -> dict[str, Mapping[str, object]]:
    """``{kind: entry}`` from the category-grouped file, ignoring ``_about``."""
    if not isinstance(payload, Mapping):
        raise StyleError(f"{path.name} is not a mapping of categories.")
    kinds: dict[str, Mapping[str, object]] = {}
    for category, members in payload.items():
        if str(category).startswith("_"):
            continue
        if not isinstance(members, Mapping):
            raise StyleError(f"{path.name}: category {category!r} is not a mapping.")
        for kind, entry in members.items():
            if not isinstance(entry, Mapping):
                raise StyleError(f"{path.name}: {kind!r} is not a mapping.")
            kinds[str(kind)] = entry
    return kinds


def read(style_id: object) -> dict[str, dict[str, str]]:
    """The bodies in one style file, as ``{kind: {lang: body}}``.

    Only the text fields are imported: ``media``/``placeholders`` in the file are
    documentation, and the catalogue is what decides both. Raises
    :class:`StyleError` for a missing file or a YAML document that is not the
    category -> kind -> ``{en, ru}`` shape.
    """
    preset = BY_ID.get(str(style_id))
    if preset is None:
        raise StyleError(f"Unknown style: {style_id}.")
    path = path_for(preset.id)
    if not path.is_file():
        raise StyleError(f"Style file is not installed: {path.name}.")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - the image installs it
        raise StyleError("PyYAML is required to read a style file.") from exc
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StyleError(f"Cannot read {path.name}: {exc}") from exc

    bodies: dict[str, dict[str, str]] = {}
    for kind, entry in _flatten(payload, path).items():
        by_language = {
            language: str(entry.get(language) or "")
            for language in _TEXT_FIELDS
            if entry.get(language) is not None
        }
        if by_language:
            bodies[kind] = by_language
    if not bodies:
        raise StyleError(f"{path.name} carries no message bodies.")
    return bodies


# ---------------------------------------------------------------------------
# Validating a style against the catalogue
# ---------------------------------------------------------------------------

def _kind_problem(
    kind_id: str, by_language: Mapping[str, str]
) -> str | None:
    """Why one kind cannot be written, or ``None`` when it can.

    The rules are the panel's own (:func:`message_templates.validate` plus the
    hard limit): a hand-typed body and a preset must not have two standards.
    """
    kind = message_catalog.get(kind_id)
    if kind is None:
        return None  # reported as "unknown", not as a rejection
    for language in LANGUAGES:
        body = by_language.get(language)
        if body is None:
            return f"no {language} body"
        reason = validate_body(body, kind_id)
        if reason:
            return f"{language}: {reason}"
    return None


def validate_body(body: object, kind_id: str) -> str | None:
    """The first thing wrong with one body, in the panel's words."""
    kind = message_catalog.get(kind_id)
    if kind is None:
        return f"unknown message kind {kind_id}"
    text = str(body or "")
    if not text.strip():
        return "empty body"
    unknown = message_templates.unknown_placeholders(text, kind.placeholders)
    if unknown:
        return "unknown placeholder " + ", ".join(f"@{name}" for name in unknown)
    if len(text) > kind.limit:
        return f"{len(text)} characters, over the {kind.limit} limit"
    return None


def plan(style_id: object) -> Plan:
    """Read and validate one preset: what would be written, what would not.

    Never raises for a bad body — the caller needs the list. A missing file or
    unreadable YAML is still an error, because there is nothing to report about
    a style that is not there.
    """
    preset = BY_ID.get(str(style_id))
    if preset is None:
        raise StyleError(f"Unknown style: {style_id}.")
    bodies = read(preset.id)
    result = Plan(style=preset)
    for kind_id in sorted(bodies, key=str):
        by_language = bodies[kind_id]
        if kind_id not in message_catalog.BY_ID:
            result.unknown.append(kind_id)
            continue
        reason = _kind_problem(kind_id, by_language)
        if reason:
            result.skipped.append(Skipped(kind=kind_id, reason=reason))
            continue
        result.bodies[kind_id] = {
            language: by_language[language] for language in LANGUAGES
        }
    return result


# ---------------------------------------------------------------------------
# The sentence the panel shows
# ---------------------------------------------------------------------------

def apply_notice(plan_result: Plan, *, cleared: int = 0) -> str:
    """``Applied 54 of 56 from Friendly. Skipped: …`` — never silent.

    The summary counts *kinds*, because that is what the operator picked; the
    first three refusals are spelled out with the reason and the rest are
    counted, so a style with a systematic mistake does not push the notice out
    of the panel.
    """
    parts = [
        f"Applied {plan_result.applied} of {plan_result.total} kinds "
        f"from {plan_result.style.label}."
    ]
    if plan_result.skipped:
        shown = "; ".join(item.as_text() for item in plan_result.skipped[:3])
        rest = len(plan_result.skipped) - 3
        if rest > 0:
            shown += f"; and {rest} more"
        parts.append(f"Skipped {len(plan_result.skipped)}: {shown}.")
    if plan_result.unknown:
        known = ", ".join(plan_result.unknown[:3])
        rest = len(plan_result.unknown) - 3
        if rest > 0:
            known += f", and {rest} more"
        parts.append(f"Ignored kinds this bot does not send: {known}.")
    if cleared:
        parts.append(f"Replaced {cleared} previous body/bodies.")
    return " ".join(parts)


def default_notice(*, cleared: int) -> str:
    """What the ``Default`` button did: every override is gone."""
    if not cleared:
        return "Already on the factory texts; there was nothing to clear."
    return (
        f"Cleared {cleared} custom template(s). Every message is the built-in "
        "default again."
    )


__all__ = [
    "BY_ID",
    "CUSTOM_STYLE",
    "DEFAULT_STYLE",
    "LANGUAGES",
    "Plan",
    "SETTING_KEY",
    "STYLES",
    "Skipped",
    "StyleError",
    "StylePreset",
    "apply_notice",
    "default_notice",
    "installed",
    "label_of",
    "normalise_style",
    "path_for",
    "plan",
    "read",
    "styles_dir",
    "validate_body",
]
