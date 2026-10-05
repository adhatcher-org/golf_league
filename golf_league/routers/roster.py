"""Verified player's privacy-safe roster view."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import User
from golf_league.security import get_optional_verified_user
from golf_league.services.roster import list_active_roster_players

router = APIRouter()


@router.get("/roster")
async def player_roster(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    user: User | None = Depends(get_optional_verified_user),  # noqa: B008
) -> Response:
    if user is None:
        return RedirectResponse(url="/login?next=/roster", status_code=303)
    players = list_active_roster_players(session)
    return request.app.state.templates.TemplateResponse(
        request, "roster.html", {"players": players}
    )
