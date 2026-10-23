"""Player request and admin response pages for weekly substitutions."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from golf_league.database import get_session
from golf_league.models import (
    Golfer,
    MatchScoreState,
    PlayerMatch,
    SubstituteRequest,
    TeamMatch,
    TeamMember,
    User,
    Week,
)
from golf_league.security import (
    generate_csrf_token,
    require_admin,
    require_verified_user,
    validate_csrf,
)
from golf_league.services.matchups import MatchupConflict, MatchupValidationError
from golf_league.services.substitute_requests import (
    RequestConflict,
    RequestNotFound,
    create_request,
    pending_requests,
    respond,
)

router = APIRouter()
logger = logging.getLogger(__name__)
PRIVATE = {"Cache-Control": "no-store"}


def _now() -> datetime:
    return datetime.now(UTC)


def _csrf(request: Request, submitted: str) -> None:
    if not validate_csrf(request.cookies.get("session") or "", submitted):
        raise HTTPException(status_code=403, detail="Invalid CSRF token", headers=PRIVATE)


def _csrf_value(request: Request) -> str:
    return generate_csrf_token(request.cookies.get("session") or "")


def _request_page(request: Request, session: Session, week_id: int, match_id: int, user: User) -> Response:
    match = session.get(PlayerMatch, match_id)
    pairing = session.get(TeamMatch, match.team_match_id) if match else None
    week = session.get(Week, week_id)
    if (user.golfer_id is None or week is None or pairing is None or pairing.week_id != week_id
            or user.golfer_id not in (match.a_golfer_id, match.b_golfer_id)):
        raise HTTPException(status_code=404, detail="Not found", headers=PRIVATE)
    state = session.get(MatchScoreState, match_id)
    if week.status != "scheduled" or week.week_type != "match" or (state and state.locked_at):
        raise HTTPException(status_code=409, detail="This match cannot accept a substitute request.", headers=PRIVATE)
    sent = request.query_params.get("sent") and session.scalar(select(SubstituteRequest.id).where(
        SubstituteRequest.player_match_id == match_id
    )) is not None
    response = request.app.state.templates.TemplateResponse(request, "player/substitute_request.html",
        {"week": week, "match": match, "sent": sent, "csrf_token": _csrf_value(request)})
    response.headers.update(PRIVATE)
    return response


def _send_notice(app, request_id: int, token: str, recipients: list[str], week_id: int, match_id: int) -> bool:
    """Only email carries the token. Remove an undelivered pending request."""
    delivered = False
    for recipient in recipients:
        try:
            app.state.email_sender.send(
                to=recipient,
                subject="Golf League substitute request",
                body=(f"A player requested a substitute for week {week_id}, match {match_id}.\n"
                      f"Open {app.state.settings.external_base_url}/admin/substitute-requests "
                      f"and enter this one-time response token: {token}\n"),
            )
            delivered = True
        except Exception:
            logger.warning("substitute request email send failed")
    if not delivered:
        with Session(bind=app.state.engine) as session:
            row = session.get(SubstituteRequest, request_id)
            if row is not None and row.status == "pending":
                session.delete(row)
                session.commit()
        logger.warning("undelivered substitute request removed")
    return delivered


@router.get("/weeks/{week_id}/matches/{match_id}/substitute-request")
async def request_form(week_id: int, match_id: int, request: Request,
                       session: Session = Depends(get_session),  # noqa: B008
                       user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return _request_page(request, session, week_id, match_id, user)


@router.post("/weeks/{week_id}/matches/{match_id}/substitute-request")
async def request_submit(week_id: int, match_id: int, request: Request,
                         csrf_token: str = Form(""), session: Session = Depends(get_session),  # noqa: B008
                         user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    _csrf(request, csrf_token)
    try:
        row, raw_token, recipients = create_request(session, week_id=week_id, player_match_id=match_id,
                                                     golfer_id=user.golfer_id, clock=_now)
    except RequestNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found", headers=PRIVATE) from exc
    except (RequestConflict, MatchupConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=PRIVATE) from exc
    delivered = await run_in_threadpool(
        _send_notice, request.app, row.id, raw_token, recipients, week_id, match_id,
    )
    if not delivered:
        raise HTTPException(status_code=503, detail="The request could not be delivered. Please try again.",
                            headers=PRIVATE)
    return RedirectResponse(f"/weeks/{week_id}/matches/{match_id}/substitute-request?sent=1",
                            status_code=303, headers=PRIVATE)


@router.get("/admin/substitute-requests")
async def admin_requests(request: Request, session: Session = Depends(get_session),  # noqa: B008
                         admin: User = Depends(require_admin)) -> Response:  # noqa: B008
    rows = pending_requests(session)
    match_ids = {row.player_match_id for row in rows}
    golfer_ids = {row.requesting_golfer_id for row in rows}
    matches = {row.id: row for row in session.scalars(select(PlayerMatch).where(PlayerMatch.id.in_(match_ids)))}
    golfers = {row.id: row for row in session.scalars(select(Golfer).where(Golfer.id.in_(golfer_ids)))}
    weeks = {row.id: row for row in session.scalars(select(Week).where(Week.id.in_(row.week_id for row in rows)))}
    substitutes = {}
    for row in rows:
        assigned = select(TeamMember.golfer_id).where(TeamMember.season_id == weeks[row.week_id].season_id)
        substitutes[row.id] = list(session.scalars(select(Golfer).where(
            Golfer.is_active.is_(True), Golfer.id.not_in(assigned)
        ).order_by(Golfer.last_name, Golfer.first_name, Golfer.id)))
    response = request.app.state.templates.TemplateResponse(request, "admin/substitute_requests.html",
        {"requests": rows, "matches": matches, "golfers": golfers, "weeks": weeks,
         "substitutes": substitutes, "csrf_token": _csrf_value(request),
         "notice": request.query_params.get("notice", "")})
    response.headers.update(PRIVATE)
    return response


@router.post("/admin/substitute-requests/{request_id}/respond")
async def admin_respond(request_id: int, request: Request, action: str = Form(""),
                        response_token: str = Form(""), golfer_id: str = Form(""),
                        csrf_token: str = Form(""), session: Session = Depends(get_session),  # noqa: B008
                        admin: User = Depends(require_admin)) -> Response:  # noqa: B008
    _csrf(request, csrf_token)
    parsed_golfer_id = None
    if action == "fulfill":
        if not golfer_id.isascii() or not golfer_id.isdecimal() or len(golfer_id) > 19:
            raise HTTPException(status_code=422, detail="Choose a valid substitute.", headers=PRIVATE)
        parsed_golfer_id = int(golfer_id)
    try:
        respond(session, request_id=request_id, raw_token=response_token, action=action,
                golfer_id=parsed_golfer_id, clock=_now)
    except RequestNotFound as exc:
        raise HTTPException(status_code=404, detail="Not found", headers=PRIVATE) from exc
    except (RequestConflict, MatchupConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers=PRIVATE) from exc
    except MatchupValidationError as exc:
        raise HTTPException(status_code=422, detail="Choose an eligible substitute.", headers=PRIVATE) from exc
    except LookupError as exc:
        raise HTTPException(status_code=422, detail="Choose an eligible substitute.", headers=PRIVATE) from exc
    return RedirectResponse("/admin/substitute-requests?notice=updated", status_code=303, headers=PRIVATE)
