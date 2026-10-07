"""Identity routes: login, registration, logout, email verification, password reset.

Routes only. No `golf_league.models` import here — every database access
goes through `golf_league.services.auth`, which owns the ORM queries.
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from golf_league.config import get_settings
from golf_league.database import get_session
from golf_league.domain.identity import normalize_email
from golf_league.domain.tokens import generate_token
from golf_league.security import SESSION_COOKIE_NAME, generate_csrf_token, validate_csrf
from golf_league.services.auth import (
    authenticate_user,
    complete_reset_token,
    consume_token,
    create_session_cookie,
    get_user_by_email,
    hash_password,
    issue_token,
    mark_email_verified,
    peek_token,
)
from golf_league.services.identity import register_roster_user

router = APIRouter()

CSRF_COOKIE_NAME = "csrf_seed"

RESET_TOKEN_TTL_SECONDS = 60 * 60  # one hour
MIN_PASSWORD_LENGTH = 8

_NEUTRAL_LOGIN_MESSAGE = "That email or password is not correct."
_NEUTRAL_RESET_MESSAGE = "If that address has an account, we've sent a link."
_NEUTRAL_REGISTRATION_MESSAGE = (
    "If that address is on the league roster, we've sent it a link to finish "
    "creating your account."
)
_NEUTRAL_REGISTRATION_MESSAGE_WITHOUT_VERIFICATION = (
    "If that address is on the league roster, the account is ready to use."
)


def _settings(request: Request):
    settings = getattr(request.app.state, "settings", None)
    return settings if settings is not None else get_settings()


def _templates(request: Request):
    return request.app.state.templates


def _csrf_seed(request: Request) -> str:
    """Return the anonymous-form CSRF seed carried in `CSRF_COOKIE_NAME`.

    Anonymous forms (login, reset request/completion) have no session
    cookie to bind a CSRF token to, so a dedicated, low-value seed cookie
    is set on the GET that renders the form and echoed back on the POST.
    """
    return request.cookies.get(CSRF_COOKIE_NAME) or ""


def _require_csrf(seed: str, submitted: str) -> None:
    if not validate_csrf(seed, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
        )


def _login_destination(requested: str | None) -> str:
    """Keep the post-login destination on the roster page and same-origin."""
    allowed_destinations = {"/roster"}
    return requested if requested in allowed_destinations else "/roster"


@router.get("/login")
async def login_form(request: Request) -> Response:
    seed = request.cookies.get(CSRF_COOKIE_NAME) or generate_token()
    response = _templates(request).TemplateResponse(
        request,
        "identity/login.html",
        {
            "errors": None,
            "csrf_token": generate_csrf_token(seed),
            "next_path": _login_destination(request.query_params.get("next")),
        },
    )
    response.set_cookie(CSRF_COOKIE_NAME, seed, httponly=True, samesite="lax")
    return response


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    next_path: str = Form("/roster", alias="next"),
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    seed = _csrf_seed(request)
    _require_csrf(seed, csrf_token)

    def neutral_response() -> Response:
        return _templates(request).TemplateResponse(
            request,
            "identity/login.html",
            {
                "errors": {"login": _NEUTRAL_LOGIN_MESSAGE},
                "csrf_token": generate_csrf_token(seed),
                "next_path": _login_destination(next_path),
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    limiter = request.app.state.login_rate_limiter
    normalized_email = normalize_email(email)
    if not limiter.check(normalized_email):
        return neutral_response()

    user = authenticate_user(session, email, password)
    if user is None:
        return neutral_response()

    settings = _settings(request)
    cookie_value = create_session_cookie(user.id, user.session_version, settings.session_secret)
    redirect = RedirectResponse(
        url=_login_destination(next_path), status_code=status.HTTP_303_SEE_OTHER
    )
    redirect.set_cookie(SESSION_COOKIE_NAME, cookie_value, httponly=True, samesite="lax")
    return redirect


@router.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form(""),
    csrf: str = Form(""),
) -> Response:
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    _require_csrf(session_cookie, csrf_token or csrf)

    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(SESSION_COOKIE_NAME)
    return redirect


@router.get("/verify/{token}")
async def verify_email(
    token: str,
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    """Consume a `verify_email` token; idempotent on repeat.

    A consumed, expired, revoked or unknown token all render the identical
    neutral result — never a 500 and never a hint about which case it was.
    """
    user_id = consume_token(session, token, "verify_email")
    if user_id is not None:
        now = datetime.now(UTC).replace(tzinfo=None)
        mark_email_verified(session, user_id, now)

    return HTMLResponse(
        "<p>If that link was valid, your email address is now verified.</p>"
    )


@router.get("/reset")
async def reset_request_form(request: Request) -> Response:
    seed = request.cookies.get(CSRF_COOKIE_NAME) or generate_token()
    response = _templates(request).TemplateResponse(
        request,
        "identity/reset_request.html",
        {"errors": None, "csrf_token": generate_csrf_token(seed)},
    )
    response.set_cookie(CSRF_COOKIE_NAME, seed, httponly=True, samesite="lax")
    return response


@router.post("/reset")
async def reset_request_submit(
    request: Request,
    email: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    seed = _csrf_seed(request)
    _require_csrf(seed, csrf_token)

    user = get_user_by_email(session, email)
    if user is not None:
        raw_token = issue_token(session, user.id, "reset_password", RESET_TOKEN_TTL_SECONDS)
        sender = request.app.state.email_sender
        sender.send(
            to=user.email,
            subject="Reset your password",
            body=f"Use this link to set a new password: /reset/{raw_token}",
        )

    return _templates(request).TemplateResponse(
        request,
        "identity/reset_request.html",
        {"errors": None, "csrf_token": generate_csrf_token(seed), "message": _NEUTRAL_RESET_MESSAGE},
    )


@router.get("/reset/{token}")
async def reset_form(
    token: str,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    user_id = peek_token(session, token, "reset_password")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from None

    seed = request.cookies.get(CSRF_COOKIE_NAME) or generate_token()
    response = _templates(request).TemplateResponse(
        request,
        "identity/reset_form.html",
        {"errors": None, "csrf_token": generate_csrf_token(seed), "token": token},
    )
    response.set_cookie(CSRF_COOKIE_NAME, seed, httponly=True, samesite="lax")
    return response


@router.post("/reset/{token}")
async def reset_submit(
    token: str,
    request: Request,
    password: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    seed = _csrf_seed(request)
    _require_csrf(seed, csrf_token)

    if len(password) < MIN_PASSWORD_LENGTH:
        return _templates(request).TemplateResponse(
            request,
            "identity/reset_form.html",
            {
                "errors": {"password": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."},
                "csrf_token": generate_csrf_token(seed),
                "token": token,
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    password_hash = hash_password(password)
    try:
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))
        result = complete_reset_token(
            session, raw_token=token, password_hash=password_hash, now=datetime.now(UTC)
        )
        if result is None:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        session.commit()
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logging.getLogger(__name__).warning("password reset completion failed")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from None

    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(SESSION_COOKIE_NAME)
    return redirect


def _registration_form(
    request: Request, *, errors: dict[str, str] | None = None, message: str | None = None
) -> Response:
    seed = _csrf_seed(request)
    return _templates(request).TemplateResponse(
        request,
        "identity/register.html",
        {
            "errors": errors,
            "message": message,
            "csrf_token": generate_csrf_token(seed),
        },
    )


@router.get("/register")
async def register_form(request: Request) -> Response:
    seed = request.cookies.get(CSRF_COOKIE_NAME) or generate_token()
    response = _templates(request).TemplateResponse(
        request,
        "identity/register.html",
        {"errors": None, "message": None, "csrf_token": generate_csrf_token(seed)},
    )
    response.set_cookie(CSRF_COOKIE_NAME, seed, httponly=True, samesite="lax")
    return response


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    seed = _csrf_seed(request)
    _require_csrf(seed, csrf_token)

    errors: dict[str, str] = {}
    normalized_email = normalize_email(email)
    if not normalized_email:
        errors["email"] = "Email is required."
    if len(password) < MIN_PASSWORD_LENGTH:
        errors["password"] = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if errors:
        response = _registration_form(request, errors=errors)
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return response

    # Do the expensive work before any eligibility or limiter decision, so
    # account existence and roster membership cannot skip it.
    password_hash = hash_password(password)
    limiter = request.app.state.registration_rate_limiter
    result = None
    if limiter.check(normalized_email):
        result = register_roster_user(
            session,
            email=normalized_email,
            password_hash=password_hash,
            max_users=_settings(request).max_users,
            email_verification_required=_settings(request).email_verification_required,
        )

    if result is not None and result.token is not None and result.email is not None:
        try:
            request.app.state.email_sender.send(
                to=result.email,
                subject="Verify your Golf League account",
                body=f"Use this link to verify your email: /verify/{result.token}",
            )
        except Exception:
            # Delivery is deliberately post-commit; do not log an address or token.
            logging.getLogger(__name__).warning("registration verification email failed")

    message = (
        _NEUTRAL_REGISTRATION_MESSAGE
        if _settings(request).email_verification_required
        else _NEUTRAL_REGISTRATION_MESSAGE_WITHOUT_VERIFICATION
    )
    return _registration_form(request, message=message)
