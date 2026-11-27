"""Every message the bot can send, as one registry.

A *message kind* is one thing the user reads: the welcome screen, the preview
card, the progress caption, a refusal, a button label. Each kind declares

* a stable ``id`` — the key the panel stores a custom template under;
* a ``category`` — how the panel groups the list (onboarding, navigation,
  download, errors, privileged, buttons);
* how it is sent (``text`` for ``send_message``, ``caption`` for a photo/video
  caption — that is what decides Telegram's 4096/1024 limit);
* the placeholders it offers, and the default template in every language.

The default templates are built from ``i18n`` at import time, so they cannot
drift from the words the bot already speaks. They are **read-only**: nothing
here writes to them, the panel only ever shows them (as the "Default" entry and
as the text a new custom template starts from). A custom template lives in the
panel's database; the bot asks for it in ``GET /api/bot-settings`` and renders it
in ``message_templates``, falling back to the default when it is unusable.

``docs/message-templates.md`` and the panel's catalogue are generated from this
registry; ``.github/tests/test_message_template_contract.py`` fails when the two
services disagree about a kind or a placeholder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import i18n
from i18n import t

#: The languages every default template is written in.
LANGUAGES: tuple[str, ...] = i18n.LANGUAGES

#: The order the panel lists the categories in.
CATEGORIES: tuple[str, ...] = (
    "onboarding",
    "navigation",
    "download",
    "errors",
    "privileged",
    "buttons",
)

#: ``{value}`` in an i18n line is ``@value`` in a template.
_I18N_HOLE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass(frozen=True)
class Kind:
    """One user-visible message."""

    id: str
    category: str
    media: str
    placeholders: tuple[str, ...]
    i18n_keys: tuple[str, ...]
    templates: dict[str, str]

    def template(self, lang: object) -> str:
        """The built-in body in ``lang`` (falling back to the default language)."""
        return self.templates.get(
            i18n.normalise(lang), self.templates[i18n.DEFAULT_LANGUAGE]
        )


def _line(key: str, lang: str, **values: object) -> str:
    """One catalogued line as a template: ``{limit}`` becomes ``@limit``."""
    return _I18N_HOLE.sub(lambda match: f"@{match.group(1)}", t(key, lang, **values))


def _templates(build) -> dict[str, str]:
    """``{lang: build(lang)}`` for every language the bot can render."""
    return {lang: build(lang) for lang in LANGUAGES}


def _one_line(key: str) -> dict[str, str]:
    return _templates(lambda lang: _line(key, lang))


# ---------------------------------------------------------------------------
# Composite screens (assembled from several catalogued lines)
# ---------------------------------------------------------------------------

def _welcome(lang: str) -> str:
    return "\n\n".join(
        t(key, lang) for key in ("welcome.title", "welcome.body", "welcome.platforms")
    )


def _heading(key: str, lang: str) -> str:
    return f"*{t(key, lang)}*"


def _help(lang: str) -> str:
    return "\n\n".join(
        (
            _heading("help.title", lang),
            "\n".join(t(key, lang) for key in ("help.step1", "help.step2", "help.step3", "help.step4")),
            _heading("help.shortcuts", lang),
            "\n".join(t(key, lang) for key in ("help.mp3", "help.mp4", "help.best")),
            _heading("help.preferences", lang),
            "\n".join(
                t(key, lang)
                for key in ("help.setquality", "help.setformat", "help.settings")
            ),
            _heading("help.other", lang),
            "\n".join(t(key, lang) for key in ("help.platforms", "help.stats", "help.cancel")),
            t("help.footer", lang),
        )
    )


def _platforms(lang: str) -> str:
    return f"{t('platforms.header', lang)}\n\n@platforms\n  {t('platforms.more', lang)}"


def _settings(lang: str) -> str:
    rows = "\n".join(
        (
            f"  {t('settings.quality', lang)}: @quality",
            f"  {t('settings.format', lang)}: @format",
            f"  {t('settings.language', lang)}: @language",
            f"  {t('settings.mode', lang)}: @mode",
        )
    )
    return "\n\n".join(
        (
            t("settings.title", lang),
            rows,
            t("settings.change", lang)
            + "\n  "
            + t("settings.setquality", lang)
            + "\n  "
            + t("settings.setformat", lang),
            t("settings.language_hint", lang),
        )
    )


def _stats(lang: str) -> str:
    rows = "\n".join(
        (
            f"  {t('stats.uptime', lang)}: @uptime",
            f"  {t('stats.downloads', lang)}: @downloads",
            f"  {t('stats.errors', lang)}: @errors",
            f"  {t('stats.cached', lang)}: @cached",
            f"  {t('stats.disk', lang)}: @disk",
            f"  {t('stats.sessions', lang)}: @sessions",
        )
    )
    return f"{t('stats.title', lang)}\n\n{rows}"


def _card(lang: str) -> str:
    lines = (
        "*@title*",
        "",
        "@channel",
        f"{t('card.duration', lang)}: @duration",
        f"{t('card.followers', lang)}: @followers",
        f"@views {t('card.views', lang)} · @likes {t('card.likes', lang)}"
        f" · @comments {t('card.comments', lang)}",
        f"{t('card.category', lang)}: @category",
        f"{t('card.uploaded', lang)}: @uploaded",
    )
    return "\n".join(lines)


def _progress(lang: str) -> str:
    return "*@title*\n@stage\n@bar"


def _queued(lang: str) -> str:
    return "*@title*\n@position\n@hint"


def _done(lang: str) -> str:
    return "*@title*\n@quality · @size · @duration\n@source"


def _error(lang: str) -> str:
    return "*@title*\n@error\n@retry_hint"


def _transcode_ask(lang: str) -> str:
    return "*@title*\n@question\n@note"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

KINDS: tuple[Kind, ...] = (
    # -- onboarding -------------------------------------------------------
    Kind(
        id="language.prompt",
        category="onboarding",
        media="text",
        placeholders=("languages",),
        i18n_keys=("language.prompt",),
        templates=_one_line("language.prompt"),
    ),
    Kind(
        id="welcome",
        category="onboarding",
        media="text",
        placeholders=("languages", "platforms", "version"),
        i18n_keys=("welcome.title", "welcome.body", "welcome.platforms"),
        templates=_templates(_welcome),
    ),
    Kind(
        id="help",
        category="onboarding",
        media="text",
        placeholders=("platforms", "version"),
        i18n_keys=(
            "help.title", "help.step1", "help.step2", "help.step3", "help.step4",
            "help.shortcuts", "help.mp3", "help.mp4", "help.best",
            "help.preferences", "help.setquality", "help.setformat", "help.settings",
            "help.other", "help.platforms", "help.stats", "help.cancel", "help.footer",
        ),
        templates=_templates(_help),
    ),
    Kind(
        id="platforms",
        category="onboarding",
        media="text",
        placeholders=("platforms", "count"),
        i18n_keys=("platforms.header", "platforms.more"),
        templates=_templates(_platforms),
    ),
    Kind(
        id="language.unknown",
        category="onboarding",
        media="text",
        placeholders=(),
        i18n_keys=("language.unknown",),
        templates=_one_line("language.unknown"),
    ),
    Kind(
        id="mode.armed",
        category="navigation",
        media="text",
        placeholders=("mode",),
        i18n_keys=("mode.armed",),
        templates=_one_line("mode.armed"),
    ),
    Kind(
        id="settings",
        category="navigation",
        media="text",
        placeholders=("quality", "format", "language", "mode"),
        i18n_keys=(
            "settings.title", "settings.quality", "settings.format",
            "settings.language", "settings.mode", "settings.mode_none",
            "settings.change", "settings.setquality", "settings.setformat",
            "settings.language_hint",
        ),
        templates=_templates(_settings),
    ),
    # -- preferences (still "navigation": a sub-screen of /settings) -------
    Kind(
        id="quality.usage",
        category="navigation", media="text", placeholders=(),
        i18n_keys=("quality.usage",), templates=_one_line("quality.usage"),
    ),
    Kind(
        id="quality.invalid",
        category="navigation", media="text", placeholders=("options",),
        i18n_keys=("quality.invalid",), templates=_one_line("quality.invalid"),
    ),
    Kind(
        id="quality.saved",
        category="navigation", media="text", placeholders=("quality",),
        i18n_keys=("quality.saved",), templates=_one_line("quality.saved"),
    ),
    Kind(
        id="format.usage",
        category="navigation", media="text", placeholders=(),
        i18n_keys=("format.usage",), templates=_one_line("format.usage"),
    ),
    Kind(
        id="format.invalid",
        category="navigation", media="text", placeholders=("options",),
        i18n_keys=("format.invalid",), templates=_one_line("format.invalid"),
    ),
    Kind(
        id="format.saved",
        category="navigation", media="text", placeholders=("format",),
        i18n_keys=("format.saved",), templates=_one_line("format.saved"),
    ),
    Kind(
        id="mp3.usage",
        category="navigation", media="text", placeholders=(),
        i18n_keys=("mp3.usage",), templates=_one_line("mp3.usage"),
    ),
    Kind(
        id="mp4.usage",
        category="navigation", media="text", placeholders=(),
        i18n_keys=("mp4.usage",), templates=_one_line("mp4.usage"),
    ),
    Kind(
        id="mp3.started",
        category="navigation", media="text", placeholders=(),
        i18n_keys=("mp3.started",), templates=_one_line("mp3.started"),
    ),
    Kind(
        id="mp4.started",
        category="navigation", media="text", placeholders=(),
        i18n_keys=("mp4.started",), templates=_one_line("mp4.started"),
    ),
    # -- download ---------------------------------------------------------
    Kind(
        id="stage.searching",
        category="download", media="text", placeholders=(),
        i18n_keys=("stage.searching",), templates=_one_line("stage.searching"),
    ),
    Kind(
        id="stage.starting",
        category="download", media="text", placeholders=(),
        i18n_keys=("stage.starting",), templates=_one_line("stage.starting"),
    ),
    Kind(
        id="card",
        category="download",
        media="caption",
        placeholders=(
            "title", "uploader", "channel", "followers", "duration", "views",
            "likes", "comments", "category", "uploaded", "source", "url", "meta",
        ),
        i18n_keys=(
            "card.untitled", "card.duration", "card.followers", "card.category",
            "card.uploaded", "card.views", "card.likes", "card.comments",
            "card.via",
        ),
        templates=_templates(_card),
    ),
    Kind(
        id="progress",
        category="download",
        media="caption",
        placeholders=(
            "title", "stage", "quality", "bar", "percent", "size", "speed",
            "eta", "elapsed", "facts",
        ),
        i18n_keys=("stage.starting", "stage.downloading", "stage.processing", "stage.uploading", "stage.converting", "progress.eta", "progress.elapsed"),
        templates=_templates(_progress),
    ),
    Kind(
        id="queued",
        category="download",
        media="caption",
        placeholders=("title", "position", "wait", "hint"),
        i18n_keys=("queue.hint", "queue.head", "queue.head_number", "queue.estimated"),
        templates=_templates(_queued),
    ),
    Kind(
        id="done",
        category="download",
        media="caption",
        placeholders=("title", "quality", "size", "duration", "source"),
        i18n_keys=("card.download", "card.via", "transcode.document", "limit.quality_selected"),
        templates=_templates(_done),
    ),
    Kind(
        id="transcode.ask",
        category="download",
        media="caption",
        placeholders=("title", "question", "quality", "codec", "note"),
        i18n_keys=("transcode.ask", "transcode.note"),
        templates=_templates(_transcode_ask),
    ),
    Kind(
        id="limit.all_too_big",
        category="download", media="text", placeholders=("limit",),
        i18n_keys=("limit.all_too_big",), templates=_one_line("limit.all_too_big"),
    ),
    Kind(
        id="limit.video_too_big",
        category="download", media="text", placeholders=("limit",),
        i18n_keys=("limit.video_too_big",), templates=_one_line("limit.video_too_big"),
    ),
    Kind(
        id="limit.audio_too_big",
        category="download", media="text", placeholders=("limit",),
        i18n_keys=("limit.audio_too_big",), templates=_one_line("limit.audio_too_big"),
    ),
    Kind(
        id="transcode.document",
        category="download", media="text", placeholders=(),
        i18n_keys=("transcode.document",), templates=_one_line("transcode.document"),
    ),
    Kind(
        id="limit.quality_selected",
        category="download", media="text", placeholders=("quality", "skipped"),
        i18n_keys=("limit.quality_selected",),
        templates=_one_line("limit.quality_selected"),
    ),
    # -- errors -----------------------------------------------------------
    Kind(
        id="error",
        category="errors",
        media="caption",
        placeholders=("title", "error", "retry_hint", "url", "limit"),
        i18n_keys=("download.failed", "download.unknown_error", "download.retry_hint"),
        templates=_templates(_error),
    ),
    Kind(
        id="download.session_expired",
        category="errors", media="text", placeholders=(),
        i18n_keys=("download.session_expired",),
        templates=_one_line("download.session_expired"),
    ),
    Kind(
        id="download.already",
        category="errors", media="text", placeholders=(),
        i18n_keys=("download.already",), templates=_one_line("download.already"),
    ),
    Kind(
        id="download.unexpected",
        category="errors", media="text", placeholders=(),
        i18n_keys=("download.unexpected",), templates=_one_line("download.unexpected"),
    ),
    Kind(
        id="download.unknown_error",
        category="errors", media="text", placeholders=(),
        i18n_keys=("download.unknown_error",),
        templates=_one_line("download.unknown_error"),
    ),
    Kind(
        id="download.file_missing",
        category="errors", media="text", placeholders=(),
        i18n_keys=("download.file_missing",),
        templates=_one_line("download.file_missing"),
    ),
    Kind(
        id="download.upload_failed",
        category="errors", media="text", placeholders=(),
        i18n_keys=("download.upload_failed",),
        templates=_one_line("download.upload_failed"),
    ),
    Kind(
        id="download.timed_out",
        category="errors", media="text", placeholders=(),
        i18n_keys=("download.timed_out",), templates=_one_line("download.timed_out"),
    ),
    Kind(
        id="download.busy_user",
        category="errors", media="text", placeholders=("when",),
        i18n_keys=("download.busy_user",), templates=_one_line("download.busy_user"),
    ),
    Kind(
        id="download.busy_service",
        category="errors", media="text", placeholders=("when",),
        i18n_keys=("download.busy_service",),
        templates=_one_line("download.busy_service"),
    ),
    Kind(
        id="engine.down",
        category="errors", media="text", placeholders=(),
        i18n_keys=("engine.down",), templates=_one_line("engine.down"),
    ),
    Kind(
        id="engine.info_failed",
        category="errors", media="text", placeholders=("error",),
        i18n_keys=("engine.info_failed",), templates=_one_line("engine.info_failed"),
    ),
    Kind(
        id="engine.error",
        category="errors", media="text", placeholders=("error",),
        i18n_keys=("engine.error",), templates=_one_line("engine.error"),
    ),
    Kind(
        id="card.render_failed",
        category="errors", media="text", placeholders=(),
        i18n_keys=("card.render_failed",), templates=_one_line("card.render_failed"),
    ),
    # A streaming link (Spotify, Apple Music, Deezer) that cannot be replaced by
    # a downloadable recording. One kind per reason the engine can give, so the
    # owner can reword "not found" without touching "this needs an API key".
    Kind(
        id="track.not_found",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.not_found",), templates=_one_line("track.not_found"),
    ),
    Kind(
        id="track.unsupported_type",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.unsupported_type",),
        templates=_one_line("track.unsupported_type"),
    ),
    Kind(
        id="track.unsupported_service",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.unsupported_service",),
        templates=_one_line("track.unsupported_service"),
    ),
    Kind(
        id="track.unsupported_site",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.unsupported_site",),
        templates=_one_line("track.unsupported_site"),
    ),
    Kind(
        id="track.drm_only",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.drm_only",), templates=_one_line("track.drm_only"),
    ),
    Kind(
        id="track.metadata_failed",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.metadata_failed",),
        templates=_one_line("track.metadata_failed"),
    ),
    Kind(
        id="track.disabled",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.disabled",), templates=_one_line("track.disabled"),
    ),
    Kind(
        id="track.no_candidates",
        category="errors", media="text", placeholders=(),
        i18n_keys=("track.no_candidates",),
        templates=_one_line("track.no_candidates"),
    ),
    Kind(
        id="intake.unsafe",
        category="errors", media="text", placeholders=(),
        i18n_keys=("intake.unsafe",), templates=_one_line("intake.unsafe"),
    ),
    Kind(
        id="intake.too_many",
        category="errors", media="text", placeholders=("limit",),
        i18n_keys=("intake.too_many",), templates=_one_line("intake.too_many"),
    ),
    # -- privileged -------------------------------------------------------
    Kind(
        id="stats",
        category="privileged",
        media="text",
        placeholders=(
            "uptime", "downloads", "errors", "cached", "disk", "sessions", "version",
        ),
        i18n_keys=(
            "stats.title", "stats.uptime", "stats.downloads", "stats.errors",
            "stats.cached", "stats.disk", "stats.sessions",
        ),
        templates=_templates(_stats),
    ),
    # -- buttons ----------------------------------------------------------
    Kind(
        id="nav.back",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("nav.back",), templates=_one_line("nav.back"),
    ),
    Kind(
        id="button.help",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("button.help",), templates=_one_line("button.help"),
    ),
    Kind(
        id="button.platforms",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("button.platforms",), templates=_one_line("button.platforms"),
    ),
    Kind(
        id="button.settings",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("button.settings",), templates=_one_line("button.settings"),
    ),
    Kind(
        id="button.stats",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("button.stats",), templates=_one_line("button.stats"),
    ),
    Kind(
        id="button.mp3",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("button.mp3",), templates=_one_line("button.mp3"),
    ),
    Kind(
        id="button.mp4",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("button.mp4",), templates=_one_line("button.mp4"),
    ),
    Kind(
        id="card.best_quality",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("card.best_quality",), templates=_one_line("card.best_quality"),
    ),
    Kind(
        id="transcode.send_as_file",
        category="buttons", media="text", placeholders=(),
        i18n_keys=("transcode.send_as_file",),
        templates=_one_line("transcode.send_as_file"),
    ),
    Kind(
        id="transcode.convert",
        category="buttons", media="text", placeholders=("eta",),
        i18n_keys=("transcode.convert", "transcode.convert_no_eta"),
        templates=_one_line("transcode.convert"),
    ),
)

BY_ID: dict[str, Kind] = {kind.id: kind for kind in KINDS}

#: Every placeholder name any kind offers, for a typo check in the tests.
ALL_PLACEHOLDERS: frozenset[str] = frozenset(
    name for kind in KINDS for name in kind.placeholders
)


def get(kind_id: str) -> Kind | None:
    """The kind with this id, or ``None`` (an old panel may name a retired one)."""
    return BY_ID.get(str(kind_id))


def template(kind_id: str, lang: object) -> str | None:
    """The built-in body of one kind, or ``None`` for an unknown id."""
    kind = get(kind_id)
    return kind.template(lang) if kind is not None else None


def media(kind_id: str) -> str:
    """``text`` or ``caption`` for one kind (defaults to ``text``)."""
    kind = get(kind_id)
    return kind.media if kind is not None else "text"


def placeholders_for(kind_id: str) -> tuple[str, ...]:
    """The placeholders one kind offers (empty for an unknown id)."""
    kind = get(kind_id)
    return kind.placeholders if kind is not None else ()


def by_category() -> dict[str, list[Kind]]:
    """The registry grouped the way the panel lists it."""
    grouped: dict[str, list[Kind]] = {category: [] for category in CATEGORIES}
    for kind in KINDS:
        grouped.setdefault(kind.category, []).append(kind)
    return grouped


__all__ = [
    "ALL_PLACEHOLDERS",
    "BY_ID",
    "CATEGORIES",
    "KINDS",
    "LANGUAGES",
    "Kind",
    "by_category",
    "get",
    "media",
    "placeholders_for",
    "template",
]
