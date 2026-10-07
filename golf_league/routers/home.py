"""Verified users' read-only home page of upcoming league matches."""

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from golf_league.config import get_settings
from golf_league.database import get_session
from golf_league.models import User
from golf_league.security import get_optional_user
from golf_league.services.player_schedule import get_home_view

router = APIRouter()


@router.get("/")
async def home(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    user: User | None = Depends(get_optional_user),  # noqa: B008
) -> Response:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    if user is None or (settings.email_verification_required and user.email_verified_at is None):
        return RedirectResponse(url="/login?next=/", status_code=303)
    season, week_view = get_home_view(session, date.today())
    return request.app.state.templates.TemplateResponse(
        request, "player/home.html", {"season": season, "week_view": week_view},
    )
