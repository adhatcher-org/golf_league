"""Player-facing Results pages: season and week point totals (R-RESULTS@1).

Any verified user can view a season's standings and any week within it. A
match with incomplete scores or a missing handicap snapshot is pending and
contributes no points, so it never appears in the totals. Responses carry
`Cache-Control: no-store`, and an unknown season is a 404.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import Season, User, Week
from golf_league.routers.scorecard_flow import _page
from golf_league.security import require_verified_user
from golf_league.services.scores import season_result, week_result
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
                         session: Session = Depends(get_session),  # noqa: B008
                         user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    """The season's points tables: every golfer and team, aggregated across weeks."""
    season = _season(session, season_id)
    weeks = list(session.scalars(
        select(Week).where(Week.season_id == season_id).order_by(Week.index)))
    result = season_result(session, season_id)
    return _page(request, "player/results.html", {
        "season": season, "weeks": weeks, "week": None, "result": result,
    })


@router.get("/seasons/{season_id}/results/weeks/{week_id}")
async def week_results(season_id: int, week_id: int, request: Request,
                       session: Session = Depends(get_session),  # noqa: B008
                       user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    """One week's points tables within a season."""
    season = _season(session, season_id)
    weeks = list(session.scalars(
        select(Week).where(Week.season_id == season_id).order_by(Week.index)))
    week = session.get(Week, week_id)
    if week is None or week.season_id != season_id:
        raise HTTPException(status_code=404, detail="Not found")
    result = week_result(session, week_id)
    return _page(request, "player/results.html", {
        "season": season, "weeks": weeks, "week": week, "result": result,
    })
