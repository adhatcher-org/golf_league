"""The scorecard process shared by the admin and player pages (R-SCORES, R-OCR-PROVIDER).

A card is a short-lived draft: a photo being read, a reading to review, or a
typed card. The same five draft pages serve both flows (the page, its status,
its photo, submit and discard), parameterised by a `Flow`. Who may open a
draft is decided by `open_draft` alone: in the admin flow any admin, in the
player flow only the player who created it. No handler calls the provider:
reading happens on the background worker only. Responses are
`Cache-Control: no-store`, and nothing here logs a name, a score or image data.

A router passes in the services its handlers call (`purge`, `submit_card`,
`delete_draft`), so each router's own module names are the ones used.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from golf_league.config import get_settings
from golf_league.domain.scorecard import Actor
from golf_league.models import ScorecardDraft, User, Week
from golf_league.security import generate_csrf_token, validate_csrf
from golf_league.services.scorecard_drafts import (
    discard_draft,
    draft_card,
    get_draft,
    image_file,
)
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
    league_today,
    week_holes,
)

logger = logging.getLogger(__name__)

PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# A read normally finishes within the provider's 180-second limit; past this the
# status page says so and offers a way out, without changing the draft.
SLOW_READ_AFTER = timedelta(minutes=5)

_FAILURE_MESSAGES = {
    "provider_timeout": "The photo reader took too long to answer.",
    "provider_unavailable": "The photo reader could not be reached.",
    "provider_http_status": "The photo reader could not be reached.",
    "provider_bad_url": "The photo reader could not be reached.",
    "interrupted": "The application restarted while the photo was being read.",
    "draft_unreadable": "The stored photo could not be opened.",
}
_UNREADABLE = "The photo could not be read."


@dataclass(frozen=True)
class Flow:
    """Where a flow's pages live and whether it acts as an admin."""

    base_path: str  # "/admin/scorecards" or "/scorecards"
    template_dir: str  # "admin/scorecards" or "player/scorecards"
    is_admin: bool


# --- request helpers shared by the score pages ----------------------------------


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


def _page(request: Request, template: str, context: dict, status_code: int = 200) -> Response:
    context = {**context, "csrf_token": generate_csrf_token(request.cookies.get("session") or "")}
    return request.app.state.templates.TemplateResponse(
        request, template, context, status_code=status_code, headers=PRIVATE_HEADERS,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found", headers=PRIVATE_HEADERS)


def _actor(user: User, flow: Flow) -> Actor:
    return Actor(user.id, True) if flow.is_admin else Actor(user.id, False, user.golfer_id)


# --- who may open a draft ----------------------------------------------------------


def _owned(draft: ScorecardDraft, user: User, flow: Flow) -> bool:
    """The admin flow's routes already required an admin; a player sees only their own card."""
    return flow.is_admin or draft.created_by_user_id == user.id


def open_draft(request: Request, session: Session, draft_id: int, user: User, flow: Flow) -> ScorecardDraft:
    """The draft, or 404 when it is gone, past its 24 hours, or not this person's to open."""
    draft = get_draft(session, draft_id)
    if draft is None or draft.expires_at <= _now(request) or not _owned(draft, user, flow):
        raise _not_found()
    return draft


# --- the pages ----------------------------------------------------------------------


def review_page(request: Request, session: Session, draft: ScorecardDraft, flow: Flow, *,
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
    return _page(request, f"{flow.template_dir}/review.html", {
        "draft": draft, "week": week, "view": view, "errors": errors, "base_path": flow.base_path,
        "field_ids": {name: name for name in field_names(hole_numbers)},
        "has_photo": path is not None and path.is_file(),
    }, status_code)


def detail(request: Request, session: Session, draft_id: int, user: User, flow: Flow, *,
           purge: Callable[[Request, Session], None], waiting: dict | None = None) -> Response:
    """The waiting page, the failed page or the review form. `waiting` adds to the waiting page."""
    purge(request, session)
    draft = open_draft(request, session, draft_id, user, flow)
    week = session.get(Week, draft.week_id)
    if draft.status == "extracting":
        return _page(request, f"{flow.template_dir}/status.html", {
            "draft": draft, "week": week, "base_path": flow.base_path,
            "slow": _now(request) - draft.created_at > SLOW_READ_AFTER, **(waiting or {}),
        })
    if draft.status == "failed" and request.query_params.get("manual") != "1":
        return _page(request, f"{flow.template_dir}/failed.html", {
            "draft": draft, "week": week, "base_path": flow.base_path,
            "reason": _FAILURE_MESSAGES.get(draft.error_code or "", _UNREADABLE),
        })
    return review_page(request, session, draft, flow)


def status(request: Request, session: Session, draft_id: int, user: User, flow: Flow) -> Response:
    draft = open_draft(request, session, draft_id, user, flow)
    return JSONResponse({"status": draft.status, "error_code": draft.error_code}, headers=PRIVATE_HEADERS)


def image(request: Request, session: Session, draft_id: int, user: User, flow: Flow) -> Response:
    draft = open_draft(request, session, draft_id, user, flow)
    path = image_file(request.app.state.scorecard_dir, draft.image_path)
    if path is None or not path.is_file():
        raise _not_found()
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


def _saved_path(flow: Flow, week_id: int, match_count: int) -> str:
    if flow.is_admin:
        # The week page says how many matches the card wrote to: one card may span two.
        return f"/admin/weeks/{week_id}/scores?saved={match_count}"
    return f"/weeks/{week_id}/scores?saved=1"


async def submit(request: Request, session: Session, draft_id: int, user: User, flow: Flow, *,
                 submit_card: Callable, delete_draft: Callable) -> Response:
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    draft = open_draft(request, session, draft_id, user, flow)
    if draft.status == "extracting":
        raise HTTPException(status_code=409, detail="The photo is still being read.", headers=PRIVATE_HEADERS)
    week_id = draft.week_id
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
        return review_page(request, session, draft, flow, values=values, errors=errors, status_code=422)

    today, now, directory = _today(request), _now(request), request.app.state.scorecard_dir
    names = {option.golfer_id: option.name for option in options}
    # End the read transaction: `submit_card` reserves the single writer as its
    # first action and re-checks everything inside that one write transaction.
    session.rollback()
    try:
        result = submit_card(session, week_id, list(submission.entries), _actor(user, flow), today,
                             acknowledged=acknowledged, left_off=submission.left_off, now=now)
        stored = session.get(ScorecardDraft, draft_id)
        if stored is None:
            session.rollback()
            raise _not_found()
        # Success deletes the draft and its photo in the same request and transaction.
        delete_draft(session, stored, directory=directory)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code in ("not_found", "not_allowed"):
            raise _not_found() from exc
        status_code, errors = _refusal(exc, names)
        kept = open_draft(request, session, draft_id, user, flow)  # a refused submit keeps the draft
        return review_page(request, session, kept, flow, values=values, errors=errors, status_code=status_code)
    except (SQLAlchemyError, OSError) as exc:
        # The database or the photo could not be written. No score was saved;
        # the form comes back with what was typed instead of an error page.
        session.rollback()
        logger.warning("scorecard submit failed draft=%d type=%s", draft_id, type(exc).__name__)
        kept = open_draft(request, session, draft_id, user, flow)
        return review_page(request, session, kept, flow, values=values, status_code=503, errors={
            CARD_ERROR: "The scores could not be saved just now. Nothing was saved; submit the card again."})
    logger.info("scorecard submitted draft=%d week=%d", draft_id, week_id)
    return RedirectResponse(_saved_path(flow, week_id, len(result.player_match_ids)), status_code=303,
                            headers=PRIVATE_HEADERS)


def _discarded_path(flow: Flow, week_id: int, retry: bool) -> str:
    if flow.is_admin:
        return f"/admin/scorecards/new?week_id={week_id}" if retry else "/admin/scorecards/new?discarded=1"
    return f"/weeks/{week_id}/scores/enter?discarded=1"


async def discard(request: Request, session: Session, draft_id: int, user: User, flow: Flow) -> Response:
    """Delete a draft and its photo; an expired draft can still be discarded."""
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    draft = get_draft(session, draft_id)
    if draft is None or not _owned(draft, user, flow):
        raise _not_found()
    week_id = draft.week_id
    discard_draft(session, draft_id, directory=request.app.state.scorecard_dir)
    return RedirectResponse(_discarded_path(flow, week_id, _text(form, "retry") == "1"), status_code=303,
                            headers=PRIVATE_HEADERS)
