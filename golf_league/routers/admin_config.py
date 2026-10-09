"""Verified-admin managed configuration form (R-DEPLOYMENT allowlist only).

GET shows the eight allowlisted settings: the value saved for the next start
and whether it differs from the running value. Secret values are never
rendered. POST validates and atomically writes only changed keys to the
managed file. Nothing here applies a value to the running process; every
managed setting is restart-required and the application never restarts itself.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from golf_league.config import get_settings
from golf_league.managed_config import (
    MANAGED_KEYS,
    MANAGED_KEYS_BY_NAME,
    read_managed_values,
    validate_value,
    write_managed_values,
)
from golf_league.security import (
    SESSION_COOKIE_NAME,
    generate_csrf_token,
    require_admin,
    validate_csrf,
)

logger = logging.getLogger(__name__)

router = APIRouter()

CONFIG_PATH = "/admin/config"
SECRET_CLEAR_SUFFIX = "__clear"
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_FORM_FIELDS = (
    {"csrf_token"}
    | set(MANAGED_KEYS_BY_NAME)
    | {key.name + SECRET_CLEAR_SUFFIX for key in MANAGED_KEYS if key.is_secret}
)


def _settings(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


def _running_value(settings, field: str) -> str:
    value = getattr(settings, field, "")
    return "" if value is None else str(value)


def _rows(settings) -> list[dict]:
    """One row per allowlisted key; secret rows carry no value at all."""
    saved = read_managed_values(settings.managed_env_path)
    rows = []
    for key in MANAGED_KEYS:
        running = _running_value(settings, key.field)
        pending = saved.get(key.name, running)
        rows.append({
            "key": key,
            "value": "" if key.is_secret else pending,
            "is_set": bool(pending),
            "restart_required": pending != running,
        })
    return rows


def _render(request: Request, *, errors: dict[str, str] | None = None,
            saved: bool = False, status_code: int = 200) -> Response:
    settings = _settings(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/config.html",
        {
            "rows": _rows(settings),
            "errors": errors or {},
            "saved": saved,
            "clear_suffix": SECRET_CLEAR_SUFFIX,
            "csrf_token": generate_csrf_token(request.cookies.get(SESSION_COOKIE_NAME, "")),
        },
        status_code=status_code,
        headers=PRIVATE_HEADERS,
    )


def _submitted_value(key, form) -> tuple[str | None, str | None]:
    """Return `(new_value, error)`; `new_value` None means leave unchanged.

    An absent field is unchanged. A blank secret keeps the stored value;
    only its clear control empties it, and a value plus clear is an error.
    """
    if key.name not in form:
        return None, None
    raw = form.get(key.name)
    if not isinstance(raw, str):
        return None, f"{key.label} is not valid."
    if key.is_secret:
        clear = form.get(key.name + SECRET_CLEAR_SUFFIX) is not None
        if clear and raw:
            return None, f"Enter a new {key.label.lower()} or clear it, not both."
        if clear:
            return "", None
        if not raw:
            return None, None
    new_value, error = validate_value(key, raw)
    return (None, error) if error else (new_value, None)


@router.get(CONFIG_PATH)
async def config_form(request: Request, admin=Depends(require_admin)) -> Response:  # noqa: B008
    return _render(request, saved=request.query_params.get("saved") == "1")


@router.post(CONFIG_PATH)
async def config_submit(request: Request, admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    submitted_token = form.get("csrf_token", "")
    if not isinstance(submitted_token, str) or not validate_csrf(
        request.cookies.get(SESSION_COOKIE_NAME, ""), submitted_token
    ):
        raise HTTPException(403, "Invalid CSRF token")

    names = [name for name, _ in form.multi_items()]
    if any(name not in _FORM_FIELDS for name in names) or len(names) != len(set(names)):
        return _render(
            request,
            errors={"form": "This form can only change the settings listed on this page."},
            status_code=422,
        )

    settings = _settings(request)
    saved = read_managed_values(settings.managed_env_path)
    errors: dict[str, str] = {}
    updates: dict[str, str] = {}
    for key in MANAGED_KEYS:
        new_value, error = _submitted_value(key, form)
        if error:
            errors[key.name] = error
        elif new_value is not None and new_value != saved.get(key.name, _running_value(settings, key.field)):
            updates[key.name] = new_value

    if errors:
        return _render(request, errors=errors, status_code=422)

    if updates:
        try:
            write_managed_values(settings.managed_env_path, updates)
        except Exception:
            logger.error("managed configuration write failed")
            return _render(
                request,
                errors={"form": "The settings file could not be saved. Nothing was changed."},
                status_code=503,
            )
        logger.info("managed configuration updated: %s", ", ".join(sorted(updates)))

    return RedirectResponse(f"{CONFIG_PATH}?saved=1", status_code=303, headers=PRIVATE_HEADERS)
