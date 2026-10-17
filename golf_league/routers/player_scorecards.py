"""A player's own scorecard: upload or type it, review it, submit it (R-SCORES, R-OCR-PROVIDER).

The same draft pages as the admin's, in the player's addresses, through
`scorecard_flow` with the player flow. A player opens only a card they
created: any other draft address is 404 to them, admins included. The server
decides what a submitted card may write (`submit_card` with the player as
`Actor(user.id, False, user.golfer_id)`): the player's own match and at most
one other match in the week. Photo uploads are limited per player and while
the reader is busy; typed cards are not. No handler calls the provider, and
nothing here logs a name, a score or image data.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from golf_league.database import get_session
from golf_league.models import ScorecardDraft, User
from golf_league.routers import scorecard_flow
from golf_league.routers.player_scores import CLOSED, _week, entry_page, opens_sentence
from golf_league.routers.scorecard_flow import (
    IMAGE_MESSAGES,
    PRIVATE_HEADERS,
    Flow,
    _actor,
    _csrf,
    _not_found,
    _now,
    _text,
    _today,
    declared_too_large,
    purge_drafts,
)
from golf_league.security import require_verified_user
from golf_league.services.scorecard_drafts import (
    count_waiting,
    create_draft_from_image,
    create_manual_draft,
    delete_draft,
    drafts_ahead,
    open_photo_draft,
)
from golf_league.services.scorecard_image import ImageRefused
from golf_league.services.scores import ScoreEntryError, player_entry_state, submit_card

logger = logging.getLogger(__name__)

router = APIRouter()

FLOW = Flow("/scorecards", "player/scorecards", False)
# More cards than this waiting to be read, and a new photo would wait too long.
BUSY_AT = 8
_HOURLY = "You have uploaded several photos in the last hour. Type your scores instead, or try again later."
_NOT_SET_UP = "Photo reading is not set up. Type the card instead."


def _draft_path(draft: ScorecardDraft) -> str:
    return f"{FLOW.base_path}/{draft.id}"


def _to(path: str) -> Response:
    return RedirectResponse(path, status_code=303, headers=PRIVATE_HEADERS)


class _Refused(Exception):
    """A start-of-card request refused with a message for the entry page."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        self.message, self.status_code = message, status_code
        super().__init__(message)


class _OpenCard(Exception):
    """The player already has a photo card open: go there instead."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(path)


async def _start(request: Request, session: Session, week, user: User, form) -> str:
    """Steps 4 to 10 of starting a card; returns where to go next."""
    today, now, state = _today(request), _now(request), request.app.state
    if _text(form, "manual") == "1":  # typed cards are not limited
        draft = create_manual_draft(session, week_id=week.id, user_id=user.id, today=today,
                                    clock=state.scorecard_utc_now, actor=_actor(user, FLOW))
        return _draft_path(draft)
    if state.vision_provider is None:
        raise _Refused(_NOT_SET_UP)
    photo = form.get("photo")
    if not isinstance(photo, UploadFile) or not photo.filename:
        raise _Refused("Choose a photo of the scorecard.")

    def admit() -> None:
        """Steps 7 to 9, run under the prepare lock so overlapping uploads are counted one at a time."""
        purge_drafts(request, session)
        waiting = open_photo_draft(session, user.id, now)
        if waiting is not None:  # one photo card at a time
            raise _OpenCard(_draft_path(waiting))
        if not state.scorecard_upload_limiter.check(str(user.id)):
            raise _Refused(_HOURLY, 409)
        busy = count_waiting(session, now)
        if busy >= BUSY_AT:
            raise _Refused(f"The photo reader is busy: {busy} cards are waiting. Type your scores instead, "
                           "or try the photo again in a few minutes.", 409)

    try:
        draft = await scorecard_flow.start_photo_draft(
            request, session, week_id=week.id, user=user, flow=FLOW, photo=photo,
            create_draft_from_image=create_draft_from_image, admit=admit)
    except _OpenCard as exc:
        return exc.path
    logger.info("player scorecard started draft=%d week=%d user=%d", draft.id, week.id, user.id)
    return _draft_path(draft)


@router.post("/weeks/{week_id}/scorecards")
async def scorecard_start(week_id: int, request: Request,
                          session: Session = Depends(get_session),  # noqa: B008
                          user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    """Start a card. The body is not read before the access check, and the steps run in this order."""
    week = _week(session, week_id)

    def refused(message: str, status_code: int = 422) -> Response:
        return entry_page(request, session, week, user, errors={"photo": message}, status_code=status_code)

    if await declared_too_large(request):
        return refused(IMAGE_MESSAGES["too_large"])
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    entry = player_entry_state(session, week.id, user.golfer_id, _today(request))
    if entry.state == "not_playing":
        raise _not_found()
    if entry.state in ("not_open_yet", "closed"):
        return refused(opens_sentence(week) if entry.state == "not_open_yet" else CLOSED)
    try:
        return _to(await _start(request, session, week, user, form))
    except _Refused as exc:
        return refused(exc.message, exc.status_code)
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code in ("not_found", "not_allowed"):
            raise _not_found() from exc
        return refused(exc.message)
    except ImageRefused as exc:
        session.rollback()
        return refused(IMAGE_MESSAGES.get(exc.code, IMAGE_MESSAGES["unsupported_type"]))
    except (SQLAlchemyError, OSError) as exc:
        session.rollback()
        logger.warning("player scorecard not started week=%d type=%s", week.id, type(exc).__name__)
        return refused("The card could not be started just now. Nothing was saved; try again.", 503)


def _waiting(session: Session):
    def extra(draft: ScorecardDraft) -> dict:
        ahead = drafts_ahead(session, draft)
        # "Taking longer than expected" only makes sense when nobody is ahead in the queue.
        return {"ahead": ahead, **({"slow": False} if ahead else {})}
    return extra


@router.get("/scorecards/{draft_id}")
async def scorecard_detail(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return scorecard_flow.detail(request, session, draft_id, user, FLOW, purge=purge_drafts,
                                 waiting=_waiting(session))


@router.get("/scorecards/{draft_id}/status")
async def scorecard_status(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return scorecard_flow.status(request, session, draft_id, user, FLOW)


@router.get("/scorecards/{draft_id}/image")
async def scorecard_image(draft_id: int, request: Request,
                          session: Session = Depends(get_session),  # noqa: B008
                          user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return scorecard_flow.image(request, session, draft_id, user, FLOW)


@router.post("/scorecards/{draft_id}/submit")
async def scorecard_submit(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return await scorecard_flow.submit(request, session, draft_id, user, FLOW, submit_card=submit_card,
                                       delete_draft=delete_draft)


@router.post("/scorecards/{draft_id}/discard")
async def scorecard_discard(draft_id: int, request: Request,
                            session: Session = Depends(get_session),  # noqa: B008
                            user: User = Depends(require_verified_user)) -> Response:  # noqa: B008
    return await scorecard_flow.discard(request, session, draft_id, user, FLOW)
