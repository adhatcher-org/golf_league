"""HTTP-only admin routes for season participant overrides."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.participants import (
    ParticipantValidationError,
    add_participant_override,
    get_participant,
    get_season,
    list_available_golfers,
    list_course_tees,
    list_participant_views,
    update_participant_override,
)
from golf_league.services.season_roster import (
    SeasonRosterValidationError,
    list_season_golfers,
    save_season_golfers,
)

router = APIRouter()


def _csrf(request: Request, token: str) -> None:
    seed = request.cookies.get("session") or ""
    if not validate_csrf(seed, token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _render(
    request: Request,
    session: Session,
    season_id: int,
    *,
    errors=None,
    saved=False,
    status_code=200,
) -> Response:
    season = get_season(session, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Not found")
    seed = request.cookies.get("session") or ""
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/seasons/participants.html",
        {
            "season": season,
            "participants": list_participant_views(session, season_id),
            "season_golfers": list_season_golfers(session, season_id),
            "available_golfers": list_available_golfers(session, season_id),
            "tees": list_course_tees(session, season_id),
            "errors": errors,
            "roster_saved": saved,
            "csrf_token": generate_csrf_token(seed) if seed else "",
        },
        status_code=status_code,
    )


@router.post("/admin/seasons/{season_id}/participants/roster")
async def season_roster_save(
    season_id: int,
    request: Request,
    golfer_ids: list[str] = Form(default_factory=list),  # noqa: B008
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    try:
        save_season_golfers(session, season_id, golfer_ids)
    except SeasonRosterValidationError as exc:
        if get_season(session, season_id) is None:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return _render(
            request,
            session,
            season_id,
            errors={"season_roster": str(exc)},
            status_code=422,
        )
    return RedirectResponse(
        f"/admin/seasons/{season_id}/participants?roster_saved=1", status_code=303
    )


@router.get("/admin/seasons/{season_id}/participants")
async def participants_page(
    season_id: int,
    request: Request,
    roster_saved: str = "",
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    return _render(request, session, season_id, saved=roster_saved == "1")


@router.post("/admin/seasons/{season_id}/participants/add")
async def participant_add(
    season_id: int,
    request: Request,
    golfer_id: str = Form(""),
    tee_set_id: str = Form(""),
    seed_handicap_strokes: str = Form(""),
    seed_source: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    try:
        row = add_participant_override(
            session, season_id, golfer_id=golfer_id, tee_set_id=tee_set_id,
            seed_handicap_strokes=seed_handicap_strokes, seed_source=seed_source,
        )
    except ParticipantValidationError as exc:
        return _render(request, session, season_id, errors=exc.errors, status_code=422)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(f"/admin/seasons/{season_id}/participants", status_code=303)


@router.post("/admin/seasons/{season_id}/participants/{participant_id}/edit")
async def participant_edit(
    season_id: int,
    participant_id: int,
    request: Request,
    tee_action: str = Form(""),
    tee_set_id: str = Form(""),
    seed_action: str = Form(""),
    seed_handicap_strokes: str = Form(""),
    seed_source: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    existing = get_participant(session, participant_id)
    if existing is None or existing.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        row = update_participant_override(
            session, season_id, participant_id,
            tee_action=tee_action, tee_set_id=tee_set_id,
            seed_action=seed_action, seed_handicap_strokes=seed_handicap_strokes,
            seed_source=seed_source,
        )
    except ParticipantValidationError as exc:
        return _render(request, session, season_id, errors=exc.errors, status_code=422)
    if row is None and get_season(session, season_id) is None:
        raise HTTPException(status_code=404, detail="Not found")
    # None can also mean both overrides were cleared and the optional row was removed.
    return RedirectResponse(f"/admin/seasons/{season_id}/participants", status_code=303)
