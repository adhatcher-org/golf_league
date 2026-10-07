"""Anonymous shared-invite and golfer password completion routes."""

import asyncio
import logging
import time
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from golf_league.config import get_settings
from golf_league.database import get_session
from golf_league.domain.tokens import generate_token
from golf_league.routers.identity import (
    CSRF_COOKIE_NAME,
    MIN_PASSWORD_LENGTH,
    _require_csrf,
)
from golf_league.security import SESSION_COOKIE_NAME, generate_csrf_token
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.invites import (
    AccountCapacityReached,
    admit_join_send,
    complete_set_password,
    get_valid_invite,
    peek_set_password_token,
    record_send_failure,
    record_send_success,
)

router = APIRouter()
logger = logging.getLogger(__name__)
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
NEUTRAL_JOIN_MESSAGE = "If that address is on the league roster, we've sent it a link."
_NO_CLIENT_IP = "unknown-client"


def _settings(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


def _now(request: Request) -> datetime:
    clock = getattr(request.app.state, "join_utc_now", None)
    return clock() if clock is not None else datetime.now(UTC)


def _seed(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE_NAME) or generate_token()


def _private_error(status_code: int, detail: str = "Not found") -> HTTPException:
    return HTTPException(status_code, detail, headers=PRIVATE_HEADERS)


def _private_csrf(seed: str, submitted: str) -> None:
    try:
        _require_csrf(seed, submitted)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from None


def _set_private(response: Response) -> Response:
    response.headers.update(PRIVATE_HEADERS)
    return response


def _begin(session: Session) -> None:
    session.rollback()
    session.execute(text("BEGIN IMMEDIATE"))


async def _sleep_floor(request: Request, started: float) -> None:
    monotonic = getattr(request.app.state, "join_monotonic", time.monotonic)
    sleeper = getattr(request.app.state, "join_sleep", asyncio.sleep)
    remaining = 0.100 - (monotonic() - started)
    if remaining > 0:
        await sleeper(remaining)


def _neutral_join(request: Request) -> Response:
    seed = request.cookies.get(CSRF_COOKIE_NAME, "")
    response = request.app.state.templates.TemplateResponse(
        request,
        "identity/join.html",
        {"csrf_token": generate_csrf_token(seed), "message": NEUTRAL_JOIN_MESSAGE},
        status_code=200,
        headers=PRIVATE_HEADERS,
    )
    return response


def _dispatch_email(request: Request, delivery, origin: str) -> None:
    """Send after foreground response, then record outcome in a fresh session."""
    from sqlalchemy.orm import Session as FreshSession

    sender = request.app.state.email_sender
    url = f"{origin}/set-password/{delivery.raw_token}"
    try:
        sender.send(
            to=delivery.recipient_email,
            subject="Set your St. Paul Golf League password",
            body=f"Use this link to set your sign-in password: {url}",
        )
    except Exception:
        logger.warning("shared invite email send failed")
        try:
            with FreshSession(bind=request.app.state.engine, autoflush=False) as session:
                session.execute(text("BEGIN IMMEDIATE"))
                record_send_failure(session, token_id=delivery.token_id, now=_now(request))
                session.commit()
        except Exception:
            logger.error("shared invite outcome recording failed")
        return

    try:
        with FreshSession(bind=request.app.state.engine, autoflush=False) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            record_send_success(session, token_id=delivery.token_id, now=_now(request))
            session.commit()
    except Exception:
        logger.error("shared invite outcome recording failed")


@router.get("/join/{token}")
async def join_form(token: str, request: Request, session: Session = Depends(get_session)) -> Response:  # noqa: B008
    if get_valid_invite(session, raw_token=token, now=_now(request)) is None:
        raise _private_error(status.HTTP_404_NOT_FOUND) from None
    seed = _seed(request)
    response = request.app.state.templates.TemplateResponse(
        request,
        "identity/join.html",
        {"csrf_token": generate_csrf_token(seed), "message": None},
        headers=PRIVATE_HEADERS,
    )
    response.set_cookie(CSRF_COOKIE_NAME, seed, httponly=True, samesite="lax")
    return response


@router.post("/join/{token}")
async def join_submit(
    token: str,
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    monotonic = getattr(request.app.state, "join_monotonic", time.monotonic)
    started = monotonic()
    _private_csrf(request.cookies.get(CSRF_COOKIE_NAME, ""), csrf_token)
    settings = _settings(request)
    client_ip = request.client.host if request.client is not None else _NO_CLIENT_IP
    delivery = None
    try:
        _begin(session)
        delivery = admit_join_send(
            session,
            raw_invite_token=token,
            email=email,
            client_ip=client_ip,
            secret=settings.session_secret,
            now=_now(request),
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("shared invite admission failed")
        delivery = None
    await _sleep_floor(request, started)
    if delivery is not None:
        background_tasks.add_task(_dispatch_email, request, delivery, settings.external_base_url)
    return _neutral_join(request)


@router.get("/set-password/{token}")
async def set_password_form(token: str, request: Request, session: Session = Depends(get_session)) -> Response:  # noqa: B008
    if peek_set_password_token(session, raw_token=token, now=_now(request)) is None:
        raise _private_error(status.HTTP_404_NOT_FOUND)
    seed = _seed(request)
    response = request.app.state.templates.TemplateResponse(
        request,
        "identity/set_password.html",
        {"csrf_token": generate_csrf_token(seed), "errors": None, "token": token},
        headers=PRIVATE_HEADERS,
    )
    response.set_cookie(CSRF_COOKIE_NAME, seed, httponly=True, samesite="lax")
    return response


@router.post("/set-password/{token}")
async def set_password_submit(
    token: str,
    request: Request,
    password: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
) -> Response:
    seed = request.cookies.get(CSRF_COOKIE_NAME, "")
    _private_csrf(seed, csrf_token)
    if peek_set_password_token(session, raw_token=token, now=_now(request)) is None:
        raise _private_error(status.HTTP_404_NOT_FOUND)
    if len(password) < MIN_PASSWORD_LENGTH:
        response = request.app.state.templates.TemplateResponse(
            request,
            "identity/set_password.html",
            {
                "csrf_token": generate_csrf_token(seed),
                "errors": {"password": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."},
                "token": token,
            },
            status_code=422,
            headers=PRIVATE_HEADERS,
        )
        return response
    password_hash = hash_password(password)
    try:
        _begin(session)
        result = complete_set_password(
            session, raw_token=token, password_hash=password_hash, now=_now(request)
        )
        if result is None:
            session.rollback()
            raise _private_error(status.HTTP_404_NOT_FOUND)
        session.commit()
    except AccountCapacityReached:
        session.rollback()
        return request.app.state.templates.TemplateResponse(
            request,
            "identity/set_password.html",
            {"csrf_token": generate_csrf_token(seed), "errors": {"password": "The account could not be created. Request a new link."}, "token": token},
            status_code=409,
            headers=PRIVATE_HEADERS,
        )
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.warning("shared password completion failed")
        raise _private_error(status.HTTP_404_NOT_FOUND) from None

    settings = _settings(request)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER, headers=PRIVATE_HEADERS)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_cookie(result.user_id, result.session_version, settings.session_secret),
        httponly=True,
        samesite="lax",
        secure=settings.external_base_url.startswith("https://"),
    )
    return response
