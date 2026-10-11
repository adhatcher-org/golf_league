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
    cookie_is_secure,
    generate_csrf_token,
    require_admin,
    validate_csrf,
)
from golf_league.services.invites import (
    InviteConflict,
    InviteNotFound,
    InviteValidationError,
    create_invite,
    has_recent_live_twin,
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


class InviteFormNonceStore:
    """One-time submission tokens for the create-invite form (single worker).

    The form carries a nonce bound to the admin and session. A POST consumes
    it atomically, so a double click or replay of the same form cannot create
    a second invite. Nonces are process-local, like the receipts above; an
    unknown or expired nonce (for example after a restart) asks the admin to
    reload the form rather than creating anything.
    """

    TTL_SECONDS = 3600

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._nonces: dict[str, tuple[int, str, float, bool]] = {}

    def issue(self, *, admin_id: int, session_cookie: str) -> str:
        with self._lock:
            now = self._clock()
            self._nonces = {key: row for key, row in self._nonces.items() if row[2] > now}
            nonce = generate_token()
            self._nonces[nonce] = (admin_id, digest(session_cookie), now + self.TTL_SECONDS, False)
            return nonce

    def consume(self, *, nonce: str, admin_id: int, session_cookie: str) -> str:
        """Return "fresh" (now used), "used" (seen before) or "unknown"."""
        with self._lock:
            row = self._nonces.get(nonce) if nonce else None
            if row is None or row[2] <= self._clock():
                return "unknown"
            if row[0] != admin_id or row[1] != digest(session_cookie):
                return "unknown"
            if row[3]:
                return "used"
            self._nonces[nonce] = (row[0], row[1], row[2], True)
            return "fresh"

    def release(self, nonce: str) -> None:
        """Make a consumed nonce usable again after a failed, uncommitted create."""
        with self._lock:
            row = self._nonces.get(nonce)
            if row is not None:
                self._nonces[nonce] = (row[0], row[1], row[2], False)


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
        secure=cookie_is_secure(settings), path=RECEIPT_PATH,
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
        "already_submitted": request.query_params.get("submitted") == "again",
    })


def _invite_form(request: Request, admin_id: int, *, errors: dict, default_days: str,
                 status_code: int = 200) -> Response:
    nonce = request.app.state.invite_form_nonces.issue(
        admin_id=admin_id, session_cookie=request.cookies.get(SESSION_COOKIE_NAME, ""),
    )
    return request.app.state.templates.TemplateResponse(request, "admin/invites/form.html", {
        "csrf_token": _csrf_value(request), "errors": errors, "default_days": default_days,
        "form_nonce": nonce,
    }, status_code=status_code)


@router.get("/admin/invites/new")
async def invite_form(request: Request, admin=Depends(require_admin)) -> Response:  # noqa: B008
    return _invite_form(request, admin.id, errors={}, default_days="30")


@router.post("/admin/invites/new")
async def invite_create(
    request: Request, label: str = Form(""), expiry_days: str = Form(""), csrf_token: str = Form(""),
    form_nonce: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    admin_id = admin.id
    nonces = request.app.state.invite_form_nonces
    state = nonces.consume(nonce=form_nonce, admin_id=admin_id,
                           session_cookie=request.cookies.get(SESSION_COOKIE_NAME, ""))
    if state == "used":
        # A double click or replay of a form that already created an invite.
        return RedirectResponse("/admin/invites?submitted=again", status_code=303, headers=PRIVATE_HEADERS)
    if state == "unknown":
        return _invite_form(request, admin_id, default_days="", status_code=422, errors={
            "form": "This form has expired. Check the invite list before creating another invite.",
        })
    try:
        _begin(session)
        now = datetime.now(UTC)
        if has_recent_live_twin(session, created_by_user_id=admin_id, label=label, now=now):
            # Back, then submit again: a fresh nonce, but the invite already exists.
            session.rollback()
            return RedirectResponse("/admin/invites?submitted=again", status_code=303, headers=PRIVATE_HEADERS)
        created = create_invite(session, created_by_user_id=admin_id, label=label, expiry_days=expiry_days, now=now)
        session.commit()
    except InviteValidationError as exc:
        session.rollback()
        nonces.release(form_nonce)  # nothing was created; a retry of this form may proceed
        return _invite_form(request, admin_id, errors=exc.errors, default_days="", status_code=422)
    except Exception:
        session.rollback()
        nonces.release(form_nonce)
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
