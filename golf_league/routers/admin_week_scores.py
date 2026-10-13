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
from golf_league.routers.admin_scorecards import (
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


def _sheet_path(week_id: int) -> str:
    return f"/admin/weeks/{week_id}/scores"


def _match_path(week_id: int, player_match_id: int) -> str:
    return f"/admin/weeks/{week_id}/matches/{player_match_id}/scores"


def _redirect(week_id: int, query: str, player_match_id: int | None = None) -> Response:
    anchor = f"#match-{player_match_id}" if player_match_id is not None else ""
    return RedirectResponse(f"{_sheet_path(week_id)}?{query}{anchor}", status_code=303, headers=PRIVATE_HEADERS)


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
    return week.status == "scheduled"


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
                errors: dict[str, str] | None = None, status_code: int = 200) -> Response:
    sheet = week_sheet(session, week.id)
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
    }, status_code)


@router.get("/admin/weeks/{week_id}/scores")
async def week_scores(week_id: int, request: Request,
                      session: Session = Depends(get_session),  # noqa: B008
                      admin=Depends(require_admin)) -> Response:  # noqa: B008
    return _sheet_page(request, session, _week(session, week_id))


def _refused_sheet(request: Request, session: Session, week_id: int, values: dict[str, str],
                   exc: ScoreEntryError) -> Response:
    """The sheet after the score service refused a save; nothing was written."""
    week = _week(session, week_id)
    if exc.code in ("out_of_date", "locked"):
        match = _sheet_match(session, week_id, exc.player_match_id)
        # That match is shown as it is saved now; what was typed for the others is kept.
        prefix = f"score_{match.player_match_id}_"
        kept = {name: value for name, value in values.items() if not name.startswith(prefix)}
        reason = ("was locked" if exc.code == "locked" else "was changed") + " after you opened this page"
        return _sheet_page(request, session, week, values=kept, status_code=409, errors={
            "sheet": f"{_name(match)} {reason}. Nothing was saved. It is shown below as it is saved now; "
                     "check it and save the other matches again."})
    if exc.code == "conflict":
        return _sheet_page(request, session, week, values=values, status_code=409, errors={"sheet": _SAVE_AGAIN})
    return _sheet_page(request, session, week, values=values, status_code=422, errors={"sheet": exc.message})


def _read_sheet(form, sheet: list[SheetMatch], hole_numbers: list[int]):
    """Read a posted week sheet: typed values, field errors, the matches to save, and a stale match.

    A match left wholly blank is skipped. Scores typed for a golfer who has
    since been saved, or whose match was locked, mean the page was out of
    date: that match is returned as stale so nothing is saved or silently dropped.
    """
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    entries = []
    stale: ScoreEntryError | None = None
    for match in sheet:
        def field(golfer_id: int, hole: int, match_id: int = match.player_match_id) -> str:
            return sheet_score_field(match_id, golfer_id, hole)

        missing = _missing(match)
        values.update({field(golfer_id, hole): _text(form, field(golfer_id, hole))
                       for golfer_id in missing for hole in hole_numbers})
        taken = [golfer.golfer_id for golfer in match.golfers if golfer.golfer_id not in missing]
        posted = {field(golfer_id, hole): _text(form, field(golfer_id, hole))
                  for golfer_id in taken for hole in hole_numbers}
        if stale is None and any_typed(posted, taken, hole_numbers, field):
            stale = ScoreEntryError("locked" if match.locked else "out_of_date", "",
                                    player_match_id=match.player_match_id)
        if not missing or not any_typed(values, missing, hole_numbers, field):
            continue
        golfers = [(golfer.golfer_id, golfer.name) for golfer in match.golfers if golfer.golfer_id in missing]
        scores, problems = parse_scores(values, golfers, hole_numbers, field)
        errors.update(problems)
        # A golfer already saved keeps their scores; the write needs the match whole.
        scores.update({golfer.golfer_id: dict(golfer.strokes_by_hole)
                       for golfer in match.golfers if golfer.golfer_id not in missing})
        entries.append((match.player_match_id, scores,
                        parse_revision(_text(form, revision_field(match.player_match_id)))))
    return values, errors, entries, stale


@router.post("/admin/weeks/{week_id}/scores")
async def week_scores_save(week_id: int, request: Request,
                           session: Session = Depends(get_session),  # noqa: B008
                           admin=Depends(require_admin)) -> Response:  # noqa: B008
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    week = _week(session, week_id)
    hole_numbers = [hole.number for hole in week_holes(session, week_id)]
    values, errors, entries, stale = _read_sheet(form, week_sheet(session, week_id), hole_numbers)
    if stale is not None:
        return _refused_sheet(request, session, week_id, values, stale)
    if not errors and not entries:
        errors["sheet"] = "Enter the scores for at least one match."
    if errors:
        return _sheet_page(request, session, week, values=values, errors=errors, status_code=422)

    actor, today, now = Actor(admin.id, True), _today(request), _now(request)
    session.rollback()  # end the read transaction: the write reserves the single writer first
    try:
        save_week(session, week_id, entries, actor, today, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        if exc.code == "not_found" and exc.player_match_id is None:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return _refused_sheet(request, session, week_id, values, exc)
    except (SQLAlchemyError, OSError) as exc:
        session.rollback()
        logger.warning("week sheet save failed week=%d type=%s", week_id, type(exc).__name__)
        return _sheet_page(request, session, _week(session, week_id), values=values, status_code=503,
                           errors={"sheet": _NOT_SAVED})
    logger.info("week sheet saved week=%d matches=%d user=%d", week_id, len(entries), actor.user_id)
    return _redirect(week_id, f"saved={len(entries)}")


# --- one match --------------------------------------------------------------


def _match_page(request: Request, session: Session, week: Week, player_match_id: int, *,
                values: dict[str, str] | None = None, errors: dict[str, str] | None = None,
                status_code: int = 200) -> Response:
    match = _sheet_match(session, week.id, player_match_id)
    hole_numbers = [hole.number for hole in week_holes(session, week.id)]
    errors = errors or {}
    editable = not match.locked and _takes_scores(week)
    if values is None:  # the saved scores, for an edit
        values = {score_field(golfer.golfer_id, hole): str(strokes)
                  for golfer in match.golfers for hole, strokes in golfer.strokes_by_hole.items()}
    view = _match_view(request, match, hole_numbers, field=score_field, values=values, invalid=set(errors),
                       input_ids={golfer.golfer_id for golfer in match.golfers} if editable else set())
    fields = [cell["field"] for row in view["rows"] for cell in row["inputs"] or []]
    return _page(request, "admin/scorecards/match.html", {
        "week": week, "view": view, "match": match, "hole_numbers": hole_numbers, "editable": editable,
        "title": _name(match), "errors": errors, "field_ids": {name: name for name in fields},
        "sheet_path": _sheet_path(week.id), "match_path": _match_path(week.id, player_match_id),
        "open_week": _takes_scores(week),
    }, status_code)


@router.get("/admin/weeks/{week_id}/matches/{player_match_id}/scores")
async def match_scores(week_id: int, player_match_id: int, request: Request,
                       session: Session = Depends(get_session),  # noqa: B008
                       admin=Depends(require_admin)) -> Response:  # noqa: B008
    return _match_page(request, session, _week(session, week_id), player_match_id)


def _refused_match(request: Request, session: Session, week_id: int, player_match_id: int,
                   values: dict[str, str] | None, exc: ScoreEntryError) -> Response:
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
                           errors={"match": _SAVE_AGAIN})
    return _match_page(request, session, week, player_match_id, values=values, status_code=422,
                       errors={"match": exc.message})


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
    if errors and not match.locked:
        return _match_page(request, session, week, player_match_id, values=values, errors=errors,
                           status_code=422)
    actor, today, now = Actor(admin.id, True), _today(request), _now(request)
    base_revision = parse_revision(_text(form, "revision"))
    session.rollback()
    try:
        save_match_scores(session, week_id, player_match_id, scores, actor, today,
                          base_revision=base_revision, now=now)
        session.commit()
    except ScoreEntryError as exc:
        session.rollback()
        return _refused_match(request, session, week_id, player_match_id, values, exc)
    except (SQLAlchemyError, OSError) as exc:
        session.rollback()
        logger.warning("match save failed week=%d match=%d type=%s", week_id, player_match_id,
                       type(exc).__name__)
        return _match_page(request, session, _week(session, week_id), player_match_id, values=values,
                           status_code=503, errors={"match": _NOT_SAVED})
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
    form = await request.form()
    _csrf(request, form.get("csrf_token", ""))
    _week(session, week_id)
    revisions = {}
    for match in week_sheet(session, week_id):
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
