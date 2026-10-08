"""Verified-admin invitation forms and transient, one-time presentation receipts."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from golf_league.config import get_settings
from golf_league.database import get_session
from golf_league.domain.tokens import digest, generate_token
from golf_league.models import LeagueInviteLink
from golf_league.security import (
    SESSION_COOKIE_NAME,
    generate_csrf_token,
    require_admin,
    validate_csrf,
)
from golf_league.services.invites import (
    InviteConflict,
    InviteNotFound,
    InviteValidationError,
    create_invite,
    revoke_invite,
    rotate_invite,
)

router = APIRouter()
RECEIPT_COOKIE = "invite_receipt"
RECEIPT_PATH = "/admin/invites/created"
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@dataclass(frozen=True)
class _Receipt:
    admin_id: int
    session_digest: str
    expires_at: float
    url: str = field(repr=False)


class InviteReceiptStore:
    """Single-worker ephemeral receipt cache; raw URLs never enter cookies."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._receipts: dict[str, _Receipt] = {}

    def put(self, *, admin_id: int, session_cookie: str, url: str) -> str:
        with self._lock:
            now = self._clock()
            self._receipts = {key: row for key, row in self._receipts.items() if row.expires_at > now}
            nonce = generate_token()
            self._receipts[nonce] = _Receipt(admin_id, digest(session_cookie), now + 300, url)
            return nonce

    def pop(self, *, nonce: str, admin_id: int, session_cookie: str) -> str | None:
        with self._lock:
            receipt = self._receipts.get(nonce)
            if receipt is None:
                return None
            if receipt.expires_at <= self._clock():
                del self._receipts[nonce]
                return None
            if receipt.admin_id != admin_id or receipt.session_digest != digest(session_cookie):
                return None
            del self._receipts[nonce]
            return receipt.url


def _csrf(request: Request, submitted: str) -> None:
    if not validate_csrf(request.cookies.get(SESSION_COOKIE_NAME, ""), submitted):
        raise HTTPException(403, "Invalid CSRF token")


def _csrf_value(request: Request) -> str:
    return generate_csrf_token(request.cookies.get(SESSION_COOKIE_NAME, ""))


def _id(value: str) -> int:
    if not value.isdecimal() or len(value) > 19:
        raise HTTPException(404, "Not found")
    parsed = int(value)
    if not 0 < parsed < 2**63:
        raise HTTPException(404, "Not found")
    return parsed


def _begin(session: Session) -> None:
    # Authentication has performed a read. End that read transaction before
    # acquiring SQLite serialization; all business reads follow BEGIN IMMEDIATE.
    session.rollback()
    session.execute(text("BEGIN IMMEDIATE"))


def _receipt_redirect(request: Request, admin_id: int, raw_token: str) -> Response:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    nonce = request.app.state.invite_receipts.put(
        admin_id=admin_id, session_cookie=request.cookies.get(SESSION_COOKIE_NAME, ""),
        url=f"{settings.external_base_url}/join/{raw_token}",
    )
    response = RedirectResponse(RECEIPT_PATH, status_code=303, headers=PRIVATE_HEADERS)
    response.set_cookie(
        RECEIPT_COOKIE, nonce, max_age=300, httponly=True, samesite="lax",
        secure=settings.external_base_url.startswith("https://"), path=RECEIPT_PATH,
    )
    return response


@router.get("/admin/invites")
async def invite_list(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    now = datetime.now(UTC)
    invites = session.scalars(select(LeagueInviteLink).order_by(LeagueInviteLink.created_at.desc(), LeagueInviteLink.id.desc())).all()
    states = {row.id: "revoked" if row.revoked_at else "expired" if row.expires_at <= now else "live" for row in invites}
    return request.app.state.templates.TemplateResponse(request, "admin/invites/list.html", {
        "invites": invites, "states": states, "csrf_token": _csrf_value(request),
    })


@router.get("/admin/invites/new")
async def invite_form(request: Request, admin=Depends(require_admin)) -> Response:  # noqa: B008
    return request.app.state.templates.TemplateResponse(request, "admin/invites/form.html", {
        "csrf_token": _csrf_value(request), "errors": {}, "default_days": "30",
    })


@router.post("/admin/invites/new")
async def invite_create(
    request: Request, label: str = Form(""), expiry_days: str = Form(""), csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    admin_id = admin.id
    try:
        _begin(session)
        created = create_invite(session, created_by_user_id=admin_id, label=label, expiry_days=expiry_days, now=datetime.now(UTC))
        session.commit()
    except InviteValidationError as exc:
        session.rollback()
        return request.app.state.templates.TemplateResponse(request, "admin/invites/form.html", {
            "csrf_token": _csrf_value(request), "errors": exc.errors, "default_days": "",
        }, status_code=422)
    except Exception:
        session.rollback()
        raise
    return _receipt_redirect(request, admin_id, created.raw_token)


@router.get(RECEIPT_PATH)
async def invite_created(request: Request, admin=Depends(require_admin)) -> Response:  # noqa: B008
    url = request.app.state.invite_receipts.pop(
        nonce=request.cookies.get(RECEIPT_COOKIE, ""), admin_id=admin.id,
        session_cookie=request.cookies.get(SESSION_COOKIE_NAME, ""),
    )
    if url is None:
        response = Response("Not found", status_code=404, headers=PRIVATE_HEADERS)
    else:
        response = request.app.state.templates.TemplateResponse(request, "admin/invites/created.html", {
            "shared_url": url,
        }, headers=PRIVATE_HEADERS)
    response.delete_cookie(RECEIPT_COOKIE, path=RECEIPT_PATH)
    return response


@router.post("/admin/invites/{invite_id}/revoke")
async def invite_revoke(
    invite_id: str, request: Request, csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    numeric_id = _id(invite_id)
    try:
        _begin(session)
        if not revoke_invite(session, invite_id=numeric_id, now=datetime.now(UTC)):
            raise HTTPException(404, "Not found")
        session.commit()
    except Exception:
        session.rollback()
        raise
    return RedirectResponse("/admin/invites", status_code=303)


@router.post("/admin/invites/{invite_id}/rotate")
async def invite_rotate(
    invite_id: str, request: Request, csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    numeric_id, admin_id = _id(invite_id), admin.id
    try:
        _begin(session)
        created = rotate_invite(session, invite_id=numeric_id, created_by_user_id=admin_id, now=datetime.now(UTC))
        session.commit()
    except InviteNotFound:
        session.rollback()
        raise HTTPException(404, "Not found") from None
    except InviteConflict:
        session.rollback()
        raise HTTPException(409, "Invitation is expired or revoked") from None
    except Exception:
        session.rollback()
        raise
    return _receipt_redirect(request, admin_id, created.raw_token)
