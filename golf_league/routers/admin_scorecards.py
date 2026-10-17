"""Admin pages for scorecard photo entry (R-SCORES, R-OCR-PROVIDER).

An admin picks a week, uploads a photo (or starts a manual card), corrects what
was read and submits it. Every route requires a verified admin, every POST a
CSRF token, and every rule is enforced again on the server. No handler calls
the provider: reading happens on the background worker only. Responses are
`Cache-Control: no-store` because they show player names, and nothing here logs
a name, a score or image data.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from golf_league.database import get_session
from golf_league.domain.scorecard import Actor
from golf_league.models import (
    HoleScore,
    PlayerMatch,
    ScorecardDraft,
    TeamMatch,
    User,
    Week,
)
from golf_league.routers import scorecard_flow
from golf_league.routers.scorecard_flow import (
    IMAGE_MESSAGES,
    PRIVATE_HEADERS,
    Flow,
    _csrf,
    _now,
    _page,
    _text,
    _today,
    declared_too_large,
)
from golf_league.security import require_admin
from golf_league.services.player_schedule import select_player_season
from golf_league.services.scorecard_drafts import (
    create_draft_from_image,
    create_manual_draft,
    delete_draft,
    list_open_drafts,
    purge_expired,
)
from golf_league.services.scorecard_image import ImageRefused
from golf_league.services.scores import (
    ScoreEntryError,
    default_week,
    eligible_weeks,
    submit_card,
)

logger = logging.getLogger(__name__)

router = APIRouter()

NEW_PATH = "/admin/scorecards/new"
FLOW = Flow("/admin/scorecards", "admin/scorecards", True)

def _number(text: str) -> int | None:
    """A positive whole number from a form or query value, else None; never raises."""
    text = text.strip()
    if not (text.isascii() and text.isdecimal()) or len(text) > 18:
        return None
    return int(text) or None


def _purge(request: Request, session: Session) -> None:
    """Retention housekeeping on the pages that list or open drafts; never fails the page."""
    try:
        purge_expired(session, _now(request), directory=request.app.state.scorecard_dir)
    except Exception as exc:
        session.rollback()
        logger.warning("scorecard draft purge failed type=%s", type(exc).__name__)


# --- start a card -----------------------------------------------------------


def _scored_weeks(session: Session, season_id: int) -> list[Week]:
    return list(session.scalars(
        select(Week).where(Week.season_id == season_id, Week.id.in_(
            select(TeamMatch.week_id).join(PlayerMatch, PlayerMatch.team_match_id == TeamMatch.id)
            .join(HoleScore, HoleScore.player_match_id == PlayerMatch.id)
        )).order_by(Week.index)
    ))


def _new_page(request: Request, session: Session, admin, *, errors: dict[str, str] | None = None,
              week_id: int | None = None, status_code: int = 200) -> Response:
    season = select_player_season(session)
    today = _today(request)
    weeks = eligible_weeks(session, season.id, today, Actor(admin.id, True)) if season else []
    chosen = week_id if week_id in {week.id for week in weeks} else None
    if chosen is None and season is not None:
        default = default_week(session, season.id, today)
        chosen = default.id if default else None
    week_by_id = {week.id: week for week in session.scalars(select(Week))} if season else {}
    admin_ids = set(session.scalars(select(User.id).where(User.is_admin.is_(True))))
    drafts = [{"draft": draft, "week": week_by_id.get(draft.week_id),
               "by_player": draft.created_by_user_id not in admin_ids}
              for draft in list_open_drafts(session, admin.id) if draft.expires_at > _now(request)]
    return _page(request, "admin/scorecards/new.html", {
        "season": season, "weeks": weeks, "chosen_week_id": chosen, "drafts": drafts,
        "configured": request.app.state.vision_provider is not None,
        "scored_weeks": _scored_weeks(session, season.id) if season else [],
        "errors": errors or {}, "field_ids": {"week_id": "week_id", "photo": "photo"},
        "discarded": request.query_params.get("discarded") == "1",
    }, status_code)


@router.get(NEW_PATH)
async def scorecard_new(request: Request,
                        session: Session = Depends(get_session),  # noqa: B008
                        admin=Depends(require_admin)) -> Response:  # noqa: B008
    _purge(request, session)
    return _new_page(request, session, admin, week_id=_number(request.query_params.get("week_id", "")))


class _Refused(Exception):
    """A start-of-card request refused with a message for one field."""

    def __init__(self, field: str, message: str) -> None:
        self.field, self.message = field, message
        super().__init__(field)


async def _start_draft(request: Request, session: Session, admin, form, week_id: int) -> ScorecardDraft:
    today, clock, state = _today(request), request.app.state.scorecard_utc_now, request.app.state
    if _text(form, "manual") == "1":
        return create_manual_draft(session, week_id=week_id, user_id=admin.id, today=today, clock=clock,
                                   actor=Actor(admin.id, True))
    if state.vision_provider is None:
        raise _Refused("photo", "Photo reading is not set up. Enter the scores manually.")
    photo = form.get("photo")
    if not isinstance(photo, UploadFile) or not photo.filename:
        raise _Refused("photo", "Choose a photo of the scorecard.")
    return await scorecard_flow.start_photo_draft(request, session, week_id=week_id, user=admin, flow=FLOW,
                                                  photo=photo, create_draft_from_image=create_draft_from_image)


@router.post("/admin/scorecards")
async def scorecard_create(request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    # The body is not parsed until the admin check has passed, and an upload
    # that is plainly too large is refused without being stored.
    if await declared_too_large(request):
        return _new_page(request, session, admin, errors={"photo": IMAGE_MESSAGES["too_large"]},
                         status_code=422)
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    week_id = _number(_text(form, "week_id"))
    if week_id is None:
        return _new_page(request, session, admin, errors={"week_id": "Choose a week."}, status_code=422)
    try:
        draft = await _start_draft(request, session, admin, form, week_id)
    except ScoreEntryError as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="Not found") from exc
        errors = {"week_id": exc.message}
    except ImageRefused as exc:
        errors = {"photo": IMAGE_MESSAGES.get(exc.code, IMAGE_MESSAGES["unsupported_type"])}
    except _Refused as exc:
        errors = {exc.field: exc.message}
    except (SQLAlchemyError, OSError) as exc:
        # A busy database or a data directory that cannot be written: nothing was kept.
        session.rollback()
        logger.warning("scorecard draft not created type=%s", type(exc).__name__)
        return _new_page(request, session, admin, week_id=week_id, status_code=503, errors={
            "photo": "The card could not be started just now. Nothing was saved; try again."})
    else:
        return RedirectResponse(f"/admin/scorecards/{draft.id}", status_code=303, headers=PRIVATE_HEADERS)
    session.rollback()
    return _new_page(request, session, admin, errors=errors, week_id=week_id, status_code=422)


# --- one card: the shared flow, in the admin's addresses ------------------


@router.get("/admin/scorecards/{draft_id}")
async def scorecard_detail(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    return scorecard_flow.detail(request, session, draft_id, admin, FLOW, purge=_purge)


@router.get("/admin/scorecards/{draft_id}/status")
async def scorecard_status(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    return scorecard_flow.status(request, session, draft_id, admin, FLOW)


@router.get("/admin/scorecards/{draft_id}/image")
async def scorecard_image(draft_id: int, request: Request,
                          session: Session = Depends(get_session),  # noqa: B008
                          admin=Depends(require_admin)) -> Response:  # noqa: B008
    return scorecard_flow.image(request, session, draft_id, admin, FLOW)


@router.post("/admin/scorecards/{draft_id}/submit")
async def scorecard_submit(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    return await scorecard_flow.submit(request, session, draft_id, admin, FLOW, submit_card=submit_card,
                                       delete_draft=delete_draft)


@router.post("/admin/scorecards/{draft_id}/discard")
async def scorecard_discard(draft_id: int, request: Request,
                            session: Session = Depends(get_session),  # noqa: B008
                            admin=Depends(require_admin)) -> Response:  # noqa: B008
    return await scorecard_flow.discard(request, session, draft_id, admin, FLOW)
