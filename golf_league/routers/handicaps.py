"""Verified-user, read-only handicap trend view."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import User
from golf_league.security import require_admin, require_verified_user
from golf_league.services.handicaps import get_handicap_trend

router = APIRouter()


@router.get("/handicaps/trend")
async def my_handicap_trend(
    request: Request,
    season_id: int | None = None,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_verified_user),  # noqa: B008
) -> Response:
    """A verified player's own handicap trend across weeks.

    The player may view only their own linked golfer's trend. Admin may view
    any golfer's trend through the admin route.
    """
    if user.golfer_id is None:
        raise HTTPException(status_code=403, detail="Player not linked to a golfer")

    trend = get_handicap_trend(session, user.golfer_id, season_id=season_id)
    if trend is None:
        raise HTTPException(status_code=404, detail="Golfer not found")

    response = request.app.state.templates.TemplateResponse(
        request, "player/handicap_trend.html", {"trend": trend, "season_id": season_id}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/handicaps/{golfer_id}")
async def golfer_handicap_trend(
    golfer_id: int,
    request: Request,
    season_id: int | None = None,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_admin),  # noqa: B008
) -> Response:
    """Admin-only view of any golfer's handicap trend.

    Only admins may view other golfers' trends.
    """
    trend = get_handicap_trend(session, golfer_id, season_id=season_id)
    if trend is None:
        raise HTTPException(status_code=404, detail="Golfer not found")

    response = request.app.state.templates.TemplateResponse(
        request, "admin/handicap_trend.html", {"trend": trend, "season_id": season_id}
    )
    response.headers["Cache-Control"] = "no-store"
    return response
