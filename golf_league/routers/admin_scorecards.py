"""Admin pages for scorecard photo entry (R-SCORES, R-OCR-PROVIDER).

An admin picks a week, uploads a photo (or starts a manual card), corrects what
was read and submits it. Every route requires a verified admin, every POST a
CSRF token, and every rule is enforced again on the server. No handler calls
the provider: reading happens on the background worker only. Responses are
`Cache-Control: no-store` because they show player names, and nothing here logs
a name, a score or image data.
"""

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from golf_league.config import get_settings
from golf_league.database import get_session
from golf_league.domain.scorecard import Actor
from golf_league.models import HoleScore, PlayerMatch, ScorecardDraft, TeamMatch, Week
from golf_league.security import generate_csrf_token, require_admin, validate_csrf
from golf_league.services.player_schedule import select_player_season
from golf_league.services.scorecard_drafts import (
    create_draft,
    create_manual_draft,
    delete_draft,
    discard_draft,
    draft_card,
    get_draft,
    image_file,
    list_open_drafts,
    purge_expired,
    start_extraction,
)
from golf_league.services.scorecard_image import MAX_UPLOAD_BYTES, ImageRefused
from golf_league.services.scorecard_review import (
    ACK_FIELD,
    CARD_ERROR,
    acknowledgement_token,
    build_review,
    field_names,
    golfer_options,
    parse_submission,
    unpaired_on_card,
)
from golf_league.services.scorecard_vision import league_timezone_name
from golf_league.services.scores import (
    ScoreEntryError,
    blocked_golfers,
    default_week,
    eligible_weeks,
    league_today,
    submit_card,
    week_holes,
)

logger = logging.getLogger(__name__)

router = APIRouter()

NEW_PATH = "/admin/scorecards/new"
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# Room for the multipart framing and the other fields around a 10 MB photo.
_FORM_OVERHEAD = 64 * 1024
# A read normally finishes within the provider's 180-second limit; past this the
# status page says so and offers a way out, without changing the draft.
SLOW_READ_AFTER = timedelta(minutes=5)

_IMAGE_MESSAGES = {
    "too_large": "The photo is larger than 10 MB. Choose a smaller photo.",
    "unsupported_type": "The photo could not be opened. Upload a JPEG, PNG or HEIC photo.",
    "too_many_pixels": "The photo has too many pixels. Choose a photo of 50 megapixels or less.",
}
_FAILURE_MESSAGES = {
    "provider_timeout": "The photo reader took too long to answer.",
    "provider_unavailable": "The photo reader could not be reached.",
    "provider_http_status": "The photo reader could not be reached.",
    "provider_bad_url": "The photo reader could not be reached.",
    "interrupted": "The application restarted while the photo was being read.",
    "draft_unreadable": "The stored photo could not be opened.",
}
_UNREADABLE = "The photo could not be read."


def _settings(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


def _now(request: Request):
    return request.app.state.scorecard_utc_now()


def _today(request: Request):
    return league_today(request.app.state.scorecard_utc_now, league_timezone_name(_settings(request)))


def _csrf(request: Request, token: object) -> None:
    if not isinstance(token, str) or not validate_csrf(request.cookies.get("session") or "", token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _text(form, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


def _number(text: str) -> int | None:
    """A positive whole number from a form or query value, else None; never raises."""
    text = text.strip()
    if not (text.isascii() and text.isdecimal()) or len(text) > 18:
        return None
    return int(text) or None


def _page(request: Request, template: str, context: dict, status_code: int = 200) -> Response:
    context = {**context, "csrf_token": generate_csrf_token(request.cookies.get("session") or "")}
    return request.app.state.templates.TemplateResponse(
        request, template, context, status_code=status_code, headers=PRIVATE_HEADERS,
    )


def _purge(request: Request, session: Session) -> None:
    """Retention housekeeping on the pages that list or open drafts; never fails the page."""
    try:
        purge_expired(session, _now(request), directory=request.app.state.scorecard_dir)
    except Exception as exc:
        session.rollback()
        logger.warning("scorecard draft purge failed type=%s", type(exc).__name__)


def _open_draft(request: Request, session: Session, draft_id: int) -> ScorecardDraft:
    """The draft, or 404 when it is gone or past its 24 hours."""
    draft = get_draft(session, draft_id)
    if draft is None or draft.expires_at <= _now(request):
        raise HTTPException(status_code=404, detail="Not found")
    return draft


# --- start a card -----------------------------------------------------------


def _scored_weeks(session: Session, season_id: int) -> list[Week]:
    return list(session.scalars(
        select(Week).where(Week.season_id == season_id, Week.id.in_(
            select(TeamMatch.week_id).join(PlayerMatch, PlayerMatch.team_match_id == TeamMatch.id)
            .join(HoleScore, HoleScore.player_match_id == PlayerMatch.id)
        )).order_by(Week.index)
    ))


def _new_page(request: Request, session: Session, admin, *, errors: dict[str, str] | None = None,
              week_id: int | None = None, status_code: int = 200) -> Response:
    season = select_player_season(session)
    today = _today(request)
    weeks = eligible_weeks(session, season.id, today, Actor(admin.id, True)) if season else []
    chosen = week_id if week_id in {week.id for week in weeks} else None
    if chosen is None and season is not None:
        default = default_week(session, season.id, today)
        chosen = default.id if default else None
    week_by_id = {week.id: week for week in session.scalars(select(Week))} if season else {}
    drafts = [{"draft": draft, "week": week_by_id.get(draft.week_id)}
              for draft in list_open_drafts(session, admin.id) if draft.expires_at > _now(request)]
    return _page(request, "admin/scorecards/new.html", {
        "season": season, "weeks": weeks, "chosen_week_id": chosen, "drafts": drafts,
        "configured": request.app.state.vision_provider is not None,
        "scored_weeks": _scored_weeks(session, season.id) if season else [],
        "errors": errors or {}, "field_ids": {"week_id": "week_id", "photo": "photo"},
        "discarded": request.query_params.get("discarded") == "1",
    }, status_code)


@router.get(NEW_PATH)
async def scorecard_new(request: Request,
                        session: Session = Depends(get_session),  # noqa: B008
                        admin=Depends(require_admin)) -> Response:  # noqa: B008
    _purge(request, session)
    return _new_page(request, session, admin, week_id=_number(request.query_params.get("week_id", "")))


class _Refused(Exception):
    """A start-of-card request refused with a message for one field."""

    def __init__(self, field: str, message: str) -> None:
        self.field, self.message = field, message
        super().__init__(field)


async def _declared_too_large(request: Request) -> bool:
    """True, with the body drained and discarded, when the upload is plainly over the limit."""
    declared = request.headers.get("content-length", "")
    if not declared.isdecimal() or int(declared) <= MAX_UPLOAD_BYTES + _FORM_OVERHEAD:
        return False
    async for _ in request.stream():
        pass
    return True


async def _start_draft(request: Request, session: Session, admin, form, week_id: int) -> ScorecardDraft:
    today, clock, state = _today(request), request.app.state.scorecard_utc_now, request.app.state
    if _text(form, "manual") == "1":
        return create_manual_draft(session, week_id=week_id, user_id=admin.id, today=today, clock=clock,
                                   actor=Actor(admin.id, True))
    if state.vision_provider is None:
        raise _Refused("photo", "Photo reading is not set up. Enter the scores manually.")
    photo = form.get("photo")
    if not isinstance(photo, UploadFile) or not photo.filename:
        raise _Refused("photo", "Choose a photo of the scorecard.")
    raw = await photo.read(MAX_UPLOAD_BYTES + 1)
    draft = create_draft(session, week_id=week_id, user_id=admin.id, raw=raw, today=today, clock=clock,
                         directory=state.scorecard_dir, actor=Actor(admin.id, True))
    start_extraction(state, draft.id)
    return draft


@router.post("/admin/scorecards")
async def scorecard_create(request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    # The body is not parsed until the admin check has passed, and an upload
    # that is plainly too large is refused without being stored.
    if await _declared_too_large(request):
        return _new_page(request, session, admin, errors={"photo": _IMAGE_MESSAGES["too_large"]},
                         status_code=422)
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    week_id = _number(_text(form, "week_id"))
    if week_id is None:
        return _new_page(request, session, admin, errors={"week_id": "Choose a week."}, status_code=422)
    try:
        draft = await _start_draft(request, session, admin, form, week_id)
    except ScoreEntryError as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="Not found") from exc
        errors = {"week_id": exc.message}
    except ImageRefused as exc:
        errors = {"photo": _IMAGE_MESSAGES.get(exc.code, _IMAGE_MESSAGES["unsupported_type"])}
    except _Refused as exc:
        errors = {exc.field: exc.message}
    except (SQLAlchemyError, OSError) as exc:
        # A busy database or a data directory that cannot be written: nothing was kept.
        session.rollback()
        logger.warning("scorecard draft not created type=%s", type(exc).__name__)
        return _new_page(request, session, admin, week_id=week_id, status_code=503, errors={
            "photo": "The card could not be started just now. Nothing was saved; try again."})
    else:
        return RedirectResponse(f"/admin/scorecards/{draft.id}", status_code=303, headers=PRIVATE_HEADERS)
    session.rollback()
    return _new_page(request, session, admin, errors=errors, week_id=week_id, status_code=422)


# --- one card ---------------------------------------------------------------


def _review_page(request: Request, session: Session, draft: ScorecardDraft, *,
                 values: dict[str, str] | None = None, errors: dict[str, str] | None = None,
                 status_code: int = 200) -> Response:
    week = session.get(Week, draft.week_id)
    hole_numbers = [hole.number for hole in week_holes(session, week.id)]
    options = golfer_options(session, week.id)
    errors = errors or {}
    # A card only adds: golfers already saved, or in a locked match, are shown as left off.
    blocked = blocked_golfers(session, week.id, [option.golfer_id for option in options])
    view = build_review(draft_card(draft) if draft.status == "ready" else None, week, hole_numbers,
                        options, values, frozenset(errors), blocked)
    path = image_file(request.app.state.scorecard_dir, draft.image_path)
    return _page(request, "admin/scorecards/review.html", {
        "draft": draft, "week": week, "view": view, "errors": errors,
        "field_ids": {name: name for name in field_names(hole_numbers)},
        "has_photo": path is not None and path.is_file(),
    }, status_code)


@router.get("/admin/scorecards/{draft_id}")
async def scorecard_detail(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    _purge(request, session)
    draft = _open_draft(request, session, draft_id)
    week = session.get(Week, draft.week_id)
    if draft.status == "extracting":
        return _page(request, "admin/scorecards/status.html", {
            "draft": draft, "week": week, "slow": _now(request) - draft.created_at > SLOW_READ_AFTER,
        })
    if draft.status == "failed" and request.query_params.get("manual") != "1":
        return _page(request, "admin/scorecards/failed.html", {
            "draft": draft, "week": week, "reason": _FAILURE_MESSAGES.get(draft.error_code or "", _UNREADABLE),
        })
    return _review_page(request, session, draft)


@router.get("/admin/scorecards/{draft_id}/status")
async def scorecard_status(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    draft = _open_draft(request, session, draft_id)
    return JSONResponse({"status": draft.status, "error_code": draft.error_code}, headers=PRIVATE_HEADERS)


@router.get("/admin/scorecards/{draft_id}/image")
async def scorecard_image(draft_id: int, request: Request,
                          session: Session = Depends(get_session),  # noqa: B008
                          admin=Depends(require_admin)) -> Response:  # noqa: B008
    draft = _open_draft(request, session, draft_id)
    path = image_file(request.app.state.scorecard_dir, draft.image_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return Response(path.read_bytes(), media_type="image/jpeg",
                    headers={**PRIVATE_HEADERS, "X-Content-Type-Options": "nosniff"})


def _refusal(exc: ScoreEntryError, names: dict[int, str]) -> tuple[int, dict[str, str]]:
    """Status and message for a card the score service refused."""
    if exc.code == "nothing_left":
        return 409, {CARD_ERROR: "Every player on this card already has saved scores or is in a locked "
                                 "match. Nothing was saved."}
    if exc.code == "left_off":
        left = [names[golfer_id] for golfer_id in exc.golfer_ids if golfer_id in names]
        if not left:
            return 409, {CARD_ERROR: "The players left off this card have changed. Nothing was saved. "
                                     "Check the card and submit again."}
        return 409, {CARD_ERROR: ", ".join(left) + (" already has" if len(left) == 1 else " already have")
                                 + " saved scores or are in a locked match, and are left off this card. "
                                 "Nothing was saved. Check the remaining players and submit again."}
    if exc.code == "conflict":
        return 409, {CARD_ERROR: "The schedule was being changed at the same moment. Nothing was saved; "
                                 "submit the card again."}
    if exc.code == "needs_acknowledgement":
        return 422, {ACK_FIELD: "Tick the box to confirm the players who have no opponent on this card."}
    return 422, {CARD_ERROR: exc.message}


@router.post("/admin/scorecards/{draft_id}/submit")
async def scorecard_submit(draft_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    draft = _open_draft(request, session, draft_id)
    if draft.status == "extracting":
        raise HTTPException(status_code=409, detail="The photo is still being read.")
    week_id, user_id = draft.week_id, admin.id
    hole_numbers = [hole.number for hole in week_holes(session, week_id)]
    options = golfer_options(session, week_id)
    values = {name: _text(form, name) for name in field_names(hole_numbers)}
    submission = parse_submission(values, hole_numbers, options)
    errors = dict(submission.errors)
    unpaired = [] if errors else unpaired_on_card(submission, options)
    # The tick counts only for the golfers it was shown beside.
    acknowledged = bool(unpaired) and submission.acknowledgement == acknowledgement_token(
        [option.golfer_id for option in unpaired])
    if unpaired and not acknowledged:
        names = [option.name for option in unpaired]
        errors[ACK_FIELD] = ("Tick the box to confirm: " + ", ".join(names)
                             + (" is" if len(names) == 1 else " are")
                             + " not paired with an opponent on this card.")
    if errors:
        return _review_page(request, session, draft, values=values, errors=errors, status_code=422)

    today, now, directory = _today(request), _now(request), request.app.state.scorecard_dir
    names = {option.golfer_id: option.name for option in options}
    # End the read transaction: `submit_card` reserves the single writer as its
    # first action and re-checks everything inside that one write transaction.
    session.rollback()
    try:
        result = submit_card(session, week_id, list(submission.entries), Actor(user_id, True), today,
                             acknowledged=acknowledged, left_off=submission.left_off, now=now)
        stored = session.get(ScorecardDraft, draft_id)
        if stored is None:
            session.rollback()
            raise HTTPException(status_code=404, detail="Not found")
        # Success deletes the draft and its photo in the same request and transaction.
        delete_draft(session, stored, directory=directory)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="Not found") from exc
        status_code, errors = _refusal(exc, names)
        kept = _open_draft(request, session, draft_id)  # a refused submit keeps the draft
        return _review_page(request, session, kept, values=values, errors=errors, status_code=status_code)
    except (SQLAlchemyError, OSError) as exc:
        # The database or the photo could not be written. No score was saved;
        # the form comes back with what the admin typed instead of an error page.
        session.rollback()
        logger.warning("scorecard submit failed draft=%d type=%s", draft_id, type(exc).__name__)
        kept = _open_draft(request, session, draft_id)
        return _review_page(request, session, kept, values=values, status_code=503, errors={
            CARD_ERROR: "The scores could not be saved just now. Nothing was saved; submit the card again."})
    logger.info("scorecard submitted draft=%d week=%d", draft_id, week_id)
    # The week page says how many matches the card wrote to: one card may span two.
    return RedirectResponse(f"/admin/weeks/{week_id}/scores?saved={len(result.player_match_ids)}",
                            status_code=303, headers=PRIVATE_HEADERS)


@router.post("/admin/scorecards/{draft_id}/discard")
async def scorecard_discard(draft_id: int, request: Request,
                            session: Session = Depends(get_session),  # noqa: B008
                            admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    draft = get_draft(session, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Not found")
    week_id = draft.week_id
    discard_draft(session, draft_id, directory=request.app.state.scorecard_dir)
    target = f"{NEW_PATH}?week_id={week_id}" if _text(form, "retry") == "1" else f"{NEW_PATH}?discarded=1"
    return RedirectResponse(target, status_code=303, headers=PRIVATE_HEADERS)
