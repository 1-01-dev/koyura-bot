"""Page routes for the Koyura admin dashboard."""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from starlette.datastructures import FormData
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import auth
import bot_languages
import db
import download_limits as limits_settings
import egress
import message_catalog
import message_styles
import message_templates
import service_catalog
import service_cookies
import subscriptions

router = APIRouter()

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _render(request: Request, template: str, context: dict, status_code: int = 200) -> Response:
    """Render a template with a signed CSRF token (cookie + template variable)."""
    token = auth.existing_csrf_token(request) or auth.new_csrf_token()
    body = dict(context)
    body["csrf_token"] = token
    body["request"] = request
    # Pages outside /admin render the sidebar too; an empty key keeps the
    # Admin group closed instead of raising on the missing variable.
    body.setdefault("admin_section", "")
    html = templates.env.get_template(template).render(**body)
    response = HTMLResponse(content=html, status_code=status_code)
    auth.set_csrf_cookie(response, token, request)
    return response


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if auth.get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return _render(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request) -> Response:
    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    submitted_csrf = str(form.get(auth.CSRF_FORM_FIELD, ""))

    key = auth.client_key(request)
    if auth.login_rate_limiter.is_blocked(key):
        retry_after = auth.login_rate_limiter.retry_after(key)
        return _render(
            request,
            "login.html",
            {"error": f"Too many failed attempts. Try again in {retry_after} seconds."},
            status_code=429,
        )

    if not auth.verify_csrf(request, submitted_csrf):
        return _render(
            request,
            "login.html",
            {"error": "Your session expired. Please try again."},
            status_code=403,
        )

    if not auth.verify_credentials(username, password):
        auth.login_rate_limiter.record_failure(key)
        # Never log the attempted password.
        return _render(
            request,
            "login.html",
            {"error": "Invalid credentials"},
            status_code=401,
        )

    auth.login_rate_limiter.reset(key)
    response = RedirectResponse(url="/", status_code=303)
    auth.create_session_cookie(response, auth.admin_username(), request)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    form = await request.form()
    submitted_csrf = str(form.get(auth.CSRF_FORM_FIELD, ""))
    response = RedirectResponse(url="/login", status_code=303)
    if not auth.verify_csrf(request, submitted_csrf):
        return response
    auth.clear_session_cookie(response)
    response.delete_cookie(auth.CSRF_COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Protected pages
# ---------------------------------------------------------------------------

def _require_user(request: Request) -> str | None:
    return auth.get_current_user(request)


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _render(request, "dashboard.html", {"user": user, "active": "dashboard"})


@router.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    page: int = 1,
    platform: str | None = None,
    status: str | None = None,
    user_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date: str | None = None,
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # The KPI cells link to /history?date=today, so honour that shortcut here
    # instead of dropping the parameter on the floor.
    if date == "today":
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_from = date_to = today

    page = max(1, page)
    data = await db.get_downloads_page(
        page=page,
        platform=platform,
        status=status,
        user=user_filter,
        date_from=date_from,
        date_to=date_to,
    )
    return _render(
        request,
        "history.html",
        {
            "user": user,
            "active": "history",
            "data": data,
            "platform": platform,
            "status": status,
            "user_filter": user_filter,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@router.get("/errors", response_class=HTMLResponse)
async def errors_page(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    errors = await db.get_error_downloads(date_from=date_from, date_to=date_to)
    return _render(
        request, "errors.html", {"user": user, "active": "errors", "errors": errors}
    )


def _limits_context(stored: dict[str, str], error: str | None = None,
                     notice: str | None = None) -> dict:
    """Template context for the Download limits section.

    The form always shows a complete, valid set of values: what is stored when
    it validates, the environment defaults otherwise. A rejected submission is
    passed back as ``submitted`` so nothing the admin typed is lost.
    """
    limits = limits_settings.Limits.from_env()
    if stored:
        limits = limits_settings.Limits.from_row(
            {key.removeprefix(limits_settings.SETTING_PREFIX): value
             for key, value in stored.items()},
            base=limits,
        )
    hardware = limits_settings.detect_hardware()
    recommended = limits_settings.recommended_preset(hardware)
    return {
        "limits": limits.as_form(),
        "limit_fields": [
            {
                "name": key,
                "env": env_name,
                "label": label,
                "unit": unit,
                "min": _pretty_bound(low),
                "max": _pretty_bound(high),
                "value": limits.as_form()[key],
                "hint": limits_settings.FIELD_HINTS.get(key, ""),
            }
            for env_name, key, label, unit, low, high in limits_settings.FIELDS
        ],
        "presets": [preset.as_dict() for preset in limits_settings.PRESETS],
        "recommended_key": recommended.key,
        "hardware": {
            "cpu": hardware.cpu,
            # An undetectable memory size is shown as unknown, never as "0 GB".
            "memory_gb": hardware.memory_label if hardware.memory_gb > 0 else "unknown RAM",
        },
        "limits_error": error,
        "limits_notice": notice,
        "limits_saved": bool(stored),
    }


def _pretty_bound(value: float) -> str:
    return str(int(value)) if float(value) == int(value) else f"{value:g}"


# ---------------------------------------------------------------------------
# Admin: cookies (the Storage page)
# ---------------------------------------------------------------------------

def _cookies_notice(
    *,
    added: int = 0,
    replaced: int = 0,
    deleted: int = 0,
    moved: str | None = None,
    checked: str | None = None,
    service: str | None = None,
) -> str | None:
    """The one green line a redirect may carry (``?added=1`` → a sentence)."""
    name = service or ""
    if added:
        return f"Cookie file added{' for ' + name if name else ''}."
    if replaced:
        return "Cookie file replaced."
    if deleted:
        return f"Deleted {deleted} cookie file{'s' if deleted != 1 else ''}."
    if moved:
        return f"Order updated ({moved})."
    if checked:
        return f"Check finished: {checked}."
    return None


async def _cookies_context() -> dict:
    """Every stored service, its jars in priority order, and the suggestions.

    The template only ever sees names, sizes, positions and the last verdict;
    a stored path is not part of the context, so a template cannot print one.
    """
    rows = await db.list_service_cookies()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["service"]), []).append(row)

    services = []
    for name in sorted(grouped):
        files = []
        for row in grouped[name]:
            label, badge = service_cookies.check_label(row.get("check_status"))
            files.append({
                "id": int(row["id"]),
                "original_name": row.get("original_name") or "(no name)",
                "size": service_cookies.human_size(row.get("size")),
                "position": int(row.get("position") or 0) + 1,
                "checked_at": row.get("checked_at"),
                "check_label": label,
                "check_class": badge,
                "is_first": int(row.get("position") or 0) == 0,
                "is_last": int(row.get("position") or 0) == len(grouped[name]) - 1,
            })
        # The table's "last check" column is the newest verdict of the service:
        # checking jar #2 tells the operator more than the stale #1 result.
        checked_rows = [row for row in grouped[name] if row.get("checked_at")]
        newest = max(checked_rows, key=lambda row: str(row.get("checked_at")), default=None)
        label, badge = service_cookies.check_label(
            newest.get("check_status") if newest else None
        )
        services.append({
            "name": name,
            "files": files,
            "count": len(files),
            "check_label": label,
            "check_class": badge,
            "checked_at": newest.get("checked_at") if newest else None,
            "probe_url": service_cookies.default_probe_url(name),
        })

    return {
        "services": services,
        "cookies_total": len(rows),
        "service_count": len(services),
        "suggestions": service_catalog.suggestions(limit=24),
        "all_services": service_catalog.service_names(),
        "max_cookie_kb": service_cookies.MAX_COOKIE_BYTES // 1024,
    }


def _languages_context(stored: dict[str, str], error: str | None = None,
                       notice: str | None = None) -> dict:
    """Template context for the Bot languages section.

    The form always shows a valid pair: what is stored when it parses, the
    defaults otherwise. A rejected submission arrives as ``error`` and the
    template echoes the submitted values, so nothing the admin clicked is lost.
    """
    settings = bot_languages.BotLanguages.from_row(stored)
    return {
        "bot_languages": settings.as_form(),
        "language_options": bot_languages.options(),
        "languages_error": error,
        "languages_notice": notice,
    }


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

TEMPLATE_DEFAULT_KIND = "welcome"


def _template_defaults(kind: str | None, lang: str | None) -> tuple[str, str]:
    """A kind and a language that exist, whatever the query string said."""
    chosen_kind = message_catalog.get(kind) if kind else None
    kind_id = chosen_kind.id if chosen_kind is not None else TEMPLATE_DEFAULT_KIND
    return kind_id, message_templates.normalise_lang(lang)


async def _templates_context(
    *,
    kind: str | None = None,
    lang: str | None = None,
    template_id: int | None = None,
    draft: dict | None = None,
    error: str | None = None,
    notice: str | None = None,
    style_notice: str | None = None,
) -> dict:
    """Everything the Message templates section renders.

    One function for the GET and every POST that re-renders the page, so a
    rejected submission shows the same kind list, language tabs and reference
    table the successful page does. ``draft`` carries ``name``/``body`` that
    were typed but not saved, so nothing is lost to a validation error.
    """
    kind_id, language = _template_defaults(kind, lang)
    selected = message_catalog.get(kind_id)
    assert selected is not None  # _template_defaults guarantees it

    rows = await db.list_message_templates(kind=kind_id, lang=language)
    active = next((row for row in rows if row.get("is_active")), None)

    # ``template`` in the query string picks the row being edited. No value
    # opens the active custom template (what the operator usually wants);
    # ``0`` opens the built-in default, which is how a new body starts.
    chosen = None
    if template_id is not None and int(template_id) > 0:
        chosen = next(
            (row for row in rows if int(row["id"]) == int(template_id)), None
        )
        if chosen is None:
            chosen = await db.get_message_template(int(template_id))
            if chosen is not None and (
                str(chosen.get("kind")) != kind_id or str(chosen.get("lang")) != language
            ):
                chosen = None
    elif template_id is None and active is not None:
        chosen = active

    draft = draft or {}
    editing = chosen is not None or bool(draft)
    body = str(draft.get("body") or (chosen or {}).get("body") or selected.template(language))
    name = str(draft.get("name") or (chosen or {}).get("name") or "")
    preview = message_templates.preview(
        kind_id, language, body if editing else None
    )

    active_rows = await db.active_message_templates()
    covered = {
        str(row.get("kind"))
        for row in active_rows
        if str(row.get("lang")) == language and row.get("is_active")
    }
    groups = []
    for group in message_catalog.categories():
        members = [
            {
                "id": member.id,
                "title": member.title,
                "custom": member.id in covered,
            }
            for member in group["kinds"]
        ]
        groups.append({"id": group["id"], "label": group["label"], "kinds": members})

    context = {
        "template_groups": groups,
        "template_kind": {
            "id": selected.id,
            "title": selected.title,
            "media": selected.media,
            "limit": selected.limit,
            "budget": selected.budget,
            "placeholders": selected.placeholder_objects(),
            "default": selected.template(language),
        },
        "template_lang": language,
        "template_languages": list(message_catalog.LANGUAGES),
        "template_rows": [message_templates.Template.from_row(row) for row in rows],
        "template_active_id": int(active["id"]) if active is not None else None,
        "template_editor": {
            "id": int(chosen["id"]) if chosen is not None else None,
            "name": name,
            "body": body,
            "is_default": not editing,
        },
        "template_preview": preview,
        "template_coverage": message_templates.coverage(active_rows),
        "template_covered_lang": len(covered),
        "template_total": len(message_catalog.KINDS),
        "template_error": error,
        "template_notice": notice,
    }
    # The style row shares the panel's scope (``styles``, ``style_active``, …)
    # rather than a nested key, so the partial and the tests name one thing.
    context.update(await _style_context(style_notice=style_notice))
    return context


async def _style_context(*, style_notice: str | None = None) -> dict:
    """The row of style buttons above the editor, and which one is active.

    The active style is one ``settings`` row, ``templates.style``. Rows written
    before this feature existed have no value, and they are not "custom" — they
    are simply a set of manual edits — so the stored value is normalised, not
    trusted: an unknown string would otherwise leave every button unlit.
    """
    stored = await db.get_settings(prefix=message_styles.SETTING_KEY)
    raw = stored.get(message_styles.SETTING_KEY)
    active = message_styles.normalise_style(raw)
    return {
        "styles": [
            {
                "id": preset.id,
                "label": preset.label,
                "tagline": preset.tagline,
                "installed": message_styles.installed()[preset.id],
            }
            for preset in message_styles.STYLES
        ],
        "style_default": {"id": message_styles.DEFAULT_STYLE, "label": "Default"},
        "style_custom": {"id": message_styles.CUSTOM_STYLE, "label": "Custom"},
        "style_active": active,
        "style_active_label": message_styles.label_of(active),
        "style_notice": style_notice,
    }


def _templates_redirect(
    kind: str,
    lang: str,
    *,
    template_id: int | None = None,
    notice: str | None = None,
    style_notice: str | None = None,
) -> RedirectResponse:
    """Back to the section with the same kind, language and template selected."""
    query = f"kind={kind}&lang={lang}"
    if template_id is not None:
        query += f"&template={int(template_id)}"
    if notice:
        query += f"&templates_notice={notice}"
    if style_notice:
        query += f"&templates_style_notice={style_notice}"
    return RedirectResponse(url=f"/admin/bot?{query}", status_code=303)


async def _templates_error(
    request: Request, user: str, message: str, **context
) -> Response:
    """Re-render /admin/bot with a template error and the submitted draft."""
    return await _bot_page(
        request, user, template_error=message, status_code=400, **context
    )


# ---------------------------------------------------------------------------
# Admin: one page per section
# ---------------------------------------------------------------------------

#: The sub-pages under /admin, in sidebar order. ``base.html`` and the in-page
#: tab strip both loop over this, so a new section is one row here plus a
#: template — the navigation cannot drift from the routes.
ADMIN_SECTIONS: tuple[dict[str, str], ...] = (
    {"key": "cookies", "label": "Cookies", "href": "/admin/cookies"},
    {"key": "channels", "label": "Channels", "href": "/admin/channels"},
    {"key": "proxy", "label": "Proxy", "href": "/admin/proxy"},
    {"key": "limits", "label": "Limits", "href": "/admin/limits"},
    {"key": "bot", "label": "Bot settings", "href": "/admin/bot"},
    {"key": "system", "label": "System", "href": "/admin/system"},
)

ADMIN_SECTION_TEMPLATES = {
    "cookies": "admin_cookies.html",
    "channels": "admin_channels.html",
    "proxy": "admin_proxy.html",
    "limits": "admin_limits.html",
    "bot": "admin_bot.html",
    "system": "admin_system.html",
}

#: A section that was renamed keeps its old path as a redirect. The owner has
#: seen ``/admin/storage`` in a chat message and a bookmark, and a 404 there
#: would read as "the feature is gone" — so an old path lands on the current
#: page with a permanent redirect.
ADMIN_SECTION_ALIASES: dict[str, str] = {
    "storage": "cookies",
}

#: Where a bare ``/admin`` lands. System is the page that only reads: the first
#: question a fresh visit asks is "is this box healthy", and the answer must not
#: depend on a settings row or on a half-finished section.
ADMIN_HOME = "system"

#: Which query string belongs to which section. Old links and old redirects
#: have exactly two shapes — ``?saved=1`` and ``?kind=…&lang=…`` — and both must
#: keep working now that the page they point at has moved.
ADMIN_SECTION_PARAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("limits", ("saved",)),
    (
        "proxy",
        (
            "egress_saved",
            "wg_saved",
            "wg_removed",
            "status_refreshed",
        ),
    ),
    (
        "bot",
        (
            "languages_saved",
            "progress_saved",
            "templates_saved",
            "kind",
            "lang",
            "template",
            "templates_notice",
        ),
    ),
    (
        "channels",
        (
            "channel_added",
            "channel_deleted",
            "channel_checked",
            "channel_moved",
            "channel_toggled",
            "channels_saved",
            "channel_error",
            "channel_notice",
        ),
    ),
)

templates.env.globals["admin_links"] = ADMIN_SECTIONS


def _admin_home_for(query: Iterable[str]) -> str:
    """The section a legacy ``/admin?…`` link belongs to."""
    present = set(query)
    for section, names in ADMIN_SECTION_PARAMS:
        if present.intersection(names):
            return section
    return ADMIN_HOME


def _render_admin(
    request: Request, section: str, context: dict, status_code: int = 200
) -> Response:
    """Render one sub-page; the section drives the sidebar and the tab strip."""
    body = dict(context)
    body["active"] = "admin"
    body["admin_section"] = section
    return _render(request, ADMIN_SECTION_TEMPLATES[section], body, status_code)


def _human_size(value: int, *, gb_decimals: int = 1, kb_fallback: bool = False) -> str:
    """A byte count with the largest unit that still reads as a number.

    The three places that print a size differ only in two details — how many
    decimals a gigabyte keeps and whether a sub-megabyte value is still worth a
    fraction or is clearer as raw bytes — so they share one ladder instead of
    three near-identical branches in the templates.
    """
    if value >= 1073741824:
        return f"{value / 1073741824:.{gb_decimals}f} GB"
    if value >= 1048576:
        return f"{value / 1048576:.1f} MB"
    if kb_fallback:
        return f"{value / 1024:.1f} KB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


def _proxy_notice(
    *,
    saved: int = 0,
    wg_saved: int = 0,
    wg_removed: int = 0,
    refreshed: int = 0,
) -> str | None:
    """The one-line notice under the Proxy page's panels."""
    if saved:
        return "Egress rule saved. The engine picks it up within a minute."
    if wg_saved:
        return "WireGuard config written. The server watcher applies the tunnel."
    if wg_removed:
        return (
            "WireGuard config removed. The engine is back to a direct connection."
        )
    if refreshed:
        return "Tunnel status re-read from the server."
    return None


async def _proxy_context(
    *,
    stored: dict[str, str] | None = None,
    error: str | None = None,
    wg_error: str | None = None,
    probe: dict | None = None,
    form_values: tuple[str, str, list[str]] | None = None,
) -> dict:
    """Everything the Proxy page renders — never the proxy password.

    The settings are read from the ``egress.*`` rows, the tunnel's state from the
    watcher's ``status.json``, and both are turned into display values here: the
    form carries a masked URL (``socks5h://***:***@host:port``), so a password
    cannot reach the HTML even if a template were to print the field raw.

    ``form_values`` echoes a rejected submission as ``(mode, assignment, names)``
    so a radio button or a tick the operator just made is not lost.
    """
    rows = stored if stored is not None else await db.get_settings(
        prefix=egress.SETTING_PREFIX
    )
    settings = egress.Egress.from_row(rows)
    status = egress.read_status()
    # The multiselect offers the same catalogue the Cookies page suggests, so an
    # operator can only pick a name the engine can actually compute.
    catalogue = list(service_catalog.service_names())
    form = settings.as_form(catalogue)
    mode = settings.mode
    assignment = settings.assignment_mode
    chosen = set(settings.services)
    if form_values is not None:
        mode, assignment, typed = form_values
        if mode not in egress.MODES:
            mode = settings.mode
        if assignment not in egress.ASSIGNMENT_MODES:
            assignment = settings.assignment_mode
        chosen = {name.strip().lower() for name in typed if name.strip()}
        unknown = sorted(chosen - set(catalogue))
        form["services"] = [
            {"name": name, "checked": name in chosen} for name in catalogue
        ]
        form["services"].extend(
            {"name": name, "checked": True} for name in unknown
        )
        form["selected_count"] = len(chosen)
    form["mode"] = mode
    form["assignment_mode"] = assignment
    form["mode_options"] = egress.mode_options(mode)
    form["assignment_options"] = egress.assignment_options(assignment)
    return {
        "egress": form,
        "egress_mask": settings,
        "egress_status": status,
        "egress_error": error,
        "egress_wg_error": wg_error,
        "egress_probe": probe,
        "wg_present": egress.wireguard_present(),
        "wg_max_kb": egress.MAX_WG_BYTES // 1024,
        "egress_proxy_schemes": " · ".join(
            f"{scheme}://" for scheme in egress.PROXY_SCHEMES
        ),
        "egress_path": str(egress.wireguard_path()),
        # The current effective target, for the header line: the mask of the
        # stored URL in manual mode, the tunnel's SOCKS address in WireGuard.
        "egress_target": (
            egress.mask_proxy_url(settings.proxy_url)
            if settings.mode == "manual"
            else (status.socks_url if settings.mode == "wireguard" else "")
        ),
    }


def _system_context() -> dict:
    """Files in ``DOWNLOADS_PATH`` and the numbers the disk panels print."""
    downloads_path = Path(os.environ.get("DOWNLOADS_PATH", "/downloads"))
    files = []
    downloads_bytes = 0
    try:
        if downloads_path.exists():
            for item in sorted(downloads_path.iterdir()):
                try:
                    stat = item.stat()
                    if item.is_file():
                        downloads_bytes += stat.st_size
                    files.append(
                        {
                            "name": item.name,
                            "size": stat.st_size,
                            "size_label": _human_size(stat.st_size),
                            "is_dir": item.is_dir(),
                        }
                    )
                except OSError:
                    continue
    except OSError:
        files = []

    try:
        max_disk_mb = int(os.environ.get("CLEANUP_MAX_DISK_MB", "5000"))
    except ValueError:
        max_disk_mb = 5000
    max_disk_bytes = max_disk_mb * 1024 * 1024
    disk_pct = round(downloads_bytes / max_disk_bytes * 100, 1) if max_disk_bytes else 0
    return {
        "files": files,
        "downloads_bytes": downloads_bytes,
        # The disk summary never drops below a kilobyte; the System info row
        # keeps two decimals per gigabyte and falls back to raw bytes.
        "downloads_label": _human_size(downloads_bytes, kb_fallback=True),
        "space_label": _human_size(downloads_bytes, gb_decimals=2),
        "max_disk_mb": max_disk_mb,
        "disk_pct": disk_pct,
    }


async def _bot_context(
    *,
    languages_error: str | None = None,
    template_error: str | None = None,
    **template_kwargs,
) -> dict:
    """The two panels of /admin/bot, with at most one showing a failure.

    The third panel that used to live here — the ``Progress fields`` switches —
    is gone: which lines the live message carries is now the ``progress``
    message template, edited in the section below the languages. Old
    ``bot_progress.*`` rows may still sit in ``settings``; nothing reads them.
    """
    stored_languages = await db.get_settings(prefix=bot_languages.SETTING_PREFIX)
    context: dict = {}
    context.update(_languages_context(stored_languages, error=languages_error))
    context.update(await _templates_context(error=template_error, **template_kwargs))
    return context


async def _limits_page(
    request: Request,
    user: str,
    *,
    error: str | None = None,
    values: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """Render /admin/limits, optionally with a failure and the typed values."""
    stored = await db.get_settings(prefix=limits_settings.SETTING_PREFIX)
    context: dict = {"user": user}
    context.update(_limits_context(stored, error=error))
    if values is not None:
        # Echo the submission so the form keeps what was typed.
        context["limits"] = values
        for field in context["limit_fields"]:
            field["value"] = values.get(field["name"], "")
    return _render_admin(request, "limits", context, status_code=status_code)


async def _bot_page(
    request: Request,
    user: str,
    *,
    languages_error: str | None = None,
    template_error: str | None = None,
    languages: dict | None = None,
    status_code: int = 200,
    **template_kwargs,
) -> Response:
    """Render /admin/bot; ``languages`` echoes a rejected submission."""
    context: dict = {"user": user}
    context.update(
        await _bot_context(
            languages_error=languages_error,
            template_error=template_error,
            **template_kwargs,
        )
    )
    if languages is not None:
        context["bot_languages"] = languages
    return _render_admin(request, "bot", context, status_code=status_code)


# ---------------------------------------------------------------------------
# Admin: Channels (the subscription gate)
# ---------------------------------------------------------------------------

#: The period selector of the Channels page, in the order it is offered.
CHANNEL_PERIODS: tuple[dict[str, str], ...] = (
    {"key": "7d", "label": "7 days"},
    {"key": "30d", "label": "30 days"},
    {"key": "all", "label": "All time"},
)


def normalise_period(value: object) -> str:
    """One of ``7d``/``30d``/``all``; anything else is the default (30 days)."""
    key = str(value or "").strip().lower()
    return key if key in {entry["key"] for entry in CHANNEL_PERIODS} else "30d"


def _channels_notice(
    *,
    added: int = 0,
    deleted: int = 0,
    moved: int = 0,
    toggled: int = 0,
    checked: int = 0,
    saved: int = 0,
) -> str | None:
    """The one-line notice above the Channels table."""
    if added:
        return (
            "Channel added. Check it, and remember the bot has to be an "
            "administrator there for the subscription check to work."
        )
    if saved:
        return "Channel updated."
    if deleted:
        return "Channel removed."
    if moved:
        return "Order saved."
    if toggled:
        return "Channel switched."
    if checked:
        return "Check finished — see the verdict in the table."
    return None


def _channel_rows(rows: list[dict], gate_enabled: bool) -> list[dict]:
    """The table rows: what each channel *is*, and whether it can work.

    ``state`` is the one thing the template branches on, and it is derived here
    rather than in Jinja so the page and the API agree about what "working"
    means:

    * ``off`` — the operator turned this channel off;
    * ``gate_off`` — the channel is fine but the master switch is off, so it is
      not in force (shown as "inactive, gate off");
    * ``no_bot`` — the bot is not an administrator, so it cannot verify anyone:
      this channel must say so in words instead of counting as working;
    * ``ready`` — enabled, switch on, bot is an administrator.
    """
    table = []
    for row in rows:
        if not row["enabled"]:
            state = "off"
        elif not gate_enabled:
            state = "gate_off"
        elif not row["bot_is_admin"]:
            state = "no_bot"
        else:
            state = "ready"
        table.append(
            {
                **row,
                "state": state,
                "state_label": {
                    "off": "off",
                    "gate_off": "inactive — gate off",
                    "no_bot": "bot is not an administrator",
                    "ready": "working",
                }[state],
                "state_class": {
                    "off": "badge",
                    "gate_off": "badge",
                    "no_bot": "badge badge-danger",
                    "ready": "badge badge-success",
                }[state],
                "check_label": subscriptions.check_error_label(row["check_error"]),
                "check_class": (
                    "badge badge-success"
                    if row["bot_is_admin"]
                    else "badge badge-danger"
                ),
                "open_link": row["link"]
                or subscriptions.channel_link(row["username"]),
            }
        )
    return table


async def _channels_context(
    *,
    period: object = None,
    error: str | None = None,
    notice: str | None = None,
) -> dict:
    """Everything the Channels page renders: the switch, the table and the KPIs."""
    current_period = normalise_period(period)
    gate_enabled = await db.subscriptions_enabled()
    rows = await db.list_required_channels()
    stats = await db.get_subscription_stats(current_period)
    working = [row for row in rows if row["enabled"] and row["bot_is_admin"]]
    return {
        "channels_enabled": gate_enabled,
        "channels": _channel_rows(rows, gate_enabled),
        "channels_working": len(working),
        "channels_total": len(rows),
        "gate_live": bool(gate_enabled and working),
        "channel_period": current_period,
        "channel_periods": [
            {**entry, "active": entry["key"] == current_period}
            for entry in CHANNEL_PERIODS
        ],
        "channel_stats": stats,
        "channel_error": error,
        "channel_notice": notice,
    }


async def _channels_page(
    request: Request,
    user: str,
    *,
    error: str | None = None,
    notice: str | None = None,
    period: object = None,
    status_code: int = 200,
) -> Response:
    """Render /admin/channels with an optional error and notice."""
    context: dict = {"user": user}
    context.update(
        await _channels_context(period=period, error=error, notice=notice)
    )
    return _render_admin(request, "channels", context, status_code=status_code)


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> Response:
    """Send ``/admin`` to a sub-page, keeping an old link's query string.

    The single-page URLs are still out there — a bookmark, the docs, the
    redirect a POST used to answer with — so the parameters pick the section
    instead of being dropped. The session is checked first: an anonymous visit
    still goes to the login form, not to a redirect chain.
    """
    if not _require_user(request):
        return RedirectResponse(url="/login", status_code=303)
    target = f"/admin/{_admin_home_for(request.query_params.keys())}"
    if request.query_params:
        target = f"{target}?{request.query_params}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/admin/{section}", response_class=HTMLResponse)
async def admin_section_page(
    request: Request,
    section: str,
    saved: int = 0,
    languages_saved: int = 0,
    progress_saved: int = 0,
    templates_saved: int = 0,
    kind: str | None = None,
    lang: str | None = None,
    template: int | None = None,
    templates_notice: str | None = None,
    templates_style_notice: str | None = None,
    added: int = 0,
    replaced: int = 0,
    deleted: int = 0,
    moved: str | None = None,
    checked: str | None = None,
    service: str | None = None,
    cookies_error: str | None = None,
    egress_saved: int = 0,
    wg_saved: int = 0,
    wg_removed: int = 0,
    status_refreshed: int = 0,
    channel_period: str | None = None,
    channel_error: str | None = None,
    channel_notice: str | None = None,
    channel_added: int = 0,
    channel_deleted: int = 0,
    channel_moved: int = 0,
    channel_toggled: int = 0,
    channel_checked: int = 0,
    channels_saved: int = 0,
) -> Response:
    """One page per section; the query string only carries a notice.

    Every section accepts the same parameters so an old link keeps its
    ``?saved=1`` when it is redirected here, even if only one section reads it.
    """
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    # A renamed section keeps its old path alive: the owner has booked
    # /admin/storage, and a 404 would read as "the page is gone".
    alias = ADMIN_SECTION_ALIASES.get(section)
    if alias is not None:
        target = f"/admin/{alias}"
        if request.query_params:
            target = f"{target}?{request.query_params}"
        return RedirectResponse(url=target, status_code=301)
    if section not in ADMIN_SECTION_TEMPLATES:
        return RedirectResponse(url=f"/admin/{ADMIN_HOME}", status_code=303)

    context: dict = {"user": user}

    if section == "system":
        context.update(_system_context())
        return _render_admin(request, section, context)

    if section == "cookies":
        context.update(await _cookies_context())
        context["cookies_notice"] = _cookies_notice(
            added=added, replaced=replaced, deleted=deleted,
            moved=moved, checked=checked, service=service,
        )
        context["cookies_error"] = cookies_error
        return _render_admin(request, section, context)

    if section == "channels":
        context.update(
            await _channels_context(
                period=channel_period,
                error=channel_error,
                notice=channel_notice or _channels_notice(
                    added=channel_added,
                    deleted=channel_deleted,
                    moved=channel_moved,
                    toggled=channel_toggled,
                    checked=channel_checked,
                    saved=channels_saved,
                ),
            )
        )
        return _render_admin(request, section, context)

    if section == "proxy":
        context.update(await _proxy_context())
        # Only a *success* travels in the query string. A failure is rendered
        # by its own POST, because the value that failed (a proxy URL with a
        # password in it) must never end up in an access log or the browser's
        # history.
        context["egress_notice"] = _proxy_notice(
            saved=egress_saved,
            wg_saved=wg_saved,
            wg_removed=wg_removed,
            refreshed=status_refreshed,
        )
        return _render_admin(request, section, context)

    if section == "limits":
        context.update(_limits_context(
            await db.get_settings(prefix=limits_settings.SETTING_PREFIX)
        ))
        context["limits_notice"] = (
            "Limits saved and applied to the engine."
            if saved and not context.get("limits_error")
            else None
        )
        return _render_admin(request, section, context)

    context.update(await _bot_context(
        kind=kind,
        lang=lang,
        template_id=template,
        notice=templates_notice,
        style_notice=templates_style_notice,
    ))
    context["languages_notice"] = (
        "Languages saved. The bot picks them up within a minute."
        if languages_saved and not context.get("languages_error")
        else None
    )
    if progress_saved:
        # The Progress fields section is retired; an old redirect still points
        # here, so the notice says where the setting went instead of pretending
        # a form was saved.
        context["templates_notice"] = (
            "Progress is set by the Message templates now — the `progress` body. "
            "Nothing else to save."
        )
    if templates_saved and not context.get("template_error"):
        context["template_notice"] = (
            "Template saved and activated. The bot picks it up within a minute."
        )
    return _render_admin(request, section, context)


@router.post("/admin/limits")
async def save_limits(request: Request) -> Response:
    """Validate, store and apply the download limits.

    On a validation error the same page is rendered with status 400, the
    message, and the values that were submitted — nothing typed is lost.
    """
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    submitted_csrf = str(form.get(auth.CSRF_FORM_FIELD, ""))
    if not auth.verify_csrf(request, submitted_csrf):
        return await _limits_page(
            request,
            user,
            error="Your session expired. Reload the page and try again.",
            status_code=403,
        )

    raw = {
        key: str(form.get(key, "")).strip()
        for key in limits_settings.FIELD_NAMES
    }
    limits, errors = limits_settings.Limits.parse(raw)
    if errors:
        return await _limits_page(
            request, user, error=" ".join(errors), values=raw, status_code=400
        )

    stored = await db.get_settings(prefix=limits_settings.SETTING_PREFIX)
    before = limits_settings.Limits.from_env()
    if stored:
        before = limits_settings.Limits.from_row(
            {key.removeprefix(limits_settings.SETTING_PREFIX): value
             for key, value in stored.items()},
            base=before,
        )

    await db.save_settings({
        f"{limits_settings.SETTING_PREFIX}{key}": _pretty_value(limits.as_dict()[key])
        for key in limits_settings.FIELD_NAMES
    })
    ok, detail = limits_settings.apply_to_engine(limits)
    # One line per save: who changed what, from which value to which.
    logging.getLogger("koyura.dashboard").info(
        "download limits updated by %s: %s (engine: %s)",
        user,
        limits_settings.describe_change(before, limits),
        "applied" if ok else f"not applied - {detail}",
    )
    if not ok:
        return await _limits_page(
            request,
            user,
            error=f"Saved, but the engine did not apply them: {detail}",
        )

    return RedirectResponse(url="/admin/limits?saved=1", status_code=303)


@router.post("/admin/languages")
async def save_languages(request: Request) -> Response:
    """Validate, store and let the bot pick up the enabled languages.

    There is nothing to push: the bot polls ``GET /api/bot-settings`` and caches
    it for under a minute, so saving here is enough. The two rules the engine of
    this page enforces are that at least one language stays on and that the
    default is one of them.
    """
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    submitted_csrf = str(form.get(auth.CSRF_FORM_FIELD, ""))
    if not auth.verify_csrf(request, submitted_csrf):
        return await _bot_page(
            request,
            user,
            languages_error="Your session expired. Reload the page and try again.",
            status_code=403,
        )

    enabled = [str(value) for value in form.getlist("languages")]
    default = str(form.get("default_language", ""))
    parsed, errors = bot_languages.parse(enabled, default)
    if parsed is None or errors:
        message = " ".join(errors) or "Could not save these languages."
        return await _bot_page(
            request,
            user,
            languages_error=message,
            status_code=400,
            # Echo the submission so the checkboxes keep what the admin clicked.
            languages={
                "languages": [code for code in enabled if code in bot_languages.LANGUAGES],
                "default_language": default,
                "options": bot_languages.options(),
            },
        )
    settings = parsed

    before = bot_languages.BotLanguages.from_row(
        await db.get_settings(prefix=bot_languages.SETTING_PREFIX)
    )
    await db.save_settings(settings.as_stored())
    # One line per save: who changed what, from which value to which.
    logging.getLogger("koyura.dashboard").info(
        "bot languages updated by %s: %s -> %s",
        user,
        before.describe(),
        settings.describe(),
    )
    return RedirectResponse(url="/admin/bot?languages_saved=1", status_code=303)


@router.post("/admin/progress")
async def save_progress(request: Request) -> Response:
    """Retired endpoint: the progress fields are a message template now.

    The section is gone, but the form is still out there in a cached page and in
    an old bookmark, so the URL keeps its answer instead of 404-ing: a signed-in
    operator is sent to the section that replaced it (`Admin → Message
    templates`, anchored at the ``progress`` body) with the CSRF check intact.
    Nothing is written to ``settings``.
    """
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    submitted_csrf = str(form.get(auth.CSRF_FORM_FIELD, ""))
    if not auth.verify_csrf(request, submitted_csrf):
        return await _bot_page(
            request,
            user,
            template_error="Your session expired. Reload the page and try again.",
            status_code=403,
        )

    logging.getLogger("koyura.dashboard").info(
        "retired /admin/progress form submitted by %s; sent to message templates",
        user,
    )
    kind, lang = _template_defaults("progress", None)
    return RedirectResponse(
        url=f"/admin/bot?kind={kind}&lang={lang}#templates-card", status_code=303
    )


# ---------------------------------------------------------------------------
# Message templates — the four actions of the section
# ---------------------------------------------------------------------------

async def _template_request(
    request: Request,
) -> tuple[str, FormData] | Response:
    """Common prologue: the session and the CSRF token, or a rendered refusal."""
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    submitted = str(form.get(auth.CSRF_FORM_FIELD, ""))
    if not auth.verify_csrf(request, submitted):
        return await _bot_page(
            request,
            user,
            template_error="Your session expired. Reload the page and try again.",
            status_code=403,
        )
    return user, form


async def _mark_style_custom() -> None:
    """A hand edit after a preset leaves the set no longer that preset.

    Called by every action that changes a stored body one pair at a time. When
    the active style is already ``default`` or ``custom`` this is a no-op: the
    first has no overrides to diverge from, and the second already says so.
    """
    stored = await db.get_settings(prefix=message_styles.SETTING_KEY)
    current = message_styles.normalise_style(
        stored.get(message_styles.SETTING_KEY)
    )
    if current in (message_styles.DEFAULT_STYLE, message_styles.CUSTOM_STYLE):
        return
    await db.save_settings({message_styles.SETTING_KEY: message_styles.CUSTOM_STYLE})


@router.post("/admin/templates/save")
async def save_message_template(request: Request) -> Response:
    """Validate and store a body, then make it the active one for its pair.

    An unknown placeholder (and an empty body or a missing name) is refused with
    the editor still holding what was typed — the message is not saved.
    """
    prologue = await _template_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    kind = str(form.get("kind") or "")
    lang = message_templates.normalise_lang(form.get("lang"))
    name = str(form.get("name") or "").strip()
    body = str(form.get("body") or "")
    template_id = str(form.get("template_id") or "").strip()

    errors = message_templates.validate(body, kind, name=name)
    if len(name) > message_templates.MAX_NAME:
        errors.append(
            f"Keep the name under {message_templates.MAX_NAME} characters."
        )
    if errors:
        return await _templates_error(
            request,
            user,
            " ".join(errors),
            kind=kind,
            lang=lang,
            template_id=int(template_id) if template_id.isdigit() else None,
            draft={"name": name, "body": body},
        )

    if template_id.isdigit():
        await db.update_message_template(int(template_id), name=name, body=body)
        await db.activate_message_template(int(template_id))
        saved_id = int(template_id)
    else:
        saved_id = await db.create_message_template(
            kind=kind, lang=lang, name=name, body=body, activate=True
        )
    await _mark_style_custom()

    logging.getLogger("koyura.dashboard").info(
        "message template saved: %s/%s #%s (%d chars)",
        kind,
        lang,
        saved_id,
        len(body),
    )
    return _templates_redirect(kind, lang, template_id=saved_id)


@router.post("/admin/templates/activate")
async def activate_message_template(request: Request) -> Response:
    """Make one stored body active; the previous one stays stored but inactive."""
    prologue = await _template_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    template_id = str(form.get("template_id") or "")
    row = (
        await db.get_message_template(int(template_id))
        if template_id.isdigit()
        else None
    )
    if row is None:
        return await _templates_error(
            request, user, "That template no longer exists.", kind=None, lang=None
        )
    await db.activate_message_template(int(template_id))
    await _mark_style_custom()
    return _templates_redirect(
        str(row["kind"]), str(row["lang"]), template_id=int(template_id),
        notice="Template activated.",
    )


@router.post("/admin/templates/duplicate")
async def duplicate_message_template(request: Request) -> Response:
    """Copy a body into a new, inactive template and open it in the editor."""
    prologue = await _template_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    template_id = str(form.get("template_id") or "")
    row = (
        await db.get_message_template(int(template_id))
        if template_id.isdigit()
        else None
    )
    if row is None:
        return await _templates_error(
            request, user, "That template no longer exists.", kind=None, lang=None
        )
    name = f"{row['name']} (copy)"[: message_templates.MAX_NAME]
    new_id = await db.create_message_template(
        kind=str(row["kind"]),
        lang=str(row["lang"]),
        name=name,
        body=str(row["body"]),
        activate=False,
    )
    return _templates_redirect(
        str(row["kind"]), str(row["lang"]), template_id=new_id,
        notice="Template duplicated — edit it and save to activate.",
    )


@router.post("/admin/templates/delete")
async def delete_message_template(request: Request) -> Response:
    """Delete one stored body; the pair falls back to the built-in default."""
    prologue = await _template_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    template_id = str(form.get("template_id") or "")
    row = (
        await db.get_message_template(int(template_id))
        if template_id.isdigit()
        else None
    )
    if row is None:
        return await _templates_error(
            request, user, "That template no longer exists.", kind=None, lang=None
        )
    await db.delete_message_template(int(template_id))
    # Deleting a body is a hand edit too: the pair falls back to the default
    # while the other 55 kinds keep the preset's voice.
    await _mark_style_custom()
    return _templates_redirect(
        str(row["kind"]), str(row["lang"]), notice="Template deleted."
    )


@router.post("/admin/templates/reset")
async def reset_message_templates(request: Request) -> Response:
    """Drop every custom body for one pair: the bot renders the default again."""
    prologue = await _template_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    kind = str(form.get("kind") or "")
    lang = message_templates.normalise_lang(form.get("lang"))
    removed = await db.reset_message_templates(kind, lang)
    await _mark_style_custom()
    return _templates_redirect(
        kind,
        lang,
        notice=f"Reset to the built-in default ({removed} custom template(s) removed).",
    )


@router.post("/admin/templates/apply-style")
async def apply_template_style(request: Request) -> Response:
    """Apply one preset to every kind and language at once, or clear them all.

    ``Default`` is the same endpoint with ``style=default``: it deletes every
    stored body so the bot renders ``message_kinds`` again. Any other value is
    a preset: the file is read, every body is validated against the catalogue,
    and the accepted ones are written in one transaction. Bodies that do not
    validate are *skipped* and named in the notice — an operator who sees
    "applied 54 of 56" can fix the file, which a silent all-or-nothing failure
    would not tell them.
    """
    prologue = await _template_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    requested = str(form.get("style") or "").strip()
    kind, lang = _template_defaults(
        str(form.get("kind") or "") or None,
        str(form.get("lang") or "") or None,
    )

    if requested == message_styles.DEFAULT_STYLE:
        cleared = await db.clear_message_templates()
        await db.save_settings({message_styles.SETTING_KEY: message_styles.DEFAULT_STYLE})
        notice = message_styles.default_notice(cleared=cleared)
        logging.getLogger("koyura.dashboard").info(
            "message templates back to default (%d rows cleared by %s)", cleared, user
        )
        return _templates_redirect(kind, lang, style_notice=notice)

    try:
        plan = message_styles.plan(requested)
    except message_styles.StyleError as exc:
        return await _bot_page(
            request,
            user,
            template_error=str(exc),
            kind=kind,
            lang=lang,
            status_code=400,
        )

    names = dict.fromkeys(plan.bodies, plan.style.label)
    written = await db.replace_message_templates(plan.bodies, names)
    await db.save_settings({message_styles.SETTING_KEY: plan.style.id})
    notice = message_styles.apply_notice(plan, cleared=int(written["replaced"]))
    logging.getLogger("koyura.dashboard").info(
        "message style %s applied by %s: %d kind(s), %d skipped, %d row(s)",
        plan.style.id,
        user,
        plan.applied,
        len(plan.skipped),
        int(written["written"]),
    )
    return _templates_redirect(kind, lang, style_notice=notice)


def _pretty_value(value: float) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


# ---------------------------------------------------------------------------
# Admin: the Proxy page
# ---------------------------------------------------------------------------
#
# Four POSTs, each a plain form: save the egress rule, upload a WireGuard config,
# remove it, re-read its status, and check the manual proxy. They share one shape
# — session, CSRF, then a redirect on success — and differ in the one thing that
# can fail. A failure is rendered *in place* rather than redirected, because the
# value that failed (a proxy URL with a password) must not enter a URL.


def _proxy_redirect(**params: object) -> RedirectResponse:
    query = "&".join(
        f"{key}={value}" for key, value in params.items() if value not in (None, "", 0)
    )
    target = "/admin/proxy" + (f"?{query}" if query else "")
    return RedirectResponse(url=target, status_code=303)


async def _proxy_request(
    request: Request,
) -> tuple[str, FormData] | Response:
    """Session + CSRF for every proxy POST; a refusal is already a response."""
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    if not auth.verify_csrf(request, str(form.get(auth.CSRF_FORM_FIELD, ""))):
        context = await _proxy_context(
            error="Your session expired. Reload the page and try again."
        )
        return _render_admin(request, "proxy", {"user": user, **context}, 403)
    return user, form


@router.post("/admin/proxy")
async def save_proxy(request: Request) -> Response:
    """Store the mode, the proxy URL and the assignment rule.

    A blank proxy field keeps the stored URL: the page never renders a password
    back, so an empty field is the normal case once one is saved. The engine
    reads the result from ``GET /api/egress`` within a minute, so there is
    nothing to push.
    """
    prologue = await _proxy_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    stored = egress.Egress.from_row(
        await db.get_settings(prefix=egress.SETTING_PREFIX)
    )
    settings, errors = egress.parse_settings(
        mode=form.get("mode"),
        proxy_url=form.get("proxy_url"),
        assignment_mode=form.get("assignment_mode"),
        services=[str(value) for value in form.getlist("services")],
        stored=stored,
    )
    if settings is None or errors:
        context = await _proxy_context(error=" ".join(errors), form_values=(
            str(form.get("mode") or stored.mode),
            str(form.get("assignment_mode") or stored.assignment_mode),
            [str(value) for value in form.getlist("services")],
        ))
        return _render_admin(request, "proxy", {"user": user, **context}, 400)

    await db.save_settings(settings.as_stored())
    # One line per save: the mode, the scope and the *masked* target.
    logging.getLogger("koyura.dashboard").info(
        "egress rule updated by %s: %s -> %s",
        user, stored.describe(), settings.describe(),
    )
    return _proxy_redirect(egress_saved=1)


@router.post("/admin/proxy/wireguard")
async def upload_wireguard(request: Request) -> Response:
    """Validate and store an uploaded ``.conf`` for the server's watcher.

    The panel only writes the file: applying a tunnel needs root, and a
    ``.path`` unit watches exactly this path. Nothing here reads the config back
    or logs a byte of it — a private key is the whole point of the file.
    """
    prologue = await _proxy_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    upload = form.get("wg_file")
    filename = getattr(upload, "filename", None)
    if not upload or not filename or isinstance(upload, str):
        context = await _proxy_context(
            wg_error="Choose a WireGuard .conf file to upload."
        )
        return _render_admin(request, "proxy", {"user": user, **context}, 400)
    data = await upload.read()
    try:
        egress.write_wireguard(data)
    except egress.EgressError as error:
        context = await _proxy_context(wg_error=str(error))
        return _render_admin(request, "proxy", {"user": user, **context}, 400)

    logging.getLogger("koyura.dashboard").info(
        "WireGuard config written by %s (%d bytes)", user, len(data)
    )
    return _proxy_redirect(wg_saved=1)


@router.post("/admin/proxy/wireguard/remove")
async def remove_wireguard(request: Request) -> Response:
    """Delete the config so the watcher brings the tunnel down."""
    prologue = await _proxy_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, _form = prologue

    removed = egress.remove_wireguard()
    logging.getLogger("koyura.dashboard").info(
        "WireGuard config %s by %s", "removed" if removed else "was already absent", user
    )
    return _proxy_redirect(wg_removed=1)


@router.post("/admin/proxy/status")
async def refresh_status(request: Request) -> Response:
    """Re-read ``status.json`` and show it (the page itself is the report)."""
    prologue = await _proxy_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, _form = prologue
    return _proxy_redirect(status_refreshed=1)


@router.post("/admin/proxy/check")
async def check_proxy(request: Request) -> Response:
    """Ask ``api.ipify.org`` through the submitted (or stored) proxy.

    The answer is the external IP or the error text, and both are redacted of
    the proxy's credentials before they reach the page or the log — curl echoes
    the proxy it could not use. The result is rendered on the page rather than
    redirected, because the URL under test is a secret.
    """
    prologue = await _proxy_request(request)
    if isinstance(prologue, Response):
        return prologue
    user, form = prologue

    stored = egress.Egress.from_row(
        await db.get_settings(prefix=egress.SETTING_PREFIX)
    )
    candidate = str(form.get("proxy_url") or "").strip() or stored.proxy_url
    if not candidate:
        context = await _proxy_context(
            probe={"ok": False, "detail": "Enter a proxy URL to check first."}
        )
        return _render_admin(request, "proxy", {"user": user, **context}, 400)

    ok, detail = egress.probe_proxy(candidate)
    context = await _proxy_context(probe={"ok": ok, "detail": detail})
    return _render_admin(request, "proxy", {"user": user, **context})


# ---------------------------------------------------------------------------
# Admin: cookie actions
# ---------------------------------------------------------------------------
#
# Five POSTs, each a plain form so nothing depends on JavaScript: add a service
# (search + "+" + upload), replace one jar, delete one jar, move one jar up or
# down, and the "Check" run. They all share one prologue — session, CSRF, then a
# redirect back to the page with a one-line notice, because a browser refresh
# must not repeat an upload.

#: Where a cookie action sends the browser back to, with its notice.
def _cookies_redirect(**params: object) -> RedirectResponse:
    query = "&".join(
        f"{key}={value}" for key, value in params.items() if value not in (None, "", 0)
    )
    target = "/admin/cookies" + (f"?{query}" if query else "")
    return RedirectResponse(url=target, status_code=303)


async def _cookies_request(
    request: Request,
) -> tuple[str, FormData] | RedirectResponse:
    """Session + CSRF for every cookie POST; a refusal is already a response."""
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    submitted = str(form.get(auth.CSRF_FORM_FIELD, ""))
    if not auth.verify_csrf(request, submitted):
        return _cookies_redirect(
            cookies_error="Your session expired. Reload the page and try again."
        )
    return user, form


async def _read_upload(form: FormData) -> tuple[bytes, str]:
    """The uploaded file's bytes and its original name.

    ``python-multipart`` is a declared dependency, and a form with no file part
    is a user error, not a crash: both cases raise a :class:`CookieError` whose
    sentence is shown as-is.
    """
    upload = form.get("cookie_file")
    filename = getattr(upload, "filename", None)
    if not upload or not filename or isinstance(upload, str):
        raise service_cookies.CookieError("Choose a cookie file to upload.")
    data = await upload.read()
    # The original name is shown back, so it is trimmed and bounded; it never
    # becomes a path (the stored name is generated).
    name = str(filename).replace("\\", "/").split("/")[-1][:120] or "cookies.txt"
    return data, name


@router.post("/admin/cookies/add")
async def add_cookies(request: Request) -> Response:
    """Add a service (if needed) and its first, or next, cookie file.

    One form does both jobs because they are one decision: the search field
    picks the service, the file field supplies the jar. A service that already
    exists simply gains another jar — that is how an operator adds the second
    account whose cookies should be tried after the first fails.
    """
    prologue = await _cookies_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    try:
        service = service_cookies.sanitise_service(form.get("service"))
        data, original_name = await _read_upload(form)
        stored_name, digest, size = service_cookies.write_cookie_file(service, data)
    except service_cookies.CookieError as error:
        return _cookies_redirect(cookies_error=str(error))
    except OSError as error:
        logging.getLogger("koyura.dashboard").warning(
            "Could not store a cookie file for %s: %s", form.get("service"), error
        )
        return _cookies_redirect(
            cookies_error="Could not write the cookie file. Check the COOKIES_PATH mount."
        )

    await db.add_service_cookie(
        service=service,
        stored_name=stored_name,
        original_name=original_name,
        size=size,
        sha256=digest,
    )
    # One line per change: the service, the size, the hash prefix. Never a
    # cookie, and never a hash long enough to be mistaken for one.
    logging.getLogger("koyura.dashboard").info(
        "cookie file added for %s (%d bytes, sha256 %.12s)", service, size, digest
    )
    return _cookies_redirect(added=1, service=service)


@router.post("/admin/cookies/replace")
async def replace_cookies(request: Request) -> Response:
    """Swap the bytes behind one row, keeping its position in the order.

    A refreshed jar must not jump to the end of the list: its place in the retry
    order is the whole reason the operator arranged it. The old file is
    removed only after the new one is on disk.
    """
    prologue = await _cookies_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    raw_id = str(form.get("cookie_id") or "")
    row = (
        await db.get_service_cookie(int(raw_id))
        if raw_id.isdigit()
        else None
    )
    if row is None:
        return _cookies_redirect(cookies_error="That cookie file no longer exists.")

    try:
        data, original_name = await _read_upload(form)
        stored_name, digest, size = service_cookies.write_cookie_file(
            str(row["service"]), data
        )
    except service_cookies.CookieError as error:
        return _cookies_redirect(cookies_error=str(error))
    except OSError:
        return _cookies_redirect(
            cookies_error="Could not write the cookie file. Check the COOKIES_PATH mount."
        )

    await db.update_service_cookie_file(
        int(row["id"]),
        stored_name=stored_name,
        original_name=original_name,
        size=size,
        sha256=digest,
    )
    if stored_name != row["stored_name"]:
        service_cookies.remove_cookie_file(str(row["service"]), row["stored_name"])
    logging.getLogger("koyura.dashboard").info(
        "cookie file replaced for %s (%d bytes)", row["service"], size
    )
    return _cookies_redirect(replaced=1, service=str(row["service"]))


@router.post("/admin/cookies/delete")
async def delete_cookies(request: Request) -> Response:
    """Delete one jar from the database and from the disk."""
    prologue = await _cookies_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    raw_id = str(form.get("cookie_id") or "")
    deleted = (
        await db.delete_service_cookie(int(raw_id))
        if raw_id.isdigit()
        else None
    )
    if deleted is None:
        return _cookies_redirect(cookies_error="That cookie file no longer exists.")
    service_cookies.remove_cookie_file(str(deleted["service"]), deleted["stored_name"])
    logging.getLogger("koyura.dashboard").info(
        "cookie file deleted for %s", deleted["service"]
    )
    return _cookies_redirect(deleted=1, service=str(deleted["service"]))


@router.post("/admin/cookies/move")
async def move_cookies(request: Request) -> Response:
    """Move one jar one step up or down inside its service."""
    prologue = await _cookies_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    raw_id = str(form.get("cookie_id") or "")
    direction = str(form.get("direction") or "")
    if not raw_id.isdigit() or direction not in ("up", "down"):
        return _cookies_redirect(cookies_error="Unknown move.")
    row = await db.get_service_cookie(int(raw_id))
    if row is None:
        return _cookies_redirect(cookies_error="That cookie file no longer exists.")
    await db.move_service_cookie(int(raw_id), -1 if direction == "up" else 1)
    return _cookies_redirect(moved=direction, service=str(row["service"]))


@router.post("/admin/cookies/check")
async def check_cookies(request: Request) -> Response:
    """Run ``yt-dlp --simulate`` with one jar and store the verdict.

    The run is synchronous on purpose: it takes a couple of seconds, and the
    operator is waiting for exactly this answer. The URL is either the service's
    own site (when its name is a domain) or whatever the operator typed, which
    is why the form has a field for it — a bare extractor key has no site to
    guess. The result is stored, so the table keeps it after a reload.
    """
    prologue = await _cookies_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    raw_id = str(form.get("cookie_id") or "")
    row = (
        await db.get_service_cookie(int(raw_id))
        if raw_id.isdigit()
        else None
    )
    if row is None:
        return _cookies_redirect(cookies_error="That cookie file no longer exists.")

    url = str(form.get("test_url") or "").strip()
    if not url:
        url = service_cookies.default_probe_url(row["service"])
    if not url:
        return _cookies_redirect(
            cookies_error="Enter the URL to test this service with (for example a post page)."
        )
    try:
        service_cookies.check_probe_url(url)
    except service_cookies.CookieError as error:
        return _cookies_redirect(cookies_error=str(error))

    try:
        path = service_cookies.cookie_path(str(row["service"]), row["stored_name"])
    except service_cookies.CookieError:
        return _cookies_redirect(cookies_error="That cookie file no longer exists.")

    status, detail = service_cookies.probe_cookie(path, url)
    await db.record_cookie_check(int(row["id"]), status)
    label, _badge = service_cookies.check_label(status)
    logging.getLogger("koyura.dashboard").info(
        "cookie check for %s: %s (%s)", row["service"], status, detail[:120] or "-"
    )
    return _cookies_redirect(checked=label, service=str(row["service"]))


@router.post("/admin/cookies/delete-service")
async def delete_cookie_service(request: Request) -> Response:
    """Drop a whole service: every row, every file, and the empty directory."""
    prologue = await _cookies_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    try:
        service = service_cookies.sanitise_service(form.get("service"))
    except service_cookies.CookieError as error:
        return _cookies_redirect(cookies_error=str(error))

    removed = await db.delete_service(service)
    for row in removed:
        service_cookies.remove_cookie_file(service, row["stored_name"])
    # Files the database never learned about (a crash between the write and the
    # insert) still go: the operator asked for the service to be gone.
    directory = service_cookies.cookies_root() / service
    for orphan in service_cookies.service_files(service):
        service_cookies.remove_cookie_file(service, orphan)
    with contextlib.suppress(OSError):
        directory.rmdir()
    logging.getLogger("koyura.dashboard").info(
        "cookie service deleted: %s (%d file(s))", service, len(removed)
    )
    return _cookies_redirect(deleted=max(1, len(removed)), service=service)


# ---------------------------------------------------------------------------
# Admin: Channels — the master switch, the list, and the checks
# ---------------------------------------------------------------------------

def _channels_redirect(
    *,
    period: str | None = None,
    notice: str | None = None,
    error: str | None = None,
    **flags: object,
) -> RedirectResponse:
    """Back to /admin/channels with a notice, an error or a saved flag.

    A notice and an error never travel together: the page shows the first
    sentence that explains what happened, and two would contradict each other.
    """
    params: dict[str, object] = {"channel_period": normalise_period(period)}
    if error:
        params["channel_error"] = error
    elif notice:
        params["channel_notice"] = notice
    params.update(flags)
    query = "&".join(
        f"{key}={quote(str(value))}" for key, value in params.items() if value
    )
    return RedirectResponse(url=f"/admin/channels?{query}", status_code=303)


async def _channels_request(
    request: Request, *, period: str | None = None
) -> tuple[str, FormData] | RedirectResponse:
    """Session + CSRF for every channel POST; a refusal is already a response."""
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    form = await request.form()
    submitted = str(form.get(auth.CSRF_FORM_FIELD, ""))
    if not auth.verify_csrf(request, submitted):
        return _channels_redirect(
            period=period,
            error="Your session expired. Reload the page and try again.",
        )
    return user, form


@router.post("/admin/channels/toggle")
async def toggle_subscription_gate(request: Request) -> Response:
    """The master switch: the one setting that turns the whole gate on or off.

    Off is the default and off means "the bot behaves exactly like it did before
    this feature existed": no screen, no check, no delay. The channels stay
    stored so an operator can prepare the configuration before flipping it.
    """
    prologue = await _channels_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    enabled = str(form.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on"}
    await db.set_subscriptions_enabled(enabled)
    logging.getLogger("koyura.dashboard").info(
        "subscription gate %s", "enabled" if enabled else "disabled"
    )
    if not enabled:
        return _channels_redirect(
            notice="Subscription gate off. The bot behaves as if it did not exist."
        )

    rows = await db.list_required_channels(enabled_only=True)
    working = [row for row in rows if row["bot_is_admin"]]
    if not rows or not working:
        # Honest about what "on" means right now: with no channel the bot can
        # verify, the gate is not in force even though the switch is on.
        return _channels_redirect(
            period=None,
            notice=(
                "Subscription gate on, but no channel is working yet — add one and "
                "check it, otherwise the bot does not ask anyone to subscribe."
            ),
        )
    return _channels_redirect(
        notice="Subscription gate on. The bot asks within a minute, no restart."
    )


@router.post("/admin/channels/add")
async def add_channel(request: Request) -> Response:
    """Add a channel from ``https://t.me/<name>`` or ``@<name>``.

    Parsing, the duplicate check and the ``getChat``/``getChatMember`` probe all
    run here, so a channel the bot cannot verify is added **with its verdict**
    rather than silently counted as working: the page then says "bot is not an
    administrator" and the gate ignores it until that changes.
    """
    prologue = await _channels_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    reference = str(form.get("reference") or "").strip()
    username = subscriptions.parse_reference(reference)
    if username is None:
        return _channels_redirect(
            error=(
                "That is not a public channel link. Use https://t.me/<name> or "
                "@<name> — a private invite link cannot be checked."
            )
        )

    existing = await db.find_required_channel(username)
    if existing is not None:
        return _channels_redirect(
            error=f"@{username} is already in the list."
        )

    verdict = await subscriptions.check_channel(username)
    row = await db.add_required_channel(
        title=verdict.get("title") or "",
        username=str(verdict.get("username") or username),
        link=subscriptions.channel_link(str(verdict.get("username") or username)),
        bot_is_admin=bool(verdict.get("ok")),
        check_error=str(verdict.get("error") or ""),
    )
    logging.getLogger("koyura.dashboard").info(
        "channel added: @%s (%s)", row.get("username", username),
        row.get("check_error") or "ok",
    )
    if not verdict.get("ok"):
        return _channels_redirect(
            period=None,
            error=(
                f"@{username} added, but it does not work yet: "
                f"{subscriptions.check_error_label(verdict.get('error'))}. "
                "The bot must be an administrator of the channel, otherwise the "
                "subscription check is impossible."
            ),
        )
    return _channels_redirect(channel_added=1)


@router.post("/admin/channels/check")
async def check_channel_again(request: Request) -> Response:
    """Re-run the probe for one stored channel (``getChat`` + ``getChatMember``)."""
    prologue = await _channels_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    try:
        channel_id = int(str(form.get("channel_id") or "0"))
    except ValueError:
        return _channels_redirect(error="Unknown channel.")

    row = await db.get_required_channel(channel_id)
    if row is None:
        return _channels_redirect(error="That channel no longer exists.")

    verdict = await subscriptions.check_channel(str(row["username"]))
    await db.record_channel_check(
        channel_id,
        title=verdict.get("title") or None,
        username=str(verdict.get("username") or "") or None,
        bot_is_admin=bool(verdict.get("ok")),
        check_error=str(verdict.get("error") or ""),
    )
    if not verdict.get("ok"):
        return _channels_redirect(
            error=(
                f"@{row['username']}: "
                f"{subscriptions.check_error_label(verdict.get('error'))}. "
                "The bot must be an administrator of the channel, otherwise the "
                "subscription check is impossible."
            )
        )
    return _channels_redirect(channel_checked=1)


@router.post("/admin/channels/toggle-channel")
async def toggle_channel(request: Request) -> Response:
    """Switch one channel on or off (the row stays, the gate stops using it)."""
    prologue = await _channels_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    try:
        channel_id = int(str(form.get("channel_id") or "0"))
    except ValueError:
        return _channels_redirect(error="Unknown channel.")
    enabled = str(form.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on"}

    row = await db.get_required_channel(channel_id)
    if row is None:
        return _channels_redirect(error="That channel no longer exists.")
    if enabled and not row["bot_is_admin"]:
        # Enabling a channel the bot cannot verify would make the gate
        # impossible to pass; refuse it and say why.
        return _channels_redirect(
            error=(
                f"@{row['username']} cannot be enabled: "
                f"{subscriptions.check_error_label(row['check_error'])}. "
                "The bot must be an administrator of the channel."
            )
        )
    await db.set_required_channel_enabled(channel_id, enabled)
    return _channels_redirect(channel_toggled=1)


@router.post("/admin/channels/move")
async def move_channel(request: Request) -> Response:
    """Move one channel up or down in the list the bot shows."""
    prologue = await _channels_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    try:
        channel_id = int(str(form.get("channel_id") or "0"))
    except ValueError:
        return _channels_redirect(error="Unknown channel.")
    direction = str(form.get("direction") or "").strip().lower()
    delta = -1 if direction == "up" else 1
    if not await db.move_required_channel(channel_id, delta):
        return _channels_redirect(error="That channel no longer exists.")
    return _channels_redirect(channel_moved=1)


@router.post("/admin/channels/delete")
async def delete_channel(request: Request) -> Response:
    """Delete one channel (its history rows stay: they are a record)."""
    prologue = await _channels_request(request)
    if isinstance(prologue, Response):
        return prologue
    _user, form = prologue

    try:
        channel_id = int(str(form.get("channel_id") or "0"))
    except ValueError:
        return _channels_redirect(error="Unknown channel.")
    row = await db.delete_required_channel(channel_id)
    if row is None:
        return _channels_redirect(error="That channel no longer exists.")
    return _channels_redirect(channel_deleted=1)
