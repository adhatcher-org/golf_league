"""Verified player's privacy-safe roster view."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import Golfer, User
from golf_league.security import get_optional_verified_user, require_verified_user
from golf_league.services.roster import list_active_roster_players

router = APIRouter()


@router.get("/roster")
async def player_roster(
    request: Request,
    season_id: int | None = None,
    session: Session = Depends(get_session),  # noqa: B008
    user: User | None = Depends(get_optional_verified_user),  # noqa: B008
) -> Response:
    if user is None:
        return RedirectResponse(url="/login?next=/roster", status_code=303)
    players = list_active_roster_players(session, season_id=season_id)
    return request.app.state.templates.TemplateResponse(
        request, "roster.html", {"players": players, "season_id": season_id}
    )


@router.get("/roster/golfers/{golfer_id}")
async def roster_golfer_detail(
    golfer_id: str,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_verified_user),  # noqa: B008
) -> Response:
    """Contact + handicap detail for one active golfer (JSON only).

    Fetched on demand by the roster detail dialog. The roster HTML itself
    never renders email/phone, so this endpoint is the only way to reach
    contact details and it requires a verified session.
    """
    normalized = golfer_id.lstrip("0")
    if (
        not golfer_id.isascii()
        or not golfer_id.isdecimal()
        or not normalized
        or len(normalized) > 19
    ):
        raise HTTPException(status_code=404, detail="Golfer not found")
    golfer = session.get(Golfer, int(normalized))
    if golfer is None or not golfer.is_active:
        raise HTTPException(status_code=404, detail="Golfer not found")
    return JSONResponse(
        {
            "name": f"{golfer.first_name} {golfer.last_name}",
            "email": golfer.email,
            "phone": golfer.phone,
            "handicap_strokes": golfer.handicap_strokes,
            "tee_color": golfer.default_tee_label,
        },
        headers={"Cache-Control": "no-store"},
    )
