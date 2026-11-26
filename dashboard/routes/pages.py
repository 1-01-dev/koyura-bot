"""Page routes for the Koyura admin dashboard."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import auth
import db
import download_limits as limits_settings

router = APIRouter()

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _render(request: Request, template: str, context: dict, status_code: int = 200) -> Response:
    """Render a template with a signed CSRF token (cookie + template variable)."""
    token = auth.existing_csrf_token(request) or auth.new_csrf_token()
    body = dict(context)
    body["csrf_token"] = token
    body["request"] = request
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


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, saved: int = 0) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

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
                        {"name": item.name, "size": stat.st_size, "is_dir": item.is_dir()}
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

    stored = await db.get_settings(prefix=limits_settings.SETTING_PREFIX)
    context = {
        "user": user,
        "active": "admin",
        "files": files,
        "downloads_bytes": downloads_bytes,
        "max_disk_mb": max_disk_mb,
        "disk_pct": disk_pct,
    }
    context.update(_limits_context(stored))
    context["limits_notice"] = (
        "Limits saved and applied to the engine."
        if saved and not context.get("limits_error")
        else None
    )
    return _render(request, "admin.html", context)


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
        return _render(
            request,
            "admin.html",
            await _admin_error_context(
                user, "Your session expired. Reload the page and try again."
            ),
            status_code=403,
        )

    raw = {
        key: str(form.get(key, "")).strip()
        for key in limits_settings.FIELD_NAMES
    }
    limits, errors = limits_settings.Limits.parse(raw)
    if errors:
        context = await _admin_error_context(user, " ".join(errors))
        # Echo the submitted values so the form keeps what was typed.
        context["limits"] = {key: raw.get(key, "") for key in limits_settings.FIELD_NAMES}
        for field in context["limit_fields"]:
            field["value"] = context["limits"][field["name"]]
        return _render(request, "admin.html", context, status_code=400)

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
        context = await _admin_error_context(
            user, f"Saved, but the engine did not apply them: {detail}"
        )
        return _render(request, "admin.html", context, status_code=200)

    return RedirectResponse(url="/admin?saved=1", status_code=303)


async def _admin_error_context(user: str, message: str) -> dict:
    """Shared context for an admin page that has to show a message."""
    stored = await db.get_settings(prefix=limits_settings.SETTING_PREFIX)
    context = {"user": user, "active": "admin", "files": [], "downloads_bytes": 0,
               "max_disk_mb": 0, "disk_pct": 0}
    context.update(_limits_context(stored, error=message))
    return context


def _pretty_value(value: float) -> str:
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:g}"
