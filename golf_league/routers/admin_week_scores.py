"""Admin pages for a week's scores: the week sheet, one match, lock, unlock and clear (R-SCORES).

The week sheet shows every match of a week, takes the scores still missing in
one save, and lets an admin confirm what is saved by locking it. A saved
match is changed, locked, unlocked or cleared from its own page. Every route
requires a verified admin, every POST a CSRF token, and every write goes
through the score service, which refuses a locked match and a form that is
out of date. Responses are `Cache-Control: no-store`, and nothing here logs a
name or a score.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from golf_league.database import get_session
from golf_league.domain.scorecard import Actor
from golf_league.models import Week
from golf_league.routers.scorecard_flow import (
    PRIVATE_HEADERS,
    _csrf,
    _now,
    _page,
    _settings,
    _text,
    _today,
)
from golf_league.security import require_admin
from golf_league.services.match_entry import (
    any_typed,
    parse_revision,
    parse_scores,
    revision_field,
    score_field,
    sheet_score_field,
)
from golf_league.services.scorecard_vision import league_timezone_name
from golf_league.services.scores import (
    ScoreEntryError,
    SheetMatch,
    clear_match_scores,
    lock_match,
    lock_week,
    save_match_scores,
    save_week,
    unlock_match,
    week_holes,
    week_sheet,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SHEET_FORM = "sheet-form"
_OUT_OF_DATE = ("These scores were changed after you opened this page. Nothing was saved. "
                "The saved scores are shown below; make your change again.")
_SAVE_AGAIN = "Scores were being changed at the same moment. Nothing was saved; save again."
_NOT_SAVED = "The scores could not be saved just now. Nothing was saved; save again."
# The form parser's own limit; a week sheet posts every input of every match, so it may need more.
_FORM_FIELDS_FLOOR = 1000


def _sheet_path(week_id: int) -> str:
    return f"/admin/weeks/{week_id}/scores"


def _match_path(week_id: int, player_match_id: int) -> str:
    return f"/admin/weeks/{week_id}/matches/{player_match_id}/scores"


def _redirect(week_id: int, query: str, player_match_id: int | None = None) -> Response:
    anchor = f"#match-{player_match_id}" if player_match_id is not None else ""
    return RedirectResponse(f"{_sheet_path(week_id)}?{query}{anchor}", status_code=303, headers=PRIVATE_HEADERS)


def _field_limit(match_count: int, hole_count: int) -> int:
    """How many fields a week's forms may post: bounded by the week's own size.

    The sheet posts, for every match, two golfers' scores and a revision
    (blank inputs included), so a large week passes the parser's default.
    """
    return max(_FORM_FIELDS_FLOOR, match_count * (2 * hole_count + 1) + 8)


def _week(session: Session, week_id: int) -> Week:
    week = session.get(Week, week_id)
    if week is None:
        raise HTTPException(status_code=404, detail="Not found")
    return week


def _sheet_match(session: Session, week_id: int, player_match_id: int) -> SheetMatch:
    """The match as it is saved now, or 404 when it is not in the week of the address."""
    for match in week_sheet(session, week_id):
        if match.player_match_id == player_match_id:
            return match
    raise HTTPException(status_code=404, detail="Not found")


def _day(request: Request, moment: datetime | None) -> str:
    if moment is None:
        return ""
    zone = ZoneInfo(league_timezone_name(_settings(request)))
    return moment.astimezone(zone).strftime("%b %-d, %Y")


def _takes_scores(week: Week) -> bool:
    return week.status == "scheduled" and week.week_type == "match"


def _closed_reason(week: Week) -> str:
    """Why a week takes no scores, for the page; empty when it does."""
    if week.status != "scheduled":
        return f"This week is {week.status}."
    return "" if week.week_type == "match" else "This week is not a match week."


def _match_view(request: Request, match: SheetMatch, hole_numbers: list[int], *, input_ids: set[int],
                field, values: dict[str, str], invalid: set[str]) -> dict:
    """One match for a template: its state in words and a row per golfer.

    A golfer in `input_ids` gets nine inputs; anyone else is shown read-only.
    """
    rows = []
    for golfer in match.golfers:
        inputs = None
        if golfer.golfer_id in input_ids:
            inputs = [{
                "hole": hole, "field": field(golfer.golfer_id, hole),
                "value": values.get(field(golfer.golfer_id, hole), ""),
                "invalid": field(golfer.golfer_id, hole) in invalid,
            } for hole in hole_numbers]
        rows.append({"golfer": golfer, "inputs": inputs})
    entered = "Not entered" if match.empty else ("Entered" if match.complete else "One of two players entered")
    return {
        "match": match, "rows": rows, "entered": entered, "has_inputs": bool(input_ids),
        "lock_line": (f"Locked on {_day(request, match.locked_at)} by {match.locked_by_name}"
                      if match.locked else "Not locked"),
        "changed_line": (f"Last changed by {match.scores_changed_by_name} on "
                         f"{_day(request, match.scores_changed_at)}" if match.scores_changed_at else ""),
    }


def _name(match: SheetMatch) -> str:
    return f"Match {match.position_label}: {match.golfers[0].name} and {match.golfers[1].name}"


# --- the week sheet ---------------------------------------------------------


def _missing(match: SheetMatch) -> list[int]:
    """The golfers of an unlocked match who have no saved scores: the sheet's inputs."""
    if match.locked:
        return []
    return [golfer.golfer_id for golfer in match.golfers if not golfer.strokes_by_hole]


def _sheet_notices(request: Request, sheet: list[SheetMatch]) -> list[str]:
    """What the last action did, read back from the address after its redirect."""
    query = request.query_params
    by_id = {match.player_match_id: match for match in sheet}
    notices = []
    saved = parse_revision(query.get("saved", ""))
    if saved:
        notices.append("The scores for 1 match were saved." if saved == 1
                       else f"The scores for {saved} matches were saved.")
    if "locked" in query:
        count = parse_revision(query.get("locked", "")) or 0
        notices.append("1 match was locked." if count == 1 else f"{count} matches were locked.")
    if query.get("unlocked") == "1":
        notices.append("The match was unlocked.")
    if query.get("cleared") == "1":
        notices.append("The match's scores were cleared. It can be entered again.")
    incomplete = parse_revision(query.get("incomplete", ""))
    if incomplete:
        notices.append("1 match is not fully entered and was left open." if incomplete == 1
                       else f"{incomplete} matches are not fully entered and were left open.")
    for part in query.get("changed", "").split("-"):
        match = by_id.get(parse_revision(part) or 0)
        if match is not None:
            notices.append(f"{_name(match)} was changed after you opened the page and was not locked.")
    return notices


def _sheet_page(request: Request, session: Session, week: Week, *, values: dict[str, str] | None = None,
                errors: dict[str, str] | None = None, status_code: int = 200,
                typed_on: dict[int, int | None] | None = None) -> Response:
    """The week sheet. `typed_on` maps a match whose typed `values` are kept to the revision they
    were typed on: the form goes back with that revision, never a newer one, so saving it again is
    still checked against what the person saw."""
    sheet = week_sheet(session, week.id)
    typed_on = typed_on or {}
    hole_numbers = [hole.number for hole in week_holes(session, week.id)]
    values, errors = values or {}, errors or {}
    open_week = _takes_scores(week)
    groups: list[dict] = []
    field_ids: dict[str, str] = {}
    for match in sheet:
        def field(golfer_id: int, hole: int, match_id: int = match.player_match_id) -> str:
            return sheet_score_field(match_id, golfer_id, hole)

        input_ids = set(_missing(match)) if open_week else set()
        view = _match_view(request, match, hole_numbers, input_ids=input_ids, field=field, values=values,
                           invalid=set(errors))
        view["revision_field"] = revision_field(match.player_match_id)
        revision = typed_on.get(match.player_match_id, match.revision)
        view["revision"] = "" if revision is None else revision
        view["base_path"] = f"/admin/weeks/{week.id}/matches/{match.player_match_id}"
        for row in view["rows"]:
            field_ids.update({cell["field"]: cell["field"] for cell in row["inputs"] or []})
        title = f"{match.home_team_name} vs {match.away_team_name}"
        if not groups or groups[-1]["team_match_id"] != match.team_match_id:
            groups.append({"team_match_id": match.team_match_id, "title": title, "matches": []})
        groups[-1]["matches"].append(view)
    return _page(request, "admin/scorecards/scores.html", {
        "week": week, "groups": groups, "hole_numbers": hole_numbers, "sheet": sheet,
        "entered": sum(1 for match in sheet if match.complete),
        "locked": sum(1 for match in sheet if match.locked),
        "has_inputs": any(view["has_inputs"] for group in groups for view in group["matches"]),
        "can_lock_all": any(match.complete and not match.locked for match in sheet),
        "notices": _sheet_notices(request, sheet) if status_code == 200 else [],
        "errors": errors, "field_ids": field_ids, "form_id": SHEET_FORM, "open_week": open_week,
        "closed_reason": _closed_reason(week),
    }, status_code)


@router.get("/admin/weeks/{week_id}/scores")
async def week_scores(week_id: int, request: Request,
                      session: Session = Depends(get_session),  # noqa: B008
                      admin=Depends(require_admin)) -> Response:  # noqa: B008
    return _sheet_page(request, session, _week(session, week_id))


def _refused_sheet(request: Request, session: Session, week_id: int, values: dict[str, str],
                   refused: list[ScoreEntryError], typed_on: dict[int, int | None]) -> Response:
    """The sheet after a save was refused; nothing was written.

    Every match that was changed or locked meanwhile is named and shown as it
    is saved now. What was typed for the other matches is kept, with the
    revision it was typed on.
    """
    week = _week(session, week_id)
    first = refused[0]
    if first.code == "conflict":
        return _sheet_page(request, session, week, values=values, status_code=409, typed_on=typed_on,
                           errors={"sheet": _SAVE_AGAIN})
    if first.code not in ("out_of_date", "locked"):
        return _sheet_page(request, session, week, values=values, status_code=422, typed_on=typed_on,
                           errors={"sheet": first.message})
    kept, kept_on, errors = dict(values), dict(typed_on), {}
    for number, exc in enumerate(refused):
        match = _sheet_match(session, week_id, exc.player_match_id)
        prefix = f"score_{match.player_match_id}_"
        kept = {name: value for name, value in kept.items() if not name.startswith(prefix)}
        kept_on.pop(match.player_match_id, None)
        reason = ("was locked" if exc.code == "locked" else "was changed") + " after you opened this page"
        errors[f"sheet-{number}"] = f"{_name(match)} {reason}."
    if len(refused) == 1:
        errors["sheet-0"] += (" Nothing was saved. It is shown below as it is saved now; "
                              "check it and save the other matches again.")
    else:
        errors["sheet"] = ("Nothing was saved. Those matches are shown below as they are saved now; "
                           "check them and save the other matches again.")
    return _sheet_page(request, session, week, values=kept, status_code=409, typed_on=kept_on, errors=errors)


def _read_sheet(form, sheet: list[SheetMatch], hole_numbers: list[int]):
    """Read a posted week sheet.

    Returns the typed values, the field errors, the matches to save, every
    stale match, and for each match with typed values the revision they were
    typed on. A match left wholly blank is skipped. Scores typed for a golfer
    who has since been saved, or whose match was locked, or typed on a page
    showing another revision of the match, mean the page was out of date:
    each such match is returned as stale, so nothing is saved or silently
    dropped and no form goes back carrying a newer revision than it was typed on.
    """
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    entries = []
    stale: dict[int, ScoreEntryError] = {}
    typed_on: dict[int, int | None] = {}
    for match in sheet:
        def field(golfer_id: int, hole: int, match_id: int = match.player_match_id) -> str:
            return sheet_score_field(match_id, golfer_id, hole)

        match_id, missing = match.player_match_id, _missing(match)
        values.update({field(golfer_id, hole): _text(form, field(golfer_id, hole))
                       for golfer_id in missing for hole in hole_numbers})
        taken = [golfer.golfer_id for golfer in match.golfers if golfer.golfer_id not in missing]
        posted = {field(golfer_id, hole): _text(form, field(golfer_id, hole))
                  for golfer_id in taken for hole in hole_numbers}
        if any_typed(posted, taken, hole_numbers, field):
            stale[match_id] = ScoreEntryError("locked" if match.locked else "out_of_date", "",
                                              player_match_id=match_id)
        if not missing or not any_typed(values, missing, hole_numbers, field):
            continue
        typed_on[match_id] = parse_revision(_text(form, revision_field(match_id)))
        if typed_on[match_id] != match.revision:
            stale.setdefault(match_id, ScoreEntryError("out_of_date", "", player_match_id=match_id))
        golfers = [(golfer.golfer_id, golfer.name) for golfer in match.golfers if golfer.golfer_id in missing]
        scores, problems = parse_scores(values, golfers, hole_numbers, field)
        errors.update(problems)
        # A golfer already saved keeps their scores; the write needs the match whole.
        scores.update({golfer.golfer_id: dict(golfer.strokes_by_hole)
                       for golfer in match.golfers if golfer.golfer_id not in missing})
        entries.append((match_id, scores, typed_on[match_id]))
    return values, errors, entries, list(stale.values()), typed_on


@router.post("/admin/weeks/{week_id}/scores")
async def week_scores_save(week_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    week = _week(session, week_id)
    sheet = week_sheet(session, week_id)
    hole_numbers = [hole.number for hole in week_holes(session, week_id)]
    form = await request.form(max_fields=_field_limit(len(sheet), len(hole_numbers)))
    _csrf(request, form.get("csrf_token", ""))
    values, errors, entries, stale, typed_on = _read_sheet(form, sheet, hole_numbers)
    if stale:
        return _refused_sheet(request, session, week_id, values, stale, typed_on)
    if not errors and not entries:
        errors["sheet"] = "Enter the scores for at least one match."
    if errors:
        return _sheet_page(request, session, week, values=values, errors=errors, status_code=422,
                           typed_on=typed_on)

    actor, today, now = Actor(admin.id, True), _today(request), _now(request)
    session.rollback()  # end the read transaction: the write reserves the single writer first
    try:
        save_week(session, week_id, entries, actor, today, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code == "not_found" and exc.player_match_id is None:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return _refused_sheet(request, session, week_id, values, [exc], typed_on)
    except (SQLAlchemyError, OSError) as exc:
        session.rollback()
        logger.warning("week sheet save failed week=%d type=%s", week_id, type(exc).__name__)
        return _sheet_page(request, session, _week(session, week_id), values=values, status_code=503,
                           typed_on=typed_on, errors={"sheet": _NOT_SAVED})
    logger.info("week sheet saved week=%d matches=%d user=%d", week_id, len(entries), actor.user_id)
    return _redirect(week_id, f"saved={len(entries)}")


# --- one match --------------------------------------------------------------


def _match_page(request: Request, session: Session, week: Week, player_match_id: int, *,
                values: dict[str, str] | None = None, errors: dict[str, str] | None = None,
                status_code: int = 200, typed_on: int | None = None) -> Response:
    """The match page: the saved scores, or `values` as typed on revision `typed_on`.

    Typed values go back with the revision they were typed on, never a newer
    one, so saving them again cannot overwrite a change made in between. A
    page showing typed values offers no lock and no clear: those act on what
    is saved, which is not what such a page shows.
    """
    match = _sheet_match(session, week.id, player_match_id)
    hole_numbers = [hole.number for hole in week_holes(session, week.id)]
    errors = errors or {}
    editable = not match.locked and _takes_scores(week)
    revision, typed = match.revision, values is not None
    if values is None:  # the saved scores, for an edit
        values = {score_field(golfer.golfer_id, hole): str(strokes)
                  for golfer in match.golfers for hole, strokes in golfer.strokes_by_hole.items()}
    else:  # never a revision the typed values were not typed on: blank is refused as out of date
        revision = "" if typed_on is None else typed_on
    view = _match_view(request, match, hole_numbers, field=score_field, values=values, invalid=set(errors),
                       input_ids={golfer.golfer_id for golfer in match.golfers} if editable else set())
    fields = [cell["field"] for row in view["rows"] for cell in row["inputs"] or []]
    return _page(request, "admin/scorecards/match.html", {
        "week": week, "view": view, "match": match, "hole_numbers": hole_numbers, "editable": editable,
        "title": _name(match), "errors": errors, "field_ids": {name: name for name in fields},
        "sheet_path": _sheet_path(week.id), "match_path": _match_path(week.id, player_match_id),
        "open_week": _takes_scores(week), "closed_reason": _closed_reason(week), "revision": revision,
        "typed": typed,
    }, status_code)


@router.get("/admin/weeks/{week_id}/matches/{player_match_id}/scores")
async def match_scores(week_id: int, player_match_id: int, request: Request,
                       session: Session = Depends(get_session),  # noqa: B008
                       admin=Depends(require_admin)) -> Response:  # noqa: B008
    return _match_page(request, session, _week(session, week_id), player_match_id)


def _refused_match(request: Request, session: Session, week_id: int, player_match_id: int,
                   values: dict[str, str] | None, exc: ScoreEntryError, *,
                   typed_on: int | None = None) -> Response:
    """The match page after the score service refused a write; nothing was written."""
    if exc.code == "not_found":
        raise HTTPException(status_code=404, detail="Not found") from exc
    week = _week(session, week_id)
    if exc.code == "out_of_date":
        return _match_page(request, session, week, player_match_id, status_code=409,
                           errors={"match": _OUT_OF_DATE})
    if exc.code == "locked":
        return _match_page(request, session, week, player_match_id, status_code=409,
                           errors={"match": "This match was locked. Nothing was saved."})
    if exc.code == "conflict":
        return _match_page(request, session, week, player_match_id, values=values, status_code=409,
                           errors={"match": _SAVE_AGAIN}, typed_on=typed_on)
    return _match_page(request, session, week, player_match_id, values=values, status_code=422,
                       errors={"match": exc.message}, typed_on=typed_on)


@router.post("/admin/weeks/{week_id}/matches/{player_match_id}/scores")
async def match_scores_save(week_id: int, player_match_id: int, request: Request,
                            session: Session = Depends(get_session),  # noqa: B008
                            admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    week = _week(session, week_id)
    match = _sheet_match(session, week_id, player_match_id)
    hole_numbers = [hole.number for hole in week_holes(session, week_id)]
    golfers = [(golfer.golfer_id, golfer.name) for golfer in match.golfers]
    values = {score_field(golfer_id, hole): _text(form, score_field(golfer_id, hole))
              for golfer_id, _ in golfers for hole in hole_numbers}
    scores, errors = parse_scores(values, golfers, hole_numbers)
    base_revision = parse_revision(_text(form, "revision"))
    # A form with no readable revision, or with mistakes typed on an older page, is out of date
    # before anything else: it goes back with the saved values, never with what was typed.
    if base_revision is None or (errors and not match.locked and base_revision != match.revision):
        return _refused_match(request, session, week_id, player_match_id, None,
                              ScoreEntryError("out_of_date", "", player_match_id=player_match_id))
    if errors and not match.locked:
        return _match_page(request, session, week, player_match_id, values=values, errors=errors,
                           status_code=422, typed_on=base_revision)
    actor, today, now = Actor(admin.id, True), _today(request), _now(request)
    session.rollback()
    try:
        save_match_scores(session, week_id, player_match_id, scores, actor, today,
                          base_revision=base_revision, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        return _refused_match(request, session, week_id, player_match_id, values, exc, typed_on=base_revision)
    except (SQLAlchemyError, OSError) as exc:
        session.rollback()
        logger.warning("match save failed week=%d match=%d type=%s", week_id, player_match_id,
                       type(exc).__name__)
        return _match_page(request, session, _week(session, week_id), player_match_id, values=values,
                           status_code=503, errors={"match": _NOT_SAVED}, typed_on=base_revision)
    logger.info("match saved week=%d match=%d user=%d", week_id, player_match_id, actor.user_id)
    return _redirect(week_id, "saved=1", player_match_id)


# --- lock, unlock, lock all -------------------------------------------------


@router.post("/admin/weeks/{week_id}/matches/{player_match_id}/lock")
async def match_lock(week_id: int, player_match_id: int, request: Request,
                     session: Session = Depends(get_session),  # noqa: B008
                     admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    _week(session, week_id)
    base_revision = parse_revision(_text(form, "revision"))
    actor, now = Actor(admin.id, True), _now(request)
    session.rollback()
    try:
        lock_match(session, week_id, player_match_id, actor, base_revision=base_revision, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code == "out_of_date":
            return _match_page(request, session, _week(session, week_id), player_match_id, status_code=409,
                               errors={"match": "The scores changed after you opened the page. "
                                                "Check them and lock again."})
        return _refused_match(request, session, week_id, player_match_id, None, exc)
    logger.info("match locked week=%d match=%d user=%d", week_id, player_match_id, actor.user_id)
    return _redirect(week_id, "locked=1", player_match_id)


@router.post("/admin/weeks/{week_id}/matches/{player_match_id}/unlock")
async def match_unlock(week_id: int, player_match_id: int, request: Request,
                       session: Session = Depends(get_session),  # noqa: B008
                       admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    _week(session, week_id)
    actor, now = Actor(admin.id, True), _now(request)
    session.rollback()
    try:
        unlock_match(session, week_id, player_match_id, actor, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        return _refused_match(request, session, week_id, player_match_id, None, exc)
    logger.info("match unlocked week=%d match=%d user=%d", week_id, player_match_id, actor.user_id)
    return _redirect(week_id, "unlocked=1", player_match_id)


@router.post("/admin/weeks/{week_id}/scores/lock-all")
async def week_lock_all(week_id: int, request: Request,
                        session: Session = Depends(get_session),  # noqa: B008
                        admin=Depends(require_admin)) -> Response:  # noqa: B008
    _week(session, week_id)
    sheet = week_sheet(session, week_id)
    form = await request.form(max_fields=_field_limit(len(sheet), 0))
    _csrf(request, form.get("csrf_token", ""))
    revisions = {}
    for match in sheet:
        shown = parse_revision(_text(form, revision_field(match.player_match_id)))
        if shown is not None:
            revisions[match.player_match_id] = shown
    actor, now = Actor(admin.id, True), _now(request)
    session.rollback()
    try:
        result = lock_week(session, week_id, actor, revisions, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code == "conflict":
            return _sheet_page(request, session, _week(session, week_id), status_code=409,
                               errors={"sheet": "Scores were being changed at the same moment. "
                                                "Nothing was locked; try again."})
        raise HTTPException(status_code=404, detail="Not found") from exc
    logger.info("week lock-all week=%d locked=%d incomplete=%d changed=%d user=%d", week_id,
                len(result.locked), len(result.incomplete), len(result.changed), actor.user_id)
    query = f"locked={len(result.locked)}&incomplete={len(result.incomplete)}"
    if result.changed:
        query += "&changed=" + "-".join(str(match_id) for match_id in result.changed)
    return _redirect(week_id, query)


# --- clearing ---------------------------------------------------------------


def _clear_page(request: Request, session: Session, week: Week, match: SheetMatch, *,
                errors: dict[str, str] | None = None, status_code: int = 200) -> Response:
    hole_numbers = [hole.number for hole in week_holes(session, week.id)]
    return _page(request, "admin/scorecards/clear.html", {
        "week": week, "match": match, "hole_numbers": hole_numbers, "title": _name(match),
        "view": _match_view(request, match, hole_numbers, input_ids=set(), field=score_field, values={},
                            invalid=set()),
        "errors": errors or {}, "field_ids": {}, "sheet_path": _sheet_path(week.id),
        "match_path": _match_path(week.id, match.player_match_id),
    }, status_code)


@router.get("/admin/weeks/{week_id}/matches/{player_match_id}/scores/clear")
async def match_clear_confirm(week_id: int, player_match_id: int, request: Request,
                              session: Session = Depends(get_session),  # noqa: B008
                              admin=Depends(require_admin)) -> Response:  # noqa: B008
    week = _week(session, week_id)
    match = _sheet_match(session, week_id, player_match_id)
    if match.empty:
        raise HTTPException(status_code=404, detail="Not found")
    if match.locked:
        return _match_page(request, session, week, player_match_id, status_code=409,
                           errors={"match": "Unlock the match first."})
    return _clear_page(request, session, week, match)


@router.post("/admin/weeks/{week_id}/matches/{player_match_id}/scores/clear")
async def match_clear(week_id: int, player_match_id: int, request: Request,
                      session: Session = Depends(get_session),  # noqa: B008
                      admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    _week(session, week_id)
    base_revision = parse_revision(_text(form, "revision"))
    actor, now = Actor(admin.id, True), _now(request)
    session.rollback()
    try:
        deleted = clear_match_scores(session, week_id, player_match_id, actor, base_revision=base_revision,
                                     now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code == "locked":
            return _match_page(request, session, _week(session, week_id), player_match_id, status_code=409,
                               errors={"match": "Unlock the match first."})
        if exc.code == "out_of_date":
            return _match_page(request, session, _week(session, week_id), player_match_id, status_code=409,
                               errors={"match": "The scores changed after you opened the page. Nothing was "
                                                "cleared. Check them before clearing."})
        return _refused_match(request, session, week_id, player_match_id, None, exc)
    logger.info("match cleared week=%d match=%d rows=%d user=%d", week_id, player_match_id, deleted,
                actor.user_id)
    return _redirect(week_id, "cleared=1", player_match_id)
