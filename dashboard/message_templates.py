"""Custom message templates: validation, rendering and preview data.

The bot's messages are built from ``bot/i18n.py``. This module is the panel side
of the same idea: it stores the owner's own bodies in SQLite, renders the
preview with **the same rules the bot applies** (values escaped, a template with
unpaired markup sent as plain text, unknown placeholders refused), and hands the
active bodies to the bot through ``GET /api/bot-settings``.

One active template per ``(kind, language)``: the partial unique index on
``message_templates`` enforces it in SQLite, and :func:`activate` moves the flag
inside one transaction so two templates can never both look active. Deleting or
resetting simply leaves the pair with no row, which is the "Default" entry: the
bot then renders the built-in text from ``message_kinds``.

The renderer here is a deliberate mirror of ``bot/message_templates.py`` — the
services cannot import each other — and
``.github/tests/test_message_template_contract.py`` renders the same bodies
through both and fails when they disagree.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import message_catalog

#: ``@name``: kept identical to the bot's ``PLACEHOLDER`` regex.
PLACEHOLDER = re.compile(r"(?<![A-Za-z0-9_])@([a-z_][a-z0-9_]*)")

#: The formatting markers a template may use, longest first.
MARKERS: tuple[str, ...] = ("**", "__", "~~", "||", "*", "_", "~", "`")
_MARKER_PAIRS: dict[str, str] = {
    "**": "**",
    "__": "__",
    "~~": "~~",
    "||": "||",
    "*": "*",
    "_": "_",
    "~": "~",
    "`": "`",
}

#: MarkdownV2 specials, the same set ``bot/i18n.py`` escapes.
MD_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"

ELLIPSIS = "…"

MAX_NAME = 80
MAX_BODY = 4000

#: Stored rows are English, like the rest of the panel's data.
STATUS_NAMES = {0: "inactive", 1: "active"}


class TemplateError(ValueError):
    """A body that cannot be rendered (unknown placeholder, empty, blank)."""


# ---------------------------------------------------------------------------
# Escaping and inspection
# ---------------------------------------------------------------------------

def escape_md(text: object) -> str:
    """Escape MarkdownV2 specials — mirrors ``bot/i18n.escape_md``."""
    return "".join(f"\\{char}" if char in MD_SPECIAL else char for char in str(text))


def placeholders(body: object) -> tuple[str, ...]:
    """Every ``@name`` a body mentions, in first-use order, without duplicates."""
    seen: list[str] = []
    for name in PLACEHOLDER.findall(str(body or "")):
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def unknown_placeholders(body: object, allowed: tuple[str, ...] | list[str]) -> list[str]:
    """The placeholders a body uses that its message kind does not offer."""
    known = set(allowed)
    return [name for name in placeholders(body) if name not in known]


# ---------------------------------------------------------------------------
# Rendering (mirrors bot/message_templates.py)
# ---------------------------------------------------------------------------

def _segments(line: str, values: Mapping[str, object]) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    text: list[str] = []

    def flush() -> None:
        if text:
            segments.append(("text", "".join(text)))
            text.clear()

    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char == "\\" and index + 1 < length:
            text.append(line[index + 1])
            index += 2
            continue
        if char == "@":
            match = PLACEHOLDER.match(line, index)
            if match is not None:
                flush()
                segments.append(("value", str(values.get(match.group(1)) or "")))
                index = match.end()
                continue
        marker = next((m for m in MARKERS if line.startswith(m, index)), None)
        if marker is not None:
            flush()
            segments.append(("marker", marker))
            index += len(marker)
            continue
        text.append(char)
        index += 1
    flush()
    return segments


def _balanced(segments: list[tuple[str, str]]) -> bool:
    expected: list[str] = []
    for kind, payload in segments:
        if kind != "marker":
            continue
        closing = _MARKER_PAIRS[payload]
        if expected and expected[-1] == closing:
            expected.pop()
        else:
            expected.append(payload)
    return not expected


def _render_line(line: str, values: Mapping[str, object]) -> tuple[str, bool]:
    segments = _segments(line, values)
    formatting = _balanced(segments)
    out: list[str] = []
    for kind, payload in segments:
        if kind == "value":
            out.append(escape_md(payload))
        elif kind == "marker" and formatting:
            out.append(payload)
        else:
            out.append(escape_md(payload))
    return "".join(out), formatting


def _line_is_empty(line: str, values: Mapping[str, object]) -> bool:
    names = placeholders(line)
    return bool(names) and all(not str(values.get(name) or "").strip() for name in names)


def _tidy(text: str) -> str:
    lines = [line.rstrip() for line in text.split("\n")]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    while kept and not kept[0]:
        kept.pop(0)
    return "\n".join(kept)


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    cut = text[: budget - 1].rstrip()
    while cut.endswith("\\"):
        cut = cut[:-1].rstrip()
    return cut + ELLIPSIS


def render_body(body: object, values: Mapping[str, object], *, media: str = "text") -> str:
    """Render a template body to a MarkdownV2 string Telegram will accept."""
    raw = str(body or "")
    if not raw.strip():
        raise TemplateError("The template is empty.")

    unknown = unknown_placeholders(raw, tuple(values))
    if unknown:
        names = ", ".join(f"@{name}" for name in unknown)
        raise TemplateError(f"Unknown placeholder: {names}.")

    rendered: list[str] = []
    all_balanced = True
    for line in raw.split("\n"):
        if _line_is_empty(line, values):
            rendered.append("")
            continue
        text, balanced = _render_line(line, values)
        all_balanced = all_balanced and balanced
        rendered.append(text)

    if not all_balanced:
        rendered = [
            escape_md(
                PLACEHOLDER.sub(lambda match: str(values.get(match.group(1)) or ""), line)
            )
            for line in raw.split("\n")
            if not _line_is_empty(line, values)
        ]

    text = _tidy("\n".join(rendered))
    if not text.strip():
        raise TemplateError("The template rendered to nothing.")
    return _truncate(text, message_catalog.BUDGETS.get(media, message_catalog.BUDGETS["text"]))


def render_plain(body: object, values: Mapping[str, object]) -> str:
    """Render without Markdown, for a plain-text emitter."""
    raw = str(body or "")
    if not raw.strip():
        raise TemplateError("The template is empty.")
    unknown = unknown_placeholders(raw, tuple(values))
    if unknown:
        names = ", ".join(f"@{name}" for name in unknown)
        raise TemplateError(f"Unknown placeholder: {names}.")
    lines = [
        PLACEHOLDER.sub(lambda match: str(values.get(match.group(1)) or ""), line)
        for line in raw.split("\n")
        if not _line_is_empty(line, values)
    ]
    text = _tidy("\n".join(lines))
    if not text.strip():
        raise TemplateError("The template rendered to nothing.")
    return _truncate(text, message_catalog.BUDGETS["text"])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(body: object, kind_id: str, *, name: object = None) -> list[str]:
    """Everything wrong with a submitted template, in the owner's words.

    An unknown placeholder is an error (the message is not saved); a body over
    Telegram's limit is only a warning — the renderer truncates it, and the
    panel shows the count next to the editor.
    """
    kind = message_catalog.get(kind_id)
    if kind is None:
        return [f"Unknown message kind: {kind_id}."]

    errors: list[str] = []
    text = str(body or "")
    if not text.strip():
        errors.append("A template cannot be empty.")
    if name is not None and not str(name or "").strip():
        errors.append("Give the template a name.")

    unknown = unknown_placeholders(text, kind.placeholders)
    if unknown:
        names = ", ".join(f"@{name}" for name in unknown)
        offered = ", ".join(f"@{name}" for name in kind.placeholders) or "none"
        errors.append(
            f"Unknown placeholder: {names}. This message offers: {offered}."
        )
    return errors


def warnings(body: object, kind_id: str) -> list[str]:
    """Non-blocking notes: the body is longer than Telegram will take."""
    kind = message_catalog.get(kind_id)
    if kind is None:
        return []
    length = len(str(body or ""))
    if length > kind.limit:
        return [
            f"{length} characters is over Telegram's limit of {kind.limit} "
            f"for a {kind.media}; the bot will cut it."
        ]
    if length > kind.budget:
        return [
            f"{length} characters is close to Telegram's limit of {kind.limit}; "
            "the bot will cut the tail."
        ]
    return []


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def render(kind_id: str, lang: object, body: object) -> str:
    """Render one body with the kind's realistic sample values."""
    kind = message_catalog.get(kind_id)
    if kind is None:
        raise TemplateError(f"Unknown message kind: {kind_id}.")
    return render_body(body, kind.samples(), media=kind.media)


def preview(kind_id: str, lang: object, body: object | None = None) -> dict[str, object]:
    """Render ``body`` (or the built-in default when ``None``) for the panel.

    Never raises: a broken body comes back as ``error`` next to an empty
    ``text``, which is what the preview pane shows instead of a half-rendered
    message.
    """
    kind = message_catalog.get(kind_id)
    if kind is None:
        return {
            "text": "",
            "error": f"Unknown message kind: {kind_id}.",
            "length": 0,
            "limit": 0,
            "over": False,
        }
    source = kind.template(lang) if body is None else str(body)
    try:
        text = render_body(source, kind.samples(), media=kind.media)
        error = None
    except TemplateError as exc:
        text, error = "", str(exc)
    return {
        "text": text,
        "error": error,
        "length": len(text),
        "limit": kind.limit,
        "over": len(text) > kind.limit,
    }


# ---------------------------------------------------------------------------
# Stored rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Template:
    """One stored body. ``active`` is the one the bot renders for its pair."""

    id: int
    kind: str
    lang: str
    name: str
    body: str
    active: bool
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> Template:
        try:
            identifier = int(str(row.get("id") or 0))
        except (TypeError, ValueError):
            identifier = 0
        return cls(
            id=identifier,
            kind=str(row.get("kind") or ""),
            lang=str(row.get("lang") or ""),
            name=str(row.get("name") or ""),
            body=str(row.get("body") or ""),
            active=bool(row.get("is_active")),
            updated_at=str(row.get("updated_at") or ""),
        )


def normalise_lang(value: object) -> str:
    """``ru``/``RU``/``ru_RU`` -> a language the catalogue has, else ``en``."""
    name = str(value or "").strip().lower().replace("-", "_")
    if name in message_catalog.LANGUAGES:
        return name
    base = name.split("_", 1)[0]
    return base if base in message_catalog.LANGUAGES else message_catalog.LANGUAGES[0]


def active_bodies(rows: Sequence[Mapping[str, object]]) -> dict[str, dict[str, str]]:
    """``{kind: {lang: body}}`` for the bot, from the rows flagged active."""
    payload: dict[str, dict[str, str]] = {}
    for row in rows:
        if not row.get("is_active"):
            continue
        kind = str(row.get("kind") or "")
        body = str(row.get("body") or "")
        if kind not in message_catalog.BY_ID or not body.strip():
            # A row for a retired kind (or an emptied body) is ignored rather
            # than shipped: the bot would only fall back to the default anyway.
            continue
        payload.setdefault(kind, {})[normalise_lang(row.get("lang"))] = body
    return payload


def coverage(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """How much of the catalogue has a custom body, for the panel's header."""
    customised = {str(row.get("kind")) for row in rows if row.get("is_active")}
    total = len(message_catalog.KINDS)
    return {
        "covered": len([kind for kind in customised if kind in message_catalog.BY_ID]),
        "total": total,
    }


__all__ = [
    "MAX_BODY",
    "MAX_NAME",
    "MD_SPECIAL",
    "PLACEHOLDER",
    "Template",
    "TemplateError",
    "active_bodies",
    "coverage",
    "escape_md",
    "normalise_lang",
    "placeholders",
    "preview",
    "render",
    "render_body",
    "render_plain",
    "unknown_placeholders",
    "validate",
    "warnings",
]
