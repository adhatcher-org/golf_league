"""Admin forms for atomic matchup generation and durable substitutions."""

from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import (
    Golfer,
    PlayerMatch,
    Season,
    Team,
    TeamMatch,
    TeamMember,
    Week,
    WeekHandicap,
)
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.matchups import (
    MatchupConflict,
    MatchupValidationError,
    generate_week_matches,
    substitute_player,
)

router = APIRouter()


def _clock():
    return datetime.now(UTC)


def _csrf(request, token):
    if not validate_csrf(request.cookies.get("session") or "", token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _number(value, field):
    text = value.strip()
    errors = {field: "Choose a valid player or team."}
    if not text.isdecimal():
        raise MatchupValidationError(errors)
    try:
        number = int(text)
    except (ValueError, OverflowError) as exc:
        raise MatchupValidationError(errors) from exc
    if not 0 < number <= (2**63 - 1):
        raise MatchupValidationError(errors)
    return number


def _week(session, season_id, week_id):
    season, week = session.get(Season, season_id), session.get(Week, week_id)
    if season is None or week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    return season, week


def _player(session, season_id, player_id):
    row = session.get(PlayerMatch, player_id)
    pairing = session.get(TeamMatch, row.team_match_id) if row else None
    if pairing is None:
        raise HTTPException(status_code=404, detail="Not found")
    season, week = _week(session, season_id, pairing.week_id)
    return season, week, row


def _render(request, session, season, week, *, player=None, errors=None, status_code=200):
    pairings = list(session.scalars(select(TeamMatch).where(TeamMatch.week_id == week.id).order_by(TeamMatch.sort_order, TeamMatch.id)))
    matches = [{"pairing": pairing, "players": list(session.scalars(select(PlayerMatch).where(
        PlayerMatch.team_match_id == pairing.id).order_by(PlayerMatch.position_label)))} for pairing in pairings]
    assigned = select(TeamMember.golfer_id).where(TeamMember.season_id == season.id)
    return request.app.state.templates.TemplateResponse(
        request, "admin/matches/substitute.html" if player is not None else "admin/matches/week.html",
        {"season": season, "week": week, "player": player, "matches": matches,
         "teams": list(session.scalars(select(Team).where(Team.season_id == season.id).order_by(Team.number))),
         "substitutes": list(session.scalars(select(Golfer).where(Golfer.is_active.is_(True), Golfer.id.not_in(assigned))
                                            .order_by(Golfer.last_name, Golfer.first_name, Golfer.id))),
         "snapshots": {s.golfer_id: s for s in session.scalars(select(WeekHandicap).where(WeekHandicap.week_id == week.id))},
         "errors": errors or {}, "notice": request.query_params.get("notice", ""),
         "csrf_token": generate_csrf_token(request.cookies.get("session") or "")}, status_code=status_code,
    )


@router.get("/admin/seasons/{season_id}/weeks/{week_id}/matches/new")
async def match_form(season_id: int, week_id: int, request: Request,
                     session: Session = Depends(get_session),  # noqa: B008
                     admin=Depends(require_admin)) -> Response:  # noqa: B008
    season, week = _week(session, season_id, week_id)
    return _render(request, session, season, week)


@router.post("/admin/seasons/{season_id}/weeks/{week_id}/matches/new")
async def create_match_submit(season_id: int, week_id: int, request: Request,
                              home_team_id: str = Form(""), away_team_id: str = Form(""),
                              csrf_token: str = Form(""),
                              session: Session = Depends(get_session),  # noqa: B008
                              admin=Depends(require_admin)) -> Response:  # noqa: B008
    _csrf(request, csrf_token)
    season, week = _week(session, season_id, week_id)
    try:
        _, _, warnings = generate_week_matches(session, season_id, week_id=week_id,
                                               home_team_id=_number(home_team_id, "home_team_id"),
                                               away_team_id=_number(away_team_id, "away_team_id"), clock=_clock)
    except MatchupValidationError as exc:
        return _render(request, session, season, week, errors=exc.errors, status_code=422)
    except MatchupConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    url = f"/admin/seasons/{season_id}/weeks/{week_id}/matches/new"
    if warnings:
        url += "?notice=" + quote(" ".join(warnings))
    return RedirectResponse(url, status_code=303)


@router.post("/admin/seasons/{season_id}/team-matches/{team_match_id}/generate")
async def generate_match_submit(season_id: int, team_match_id: int, request: Request,
                                csrf_token: str = Form(""),
                                session: Session = Depends(get_session),  # noqa: B008
                                admin=Depends(require_admin)) -> Response:  # noqa: B008
    _csrf(request, csrf_token)
    pairing = session.get(TeamMatch, team_match_id)
    if pairing is None:
        raise HTTPException(status_code=404, detail="Not found")
    season, week = _week(session, season_id, pairing.week_id)
    try:
        generate_week_matches(session, season_id, team_match_id=team_match_id, clock=_clock)
    except MatchupValidationError as exc:
        return _render(request, session, season, week, errors=exc.errors, status_code=422)
    except MatchupConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    return RedirectResponse(f"/admin/seasons/{season_id}/weeks/{week.id}/matches/new", status_code=303)


@router.get("/admin/seasons/{season_id}/player-matches/{player_match_id}/substitute")
async def substitute_form(season_id: int, player_match_id: int, request: Request,
                          session: Session = Depends(get_session),  # noqa: B008
                          admin=Depends(require_admin)) -> Response:  # noqa: B008
    season, week, player = _player(session, season_id, player_match_id)
    return _render(request, session, season, week, player=player)


@router.post("/admin/seasons/{season_id}/player-matches/{player_match_id}/substitute")
async def substitute_submit(season_id: int, player_match_id: int, request: Request,
                            side: str = Form(""), golfer_id: str = Form(""), csrf_token: str = Form(""),
                            session: Session = Depends(get_session),  # noqa: B008
                            admin=Depends(require_admin)) -> Response:  # noqa: B008
    _csrf(request, csrf_token)
    season, week, player = _player(session, season_id, player_match_id)
    try:
        substitute_player(session, season_id, player_match_id, side=side,
                          golfer_id=_number(golfer_id, "golfer_id"), clock=_clock)
    except MatchupValidationError as exc:
        return _render(request, session, season, week, player=player, errors=exc.errors, status_code=422)
    except MatchupConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    return RedirectResponse(f"/admin/seasons/{season_id}/weeks/{week.id}/matches/new", status_code=303)
