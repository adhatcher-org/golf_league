"""Player round history routes.

Verified-user read-only route listing a player's own past rounds with gross strokes
by hole, net total (gross - WeekHandicap.strokes), and match result.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import User, Week
from golf_league.security import require_verified_user
from golf_league.services.player_rounds import get_player_rounds

router = APIRouter()


@router.get("/my/rounds")
async def player_rounds(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_verified_user),  # noqa: B008
) -> Response:
    """List the player's own past rounds with scores and results.

    Returns a page showing each past match the player participated in, with:
    - Gross strokes by hole
    - Net total (gross - WeekHandicap.strokes)
    - Match result and points when both sides can be scored

    Only accessible to verified users. No write operations allowed.
    """
    if user.golfer_id is None:
        raise HTTPException(status_code=403, detail="Player not linked to a golfer",
                            headers={"Cache-Control": "no-store"})

    rounds = get_player_rounds(session, user.golfer_id)
    response = request.app.state.templates.TemplateResponse(
        request,
        "player/rounds.html",
        {"rounds": rounds},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/my/rounds/{week_id}")
async def player_rounds_week(
    week_id: int,
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
    user: User = Depends(require_verified_user),  # noqa: B008
) -> Response:
    """Show the linked player's saved rounds for one known week."""
    if user.golfer_id is None:
        raise HTTPException(status_code=403, detail="Player not linked to a golfer",
                            headers={"Cache-Control": "no-store"})
    if session.get(Week, week_id) is None:
        raise HTTPException(status_code=404, detail="Week not found",
                            headers={"Cache-Control": "no-store"})
    response = request.app.state.templates.TemplateResponse(
        request, "player/rounds.html",
        {"rounds": get_player_rounds(session, user.golfer_id, week_id)},
    )
    response.headers["Cache-Control"] = "no-store"
    return response
