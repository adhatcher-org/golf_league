"""Verified player schedule and read-only weekly matchup pages."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import User
from golf_league.security import require_verified_user
from golf_league.services.player_schedule import (
    get_opponent_contact,
    get_player_schedule,
    get_week_detail,
)

router = APIRouter()


@router.get("/my/schedule")
async def my_schedule(
    request: Request,
    season_id: int | None = None,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_verified_user),  # noqa: B008
) -> Response:
    projection = get_player_schedule(session, user.golfer_id or -1, season_id=season_id)
    if season_id is not None and projection.season is None:
        raise HTTPException(status_code=404, detail="Season not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "player/my_schedule.html",
        {"projection": projection, "user_golfer_id": user.golfer_id},
    )


@router.get("/weeks/{week_id}/opponents/{golfer_id}")
async def opponent_contact(
    week_id: str,
    golfer_id: str,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_verified_user),  # noqa: B008
) -> Response:
    ids = []
    for value in (week_id, golfer_id):
        # Bound the text before conversion, including arbitrarily long decimals.
        normalized = value.lstrip("0")
        if (not value.isascii() or not value.isdecimal()
                or not normalized or len(normalized) > 19):
            raise HTTPException(status_code=404, detail="Opponent not found")
        ids.append(int(normalized))
    contact = get_opponent_contact(session, user.golfer_id, *ids)
    if contact is None:
        raise HTTPException(status_code=404, detail="Opponent not found")
    week, golfer = contact
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {
                "name": f"{golfer.first_name} {golfer.last_name}",
                "email": golfer.email,
                "phone": golfer.phone,
                "week_index": week.index,
                "week_date": week.play_date.strftime("%b %-d, %Y"),
            },
            headers={"Cache-Control": "no-store"},
        )
    response = request.app.state.templates.TemplateResponse(
        request,
        "player/opponent_contact.html",
        {"week": week, "golfer": golfer},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/weeks/{week_id}")
async def week_detail(
    week_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_verified_user),  # noqa: B008
) -> Response:
    detail = get_week_detail(session, week_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Week not found")
    season, seasons, week_view = detail
    return request.app.state.templates.TemplateResponse(
        request,
        "player/week.html",
        {"season": season, "seasons": seasons, "week_view": week_view, "is_admin": user.is_admin},
    )
