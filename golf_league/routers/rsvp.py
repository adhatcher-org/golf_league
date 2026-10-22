"""Private weekly RSVP form and explicit admin reminder action."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import User, Week
from golf_league.routers.scorecard_flow import PRIVATE_HEADERS, _csrf, _page, _text
from golf_league.security import require_admin, require_verified_user
from golf_league.services.rsvp import (
    RsvpError,
    get_rsvp,
    reminder_recipients,
    set_rsvp,
    week_summary,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _week(session: Session, week_id: int) -> Week:
    week = session.get(Week, week_id)
    if week is None:
        raise HTTPException(status_code=404, detail="Week not found", headers=PRIVATE_HEADERS)
    return week


def _player_page(request: Request, session: Session, week: Week, golfer_id: int, *,
                 selected: str | None = None, error: str | None = None, status_code: int = 200) -> Response:
    current = get_rsvp(session, week.id, golfer_id)
    return _page(request, "player/rsvp.html", {
        "week": week, "selected": selected if selected is not None else (current.status if current else ""),
        "error": error,
    }, status_code)


def _send_reminders(sender, recipients: list[str], week: Week, base_url: str) -> None:
    for address in recipients:
        try:
            sender.send(
                to=address,
                subject=f"Golf League week {week.index} RSVP reminder",
                body=(f"Please RSVP for week {week.index} on {week.play_date:%b %d, %Y}.\n"
                      f"Open {base_url}/weeks/{week.id}/rsvp to respond.\n"),
            )
        except Exception:
            logger.warning("weekly RSVP reminder delivery failed")


@router.get("/weeks/{week_id}/rsvp")
async def rsvp_form(week_id: int, request: Request,
                    session: Session = Depends(get_session),  # noqa: B008
                    user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    if user.golfer_id is None:
        raise HTTPException(status_code=403, detail="A linked golfer is required", headers=PRIVATE_HEADERS)
    return _player_page(request, session, _week(session, week_id), user.golfer_id)


@router.post("/weeks/{week_id}/rsvp")
async def rsvp_submit(week_id: int, request: Request,
                      session: Session = Depends(get_session),  # noqa: B008
                      user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    if user.golfer_id is None:
        raise HTTPException(status_code=403, detail="A linked golfer is required", headers=PRIVATE_HEADERS)
    week = _week(session, week_id)
    form = await request.form()
    _csrf(request, form.get("csrf_token"))
    status = _text(form, "status")
    try:
        set_rsvp(session, week.id, user.golfer_id, status)
    except RsvpError as exc:
        return _player_page(request, session, week, user.golfer_id, selected=status,
                            error=str(exc), status_code=422)
    return RedirectResponse(f"/weeks/{week.id}/rsvp", status_code=303, headers=PRIVATE_HEADERS)


@router.post("/admin/weeks/{week_id}/rsvp/reminders")
async def send_rsvp_reminders(week_id: int, request: Request, background_tasks: BackgroundTasks,
                              session: Session = Depends(get_session),  # noqa: B008
                              admin: User = Depends(require_admin)) -> Response:  # noqa: B008
    week = _week(session, week_id)
    form = await request.form()
    _csrf(request, form.get("csrf_token"))
    recipients = reminder_recipients(week_summary(session, week.id))
    background_tasks.add_task(_send_reminders, request.app.state.email_sender, recipients,
                              week, request.app.state.settings.external_base_url.rstrip("/"))
    return RedirectResponse(f"/admin/weeks/{week.id}/scores?reminders={len(recipients)}",
                            status_code=303, headers=PRIVATE_HEADERS)
