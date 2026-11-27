"""Render one message from a template body, safely, in MarkdownV2.

The owner edits message text in the panel; the bot renders it here. A template
is plain text with ``@name`` placeholders::

    *@title*

    Duration: @duration · @quality

Three rules make an edited template safe to send, and all three are enforced in
this module rather than at the call sites:

* **values are escaped, the template is not.** ``@title`` becomes
  ``escape_md(title)`` so a video called ``Tom & Jerry (1940)`` cannot break the
  message; the prose around it is the owner's, and stays as written.
* **a template that cannot be MarkdownV2 falls back to plain text.** Balanced
  ``*bold*``/``_italic_``/``~strike~``/``||spoiler||``/`` `code` `` markers are
  kept; a template with an unpaired marker is escaped as a whole, so Telegram
  renders it as literal text instead of rejecting the message.
* **an unusable template never sends nothing.** An unknown ``@placeholder``, an
  empty body or a body that renders to whitespace raises :class:`TemplateError`
  and the caller sends the built-in default instead.

The renderer is pure: it knows nothing about Telegram, the panel or a clock, so
the panel's preview and the bot's message are produced by the same code.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

import i18n

# ``@name``: the placeholder syntax. The look-behind keeps an email address in
# the prose from being mistaken for one (``user@example.com`` stays as typed).
PLACEHOLDER = re.compile(r"(?<![A-Za-z0-9_])@([a-z_][a-z0-9_]*)")

# Telegram limits. The budget leaves headroom: the API counts UTF-16 units and
# an escaped character is two characters of the wire format.
LIMITS: dict[str, int] = {"text": 4096, "caption": 1024}
BUDGETS: dict[str, int] = {"text": 4000, "caption": 1000}

# The formatting markers a template may use. Longest first, so ``**`` is not
# read as two ``*``.
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

ELLIPSIS = "…"


class TemplateError(ValueError):
    """The body cannot be rendered (unknown placeholder, empty, blank)."""


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

def placeholders(body: object) -> tuple[str, ...]:
    """Every ``@name`` a body mentions, in first-use order, without duplicates."""
    seen: list[str] = []
    for name in PLACEHOLDER.findall(str(body or "")):
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def unknown_placeholders(body: object, allowed: tuple[str, ...] | list[str]) -> list[str]:
    """The placeholders a body uses that this message kind does not offer."""
    known = set(allowed)
    return [name for name in placeholders(body) if name not in known]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _segments(line: str, values: Mapping[str, object]) -> list[tuple[str, str]]:
    """Split one line into ``("text"|"marker"|"value", payload)`` segments.

    ``\\*`` in a template is a literal asterisk and never a marker, which is how
    an owner writes a character that would otherwise change the formatting.
    """
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
    """Whether the markers pair up (an unescaped ``*`` needs a second one)."""
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
    """Render one line; the flag says whether its markers survived the check."""
    segments = _segments(line, values)
    formatting = _balanced(segments)
    out: list[str] = []
    for kind, payload in segments:
        if kind == "value":
            out.append(i18n.escape_md(payload))
        elif kind == "marker" and formatting:
            out.append(payload)
        else:
            out.append(i18n.escape_md(payload))
    return "".join(out), formatting


def _line_is_empty(line: str, values: Mapping[str, object]) -> bool:
    """A line whose every placeholder is empty disappears, prose and all.

    That is the rule the metadata lines of the preview card rely on: no duration
    means no "Duration:" line, not a label with a hole after it.
    """
    names = placeholders(line)
    return bool(names) and all(not str(values.get(name) or "").strip() for name in names)


def _tidy(text: str) -> str:
    """Drop the blank lines an empty directive left behind."""
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
    """Cut to the budget without leaving a dangling escape or marker."""
    if len(text) <= budget:
        return text
    cut = text[: budget - 1].rstrip()
    while cut.endswith("\\"):
        cut = cut[:-1].rstrip()
    return cut + ELLIPSIS


def render_body(body: object, values: Mapping[str, object], *, media: str = "text") -> str:
    """Render a template body to a MarkdownV2 string Telegram will accept.

    Raises :class:`TemplateError` for a body that cannot be used; the callers
    treat that as "send the built-in default" rather than as a failure.
    """
    raw = str(body or "")
    if not raw.strip():
        raise TemplateError("The template is empty.")

    allowed = tuple(values)
    unknown = unknown_placeholders(raw, allowed)
    if unknown:
        names = ", ".join(f"@{name}" for name in unknown)
        raise TemplateError(f"Unknown placeholder: {names}.")

    rendered: list[str] = []
    # If any line holds an unpaired marker the whole body goes plain: mixing
    # "escape the marker here, keep it there" would be unreadable and is what
    # Telegram rejects with "can't parse entities".
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
            i18n.escape_md(
                PLACEHOLDER.sub(
                    lambda match: str(values.get(match.group(1)) or ""), line
                )
            )
            for line in raw.split("\n")
            if not _line_is_empty(line, values)
        ]

    text = _tidy("\n".join(rendered))
    if not text.strip():
        raise TemplateError("The template rendered to nothing.")
    return _truncate(text, BUDGETS.get(media, BUDGETS["text"]))


def render_plain(body: object, values: Mapping[str, object]) -> str:
    """Render without any Markdown: for ``query.answer`` toasts.

    Values are inserted as-is because a toast has no ``parse_mode``; the panel
    warns about the limit, and the bot never sends an empty toast.
    """
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
    return _truncate(text, BUDGETS["text"])


# ---------------------------------------------------------------------------
# The live template for one message kind
# ---------------------------------------------------------------------------

def custom_body(kind: str, lang: object) -> str | None:
    """The active custom body for ``(kind, language)``, or ``None``.

    Reads the answer the bot already caches (``bot_settings``): no request per
    message, and a panel that is down simply means "no custom bodies", which is
    the same state as "nothing was ever saved".
    """
    import bot_settings  # local: bot_settings imports i18n, not this module

    settings = bot_settings.cached()
    if settings is None:
        return None
    body = settings.custom_template(kind, lang)
    return body or None


def render(
    kind: str,
    lang: object,
    values: Mapping[str, object],
    default: str,
    *,
    media: str = "text",
) -> str:
    """The body for ``(kind, language)``, custom when usable, else ``default``."""
    body = custom_body(kind, lang)
    if body is None:
        return default
    try:
        return render_body(body, values, media=media)
    except TemplateError as error:
        import logging

        logging.getLogger(__name__).debug(
            "Template %s/%s is unusable (%s); sending the default",
            kind,
            i18n.normalise(lang),
            error,
        )
        return default


def render_line(kind: str, lang: object, default: str, **values: object) -> str:
    """A one-line catalogued message, custom template first.

    Used by ``handlers.texts.md`` so every one-line message the bot sends is
    editable without touching its call site.
    """
    return render(kind, lang, values, default)


def render_plain_line(
    kind: str, lang: object, default: str, **values: object
) -> str:
    """The same for a plain-text emitter (a toast, a caption edit without mode)."""
    body = custom_body(kind, lang)
    if body is None:
        return default
    try:
        return render_plain(body, values)
    except TemplateError:
        return default


__all__ = [
    "BUDGETS",
    "LIMITS",
    "TemplateError",
    "custom_body",
    "placeholders",
    "render",
    "render_body",
    "render_line",
    "render_plain",
    "render_plain_line",
    "unknown_placeholders",
]
