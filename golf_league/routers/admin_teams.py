"""HTTP-only admin routes for manually configured season teams."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.participants import get_season
from golf_league.services.teams import (
    TeamValidationError,
    assign_positions_by_handicap,
    get_team,
    list_assignable_golfers,
    list_season_teams,
    save_team,
)

router = APIRouter()


def _csrf(request: Request, token: str) -> None:
    seed = request.cookies.get("session") or ""
    if not validate_csrf(seed, token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _form(request: Request, session: Session, season_id: int, *, team=None,
          errors=None, status_code: int = 200) -> Response:
    season = get_season(session, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Not found")
    seed = request.cookies.get("session") or ""
    selected = {member.position: member.golfer_id for member in team.members} if team else {}
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/teams/form.html",
        {
            "season": season,
            "team": team,
            "errors": errors,
            "golfers": list_assignable_golfers(session, season_id, team.id if team else None),
            "selected": selected,
            "csrf_token": generate_csrf_token(seed) if seed else "",
        },
        status_code=status_code,
    )


def _members(member_1: str, member_2: str, member_3: str, member_4: str) -> list[dict[str, str]]:
    return [
        {"golfer_id": golfer_id, "position": str(position)}
        for position, golfer_id in enumerate((member_1, member_2, member_3, member_4), start=1)
        if golfer_id.strip()
    ]


@router.get("/admin/seasons/{season_id}/teams")
async def teams_page(
    season_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    season = get_season(session, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "admin/teams/list.html",
        {"season": season, "teams": list_season_teams(session, season_id)},
    )


@router.get("/admin/seasons/{season_id}/teams/new")
async def new_team_form(
    season_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    return _form(request, session, season_id)


@router.post("/admin/seasons/{season_id}/teams/new")
async def create_team_submit(
    season_id: int,
    request: Request,
    name: str = Form(""),
    number: str = Form(""),
    sort_order: str = Form(""),
    member_1: str = Form(""),
    member_2: str = Form(""),
    member_3: str = Form(""),
    member_4: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    try:
        team = save_team(
            session, season_id, name=name, number=number, sort_order=sort_order,
            members=_members(member_1, member_2, member_3, member_4),
        )
    except TeamValidationError as exc:
        return _form(request, session, season_id, errors=exc.errors, status_code=422)
    if team is None:
        raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(f"/admin/seasons/{season_id}/teams", status_code=303)


@router.get("/admin/teams/{team_id}/edit")
async def edit_team_form(
    team_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    team = get_team(session, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Not found")
    return _form(request, session, team.season_id, team=team)


@router.post("/admin/teams/{team_id}/edit")
async def update_team_submit(
    team_id: int,
    request: Request,
    name: str = Form(""),
    number: str = Form(""),
    sort_order: str = Form(""),
    member_1: str = Form(""),
    member_2: str = Form(""),
    member_3: str = Form(""),
    member_4: str = Form(""),
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    team = get_team(session, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Not found")
    season_id = team.season_id
    try:
        updated = save_team(
            session, season_id, team_id=team_id, name=name, number=number,
            sort_order=sort_order,
            members=_members(member_1, member_2, member_3, member_4),
        )
    except TeamValidationError as exc:
        return _form(request, session, season_id, team=team, errors=exc.errors, status_code=422)
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(f"/admin/seasons/{season_id}/teams", status_code=303)


@router.post("/admin/teams/{team_id}/assign-positions")
async def assign_team_positions(
    team_id: int,
    request: Request,
    csrf_token: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
    admin=Depends(require_admin),  # noqa: B008
) -> Response:
    _csrf(request, csrf_token)
    team = get_team(session, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        updated = assign_positions_by_handicap(session, team_id)
    except TeamValidationError as exc:
        return _form(request, session, team.season_id, team=team, errors=exc.errors, status_code=422)
    if updated is None:
        raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(f"/admin/teams/{team_id}/edit", status_code=303)
