"""Player-facing Results pages: season and week point totals (R-RESULTS@1).

Any verified user can view a season's standings and any week within it. A
match with incomplete scores or a missing handicap snapshot is pending and
contributes no points, so it never appears in the totals. Responses carry
`Cache-Control: no-store`, and an unknown season is a 404.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import PlayerMatch, Season, TeamMatch, User, Week
from golf_league.routers.scorecard_flow import _page
from golf_league.security import require_verified_user
from golf_league.services.scores import (
    season_result,
    week_net_skins,
    week_result,
    week_skins,
)
from golf_league.services.seasons import get_season

logger = logging.getLogger(__name__)

router = APIRouter()


def _season(session: Session, season_id: int) -> Season:
    season = get_season(session, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Not found")
    return season


@router.get("/seasons/{season_id}/results")
async def season_results(season_id: int, request: Request,
                         total: Literal["gross", "net"] = "gross",
                         session: Session = Depends(get_session),  # noqa: B008
                         user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    """The season's points tables: every golfer and team, aggregated across weeks."""
    season = _season(session, season_id)
    weeks = list(session.scalars(
        select(Week).where(Week.season_id == season_id).order_by(Week.index)))
    result = season_result(session, season_id)
    return _page(request, "player/results.html", {
        "season": season, "weeks": weeks, "week": None, "result": result,
        "total": total,
    })


@router.get("/seasons/{season_id}/results/weeks/{week_id}")
async def week_results(season_id: int, week_id: int, request: Request,
                       total: Literal["gross", "net"] = "gross",
                       session: Session = Depends(get_session),  # noqa: B008
                       user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    """One week's points tables within a season.

    Shows the complete-match point leaders (reconciled with the season page),
    links to the week's scores page and its gross/net skins pages, and an
    explicit empty state when the week is cancelled, has no generated matches,
    or has matches but nothing scored completely yet.
    """
    season = _season(session, season_id)
    weeks = list(session.scalars(
        select(Week).where(Week.season_id == season_id).order_by(Week.index)))
    week = session.get(Week, week_id)
    if week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    result = week_result(session, week_id)

    generated = session.scalar(
        select(PlayerMatch.id)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id)
        .limit(1)
    ) is not None

    cancelled = week.status == "cancelled"
    ungenerated = not generated
    pending = generated and not result.players

    return _page(request, "player/results/week.html", {
        "season": season, "weeks": weeks, "week": week, "result": result,
        "cancelled": cancelled, "ungenerated": ungenerated, "pending": pending,
        "show_skins": generated and not cancelled,
        "total": total,
    })


@router.get("/seasons/{season_id}/results/weeks/{week_id}/skins")
async def week_skins_page(season_id: int, week_id: int, request: Request,
                          session: Session = Depends(get_session),  # noqa: B008
                          user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    """One week's gross-skins winners and ranked gross score grid (R-RESULTS@1).

    Verified users only; the response carries `Cache-Control: no-store`.
    """
    season = _season(session, season_id)
    week = session.get(Week, week_id)
    if week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    skins = week_skins(session, week_id)
    winner_names = {golfer.golfer_id: golfer.name for golfer in skins.golfers}
    return _page(request, "player/skins.html", {
        "season": season, "week": week, "skins": skins, "winner_names": winner_names,
    })


@router.get("/seasons/{season_id}/results/weeks/{week_id}/net-skins")
async def week_net_skins_page(season_id: int, week_id: int, request: Request,
                              session: Session = Depends(get_session),  # noqa: B008
                              user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    """One week's net-skins winners and ranked net score grid (R-RESULTS@1).

    Uses 100% of each golfer's historical signed nine-hole handicap snapshot,
    relative to the lowest snapshot in the field, with tie carryovers.
    Verified users only; the response carries `Cache-Control: no-store`.
    """
    season = _season(session, season_id)
    week = session.get(Week, week_id)
    if week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    skins = week_net_skins(session, week_id)
    winner_names = {golfer.golfer_id: golfer.name for golfer in skins.golfers}
    return _page(request, "player/net_skins.html", {
        "season": season, "week": week, "skins": skins, "winner_names": winner_names,
    })
