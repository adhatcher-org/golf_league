"""Player round history routes.

Verified-user read-only route listing a player's own past rounds with gross strokes
by hole, net total (gross - WeekHandicap.strokes), and match result.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.models import User
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
    - Match result (win/loss/tie)

    Only accessible to verified users. No write operations allowed.
    """
    if user.golfer_id is None:
        raise HTTPException(status_code=403, detail="Player not linked to a golfer")

    rounds = get_player_rounds(session, user.golfer_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "player/rounds.html",
        {"rounds": rounds},
    )
